from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from io import BytesIO
import re
from typing import Any
from urllib.parse import urlparse
from collections.abc import Callable, Mapping, Sequence

from PIL import Image
import requests

from audiobookdl import logging
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookMetadata,
    Chapter,
    Cover,
    clean_html_description,
    parse_language,
    parse_release_date,
    parse_series_order,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
    default_enrichment_cache_path,
    make_cache_key,
)
from audiobookdl.utils.http import DEFAULT_USER_AGENT


DEFAULT_PROVIDERS: tuple[str, ...] = ("audnexus", "itunes", "google", "openlibrary")
CUSTOM_PROVIDER_PREFIX = "custom:"
PROVIDER_ALIASES: dict[str, str] = {
    "audnexus": "audnexus",
    "audible": "audnexus",
    "itunes": "itunes",
    "apple": "itunes",
    "google": "google",
    "google_books": "google",
    "googlebooks": "google",
    "openlibrary": "openlibrary",
    "ol": "openlibrary",
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.65

_CUSTOM_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_EDITION_MARKER_RE = re.compile(
    r"\s*(?:\([^)]*\b(?:unabridged|abridged|audiobook|edition|annotated|illustrated)\b[^)]*\)"
    r"|\[[^\]]*\b(?:unabridged|abridged|audiobook|edition)\b[^\]]*\])\s*",
    re.IGNORECASE,
)
_SUBTITLE_NOVEL_RE = re.compile(r"\s*[:\-]\s*a\s+novel\b.*$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_ISBN_CLEAN_RE = re.compile(r"[^0-9Xx]")
_ASIN_RE = re.compile(r"^[A-Za-z0-9]{10}$")
_ITUNES_ARTWORK_SIZE_RE = re.compile(r"/\d+x\d+bb[^/]*$")


@dataclass
class EnrichmentCandidate:
    """Normalized metadata match candidate produced by an enrichment provider."""

    provider: str
    score: float
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: str | None = None
    series_order: int | None = None
    genres: list[str] = field(default_factory=list)
    language: Any | None = None
    description: str | None = None
    isbn: str | None = None
    asin: str | None = None
    publisher: str | None = None
    release_date: date | None = None
    cover_url: str | None = None
    chapters: list[Chapter] = field(default_factory=list)
    raw_id: str | None = None


@dataclass
class EnrichmentResult:
    """Summary of metadata, cover, and chapter updates applied by enrichment."""

    updated: bool = False
    providers_used: list[str] = field(default_factory=list)
    matched_candidates: list[EnrichmentCandidate] = field(default_factory=list)
    fields_updated: list[str] = field(default_factory=list)
    cover_updated: bool = False
    chapters_updated: bool = False


CandidateSelector = Callable[[str, Sequence[EnrichmentCandidate]], int | None]


def normalize_custom_provider_name(raw: str) -> str:
    """Validate and normalize a custom metadata provider slug."""
    cleaned = str(raw or "").strip().lower()
    if cleaned.startswith(CUSTOM_PROVIDER_PREFIX):
        cleaned = cleaned[len(CUSTOM_PROVIDER_PREFIX) :].strip()
    if not cleaned or not _CUSTOM_PROVIDER_NAME_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid custom provider name {raw!r}; use lowercase letters, digits, '-' or '_'"
        )
    if cleaned in PROVIDER_ALIASES:
        raise ValueError(
            f"Custom provider name '{cleaned}' conflicts with built-in provider name"
        )
    return cleaned


def validate_custom_provider_url(raw: str) -> str:
    """Validate and normalize a base HTTP(S) URL for an ABS custom metadata provider."""
    cleaned = str(raw or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Invalid custom provider URL {raw!r}; expected http:// or https:// URL"
        )
    return cleaned


def _lookup_custom_provider(
    slug: str,
    custom_providers: Mapping[str, Any] | None,
) -> Any | None:
    if not custom_providers:
        return None
    if slug in custom_providers:
        return custom_providers[slug]
    lowered = slug.lower()
    for key, val in custom_providers.items():
        if str(key).strip().lower() == lowered:
            return val
    return None


def parse_provider_list(
    raw: str | None,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Parse a comma-separated provider list into canonical deduplicated provider identifiers."""
    if raw is None or not raw.strip():
        return DEFAULT_PROVIDERS
    resolved: list[str] = []
    seen = set()
    for token in raw.split(","):
        cleaned = token.strip().lower()
        if not cleaned:
            continue
        canonical = PROVIDER_ALIASES.get(cleaned)
        if canonical is None:
            if cleaned.startswith(CUSTOM_PROVIDER_PREFIX):
                slug = normalize_custom_provider_name(cleaned)
                if custom_providers is not None and _lookup_custom_provider(slug, custom_providers) is None:
                    raise ValueError(
                        f"Unknown custom enrichment provider '{token.strip()}'. "
                        f"Configured custom providers: {', '.join(custom_providers.keys()) or 'none'}"
                    )
                canonical = f"{CUSTOM_PROVIDER_PREFIX}{slug}"
            elif custom_providers is not None:
                slug = cleaned
                if _lookup_custom_provider(slug, custom_providers) is not None:
                    canonical = f"{CUSTOM_PROVIDER_PREFIX}{slug}"
        if canonical is None:
            valid = list(DEFAULT_PROVIDERS) + [
                f"{CUSTOM_PROVIDER_PREFIX}{k}" for k in (custom_providers or {})
            ]
            raise ValueError(
                f"Unknown enrichment provider '{token.strip()}'. Valid providers: {', '.join(valid)}"
            )
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    if not resolved:
        raise ValueError("At least one enrichment provider must be specified")
    return tuple(resolved)


def resolve_provider_list(
    raw: str | None = None,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return explicit `raw` providers, or `DEFAULT_PROVIDERS` plus enabled custom providers."""
    if raw is not None and raw.strip():
        return parse_provider_list(raw, custom_providers=custom_providers)
    if not custom_providers:
        return DEFAULT_PROVIDERS
    resolved: list[str] = list(DEFAULT_PROVIDERS)
    for key, cfg in custom_providers.items():
        if not getattr(cfg, "enabled", True):
            continue
        name = getattr(cfg, "name", None) or key
        try:
            slug = normalize_custom_provider_name(str(name))
        except ValueError:
            continue
        token = f"{CUSTOM_PROVIDER_PREFIX}{slug}"
        if token not in resolved:
            resolved.append(token)
    return tuple(resolved)


def resolve_redirect_url(
    url: str,
    *,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Follow HTTP redirects on short/share links to obtain the canonical target URL."""
    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        return url
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    session_cm = nullcontext(session) if session is not None else requests.Session()
    try:
        with session_cm as sess:
            resp = sess.head(
                cleaned,
                allow_redirects=True,
                timeout=timeout,
                proxies=proxies,
                headers=headers,
            )
            if resp.status_code in (403, 405) or resp.url == cleaned:
                resp = sess.get(
                    cleaned,
                    allow_redirects=True,
                    stream=True,
                    timeout=timeout,
                    proxies=proxies,
                    headers=headers,
                )
                try:
                    final_url = resp.url or cleaned
                finally:
                    resp.close()
                return final_url
            return resp.url or cleaned
    except requests.RequestException:
        return url


def _clean_isbn(raw: Any) -> str | None:
    if raw is None:
        return None
    candidate = _ISBN_CLEAN_RE.sub("", str(raw)).upper()
    if len(candidate) in (10, 13):
        return candidate
    return None


def _clean_asin(raw: Any) -> str | None:
    if raw is None:
        return None
    candidate = str(raw).strip().upper()
    if _ASIN_RE.match(candidate):
        return candidate
    return None


def _extract_names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            raw_name = item.get("name") or item.get("title") or ""
        else:
            raw_name = item
        cleaned = str(raw_name).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            names.append(cleaned)
    return names


def _normalize_title(text: str) -> str:
    cleaned = _EDITION_MARKER_RE.sub(" ", text)
    cleaned = _SUBTITLE_NOVEL_RE.sub("", cleaned)
    cleaned = cleaned.replace("&", " and ").lower()
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _normalize_person(text: str) -> str:
    cleaned = text.replace("&", " and ").lower()
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def score_candidate_match(metadata: AudiobookMetadata, candidate: EnrichmentCandidate) -> float:
    """
    Compute a confidence score in `[0.0, 1.0]` measuring how closely
    `candidate` matches `metadata`.
    """
    q_asin = _clean_asin(metadata.asin)
    c_asin = _clean_asin(candidate.asin)
    if q_asin and c_asin and q_asin == c_asin:
        return 1.0

    q_isbn = _clean_isbn(metadata.isbn)
    c_isbn = _clean_isbn(candidate.isbn)
    if q_isbn and c_isbn and q_isbn == c_isbn:
        return 1.0

    q_title = _normalize_title(metadata.title or "")
    c_title = _normalize_title(candidate.title or "")
    if not q_title or not c_title:
        return 0.0

    if q_title == c_title:
        title_score = 1.0
    else:
        title_score = SequenceMatcher(None, q_title, c_title).ratio()
        if c_title.startswith(q_title + " ") or q_title.startswith(c_title + " "):
            title_score = max(title_score, 0.88)

    q_authors = [_normalize_person(a) for a in metadata.authors if _normalize_person(a)]
    c_authors = [_normalize_person(a) for a in candidate.authors if _normalize_person(a)]
    if q_authors and c_authors:
        best_author = 0.0
        for qa in q_authors:
            qa_tokens = set(qa.split())
            for ca in c_authors:
                seq = SequenceMatcher(None, qa, ca).ratio()
                ca_tokens = set(ca.split())
                if qa_tokens and ca_tokens:
                    overlap = len(qa_tokens & ca_tokens) / max(len(qa_tokens), len(ca_tokens))
                    seq = max(seq, overlap)
                if seq > best_author:
                    best_author = seq
        score = 0.65 * title_score + 0.35 * best_author
    elif q_authors and not c_authors:
        score = 0.85 * title_score
    else:
        score = title_score

    if (
        metadata.release_date is not None
        and candidate.release_date is not None
        and metadata.release_date.year == candidate.release_date.year
    ):
        score = min(1.0, score + 0.04)

    return round(max(0.0, min(1.0, score)), 4)


def _cached_get_json(
    session: requests.Session,
    provider: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> Any | None:
    cache_key = make_cache_key(provider, url, params)
    if cache is not None and use_cache:
        cached = cache.get(cache_key, ttl_seconds=cache_ttl_seconds)
        if cached is not None:
            return cached

    proxies = {"http": proxy, "https": proxy} if proxy else None
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        req_headers.update(headers)
    try:
        resp = session.get(
            url,
            params=params,
            timeout=timeout,
            proxies=proxies,
            headers=req_headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logging.debug(f"Enrichment request failed ({provider} {url}): {exc}")
        return None

    if cache is not None and isinstance(payload, (dict, list)):
        cache.set(cache_key, provider, payload)
    return payload


def _parse_audnexus_book(payload: Mapping[str, Any]) -> EnrichmentCandidate | None:
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(payload.get("asin"))
    authors = _extract_names(payload.get("authors"))
    narrators = _extract_names(payload.get("narrators"))

    genres: list[str] = []
    raw_genres = payload.get("genres")
    if isinstance(raw_genres, list):
        for g in raw_genres:
            if isinstance(g, dict):
                g_name = str(g.get("name") or "").strip()
            else:
                g_name = str(g).strip()
            if g_name and g_name not in genres:
                genres.append(g_name)

    series_name: str | None = None
    series_order: int | None = None
    series_primary = payload.get("seriesPrimary")
    if isinstance(series_primary, dict):
        raw_series = str(series_primary.get("name") or "").strip()
        series_name = raw_series or None
        series_order = parse_series_order(series_primary.get("position"))

    description = clean_html_description(
        payload.get("summary") or payload.get("description"),
        preserve_newlines=True,
    )
    cover_url = str(payload.get("image") or "").strip() or None
    publisher = str(payload.get("publisherName") or "").strip() or None
    release_date = parse_release_date(payload.get("releaseDate"))
    language = parse_language(payload.get("language"))
    isbn = _clean_isbn(payload.get("isbn"))

    return EnrichmentCandidate(
        provider="audnexus",
        score=0.0,
        title=title,
        authors=authors,
        narrators=narrators,
        series=series_name,
        series_order=series_order,
        genres=genres[:5],
        language=language,
        description=description,
        isbn=isbn,
        asin=asin,
        publisher=publisher,
        release_date=release_date,
        cover_url=cover_url,
        raw_id=asin,
    )


def _parse_audible_product(product: Mapping[str, Any]) -> EnrichmentCandidate | None:
    title = str(product.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(product.get("asin"))
    authors = _extract_names(product.get("authors"))
    narrators = _extract_names(product.get("narrators"))

    series_name: str | None = None
    series_order: int | None = None
    series_list = product.get("series")
    if isinstance(series_list, list) and series_list and isinstance(series_list[0], dict):
        series_name = str(series_list[0].get("title") or "").strip() or None
        series_order = parse_series_order(series_list[0].get("sequence"))

    cover_url: str | None = None
    images = product.get("product_images")
    if isinstance(images, dict) and images:
        for size_key in ("1024", "900", "500"):
            if images.get(size_key):
                cover_url = str(images[size_key]).strip()
                break
        if cover_url is None:
            first_val = next(iter(images.values()))
            if first_val:
                cover_url = str(first_val).strip()

    description = clean_html_description(
        product.get("publisher_summary") or product.get("merchandising_summary"),
        preserve_newlines=True,
    )
    publisher = str(product.get("publisher_name") or "").strip() or None
    release_date = parse_release_date(product.get("release_date"))
    language = parse_language(product.get("language"))

    return EnrichmentCandidate(
        provider="audnexus",
        score=0.0,
        title=title,
        authors=authors,
        narrators=narrators,
        series=series_name,
        series_order=series_order,
        language=language,
        description=description,
        asin=asin,
        publisher=publisher,
        release_date=release_date,
        cover_url=cover_url,
        raw_id=asin,
    )


def _fetch_audnexus_chapters(
    session: requests.Session,
    asin: str,
    *,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> list[Chapter]:
    payload = _cached_get_json(
        session,
        "audnexus",
        f"https://api.audnex.us/books/{asin}/chapters",
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(payload, dict):
        return []
    raw_chapters = payload.get("chapters")
    if not isinstance(raw_chapters, list):
        return []
    chapters: list[Chapter] = []
    for item in raw_chapters:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        start_ms = item.get("startOffsetMs")
        if start_ms is None and item.get("startOffsetSec") is not None:
            try:
                start_ms = int(float(item["startOffsetSec"]) * 1000)
            except (TypeError, ValueError):
                start_ms = None
        if title and start_ms is not None:
            try:
                chapters.append(Chapter(start=max(0, int(start_ms)), title=title))
            except (TypeError, ValueError):
                continue
    return chapters


def _query_audnexus(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[EnrichmentCandidate]:
    direct_asin = _clean_asin(metadata.asin)
    if direct_asin:
        book_payload = _cached_get_json(
            session,
            "audnexus",
            f"https://api.audnex.us/books/{direct_asin}",
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        if isinstance(book_payload, dict):
            cand = _parse_audnexus_book(book_payload)
            if cand is not None:
                cand.score = score_candidate_match(metadata, cand)
                return [cand]
        return []

    if not metadata.title:
        return []

    search_params: dict[str, Any] = {
        "title": metadata.title,
        "num_results": 5,
        "products_sort_by": "Relevance",
        "response_groups": "contributors,media,product_attrs,product_desc,series",
    }
    if metadata.authors:
        search_params["author"] = metadata.authors[0]

    catalog_payload = _cached_get_json(
        session,
        "audnexus",
        "https://api.audible.com/1.0/catalog/products",
        params=search_params,
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(catalog_payload, dict):
        return []
    products = catalog_payload.get("products")
    if not isinstance(products, list):
        return []

    preliminary: list[EnrichmentCandidate] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        cand = _parse_audible_product(product)
        if cand is not None:
            cand.score = score_candidate_match(metadata, cand)
            preliminary.append(cand)

    preliminary.sort(key=lambda c: c.score, reverse=True)
    enriched: list[EnrichmentCandidate] = []
    for idx, cand in enumerate(preliminary):
        if cand.asin and (idx < 3 and cand.score >= min_confidence * 0.7):
            book_payload = _cached_get_json(
                session,
                "audnexus",
                f"https://api.audnex.us/books/{cand.asin}",
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
            )
            if isinstance(book_payload, dict):
                full_cand = _parse_audnexus_book(book_payload)
                if full_cand is not None:
                    full_cand.score = score_candidate_match(metadata, full_cand)
                    enriched.append(full_cand)
                    continue
        enriched.append(cand)
    enriched.sort(key=lambda c: c.score, reverse=True)
    return enriched


def _upgrade_itunes_artwork(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    if "mzstatic.com" in cleaned:
        return _ITUNES_ARTWORK_SIZE_RE.sub("/1200x1200bb.jpg", cleaned)
    return cleaned


def _query_itunes(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> list[EnrichmentCandidate]:
    if not metadata.title:
        return []
    term = f"{metadata.title} {metadata.authors[0]}" if metadata.authors else metadata.title
    payload = _cached_get_json(
        session,
        "itunes",
        "https://itunes.apple.com/search",
        params={"term": term, "media": "audiobook", "entity": "audiobook", "limit": 5},
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    candidates: list[EnrichmentCandidate] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("collectionName") or item.get("trackName") or "").strip()
        if not title:
            continue
        artist = str(item.get("artistName") or "").strip()
        authors = [artist] if artist else []
        genre = str(item.get("primaryGenreName") or "").strip()
        genres = [genre] if genre else []
        description = clean_html_description(item.get("description"), preserve_newlines=True)
        release_date = parse_release_date(item.get("releaseDate"))
        artwork_raw = item.get("artworkUrl100") or item.get("artworkUrl60") or item.get("artworkUrl30")
        cover_url = _upgrade_itunes_artwork(str(artwork_raw)) if artwork_raw else None
        raw_id = str(item.get("collectionId")) if item.get("collectionId") is not None else None
        cand = EnrichmentCandidate(
            provider="itunes",
            score=0.0,
            title=title,
            authors=authors,
            genres=genres,
            description=description,
            release_date=release_date,
            cover_url=cover_url,
            raw_id=raw_id,
        )
        cand.score = score_candidate_match(metadata, cand)
        candidates.append(cand)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _upgrade_google_cover(image_links: Any) -> str | None:
    if not isinstance(image_links, dict):
        return None
    for key in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"):
        raw = image_links.get(key)
        if isinstance(raw, str) and raw.strip():
            url = raw.strip()
            if url.startswith("http://"):
                url = "https://" + url[7:]
            url = url.replace("&edge=curl", "")
            return url
    return None


def _extract_google_isbn(identifiers: Any) -> str | None:
    if not isinstance(identifiers, list):
        return None
    isbn10: str | None = None
    for entry in identifiers:
        if not isinstance(entry, dict):
            continue
        id_type = str(entry.get("type") or "").upper()
        cleaned = _clean_isbn(entry.get("identifier"))
        if not cleaned:
            continue
        if id_type == "ISBN_13" or len(cleaned) == 13:
            return cleaned
        if isbn10 is None:
            isbn10 = cleaned
    return isbn10


def _query_google(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> list[EnrichmentCandidate]:
    isbn = _clean_isbn(metadata.isbn)
    if isbn:
        query = f"isbn:{isbn}"
    elif metadata.title:
        query = f"intitle:{metadata.title}"
        if metadata.authors:
            query += f"+inauthor:{metadata.authors[0]}"
    else:
        return []

    payload = _cached_get_json(
        session,
        "google",
        "https://www.googleapis.com/books/v1/volumes",
        params={"q": query, "maxResults": 5},
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    candidates: list[EnrichmentCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        info = item.get("volumeInfo")
        if not isinstance(info, dict):
            continue
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        authors = _extract_names(info.get("authors"))
        categories = _extract_names(info.get("categories"))[:5]
        cand = EnrichmentCandidate(
            provider="google",
            score=0.0,
            title=title,
            authors=authors,
            genres=categories,
            language=parse_language(info.get("language")),
            description=clean_html_description(info.get("description"), preserve_newlines=True),
            isbn=_extract_google_isbn(info.get("industryIdentifiers")),
            publisher=str(info.get("publisher") or "").strip() or None,
            release_date=parse_release_date(info.get("publishedDate")),
            cover_url=_upgrade_google_cover(info.get("imageLinks")),
            raw_id=str(item.get("id")) if item.get("id") is not None else None,
        )
        cand.score = score_candidate_match(metadata, cand)
        candidates.append(cand)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _extract_first_isbn(doc: Mapping[str, Any]) -> str | None:
    isbns = doc.get("isbn") or []
    for raw in isbns:
        candidate = _clean_isbn(raw)
        if candidate:
            return candidate
    return None


def _query_openlibrary(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> list[EnrichmentCandidate]:
    params: dict[str, Any] = {"limit": 5}
    if metadata.isbn:
        params["isbn"] = metadata.isbn
    elif metadata.title:
        params["title"] = metadata.title
        if metadata.authors:
            params["author"] = metadata.authors[0]
    else:
        return []

    payload = _cached_get_json(
        session,
        "openlibrary",
        "https://openlibrary.org/search.json",
        params=params,
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(payload, dict):
        return []
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return []

    candidates: list[EnrichmentCandidate] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or metadata.title or "").strip()
        if not title:
            continue
        authors = _extract_names(doc.get("author_name"))
        publishers = doc.get("publisher") or []
        publisher = str(publishers[0]).strip() if publishers else None
        genres = [str(s).strip() for s in (doc.get("subject") or [])[:5] if str(s).strip()]
        description: str | None = None
        first_sentence = doc.get("first_sentence")
        if isinstance(first_sentence, (list, str)):
            text = first_sentence[0] if isinstance(first_sentence, list) and first_sentence else first_sentence
            if isinstance(text, str) and text.strip():
                description = text.strip()
        cover_i = doc.get("cover_i")
        cover_url = (
            f"https://covers.openlibrary.org/b/id/{int(cover_i)}-L.jpg"
            if isinstance(cover_i, int) and cover_i > 0
            else None
        )
        cand = EnrichmentCandidate(
            provider="openlibrary",
            score=0.0,
            title=title,
            authors=authors,
            genres=genres,
            description=description,
            isbn=_extract_first_isbn(doc),
            publisher=publisher or None,
            release_date=parse_release_date(doc.get("first_publish_year")),
            cover_url=cover_url,
            raw_id=str(doc.get("key")) if doc.get("key") else None,
        )
        cand.score = score_candidate_match(metadata, cand)
        candidates.append(cand)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _split_people_field(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return _extract_names(raw)
    if not isinstance(raw, str) or not raw.strip():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            names.append(cleaned)
    return names


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _parse_abs_custom_match(
    provider_key: str,
    match: Mapping[str, Any],
) -> EnrichmentCandidate | None:
    title = str(match.get("title") or "").strip()
    if not title:
        return None
    authors = _split_people_field(match.get("author"))
    narrators = _split_people_field(match.get("narrator"))

    series_name: str | None = None
    series_order: int | None = None
    raw_series = match.get("series")
    if isinstance(raw_series, list):
        for entry in raw_series:
            if isinstance(entry, dict):
                candidate_series = str(entry.get("series") or entry.get("name") or "").strip()
                if candidate_series:
                    series_name = candidate_series
                    series_order = parse_series_order(entry.get("sequence"))
                    break
            elif isinstance(entry, str) and entry.strip():
                series_name = entry.strip()
                break
    elif isinstance(raw_series, str) and raw_series.strip():
        series_name = raw_series.strip()

    raw_genres = match.get("genres")
    genres: list[str] = []
    if isinstance(raw_genres, list):
        seen_genres: set[str] = set()
        for item in raw_genres:
            cleaned = str(item or "").strip()
            if cleaned and cleaned.lower() not in seen_genres:
                seen_genres.add(cleaned.lower())
                genres.append(cleaned)
                if len(genres) >= 5:
                    break

    raw_cover = str(match.get("cover") or "").strip()
    cover_url = raw_cover if raw_cover and _is_http_url(raw_cover) else None
    publisher = str(match.get("publisher") or "").strip() or None
    description = clean_html_description(match.get("description"), preserve_newlines=True)
    raw_id = (
        _clean_asin(match.get("asin"))
        or _clean_isbn(match.get("isbn"))
        or str(match.get("id") or "").strip()
        or None
    )
    return EnrichmentCandidate(
        provider=provider_key,
        score=0.0,
        title=title,
        authors=authors,
        narrators=narrators,
        series=series_name,
        series_order=series_order,
        genres=genres,
        language=parse_language(match.get("language")),
        description=description,
        isbn=_clean_isbn(match.get("isbn")),
        asin=_clean_asin(match.get("asin")),
        publisher=publisher,
        release_date=parse_release_date(match.get("publishedYear")),
        cover_url=cover_url,
        raw_id=raw_id,
    )


def _query_abs_custom_provider(
    provider_cfg: Any,
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> list[EnrichmentCandidate]:
    if not getattr(provider_cfg, "enabled", True):
        return []
    base_url = validate_custom_provider_url(str(getattr(provider_cfg, "url", "") or ""))
    slug = normalize_custom_provider_name(str(getattr(provider_cfg, "name", "") or ""))
    provider_key = f"{CUSTOM_PROVIDER_PREFIX}{slug}"

    query_text = (metadata.title or "").strip() or (metadata.isbn or "").strip() or (metadata.asin or "").strip()
    if not query_text:
        return []
    params: dict[str, Any] = {"query": query_text}
    if metadata.authors:
        params["author"] = metadata.authors[0]

    api_key = getattr(provider_cfg, "api_key", None)
    headers = {"Authorization": str(api_key)} if api_key else None
    payload = _cached_get_json(
        session,
        provider_key,
        f"{base_url}/search",
        params=params,
        headers=headers,
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not isinstance(payload, dict):
        return []
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return []

    candidates: list[EnrichmentCandidate] = []
    for item in matches:
        if not isinstance(item, Mapping):
            continue
        cand = _parse_abs_custom_match(provider_key, item)
        if cand is None:
            continue
        cand.score = score_candidate_match(metadata, cand)
        candidates.append(cand)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def test_custom_metadata_provider(
    url: str,
    *,
    api_key: str | None = None,
    sample_query: str = "Dune",
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """Probe an ABS-compatible custom metadata provider endpoint and return `(match_count, summary)`."""
    base_url = validate_custom_provider_url(url)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if api_key and api_key.strip():
        headers["Authorization"] = api_key.strip()
    session_cm = nullcontext(session) if session is not None else requests.Session()
    with session_cm as sess:
        resp = sess.get(
            f"{base_url}/search",
            params={"query": sample_query},
            headers=headers,
            proxies=proxies,
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise ValueError("Provider response JSON must be an object containing a 'matches' array")
    count = len(payload["matches"])
    return count, f"Connected ({count} sample match{'es' if count != 1 else ''})"


def _query_provider(
    provider: str,
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    custom_providers: Mapping[str, Any] | None = None,
) -> list[EnrichmentCandidate]:
    if provider == "audnexus":
        return _query_audnexus(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            min_confidence=min_confidence,
        )
    if provider == "itunes":
        return _query_itunes(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    if provider == "google":
        return _query_google(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    if provider == "openlibrary":
        return _query_openlibrary(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    if provider.startswith(CUSTOM_PROVIDER_PREFIX):
        slug = provider[len(CUSTOM_PROVIDER_PREFIX) :].strip().lower()
        provider_cfg = _lookup_custom_provider(slug, custom_providers)
        if provider_cfg is None:
            raise ValueError(f"Custom enrichment provider '{slug}' is not configured")
        return _query_abs_custom_provider(
            provider_cfg,
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    raise ValueError(f"Unknown enrichment provider: {provider!r}")


def _download_cover(
    session: requests.Session,
    cover_url: str,
    *,
    proxy: str | None = None,
    timeout: float = 10.0,
) -> Cover | None:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = session.get(
            cover_url,
            timeout=timeout,
            proxies=proxies,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        resp.raise_for_status()
        raw_bytes = resp.content
    except requests.RequestException as exc:
        logging.debug(f"Enrichment cover download skipped ({cover_url}): {exc}")
        return None
    if not raw_bytes:
        return None
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img.verify()
            fmt = (img.format or "").lower()
    except Exception as exc:
        logging.debug(f"Enrichment cover image invalid ({cover_url}): {exc}")
        return None
    ext = "jpg" if fmt == "jpeg" else (fmt or "jpg")
    return Cover(image=raw_bytes, extension=ext)


_PROVIDER_CAPABILITIES: dict[str, frozenset[str]] = {
    "audnexus": frozenset({
        "title",
        "authors",
        "narrators",
        "series",
        "series_order",
        "genres",
        "language",
        "description",
        "asin",
        "publisher",
        "release_date",
        "cover",
        "chapters",
    }),
    "itunes": frozenset({
        "title",
        "authors",
        "genres",
        "description",
        "release_date",
        "cover",
    }),
    "google": frozenset({
        "title",
        "authors",
        "genres",
        "language",
        "description",
        "isbn",
        "publisher",
        "release_date",
        "cover",
    }),
    "openlibrary": frozenset({
        "title",
        "authors",
        "genres",
        "description",
        "isbn",
        "publisher",
        "release_date",
        "cover",
    }),
}


_ABS_CUSTOM_CAPABILITIES: frozenset[str] = frozenset({
    "title",
    "authors",
    "narrators",
    "series",
    "series_order",
    "genres",
    "language",
    "description",
    "isbn",
    "asin",
    "publisher",
    "release_date",
    "cover",
})


def _provider_capabilities_for(provider: str) -> frozenset[str] | None:
    if provider.startswith(CUSTOM_PROVIDER_PREFIX):
        return _ABS_CUSTOM_CAPABILITIES
    return _PROVIDER_CAPABILITIES.get(provider)


def _missing_enrichment_fields(
    audiobook: Audiobook,
    *,
    include_chapters: bool = True,
) -> set[str]:
    meta = audiobook.metadata
    missing: set[str] = set()
    if not meta.title:
        missing.add("title")
    if not meta.authors:
        missing.add("authors")
    if not meta.narrators:
        missing.add("narrators")
    if not meta.series:
        missing.add("series")
    if meta.series_order is None:
        missing.add("series_order")
    if not meta.genres:
        missing.add("genres")
    if meta.language is None:
        missing.add("language")
    if not meta.description:
        missing.add("description")
    if not meta.isbn:
        missing.add("isbn")
    if not meta.asin:
        missing.add("asin")
    if not meta.publisher:
        missing.add("publisher")
    if meta.release_date is None:
        missing.add("release_date")
    if audiobook.cover is None:
        missing.add("cover")
    if include_chapters and not audiobook.chapters:
        missing.add("chapters")
    return missing


def _has_missing_enrichment_fields(
    audiobook: Audiobook,
    *,
    include_chapters: bool = True,
) -> bool:
    return bool(_missing_enrichment_fields(audiobook, include_chapters=include_chapters))


def apply_candidate_to_audiobook(
    audiobook: Audiobook,
    candidate: EnrichmentCandidate,
    *,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    include_chapters: bool = True,
) -> tuple[list[str], bool, bool]:
    """
    Apply missing metadata fields, cover, and chapters from `candidate` onto
    `audiobook` without overwriting existing non-empty fields.
    """
    meta = audiobook.metadata
    updated_fields: list[str] = []

    if not meta.title and candidate.title:
        meta.title = candidate.title
        updated_fields.append("title")
    if not meta.authors and candidate.authors:
        meta.add_authors(candidate.authors)
        updated_fields.append("authors")
    if not meta.narrators and candidate.narrators:
        meta.add_narrators(candidate.narrators)
        updated_fields.append("narrators")
    if not meta.series and candidate.series:
        meta.series = candidate.series
        updated_fields.append("series")
        if meta.series_order is None and candidate.series_order is not None:
            meta.series_order = candidate.series_order
            updated_fields.append("series_order")
    elif (
        meta.series
        and meta.series_order is None
        and candidate.series
        and candidate.series_order is not None
        and _normalize_title(meta.series) == _normalize_title(candidate.series)
    ):
        meta.series_order = candidate.series_order
        updated_fields.append("series_order")
    if not meta.genres and candidate.genres:
        meta.add_genres(candidate.genres)
        updated_fields.append("genres")
    if meta.language is None and candidate.language is not None:
        meta.language = candidate.language
        updated_fields.append("language")
    if not meta.description and candidate.description:
        meta.description = candidate.description
        updated_fields.append("description")
    if not meta.isbn and candidate.isbn:
        meta.isbn = candidate.isbn
        updated_fields.append("isbn")
    if not meta.asin and candidate.asin:
        meta.asin = candidate.asin
        updated_fields.append("asin")
    if not meta.publisher and candidate.publisher:
        meta.publisher = candidate.publisher
        updated_fields.append("publisher")
    if meta.release_date is None and candidate.release_date is not None:
        meta.release_date = candidate.release_date
        updated_fields.append("release_date")

    cover_updated = False
    chapters_updated = False
    need_cover = audiobook.cover is None and bool(candidate.cover_url)
    need_chapters = (
        include_chapters
        and not audiobook.chapters
        and (bool(candidate.chapters) or (candidate.provider == "audnexus" and bool(candidate.asin)))
    )
    if need_cover or need_chapters:
        session_cm = nullcontext(session) if session is not None else requests.Session()
        with session_cm as sess:
            if need_cover and candidate.cover_url:
                cover = _download_cover(sess, candidate.cover_url, proxy=proxy, timeout=timeout)
                if cover is not None:
                    audiobook.cover = cover
                    cover_updated = True
            if need_chapters:
                chapters = list(candidate.chapters)
                if not chapters and candidate.provider == "audnexus" and candidate.asin:
                    chapters = _fetch_audnexus_chapters(
                        sess,
                        candidate.asin,
                        proxy=proxy,
                        timeout=timeout,
                        cache=cache,
                        use_cache=use_cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                    )
                if chapters:
                    audiobook.chapters = chapters
                    chapters_updated = True

    return updated_fields, cover_updated, chapters_updated


def search_enrichment_candidates(
    metadata: AudiobookMetadata,
    *,
    providers: Sequence[str] | None = None,
    custom_providers: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    database_directory: str | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[EnrichmentCandidate]:
    """Query configured providers and return scored `EnrichmentCandidate` entries."""
    provider_list = (
        tuple(providers)
        if providers is not None
        else resolve_provider_list(None, custom_providers=custom_providers)
    )
    cache_cm: Any = (
        EnrichmentCache(
            default_enrichment_cache_path(database_directory),
            ttl_seconds=cache_ttl_seconds,
            persistent=True,
        )
        if database_directory
        else nullcontext(None)
    )
    collected: list[EnrichmentCandidate] = []
    with cache_cm as cache:
        if session is None and len(provider_list) > 1:
            def _query_worker(prov: str) -> list[EnrichmentCandidate]:
                with requests.Session() as worker_sess:
                    return _query_provider(
                        prov,
                        metadata,
                        session=worker_sess,
                        proxy=proxy,
                        timeout=timeout,
                        cache=cache,
                        use_cache=use_cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                        min_confidence=min_confidence,
                        custom_providers=custom_providers,
                    )

            with ThreadPoolExecutor(max_workers=min(4, len(provider_list))) as executor:
                futures = [executor.submit(_query_worker, provider) for provider in provider_list]
                for future in futures:
                    collected.extend(future.result())
        else:
            session_cm = nullcontext(session) if session is not None else requests.Session()
            with session_cm as sess:
                for provider in provider_list:
                    collected.extend(
                        _query_provider(
                            provider,
                            metadata,
                            session=sess,
                            proxy=proxy,
                            timeout=timeout,
                            cache=cache,
                            use_cache=use_cache,
                            cache_ttl_seconds=cache_ttl_seconds,
                            min_confidence=min_confidence,
                            custom_providers=custom_providers,
                        )
                    )
    collected.sort(key=lambda c: c.score, reverse=True)
    return collected


def enrich_audiobook_metadata(
    audiobook: Audiobook,
    *,
    providers: Sequence[str] | None = None,
    custom_providers: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    database_directory: str | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    interactive: bool = False,
    selector: CandidateSelector | None = None,
    include_chapters: bool = True,
) -> EnrichmentResult:
    """
    Enrich `audiobook` across `providers` in priority order, filling missing
    metadata, cover art, and chapters without overwriting existing source data.
    """
    provider_list = (
        tuple(providers)
        if providers is not None
        else resolve_provider_list(None, custom_providers=custom_providers)
    )
    result = EnrichmentResult()
    if not _has_missing_enrichment_fields(audiobook, include_chapters=include_chapters):
        return result

    cache_cm: Any = (
        EnrichmentCache(
            default_enrichment_cache_path(database_directory),
            ttl_seconds=cache_ttl_seconds,
            persistent=True,
        )
        if database_directory
        else nullcontext(None)
    )
    session_cm = nullcontext(session) if session is not None else requests.Session()

    with cache_cm as cache, session_cm as sess:
        for provider in provider_list:
            missing = _missing_enrichment_fields(audiobook, include_chapters=include_chapters)
            if not missing:
                break
            supported = _provider_capabilities_for(provider)
            if supported is not None and missing.isdisjoint(supported):
                continue
            candidates = _query_provider(
                provider,
                audiobook.metadata,
                session=sess,
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                min_confidence=min_confidence,
                custom_providers=custom_providers,
            )
            if not candidates:
                continue

            chosen: EnrichmentCandidate | None = None
            if interactive and selector is not None:
                chosen_idx = selector(provider, candidates)
                if chosen_idx is not None and 0 <= chosen_idx < len(candidates):
                    chosen = candidates[chosen_idx]
            else:
                top = candidates[0]
                if top.score >= min_confidence:
                    chosen = top

            if chosen is None:
                continue

            fields, cover_updated, chapters_updated = apply_candidate_to_audiobook(
                audiobook,
                chosen,
                session=sess,
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                include_chapters=include_chapters,
            )
            result.providers_used.append(provider)
            result.matched_candidates.append(chosen)
            if fields or cover_updated or chapters_updated:
                result.updated = True
                result.fields_updated.extend(f for f in fields if f not in result.fields_updated)
                result.cover_updated = result.cover_updated or cover_updated
                result.chapters_updated = result.chapters_updated or chapters_updated
            if not _has_missing_enrichment_fields(audiobook, include_chapters=include_chapters):
                break

    return result


def enrich_metadata_from_openlibrary(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    database_directory: str | None = None,
    use_cache: bool = True,
) -> bool:
    """
    Populate missing `AudiobookMetadata` fields via the OpenLibrary public search API.

    Existing non-empty fields on `metadata` are preserved; network or parse errors
    log a debug message and return `False` without failing the download.
    """
    cache_cm: Any = (
        EnrichmentCache(default_enrichment_cache_path(database_directory), persistent=True)
        if database_directory
        else nullcontext(None)
    )
    session_cm = nullcontext(session) if session is not None else requests.Session()
    with cache_cm as cache, session_cm as sess:
        dummy_book = Audiobook(session=sess, metadata=metadata, files=[])
        candidates = _query_openlibrary(
            metadata,
            session=sess,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
        )
        if not candidates:
            return False
        top = candidates[0]
        if top.score < DEFAULT_CONFIDENCE_THRESHOLD:
            return False
        top_no_cover = EnrichmentCandidate(
            provider=top.provider,
            score=top.score,
            title=top.title,
            authors=top.authors,
            narrators=top.narrators,
            series=top.series,
            series_order=top.series_order,
            genres=top.genres,
            language=top.language,
            description=top.description,
            isbn=top.isbn,
            asin=top.asin,
            publisher=top.publisher,
            release_date=top.release_date,
            cover_url=None,
            chapters=[],
            raw_id=top.raw_id,
        )
        fields, _, _ = apply_candidate_to_audiobook(
            dummy_book,
            top_no_cover,
            session=sess,
            proxy=proxy,
            timeout=timeout,
            include_chapters=False,
        )
        return bool(fields)

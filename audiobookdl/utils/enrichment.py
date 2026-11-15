from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
import html
from io import BytesIO
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PIL import Image
import pycountry
import requests

from audiobookdl import logging
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookMetadata,
    Chapter,
    Cover,
    parse_release_date,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
    default_enrichment_cache_path,
    make_cache_key,
)
from audiobookdl.utils.http import DEFAULT_USER_AGENT


DEFAULT_PROVIDERS: Tuple[str, ...] = ("audnexus", "itunes", "google", "openlibrary")
PROVIDER_ALIASES: Dict[str, str] = {
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

_EDITION_MARKER_RE = re.compile(
    r"\s*(?:\([^)]*\b(?:unabridged|abridged|audiobook|edition|annotated|illustrated)\b[^)]*\)"
    r"|\[[^\]]*\b(?:unabridged|abridged|audiobook|edition)\b[^\]]*\])\s*",
    re.IGNORECASE,
)
_SUBTITLE_NOVEL_RE = re.compile(r"\s*[:\-]\s*a\s+novel\b.*$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_HTML_BREAK_RE = re.compile(r"<(?:br\s*/?|/p|/div|/li)\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ISBN_CLEAN_RE = re.compile(r"[^0-9Xx]")
_ASIN_RE = re.compile(r"^[A-Za-z0-9]{10}$")
_SERIES_INT_RE = re.compile(r"^\s*(\d+)(?:\.0+)?\s*$")
_ITUNES_ARTWORK_SIZE_RE = re.compile(r"/\d+x\d+bb[^/]*$")


@dataclass
class EnrichmentCandidate:
    """Normalized metadata match candidate produced by an enrichment provider."""

    provider: str
    score: float
    title: str
    authors: List[str] = field(default_factory=list)
    narrators: List[str] = field(default_factory=list)
    series: Optional[str] = None
    series_order: Optional[int] = None
    genres: List[str] = field(default_factory=list)
    language: Optional[Any] = None
    description: Optional[str] = None
    isbn: Optional[str] = None
    asin: Optional[str] = None
    publisher: Optional[str] = None
    release_date: Optional[date] = None
    cover_url: Optional[str] = None
    chapters: List[Chapter] = field(default_factory=list)
    raw_id: Optional[str] = None


@dataclass
class EnrichmentResult:
    """Summary of metadata, cover, and chapter updates applied by enrichment."""

    updated: bool = False
    providers_used: List[str] = field(default_factory=list)
    matched_candidates: List[EnrichmentCandidate] = field(default_factory=list)
    fields_updated: List[str] = field(default_factory=list)
    cover_updated: bool = False
    chapters_updated: bool = False


CandidateSelector = Callable[[str, Sequence[EnrichmentCandidate]], Optional[int]]


def parse_provider_list(raw: Optional[str]) -> Tuple[str, ...]:
    """Parse a comma-separated provider list into canonical deduplicated provider names."""
    if raw is None or not raw.strip():
        return DEFAULT_PROVIDERS
    resolved: List[str] = []
    seen = set()
    for token in raw.split(","):
        cleaned = token.strip().lower()
        if not cleaned:
            continue
        canonical = PROVIDER_ALIASES.get(cleaned)
        if canonical is None:
            valid = ", ".join(DEFAULT_PROVIDERS)
            raise ValueError(f"Unknown enrichment provider '{token.strip()}'. Valid providers: {valid}")
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    if not resolved:
        raise ValueError("At least one enrichment provider must be specified")
    return tuple(resolved)


def resolve_redirect_url(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
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


def _clean_isbn(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    candidate = _ISBN_CLEAN_RE.sub("", str(raw)).upper()
    if len(candidate) in (10, 13):
        return candidate
    return None


def _clean_asin(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    candidate = str(raw).strip().upper()
    if _ASIN_RE.match(candidate):
        return candidate
    return None


def _strip_html(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = _HTML_BREAK_RE.sub("\n", raw)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned.strip() or None


def _parse_language(raw: Any) -> Optional[Any]:
    if raw is None:
        return None
    if hasattr(raw, "alpha_3"):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    base = re.split(r"[-_]", text)[0].strip()
    for candidate in (text, base):
        if len(candidate) == 2:
            found = pycountry.languages.get(alpha_2=candidate.lower())
            if found is not None:
                return found
        elif len(candidate) == 3:
            found = pycountry.languages.get(alpha_3=candidate.lower())
            if found is not None:
                return found
        else:
            found = pycountry.languages.get(name=candidate.title())
            if found is not None:
                return found
    return None


def _parse_series_order(raw: Any) -> Optional[int]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    m = _SERIES_INT_RE.match(str(raw))
    return int(m.group(1)) if m else None


def _extract_names(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    names: List[str] = []
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
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> Optional[Any]:
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


def _parse_audnexus_book(payload: Mapping[str, Any]) -> Optional[EnrichmentCandidate]:
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(payload.get("asin"))
    authors = _extract_names(payload.get("authors"))
    narrators = _extract_names(payload.get("narrators"))

    genres: List[str] = []
    raw_genres = payload.get("genres")
    if isinstance(raw_genres, list):
        for g in raw_genres:
            if isinstance(g, dict):
                g_name = str(g.get("name") or "").strip()
            else:
                g_name = str(g).strip()
            if g_name and g_name not in genres:
                genres.append(g_name)

    series_name: Optional[str] = None
    series_order: Optional[int] = None
    series_primary = payload.get("seriesPrimary")
    if isinstance(series_primary, dict):
        raw_series = str(series_primary.get("name") or "").strip()
        series_name = raw_series or None
        series_order = _parse_series_order(series_primary.get("position"))

    description = _strip_html(payload.get("summary") or payload.get("description"))
    cover_url = str(payload.get("image") or "").strip() or None
    publisher = str(payload.get("publisherName") or "").strip() or None
    release_date = parse_release_date(payload.get("releaseDate"))
    language = _parse_language(payload.get("language"))
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


def _parse_audible_product(product: Mapping[str, Any]) -> Optional[EnrichmentCandidate]:
    title = str(product.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(product.get("asin"))
    authors = _extract_names(product.get("authors"))
    narrators = _extract_names(product.get("narrators"))

    series_name: Optional[str] = None
    series_order: Optional[int] = None
    series_list = product.get("series")
    if isinstance(series_list, list) and series_list and isinstance(series_list[0], dict):
        series_name = str(series_list[0].get("title") or "").strip() or None
        series_order = _parse_series_order(series_list[0].get("sequence"))

    cover_url: Optional[str] = None
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

    description = _strip_html(product.get("publisher_summary") or product.get("merchandising_summary"))
    publisher = str(product.get("publisher_name") or "").strip() or None
    release_date = parse_release_date(product.get("release_date"))
    language = _parse_language(product.get("language"))

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
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> List[Chapter]:
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
    chapters: List[Chapter] = []
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
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[EnrichmentCandidate]:
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

    search_params: Dict[str, Any] = {
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

    preliminary: List[EnrichmentCandidate] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        cand = _parse_audible_product(product)
        if cand is not None:
            cand.score = score_candidate_match(metadata, cand)
            preliminary.append(cand)

    preliminary.sort(key=lambda c: c.score, reverse=True)
    enriched: List[EnrichmentCandidate] = []
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


def _upgrade_itunes_artwork(url: Optional[str]) -> Optional[str]:
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
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> List[EnrichmentCandidate]:
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

    candidates: List[EnrichmentCandidate] = []
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
        description = _strip_html(item.get("description"))
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


def _upgrade_google_cover(image_links: Any) -> Optional[str]:
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


def _extract_google_isbn(identifiers: Any) -> Optional[str]:
    if not isinstance(identifiers, list):
        return None
    isbn10: Optional[str] = None
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
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> List[EnrichmentCandidate]:
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

    candidates: List[EnrichmentCandidate] = []
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
            language=_parse_language(info.get("language")),
            description=_strip_html(info.get("description")),
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


def _extract_first_isbn(doc: Mapping[str, Any]) -> Optional[str]:
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
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> List[EnrichmentCandidate]:
    params: Dict[str, Any] = {"limit": 5}
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

    candidates: List[EnrichmentCandidate] = []
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
        description: Optional[str] = None
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


def _query_provider(
    provider: str,
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[EnrichmentCandidate]:
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
    raise ValueError(f"Unknown enrichment provider: {provider!r}")


def _download_cover(
    session: requests.Session,
    cover_url: str,
    *,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[Cover]:
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


def _has_missing_enrichment_fields(
    audiobook: Audiobook,
    *,
    include_chapters: bool = True,
) -> bool:
    meta = audiobook.metadata
    if not all((
        meta.authors,
        meta.narrators,
        meta.series,
        meta.genres,
        meta.language is not None,
        meta.description,
        meta.isbn,
        meta.asin,
        meta.publisher,
        meta.release_date is not None,
    )):
        return True
    if audiobook.cover is None:
        return True
    if include_chapters and not audiobook.chapters:
        return True
    return False


def apply_candidate_to_audiobook(
    audiobook: Audiobook,
    candidate: EnrichmentCandidate,
    *,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    cache: Optional[EnrichmentCache] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    include_chapters: bool = True,
) -> Tuple[List[str], bool, bool]:
    """
    Apply missing metadata fields, cover, and chapters from `candidate` onto
    `audiobook` without overwriting existing non-empty fields.
    """
    meta = audiobook.metadata
    updated_fields: List[str] = []

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
    providers: Optional[Sequence[str]] = None,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    database_directory: Optional[str] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[EnrichmentCandidate]:
    """Query configured providers and return scored `EnrichmentCandidate` entries."""
    provider_list = tuple(providers) if providers is not None else DEFAULT_PROVIDERS
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
    collected: List[EnrichmentCandidate] = []
    with cache_cm as cache, session_cm as sess:
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
                )
            )
    collected.sort(key=lambda c: c.score, reverse=True)
    return collected


def enrich_audiobook_metadata(
    audiobook: Audiobook,
    *,
    providers: Optional[Sequence[str]] = None,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    database_directory: Optional[str] = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    interactive: bool = False,
    selector: Optional[CandidateSelector] = None,
    include_chapters: bool = True,
) -> EnrichmentResult:
    """
    Enrich `audiobook` across `providers` in priority order, filling missing
    metadata, cover art, and chapters without overwriting existing source data.
    """
    provider_list = tuple(providers) if providers is not None else DEFAULT_PROVIDERS
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
            if not _has_missing_enrichment_fields(audiobook, include_chapters=include_chapters):
                break
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
            )
            if not candidates:
                continue

            chosen: Optional[EnrichmentCandidate] = None
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

    return result


def enrich_metadata_from_openlibrary(
    metadata: AudiobookMetadata,
    *,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    timeout: float = 10.0,
    database_directory: Optional[str] = None,
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

"""ABS-spec custom metadata provider (`custom:`) and connection test."""

from contextlib import nullcontext
import threading
from typing import Any
from collections.abc import Mapping

import requests

from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    clean_html_description,
    parse_language,
    parse_release_date,
    parse_series_order,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
)
from audiobookdl.utils.http import (
    DEFAULT_USER_AGENT,
    create_configured_http_session,
    warn_if_insecure_token_url,
)
from audiobookdl.utils.enrichment.matching import score_candidate_match
from audiobookdl.utils.enrichment.models import (
    CUSTOM_PROVIDER_PREFIX,
    EnrichmentCandidate,
    _clean_asin,
    _clean_isbn,
    _is_http_url,
    _raise_if_cancelled,
    _split_people_field,
)
from audiobookdl.utils.enrichment.providers.registry import (
    _cached_get_json,
    normalize_custom_provider_name,
    validate_custom_provider_url,
)


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
    cancel_event: threading.Event | None = None,
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
    if api_key:
        warn_if_insecure_token_url(base_url, f"custom metadata provider '{slug}'")
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
        cancel_event=cancel_event,
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
    cancel_event: threading.Event | None = None,
) -> tuple[int, str]:
    """Probe an ABS-compatible custom metadata provider endpoint and return `(match_count, summary)`."""
    _raise_if_cancelled(cancel_event)
    base_url = validate_custom_provider_url(url)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if api_key and api_key.strip():
        warn_if_insecure_token_url(base_url, "custom metadata provider")
        headers["Authorization"] = api_key.strip()
    session_cm: Any = (
        nullcontext(session)
        if session is not None
        else create_configured_http_session(proxy=proxy)
    )
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

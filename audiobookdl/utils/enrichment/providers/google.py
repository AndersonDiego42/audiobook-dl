"""Google Books API enrichment provider."""

from typing import Any

import threading

import requests

from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    clean_html_description,
    parse_language,
    parse_release_date,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
)
from audiobookdl.utils.enrichment.matching import score_candidate_match
from audiobookdl.utils.enrichment.models import (
    EnrichmentCandidate,
    _clean_isbn,
    _extract_names,
)
from audiobookdl.utils.enrichment.providers.registry import _cached_get_json


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
    cancel_event: threading.Event | None = None,
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
        cancel_event=cancel_event,
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

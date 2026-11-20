"""Open Library enrichment provider."""

from typing import Any
from collections.abc import Mapping

import threading

import requests

from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
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
    cancel_event: threading.Event | None = None,
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
        cancel_event=cancel_event,
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

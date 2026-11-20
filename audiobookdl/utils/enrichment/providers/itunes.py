"""iTunes Search API enrichment provider."""

import threading

import requests

from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    clean_html_description,
    parse_release_date,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
)
from audiobookdl.utils.enrichment.matching import score_candidate_match
from audiobookdl.utils.enrichment.models import (
    EnrichmentCandidate,
    _ITUNES_ARTWORK_SIZE_RE,
)
from audiobookdl.utils.enrichment.providers.registry import _cached_get_json


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
    cancel_event: threading.Event | None = None,
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
        cancel_event=cancel_event,
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

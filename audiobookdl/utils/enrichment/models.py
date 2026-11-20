"""Shared constants, dataclasses, and low-level helpers for metadata enrichment."""

from dataclasses import dataclass, field
from datetime import date
import re
import threading
from typing import Any
from urllib.parse import urlparse
from collections.abc import Callable, Sequence

from audiobookdl import logging
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.utils.audiobook import Chapter


DEFAULT_PROVIDERS: tuple[str, ...] = ("audnexus", "itunes", "google", "openlibrary")
CUSTOM_PROVIDER_PREFIX = "custom:"
PLUGIN_PROVIDER_PREFIX = "plugin:"
ENRICHMENT_ENTRY_POINT_GROUP = "audiobookdl.enrichment"
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


def _resolve_cancel_event(cancel_event: threading.Event | None) -> threading.Event | None:
    """Return `cancel_event`, falling back to the active `JobContext` cancel event."""
    if cancel_event is not None:
        return cancel_event
    ctx = logging.get_job_context()
    return ctx.cancel_event if ctx is not None else None


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    ev = _resolve_cancel_event(cancel_event)
    if ev is not None and ev.is_set():
        raise DownloadCancelled


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

"""Data models and query parsing for the download/library ledger."""

from dataclasses import dataclass
import re
import shlex


VALID_LIBRARY_STATUSES: tuple[str, ...] = ("pending", "downloaded", "trashed")
_TAG_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class ArchiveEntry:
    """Single library or download entry stored in the SQLite ledger."""

    id: int
    source: str
    book_key: str
    url: str | None
    title: str
    author: str
    output_path: str | None
    audio_format: str | None
    downloaded_at: str
    profile: str = "default"
    status: str = "downloaded"
    series: str | None = None
    series_order: int | None = None
    narrator: str = ""
    duration_seconds: float | None = None
    isbn: str | None = None
    asin: str | None = None
    last_seen_at: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def is_hidden(self) -> bool:
        """Return True if this entry carries the reserved `hidden` tag."""
        return "hidden" in self.tags


@dataclass(frozen=True)
class SearchTerm:
    """Parsed token from a library search query."""

    field: str | None
    value: str
    negated: bool = False


def normalize_tag(raw: str) -> str:
    """Normalize a tag to lowercase `[a-z0-9_-]` characters."""
    cleaned = _TAG_CLEAN_RE.sub("_", raw.strip().lower()).strip("_")
    return cleaned


def parse_tags_input(raw: str) -> list[str]:
    """Parse space- or comma-separated tags into unique normalized tags."""
    tokens = re.split(r"[\s,]+", raw.strip())
    out: list[str] = []
    for token in tokens:
        tag = normalize_tag(token)
        if tag and tag not in out:
            out.append(tag)
    return out


def parse_library_query(query: str) -> list[SearchTerm]:
    """
    Parse a Libation-style search query supporting quoted phrases, negation (`-`),
    and field prefixes (`author:`, `title:`, `narrator:`, `series:`, `source:`,
    `profile:`, `tag:`, `status:`, `isbn:`, `asin:`).
    """
    cleaned = query.strip()
    if not cleaned:
        return []
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()

    known_fields = {
        "author",
        "title",
        "narrator",
        "series",
        "source",
        "profile",
        "tag",
        "status",
        "isbn",
        "asin",
    }
    terms: list[SearchTerm] = []
    for token in tokens:
        negated = False
        body = token
        if body.startswith("-") and len(body) > 1:
            negated = True
            body = body[1:]
        field: str | None = None
        value = body
        if ":" in body:
            prefix, rest = body.split(":", 1)
            prefix_lower = prefix.strip().lower()
            if prefix_lower in known_fields and rest:
                field = prefix_lower
                value = rest
        val_lower = value.strip().lower()
        if val_lower:
            terms.append(SearchTerm(field=field, value=val_lower, negated=negated))
    return terms

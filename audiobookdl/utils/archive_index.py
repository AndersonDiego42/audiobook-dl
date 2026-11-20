"""In-memory matching index and stored-value normalizers for the download ledger."""

from datetime import datetime, timezone
import os
import re

from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.audiobook import Audiobook, is_ebook


_ISBN_STRIP_RE = re.compile(r"[\s-]+")


def _normalize_output_path_key(output_path: str | None) -> str | None:
    """Return canonical case-folded absolute path for O(1) `output_path_norm` lookups."""
    cleaned = str(output_path or "").strip()
    if not cleaned:
        return None
    return os.path.normcase(os.path.abspath(cleaned))


def _normalize_source_stored(raw: str | None) -> str:
    """Return the canonical lowercase form used for the `source` column."""
    return (raw or "").strip().lower()


def _normalize_asin_stored(raw: str | None) -> str | None:
    """Return canonical uppercase ASIN for storage, or `None` when empty."""
    cleaned = (raw or "").strip().upper()
    return cleaned or None


def _normalize_isbn_stored(raw: str | None) -> str | None:
    """Return canonical ISBN (uppercase, no hyphens/spaces) for storage, or `None`."""
    cleaned = _ISBN_STRIP_RE.sub("", (raw or "").strip().upper())
    return cleaned or None


def _fts5_phrase(value: str) -> str:
    """Quote an arbitrary search string as a literal FTS5 phrase token."""
    return '"' + value.replace('"', '""') + '"'


def _sql_like_escape(value: str) -> str:
    """Escape SQLite `LIKE` wildcards using `\\` as the escape character."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class ArchiveMatchIndex:
    """In-memory normalized identifier/path/title index for O(1) bulk ledger matching."""

    def __init__(self) -> None:
        self._by_url: dict[str, int] = {}
        self._by_asin: dict[str, int] = {}
        self._by_isbn: dict[str, int] = {}
        self._by_path: dict[str, int] = {}
        self._by_title: dict[str, list[tuple[int, str]]] = {}

    @staticmethod
    def _norm_text(raw: str | None) -> str:
        return " ".join((raw or "").casefold().split())

    def add(
        self,
        entry_id: int,
        *,
        book_key: str | None = None,
        url: str | None = None,
        title: str = "",
        author: str = "",
        output_path: str | None = None,
        isbn: str | None = None,
        asin: str | None = None,
    ) -> None:
        if book_key:
            self._by_url.setdefault(book_key.strip(), entry_id)
        if url:
            self._by_url.setdefault(url.strip(), entry_id)
        norm_asin = (asin or "").strip().lower()
        if norm_asin:
            self._by_asin.setdefault(norm_asin, entry_id)
        norm_isbn = (isbn or "").strip().replace("-", "").lower()
        if norm_isbn:
            self._by_isbn.setdefault(norm_isbn, entry_id)
        if output_path and output_path.strip():
            norm_path = os.path.normcase(os.path.abspath(output_path))
            self._by_path.setdefault(norm_path, entry_id)
        norm_title = self._norm_text(title)
        if norm_title:
            self._by_title.setdefault(norm_title, []).append(
                (entry_id, self._norm_text(author))
            )

    def find(
        self,
        *,
        title: str = "",
        author: str = "",
        isbn: str | None = None,
        asin: str | None = None,
        scrape_url: str | None = None,
        output_path: str | None = None,
    ) -> int | None:
        norm_url = (scrape_url or "").strip()
        if norm_url and norm_url in self._by_url:
            return self._by_url[norm_url]
        norm_asin = (asin or "").strip().lower()
        if norm_asin and norm_asin in self._by_asin:
            return self._by_asin[norm_asin]
        norm_isbn = (isbn or "").strip().replace("-", "").lower()
        if norm_isbn and norm_isbn in self._by_isbn:
            return self._by_isbn[norm_isbn]
        if output_path:
            norm_path = os.path.normcase(os.path.abspath(output_path))
            if norm_path in self._by_path:
                return self._by_path[norm_path]
        norm_title = self._norm_text(title)
        norm_author = self._norm_text(author)
        if norm_title:
            matches = self._by_title.get(norm_title, [])
            if norm_author:
                for entry_id, row_author in matches:
                    if row_author and (
                        row_author == norm_author
                        or norm_author in row_author
                        or row_author in norm_author
                    ):
                        return entry_id
            if len(matches) == 1 and (not norm_author or not matches[0][1]):
                return matches[0][0]
        return None


def make_book_key(source_name: str, audiobook: Audiobook, fallback_url: str | None = None) -> str:
    """Build a stable deduplication key for an `Audiobook`."""
    scrape_url = (audiobook.metadata.scrape_url or "").strip()
    if scrape_url:
        base_key = scrape_url
    elif fallback_url and fallback_url.strip():
        base_key = fallback_url.strip()
    else:
        title = (audiobook.metadata.title or "").strip().lower()
        author = (audiobook.metadata.author or "").strip().lower()
        base_key = f"{source_name.lower()}::{title}::{author}"
    if is_ebook(audiobook):
        return f"{base_key}::ebook"
    return base_key


def make_catalog_book_key(source_name: str, book: CatalogBook) -> str:
    """Build a stable deduplication key matching `make_book_key` for a `CatalogBook`."""
    url = (book.url or "").strip()
    if url:
        return url
    title = (book.title or "").strip().lower()
    author = (book.author or "").strip().lower()
    return f"{source_name.lower()}::{title}::{author}"

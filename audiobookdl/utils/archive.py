from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import shlex
import sqlite3
import threading
from typing import Any
from collections.abc import Generator, Iterable, Sequence

from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.audiobook import Audiobook, is_ebook


VALID_LIBRARY_STATUSES: tuple[str, ...] = ("pending", "downloaded", "trashed")
_SCHEMA_VERSION = 3
_TAG_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")
_UNSET: Any = object()


def _normalize_output_path_key(output_path: str | None) -> str | None:
    """Return canonical case-folded absolute path for O(1) `output_path_norm` lookups."""
    cleaned = str(output_path or "").strip()
    if not cleaned:
        return None
    return os.path.normcase(os.path.abspath(cleaned))


def _fts5_phrase(value: str) -> str:
    """Quote an arbitrary search string as a literal FTS5 phrase token."""
    return '"' + value.replace('"', '""') + '"'


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


def default_archive_path(database_directory: str) -> str:
    """Return the default SQLite download ledger path inside `database_directory`."""
    return os.path.join(database_directory, "downloads.db")


def resolve_database_directory(
    config: Any,
    *,
    config_location: str | None = None,
) -> str:
    """Resolve the effective database directory from `config`, `config_location`, or default."""
    configured = getattr(config, "database_directory", None)
    if isinstance(configured, str) and configured.strip():
        return os.path.abspath(os.path.expanduser(configured.strip()))
    if config_location:
        return os.path.join(os.path.dirname(os.path.abspath(config_location)), "db")
    from audiobookdl.options_schema import default_database_directory

    return default_database_directory()


def resolve_archive_path(config: Any, explicit_path: str | None = None) -> str:
    """Resolve the SQLite ledger path from an explicit override, `config`, or default db dir."""
    if explicit_path:
        return explicit_path
    configured = getattr(config, "download_archive", None)
    if configured:
        return str(configured)
    return default_archive_path(resolve_database_directory(config))


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


def _sql_like_escape(value: str) -> str:
    """Escape SQLite `LIKE` wildcards using `\\` as the escape character."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class DownloadArchive:
    """Thread-safe SQLite-backed library & download history ledger."""

    def __init__(self, db_path: str, *, persistent: bool = False) -> None:
        self.db_path = os.path.abspath(db_path)
        self._persistent = persistent
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._fts5_trigram = False
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            if self._persistent:
                if self._conn is None:
                    self._conn = self._connect()
                with self._conn:
                    yield self._conn
                return
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @contextmanager
    def batch_connection(self) -> Generator[None, None, None]:
        """Keep a single SQLite connection open across bulk operations (such as library scanning)."""
        with self._lock:
            was_persistent = self._persistent
            self._persistent = True
            try:
                yield
            finally:
                self._persistent = was_persistent
                if not was_persistent and self._conn is not None:
                    self._conn.close()
                    self._conn = None

    def close(self) -> None:
        """Close the cached persistent SQLite connection, if open."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "DownloadArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            if version_row is not None and int(version_row[0]) == _SCHEMA_VERSION:
                fts_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'downloads_fts' LIMIT 1"
                ).fetchone()
                self._fts5_trigram = fts_row is not None
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    book_key TEXT NOT NULL,
                    url TEXT,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    output_path TEXT,
                    output_path_norm TEXT,
                    format TEXT,
                    downloaded_at TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL DEFAULT 'downloaded',
                    series TEXT,
                    series_order INTEGER,
                    narrator TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    isbn TEXT,
                    asin TEXT,
                    last_seen_at TEXT,
                    UNIQUE(source, book_key)
                )
                """
            )
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
            }
            migrations = (
                ("output_path_norm", "TEXT"),
                ("profile", "TEXT NOT NULL DEFAULT 'default'"),
                ("status", "TEXT NOT NULL DEFAULT 'downloaded'"),
                ("series", "TEXT"),
                ("series_order", "INTEGER"),
                ("narrator", "TEXT NOT NULL DEFAULT ''"),
                ("duration_seconds", "REAL"),
                ("isbn", "TEXT"),
                ("asin", "TEXT"),
                ("last_seen_at", "TEXT"),
            )
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE downloads ADD COLUMN {col_name} {col_def}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_tags (
                    item_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY(item_id, tag),
                    FOREIGN KEY(item_id) REFERENCES downloads(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_time ON downloads(downloaded_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_book_key ON downloads(book_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_asin ON downloads(asin)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_isbn ON downloads(isbn)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_output_path ON downloads(output_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_output_path_norm ON downloads(output_path_norm)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_title_nocase ON downloads(title COLLATE NOCASE)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag)"
            )
            missing_norm_rows = conn.execute(
                "SELECT id, output_path FROM downloads "
                "WHERE output_path IS NOT NULL AND output_path != '' "
                "AND (output_path_norm IS NULL OR output_path_norm = '')"
            ).fetchall()
            for r in missing_norm_rows:
                norm_val = _normalize_output_path_key(r["output_path"])
                if norm_val:
                    conn.execute(
                        "UPDATE downloads SET output_path_norm = ? WHERE id = ?",
                        (norm_val, int(r["id"])),
                    )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS downloads_fts USING fts5(
                        title, author, narrator, series, source, profile, url, isbn, asin,
                        content='downloads',
                        content_rowid='id',
                        tokenize='trigram'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS downloads_ai AFTER INSERT ON downloads BEGIN
                        INSERT INTO downloads_fts(rowid, title, author, narrator, series, source, profile, url, isbn, asin)
                        VALUES (new.id, new.title, new.author, new.narrator, COALESCE(new.series, ''), new.source, new.profile, COALESCE(new.url, ''), COALESCE(new.isbn, ''), COALESCE(new.asin, ''));
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS downloads_ad AFTER DELETE ON downloads BEGIN
                        INSERT INTO downloads_fts(downloads_fts, rowid, title, author, narrator, series, source, profile, url, isbn, asin)
                        VALUES ('delete', old.id, old.title, old.author, old.narrator, COALESCE(old.series, ''), old.source, old.profile, COALESCE(old.url, ''), COALESCE(old.isbn, ''), COALESCE(old.asin, ''));
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS downloads_au AFTER UPDATE OF title, author, narrator, series, source, profile, url, isbn, asin ON downloads BEGIN
                        INSERT INTO downloads_fts(downloads_fts, rowid, title, author, narrator, series, source, profile, url, isbn, asin)
                        VALUES ('delete', old.id, old.title, old.author, old.narrator, COALESCE(old.series, ''), old.source, old.profile, COALESCE(old.url, ''), COALESCE(old.isbn, ''), COALESCE(old.asin, ''));
                        INSERT INTO downloads_fts(rowid, title, author, narrator, series, source, profile, url, isbn, asin)
                        VALUES (new.id, new.title, new.author, new.narrator, COALESCE(new.series, ''), new.source, new.profile, COALESCE(new.url, ''), COALESCE(new.isbn, ''), COALESCE(new.asin, ''));
                    END
                    """
                )
                conn.execute("INSERT INTO downloads_fts(downloads_fts) VALUES('rebuild')")
                self._fts5_trigram = True
            except sqlite3.OperationalError:
                conn.execute("DROP TABLE IF EXISTS downloads_fts")
                self._fts5_trigram = False
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def contains(self, source: str, book_key: str) -> bool:
        """Return True if `(source, book_key)` is recorded as `'downloaded'` in the ledger."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE source = ? AND book_key = ? AND status = 'downloaded' LIMIT 1",
                (source, book_key),
            ).fetchone()
            return row is not None

    def get_entry_status(self, source: str, book_key: str) -> str | None:
        """Return the stored status for `(source, book_key)` or `None` if absent."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM downloads WHERE source = ? AND book_key = ? LIMIT 1",
                (source, book_key),
            ).fetchone()
            return str(row["status"]) if row is not None else None

    def record(
        self,
        source: str,
        book_key: str,
        *,
        title: str,
        author: str = "",
        url: str | None = None,
        output_path: str | None = None,
        audio_format: str | None = None,
        profile: str = "default",
        status: str = "downloaded",
        series: str | None = None,
        series_order: int | None = None,
        narrator: str = "",
        duration_seconds: float | None = None,
        isbn: str | None = None,
        asin: str | None = None,
    ) -> int:
        """Insert or update a library/download entry and return its row id."""
        if status not in VALID_LIBRARY_STATUSES:
            raise ValueError(f"Invalid library status: {status!r}")
        timestamp = _utc_now()
        norm_profile = (profile or "default").strip().lower() or "default"
        output_path_norm = _normalize_output_path_key(output_path)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO downloads (
                    source, book_key, url, title, author, output_path, output_path_norm, format, downloaded_at,
                    profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, book_key) DO UPDATE SET
                    url = COALESCE(excluded.url, downloads.url),
                    title = excluded.title,
                    author = CASE WHEN excluded.author != '' THEN excluded.author ELSE downloads.author END,
                    output_path = COALESCE(excluded.output_path, downloads.output_path),
                    output_path_norm = COALESCE(excluded.output_path_norm, downloads.output_path_norm),
                    format = COALESCE(excluded.format, downloads.format),
                    downloaded_at = excluded.downloaded_at,
                    profile = excluded.profile,
                    status = excluded.status,
                    series = COALESCE(excluded.series, downloads.series),
                    series_order = COALESCE(excluded.series_order, downloads.series_order),
                    narrator = CASE WHEN excluded.narrator != '' THEN excluded.narrator ELSE downloads.narrator END,
                    duration_seconds = COALESCE(excluded.duration_seconds, downloads.duration_seconds),
                    isbn = COALESCE(excluded.isbn, downloads.isbn),
                    asin = COALESCE(excluded.asin, downloads.asin),
                    last_seen_at = COALESCE(excluded.last_seen_at, downloads.last_seen_at)
                """,
                (
                    source,
                    book_key,
                    url,
                    title,
                    author,
                    output_path,
                    output_path_norm,
                    audio_format,
                    timestamp,
                    norm_profile,
                    status,
                    series,
                    series_order,
                    narrator,
                    duration_seconds,
                    isbn,
                    asin,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT id FROM downloads WHERE source = ? AND book_key = ? LIMIT 1",
                (source, book_key),
            ).fetchone()
            return int(row["id"])

    def upsert_catalog_items(
        self,
        source: str,
        books: Sequence[CatalogBook],
        *,
        profile: str = "default",
    ) -> tuple[int, int]:
        """
        Insert unseen `CatalogBook`s as `'pending'` and refresh metadata on existing entries
        without overwriting `'downloaded'` or `'trashed'` status.

        Returns `(inserted_count, updated_count)`.
        """
        timestamp = _utc_now()
        norm_profile = (profile or "default").strip().lower() or "default"
        inserted = 0
        updated = 0
        with self._connection() as conn:
            for book in books:
                item_source = (book.source or source).strip().lower() or source.lower()
                book_key = make_catalog_book_key(item_source, book)
                existing = conn.execute(
                    "SELECT id FROM downloads WHERE source = ? AND book_key = ? LIMIT 1",
                    (item_source, book_key),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO downloads (
                            source, book_key, url, title, author, downloaded_at,
                            profile, status, series, series_order, last_seen_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            item_source,
                            book_key,
                            book.url or None,
                            book.title,
                            book.author or "",
                            timestamp,
                            norm_profile,
                            book.series,
                            book.series_order,
                            timestamp,
                        ),
                    )
                    inserted += 1
                else:
                    conn.execute(
                        """
                        UPDATE downloads
                        SET url = COALESCE(?, url),
                            title = ?,
                            author = CASE WHEN ? != '' THEN ? ELSE author END,
                            series = COALESCE(?, series),
                            series_order = COALESCE(?, series_order),
                            profile = ?,
                            last_seen_at = ?
                        WHERE id = ?
                        """,
                        (
                            book.url or None,
                            book.title,
                            book.author or "",
                            book.author or "",
                            book.series,
                            book.series_order,
                            norm_profile,
                            timestamp,
                            int(existing["id"]),
                        ),
                    )
                    updated += 1
        return inserted, updated

    def lookup_catalog_statuses(
        self,
        source: str,
        books: Sequence[CatalogBook],
    ) -> list[str]:
        """Return display badge labels (`'Downloaded'`, `'Pending'`, `'Hidden'`, `'Trashed'`, or `''`) for `books`."""
        if not books:
            return []
        default_source = (source or "").strip().lower()
        target_sources = {
            (b.source or default_source).strip().lower()
            for b in books
            if (b.source or default_source).strip()
        }
        if default_source:
            target_sources.add(default_source)
        if target_sources:
            # Include locally scanned entries so title/author matching still finds local books.
            target_sources.add("local")
        with self._connection() as conn:
            if target_sources:
                placeholders = ",".join("?" for _ in target_sources)
                rows = conn.execute(
                    f"""
                    SELECT d.id, d.source, d.book_key, d.url, d.title, d.author, d.status,
                           EXISTS (
                               SELECT 1 FROM item_tags t
                               WHERE t.item_id = d.id AND t.tag = 'hidden'
                           ) AS is_hidden
                    FROM downloads d
                    WHERE lower(d.source) IN ({placeholders})
                    """,
                    tuple(sorted(target_sources)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT d.id, d.source, d.book_key, d.url, d.title, d.author, d.status,
                           EXISTS (
                               SELECT 1 FROM item_tags t
                               WHERE t.item_id = d.id AND t.tag = 'hidden'
                           ) AS is_hidden
                    FROM downloads d
                    """
                ).fetchall()

        if not rows:
            return [""] * len(books)

        _RANK = {"downloaded": 3, "pending": 2, "trashed": 1}
        by_source_key: dict[tuple[str, str], tuple[str, bool]] = {}
        by_url: dict[str, tuple[str, bool]] = {}
        row_badges: dict[int, tuple[str, bool]] = {}
        index = ArchiveMatchIndex()

        def _pick_better(
            existing: tuple[str, bool] | None,
            candidate: tuple[str, bool],
        ) -> tuple[str, bool]:
            if existing is None:
                return candidate
            if _RANK.get(candidate[0], 0) > _RANK.get(existing[0], 0):
                return candidate
            return existing

        for row in rows:
            entry_id = int(row["id"])
            row_source = str(row["source"] or "").strip().lower()
            book_key = str(row["book_key"] or "").strip()
            row_url = str(row["url"] or "").strip()
            status = str(row["status"] or "downloaded").strip().lower()
            is_hidden = bool(row["is_hidden"])
            badge_pair = (status, is_hidden)
            row_badges[entry_id] = badge_pair
            if row_source and book_key:
                key_pair = (row_source, book_key)
                by_source_key[key_pair] = _pick_better(by_source_key.get(key_pair), badge_pair)
            if row_url:
                by_url[row_url] = _pick_better(by_url.get(row_url), badge_pair)
            index.add(
                entry_id,
                book_key=book_key,
                url=row_url,
                title=str(row["title"] or ""),
                author=str(row["author"] or ""),
            )

        def _format_badge(pair: tuple[str, bool] | None) -> str:
            if pair is None:
                return ""
            status, is_hidden = pair
            if status == "trashed":
                return "Trashed"
            if status == "downloaded":
                return "Downloaded (Hidden)" if is_hidden else "Downloaded"
            if is_hidden:
                return "Hidden"
            if status == "pending":
                return "Pending"
            return status.capitalize()

        out: list[str] = []
        for book in books:
            item_source = (book.source or default_source).strip().lower() or default_source
            book_key = make_catalog_book_key(item_source, book)
            matched = by_source_key.get((item_source, book_key))
            if matched is None and book.url:
                matched = by_url.get(book.url.strip())
            if matched is None:
                matched_id = index.find(
                    title=book.title,
                    author=book.author,
                    scrape_url=book.url,
                )
                if matched_id is not None:
                    matched = row_badges.get(matched_id)
            out.append(_format_badge(matched))
        return out

    def set_status(self, entry_id: int, status: str) -> bool:
        """Update the lifecycle `status` (`pending`, `downloaded`, `trashed`) of `entry_id`."""
        if status not in VALID_LIBRARY_STATUSES:
            raise ValueError(f"Invalid library status: {status!r}")
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE downloads SET status = ? WHERE id = ?",
                (status, int(entry_id)),
            )
            return cur.rowcount > 0

    def _fetch_tags_for_ids(self, conn: sqlite3.Connection, item_ids: Sequence[int]) -> dict[int, tuple[str, ...]]:
        if not item_ids:
            return {}
        grouped: dict[int, list[str]] = {}
        chunk_size = 500
        for start in range(0, len(item_ids), chunk_size):
            batch = [int(i) for i in item_ids[start : start + chunk_size]]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT item_id, tag FROM item_tags WHERE item_id IN ({placeholders}) ORDER BY tag ASC",
                tuple(batch),
            ).fetchall()
            for row in rows:
                grouped.setdefault(int(row["item_id"]), []).append(str(row["tag"]))
        return {k: tuple(v) for k, v in grouped.items()}

    def get_tags(self, entry_id: int) -> tuple[str, ...]:
        """Return sorted normalized tags assigned to `entry_id`."""
        with self._connection() as conn:
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def add_tags(self, entry_id: int, tags: Iterable[str]) -> tuple[str, ...]:
        """Add one or more normalized tags to `entry_id` and return updated tags."""
        normalized = [t for t in (normalize_tag(raw) for raw in tags) if t]
        with self._connection() as conn:
            for tag in normalized:
                conn.execute(
                    "INSERT OR IGNORE INTO item_tags (item_id, tag) VALUES (?, ?)",
                    (int(entry_id), tag),
                )
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def remove_tags(self, entry_id: int, tags: Iterable[str]) -> tuple[str, ...]:
        """Remove one or more normalized tags from `entry_id` and return updated tags."""
        normalized = [t for t in (normalize_tag(raw) for raw in tags) if t]
        with self._connection() as conn:
            for tag in normalized:
                conn.execute(
                    "DELETE FROM item_tags WHERE item_id = ? AND tag = ?",
                    (int(entry_id), tag),
                )
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def set_tags(self, entry_id: int, tags: Iterable[str]) -> tuple[str, ...]:
        """Replace all tags on `entry_id` with normalized `tags`."""
        normalized = sorted({t for t in (normalize_tag(raw) for raw in tags) if t})
        with self._connection() as conn:
            conn.execute("DELETE FROM item_tags WHERE item_id = ?", (int(entry_id),))
            for tag in normalized:
                conn.execute(
                    "INSERT INTO item_tags (item_id, tag) VALUES (?, ?)",
                    (int(entry_id), tag),
                )
            return tuple(normalized)

    def _build_entry(self, row: sqlite3.Row, tags: tuple[str, ...]) -> ArchiveEntry:
        return ArchiveEntry(
            id=int(row["id"]),
            source=str(row["source"]),
            book_key=str(row["book_key"]),
            url=row["url"],
            title=str(row["title"]),
            author=str(row["author"]),
            output_path=row["output_path"],
            audio_format=row["format"],
            downloaded_at=str(row["downloaded_at"]),
            profile=str(row["profile"] or "default"),
            status=str(row["status"] or "downloaded"),
            series=row["series"],
            series_order=row["series_order"],
            narrator=str(row["narrator"] or ""),
            duration_seconds=row["duration_seconds"],
            isbn=row["isbn"],
            asin=row["asin"],
            last_seen_at=row["last_seen_at"],
            tags=tags,
        )

    @staticmethod
    def _matches_term(entry: ArchiveEntry, term: SearchTerm) -> bool:
        val = term.value
        if term.field == "tag":
            matched = any(val in tag for tag in entry.tags)
        elif term.field == "status":
            if val == "hidden":
                matched = entry.is_hidden
            else:
                matched = entry.status.lower() == val
        elif term.field == "author":
            matched = val in entry.author.lower()
        elif term.field == "title":
            matched = val in entry.title.lower()
        elif term.field == "narrator":
            matched = val in entry.narrator.lower()
        elif term.field == "series":
            matched = val in (entry.series or "").lower()
        elif term.field == "source":
            matched = val in entry.source.lower()
        elif term.field == "profile":
            matched = val in entry.profile.lower()
        elif term.field == "isbn":
            matched = val in (entry.isbn or "").lower()
        elif term.field == "asin":
            matched = val in (entry.asin or "").lower()
        else:
            haystack = " ".join(
                part
                for part in (
                    entry.title,
                    entry.author,
                    entry.narrator,
                    entry.series or "",
                    entry.source,
                    entry.profile,
                    entry.url or "",
                    entry.isbn or "",
                    entry.asin or "",
                    " ".join(entry.tags),
                )
                if part
            ).lower()
            matched = val in haystack
        return (not matched) if term.negated else matched

    def _build_filter_plan(
        self,
        *,
        query: str = "",
        include_trashed: bool = False,
        include_hidden: bool = True,
        status: str | None = None,
    ) -> tuple[list[SearchTerm], bool, str, list[object], bool]:
        terms = parse_library_query(query)
        explicit_status_query = any(t.field == "status" for t in terms)
        explicit_hidden_query = any(
            (t.field == "tag" and t.value == "hidden")
            or (t.field == "status" and t.value == "hidden")
            for t in terms
        )

        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        elif not include_trashed and not explicit_status_query:
            clauses.append("status != 'trashed'")

        if not include_hidden and not explicit_hidden_query:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag = 'hidden')"
            )

        _FIELD_COL_MAP = {
            "author": "author",
            "title": "title",
            "narrator": "narrator",
            "series": "series",
            "source": "source",
            "profile": "profile",
            "isbn": "isbn",
            "asin": "asin",
        }
        needs_python_filter = False
        use_fts5 = bool(getattr(self, "_fts5_trigram", False))
        for term in terms:
            val = term.value
            can_fts5 = use_fts5 and len(val) >= 3
            if term.field in _FIELD_COL_MAP and can_fts5:
                col = _FIELD_COL_MAP[term.field]
                op = "NOT IN" if term.negated else "IN"
                clauses.append(
                    f"downloads.id {op} (SELECT rowid FROM downloads_fts WHERE {col} MATCH ?)"
                )
                params.append(_fts5_phrase(val))
            elif term.field is None and can_fts5 and val.isascii():
                phrase = _fts5_phrase(val)
                pattern = f"%{_sql_like_escape(val)}%"
                if term.negated:
                    clauses.append(
                        "("
                        "downloads.id NOT IN (SELECT rowid FROM downloads_fts WHERE downloads_fts MATCH ?) "
                        "AND NOT EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag LIKE ? ESCAPE '\\')"
                        ")"
                    )
                else:
                    clauses.append(
                        "("
                        "downloads.id IN (SELECT rowid FROM downloads_fts WHERE downloads_fts MATCH ?) "
                        "OR EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag LIKE ? ESCAPE '\\')"
                        ")"
                    )
                params.extend([phrase, pattern])
            elif term.field is None and can_fts5 and not val.isascii():
                phrase = _fts5_phrase(val)
                if term.negated:
                    clauses.append(
                        "downloads.id NOT IN (SELECT rowid FROM downloads_fts WHERE downloads_fts MATCH ?)"
                    )
                else:
                    clauses.append(
                        "downloads.id IN (SELECT rowid FROM downloads_fts WHERE downloads_fts MATCH ?)"
                    )
                params.append(phrase)
            elif not term.negated and val.isascii():
                pattern = f"%{_sql_like_escape(val)}%"
                if term.field in _FIELD_COL_MAP:
                    col = _FIELD_COL_MAP[term.field]
                    clauses.append(f"COALESCE({col}, '') LIKE ? ESCAPE '\\'")
                    params.append(pattern)
                elif term.field == "tag":
                    clauses.append(
                        "EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag LIKE ? ESCAPE '\\')"
                    )
                    params.append(pattern)
                elif term.field == "status":
                    if val == "hidden":
                        clauses.append(
                            "EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag = 'hidden')"
                        )
                    else:
                        clauses.append("LOWER(status) = ?")
                        params.append(val)
                else:
                    clauses.append(
                        "("
                        "title LIKE ? ESCAPE '\\' OR "
                        "author LIKE ? ESCAPE '\\' OR "
                        "narrator LIKE ? ESCAPE '\\' OR "
                        "COALESCE(series, '') LIKE ? ESCAPE '\\' OR "
                        "source LIKE ? ESCAPE '\\' OR "
                        "profile LIKE ? ESCAPE '\\' OR "
                        "COALESCE(url, '') LIKE ? ESCAPE '\\' OR "
                        "COALESCE(isbn, '') LIKE ? ESCAPE '\\' OR "
                        "COALESCE(asin, '') LIKE ? ESCAPE '\\' OR "
                        "EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag LIKE ? ESCAPE '\\')"
                        ")"
                    )
                    params.extend([pattern] * 10)
            elif term.negated and val.isascii() and term.field == "tag":
                pattern = f"%{_sql_like_escape(val)}%"
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = downloads.id AND t.tag LIKE ? ESCAPE '\\')"
                )
                params.append(pattern)
            else:
                needs_python_filter = True

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return terms, explicit_hidden_query, where_sql, params, needs_python_filter

    def count_entries(
        self,
        *,
        query: str = "",
        include_trashed: bool = False,
        include_hidden: bool = True,
        status: str | None = None,
    ) -> int:
        """Return total count of ledger rows matching `query`, status, and visibility filters."""
        terms, explicit_hidden_query, where_sql, params, needs_python_filter = self._build_filter_plan(
            query=query,
            include_trashed=include_trashed,
            include_hidden=include_hidden,
            status=status,
        )
        if not needs_python_filter:
            with self._connection() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM downloads{where_sql}",
                    tuple(params),
                ).fetchone()
                return int(row["cnt"]) if row is not None else 0

        sql = (
            "SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,"
            " profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at"
            f" FROM downloads{where_sql}"
        )
        matched = 0
        with self._connection() as conn:
            cursor = conn.execute(sql, tuple(params))
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                tags_by_id = self._fetch_tags_for_ids(conn, [int(r["id"]) for r in batch])
                for row in batch:
                    entry = self._build_entry(row, tags_by_id.get(int(row["id"]), ()))
                    if not include_hidden and not explicit_hidden_query and entry.is_hidden:
                        continue
                    if terms and not all(self._matches_term(entry, term) for term in terms):
                        continue
                    matched += 1
        return matched

    def list_entries(
        self,
        *,
        query: str = "",
        limit: int = 500,
        offset: int = 0,
        include_trashed: bool = False,
        include_hidden: bool = True,
        status: str | None = None,
    ) -> list[ArchiveEntry]:
        """Return library/ledger rows filtered by rich `query`, status, and visibility flags."""
        terms, explicit_hidden_query, where_sql, params, needs_python_filter = self._build_filter_plan(
            query=query,
            include_trashed=include_trashed,
            include_hidden=include_hidden,
            status=status,
        )
        effective_limit = max(1, int(limit))
        effective_offset = max(0, int(offset))

        sql = """
            SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,
                   profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
            FROM downloads
        """ + where_sql + " ORDER BY id DESC"
        if not needs_python_filter:
            sql += " LIMIT ? OFFSET ?"
            params.extend([effective_limit, effective_offset])

        results: list[ArchiveEntry] = []
        skipped = 0
        with self._connection() as conn:
            cursor = conn.execute(sql, tuple(params))
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                tags_by_id = self._fetch_tags_for_ids(conn, [int(r["id"]) for r in batch])
                for row in batch:
                    entry = self._build_entry(row, tags_by_id.get(int(row["id"]), ()))
                    if not include_hidden and not explicit_hidden_query and entry.is_hidden:
                        continue
                    if terms and not all(self._matches_term(entry, term) for term in terms):
                        continue
                    if needs_python_filter and skipped < effective_offset:
                        skipped += 1
                        continue
                    results.append(entry)
                    if len(results) >= effective_limit:
                        return results
        return results

    def update_entry(
        self,
        entry_id: int,
        *,
        title: str | Any = _UNSET,
        author: str | Any = _UNSET,
        narrator: str | Any = _UNSET,
        series: str | None | Any = _UNSET,
        series_order: int | None | Any = _UNSET,
        isbn: str | None | Any = _UNSET,
        asin: str | None | Any = _UNSET,
        output_path: str | None | Any = _UNSET,
        status: str | Any = _UNSET,
        tags: Iterable[str] | None | Any = _UNSET,
    ) -> ArchiveEntry | None:
        """Update editable metadata, path, status, and optional tags on `entry_id`."""
        assignments: list[str] = []
        params: list[object] = []

        if title is not _UNSET:
            clean_title = str(title or "").strip()
            if not clean_title:
                raise ValueError("Title cannot be empty.")
            assignments.append("title = ?")
            params.append(clean_title)
        if author is not _UNSET:
            assignments.append("author = ?")
            params.append(str(author or "").strip())
        if narrator is not _UNSET:
            assignments.append("narrator = ?")
            params.append(str(narrator or "").strip())
        if series is not _UNSET:
            clean_series = str(series or "").strip() or None
            assignments.append("series = ?")
            params.append(clean_series)
        if series_order is not _UNSET:
            assignments.append("series_order = ?")
            params.append(int(series_order) if series_order is not None else None)
        if isbn is not _UNSET:
            clean_isbn = str(isbn or "").strip() or None
            assignments.append("isbn = ?")
            params.append(clean_isbn)
        if asin is not _UNSET:
            clean_asin = str(asin or "").strip() or None
            assignments.append("asin = ?")
            params.append(clean_asin)
        if output_path is not _UNSET:
            clean_out = str(output_path or "").strip() or None
            assignments.append("output_path = ?")
            params.append(clean_out)
            assignments.append("output_path_norm = ?")
            params.append(_normalize_output_path_key(clean_out))
        if status is not _UNSET:
            clean_status = str(status or "").strip().lower()
            if clean_status not in VALID_LIBRARY_STATUSES:
                raise ValueError(f"Invalid library status: {status!r}")
            assignments.append("status = ?")
            params.append(clean_status)

        target_id = int(entry_id)
        with self._connection() as conn:
            exists_row = conn.execute(
                "SELECT 1 FROM downloads WHERE id = ? LIMIT 1",
                (target_id,),
            ).fetchone()
            if exists_row is None:
                return None
            if assignments:
                params.append(target_id)
                conn.execute(
                    f"UPDATE downloads SET {', '.join(assignments)} WHERE id = ?",
                    tuple(params),
                )
            if tags is not _UNSET and tags is not None:
                normalized = sorted({t for t in (normalize_tag(raw) for raw in tags) if t})
                conn.execute("DELETE FROM item_tags WHERE item_id = ?", (target_id,))
                for tag in normalized:
                    conn.execute(
                        "INSERT INTO item_tags (item_id, tag) VALUES (?, ?)",
                        (target_id, tag),
                    )
        return self.get_entry(target_id)

    def get_entry(self, entry_id: int) -> ArchiveEntry | None:
        """Fetch a single `ArchiveEntry` by row id, or `None` if not found."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,
                       profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
                FROM downloads WHERE id = ? LIMIT 1
                """,
                (int(entry_id),),
            ).fetchone()
            if row is None:
                return None
            tags = self._fetch_tags_for_ids(conn, [int(entry_id)]).get(int(entry_id), ())
            return self._build_entry(row, tags)

    def get_entry_by_key(self, source: str, book_key: str) -> ArchiveEntry | None:
        """Fetch a single `ArchiveEntry` by `(source, book_key)` or return `None`."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,
                       profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
                FROM downloads WHERE source = ? AND book_key = ? LIMIT 1
                """,
                (source, book_key),
            ).fetchone()
            if row is None:
                return None
            entry_id = int(row["id"])
            tags = self._fetch_tags_for_ids(conn, [entry_id]).get(entry_id, ())
            return self._build_entry(row, tags)

    def get_entries_by_keys(
        self,
        keys: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], ArchiveEntry]:
        """Batch-fetch `ArchiveEntry` rows for `(source, book_key)` pairs."""
        by_source: dict[str, list[str]] = {}
        for source, book_key in keys:
            if not book_key:
                continue
            src_list = by_source.setdefault(source, [])
            if book_key not in src_list:
                src_list.append(book_key)
        if not by_source:
            return {}
        matched_rows: list[sqlite3.Row] = []
        with self._connection() as conn:
            for source, key_list in by_source.items():
                for start in range(0, len(key_list), 500):
                    batch = key_list[start : start + 500]
                    placeholders = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"""
                        SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,
                               profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
                        FROM downloads
                        WHERE source = ? AND book_key IN ({placeholders})
                        """,
                        (source, *batch),
                    ).fetchall()
                    matched_rows.extend(rows)
            if not matched_rows:
                return {}
            tags_by_id = self._fetch_tags_for_ids(
                conn, [int(row["id"]) for row in matched_rows]
            )
        result: dict[tuple[str, str], ArchiveEntry] = {}
        for row in matched_rows:
            entry = self._build_entry(row, tags_by_id.get(int(row["id"]), ()))
            result[(entry.source, entry.book_key)] = entry
        return result

    def find_matching_entry_id(
        self,
        *,
        title: str = "",
        author: str = "",
        isbn: str | None = None,
        asin: str | None = None,
        scrape_url: str | None = None,
        output_path: str | None = None,
    ) -> int | None:
        """Find an existing ledger row matching identifiers, path, or title/author."""
        norm_url = (scrape_url or "").strip()
        norm_asin = (asin or "").strip().lower()
        norm_isbn = (isbn or "").strip().replace("-", "").lower()
        abs_path = os.path.abspath(output_path) if output_path else ""
        norm_path = os.path.normcase(abs_path) if abs_path else ""
        norm_title = " ".join((title or "").casefold().split())
        norm_author = " ".join((author or "").casefold().split())

        with self._connection() as conn:
            if norm_url:
                row = conn.execute(
                    "SELECT id FROM downloads WHERE book_key = ? OR url = ? LIMIT 1",
                    (norm_url, norm_url),
                ).fetchone()
                if row is not None:
                    return int(row["id"])

            if norm_asin:
                row = conn.execute(
                    "SELECT id FROM downloads WHERE asin = ? COLLATE NOCASE OR TRIM(LOWER(asin)) = ? LIMIT 1",
                    (norm_asin, norm_asin),
                ).fetchone()
                if row is not None:
                    return int(row["id"])

            if norm_isbn:
                row = conn.execute(
                    "SELECT id FROM downloads WHERE isbn = ? OR REPLACE(LOWER(TRIM(isbn)), '-', '') = ? LIMIT 1",
                    (isbn.strip(), norm_isbn) if isbn else ("", norm_isbn),
                ).fetchone()
                if row is not None:
                    return int(row["id"])

            if norm_path:
                row = conn.execute(
                    "SELECT id FROM downloads WHERE output_path_norm = ? LIMIT 1",
                    (norm_path,),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
                row = conn.execute(
                    "SELECT id FROM downloads WHERE output_path = ? OR output_path = ? LIMIT 1",
                    (output_path, abs_path),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
                path_rows = conn.execute(
                    "SELECT id, output_path FROM downloads "
                    "WHERE output_path_norm IS NULL AND output_path IS NOT NULL AND output_path != ''"
                ).fetchall()
                for r in path_rows:
                    rp = str(r["output_path"] or "").strip()
                    if rp and os.path.normcase(os.path.abspath(rp)) == norm_path:
                        return int(r["id"])

            if norm_title:
                title_rows = conn.execute(
                    "SELECT id, title, author FROM downloads WHERE title = ? COLLATE NOCASE",
                    (norm_title,),
                ).fetchall()
                if not title_rows and title:
                    title_rows = conn.execute(
                        "SELECT id, title, author FROM downloads WHERE title = ? COLLATE NOCASE",
                        (title.strip(),),
                    ).fetchall()
                title_matches = [
                    r
                    for r in title_rows
                    if " ".join(str(r["title"] or "").casefold().split()) == norm_title
                ]
                if norm_author:
                    for r in title_matches:
                        row_author = " ".join(str(r["author"] or "").casefold().split())
                        if row_author and (
                            row_author == norm_author
                            or norm_author in row_author
                            or row_author in norm_author
                        ):
                            return int(r["id"])
                if len(title_matches) == 1 and (
                    not norm_author or not str(title_matches[0]["author"] or "").strip()
                ):
                    return int(title_matches[0]["id"])
        return None

    def find_cross_source_duplicate(
        self,
        source: str,
        book_key: str = "",
        *,
        title: str = "",
        author: str = "",
        isbn: str | None = None,
        asin: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an active non-trashed ledger row from another entry matching ASIN, ISBN, or title/author."""
        norm_asin = (asin or "").strip().lower()
        norm_isbn = (isbn or "").strip().replace("-", "").lower()
        norm_title = " ".join((title or "").casefold().split())
        norm_author = " ".join((author or "").casefold().split())
        src_clean = (source or "").strip()
        key_clean = (book_key or "").strip()

        def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
            return {
                "id": int(row["id"]),
                "source": str(row["source"] or ""),
                "book_key": str(row["book_key"] or ""),
                "title": str(row["title"] or ""),
                "author": str(row["author"] or ""),
                "output_path": str(row["output_path"] or ""),
                "status": str(row["status"] or ""),
            }

        with self._connection() as conn:
            excl_sql = (
                "status IN ('downloaded', 'queued', 'sync') "
                "AND NOT (source = ? AND book_key = ?)"
            )
            cols = "id, source, book_key, title, author, output_path, status"
            if norm_asin:
                row = conn.execute(
                    f"SELECT {cols} FROM downloads WHERE {excl_sql} AND TRIM(LOWER(asin)) = ? LIMIT 1",
                    (src_clean, key_clean, norm_asin),
                ).fetchone()
                if row is not None:
                    return _row_dict(row)
            if norm_isbn:
                row = conn.execute(
                    f"SELECT {cols} FROM downloads WHERE {excl_sql} AND REPLACE(LOWER(TRIM(isbn)), '-', '') = ? LIMIT 1",
                    (src_clean, key_clean, norm_isbn),
                ).fetchone()
                if row is not None:
                    return _row_dict(row)
            if norm_title:
                title_rows = conn.execute(
                    f"SELECT {cols} FROM downloads WHERE {excl_sql} AND title = ? COLLATE NOCASE",
                    (src_clean, key_clean, norm_title),
                ).fetchall()
                if not title_rows and title:
                    title_rows = conn.execute(
                        f"SELECT {cols} FROM downloads WHERE {excl_sql} AND title = ? COLLATE NOCASE",
                        (src_clean, key_clean, title.strip()),
                    ).fetchall()
                exact_title_rows = [
                    r
                    for r in title_rows
                    if " ".join(str(r["title"] or "").casefold().split()) == norm_title
                ]
                if norm_author:
                    for r in exact_title_rows:
                        row_author = " ".join(str(r["author"] or "").casefold().split())
                        if row_author and (
                            row_author == norm_author
                            or norm_author in row_author
                            or row_author in norm_author
                        ):
                            return _row_dict(r)
                elif len(exact_title_rows) == 1 and not str(exact_title_rows[0]["author"] or "").strip():
                    return _row_dict(exact_title_rows[0])
        return None

    def build_match_index(self) -> "ArchiveMatchIndex":
        """Load an in-memory O(1) lookup index across all ledger rows for bulk matching."""
        index = ArchiveMatchIndex()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, book_key, url, title, author, output_path, isbn, asin FROM downloads"
            ).fetchall()
        for row in rows:
            index.add(
                int(row["id"]),
                book_key=row["book_key"],
                url=row["url"],
                title=row["title"],
                author=row["author"],
                output_path=row["output_path"],
                isbn=row["isbn"],
                asin=row["asin"],
            )
        return index

    def mark_local_match(
        self,
        entry_id: int,
        *,
        output_path: str,
        audio_format: str | None = None,
        series: str | None = None,
        series_order: int | None = None,
        narrator: str = "",
        isbn: str | None = None,
        asin: str | None = None,
    ) -> bool:
        """Link an existing entry to a discovered local `output_path` and mark it `'downloaded'`."""
        timestamp = _utc_now()
        with self._connection() as conn:
            cur = conn.execute(
                """
                UPDATE downloads
                SET output_path = ?,
                    format = COALESCE(?, format),
                    status = CASE WHEN status = 'trashed' THEN 'trashed' ELSE 'downloaded' END,
                    series = COALESCE(series, ?),
                    series_order = COALESCE(series_order, ?),
                    narrator = CASE WHEN narrator != '' THEN narrator ELSE ? END,
                    isbn = COALESCE(isbn, ?),
                    asin = COALESCE(asin, ?),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    output_path,
                    audio_format,
                    series,
                    series_order,
                    narrator,
                    isbn,
                    asin,
                    timestamp,
                    int(entry_id),
                ),
            )
            return cur.rowcount > 0

    def delete_entry(self, entry_id: int) -> bool:
        """Permanently delete a single entry by row id; return True if a row was removed."""
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM downloads WHERE id = ?", (int(entry_id),))
            return cur.rowcount > 0

    def clear(self) -> int:
        """Delete all ledger entries and return count removed."""
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM downloads")
            return max(0, int(cur.rowcount))

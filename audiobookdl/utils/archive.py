from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import shlex
import sqlite3
import threading
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.audiobook import Audiobook, is_ebook


VALID_LIBRARY_STATUSES: Tuple[str, ...] = ("pending", "downloaded", "trashed")
_SCHEMA_VERSION = 2
_TAG_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class ArchiveEntry:
    """Single library or download entry stored in the SQLite ledger."""

    id: int
    source: str
    book_key: str
    url: Optional[str]
    title: str
    author: str
    output_path: Optional[str]
    audio_format: Optional[str]
    downloaded_at: str
    profile: str = "default"
    status: str = "downloaded"
    series: Optional[str] = None
    series_order: Optional[int] = None
    narrator: str = ""
    duration_seconds: Optional[float] = None
    isbn: Optional[str] = None
    asin: Optional[str] = None
    last_seen_at: Optional[str] = None
    tags: Tuple[str, ...] = ()

    @property
    def is_hidden(self) -> bool:
        """Return True if this entry carries the reserved `hidden` tag."""
        return "hidden" in self.tags


@dataclass(frozen=True)
class SearchTerm:
    """Parsed token from a library search query."""

    field: Optional[str]
    value: str
    negated: bool = False


def normalize_tag(raw: str) -> str:
    """Normalize a tag to lowercase `[a-z0-9_-]` characters."""
    cleaned = _TAG_CLEAN_RE.sub("_", raw.strip().lower()).strip("_")
    return cleaned


def parse_tags_input(raw: str) -> List[str]:
    """Parse space- or comma-separated tags into unique normalized tags."""
    tokens = re.split(r"[\s,]+", raw.strip())
    out: List[str] = []
    for token in tokens:
        tag = normalize_tag(token)
        if tag and tag not in out:
            out.append(tag)
    return out


def parse_library_query(query: str) -> List[SearchTerm]:
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
    terms: List[SearchTerm] = []
    for token in tokens:
        negated = False
        body = token
        if body.startswith("-") and len(body) > 1:
            negated = True
            body = body[1:]
        field: Optional[str] = None
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


def make_book_key(source_name: str, audiobook: Audiobook, fallback_url: Optional[str] = None) -> str:
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
        self._conn: Optional[sqlite3.Connection] = None
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
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
                "CREATE INDEX IF NOT EXISTS idx_downloads_title_nocase ON downloads(title COLLATE NOCASE)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag)"
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def contains(self, source: str, book_key: str) -> bool:
        """Return True if `(source, book_key)` is recorded as `'downloaded'` in the ledger."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE source = ? AND book_key = ? AND status = 'downloaded' LIMIT 1",
                (source, book_key),
            ).fetchone()
            return row is not None

    def get_entry_status(self, source: str, book_key: str) -> Optional[str]:
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
        url: Optional[str] = None,
        output_path: Optional[str] = None,
        audio_format: Optional[str] = None,
        profile: str = "default",
        status: str = "downloaded",
        series: Optional[str] = None,
        series_order: Optional[int] = None,
        narrator: str = "",
        duration_seconds: Optional[float] = None,
        isbn: Optional[str] = None,
        asin: Optional[str] = None,
    ) -> int:
        """Insert or update a library/download entry and return its row id."""
        if status not in VALID_LIBRARY_STATUSES:
            raise ValueError(f"Invalid library status: {status!r}")
        timestamp = _utc_now()
        norm_profile = (profile or "default").strip().lower() or "default"
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO downloads (
                    source, book_key, url, title, author, output_path, format, downloaded_at,
                    profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, book_key) DO UPDATE SET
                    url = COALESCE(excluded.url, downloads.url),
                    title = excluded.title,
                    author = CASE WHEN excluded.author != '' THEN excluded.author ELSE downloads.author END,
                    output_path = COALESCE(excluded.output_path, downloads.output_path),
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
    ) -> Tuple[int, int]:
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

    def _fetch_tags_for_ids(self, conn: sqlite3.Connection, item_ids: Sequence[int]) -> Dict[int, Tuple[str, ...]]:
        if not item_ids:
            return {}
        grouped: Dict[int, List[str]] = {}
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

    def get_tags(self, entry_id: int) -> Tuple[str, ...]:
        """Return sorted normalized tags assigned to `entry_id`."""
        with self._connection() as conn:
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def add_tags(self, entry_id: int, tags: Iterable[str]) -> Tuple[str, ...]:
        """Add one or more normalized tags to `entry_id` and return updated tags."""
        normalized = [t for t in (normalize_tag(raw) for raw in tags) if t]
        with self._connection() as conn:
            for tag in normalized:
                conn.execute(
                    "INSERT OR IGNORE INTO item_tags (item_id, tag) VALUES (?, ?)",
                    (int(entry_id), tag),
                )
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def remove_tags(self, entry_id: int, tags: Iterable[str]) -> Tuple[str, ...]:
        """Remove one or more normalized tags from `entry_id` and return updated tags."""
        normalized = [t for t in (normalize_tag(raw) for raw in tags) if t]
        with self._connection() as conn:
            for tag in normalized:
                conn.execute(
                    "DELETE FROM item_tags WHERE item_id = ? AND tag = ?",
                    (int(entry_id), tag),
                )
            return self._fetch_tags_for_ids(conn, [entry_id]).get(int(entry_id), ())

    def set_tags(self, entry_id: int, tags: Iterable[str]) -> Tuple[str, ...]:
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

    def _build_entry(self, row: sqlite3.Row, tags: Tuple[str, ...]) -> ArchiveEntry:
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

    def list_entries(
        self,
        *,
        query: str = "",
        limit: int = 500,
        include_trashed: bool = False,
        include_hidden: bool = True,
        status: Optional[str] = None,
    ) -> List[ArchiveEntry]:
        """Return library/ledger rows filtered by rich `query`, status, and visibility flags."""
        terms = parse_library_query(query)
        explicit_status_query = any(t.field == "status" for t in terms)
        explicit_hidden_query = any(
            (t.field == "tag" and t.value == "hidden")
            or (t.field == "status" and t.value == "hidden")
            for t in terms
        )
        effective_limit = max(1, int(limit))

        sql = """
            SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at,
                   profile, status, series, series_order, narrator, duration_seconds, isbn, asin, last_seen_at
            FROM downloads
        """
        clauses: List[str] = []
        params: List[object] = []
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
        for term in terms:
            val = term.value
            # SQLite `LIKE` is ASCII case-insensitive only; push ASCII predicates to SQL
            # and keep Python `_matches_term()` for full Unicode & negated edge cases.
            if not term.negated and val.isascii():
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
            else:
                needs_python_filter = True

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        if not needs_python_filter:
            sql += " LIMIT ?"
            params.append(effective_limit)

        results: List[ArchiveEntry] = []
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
                    results.append(entry)
                    if len(results) >= effective_limit:
                        return results
        return results

    def get_entry(self, entry_id: int) -> Optional[ArchiveEntry]:
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

    def get_entry_by_key(self, source: str, book_key: str) -> Optional[ArchiveEntry]:
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

    def find_matching_entry_id(
        self,
        *,
        title: str = "",
        author: str = "",
        isbn: Optional[str] = None,
        asin: Optional[str] = None,
        scrape_url: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> Optional[int]:
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
                    "SELECT id FROM downloads WHERE output_path = ? OR output_path = ? LIMIT 1",
                    (output_path, abs_path),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
                path_rows = conn.execute(
                    "SELECT id, output_path FROM downloads WHERE output_path IS NOT NULL AND output_path != ''"
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

    def mark_local_match(
        self,
        entry_id: int,
        *,
        output_path: str,
        audio_format: Optional[str] = None,
        series: Optional[str] = None,
        series_order: Optional[int] = None,
        narrator: str = "",
        isbn: Optional[str] = None,
        asin: Optional[str] = None,
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

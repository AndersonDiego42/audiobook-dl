from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3
from typing import List, Optional

from audiobookdl.utils.audiobook import Audiobook, is_ebook


@dataclass(frozen=True)
class ArchiveEntry:
    """Single historical download entry stored in the SQLite ledger."""

    id: int
    source: str
    book_key: str
    url: Optional[str]
    title: str
    author: str
    output_path: Optional[str]
    audio_format: Optional[str]
    downloaded_at: str


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


class DownloadArchive:
    """Thread-safe SQLite-backed download archive and history ledger."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
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
                    UNIQUE(source, book_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_time ON downloads(downloaded_at DESC)"
            )

    def contains(self, source: str, book_key: str) -> bool:
        """Return True if `(source, book_key)` is already recorded in the ledger."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE source = ? AND book_key = ? LIMIT 1",
                (source, book_key),
            ).fetchone()
            return row is not None

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
    ) -> None:
        """Insert or update a completed download entry cleanly."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads (source, book_key, url, title, author, output_path, format, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, book_key) DO UPDATE SET
                    url = COALESCE(excluded.url, downloads.url),
                    title = excluded.title,
                    author = excluded.author,
                    output_path = COALESCE(excluded.output_path, downloads.output_path),
                    format = COALESCE(excluded.format, downloads.format),
                    downloaded_at = excluded.downloaded_at
                """,
                (source, book_key, url, title, author, output_path, audio_format, timestamp),
            )

    def list_entries(self, *, query: str = "", limit: int = 500) -> List[ArchiveEntry]:
        """Return recent download ledger rows filtered optionally by substring `query`."""
        cleaned = query.strip().lower()
        with self._connect() as conn:
            if cleaned:
                like = f"%{cleaned}%"
                rows = conn.execute(
                    """
                    SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at
                    FROM downloads
                    WHERE lower(title) LIKE ?
                       OR lower(author) LIKE ?
                       OR lower(source) LIKE ?
                       OR lower(COALESCE(url, '')) LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (like, like, like, like, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, source, book_key, url, title, author, output_path, format, downloaded_at
                    FROM downloads
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
        return [
            ArchiveEntry(
                id=int(row["id"]),
                source=str(row["source"]),
                book_key=str(row["book_key"]),
                url=row["url"],
                title=str(row["title"]),
                author=str(row["author"]),
                output_path=row["output_path"],
                audio_format=row["format"],
                downloaded_at=str(row["downloaded_at"]),
            )
            for row in rows
        ]

    def delete_entry(self, entry_id: int) -> bool:
        """Delete a single entry by row id; return True if a row was removed."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM downloads WHERE id = ?", (int(entry_id),))
            return cur.rowcount > 0

    def clear(self) -> int:
        """Delete all ledger entries and return count removed."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM downloads")
            return max(0, int(cur.rowcount))

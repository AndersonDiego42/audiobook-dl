from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any
from collections.abc import Iterator, Mapping


DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_CACHE_SCHEMA_VERSION = 1


def default_enrichment_cache_path(database_directory: str) -> str:
    """Return the default SQLite enrichment cache path inside `database_directory`."""
    return os.path.join(database_directory, "enrichment_cache.db")


def make_cache_key(
    provider: str,
    endpoint: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Build a deterministic SHA-256 cache key for a provider HTTP query."""
    normalized_params = sorted(
        (str(k), "" if v is None else str(v))
        for k, v in (params or {}).items()
    )
    normalized_headers = sorted(
        (str(k).lower(), str(v).strip())
        for k, v in (headers or {}).items()
    )
    canonical = json.dumps(
        {
            "provider": provider.strip().lower(),
            "endpoint": endpoint.strip(),
            "params": normalized_params,
            "headers": normalized_headers,
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{provider.strip().lower()}:{digest}"


class EnrichmentCache:
    """Thread-safe SQLite-backed HTTP JSON response cache with TTL expiration."""

    def __init__(
        self,
        db_path: str,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        persistent: bool = False,
    ) -> None:
        self.db_path = os.path.abspath(db_path)
        self.ttl_seconds = float(ttl_seconds)
        self._persistent = persistent
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._pruned = False
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
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
        """Close any persistent SQLite connection held by this cache."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "EnrichmentCache":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            if version_row is not None and int(version_row[0]) == _CACHE_SCHEMA_VERSION:
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enrichment_cache_fetched ON enrichment_cache(fetched_at)"
            )
            conn.execute(f"PRAGMA user_version = {_CACHE_SCHEMA_VERSION}")

    def get(
        self,
        cache_key: str,
        *,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> Any | None:
        """Return cached JSON payload for `cache_key` if present and within TTL, else `None`."""
        effective_ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        current_ts = time.time() if now is None else float(now)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at FROM enrichment_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        fetched_at = float(row["fetched_at"])
        if effective_ttl <= 0 or (current_ts - fetched_at) > effective_ttl:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None

    def prune_expired(self, *, now: float | None = None) -> int:
        """Delete rows older than `ttl_seconds` and return the number removed."""
        current_ts = time.time() if now is None else float(now)
        cutoff = current_ts - self.ttl_seconds
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM enrichment_cache WHERE fetched_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def set(
        self,
        cache_key: str,
        provider: str,
        payload: Any,
        *,
        now: float | None = None,
    ) -> None:
        """Persist `payload` as JSON for `cache_key` under `provider`."""
        if not self._pruned:
            self._pruned = True
            try:
                self.prune_expired()
            except sqlite3.Error:
                pass
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        timestamp = time.time() if now is None else float(now)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO enrichment_cache (cache_key, provider, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider = excluded.provider,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (cache_key, provider.strip().lower(), serialized, timestamp),
            )

    def clear(self, provider: str | None = None) -> int:
        """Delete cached entries (optionally scoped to `provider`) and return row count."""
        with self._connection() as conn:
            if provider:
                cursor = conn.execute(
                    "DELETE FROM enrichment_cache WHERE provider = ?",
                    (provider.strip().lower(),),
                )
            else:
                cursor = conn.execute("DELETE FROM enrichment_cache")
            return int(cursor.rowcount)

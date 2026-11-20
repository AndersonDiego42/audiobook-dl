"""Per-thread pooled HTTP sessions for GUI background tasks."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import TYPE_CHECKING, Any

import requests

from audiobookdl.utils.http import create_configured_http_session

if TYPE_CHECKING:
    from audiobookdl.config import Config


class GuiHttpPool:
    """Owns thread-local `requests.Session` objects configured from the current `Config`."""

    def __init__(self, config_snapshot: Callable[[], Config]) -> None:
        self._config_snapshot = config_snapshot
        self._thread_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._thread_sessions: list[Any] = []

    def get_http_session(self) -> Any:
        """Return a per-thread pooled `requests.Session` configured with user proxy settings."""
        cfg = self._config_snapshot()
        desired_proxy = (cfg.proxy or "").strip() or None
        session = getattr(self._thread_local, "http_session", None)
        if session is None:
            session = create_configured_http_session(cfg, proxy=desired_proxy)
            self._thread_local.http_session = session
            with self._sessions_lock:
                self._thread_sessions.append(session)
        else:
            proxies = getattr(session, "proxies", None)
            if isinstance(proxies, dict):
                if desired_proxy:
                    proxies["http"] = desired_proxy
                    proxies["https"] = desired_proxy
                else:
                    proxies.pop("http", None)
                    proxies.pop("https", None)
        return session

    def fetch_cover_bytes(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout: float | None = None,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> bytes | None:
        """Download cover image bytes via pooled thread-local session, respecting proxy, timeout, and size cap."""
        if not url or not url.lower().startswith(("http://", "https://")):
            return None
        cfg = self._config_snapshot()
        eff_proxy = (proxy.strip() if isinstance(proxy, str) else "") or (cfg.proxy or "").strip() or None
        proxies = {"http": eff_proxy, "https": eff_proxy} if eff_proxy else None
        eff_timeout = (
            float(timeout)
            if timeout is not None and float(timeout) > 0
            else (float(cfg.timeout) if cfg.timeout and cfg.timeout > 0 else 10.0)
        )
        session = self.get_http_session()
        try:
            with session.get(
                url,
                timeout=eff_timeout,
                proxies=proxies,
                headers={"User-Agent": "audiobook-dl"},
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                raw_len = resp.headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        if int(str(raw_len).strip()) > max_bytes:
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data if data else None
        except (OSError, requests.RequestException):
            return None

    def close_all(self) -> None:
        """Close every pooled thread-local session and clear the tracking list."""
        with self._sessions_lock:
            sessions = list(self._thread_sessions)
            self._thread_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except (OSError, requests.RequestException):
                pass

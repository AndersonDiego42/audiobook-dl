"""Cover thumbnail HTTP session pooling, LRU image caches, and background cover loaders."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
import threading
import tkinter as tk
from typing import Any

import requests

from audiobookdl.config import Config
from audiobookdl.gui.formatting import (
    create_placeholder_cover_thumbnail,
    photo_from_image_bytes,
    resize_cover_bytes_aspect_thumbnail,
    resize_cover_thumbnail_bytes,
)
from audiobookdl.gui.theme import ThemePalette
from audiobookdl.gui.widgets import MainThreadCallbackQueue
from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.http import create_configured_http_session, resolve_http_timeout


class CatalogCoverLoader:
    """Manages pooled cover HTTP fetching, LRU caches, and background thumbnail workers."""

    MAX_COVER_CACHE = 2000
    MAX_DETAIL_COVER_CACHE = 128
    MAX_COVER_BYTES = 5 * 1024 * 1024

    def __init__(
        self,
        widget: tk.Misc,
        cb_queue: MainThreadCallbackQueue,
        *,
        thumb_size: int,
        detail_thumb_size: int,
        cover_fetcher: Callable[[str], bytes | None] | None = None,
        config_provider: Callable[[], Config] | None = None,
    ) -> None:
        self._widget = widget
        self._cb_queue = cb_queue
        self._thumb_size = thumb_size
        self._detail_thumb_size = detail_thumb_size
        self._cover_fetcher = cover_fetcher
        self._config_provider = config_provider

        self._thread_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: list[requests.Session] = []

        self.cover_fetch_token = 0
        self.detail_token = 0
        self.cover_png_cache: OrderedDict[str, bytes | None] = OrderedDict()
        self.detail_cover_png_cache: OrderedDict[str, bytes | None] = OrderedDict()
        self.url_photos: OrderedDict[str, Any] = OrderedDict()
        self.row_photos: dict[str, Any] = {}
        self.detail_cover_photo: Any | None = None
        self.placeholder_photo: Any | None = None

    @staticmethod
    def _lru_set(cache: OrderedDict[str, Any], key: str, value: Any, max_size: int) -> None:
        if key in cache:
            cache.move_to_end(key)
        cache[key] = value
        while len(cache) > max_size:
            cache.popitem(last=False)

    def refresh_placeholder(self, palette: ThemePalette) -> None:
        self.placeholder_photo = create_placeholder_cover_thumbnail(
            self._widget,
            size=self._thumb_size,
            bg_hex=palette.surface_alt,
            border_hex=palette.border,
            glyph_hex=palette.fg_muted,
        )

    def photo_for_book(self, book: CatalogBook) -> Any | None:
        cover_url = (getattr(book, "cover_url", None) or "").strip()
        if cover_url:
            photo = self.url_photos.get(cover_url)
            if photo is not None:
                self.url_photos.move_to_end(cover_url)
                return photo
            if cover_url in self.cover_png_cache:
                cached_png = self.cover_png_cache[cover_url]
                self.cover_png_cache.move_to_end(cover_url)
                if cached_png:
                    photo = photo_from_image_bytes(cached_png, self._widget)
                    if photo is not None:
                        self._lru_set(self.url_photos, cover_url, photo, self.MAX_COVER_CACHE)
                        return photo
        return self.placeholder_photo

    def _get_http_session(self, cfg: Config | None) -> requests.Session:
        desired_proxy = (getattr(cfg, "proxy", None) or "").strip() or None
        session: requests.Session | None = getattr(self._thread_local, "session", None)
        if session is None:
            session = create_configured_http_session(
                cfg,
                proxy=desired_proxy,
                pool_connections=8,
                pool_maxsize=8,
            )
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        else:
            if desired_proxy:
                session.proxies["http"] = desired_proxy
                session.proxies["https"] = desired_proxy
            else:
                session.proxies.pop("http", None)
                session.proxies.pop("https", None)
        return session

    def default_fetch_cover_bytes(self, url: str) -> bytes | None:
        if not url.lower().startswith(("http://", "https://")):
            return None
        cfg = self._config_provider() if self._config_provider is not None else None
        session = self._get_http_session(cfg)
        timeout = resolve_http_timeout(cfg, default=10)
        try:
            with session.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "audiobook-dl"},
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                raw_len = resp.headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        if int(str(raw_len).strip()) > self.MAX_COVER_BYTES:
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.MAX_COVER_BYTES:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data if data else None
        except Exception:
            return None

    def start_thumbnail_loading(
        self,
        books: Sequence[CatalogBook],
        token: int,
        on_ready: Callable[[int, str, bytes | None], None],
    ) -> None:
        urls_to_fetch: list[str] = []
        seen: set[str] = set()
        for book in books:
            cover_url = (getattr(book, "cover_url", None) or "").strip()
            if not cover_url or cover_url in seen or cover_url in self.cover_png_cache:
                continue
            seen.add(cover_url)
            urls_to_fetch.append(cover_url)
        if not urls_to_fetch:
            return

        fetcher = self._cover_fetcher or self.default_fetch_cover_bytes
        thumb_size = self._thumb_size
        self._cb_queue.begin_task()

        def _fetch_one(url: str) -> None:
            if token != self.cover_fetch_token:
                return
            raw = fetcher(url)
            png_bytes = resize_cover_thumbnail_bytes(raw, size=thumb_size) if raw else None
            if token == self.cover_fetch_token:
                def _post_cover() -> None:
                    on_ready(token, url, png_bytes)

                self._cb_queue.post(_post_cover)

        def _dispatcher() -> None:
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(_fetch_one, urls_to_fetch))
            finally:
                self._cb_queue.finish_worker()

        threading.Thread(target=_dispatcher, daemon=True).start()

    def store_thumbnail_bytes(self, url: str, png_bytes: bytes | None) -> Any | None:
        if not png_bytes:
            self._lru_set(self.cover_png_cache, url, None, self.MAX_COVER_CACHE)
            return None
        if len(self.url_photos) >= self.MAX_COVER_CACHE and url not in self.url_photos:
            evicted_url, _ = self.url_photos.popitem(last=False)
            self.cover_png_cache.pop(evicted_url, None)
        self._lru_set(self.cover_png_cache, url, png_bytes, self.MAX_COVER_CACHE)
        photo = photo_from_image_bytes(png_bytes, self._widget)
        if photo is None:
            return None
        self._lru_set(self.url_photos, url, photo, self.MAX_COVER_CACHE)
        return photo

    def fetch_detail_cover(
        self,
        token: int,
        cover_url: str,
        on_ready: Callable[[int, str, bytes | None], None],
    ) -> None:
        fetcher = self._cover_fetcher or self.default_fetch_cover_bytes
        detail_size = self._detail_thumb_size
        self._cb_queue.begin_task()

        def _worker() -> None:
            try:
                if token != self.detail_token:
                    return
                raw = fetcher(cover_url)
                if token != self.detail_token:
                    return
                png_bytes = (
                    resize_cover_bytes_aspect_thumbnail(raw, max_size=detail_size) if raw else None
                )
                if token == self.detail_token:
                    def _post_detail() -> None:
                        on_ready(token, cover_url, png_bytes)

                    self._cb_queue.post(_post_detail)
            finally:
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def resolve_detail_photo(
        self,
        cover_url: str,
        png_bytes: bytes | None,
        *,
        cache_result: bool = False,
    ) -> Any | None:
        if cache_result:
            self._lru_set(
                self.detail_cover_png_cache,
                cover_url,
                png_bytes,
                self.MAX_DETAIL_COVER_CACHE,
            )
        photo = photo_from_image_bytes(png_bytes, self._widget) if png_bytes else None
        if photo is not None:
            self.detail_cover_photo = photo
            return photo
        if cover_url in self.url_photos:
            small = self.url_photos[cover_url]
            self.detail_cover_photo = small
            return small
        self.detail_cover_photo = None
        return None

    def close(self) -> None:
        self.cover_fetch_token += 1
        self.detail_token += 1
        self.row_photos.clear()
        self.url_photos.clear()
        self.detail_cover_photo = None
        self.placeholder_photo = None
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

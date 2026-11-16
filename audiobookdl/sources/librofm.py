"""Libro.fm DRM-free audiobook & library sync adapter."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote, urlparse

from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import BookNotFound, RequestError, UserNotAuthorized


def _join_people(raw: Any) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        names = []
        for item in raw:
            if isinstance(item, dict):
                val = str(item.get("name") or item.get("full_name") or "").strip()
            else:
                val = str(item or "").strip()
            if val:
                names.append(val)
        return ", ".join(names)
    return str(raw).strip()


class LibrofmSource(Source):
    """Libro.fm API adapter for purchased DRM-free M4B/MP3 audiobooks and library sync."""

    _authentication_methods = ["login", "cookies"]
    names = ["Libro.fm", "Librofm"]
    login_data = ["username", "password"]
    supports_search = True
    supports_library = True

    match = [
        r"https?://(?:www\.)?libro\.fm/audiobooks/([^/?#]+)",
    ]

    def __init__(self, options) -> None:
        super().__init__(options)
        self._access_token: str | None = None
        self._library_cache: list[dict[str, Any]] | None = None

    def _login(self, url: str, username: str, password: str) -> None:
        resp = self.request(
            "POST",
            "https://libro.fm/oauth/token",
            data={
                "grant_type": "password",
                "username": username,
                "password": password,
            },
        )
        if resp.status_code in (401, 403):
            raise UserNotAuthorized
        if resp.status_code != 200:
            raise RequestError
        payload = resp.json() if hasattr(resp, "json") else {}
        token = str((payload or {}).get("access_token") or "").strip()
        if not token:
            raise UserNotAuthorized
        self._access_token = token
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _fetch_raw_library(self) -> list[dict[str, Any]]:
        if self._library_cache is not None:
            return self._library_cache
        books: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            payload = self.get_json(f"https://libro.fm/api/v7/library?page={page}")
            items = payload.get("audiobooks") or payload.get("library") or payload.get("data") or []
            if not isinstance(items, list) or not items:
                break
            books.extend(item for item in items if isinstance(item, dict))
            total_pages = payload.get("total_pages")
            if isinstance(total_pages, int) and page >= total_pages:
                break
            if not payload.get("next_page"):
                break
            page += 1
        self._library_cache = books
        return books

    def _book_to_catalog_entry(self, item: dict[str, Any]) -> CatalogBook | None:
        isbn = str(item.get("isbn") or item.get("id") or "").strip()
        title = str(item.get("title") or isbn).strip()
        if not isbn or not title:
            return None
        book_url = str(item.get("url") or f"https://libro.fm/audiobooks/{isbn}").strip()
        if book_url.startswith("/"):
            book_url = f"https://libro.fm{book_url}"
        author = _join_people(item.get("authors") or item.get("author"))
        series_raw = item.get("series")
        series_name: str | None = None
        series_order: int | None = None
        if isinstance(series_raw, dict):
            series_name = str(series_raw.get("name") or "").strip() or None
            raw_ord = series_raw.get("book_number") or series_raw.get("order")
            if raw_ord is not None and str(raw_ord).isdigit():
                series_order = int(str(raw_ord))
        elif isinstance(series_raw, str) and series_raw.strip():
            series_name = series_raw.strip()
        return CatalogBook(
            title=title,
            url=book_url,
            author=author,
            series=series_name,
            series_order=series_order,
            source="libro.fm",
        )

    def list_library(self) -> list[CatalogBook]:
        results: list[CatalogBook] = []
        for item in self._fetch_raw_library():
            entry = self._book_to_catalog_entry(item)
            if entry is not None:
                results.append(entry)
        return results

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip().lower()
        if not cleaned:
            return []
        try:
            library_entries = self.list_library()
        except Exception:
            library_entries = []
        return [
            entry
            for entry in library_entries
            if cleaned in entry.title.lower() or cleaned in entry.author.lower()
        ]

    def _resolve_book_entry(self, url: str) -> tuple[str, dict[str, Any] | None]:
        m = re.search(r"/audiobooks/([^/?#]+)", url)
        if not m:
            raise BookNotFound
        slug = m.group(1).strip()
        isbn_match = re.match(r"^(97[89]\d{10}|\d{9}[\dXx])(?:-|$)", slug)
        candidate_isbn = isbn_match.group(1) if isbn_match else None

        matched_entry: dict[str, Any] | None = None
        if self._access_token or self.authenticated:
            try:
                for item in self._fetch_raw_library():
                    item_isbn = str(item.get("isbn") or "").strip()
                    item_url = str(item.get("url") or "").strip()
                    if candidate_isbn and item_isbn == candidate_isbn:
                        matched_entry = item
                        break
                    if slug and (slug == item_isbn or item_url.rstrip("/").endswith(f"/{slug}")):
                        matched_entry = item
                        candidate_isbn = item_isbn or candidate_isbn
                        break
            except Exception:
                matched_entry = None

        if not candidate_isbn:
            raise BookNotFound
        return candidate_isbn, matched_entry

    def download(self, url: str) -> Audiobook:
        isbn, lib_entry = self._resolve_book_entry(url)
        manifest = self.get_json(f"https://libro.fm/api/v9/download-manifest?isbn={quote(isbn)}")
        if not isinstance(manifest, dict):
            raise BookNotFound

        info = lib_entry or manifest.get("audiobook") or manifest
        title = str(info.get("title") or manifest.get("title") or isbn).strip()
        meta = AudiobookMetadata(title=title)
        meta.source = "libro.fm"
        meta.scrape_url = url
        meta.isbn = isbn

        for author in (info.get("authors") or []):
            name = _join_people(author)
            if name:
                meta.add_author(name)
        if not meta.authors and info.get("author"):
            for name in _join_people(info.get("author")).split(","):
                if name.strip():
                    meta.add_author(name.strip())

        for narrator in (info.get("narrators") or []):
            name = _join_people(narrator)
            if name:
                meta.add_narrator(name)

        if info.get("description"):
            meta.description = str(info["description"]).strip()
        if info.get("publisher"):
            pub = info["publisher"]
            meta.publisher = str(pub.get("name") if isinstance(pub, dict) else pub).strip()

        files: list[AudiobookFile] = []
        chapters: list[Chapter] = []

        m4b_url = manifest.get("m4b_url") or manifest.get("audiobook_m4b")
        if isinstance(m4b_url, str) and m4b_url.strip():
            files.append(AudiobookFile(url=m4b_url.strip(), ext="m4b", title=title))
        else:
            raw_parts = manifest.get("parts") or manifest.get("tracks") or []
            running_ms = 0
            for idx, part in enumerate(raw_parts, start=1):
                if not isinstance(part, dict):
                    continue
                part_url = str(part.get("url") or "").strip()
                if not part_url:
                    continue
                ext = os.path.splitext(urlparse(part_url).path)[1].lstrip(".").lower() or "mp3"
                part_title = str(part.get("title") or part.get("chapter_title") or f"Track {idx}").strip()
                files.append(AudiobookFile(url=part_url, ext=ext, title=part_title))
                dur_sec = part.get("duration") or part.get("duration_seconds")
                if isinstance(dur_sec, (int, float)) and dur_sec > 0:
                    chapters.append(Chapter(start=running_ms, title=part_title))
                    running_ms += round(float(dur_sec) * 1000)

        if not files:
            raise BookNotFound

        cover: Cover | None = None
        cover_url = info.get("cover_url") or manifest.get("cover_url")
        if isinstance(cover_url, str) and cover_url.strip():
            try:
                cover_data = self.get(cover_url.strip())
                cover_ext = os.path.splitext(urlparse(cover_url).path)[1].lstrip(".").lower() or "jpg"
                cover = Cover(image=cover_data, extension=cover_ext)
            except Exception:
                cover = None

        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

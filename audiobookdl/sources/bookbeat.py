from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover, Result, Series
from audiobookdl.exceptions import BookHasNoAudiobook, BookHasNoEbook, MissingBookAccess
from audiobookdl.utils.fs import atomic_write_text

import base64
from datetime import datetime
import os
import re
from typing import Any
from collections.abc import MutableMapping
from urllib.parse import quote_plus
import uuid


class BookBeatSource(Source):
    match = [
        r"https?://(?:www\.)?bookbeat\..+",
    ]
    names = ["BookBeat"]
    _authentication_methods = ["login"]
    supports_library = True
    supports_search = True
    saved_books: dict
    book_info: dict

    def __init__(self, options):
        super().__init__(options)
        self.saved_books = {"_embedded": {"savedBooks": []}}

    @staticmethod
    def create_device_id() -> str:
        """Create a random per-installation BookBeat device identifier."""
        return (
            str(uuid.uuid4())
            + " "
            + base64.b64encode(b"Personal Computer").decode()
        )

    def _get_or_create_device_id(self) -> str:
        """Load the persisted device id for this database dir, creating one if absent."""
        device_file = os.path.join(self.database_directory, "bookbeat_device_id.txt")
        if os.path.isfile(device_file):
            try:
                with open(device_file, encoding="utf-8") as fh:
                    existing = fh.read().strip()
                if existing:
                    return existing
            except OSError:
                pass
        generated = self.create_device_id()
        try:
            atomic_write_text(device_file, generated + "\n", encoding="utf-8", private=True)
        except OSError:
            pass
        return generated

    def _login(self, url: str, username: str, password: str):
        headers: MutableMapping[str, str | bytes] = {
            "accept": "application/hal+json",
            "bb-client": "BookBeatApp",
            "bb-device": self._get_or_create_device_id(),
        }
        self._session.headers.update(headers)
        login_json = {"username": username, "password": password}
        tokens = self.post_json(
            "https://api.bookbeat.com/api/login",
            json=login_json,
        )
        token = tokens["token"]
        self._session.headers.update({"authorization": f"Bearer {token}"})
        self.saved_books = self._fetch_saved_books()

    def _fetch_saved_books(self) -> dict[str, Any]:
        """Fetch all saved books from BookBeat across paginated responses."""
        limit = 100
        offset = 0
        all_saved: list[dict[str, Any]] = []
        next_url: str | None = f"https://api.bookbeat.com/api/my/books/saved?offset={offset}&limit={limit}"
        seen_urls = set()
        while next_url and next_url not in seen_urls:
            seen_urls.add(next_url)
            page = self.get_json(next_url)
            page_books = page.get("_embedded", {}).get("savedBooks", [])
            all_saved.extend(page_books)
            next_link = page.get("_links", {}).get("next", {}).get("href")
            if next_link:
                next_url = next_link
            elif len(page_books) >= limit:
                offset += len(page_books)
                next_url = f"https://api.bookbeat.com/api/my/books/saved?offset={offset}&limit={limit}"
            else:
                next_url = None
        return {"_embedded": {"savedBooks": all_saved}}

    def _build_ebook(self, book_info: dict[str, Any], dl_entry: dict[str, Any], url: str) -> Audiobook:
        license_url = (dl_entry.get("_links") or {}).get("license", {}).get("href")
        if not license_url:
            raise BookHasNoEbook
        lic = self.get_json(license_url)
        href = ((lic.get("_links") or {}).get("download") or {}).get("href")
        if not href:
            raise MissingBookAccess
        ext = "pdf" if ".pdf" in href.lower() else "epub"
        return Audiobook(
            session=self._session,
            files=[AudiobookFile(url=href, headers=dict(self._session.headers), ext=ext)],
            metadata=self.get_metadata(book_info, url=url, is_ebook=True),
            cover=self.get_cover(book_info),
            chapters=[],
        )

    def download(self, url: str) -> Result:
        book_id_re = r"(\d+)(?:[/?#].*)?$"
        wanted_id_match = re.search(book_id_re, url)
        if not wanted_id_match:
            raise ValueError(f"Couldn't get bookid from url {url}")
        wanted_id = wanted_id_match.group(1)
        book_info = self.find_book_info(wanted_id)
        mode = getattr(self._options, "media_type", None) or "auto"

        if mode == "audiobook":
            return Audiobook(
                session=self._session,
                files=self.get_files(book_info),
                metadata=self.get_metadata(book_info, url=url),
                cover=self.get_cover(book_info),
                chapters=self.get_chapters(book_info),
            )

        downloads = self._get_download_entries(book_info)
        audio_entry = next((d for d in downloads if d.get("format") == "audioBook"), None)
        ebook_entry = next((d for d in downloads if d.get("format") == "eBook"), None)

        if mode == "ebook":
            if ebook_entry is None:
                raise BookHasNoEbook
            return self._build_ebook(book_info, ebook_entry, url)

        if mode == "both":
            items: list[Audiobook] = []
            if audio_entry is not None:
                items.append(
                    Audiobook(
                        session=self._session,
                        files=self.get_files(book_info),
                        metadata=self.get_metadata(book_info, url=url),
                        cover=self.get_cover(book_info),
                        chapters=self.get_chapters(book_info),
                    )
                )
            if ebook_entry is not None:
                items.append(self._build_ebook(book_info, ebook_entry, url))
            if not items:
                raise MissingBookAccess
            if len(items) == 1:
                return items[0]
            return Series(title=items[0].metadata.title, books=items)

        # Default "auto": prefer audiobook when present, fall back to ebook
        if audio_entry is not None:
            return Audiobook(
                session=self._session,
                files=self.get_files(book_info),
                metadata=self.get_metadata(book_info, url=url),
                cover=self.get_cover(book_info),
                chapters=self.get_chapters(book_info),
            )
        if ebook_entry is not None:
            return self._build_ebook(book_info, ebook_entry, url)
        raise BookHasNoAudiobook

    def _get_download_entries(self, book_info: dict[str, Any]) -> list[dict[str, Any]]:
        cached = book_info.get("_downloads")
        if isinstance(cached, list):
            return cached
        dl_info = self.get_json(
            "https://api.bookbeat.com/api/downloadinfo/" + str(book_info["bookid"])
        )
        entries = list((dl_info.get("_embedded") or {}).get("downloads") or [])
        book_info["_downloads"] = entries
        return entries

    def download_license_url(self, book_info) -> str:
        for dl in self._get_download_entries(book_info):
            if dl.get("format") == "audioBook":
                return dl["_links"]["license"]["href"]
        raise MissingBookAccess

    def _get_license(self, book_info: dict[str, Any]) -> dict[str, Any]:
        cached = book_info.get("license")
        if isinstance(cached, dict):
            return cached
        license_url = self.download_license_url(book_info)
        lic = self.get_json(license_url)
        book_info["license"] = lic
        return lic

    def get_files(self, book_info: dict) -> list[AudiobookFile]:
        lic = self._get_license(book_info)
        if "_links" in lic:
            return [
                AudiobookFile(
                    url=lic["_links"]["download"]["href"],
                    headers=dict(self._session.headers),
                    ext="mp4",
                )
            ]
        raise MissingBookAccess

    def get_metadata(
        self,
        book_info: dict,
        *,
        url: str | None = None,
        is_ebook: bool = False,
    ) -> AudiobookMetadata:
        meta = book_info["metadata"]
        title = meta["title"]
        scrape_url = (
            ((meta.get("_links") or {}).get("web") or {}).get("href")
            or url
            or (f"https://www.bookbeat.com/book/{book_info['bookid']}" if book_info.get("bookid") is not None else None)
        )
        metadata = AudiobookMetadata(
            title=title,
            description=meta.get("summary") or meta.get("description"),
            language=meta.get("language"),
            source="bookbeat",
            scrape_url=scrape_url,
        )
        series_raw = meta.get("series")
        if isinstance(series_raw, dict):
            metadata.series = str(series_raw.get("name") or series_raw.get("title") or "").strip() or None
            part = series_raw.get("partnumber") or series_raw.get("partNumber") or series_raw.get("number")
            if part is not None:
                try:
                    metadata.series_order = int(float(part))
                except (TypeError, ValueError):
                    pass
        elif isinstance(series_raw, str) and series_raw.strip():
            metadata.series = series_raw.strip()

        target_fmt = "eBook" if is_ebook else "audioBook"
        editions = [
            e for e in (meta.get("editions") or []) if isinstance(e, dict) and e.get("format") == target_fmt
        ] or [e for e in (meta.get("editions") or []) if isinstance(e, dict)]
        if editions:
            ed = editions[0]
            pub = ed.get("publisher")
            if isinstance(pub, dict):
                pub = pub.get("name")
            if isinstance(pub, str) and pub.strip():
                metadata.publisher = pub.strip()
            isbn = ed.get("isbn") or ed.get("isbn13")
            if isbn:
                metadata.isbn = str(isbn).strip()
            pub_date = ed.get("published") or ed.get("releaseDate") or meta.get("published")
            if isinstance(pub_date, str) and pub_date.strip():
                try:
                    metadata.release_date = datetime.fromisoformat(pub_date.strip().replace("Z", "+00:00")).date()
                except ValueError:
                    pass
            contributors = ed.get("contributors") or []
            for contributor in contributors:
                name = f"{contributor.get('firstname', '')} {contributor.get('lastname', '')}".strip()
                if not name:
                    continue
                roles = contributor.get("role") or []
                if "author" in roles:
                    metadata.add_author(name)
                if "narrator" in roles:
                    metadata.add_narrator(name)
        return metadata

    def get_chapters(self, book_info: dict) -> list[Chapter]:
        lic = self._get_license(book_info)
        chapters = []
        for chapter_number, track in enumerate(lic.get("tracks") or []):
            ch_title = str(
                track.get("title") or track.get("name") or f"Chapter {chapter_number + 1}"
            ).strip()
            chapters.append(Chapter(track["start"], ch_title))
        return chapters

    def get_cover(self, book_info: dict) -> Cover:
        cover_url = book_info["metadata"]["cover"]
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def find_book_info(self, book_id: str) -> dict:
        """Find book metadata by id, checking `saved_books` first and falling back to catalog API."""
        saved_list = self.saved_books.get("_embedded", {}).get("savedBooks", [])
        for book in saved_list:
            if str(book.get("bookid")) == str(book_id):
                book_copy = dict(book)
                book_copy["metadata"] = self.get_json(
                    book["_links"]["book"]["href"]
                )
                return book_copy
        # Allow downloading arbitrary catalog URLs without requiring saved_books membership.
        catalog_meta = self.get_json(f"https://api.bookbeat.com/api/books/{book_id}")
        return {
            "bookid": int(book_id) if book_id.isdigit() else book_id,
            "metadata": catalog_meta,
        }

    @staticmethod
    def _entry_to_catalog_book(entry: dict[str, Any]) -> CatalogBook | None:
        embedded_book = entry.get("_embedded", {}).get("book") if isinstance(entry.get("_embedded"), dict) else None
        source_obj = embedded_book if isinstance(embedded_book, dict) else entry
        book_id = entry.get("bookid") or source_obj.get("bookid") or source_obj.get("id")
        if not book_id:
            return None
        title = str(source_obj.get("title") or f"Book {book_id}").strip()
        author_raw = source_obj.get("author")
        if not author_raw and isinstance(source_obj.get("authors"), list):
            names = [
                f"{a.get('firstname', '')} {a.get('lastname', '')}".strip()
                if isinstance(a, dict)
                else str(a).strip()
                for a in source_obj["authors"]
            ]
            author_raw = ", ".join(n for n in names if n) or None
        url = (
            source_obj.get("_links", {}).get("web", {}).get("href")
            if isinstance(source_obj.get("_links"), dict)
            else None
        ) or f"https://www.bookbeat.com/book/{book_id}"
        return CatalogBook(
            title=title,
            author=str(author_raw).strip() if author_raw else "",
            url=str(url),
            source="bookbeat",
        )

    def list_library(self) -> list[CatalogBook]:
        if not self.saved_books.get("_embedded", {}).get("savedBooks"):
            self.saved_books = self._fetch_saved_books()
        books: list[CatalogBook] = []
        for entry in self.saved_books.get("_embedded", {}).get("savedBooks", []):
            item = self._entry_to_catalog_book(entry)
            if item is not None:
                books.append(item)
        return books

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        payload = self.get_json(f"https://api.bookbeat.com/api/search?query={quote_plus(cleaned)}")
        raw_items = (
            payload.get("_embedded", {}).get("books")
            or payload.get("books")
            or payload.get("items")
            or []
        )
        books: list[CatalogBook] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            item = self._entry_to_catalog_book(entry)
            if item is not None:
                books.append(item)
        return books

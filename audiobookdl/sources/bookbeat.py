from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import MissingBookAccess
from audiobookdl.utils.fs import atomic_write_text

import base64
import os
import re
from typing import Any, Dict, List, MutableMapping, Optional, Union
from urllib.parse import quote_plus
import uuid


class BookBeatSource(Source):
    match = [
        r"https?://(www.)?bookbeat.+",
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
                existing = open(device_file, "r", encoding="utf-8").read().strip()
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
        headers: MutableMapping[str, Union[str, bytes]] = {
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

    def _fetch_saved_books(self) -> Dict[str, Any]:
        """Fetch all saved books from BookBeat across paginated responses."""
        limit = 100
        offset = 0
        all_saved: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"https://api.bookbeat.com/api/my/books/saved?offset={offset}&limit={limit}"
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

    def download(self, url: str) -> Audiobook:
        book_id_re = r"(\d+)(?:[/?#].*)?$"
        wanted_id_match = re.search(book_id_re, url)
        if not wanted_id_match:
            raise ValueError(f"Couldn't get bookid from url {url}")
        wanted_id = wanted_id_match.group(1)
        book_info = self.find_book_info(wanted_id)
        return Audiobook(
            session=self._session,
            files=self.get_files(book_info),
            metadata=self.get_metadata(book_info),
            cover=self.get_cover(book_info),
            chapters=self.get_chapters(book_info),
        )

    def download_license_url(self, book_info) -> str:
        dl_info = self.get_json(
            "https://api.bookbeat.com/api/downloadinfo/" + str(book_info["bookid"])
        )
        if "_embedded" in dl_info:
            if "downloads" in dl_info["_embedded"]:
                for dl in dl_info["_embedded"]["downloads"]:
                    if dl["format"] == "audioBook":
                        return dl["_links"]["license"]["href"]
        raise MissingBookAccess

    def get_files(self, book_info: Dict) -> List[AudiobookFile]:
        license_url = self.download_license_url(book_info)
        lic = self.get_json(license_url)
        book_info["license"] = lic
        if "_links" in lic:
            return [
                AudiobookFile(
                    url=lic["_links"]["download"]["href"],
                    headers=dict(self._session.headers),
                    ext="mp4",
                )
            ]
        raise MissingBookAccess

    def get_metadata(self, book_info: Dict) -> AudiobookMetadata:
        meta = book_info["metadata"]
        title = meta["title"]
        metadata = AudiobookMetadata(
            title=title,
            description=meta.get("summary") or meta.get("description"),
            language=meta.get("language"),
            source="bookbeat",
        )
        try:
            contributors = [
                e["contributors"] for e in meta["editions"] if e["format"] == "audioBook"
            ][0]
            if not contributors:
                return metadata
            for contributor in contributors:
                name = f"{contributor['firstname']} {contributor['lastname']}".strip()
                if "author" in contributor["role"]:
                    metadata.add_author(name)
                if "narrator" in contributor["role"]:
                    metadata.add_narrator(name)
            return metadata
        except (KeyError, IndexError, TypeError):
            return metadata

    @staticmethod
    def get_chapters(book_info: Dict) -> List[Chapter]:
        chapters = []
        for chapter_number, track in enumerate(book_info["license"]["tracks"]):
            chapters.append(Chapter(track["start"], f"Chapter {chapter_number + 1}"))
        return chapters

    def get_cover(self, book_info: Dict) -> Cover:
        cover_url = book_info["metadata"]["cover"]
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def find_book_info(self, book_id: str) -> Dict:
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
    def _entry_to_catalog_book(entry: Dict[str, Any]) -> Optional[CatalogBook]:
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

    def list_library(self) -> List[CatalogBook]:
        if not self.saved_books.get("_embedded", {}).get("savedBooks"):
            self.saved_books = self._fetch_saved_books()
        books: List[CatalogBook] = []
        for entry in self.saved_books.get("_embedded", {}).get("savedBooks", []):
            item = self._entry_to_catalog_book(entry)
            if item is not None:
                books.append(item)
        return books

    def search(self, query: str) -> List[CatalogBook]:
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
        books: List[CatalogBook] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            item = self._entry_to_catalog_book(entry)
            if item is not None:
                books.append(item)
        return books

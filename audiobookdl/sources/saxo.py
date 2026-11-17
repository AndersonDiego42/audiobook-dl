from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover, logging
from audiobookdl.exceptions import BookNotFound, NoSourceFound
from audiobookdl.utils.audiobook import (
    AESEncryption,
    clean_html_description,
    parse_language,
    parse_release_date,
)

import os
import re
from typing import Any
from urllib.parse import quote


class SaxoSource(Source):
    _authentication_methods = ["login"]
    names = ["Saxo"]
    supports_search = True
    supports_library = True
    match = [
        r"https?://(?:www\.)?saxo\.(?:com|dk)/[^/]+/.+",
    ]
    DEFAULT_APP_OS = "android"
    DEFAULT_APP_VERSION = "6.2.4"

    def _app_headers(self) -> dict[str, str]:
        app_os = os.environ.get("AUDIOBOOK_DL_SAXO_APP_OS") or self.DEFAULT_APP_OS
        app_version = os.environ.get("AUDIOBOOK_DL_SAXO_APP_VERSION") or self.DEFAULT_APP_VERSION
        return {
            "Appauthorization": f"bearer {self.bearer_token}",
            "App-Os": app_os,
            "App-Version": app_version,
        }

    def _login(self, url: str, username: str, password: str) -> None:
        resp = self.post_json(
            "https://auth-read.saxo.com/auth/token",
            data={
                "username": username,
                "password": password,
                "grant_type": "password",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self.bearer_token = resp["access_token"]
        self.user_id = resp["id"]
        logging.debug(f"bearer_token={logging.mask_secret(self.bearer_token)}")
        logging.debug(f"{self.user_id=}")

    def download(self, url: str) -> Audiobook:
        isbn = self._extract_isbn(url)
        book_id = self._search_for_book(isbn)
        logging.debug(f"{book_id=}")
        book_info = self._get_book_metadata(book_id)
        return Audiobook(
            session=self._session,
            files=self.get_files(book_info),
            metadata=self.get_metadata(book_info, isbn=isbn, url=url),
            cover=self.get_cover(book_info),
            chapters=self.get_chapters(book_info),
        )

    def _extract_isbn(self, url: str) -> str:
        """Extract isbn of book from url"""
        isbn_match = re.search(r"(\d+)(?:[/?#].*)?$", url)
        if isbn_match and isbn_match.group(1):
            return isbn_match.group(1)
        raise NoSourceFound

    def _search_for_book(self, isbn: str) -> str:
        """Search for internal book id by isbn number"""
        logging.debug(f"Searching for book with isbn: {isbn}")
        resp = self.get_json(
            f"https://api-read.saxo.com/api/v2/search/user/{self.user_id}/premium/books/{isbn}?booktypefilter=Audiobook",
            headers=self._app_headers(),
        )
        items = resp.get("items") or []
        if not items:
            raise BookNotFound
        return items[0]["bookId"]

    def _get_book_metadata(self, book_id: str) -> dict:
        """Downloads metadata about book"""
        resp = self.post_json(
            f"https://api-read.saxo.com/api/v1/book/data/user/{self.user_id}/",
            headers=self._app_headers(),
            json=[book_id],
        )
        items = resp.get("items") or []
        if not items:
            raise BookNotFound
        return items[0]

    def get_files(self, book_info) -> list[AudiobookFile]:
        book_id = book_info["bookId"]
        chapters = book_info["techInfo"]["chapters"]

        def _make_resolver(filename: str):
            def _resolve() -> str:
                return self.get_json(
                    f"https://api-read.saxo.com/api/v1/book/{book_id}/content/encryptedstream/{filename}",
                    headers=self._app_headers(),
                )["link"]

            return _resolve

        return [
            AudiobookFile(
                url="",
                ext="mp3",
                encryption_method=AESEncryption(
                    b"CD3E9D141D8EFC0886912E7A8F3652C4",
                    b"78CB354D377772F1",
                ),
                url_resolver=_make_resolver(chapter["fileName"]),
            )
            for chapter in chapters
        ]

    @staticmethod
    def get_chapters(book_info: dict[str, Any]) -> list[Chapter]:
        """Build chapters from `techInfo.chapters` when titles or timings are supplied."""
        tech_chapters = (book_info.get("techInfo") or {}).get("chapters") or []
        chapters: list[Chapter] = []
        offset_ms = 0
        for idx, ch in enumerate(tech_chapters, start=1):
            if not isinstance(ch, dict):
                continue
            ch_title = str(ch.get("title") or ch.get("chapterTitle") or f"Chapter {idx}").strip()
            start_val = ch.get("startTime")
            start_ms = round(float(start_val) * 1000) if isinstance(start_val, (int, float)) else offset_ms
            if ch.get("title") or ch.get("chapterTitle") or ch.get("duration") or ch.get("startTime") is not None:
                chapters.append(Chapter(start=start_ms, title=ch_title))
            dur = ch.get("duration") or ch.get("durationSeconds")
            if isinstance(dur, (int, float)) and dur > 0:
                offset_ms += round(float(dur) * 1000)
        return chapters

    def get_metadata(
        self,
        book_info: dict[str, Any],
        *,
        isbn: str | None = None,
        url: str | None = None,
    ) -> AudiobookMetadata:
        metadata: dict = book_info["bookMetadata"]
        title = metadata["title"]
        result = AudiobookMetadata(title, source="saxo", scrape_url=url)
        result.add_authors(metadata.get("authors") or [])
        result.add_narrators(metadata.get("readBy") or [])
        result.series = metadata.get("seriesName")

        resolved_isbn = isbn or metadata.get("isbn") or metadata.get("isbn13")
        if resolved_isbn:
            result.isbn = str(resolved_isbn).strip()

        raw_desc = metadata.get("description") or metadata.get("summary")
        cleaned_desc = clean_html_description(raw_desc)
        if cleaned_desc:
            result.description = cleaned_desc

        publisher = metadata.get("publisherName") or metadata.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        if isinstance(publisher, str) and publisher.strip():
            result.publisher = publisher.strip()

        result.language = parse_language(metadata.get("language"))
        result.release_date = parse_release_date(
            metadata.get("publishingDate") or metadata.get("releaseDate")
        )
        return result

    def get_cover(self, book_info) -> Cover:
        cover_url = book_info["bookMetadata"]["image"]["highQualityImageUrl"]
        data = self.get(cover_url)
        return Cover(data, "jpg")

    @staticmethod
    def _items_to_catalog(items: Any) -> list[CatalogBook]:
        results: list[CatalogBook] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            raw_meta = item.get("bookMetadata")
            meta = raw_meta if isinstance(raw_meta, dict) else item
            book_id = item.get("bookId") or meta.get("bookId") or meta.get("id")
            isbn = meta.get("isbn13") or meta.get("isbn") or item.get("isbn") or book_id
            title = str(meta.get("title") or item.get("title") or isbn or "").strip()
            if not isbn or not title:
                continue
            authors = meta.get("authors") or item.get("authors") or []
            if isinstance(authors, list):
                author = ", ".join(
                    str(a.get("name") if isinstance(a, dict) else a).strip()
                    for a in authors
                    if a
                )
            else:
                author = str(authors or "").strip()
            url = (
                meta.get("url")
                or item.get("url")
                or f"https://www.saxo.com/dk/audiobook_{isbn}"
            )
            results.append(
                CatalogBook(
                    title=title,
                    author=author,
                    url=str(url),
                    source="saxo",
                )
            )
        return results

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        resp = self.get_json(
            f"https://api-read.saxo.com/api/v2/search/user/{self.user_id}/premium/books/{quote(cleaned)}?booktypefilter=Audiobook",
            headers=self._app_headers(),
        )
        return self._items_to_catalog(resp.get("items") or [])

    def list_library(self) -> list[CatalogBook]:
        resp = self.get_json(
            f"https://api-read.saxo.com/api/v2/user/{self.user_id}/books?booktypefilter=Audiobook",
            headers=self._app_headers(),
        )
        return self._items_to_catalog(resp.get("items") or [])

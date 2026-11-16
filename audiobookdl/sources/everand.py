from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, BookId, Chapter, Cover, Result, Series, logging
from audiobookdl.exceptions import BookNotFound, RequestError
from audiobookdl.utils.audiobook import (
    clean_html_description,
    parse_language,
    parse_release_date,
)

import json
import lxml.html
import re
from typing import Any
from urllib.parse import quote_plus, urljoin


class EverandSource(Source[str]):
    match = [
        r"https?://(www\.)?(scribd|everand)\.com/listen/\d+",
        r"https?://(www\.)?(scribd|everand)\.com/audiobook/\d+(/.*)?",
        r"https?://(www\.)?(scribd|everand)\.com/series/\d+",
        r"https?://(www\.)?(scribd|everand)\.com/book/\d+(/.*)?",
    ]
    names = ["Everand", "Scribd"]
    supports_search = True
    supports_library = True

    def download(self, url: str) -> Result:
        # Matches series url
        if re.match(self.match[2], url):
            return self.download_series(url)
        return self.download_book_from_url(url)

    def download_from_id(self, book_id: str) -> Audiobook:
        return self.download_book_from_url(
            f"https://www.everand.com/listen/{book_id}"
        )

    def download_series(self, url: str) -> Series[str]:
        series_match = re.search(r"/series/(\d+)", url)
        if series_match is None:
            raise BookNotFound
        series_id: str = series_match.group(1)
        logging.debug(f"{series_id=}")

        return Series(
            title=self.find_elem_in_page(url, "h1"),
            books=self.download_series_books(series_id),
        )

    def download_series_books(self, series_id: str) -> list:
        """
        Downloads ids of books in series

        :param series_id: Id of series
        :returns: Book ids of books in series
        """
        response = self.get_json(
            f"https://www.everand.com/series/{series_id}/data",
            headers={
                "X-Requested-With": "XMLHttpRequest"
            },
        )
        return [
            BookId(i["id"])
            for i in response["compilation"]["modules"][0]["documents"]
        ]

    def download_book_from_url(self, url: str) -> Audiobook:
        """
        Download audiobook

        :param url: Url to information page or listening page of audiobook
        :return: Audiobook object
        """
        url = self.create_listen_url(url)
        book_info = self.extract_info(url)
        metadata = book_info["doc"]
        logging.debug(f"{metadata=}")
        csrf = self.post_json(
            "https://www.everand.com/csrf_token",
            headers={"href": url},
        )
        logging.debug(f"csrf_received={bool(csrf)}")
        return Audiobook(
            session=self._session,
            files=self.get_files(book_info),
            metadata=self.format_metadata(metadata, url=url),
            cover=self.download_cover(metadata),
            chapters=self.get_chapters(book_info),
        )

    def extract_info(self, url: str) -> dict:
        """
        Extract information from listening page

        :param url: Url of listening page
        :return: Metadata from page as dictionary
        """
        raw = self.find_in_page(
            url,
            r'(?<=(Scribd.Audiobooks.Show, )){.+}(?=\))',
        )
        return json.loads(raw)

    def get_files(self, book_info: dict) -> list[AudiobookFile]:
        """
        Format audio files

        :param book_info: Information extracted from listening page
        """
        if book_info["jwt_token"]["token"] is not None:
            return self.get_internal_files(book_info)
        return self.get_external_files(book_info)

    def get_internal_files(self, book_info: dict) -> list[AudiobookFile]:
        """
        Format audio files for internal books

        :param book_info: Information extracted from listening page
        """
        book_id = book_info["share_opts"]["id"]
        jwt = book_info["jwt_token"]["token"]
        headers = {"Authorization": jwt}
        master_url = f"https://audio.production.scribd.com/audiobooks/{book_id}/master.m3u8"
        try:
            return self.get_stream_files(master_url, headers=headers)
        except RequestError:
            fallback_url = f"https://audio.production.scribd.com/audiobooks/{book_id}/96kbps.m3u8"
            return self.get_stream_files(fallback_url, headers=headers)

    def get_external_files(self, book_info: dict) -> list[AudiobookFile]:
        """
        Format audio files for external books

        :param book_info: Information extracted from listening page
        """
        external_id = book_info["audiobook"]["external_id"]
        logging.debug(f"{external_id=}")
        account_id = book_info["audiobook"]["account_id"]
        logging.debug(f"{account_id=}")
        session_key = book_info["audiobook"]["session_key"]
        logging.debug(f"session_key={logging.mask_secret(session_key)}")
        headers = {"Session-Key": session_key}
        license_id = self.get_json(
            f"https://api.findawayworld.com/v4/accounts/{account_id}/audiobooks/{external_id}",
            headers=headers,
        )["licenses"][0]["id"]
        response = self.post_json(
            f"https://api.findawayworld.com/v4/audiobooks/{external_id}/playlists",
            json={"license_id": license_id},
            headers=headers,
        )
        files = []
        for i in response["playlist"]:
            chapter = i["chapter_number"]
            files.append(
                AudiobookFile(
                    url=i["url"],
                    title=f"Chapter {chapter}",
                    ext="mp3",
                )
            )
        return files

    def create_listen_url(self, url: str) -> str:
        """
        Change url to listen page if `/audiobook/<id>` or `/book/<id>` info page is used.

        :param url: Url of information or listen page
        :return: Listen page url
        """
        match = re.search(r"/(?:audiobook|book)/(\d+)", url)
        if match:
            return f"https://www.everand.com/listen/{match.group(1)}"
        return url

    def download_cover(self, metadata: dict) -> Cover | None:
        """
        Download and clean cover

        :param metadata: Metadata dictionary containing `cover_url`
        :returns: Cover of book
        """
        cover_url = metadata["cover_url"]
        raw_cover = self.get(cover_url)
        if raw_cover is None:
            return None
        return Cover(raw_cover, "jpg")

    @staticmethod
    def clean_title(title: str):
        """
        Move ', The' from the end to the beginning of the title

        :param title: Original title
        :returns: Fixed title
        """
        if title[-3:] == ", A":
            return f"A {title[:-3]}"
        if title[-5:] == ", The":
            return f"The {title[:-5]}"
        return title

    @staticmethod
    def format_metadata(book_info: dict, url: str | None = None) -> AudiobookMetadata:
        series_name: str | None = None
        series_order: int | None = None
        raw_series = book_info.get("series")
        if isinstance(raw_series, dict):
            series_name = str(raw_series.get("title") or raw_series.get("name") or "").strip() or None
            pos = raw_series.get("position") or book_info.get("series_position") or book_info.get("book_number")
            if pos is not None:
                try:
                    series_order = int(float(pos))
                except (TypeError, ValueError):
                    series_order = None
        elif isinstance(raw_series, str) and raw_series.strip():
            series_name = raw_series.strip()
        elif isinstance(book_info.get("series_name"), str) and book_info["series_name"].strip():
            series_name = book_info["series_name"].strip()

        publisher_raw = book_info.get("publisher")
        if isinstance(publisher_raw, dict):
            publisher = str(publisher_raw.get("name") or "").strip() or None
        elif isinstance(publisher_raw, str):
            publisher = publisher_raw.strip() or None
        else:
            publisher = (str(book_info.get("publisher_name") or "").strip() or None)

        description = clean_html_description(
            book_info.get("description")
            or book_info.get("short_description")
            or book_info.get("blurb")
        )

        isbn = str(book_info.get("isbn") or "").strip() or None
        language = parse_language(book_info.get("language") or book_info.get("lang"))
        release_date = parse_release_date(
            book_info.get("publish_date") or book_info.get("release_date")
        )

        return AudiobookMetadata(
            title=EverandSource.clean_title(book_info["title"]),
            authors=[book_info["author"]["name"]],
            narrators=[narrator["name"] for narrator in book_info.get("narrators", [])],
            description=description,
            series=series_name,
            series_order=series_order,
            isbn=isbn,
            publisher=publisher,
            language=language,
            release_date=release_date,
            source="everand",
            scrape_url=url,
        )

    @staticmethod
    def get_chapter_title(chapter) -> str:
        """Extract title for chapter"""
        if chapter.get("title"):
            return str(chapter["title"]).strip()
        number = chapter["chapter_number"]
        if number == 0:
            return "Introduction"
        return f"Chapter {number}"

    @staticmethod
    def get_chapters(book_info) -> list[Chapter]:
        chapters = []
        raw_chapters = book_info.get("chapters")
        if raw_chapters is None and isinstance(book_info.get("audiobook"), dict):
            raw_chapters = book_info["audiobook"].get("chapters")
        if raw_chapters:
            start_time = 0
            for chapter in raw_chapters:
                title = EverandSource.get_chapter_title(chapter)
                chapters.append(Chapter(start_time, title))
                start_time += chapter["duration"]
        return chapters

    @staticmethod
    def _catalog_items_from_data(payload: Any) -> list[CatalogBook]:
        results: list[CatalogBook] = []
        if isinstance(payload, dict):
            raw_items = (
                payload.get("results")
                or payload.get("documents")
                or payload.get("items")
                or payload.get("books")
                or []
            )
            if isinstance(payload.get("search_results"), dict):
                raw_items = payload["search_results"].get("results") or raw_items
        elif isinstance(payload, list):
            raw_items = payload
        else:
            raw_items = []

        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            doc = entry.get("document") if isinstance(entry.get("document"), dict) else entry
            doc_id = doc.get("id")
            title = str(doc.get("title") or "").strip()
            book_url = doc.get("url") or (f"https://www.everand.com/audiobook/{doc_id}/" if doc_id else "")
            if not title or not book_url:
                continue
            author_obj = doc.get("author")
            author = author_obj.get("name") if isinstance(author_obj, dict) else author_obj
            results.append(
                CatalogBook(
                    title=EverandSource.clean_title(title),
                    author=str(author).strip() if author else "",
                    url=urljoin("https://www.everand.com", str(book_url)),
                    source="everand",
                )
            )
        return results

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        raw = self.get(
            f"https://www.everand.com/search?query={quote_plus(cleaned)}&content_type=audiobooks",
            headers={"X-Requested-With": "XMLHttpRequest"},
        ).decode("utf-8", errors="replace")
        try:
            return self._catalog_items_from_data(json.loads(raw))
        except ValueError:
            pass
        tree = lxml.html.fromstring(raw)
        books: list[CatalogBook] = []
        seen = set()
        for link in tree.cssselect('a[href*="/audiobook/"], a[href*="/listen/"], a[href*="/book/"]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            full_url = urljoin("https://www.everand.com", href)
            if full_url in seen:
                continue
            title = " ".join(link.text_content().split())
            if not title:
                continue
            seen.add(full_url)
            books.append(CatalogBook(title=title, url=full_url, source="everand"))
        return books

    def list_library(self) -> list[CatalogBook]:
        payload = self.get_json(
            "https://www.everand.com/saved/data",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        return self._catalog_items_from_data(payload)

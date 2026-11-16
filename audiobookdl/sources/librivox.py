from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import RequestError
from audiobookdl.utils.audiobook import parse_language, parse_release_date

from datetime import date

import lxml.html
import re
from typing import Any
from urllib.parse import quote


def _strip_html(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
    return cleaned or None


def _parse_playtime_ms(raw: Any) -> int | None:
    """Parse LibriVox `playtime` (`HH:MM:SS`, `MM:SS`, or seconds) into milliseconds."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return max(0, round(float(raw) * 1000))
    text = str(raw).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0, round(float(text) * 1000))
    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        sec = 0.0
        for p in parts:
            sec = sec * 60.0 + float(p)
        return max(0, round(sec * 1000))
    return None


class LibrivoxSource(Source):
    _authentication_methods: list[str] = []

    names = ["Librivox"]
    supports_search = True
    supports_library = False

    match = [
        r"https?://(?:www\.)?librivox\.org/.+",
    ]

    def _fetch_api_book(self, url: str) -> dict[str, Any] | None:
        """
        Look up LibriVox JSON API book payload when an RSS project id is present in page HTML.
        """
        page_html = self._get_page(url).decode("utf-8", errors="replace")
        rss_match = re.search(r"librivox\.org/rss/(\d+)", page_html)
        if not rss_match:
            rss_match = re.search(r'href=["\']/rss/(\d+)["\']', page_html)
        if not rss_match:
            return None
        book_id = rss_match.group(1)
        try:
            payload = self.get_json(
                f"https://librivox.org/api/feed/audiobooks?id={book_id}&extended=1&format=json"
            )
        except RequestError:
            return None
        books = payload.get("books") if isinstance(payload, dict) else None
        if isinstance(books, dict):
            books = list(books.values())
        if isinstance(books, list) and books and isinstance(books[0], dict):
            return books[0]
        return None

    def download(self, url: str) -> Audiobook:
        api_book = self._fetch_api_book(url)
        files = self.get_files(url)
        chapters = self.get_chapters(api_book, files)
        return Audiobook(
            session=self._session,
            files=files,
            metadata=self.get_metadata(url, api_book=api_book),
            cover=self.get_cover(url),
            chapters=chapters,
        )

    def get_metadata(self, url: str, api_book: dict[str, Any] | None = None) -> AudiobookMetadata:
        title = self.find_elem_in_page(url, ".content-wrap h1")
        page_html = self._get_page(url).decode("utf-8", errors="replace")
        tree = lxml.html.fromstring(page_html)

        authors: list[str] = []
        narrators: list[str] = []
        genres: list[str] = []
        language: Any = None
        description: str | None = None
        release_date: date | None = None

        if isinstance(api_book, dict):
            if api_book.get("title"):
                title = str(api_book["title"]).strip()
            for a in api_book.get("authors") or []:
                if isinstance(a, dict):
                    full = " ".join(
                        p for p in (a.get("first_name", ""), a.get("last_name", "")) if p
                    ).strip()
                    if full and full not in authors:
                        authors.append(full)
            for section in api_book.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                for reader in section.get("readers") or []:
                    if isinstance(reader, dict) and reader.get("display_name"):
                        name = str(reader["display_name"]).strip()
                        if name and name not in narrators:
                            narrators.append(name)
            for g in api_book.get("genres") or []:
                if isinstance(g, dict) and g.get("name"):
                    g_name = str(g["name"]).strip()
                    if g_name and g_name not in genres:
                        genres.append(g_name)
            if api_book.get("language"):
                language = parse_language(api_book["language"])
            description = _strip_html(api_book.get("description"))
            if api_book.get("copyright_year"):
                release_date = parse_release_date(api_book["copyright_year"])

        if not authors:
            for elem in tree.cssselect(".book-page-author a, .product-details a[href*='/author/']"):
                name = " ".join(elem.text_content().split())
                if name and name not in authors:
                    authors.append(name)
        if not genres:
            for elem in tree.cssselect(".book-page-genre a, p.book-page-genre"):
                text = re.sub(r"^Genre\(s\):\s*", "", " ".join(elem.text_content().split()), flags=re.IGNORECASE)
                for part in text.split(","):
                    cleaned = part.strip()
                    if cleaned and cleaned not in genres:
                        genres.append(cleaned)
        if not description:
            desc_elems = tree.cssselect(".description")
            if desc_elems:
                description = " ".join(desc_elems[0].text_content().split()) or None

        return AudiobookMetadata(
            title=title,
            authors=authors,
            narrators=narrators,
            genres=genres,
            language=language,
            description=description,
            release_date=release_date,
            source="librivox",
            scrape_url=url,
        )

    @staticmethod
    def get_chapters(
        api_book: dict[str, Any] | None,
        files: list[AudiobookFile],
    ) -> list[Chapter]:
        """Build chapters from LibriVox API `sections` when available."""
        if not isinstance(api_book, dict):
            return []
        sections = api_book.get("sections") or []
        chapters: list[Chapter] = []
        offset_ms = 0
        for idx, section in enumerate(sections, start=1):
            if not isinstance(section, dict):
                continue
            ch_title = str(section.get("title") or f"Section {idx}").strip()
            chapters.append(Chapter(start=offset_ms, title=ch_title))
            dur_ms = _parse_playtime_ms(section.get("playtime"))
            if dur_ms:
                offset_ms += dur_ms
        return chapters

    def get_cover(self, url: str) -> Cover:
        cover_url = self.find_elem_in_page(
            url,
            ".book-page-book-cover img",
            data="src",
        )
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def get_files(self, url: str) -> list[AudiobookFile]:
        parts = self.find_elems_in_page(url, ".chapter-download .chapter-name")
        files = []
        for part in parts:
            files.append(
                AudiobookFile(
                    url=part.get("href"),
                    title=part.text,
                    ext="mp3",
                )
            )
        return files

    @staticmethod
    def _parse_feed_books(payload: dict) -> list[CatalogBook]:
        results: list[CatalogBook] = []
        raw_books = payload.get("books") if isinstance(payload, dict) else []
        if isinstance(raw_books, dict):
            raw_books = list(raw_books.values())
        for item in raw_books or ():
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url_librivox") or "").strip()
            if not title or not url:
                continue
            authors_raw = item.get("authors") or []
            author_names = [
                " ".join(
                    part for part in (a.get("first_name", ""), a.get("last_name", "")) if part
                ).strip()
                for a in authors_raw
                if isinstance(a, dict)
            ]
            results.append(
                CatalogBook(
                    title=title,
                    url=url,
                    author=", ".join(a for a in author_names if a),
                    source="librivox",
                )
            )
        return results

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        encoded = quote(cleaned)
        results: list[CatalogBook] = []
        seen_urls = set()
        for param in ("title", "author"):
            try:
                data = self.get_json(
                    f"https://librivox.org/api/feed/audiobooks?{param}={encoded}&format=json"
                )
            except RequestError:
                continue
            for item in self._parse_feed_books(data):
                if item.url not in seen_urls:
                    seen_urls.add(item.url)
                    results.append(item)
        return results

    def list_library(self) -> list[CatalogBook]:
        """Raise NotImplementedError: LibriVox has no personal user library."""
        raise NotImplementedError("LibriVox has no user accounts or personal library")

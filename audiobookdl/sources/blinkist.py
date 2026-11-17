from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import RequestError, UserNotAuthorized
from audiobookdl.utils.audiobook import clean_html_description, parse_language

from typing import Any
from urllib.parse import quote_plus, urlsplit


def _duration_seconds(chapter: dict[str, Any]) -> float:
    for key in ("duration", "duration_in_seconds", "audio_duration", "length"):
        val = chapter.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return 0.0


class BlinkistSource(Source):
    names = ["Blinkist"]
    _authentication_methods = ["cookies"]
    supports_search = True
    supports_library = True
    match = [
        r"https?://(www\.)?blinkist\.com/([a-z]{2}(?:-[a-z]{2})?/)?(nc/reader|books)/.+"
    ]

    def _prepare_headers(self) -> None:
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
                "x-requested-with": "XMLHttpRequest",
            }
        )

    def download(self, url: str) -> Audiobook:
        self._prepare_headers()
        book_id = self.extract_id_from_url(url)
        book_info = self.download_book_info(book_id)
        return Audiobook(
            session=self._session,
            files=self.download_files(book_info),
            metadata=self.format_metadata(book_info),
            cover=self.download_cover(book_info),
            chapters=self.get_chapters(book_info),
        )

    def download_cover(self, book_info: dict) -> Cover | None:
        cover_url = ((book_info.get("book") or {}).get("cover") or {}).get("default", {}).get("src")
        return self.fetch_cover_safe(cover_url)

    @staticmethod
    def format_metadata(book_info: dict) -> AudiobookMetadata:
        book = book_info.get("book", book_info)
        authors: list[str] = []
        if isinstance(book.get("authors"), list):
            authors = [str(a).strip() for a in book["authors"] if str(a).strip()]
        elif isinstance(book.get("author"), str) and book["author"].strip():
            authors = [book["author"].strip()]

        narrators: list[str] = []
        raw_narrators = book.get("narrators") or book.get("narrator") or book.get("reader")
        if isinstance(raw_narrators, list):
            narrators = [
                str(n.get("name") if isinstance(n, dict) else n).strip()
                for n in raw_narrators
                if str(n.get("name") if isinstance(n, dict) else n).strip()
            ]
        elif isinstance(raw_narrators, dict) and raw_narrators.get("name"):
            narrators = [str(raw_narrators["name"]).strip()]
        elif isinstance(raw_narrators, str) and raw_narrators.strip():
            narrators = [raw_narrators.strip()]

        description = (
            clean_html_description(book.get("about_the_book"))
            or clean_html_description(book.get("description"))
            or clean_html_description(book.get("subtitle"))
            or clean_html_description(book.get("summary"))
        )

        genres: list[str] = []
        if isinstance(book.get("categories"), list):
            for cat in book["categories"]:
                label = cat.get("label") if isinstance(cat, dict) else cat
                if isinstance(label, str) and label.strip():
                    genres.append(label.strip())
        elif isinstance(book.get("category"), dict) and book["category"].get("label"):
            genres.append(str(book["category"]["label"]).strip())

        return AudiobookMetadata(
            title=book["title"],
            authors=authors,
            narrators=narrators,
            description=description,
            genres=genres,
            language=parse_language(book.get("language")),
            source="blinkist",
        )

    @staticmethod
    def get_chapters(book_info: dict) -> list[Chapter]:
        raw_chapters = book_info.get("chapters") or []
        chapters: list[Chapter] = []
        offset_ms = 0
        for idx, chapter in enumerate(raw_chapters, start=1):
            title = (
                chapter.get("title")
                or chapter.get("headline")
                or f"Chapter {chapter.get('order_no', idx)}"
            )
            chapters.append(Chapter(start=offset_ms, title=str(title).strip()))
            offset_ms += round(_duration_seconds(chapter) * 1000)
        return chapters

    def download_files(self, book_info: dict) -> list[AudiobookFile]:
        book_id = book_info["book"]["id"]
        chapters = book_info["chapters"]

        def _make_resolver(chapter_id: str):
            def _resolve() -> str:
                return self.get_json(
                    f"https://www.blinkist.com/api/books/{book_id}/chapters/{chapter_id}"
                )["signed_audio_url"]

            return _resolve

        files: list[AudiobookFile] = []
        for chapter in chapters:
            ch_title = chapter.get("title") or chapter.get("headline")
            files.append(
                AudiobookFile(
                    url="",
                    ext="m4a",
                    title=str(ch_title).strip() if ch_title else None,
                    url_resolver=_make_resolver(chapter["id"]),
                )
            )
        return files

    def download_book_info(self, book_id: str) -> dict:
        return self.get_json(
            f"https://www.blinkist.com/api/books/{book_id}/chapters",
        )

    @staticmethod
    def extract_id_from_url(url: str) -> str:
        path = urlsplit(url).path.rstrip("/")
        if not path:
            return ""
        return path.split("/")[-1]

    @staticmethod
    def _catalog_items_from_payload(payload: Any) -> list[CatalogBook]:
        if isinstance(payload, dict):
            raw_items = (
                payload.get("books")
                or payload.get("items")
                or payload.get("results")
                or payload.get("library")
                or []
            )
        elif isinstance(payload, list):
            raw_items = payload
        else:
            raw_items = []

        results: list[CatalogBook] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            raw_book = entry.get("book")
            item = raw_book if isinstance(raw_book, dict) else entry
            slug_or_id = str(item.get("slug") or item.get("id") or "").strip()
            title = str(item.get("title") or slug_or_id).strip()
            if not slug_or_id or not title:
                continue
            lang = str(item.get("language") or "en").strip().lower()
            book_url = item.get("url") or f"https://www.blinkist.com/{lang}/nc/reader/{slug_or_id}"
            author = item.get("author")
            if isinstance(author, list):
                author = ", ".join(str(a) for a in author if a)
            results.append(
                CatalogBook(
                    title=title,
                    author=str(author).strip() if author else "",
                    url=str(book_url),
                    source="blinkist",
                )
            )
        return results

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        self._prepare_headers()
        payload = self.get_json(f"https://www.blinkist.com/api/books/search?q={quote_plus(cleaned)}")
        return self._catalog_items_from_payload(payload)

    def list_library(self) -> list[CatalogBook]:
        self._prepare_headers()
        payload = self.get_json("https://www.blinkist.com/api/library")
        return self._catalog_items_from_payload(payload)

    def verify_auth(self, url: str = "") -> bool:
        self._prepare_headers()
        try:
            self.get_json("https://www.blinkist.com/api/library")
            return True
        except (UserNotAuthorized, RequestError, ValueError):
            return False

from .source import CatalogBook, Source
from audiobookdl import AudiobookFile, AudiobookMetadata, Cover, Audiobook
from typing import List
from urllib.parse import quote


class LibrivoxSource(Source):
    _authentication_methods: List[str] = []

    names = [ "Librivox" ]
    supports_search = True
    supports_library = True

    match = [
        r"https?://librivox.org/.+"
    ]
    def download(self, url: str) -> Audiobook:
        return Audiobook(
            session = self._session,
            files = self.get_files(url),
            metadata = self.get_metadata(url),
            cover = self.get_cover(url)
        )


    def get_metadata(self, url: str) -> AudiobookMetadata:
        title = self.find_elem_in_page(url, ".content-wrap h1")
        return AudiobookMetadata(title)

    def get_cover(self, url: str) -> Cover:
        cover_url = self.find_elem_in_page(
            url,
            ".book-page-book-cover img",
            data="src"
        )
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def get_files(self, url: str) -> List[AudiobookFile]:
        parts = self.find_elems_in_page(url, ".chapter-download .chapter-name")
        files = []
        for part in parts:
            files.append(AudiobookFile(
                url = part.get("href"),
                title = part.text,
                ext = "mp3"
            ))
        return files

    @staticmethod
    def _parse_feed_books(payload: dict) -> List[CatalogBook]:
        results: List[CatalogBook] = []
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

    def search(self, query: str) -> List[CatalogBook]:
        encoded = quote(f"^{query.strip()}")
        data = self.get_json(f"https://librivox.org/api/feed/audiobooks?title={encoded}&format=json")
        return self._parse_feed_books(data)

    def list_library(self) -> List[CatalogBook]:
        data = self.get_json("https://librivox.org/api/feed/audiobooks?limit=25&format=json")
        return self._parse_feed_books(data)

from .source import Source
from audiobookdl import AudiobookFile, Chapter, AudiobookMetadata, Cover, Audiobook
from typing import Any, List, Dict, MutableMapping, Optional, Union
import uuid
from audiobookdl.exceptions import MissingBookAccess
import base64
import re




class BookBeatSource(Source):
    match = [
        r"https?://(www.)?bookbeat.+",
    ]
    names = ["BookBeat"]
    _authentication_methods = [
        "login",
    ]
    saved_books: dict
    book_info: dict

    @staticmethod
    def create_device_id() -> str:
        """Create random device id"""
        return (
            str(uuid.uuid3(uuid.NAMESPACE_DNS, "audiobook-dl"))
            + " "
            + base64.b64encode(b"Personal Computer").decode()
        )

    def _login(self, url: str, username: str, password: str):
        headers: MutableMapping[str, Union[str, bytes]] = {
            "accept": "application/hal+json",
            "bb-client": "BookBeatApp",
            "bb-device": self.create_device_id(),
        }
        self._session.headers.update(headers)
        login_json = {"username": username, "password": password}
        tokens = self.post_json(
            "https://api.bookbeat.com/api/login",
            json=login_json
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
        book_id_re = r"(\d+)$"
        wanted_id_match = re.search(book_id_re, url)
        if not wanted_id_match:
            raise ValueError(f"Couldn't get bookid from url {url}")
        wanted_id = wanted_id_match.group(1)
        book_info = self.find_book_info(wanted_id)
        return Audiobook(
            session = self._session,
            files = self.get_files(book_info),
            metadata = self.get_metadata(book_info),
            cover = self.get_cover(book_info),
            chapters = self.get_chapters(book_info),
        )


    def download_license_url(self, book_info)-> str:
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
        title = book_info["metadata"]["title"]
        metadata = AudiobookMetadata(title)
        try:
            contributors = [
                e["contributors"] for e in book_info["metadata"]["editions"] if e["format"] == "audioBook"
            ][0]
            if not contributors:
                return metadata
            for contributor in contributors:
                name = f"{contributor['firstname']} {contributor['lastname']}"
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
            chapters.append(Chapter(track["start"], f"Chapter {chapter_number+1}"))
        return chapters


    def get_cover(self, book_info: Dict) -> Cover:
        cover_url = book_info["metadata"]["cover"]
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")


    def find_book_info(self, book_id: str) -> Dict:
        """Find book by id from owned books"""
        for book in self.saved_books["_embedded"]["savedBooks"]:
            if str(book["bookid"]) == book_id:
                book["metadata"] = self.get_json(
                    book["_links"]["book"]["href"]
                )
                return book
        raise MissingBookAccess

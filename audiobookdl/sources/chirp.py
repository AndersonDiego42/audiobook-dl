from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import BookNotFound, DataNotPresent

import base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import lxml.html
import re
from urllib.parse import urljoin

LOGIN_URL = "https://www.chirpbooks.com/users/sign_in"


class ChirpSource(Source):
    match = [
        r"https?://(www\.)?chirpbooks\.com/player/\d+",
        r"https?://(www\.)?chirpbooks\.com/audiobooks/[^/?#]+",
    ]
    names = ["Chirp"]
    supports_library = True
    headers = {
        "content-type": "application/json"
    }

    def _resolve_player_url(self, url: str) -> str:
        """Resolve `/audiobooks/...` catalog URLs to `/player/<id>`."""
        if re.search(r"/player/\d+", url):
            return url
        page_html = self._get_page(url).decode("utf-8", errors="replace")
        for pattern in (
            r'data-audiobook-id=["\'](\d+)["\']',
            r"/player/(\d+)",
            r'"audiobookId"\s*:\s*(\d+)',
            r'"id"\s*:\s*(\d+)\s*,\s*"__typename"\s*:\s*"Audiobook"',
        ):
            match = re.search(pattern, page_html)
            if match:
                return f"https://www.chirpbooks.com/player/{match.group(1)}"
        raise BookNotFound

    def _extract_user_id(self, player_url: str) -> int:
        """Extract the authenticated Chirp user id from player HTML, ignoring book/entity ids."""
        page_html = self._get_page(player_url).decode("utf-8", errors="replace")
        for pattern in (
            r'"currentUser"\s*:\s*\{[^{}]*?"id"\s*:\s*(\d+)',
            r'"id"\s*:\s*(\d+)\s*,\s*"__typename"\s*:\s*"User"',
            r'"__typename"\s*:\s*"User"[^{}]*?"id"\s*:\s*(\d+)',
            r'"user(?:Id|_id)"\s*:\s*(\d+)',
            r'data-user-id=["\'](\d+)["\']',
        ):
            match = re.search(pattern, page_html)
            if match:
                return int(match.group(1))
        for match in re.finditer(r'"id"\s*:\s*(\d+)', page_html):
            snippet = page_html[match.end() : match.end() + 80]
            if re.search(
                r'^\s*,\s*"__typename"\s*:\s*"(?:Audiobook|Author|Narrator|Series|Chapter)"',
                snippet,
            ):
                continue
            return int(match.group(1))
        raise DataNotPresent

    def download(self, url: str) -> Audiobook:
        player_url = self._resolve_player_url(url)
        book_id = int(self.find_elem_in_page(player_url, "div.user-audiobook", "data-audiobook-id"))
        user_id = self._extract_user_id(player_url)
        tracks = self._get_tracks(book_id)
        key, iv = self._create_key(player_url, user_id)
        return Audiobook(
            session=self._session,
            files=self.get_files(book_id, key, iv, tracks),
            metadata=self.get_metadata(player_url),
            cover=self.get_cover(player_url),
            chapters=self.get_chapters(tracks),
        )

    def get_metadata(self, url: str) -> AudiobookMetadata:
        title = self.find_elem_in_page(url, "title")
        metadata = AudiobookMetadata(title, source="chirp", scrape_url=url)
        for credit in self.find_elems_in_page(url, ".credit"):
            text = " ".join(credit.text_content().split())
            if text.startswith("Written by"):
                metadata.add_author(text[11:].strip())
            elif text.startswith("Narrated by"):
                metadata.add_narrator(text[12:].strip())
        return metadata

    def get_cover(self, url: str) -> Cover:
        cover_url = self.find_elem_in_page(url, "img.cover-image", data="src")
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def get_audio_url(self, book_id: int, key: bytes, iv: bytes, track):
        url_resp = self.post_json(
            "https://www.chirpbooks.com/api/graphql",
            json={
                "operationName": "fetchAudiobookTrackUrl",
                "query": "query fetchAudiobookTrackUrl($id:ID!,$partNumber:Int!,$chapterNumber:Int!){audiobook(id:$id){track(partNumber:$partNumber,chapterNumber:$chapterNumber){webPlayerMediaUrl}}}",
                "variables": {
                    "id": book_id,
                    "chapterNumber": track["chapterNumber"],
                    "partNumber": track["partNumber"],
                },
            },
            headers=self.headers,
        )
        webplayermediaurl = url_resp["data"]["audiobook"]["track"]["webPlayerMediaUrl"]
        ciphertext = base64.b64decode(webplayermediaurl)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return unpad(cipher.decrypt(ciphertext), AES.block_size).decode("utf-8")

    def get_files(self, book_id: int, key: bytes, iv: bytes, tracks) -> list[AudiobookFile]:
        def _make_resolver(trk: dict):
            return lambda: self.get_audio_url(book_id, key, iv, trk)

        return [
            AudiobookFile(
                url="",
                ext="mp3",
                title=track["displayName"],
                url_resolver=_make_resolver(track),
            )
            for track in tracks
        ]

    def get_chapters(self, tracks) -> list[Chapter]:
        chapters = []
        start_time = 0
        for track in tracks:
            title = track["displayName"]
            chapters.append(Chapter(start_time, title))
            start_time += track["durationMs"]
        return chapters

    def _create_key(self, url: str, user_id: int) -> tuple[bytes, bytes]:
        """Creates key and iv to decrypt audio url"""
        key = bytes(self.find_elem_in_page(url, "div.user-audiobook", "data-dk"), "UTF-8")
        padding = "x" * (12 - len(str(user_id)))
        padded_user_id = f"{padding}{user_id}"
        iv = base64.b64encode(bytes(padded_user_id, "UTF-8"))
        return key, iv

    def _get_tracks(self, book_id: int):
        response = self.post_json(
            "https://www.chirpbooks.com/api/graphql",
            json={
                "operationName": "fetchAudiobookTracks",
                "query": "query fetchAudiobookTracks($id:ID!){audiobook(id:$id){tracks{partNumber chapterNumber offsetFromBookStartMs durationMs displayName}}}",
                "variables": {
                    "id": book_id
                },
            },
            headers=self.headers,
        )
        return response["data"]["audiobook"]["tracks"]

    def list_library(self) -> list[CatalogBook]:
        """Return purchased audiobooks from the user's Chirp library."""
        books: list[CatalogBook] = []
        try:
            payload = self.post_json(
                "https://www.chirpbooks.com/api/graphql",
                json={
                    "operationName": "fetchUserLibrary",
                    "query": (
                        "query fetchUserLibrary{"
                        "currentUser{libraryAudiobooks{id title authors{name}}}"
                        "}"
                    ),
                    "variables": {},
                },
                headers=self.headers,
            )
            items = (
                (payload or {})
                .get("data", {})
                .get("currentUser", {})
                .get("libraryAudiobooks", [])
            )
            for item in items or ():
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                authors = [
                    str(a.get("name")).strip()
                    for a in (item.get("authors") or [])
                    if isinstance(a, dict) and a.get("name")
                ]
                books.append(
                    CatalogBook(
                        title=str(item.get("title") or f"Chirp Book {item['id']}").strip(),
                        author=", ".join(authors),
                        url=f"https://www.chirpbooks.com/player/{item['id']}",
                        source="chirp",
                    )
                )
            if books:
                return books
        except Exception:
            pass

        html = self._get_page("https://www.chirpbooks.com/users/library", use_cache=False).decode(
            "utf-8", errors="replace"
        )
        tree = lxml.html.fromstring(html)
        seen = set()
        for link in tree.cssselect('a[href*="/player/"]'):
            href = (link.get("href") or "").strip()
            match = re.search(r"/player/(\d+)", href)
            if not match:
                continue
            bid = match.group(1)
            if bid in seen:
                continue
            seen.add(bid)
            title = " ".join(link.text_content().split()) or f"Chirp Book {bid}"
            books.append(
                CatalogBook(
                    title=title,
                    url=urljoin("https://www.chirpbooks.com", href),
                    source="chirp",
                )
            )
        return books

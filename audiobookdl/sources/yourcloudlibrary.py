from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover, logging
from audiobookdl.exceptions import RequestError, UserNotAuthorized

from datetime import datetime
import json
import lxml.html
import re
from urllib.parse import urljoin


class YourCloudLibrarySource(Source):
    match = [
        r"https?://audio\.yourcloudlibrary\.com/listen/.+",
        r"https?://ebook\.yourcloudlibrary\.com/library/[^/]+/detail/.+",
        r"https?://ebook\.yourcloudlibrary\.com/library/[^/]+/(audiobook|title)/.+",
    ]
    names = ["YourCloudLibrary"]
    login_data = ["username", "password", "library"]
    _authentication_methods = [
        "cookies",
        "login",
    ]
    supports_library = True

    def download(self, url: str) -> Audiobook:
        url = self.get_listening_url(url)
        account_id = self.extract_json_string(url, "accountId")
        logging.debug(f"{account_id=}")
        fulfillment_id = self.extract_json_string(url, "fulfillmentId")
        logging.debug(f"{fulfillment_id=}")
        license_id = self.extract_json_string(url, "licenseId")
        logging.debug(f"{license_id=}")
        session_key = self.extract_json_string(url, "session_key")
        self._session.headers.update({"Session-Key": session_key})
        book_info = self.download_book_info(account_id, fulfillment_id)
        playlist = self.download_playlist(fulfillment_id, license_id)
        return Audiobook(
            session=self._session,
            files=self.get_files(playlist),
            metadata=self.get_metadata(book_info, url=url),
            cover=self.download_cover(book_info),
            chapters=self.create_chapters(book_info),
        )

    @staticmethod
    def get_listening_url(url: str) -> str:
        """
        Get url for listening page.

        :param url: Url to information or listening page
        :return: Url to listening page
        """
        if re.match(YourCloudLibrarySource.match[0], url):
            return url
        clean_path = url.rstrip("/").split("?")[0].split("#")[0]
        book_id = clean_path.split("/")[-1]
        return f"https://audio.yourcloudlibrary.com/listen/{book_id}"

    def extract_json_string(self, url: str, key: str) -> str:
        """
        Extracts string from json in web page.

        :param url: Url of page to extract from
        :param key: Key of value to extract
        :returns: Value
        """
        return self.find_in_page(
            url,
            fr'(?<=("{key}":"))[^"]+',
            force_cookies=True,
        )

    @staticmethod
    def get_files(playlist: dict) -> list[AudiobookFile]:
        files = []
        for f in playlist["playlist"]:
            files.append(
                AudiobookFile(
                    url=f["url"],
                    ext="mp3",
                )
            )
        return files

    @staticmethod
    def get_metadata(book_info: dict, *, url: str | None = None) -> AudiobookMetadata:
        publisher_raw = book_info.get("publisher")
        publisher = (
            publisher_raw.get("name")
            if isinstance(publisher_raw, dict)
            else (str(publisher_raw).strip() if isinstance(publisher_raw, str) and publisher_raw.strip() else None)
        )
        desc_raw = book_info.get("description") or book_info.get("synopsis")
        description = (
            " ".join(re.sub(r"<[^>]+>", " ", desc_raw).split()) or None
            if isinstance(desc_raw, str) and desc_raw.strip()
            else None
        )
        isbn_raw = book_info.get("isbn") or book_info.get("isbn13")
        metadata = AudiobookMetadata(
            title=book_info["title"],
            authors=book_info["authors"],
            narrators=book_info["narrators"],
            description=description,
            publisher=publisher,
            isbn=str(isbn_raw).strip() if isbn_raw else None,
            language=book_info.get("language"),
            source="yourcloudlibrary",
            scrape_url=url,
        )
        if book_info.get("series") is not None and len(book_info["series"]) >= 1:
            metadata.series = book_info["series"][0]
        pub_date = book_info.get("publish_date") or book_info.get("release_date")
        if isinstance(pub_date, str) and pub_date.strip():
            try:
                metadata.release_date = datetime.fromisoformat(pub_date.strip().replace("Z", "+00:00")).date()
            except ValueError:
                pass
        return metadata

    def download_cover(self, meta) -> Cover:
        cover_url = meta["cover_url"]
        cover_data = self.get(f"{cover_url}?aspect=1:1")
        return Cover(cover_data, "jpg")

    @staticmethod
    def create_chapters(book_info: dict) -> list[Chapter]:
        chapters = []
        elapsed = 0
        for chapter in book_info["chapters"]:
            ch_title = str(
                chapter.get("title") or f"Chapter {chapter['chapter_number']}"
            ).strip()
            chapters.append(
                Chapter(
                    start=elapsed,
                    title=ch_title,
                )
            )
            elapsed += chapter["duration"]
        return chapters

    def download_book_info(self, account_id: str, fulfillment_id: str) -> dict:
        """
        Download metadata about book.

        :param account_id: Findaway account identifier
        :param fulfillment_id: Id used for book
        :returns: Metadata about book
        """
        return self.get_json(
            f"https://api.findawayworld.com/v4/accounts/{account_id}/audiobooks/{fulfillment_id}",
            force_cookies=True,
        )["audiobook"]

    def download_playlist(self, fulfillment_id: str, license_id: str) -> dict:
        """Download list of audio files."""
        return self.post_json(
            f"https://api.findawayworld.com/v4/audiobooks/{fulfillment_id}/playlists",
            json={"license_id": license_id},
        )

    @staticmethod
    def _is_failed_login_payload(payload: dict) -> bool:
        if any(payload.get(k) for k in ("error", "errors", "loginError", "errorMessage")):
            return True
        if any(payload.get(k) is False for k in ("ok", "success", "authenticated", "loggedIn", "isLoggedIn")):
            return True
        for key in ("user", "patron"):
            if key in payload and payload[key] is None:
                return True
        return False

    def _login(self, url: str, username: str, password: str, library: str):  # type: ignore
        self._library_id = library
        self.get(f"https://ebook.yourcloudlibrary.com/library/{library}/featured")
        resp = self.request(
            "POST",
            "https://ebook.yourcloudlibrary.com/?_data=root",
            data={
                "action": "login",
                "barcode": username,
                "pin": password,
                "library": library,
            },
        )
        if resp.status_code in (400, 401, 403, 422):
            raise UserNotAuthorized
        if resp.status_code != 200:
            raise RequestError
        if not resp.content:
            return
        try:
            payload = json.loads(resp.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and self._is_failed_login_payload(payload):
            raise UserNotAuthorized

    def list_library(self) -> list[CatalogBook]:
        """List currently borrowed audiobooks from the patron's CloudLibrary shelf."""
        library: str | None = getattr(self, "_library_id", None) or getattr(
            self._options, "library", None
        )
        if not library:
            return []
        shelf_url = f"https://ebook.yourcloudlibrary.com/library/{library}/shelf"
        raw = self._get_page(shelf_url, use_cache=False).decode("utf-8", errors="replace")
        results: list[CatalogBook] = []
        seen = set()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None

        if isinstance(data, dict):
            items = data.get("items") or data.get("checkouts") or data.get("loans") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("isbn") or item.get("itemId")
                title = str(item.get("title") or "").strip()
                if not item_id or not title:
                    continue
                book_url = f"https://ebook.yourcloudlibrary.com/library/{library}/detail/{item_id}"
                if book_url in seen:
                    continue
                seen.add(book_url)
                results.append(
                    CatalogBook(
                        title=title,
                        author=str(item.get("author") or "").strip(),
                        url=book_url,
                        source="yourcloudlibrary",
                    )
                )
            return results

        tree = lxml.html.fromstring(raw)
        for link in tree.cssselect('a[href*="/detail/"], a[href*="/listen/"]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            full_url = urljoin("https://ebook.yourcloudlibrary.com", href)
            if full_url in seen:
                continue
            title = " ".join(link.text_content().split())
            if not title:
                continue
            seen.add(full_url)
            results.append(CatalogBook(title=title, url=full_url, source="yourcloudlibrary"))
        return results

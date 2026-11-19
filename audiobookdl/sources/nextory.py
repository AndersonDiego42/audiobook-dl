from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, BookId, Chapter, Cover, Result, Series, logging
from audiobookdl.exceptions import (
    AudiobookDLException,
    BookHasNoAudiobook,
    BookNotFound,
    DataNotPresent,
    GenericAudiobookDLException,
    RequestError,
    UserNotAuthorized,
)
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import strip_auth_headers

import calendar
from datetime import date, datetime
import pycountry
import re
from typing import Any
from urllib.parse import quote_plus, urlparse
import uuid


_TLD_LOCALES: dict[str, str] = {
    "se": "sv_SE",
    "fi": "fi_FI",
    "de": "de_DE",
    "nl": "nl_NL",
    "no": "nb_NO",
    "dk": "da_DK",
    "fr": "fr_FR",
    "es": "es_ES",
    "ch": "de_CH",
    "at": "de_AT",
    "com": "en_GB",
}


class NextorySource(Source):
    match = [
        r"https?://((www|catalog-\w\w)\.)?nextory.+",
    ]
    names = ["Nextory"]
    _authentication_methods = ["login"]
    APP_ID = "200"
    LOCALE = "en_GB"
    supports_library = True
    supports_search = True

    def __init__(self, options) -> None:
        super().__init__(options)
        # Keep want-to-read cache isolated per source instance.
        self._wantlist_cache: list[dict] | None = None
        self._configured_locale: str | None = None

    def verify_auth(self, url: str = "") -> bool:
        has_token = "X-Profile-Token" in self._session.headers or "token" in self._session.headers
        if not self.authenticated and not has_token:
            return False
        try:
            payload = self.get_json("https://api.nextory.com/user/v1/me/profiles")
            return isinstance(payload, dict) and isinstance(payload.get("profiles"), list)
        except (RequestError, UserNotAuthorized, ValueError):
            return False

    @staticmethod
    def create_device_id() -> str:
        """Generate a random per-user UUIDv4 device identifier."""
        return str(uuid.uuid4())

    def _resolve_locale(self, url: str = "") -> str:
        configured = getattr(self._options, "locale", None) or getattr(
            self, "_configured_locale", None
        )
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        if url:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            tld = host.rsplit(".", 1)[-1] if "." in host else ""
            if tld in _TLD_LOCALES and tld != "com":
                return _TLD_LOCALES[tld]
            path_parts = [p for p in (parsed.path or "").split("/") if p]
            if path_parts and len(path_parts[0]) == 2 and path_parts[0].lower() in _TLD_LOCALES:
                return _TLD_LOCALES[path_parts[0].lower()]
        return self.LOCALE

    def get_compatible_app_version(self) -> str:
        today = date.today()
        month_idx = today.year * 12 + (today.month - 1) - 2
        year, month_zero = divmod(month_idx, 12)
        month = month_zero + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        d = date(year, month, day)
        return f"{d.year}-{d.month}-{d.day}"

    @staticmethod
    def _select_profile(profiles: list[dict[str, Any]], preferred: str | None = None) -> dict[str, Any]:
        """Select a Nextory profile by name/id when `preferred` matches, otherwise `profiles[0]`."""
        if not profiles:
            raise UserNotAuthorized()
        if preferred:
            target = preferred.strip().lower()
            for prof in profiles:
                if not isinstance(prof, dict):
                    continue
                if str(prof.get("name") or "").strip().lower() == target:
                    return prof
                if str(prof.get("id") or "").strip().lower() == target:
                    return prof
        return profiles[0]

    def _login(self, url: str, username: str, password: str):
        device_id = self.get_or_create_device_id(
            "nextory_device_id.txt", self.create_device_id
        )
        locale = self._resolve_locale(url)
        logging.debug(f"{device_id=} {locale=}")
        self._session.headers.update(
            {
                "X-Application-Id": self.APP_ID,
                "X-App-Version": self.get_compatible_app_version(),
                "X-Locale": locale,
                "X-Model": "Personal Computer",
                "X-Device-Id": device_id,
                "X-Os-Info": "ios",
            }
        )
        session_response = self.request(
            "POST",
            "https://api.nextory.com/user/v1/sessions",
            json={
                "identifier": username,
                "password": password,
            },
        )
        session_response_json = session_response.json()
        logging.debug(f"nextory login status={session_response.status_code}")

        login_token = session_response_json.get("login_token")
        if login_token is None:
            error = session_response_json.get("error", {})
            reason = error.get("key", {})
            error_details = error.get("description", "Unknown Error")
            if reason == "UserNotFound":
                raise UserNotAuthorized()
            raise GenericAudiobookDLException(heading=reason, body=error_details)

        country = session_response_json["country"]
        self._session.headers.update(
            {
                "token": login_token,
                "X-Login-Token": login_token,
                "X-Country-Code": country,
            }
        )
        profiles_response_json = self.get_json(
            "https://api.nextory.com/user/v1/me/profiles",
        )
        preferred_profile = getattr(self._options, "library", None)
        profile = self._select_profile(
            profiles_response_json.get("profiles") or [],
            preferred=preferred_profile,
        )
        login_key = profile["login_key"]
        authorize_response_json = self.post_json(
            "https://api.nextory.com/user/v1/profile/authorize",
            json={
                "login_key": login_key
            },
        )
        profile_token = authorize_response_json["profile_token"]
        self._session.headers.update({"X-Profile-Token": profile_token})
        logging.debug(f"profile_token={logging.mask_secret(profile_token)}")

    def download(self, url) -> Result:
        if self._is_wantlist_url(url):
            return self._download_wantlist()
        id_match = re.search(r"(\d+)(?:[/?#].*)?$", url)
        if id_match is None:
            raise BookNotFound
        book_id = int(id_match.group(1))
        return self.download_from_id(book_id)

    def download_from_id(self, book_id: int) -> Audiobook:
        book_info = self.find_book_info(book_id)
        logging.debug(f"nextory book_info keys: {sorted(book_info.keys())}")
        audio_data = self.download_audio_data(book_info)
        if "files" not in audio_data:
            logging.debug(f"nextory audio_data without 'files': {audio_data!r}")
            raise BookHasNoAudiobook
        return Audiobook(
            session=self.create_unauthenticated_session(),
            files=self.get_files(audio_data),
            metadata=self.get_metadata(book_info),
            cover=self.get_cover(book_info),
            chapters=self.get_chapters(audio_data),
        )

    @staticmethod
    def _is_wantlist_url(url: str) -> bool:
        """Match URLs that refer to the user's want-to-read list rather
        than a specific book, e.g. ``https://nextory.com/se/want-to-read``."""
        return "want-to-read" in url.lower()

    def _get_wantlist(self) -> list[dict]:
        """Return the want-to-read list, fetching it at most once per source."""
        if self._wantlist_cache is None:
            self._wantlist_cache = self.download_want_to_read_list()
        return self._wantlist_cache

    def _download_wantlist(self) -> Series[int]:
        """Build a Series of every audiobook currently on the want-to-read list."""
        books: list[BookId[int] | Audiobook] = []
        for book_info in self._get_wantlist():
            try:
                self.find_format_data(book_info)
            except DataNotPresent:
                continue
            books.append(BookId(book_info["id"]))
        return Series(
            title="Nextory want to read",
            books=books,
        )

    def find_book_info(self, book_id: int, want_to_read_list: list | None = None) -> dict:
        """Find book metadata by id, checking cached wantlist or product API first."""
        if want_to_read_list is not None:
            for book in want_to_read_list:
                if book.get("id") == book_id:
                    return book
        elif self._wantlist_cache is not None:
            for book in self._wantlist_cache:
                if book.get("id") == book_id:
                    return book

        try:
            product = self.get_json(f"https://api.nextory.com/library/v1/products/{book_id}")
            if isinstance(product, dict) and product.get("id") is not None:
                return product
        except RequestError:
            pass

        if want_to_read_list is None and self._wantlist_cache is None:
            try:
                fallback_list = self._get_wantlist()
            except DataNotPresent:
                fallback_list = []
            for book in fallback_list:
                if book.get("id") == book_id:
                    return book

        raise AudiobookDLException(error_description="nextory_want_to_read")

    def download_want_to_read_id(self) -> str:
        """Downloads profile id for want to read list"""
        products_lists = self.get_json(
            "https://api.nextory.com/library/v1/me/product_lists",
            params={
                "page": 0,
                "per": 50,
            },
        )["product_lists"]
        for product_list in products_lists:
            if product_list["type"] == "want_to_read":
                return product_list["id"]
        raise DataNotPresent

    def download_want_to_read_list(self) -> list[dict]:
        want_to_read_id = self.download_want_to_read_id()
        all_products: list[dict] = []
        per = 100
        page = 0
        while True:
            data = self.get_json(
                "https://api.nextory.com/library/v1/me/product_lists/want_to_read/products",
                params={
                    "page": str(page),
                    "per": str(per),
                    "id": want_to_read_id,
                },
            )
            products = data.get("products") or []
            all_products.extend(products)
            if data.get("has_next") is False:
                break
            if not products or (len(products) < per and not data.get("has_next")):
                break
            page += 1
        return all_products

    def download_audio_data(self, book_info: dict) -> dict:
        format_data = self.find_format_data(book_info)
        format_id = format_data["identifier"]
        return self.get_json(
            f"https://api.nextory.com/reader/books/{format_id}/packages/audio"
        )

    @staticmethod
    def find_format_data(book_info: dict) -> dict:
        for fmt in book_info["formats"]:
            if fmt["type"] == "hls":
                return fmt
        raise DataNotPresent

    def _resolve_stream_uri(self, uri: str, session_headers: dict[str, str]) -> list[AudiobookFile]:
        """Resolve HLS playlist URI directly via session redirects, falling back cleanly."""
        try:
            files = self.get_stream_files(
                uri,
                headers=session_headers,
                expected_content_type=("audio/aac", "audio/x-aac", "video/MP2T"),
            )
            if files:
                return files
        except RequestError:
            if "master" not in uri:
                raise
        if "master" in uri:
            return self.get_stream_files(
                uri.replace("master", "media"),
                headers=session_headers,
                expected_content_type=("audio/aac", "audio/x-aac", "video/MP2T"),
            )
        return []

    def get_files(self, audio_data) -> list[AudiobookFile]:
        session_headers = strip_auth_headers(self._session.headers)
        raw_files = audio_data["files"]
        if len(raw_files) <= 1:
            files: list[AudiobookFile] = []
            for file in raw_files:
                files.extend(self._resolve_stream_uri(file["uri"], session_headers))
            return files

        grouped = run_cancellable_pool(
            lambda entry: self._resolve_stream_uri(entry["uri"], session_headers),
            raw_files,
            max_workers=min(8, len(raw_files)),
        )
        return [item for part_files in grouped for item in part_files]

    def get_metadata(self, book_info) -> AudiobookMetadata:
        series_name, series_order = self._extract_series_metadata(book_info)
        prod_id = book_info.get("id")
        scrape_url = f"https://www.nextory.com/book/{prod_id}" if prod_id is not None else None
        metadata = AudiobookMetadata(
            title=book_info["title"],
            authors=[author["name"] for author in book_info["authors"]],
            narrators=[narrator["name"] for narrator in book_info["narrators"]],
            description=book_info["description_full"],
            series=series_name,
            series_order=series_order,
            source="nextory",
            scrape_url=scrape_url,
        )
        lang_code = book_info.get("language")
        if lang_code:
            language = pycountry.languages.get(alpha_2=lang_code)
            if language is not None:
                metadata.language = language
        try:
            audio_format = self.find_format_data(book_info)
        except DataNotPresent:
            audio_format = None
        if audio_format:
            publisher = audio_format.get("publisher")
            if isinstance(publisher, dict):
                publisher_name = publisher.get("name")
                if publisher_name:
                    metadata.publisher = publisher_name
            isbn = audio_format.get("isbn")
            if isbn:
                metadata.isbn = str(isbn)
            pub_date = audio_format.get("publication_date")
            if pub_date:
                try:
                    metadata.release_date = datetime.strptime(pub_date, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    pass
        return metadata

    @staticmethod
    def _extract_series_metadata(book_info: dict) -> tuple:
        """Pull series name and position from Nextory's book payload."""
        series = book_info.get("series")
        if not series:
            return None, None
        name = series.get("name")
        position = None
        raw_volume = book_info.get("volume")
        if raw_volume is not None:
            try:
                volume = int(raw_volume)
                if volume > 0:
                    position = volume
            except (TypeError, ValueError):
                pass
        return name, position

    @staticmethod
    def get_chapters(audio_data: dict) -> list[Chapter]:
        raw_chapters = audio_data.get("chapters")
        if isinstance(raw_chapters, list) and raw_chapters:
            chapters: list[Chapter] = []
            for index, ch in enumerate(raw_chapters):
                if not isinstance(ch, dict):
                    continue
                start_val = ch.get("start_at") if ch.get("start_at") is not None else ch.get("start", 0)
                ch_title = str(ch.get("title") or ch.get("name") or f"Chapter {index + 1}").strip()
                chapters.append(Chapter(title=ch_title, start=int(start_val or 0)))
            if chapters:
                return chapters

        chapters = []
        for index, file in enumerate(audio_data.get("files") or []):
            ch_title = str(file.get("title") or file.get("name") or f"Chapter {index + 1}").strip()
            chapters.append(Chapter(title=ch_title, start=file["start_at"]))
        return chapters

    def get_cover(self, book_info) -> Cover | None:
        cover_url = self.find_format_data(book_info).get("img_url")
        return self.fetch_cover_safe(cover_url)

    @staticmethod
    def _products_to_catalog(products: Any) -> list[CatalogBook]:
        results: list[CatalogBook] = []
        for product in products or ():
            if not isinstance(product, dict):
                continue
            prod_id = product.get("id")
            if not prod_id:
                continue
            title = str(product.get("title") or prod_id)
            authors = [
                str(a.get("name") or "")
                for a in (product.get("authors") or [])
                if isinstance(a, dict)
            ]
            results.append(
                CatalogBook(
                    title=title,
                    url=f"https://www.nextory.com/book/{prod_id}",
                    author=", ".join(a for a in authors if a),
                    source="nextory",
                )
            )
        return results

    def list_library(self) -> list[CatalogBook]:
        return self._products_to_catalog(self.download_want_to_read_list())

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        data = self.get_json(f"https://api.nextory.com/discovery/v1/search?q={quote_plus(cleaned)}")
        raw_items = (
            data.get("products")
            or data.get("items")
            or data.get("results")
            or []
        )
        return self._products_to_catalog(raw_items)

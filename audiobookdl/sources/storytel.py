from __future__ import annotations
from requests.models import Response
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audiobookdl.args import DownloadOptions
from .source import CatalogBook, Source
from audiobookdl import (
    AudiobookFile,
    Chapter,
    logging,
    AudiobookMetadata,
    Cover,
    Audiobook,
    Series,
    BookId,
    Result,
)
from audiobookdl.utils.audiobook import is_ebook
from audiobookdl.exceptions import (
    GenericAudiobookDLException,
    UserNotAuthorized,
    CloudflareBlocked,
    BookNotFound,
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotReleased,
    DataNotPresent,
    RequestError,
)
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from typing import Any
from collections.abc import Sequence
from urllib3.util import parse_url
from urllib.parse import quote_plus, parse_qs
from datetime import datetime, date
import pycountry
import json
import re
import os
import uuid
from audiobookdl.utils import read_asset_file


def _load_metadata_corrections() -> dict[str, dict[str, Any]]:
    raw = json.loads(read_asset_file("assets/sources/storytel/metadata_corrections.json"))
    for entry in (raw.get("books") or {}).values():
        if isinstance(entry.get("release_date"), str):
            entry["release_date"] = date.fromisoformat(entry["release_date"])
    return raw


metadata_corrections: dict[str, dict[str, Any]] = _load_metadata_corrections()


# path data of the headphone icon on the website used to identify audiobooks
svg_headphone_path = "M8.25 12.371h-.625c-1.38 0-2.5 1.121-2.5 2.505v3.12a2.503 2.503 0 0 0 2.5 2.504h.625c.69 0 1.25-.56 1.25-1.252v-5.627c0-.691-.559-1.25-1.25-1.25Zm-.625 6.254a.628.628 0 0 1-.625-.63v-3.12c0-.347.28-.63.625-.63v4.38ZM12 3C6.41 3 2.178 7.652 2 13v4.375c0 .346.28.625.625.625h.625a.626.626 0 0 0 .625-.627V13c0-4.48 3.646-8.117 8.125-8.117 4.48 0 8.125 3.637 8.125 8.117v4.371c-.035.348.281.629.625.629l.625.001c.346 0 .625-.28.625-.625v-4.411C21.82 7.652 17.59 3 12 3Zm4.375 9.371h-.625c-.69 0-1.25.56-1.25 1.252v5.625c0 .692.56 1.252 1.25 1.252h.625c1.38 0 2.5-1.121 2.5-2.505v-3.12a2.503 2.503 0 0 0-2.5-2.504ZM17 17.996a.628.628 0 0 1-.625.629v-4.379c.345 0 .625.283.625.63v3.12Z"


class StorytelSource(Source):
    match = [
        r"https?://(?:www\.)?(?:storytel|mofibo)\.com/(?:(?P<language>\w+)(?:/(?P<language2>\w+))?/)?(?P<list_type>(?:books|series|authors|narrators|publishers|categories))/.+",
        # The user's own bookshelf ("want to read"); dispatched in download().
        # Allows one or two locale segments (e.g. /se/ or /se/sv/).
        r"https?://(?:www\.)?(?:storytel|mofibo)\.com/\w+(?:/\w+)?/(?:want-to-read|bookshelf)/?$",
    ]
    names = ["Storytel", "Mofibo"]
    _authentication_methods = [
        "login",
    ]
    create_storage_dir = True

    def __init__(self, options: DownloadOptions) -> None:
        super().__init__(options)
        self._download_counter: int = 0
        self.database_directory_books = os.path.join(self.database_directory, "books")
        self.database_directory_playback_metadata = os.path.join(
            self.database_directory, "playback-metadata"
        )
        self.database_directory_lists = os.path.join(self.database_directory, "lists")
        if self.skip_downloaded or self._debug_dump_enabled():
            os.makedirs(self.database_directory_books, exist_ok=True)
        if self._debug_dump_enabled():
            os.makedirs(self.database_directory_playback_metadata, exist_ok=True)
            os.makedirs(self.database_directory_lists, exist_ok=True)

    def _debug_dump_enabled(self) -> bool:
        """Return True when opt-in debug mode enables raw JSON payload persistence."""
        return bool(getattr(self._options, "debug", False)) or logging.is_debug_mode()

    @property
    def media_type(self) -> str:
        raw = getattr(self._options, "media_type", None) or "auto"
        normalized = str(raw).strip().lower()
        if normalized in ("auto", "audiobook", "ebook", "both"):
            return normalized
        return "auto"

    def _get_book_path(self, consumableId: str, format_type: str = "abook") -> str:
        suffix = "_ebook" if format_type == "ebook" else ""
        return os.path.join(self.database_directory_books, f"{consumableId}{suffix}.json")

    def _get_playback_metadata_path(self, consumableId: str) -> str:
        return os.path.join(
            self.database_directory_playback_metadata, f"{consumableId}.json"
        )

    def _get_lists_path(self, list_name: str, languages: str, formats: str) -> str:
        return os.path.join(
            self.database_directory_lists, f"{list_name}_{languages}_{formats}.json"
        )

    def _skip_download_check(self, book_id: str, format_type: str | None = None) -> bool:
        if not self.skip_downloaded:
            return False
        if format_type is not None:
            return os.path.exists(self._get_book_path(book_id, format_type))
        if self.media_type == "ebook":
            return os.path.exists(self._get_book_path(book_id, "ebook"))
        return os.path.exists(self._get_book_path(book_id, "abook"))

    @staticmethod
    def encrypt_password(password: str) -> str:
        """
        Encrypt password with predefined keys.
        This encrypted password is used for login.

        :param password: User defined password
        :returns: Encrypted password
        """
        # Thanks to https://github.com/javsanpar/storytel-tui
        key = b"VQZBJ6TD8M9WBUWT"
        iv = b"joiwef08u23j341a"
        msg = pad(password.encode(), AES.block_size)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        cipher_text = cipher.encrypt(msg)
        return cipher_text.hex()

    def check_cloudflare_blocked(self, response: Response) -> None:
        if response.status_code == 403:
            error_str = "<title>Attention Required! | Cloudflare</title>"
            if error_str in response.text:
                raise CloudflareBlocked

    def _login(self, url: str, username: str, password: str) -> None:
        self._url = url
        self._username = username
        self._password = self.encrypt_password(password)
        self._session.headers.update(
            {
                "User-Agent": "Storytel/24.22 (Android 14; Google Pixel 8 Pro) Release/2288629",
            }
        )
        self._do_login()

    def _do_login(self) -> None:
        # Generate a new UUID for each request
        generated_device_id = str(uuid.uuid4())

        resp = self.request(
            "POST",
            f"https://www.storytel.com/api/login.action?m=1&token=guestsv&userid=-1&version=24.22"
            f"&terminal=android&locale=sv&deviceId={generated_device_id}&kidsMode=false",

            data={
                "uid": self._username,
                "pwd": self._password,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code != 200:
            if resp.status_code == 403:
                self.check_cloudflare_blocked(resp)
            raise UserNotAuthorized(
                detail=f"Storytel login failed (HTTP {resp.status_code})"
            )

        try:
            user_data = resp.json()
        except ValueError as exc:
            raise UserNotAuthorized(detail="Storytel login returned invalid JSON") from exc
        account_info = user_data.get("accountInfo") if isinstance(user_data, dict) else None
        jwt = account_info.get("jwt") if isinstance(account_info, dict) else None
        if not jwt:
            raise UserNotAuthorized(detail="Storytel login response missing session token")
        self._language = account_info.get("lang") or "en"
        self._session.headers.update({"authorization": f"Bearer {jwt}"})

    def _relogin_check(self) -> None:
        """
        There's a ratelimit for the MP3 download, if triggered, it will invalidate all sessions and you'll get an email about suspicios activities on your account
        To avoid the rate limtier we regularly re-login to get a new session token (which seems to byepass the rate limiter)
        """
        if self._download_counter > 0 and self._download_counter % 10 == 0:
            logging.debug("refreshing login")
            self._do_login()

    @staticmethod
    def _clean_share_url(url: str) -> str:
        """remove query string/fragment from url"""
        return url.split("?")[0]

    def download_from_id(self, book_id: str) -> Result:
        self._relogin_check()
        return self.download_book_from_book_id(book_id)

    def download(self, url: str) -> Result:
        self._relogin_check()

        # Anchor to the final path segment so a catalog URL whose slug merely
        # contains "bookshelf"/"want-to-read" is not misrouted here.
        if re.search(r"/(?:want-to-read|bookshelf)/?$", url.split("?")[0].split("#")[0]):
            return self.download_bookshelf_wtr()

        if m := re.match(self.match[0], url):
            language, language2, list_type = m.groups()
            effective_lang = language or getattr(self, "_language", None) or "en"
            logging.debug(f"download: {url=}, {list_type=}, {language=}, {language2=}")
            # individual books
            if list_type == "books":
                return self.download_book_from_url(url)
            # use API when possible
            elif list_type in ("series", "authors", "narrators"):
                return self.download_lists_api(url, list_type, effective_lang)
            # some lists are not avaialble via the API, use website scrapting
            else:
                return self.download_books_from_website(url)
        raise BookNotFound

    def _item_selected_formats(self, formats: Sequence[dict[str, Any]]) -> list[str]:
        has_abook = any(f.get("type") == "abook" and f.get("isReleased") for f in formats)
        has_ebook = any(f.get("type") == "ebook" and f.get("isReleased") for f in formats)
        mode = self.media_type
        if mode == "audiobook":
            return ["abook"] if has_abook else []
        if mode == "ebook":
            return ["ebook"] if has_ebook else []
        if mode == "both":
            selected: list[str] = []
            if has_abook:
                selected.append("abook")
            if has_ebook:
                selected.append("ebook")
            return selected
        if has_abook:
            return ["abook"]
        if has_ebook:
            return ["ebook"]
        return []

    def _should_include_item(self, book_id: str, formats: Sequence[dict[str, Any]]) -> bool:
        selected = self._item_selected_formats(formats)
        if not selected:
            return False
        return any(not self._skip_download_check(book_id, fmt) for fmt in selected)

    def download_lists_api(
        self,
        url: str,
        list_type: str,
        language: str,
    ) -> Series[str]:
        list_id: str = self.get_id_from_url(url)
        list_details = self.download_list_books(list_id, list_type, language)

        books: list[BookId[str] | Audiobook] = []
        for item in list_details["items"]:
            if self._should_include_item(item["id"], item.get("formats", [])):
                books.append(BookId(item["id"]))

        return Series(
            title=list_details["title"],
            books=books,
        )

    @staticmethod
    def _iter_bookshelf_models(data: Any) -> list[dict[str, Any]]:
        """Extract normalized book model dicts from Storytel bookshelf payloads."""
        if isinstance(data, list):
            entries: Any = data
        elif isinstance(data, dict):
            raw_items = data.get("items")
            if raw_items is None:
                raw_items = data.get("books") or data.get("consumables") or []
            if isinstance(raw_items, dict):
                entries = raw_items.values()
            elif isinstance(raw_items, list):
                entries = raw_items
            else:
                entries = ()
        else:
            entries = ()
        models: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            model = item.get("model") or item.get("book") or item.get("consumable") or item
            if isinstance(model, dict):
                models.append(model)
        return models

    def download_bookshelf_wtr(self) -> Series[str]:
        """
        Download books in the user's bookshelf marked as "want to read"
        (Storytel state ``WILL_CONSUME``) matching the configured ``media_type``.
        """
        data = self.download_bookshelf()
        books: list[BookId[str] | Audiobook] = []
        for model in self._iter_bookshelf_models(data):
            book_id = model.get("id") or model.get("consumableId")
            if not book_id or model.get("state") != "WILL_CONSUME":
                continue
            if self._should_include_item(book_id, model.get("formats", [])):
                books.append(BookId(book_id))
        return Series(
            title="Storytel - Vill läsa",
            books=books,
        )

    @staticmethod
    def _validate_single_format(
        candidates: list[dict[str, Any]],
        format_type: str,
        *,
        require_released: bool = True,
    ) -> dict[str, Any]:
        if len(candidates) != 1:
            raise GenericAudiobookDLException(
                f"multiple {format_type} formats",
                f"found multiple {format_type} formats, please report this book for bugfixing",
            )
        fmt = candidates[0]
        if require_released and not fmt.get("isReleased", True):
            raise BookNotReleased
        return fmt

    def _select_format_types(self, book_details: dict[str, Any]) -> list[str]:
        if "formats" not in book_details:
            raise DataNotPresent
        formats = book_details["formats"]
        abook_formats = [f for f in formats if f.get("type") == "abook"]
        ebook_formats = [f for f in formats if f.get("type") == "ebook"]
        mode = self.media_type

        if mode == "audiobook":
            if not abook_formats:
                raise BookHasNoAudiobook
            self._validate_single_format(abook_formats, "abook")
            return ["abook"]

        if mode == "ebook":
            if not ebook_formats:
                raise BookHasNoEbook
            self._validate_single_format(ebook_formats, "ebook")
            return ["ebook"]

        if mode == "both":
            selected: list[str] = []
            if abook_formats:
                fmt = self._validate_single_format(abook_formats, "abook", require_released=False)
                if fmt.get("isReleased", True):
                    selected.append("abook")
            if ebook_formats:
                fmt = self._validate_single_format(ebook_formats, "ebook", require_released=False)
                if fmt.get("isReleased", True):
                    selected.append("ebook")
            if not selected:
                if abook_formats or ebook_formats:
                    raise BookNotReleased
                raise BookHasNoAudiobook
            return selected

        # mode == "auto": prefer audiobook when present, fall back to ebook
        if abook_formats:
            self._validate_single_format(abook_formats, "abook")
            return ["abook"]
        if ebook_formats:
            self._validate_single_format(ebook_formats, "ebook")
            return ["ebook"]
        raise BookHasNoAudiobook

    def _build_book_for_format(
        self,
        consumableId: str,
        book_details: dict[str, Any],
        format_type: str,
        *,
        print_only: bool,
        cover_only: bool,
        no_chapters: bool,
        cover: Cover | None,
    ) -> Audiobook:
        metadata = self.get_metadata(book_details, format_type=format_type)
        files = [] if (print_only or cover_only) else self.get_files(book_details, format_type=format_type)
        chapters = (
            []
            if (print_only or cover_only or no_chapters or format_type != "abook")
            else self.get_chapters(book_details)
        )
        source_data = dict(book_details)
        source_data["_format_type"] = format_type
        self._update_metadata(consumableId, source_data, metadata, files)
        return Audiobook(
            session=self._session,
            files=files,
            metadata=metadata,
            cover=cover,
            chapters=chapters,
            source_data=source_data,
        )

    def download_book_from_book_id(
        self,
        consumableId: str,
    ) -> Result:
        book_details = self.download_book_details(consumableId)
        selected_formats = self._select_format_types(book_details)
        if len(selected_formats) > 1 and self.skip_downloaded:
            remaining = [
                fmt for fmt in selected_formats
                if not self._skip_download_check(consumableId, fmt)
            ]
            if remaining:
                selected_formats = remaining

        options = getattr(self, "_options", None)
        print_only = bool(options and getattr(options, "print_output", False))
        cover_only = bool(options and getattr(options, "cover", False))
        no_chapters = bool(options and getattr(options, "no_chapters", False))
        cover = None if print_only else self.download_cover(book_details)

        built_books: list[Audiobook] = []
        for idx, fmt in enumerate(selected_formats):
            if idx > 0:
                self._relogin_check()
            built_books.append(
                self._build_book_for_format(
                    consumableId,
                    book_details,
                    fmt,
                    print_only=print_only,
                    cover_only=cover_only,
                    no_chapters=no_chapters,
                    cover=cover,
                )
            )
        if len(built_books) == 1:
            return built_books[0]
        return Series(title=built_books[0].title, books=built_books)

    def download_book_from_url(self, url: str) -> Result:
        consumableId = self.get_id_from_url(url)
        return self.download_book_from_book_id(consumableId)

    @staticmethod
    def get_id_from_url(url: str) -> str:
        """
        Find book id in url

        :param url: Url to book
        :returns: Id of book from url
        """
        parsed = parse_url(url)
        if parsed.path is None:
            raise DataNotPresent
        segment = parsed.path.rstrip("/").split("/")[-1]
        if not segment:
            raise DataNotPresent
        return segment.split("-")[-1]

    @staticmethod
    def _update_metadata(
        consumableId: str,
        book_details: dict[str, Any],
        metadata: AudiobookMetadata,
        files: list[AudiobookFile],
    ) -> None:
        """
        update metadata once all data is available
        """
        # The ISBN is only available from the download link
        if files:
            parsed = parse_url(files[0].url)
            q = parse_qs(parsed.query)
            if "isbn" in q:
                isbn = q["isbn"][0]
                book_details["_download_url_isbn"] = isbn
                metadata.isbn = isbn
        if consumableId in metadata_corrections["books"]:
            corrections = metadata_corrections["books"][consumableId]
            for key, value in corrections.items():
                logging.log(
                    f"overriding metadata [yellow]{key}[/] from [blue]{getattr(metadata, key)}[/] to [magenta]{value}[/]"
                )
                setattr(metadata, key, value)

    def download_bookshelf(self) -> dict[str, Any]:
        """Download full bookshelf snapshot from Storytel's delta-sync endpoint."""
        # An empty body requests the full snapshot; Storytel rejects {"items": []} with HTTP 400
        # because `items` expects a map keyed by consumable id.
        data: dict[str, Any] = self.post_json(
            "https://api.storytel.net/libraries/bookshelf",
            data="",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
        )

        if self._debug_dump_enabled():
            bookshelf_path = os.path.join(self.database_directory_lists, "bookshelf.json")
            self._write_json(bookshelf_path, data)

        return data

    def download_books_from_website(self, url: str) -> Series[str]:
        """Download series details from a Storytel web page."""
        title = self.find_elems_in_page(url, "h1")[-1].text
        items = self.find_elems_in_page(url, 'a[href*="/books/"]')
        books: list[BookId[str] | Audiobook] = []
        seen_ids: set[str] = set()
        audiobook_only = self.media_type == "audiobook"
        for item in items:
            href: str | None = item.get("href")
            if not href:
                continue
            if audiobook_only:
                svg_headphone_element = item.cssselect(
                    f"svg > path[d='{svg_headphone_path}']"
                )
                if len(svg_headphone_element) == 0:
                    logging.debug(f"skipping {href} (has no audiobook)")
                    continue

            consumableId = self.get_id_from_url(href)
            if consumableId in seen_ids:
                continue
            seen_ids.add(consumableId)
            if self.media_type == "both":
                already_downloaded = (
                    self._skip_download_check(consumableId, "abook")
                    and self._skip_download_check(consumableId, "ebook")
                )
            else:
                already_downloaded = self._skip_download_check(consumableId)
            if not already_downloaded:
                books.append(BookId(consumableId))

        return Series(
            title=title,
            books=books,
        )

    def _default_list_formats(self) -> str:
        mode = self.media_type
        if mode == "audiobook":
            return "abook"
        if mode == "ebook":
            return "ebook"
        return "abook,ebook"

    def download_list_books(
        self,
        list_id: str,
        list_type: str,
        languages: str,
        formats: str | None = None,
    ) -> dict[str, Any]:
        """Download details about book list

        :param formats: comma separated list of formats (abook,ebook,podcast)
        :param languages: comma separated list of languages (en,de,tr,ar,ru,pl,it,es,sv,fr,nl)
        """
        resolved_formats = formats or self._default_list_formats()
        result: dict[str, Any] | None = None
        next_page_token: str | None = None
        first_page = True

        while first_page or next_page_token is not None:
            params: dict[str, str] = {
                "includeListDetails": "true",  # include listMetadata,filterOptions,sortOption sections
                "includeFormats": resolved_formats,
                "includeLanguages": languages,
                "kidsMode": "false",
            }
            if next_page_token is not None:
                params["nextPageToken"] = next_page_token

            data = self.get_json(
                f"https://api.storytel.net/explore/lists/{list_type}/{list_id}",
                params=params,
            )
            if first_page or result is None:
                result = data
                result.setdefault("items", [])
                first_page = False
            else:
                result["items"].extend(data.get("items", []))
                result["nextPageToken"] = data.get("nextPageToken")
            next_page_token = data.get("nextPageToken")

        assert result is not None
        if self._debug_dump_enabled():
            lists_path = self._get_lists_path(result["id"], languages, resolved_formats)
            self._write_json(lists_path, result)

        return result

    def download_book_details(self, consumableId: str) -> dict[str, Any]:
        """Download books details"""
        resp = self.request(
            "GET",
            f"https://api.storytel.net/book-details/consumables/{consumableId}?kidsMode=false&configVariant=default",
        )
        if resp.status_code == 404:
            raise BookNotFound
        if resp.status_code in (401, 403):
            raise UserNotAuthorized
        if resp.status_code != 200:
            raise RequestError
        data = resp.json()
        return data

    def _get_asset_url(self, consumableId: str, asset_type: str) -> str:
        """Get a final asset URL by requesting the assets API and returning the redirect location."""
        resp = self.request(
            "GET",
            f"https://api.storytel.net/assets/v2/consumables/{consumableId}/{asset_type}",
            allow_redirects=False,
        )
        self._download_counter += 1
        if resp.status_code != 302 or "Location" not in resp.headers:
            raise GenericAudiobookDLException(
                f"request to {resp.url} failed, got {resp.status_code} response: {resp.text}"
            )
        location: str = resp.headers["Location"]
        return location

    def get_audio_url(self, consumableId: str) -> str:
        """Get the final Audio URL from Storytel's assets API."""
        return self._get_asset_url(consumableId, "abook")

    def get_ebook_url(self, consumableId: str) -> str:
        """Get the final Ebook URL from Storytel's assets API."""
        return self._get_asset_url(consumableId, "ebook")

    @staticmethod
    def _infer_ebook_extension(ebook_url: str) -> str:
        path = (parse_url(ebook_url).path or "").lower()
        if path.endswith(".pdf"):
            return "pdf"
        return "epub"

    def get_files(
        self,
        book_info: dict[str, Any],
        format_type: str = "abook",
    ) -> list[AudiobookFile]:
        consumableId = book_info["consumableId"]
        if format_type == "ebook":
            ebook_url = self.get_ebook_url(consumableId)
            return [
                AudiobookFile(
                    url=ebook_url,
                    headers=dict(self._session.headers),
                    ext=self._infer_ebook_extension(ebook_url),
                    expected_status_code=200,
                )
            ]
        audio_url = self.get_audio_url(consumableId)
        return [
            AudiobookFile(
                url=audio_url,
                headers=dict(self._session.headers),
                ext="mp3",
                expected_status_code=200,
                expected_content_type="audio/mpeg",
            )
        ]

    def get_metadata(
        self,
        book_details: dict[str, Any],
        format_type: str | None = None,
    ) -> AudiobookMetadata:
        if "formats" not in book_details:
            raise DataNotPresent
        if format_type is None:
            format_type = self._select_format_types(book_details)[0]
            candidates = [f for f in book_details["formats"] if f.get("type") == format_type]
            format_info = candidates[0]
        else:
            candidates = [f for f in book_details["formats"] if f.get("type") == format_type]
            if not candidates:
                raise BookHasNoEbook if format_type == "ebook" else BookHasNoAudiobook
            format_info = self._validate_single_format(candidates, format_type)

        title = book_details["title"]
        metadata = AudiobookMetadata(title)
        metadata.add_genre("Ebook" if format_type == "ebook" else "Audiobook")
        if book_details.get("shareUrl"):
            metadata.scrape_url = self._clean_share_url(book_details["shareUrl"])
            logging.debug(f"URL {metadata.scrape_url}")
        for author in book_details.get("authors", []):
            metadata.add_author(author["name"])
        if format_type == "abook":
            for narrator in book_details.get("narrators", []):
                metadata.add_narrator(narrator["name"])
        if book_details.get("isbn"):
            metadata.isbn = book_details["isbn"]
        if format_info.get("isbn") and (format_type == "ebook" or not metadata.isbn):
            metadata.isbn = format_info["isbn"]
        if "description" in book_details:
            metadata.description = book_details["description"]
        if book_details.get("language"):
            metadata.language = pycountry.languages.get(
                alpha_2=book_details["language"]
            )
        category = book_details.get("category")
        if isinstance(category, dict) and "name" in category:
            metadata.add_genre(category["name"])
        if "seriesInfo" in book_details and book_details["seriesInfo"]:
            metadata.series = book_details["seriesInfo"]["name"]
            if "orderInSeries" in book_details["seriesInfo"]:
                metadata.series_order = book_details["seriesInfo"]["orderInSeries"]

        publisher = format_info.get("publisher")
        if isinstance(publisher, dict) and "name" in publisher:
            metadata.publisher = publisher["name"]
        if format_info.get("releaseDate"):
            date_str: str = format_info["releaseDate"]
            try:
                metadata.release_date = datetime.strptime(
                    date_str, "%Y-%m-%dT%H:%M:%SZ"
                ).date()
            except ValueError:
                metadata.release_date = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                ).date()
        return metadata

    def download_audiobook_info(self, book_details) -> dict[str, Any]:
        """Download information about the audiobook files"""
        consumableId = book_details["consumableId"]
        url = f"https://api.storytel.net/playback-metadata/consumable/{consumableId}"
        playback_metadata = self.get_json(url)
        if self._debug_dump_enabled():
            playback_metadata_path = self._get_playback_metadata_path(consumableId)
            self._write_json(playback_metadata_path, playback_metadata)
        if not "formats" in playback_metadata:
            raise DataNotPresent
        for format in playback_metadata["formats"]:
            if format["type"] == "abook":
                return format
        raise DataNotPresent

    def get_chapters(self, book_details) -> list[Chapter]:
        chapters: list[Chapter] = []
        book_title = book_details["title"]
        file_metadata = self.download_audiobook_info(book_details)
        if not "chapters" in file_metadata:
            return []
        start_time = 0
        for chapter in file_metadata["chapters"]:
            if "title" in chapter and chapter["title"] is not None:
                title = chapter["title"]
                # remove book title prefix from chapter title
                if len(title) > len(book_title) and title.startswith(book_title):
                    title = title[len(book_title) :].strip(" -")
            else:
                title = f"Chapter {chapter['number']}"
            chapters.append(Chapter(start_time, title))
            start_time += chapter["durationInMilliseconds"]
        return chapters

    def download_cover(self, book_details) -> Cover | None:
        cover_info = book_details.get("cover")
        cover_url = cover_info.get("url") if isinstance(cover_info, dict) else None
        if not cover_url:
            return None
        # cover_url = f"https://www.storytel.com/images/{isbn}/640x640/cover.jpg"
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    @staticmethod
    def _write_json(path: str, payload: Any) -> None:
        """Write `payload` as UTF-8 JSON to `path`."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as json_file:
            json.dump(payload, json_file, indent=2, ensure_ascii=False)

    def on_download_complete(self, audiobook: Audiobook) -> None:
        if not (self._debug_dump_enabled() or self.skip_downloaded):
            return
        consumableId = audiobook.source_data["consumableId"]
        raw_format = audiobook.source_data.get("_format_type")
        if raw_format in ("abook", "ebook"):
            format_type = raw_format
        else:
            format_type = "ebook" if is_ebook(audiobook) else "abook"
        book_path = self._get_book_path(consumableId, format_type)
        self._write_json(book_path, audiobook.source_data)

    supports_library = True
    supports_search = True

    def _book_url(self, book_id: str, model: dict[str, Any] | None = None) -> str:
        """Build a routable Storytel book URL using `shareUrl` or a locale-prefixed fallback."""
        if isinstance(model, dict):
            share_url = model.get("shareUrl")
            if isinstance(share_url, str) and share_url.strip():
                cleaned = self._clean_share_url(share_url.strip())
                if re.match(self.match[0], cleaned):
                    return cleaned
            raw_lang = model.get("language")
        else:
            raw_lang = None
        model_lang = (
            raw_lang.strip()
            if isinstance(raw_lang, str) and re.fullmatch(r"\w+", raw_lang.strip())
            else None
        )
        lang = getattr(self, "_language", None) or model_lang or "en"
        return f"https://www.storytel.com/{lang}/books/{book_id}"

    @staticmethod
    def _normalize_cover_url(raw_url: Any) -> str | None:
        if not isinstance(raw_url, str):
            return None
        cleaned = raw_url.strip()
        if not cleaned:
            return None
        if cleaned.startswith("//"):
            return f"https:{cleaned}"
        if cleaned.startswith("/"):
            return f"https://www.storytel.com{cleaned}"
        return cleaned

    @classmethod
    def _extract_formats_and_cover(
        cls,
        model: dict[str, Any],
    ) -> tuple[tuple[str, ...], str | None]:
        """Extract available media formats (`audiobook`, `ebook`) and cover URL from `model`."""
        found_formats: set[str] = set()
        preferred_cover: str | None = None
        fallback_cover: str | None = None
        formats_raw = model.get("formats")
        if isinstance(formats_raw, list):
            for fmt in formats_raw:
                if not isinstance(fmt, dict):
                    continue
                if fmt.get("isReleased") is False:
                    continue
                ftype = str(fmt.get("type") or "").strip().lower()
                if ftype == "abook":
                    found_formats.add("audiobook")
                elif ftype == "ebook":
                    found_formats.add("ebook")
                cov_obj = fmt.get("cover")
                cov_url = cls._normalize_cover_url(
                    cov_obj.get("url") if isinstance(cov_obj, dict) else fmt.get("coverUrl")
                )
                if cov_url:
                    if ftype == "abook" and preferred_cover is None:
                        preferred_cover = cov_url
                    elif fallback_cover is None:
                        fallback_cover = cov_url

        if isinstance(model.get("abook"), dict) or model.get("abookExists") or model.get("hasAbook"):
            found_formats.add("audiobook")
        if isinstance(model.get("ebook"), dict) or model.get("ebookExists") or model.get("hasEbook"):
            found_formats.add("ebook")

        if preferred_cover is None and fallback_cover is None:
            model_cover = model.get("cover")
            fallback_cover = cls._normalize_cover_url(
                model_cover.get("url")
                if isinstance(model_cover, dict)
                else (
                    model.get("largeCover")
                    or model.get("largeCoverE")
                    or model.get("coverUrl")
                    or model.get("imageUrl")
                )
            )
        ordered = tuple(f for f in ("audiobook", "ebook") if f in found_formats)
        return ordered, (preferred_cover or fallback_cover)

    def _catalog_book_from_model(
        self,
        model: dict[str, Any],
        *,
        require_title: bool = False,
    ) -> CatalogBook | None:
        book_id = model.get("id") or model.get("consumableId")
        if not book_id:
            return None
        raw_title = str(model.get("name") or model.get("title") or "").strip()
        if require_title and not raw_title:
            return None
        title = raw_title or str(book_id)
        authors_raw = model.get("authors") or []
        author_names = [
            str(a.get("name") or "").strip()
            for a in authors_raw
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        series_name: str | None = None
        series_order: int | None = None
        series_info = model.get("seriesInfo") or model.get("series")
        if isinstance(series_info, dict):
            s_name = str(series_info.get("name") or series_info.get("title") or "").strip()
            if s_name:
                series_name = s_name
            raw_order = series_info.get("orderInSeries")
            if isinstance(raw_order, int) and not isinstance(raw_order, bool):
                series_order = raw_order
        formats, cover_url = self._extract_formats_and_cover(model)
        return CatalogBook(
            title=title,
            url=self._book_url(str(book_id), model),
            author=", ".join(author_names),
            series=series_name,
            series_order=series_order,
            source="storytel",
            cover_url=cover_url,
            formats=formats,
        )

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        data = self.get_json(
            f"https://api.storytel.net/search/client/search?query={quote_plus(cleaned)}&kidsMode=false"
        )
        raw_items = (
            data.get("books")
            or data.get("consumables")
            or data.get("items")
            or data.get("results")
            or []
        )
        results: list[CatalogBook] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            model = entry.get("book") or entry.get("consumable") or entry
            if not isinstance(model, dict):
                continue
            item = self._catalog_book_from_model(model, require_title=True)
            if item is not None:
                results.append(item)
        return results

    def list_library(self) -> list[CatalogBook]:
        data = self.download_bookshelf()
        results: list[CatalogBook] = []
        for model in self._iter_bookshelf_models(data):
            item = self._catalog_book_from_model(model, require_title=False)
            if item is not None:
                results.append(item)
        return results

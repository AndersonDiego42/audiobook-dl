from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover, logging, utils
from audiobookdl.exceptions import BookHasNoAudiobook, DataNotPresent, RequestError, UserNotAuthorized
from audiobookdl.utils.audiobook import parse_duration_ms, parse_language

import re
from typing import Any
from urllib.parse import urlparse
import requests


class EreolenSource(Source):
    _authentication_methods = [
        "cookies",
        "login",
    ]
    names = ["eReolen"]
    login_data = ["username", "password"]
    supports_library = True
    library_domains = [
        line.strip()
        for line in utils.read_asset_file("assets/sources/ereolen/libraries.txt").splitlines()
        if line.strip()
    ]
    @classmethod
    def _build_match_patterns(cls, domains: list[str]) -> list[str]:
        return [
            rf"https://(www\.)?({'|'.join(re.escape(d) for d in domains)})/work/work-of:.+",
            r"https://(www\.)?ereolen\.dk/work/work-of:.+",
        ]

    match = [
        rf"https://(www\.)?({'|'.join(re.escape(d) for d in library_domains)})/work/work-of:.+",
        r"https://(www\.)?ereolen\.dk/work/work-of:.+",
    ]

    @classmethod
    def refresh_library_domains(
        cls,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> list[str]:
        """
        Fetch current Danish library domains from eReolen and update `library_domains` and `match`.

        Falls back to existing static domains if the registry page cannot be reached.
        """
        http_client = session or requests
        try:
            resp = http_client.get("https://ereolen.dk/libraries", timeout=timeout)
        except requests.RequestException:
            return list(cls.library_domains)
        if getattr(resp, "status_code", 0) != 200:
            return list(cls.library_domains)
        html = getattr(resp, "text", None) or (
            resp.content.decode("utf-8", errors="replace") if getattr(resp, "content", None) else ""
        )
        discovered = re.findall(
            r"https?://(?:www\.)?([a-z0-9.-]+\.(?:dk|fo|gl))(?:/|\"|')",
            html,
            flags=re.IGNORECASE,
        )
        cleaned: list[str] = []
        seen = set(cls.library_domains)
        cleaned.extend(cls.library_domains)
        for domain in discovered:
            host = domain.lower().removeprefix("www.")
            if host and host not in seen and "ereolen.dk" not in host and "dbc.dk" not in host:
                seen.add(host)
                cleaned.append(host)
        cls.library_domains = cleaned
        cls.match = cls._build_match_patterns(cls.library_domains)
        return list(cls.library_domains)

    def _login(self, url: str, username: str, password: str):
        hostname = urlparse(url).hostname or "ereolen.dk"
        login_page_url = f"https://{hostname}/login"
        login_path = self.find_elem_in_page(login_page_url, "#borchk-login-form", "action")
        library_id = self.find_elem_in_page(login_page_url, "#libraryid-input", "value")
        library_name = self.find_elem_in_page(login_page_url, "#libraryname-input", "value")
        logging.debug(f"{library_name=} {library_id=}")
        self.post(
            f"https://login.bib.dk{login_path}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "libraryName": library_name,
                "agency": library_id,
                "loginBibDkUserId": username,
                "pincode": password,
            },
            allow_redirects=True,
            timeout=20.0,
        )
        user_token = self.find_in_page(
            f"https://{hostname}/dpl-react/user-tokens",
            r'"user",\s*"([^"]+)"',
            1,
        )
        self._session.headers.update(
            {
                "Authorization": f"Bearer {user_token}",
            }
        )
        logging.debug(f"user_token={logging.mask_secret(user_token)}")

    def verify_auth(self, url: str = "") -> bool:
        if not self._session.cookies and not self.authenticated and "Authorization" not in self._session.headers:
            return False
        try:
            payload = self.get_json("https://pubhub-openplatform.dbc.dk/v1/user/loans?")
            return isinstance(payload, dict) and isinstance(payload.get("loans"), list)
        except (UserNotAuthorized, RequestError, ValueError):
            return False

    @staticmethod
    def _extract_isbn(manifestation: dict[str, Any]) -> str | None:
        for ident in manifestation.get("identifiers") or []:
            if not isinstance(ident, dict):
                continue
            val = str(ident.get("value") or "").strip()
            ident_type = str(ident.get("type") or "").upper()
            if val and ("ISBN" in ident_type or re.fullmatch(r"(?:97[89])?\d{9}[\dXx]", val.replace("-", ""))):
                return val
        return None

    @staticmethod
    def _extract_chapters(manifestation: dict[str, Any]) -> list[Chapter]:
        contents = manifestation.get("contents") or {}
        entries = contents.get("entries") or [] if isinstance(contents, dict) else []
        chapters: list[Chapter] = []
        offset_ms = 0
        for idx, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            title_obj = entry.get("title")
            title = (
                title_obj.get("display")
                if isinstance(title_obj, dict)
                else str(title_obj or "")
            )
            ch_title = str(title or f"Chapter {idx}").strip()
            chapters.append(Chapter(start=offset_ms, title=ch_title))
            dur_ms = parse_duration_ms(entry.get("playingTime"))
            if dur_ms:
                offset_ms += dur_ms
        return chapters

    @staticmethod
    def _build_metadata(
        work_meta: dict[str, Any],
        audiobook_manifestation: dict[str, Any],
        url: str,
    ) -> AudiobookMetadata:
        title = work_meta["titles"]["full"][0]
        authors = [
            author["display"]
            for author in (work_meta.get("creators") or [])
            if isinstance(author, dict) and author.get("display")
        ]

        publisher = audiobook_manifestation.get("publisher")
        if isinstance(publisher, list):
            publisher = ", ".join(str(p).strip() for p in publisher if p) or None
        elif isinstance(publisher, str):
            publisher = publisher.strip() or None
        else:
            publisher = None

        isbn = EreolenSource._extract_isbn(audiobook_manifestation)
        description = (work_meta.get("abstract") or "").strip() or None

        series_name: str | None = None
        series_order: int | None = None
        raw_series = work_meta.get("series") or []
        if isinstance(raw_series, list) and raw_series:
            first_series = raw_series[0]
            if isinstance(first_series, dict):
                series_name = str(first_series.get("title") or "").strip() or None
                wid = work_meta.get("workId")
                for member in first_series.get("members") or []:
                    if not isinstance(member, dict):
                        continue
                    member_work = member.get("work") or {}
                    if wid and member_work.get("workId") == wid and member.get("numberInSeries") is not None:
                        try:
                            series_order = int(float(member["numberInSeries"]))
                        except (TypeError, ValueError):
                            series_order = None
                        break

        genres = [
            str(g).strip()
            for g in (work_meta.get("genreAndForm") or [])
            if isinstance(g, str) and g.strip()
        ]
        language = None
        langs = work_meta.get("mainLanguages") or []
        if isinstance(langs, list) and langs and isinstance(langs[0], dict):
            language = parse_language(langs[0].get("isoCode")) or parse_language(
                langs[0].get("display")
            )

        return AudiobookMetadata(
            title=title,
            authors=authors,
            description=description,
            series=series_name,
            series_order=series_order,
            isbn=isbn,
            publisher=publisher,
            genres=genres,
            language=language,
            source="ereolen",
            scrape_url=url,
        )

    def get_cover(
        self,
        work_meta: dict[str, Any],
        page_url: str,
        loan: dict[str, Any] | None = None,
    ) -> Cover | None:
        """Extract cover image URL from GraphQL metadata, loan payload, or cached page og:image."""
        cover_url: str | None = None
        cover_obj = work_meta.get("cover")
        if isinstance(cover_obj, dict):
            for key in ("detail", "large", "original", "thumbnail", "url"):
                val = cover_obj.get(key)
                if isinstance(val, dict) and val.get("url"):
                    cover_url = str(val["url"])
                    break
                if isinstance(val, str) and val.strip():
                    cover_url = val.strip()
                    break
        elif isinstance(cover_obj, str) and cover_obj.strip():
            cover_url = cover_obj.strip()

        if not cover_url and isinstance(loan, dict):
            lib_book = loan.get("libraryBook")
            if isinstance(lib_book, dict):
                cover_url = lib_book.get("coverUrl") or lib_book.get("cover")

        if not cover_url:
            page_html = self._get_page(page_url).decode("utf-8", errors="replace")
            og_match = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                page_html,
                flags=re.IGNORECASE,
            )
            if og_match:
                cover_url = og_match.group(1).strip()

        if not cover_url:
            return None
        return self.fetch_cover_safe(cover_url)

    def download(self, url: str) -> Audiobook:
        work_id = urlparse(url).path.split("/")[-1]
        logging.debug(f"{work_id=}")

        # Extract api path
        api_path_segment = self.find_in_page(
            url,
            'data-fbi-base-url="https://fbi-api.dbc.dk/([^/]+)/graphql"',
            1,
        )
        logging.debug(f"{api_path_segment=}")

        # Fetch metadata
        metadata = self.post_json(
            f"https://fbi-api.dbc.dk/{api_path_segment}/graphql",
            json={
                "query": utils.read_asset_file("assets/sources/ereolen/metadata_query.graphql"),
                "variables": {
                    "wid": work_id
                },
            },
        )
        if metadata is None:
            raise RequestError
        metadata = metadata["data"]["work"]

        # Extract book id and matching audiobook manifestation
        book_id: str | None = None
        audiobook_manifestation: dict[str, Any] = {}
        for manifestation in metadata["manifestations"]["all"]:
            if manifestation["materialTypes"][0]["materialTypeSpecific"]["display"] == "lydbog (online)":
                book_id = manifestation["identifiers"][0]["value"]
                audiobook_manifestation = manifestation
        if book_id is None:
            raise BookHasNoAudiobook
        logging.debug(f"{book_id=}")

        # Fetch loans
        loans = self.get_json("https://pubhub-openplatform.dbc.dk/v1/user/loans?")
        logging.debug(f"{loans=}")
        order_id: str | None = None
        matched_loan: dict[str, Any] | None = None
        for loan in loans["loans"]:
            if loan["libraryBook"]["identifier"] == book_id:
                order_id = loan["orderId"]
                matched_loan = loan

        if order_id is None:
            raise DataNotPresent(
                message="Not currently loaned on eReolen. Borrow this audiobook on your library website before downloading."
            )

        return Audiobook(
            session=self.create_unauthenticated_session(),
            files=self.get_files(order_id),
            metadata=self._build_metadata(metadata, audiobook_manifestation, url),
            cover=self.get_cover(metadata, url, matched_loan),
            chapters=self._extract_chapters(audiobook_manifestation),
        )

    def get_files(self, order_id: str) -> list[AudiobookFile]:
        return self.get_stream_files(
            f"https://audio.api.streaming.pubhub.dk/v1/stream/hls/{order_id}/playlist.m3u8",
            extension="ts",
        )

    def list_library(self) -> list[CatalogBook]:
        """Return currently loaned audiobooks from PubHub openplatform loans API."""
        payload = self.get_json("https://pubhub-openplatform.dbc.dk/v1/user/loans?")
        results: list[CatalogBook] = []
        for loan in (payload.get("loans") or []) if isinstance(payload, dict) else []:
            if not isinstance(loan, dict):
                continue
            lib_book = loan.get("libraryBook") or {}
            if not isinstance(lib_book, dict):
                continue
            work_id = lib_book.get("workId") or lib_book.get("identifier") or loan.get("orderId")
            title = str(lib_book.get("title") or work_id or "").strip()
            if not work_id or not title:
                continue
            author = str(lib_book.get("author") or "").strip()
            if not author and isinstance(lib_book.get("authors"), list):
                author = ", ".join(str(a).strip() for a in lib_book["authors"] if a)
            wid_str = str(work_id).strip()
            book_url = (
                f"https://ereolen.dk/work/{wid_str}"
                if wid_str.startswith("work-of:")
                else f"https://ereolen.dk/work/work-of:{wid_str}"
            )
            results.append(
                CatalogBook(
                    title=title,
                    author=author,
                    url=book_url,
                    source="ereolen",
                )
            )
        return results

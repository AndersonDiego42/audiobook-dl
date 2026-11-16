from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, BookId, Chapter, Cover, Result, Series
from audiobookdl.exceptions import DataNotPresent, RequestError, UserNotAuthorized

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
from urllib3.util import parse_url


class OverdriveSource(Source):
    match = [
        r"https?://.+\.listen\.overdrive\.com.*",
        r"https?://(www\.)?libbyapp\.com/(open/loan|shelf)/.*",
    ]
    names = ["Overdrive", "Libby"]
    supports_library = True

    def _validate_auth_state(self, url: str) -> None:
        if "libbyapp.com" in url and not self._session.cookies:
            raise UserNotAuthorized(
                message="Libby requires valid browser cookies exported from libbyapp.com."
            )

    def verify_auth(self, url: str = "") -> bool:
        if not self._session.cookies and not self.authenticated:
            return False
        try:
            chip = self.get_json("https://sentry-read.svc.overdrive.com/chip")
            return bool(isinstance(chip, dict) and (chip.get("identity") or chip.get("loans") is not None))
        except (RequestError, UserNotAuthorized, ValueError):
            return self.authenticated

    def _resolve_libby_open_url(self, url: str) -> str:
        """Resolve a `libbyapp.com/open/loan/<id>` URL via Libby's chip API to its listen URL."""
        loan_match = re.search(r"/open/loan/([^/?#]+)", url)
        loan_id = loan_match.group(1) if loan_match else ""
        chip = self.get_json("https://sentry-read.svc.overdrive.com/chip")
        loans = chip.get("loans") or []
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            if loan_id and str(loan.get("id")) != loan_id and str(loan.get("titleId")) != loan_id:
                continue
            open_url = loan.get("openUrl") or loan.get("listenUrl") or loan.get("webUrl")
            if isinstance(open_url, str) and "overdrive.com" in open_url:
                return open_url
            card_id = loan.get("cardId")
            title_id = loan.get("id") or loan.get("titleId") or loan_id
            if card_id and title_id:
                open_resp = self.get_json(
                    f"https://sentry-read.svc.overdrive.com/open/audiobook/card/{card_id}/title/{title_id}"
                )
                resolved = open_resp.get("url") or open_resp.get("listenUrl")
                if isinstance(resolved, str) and resolved:
                    return resolved
        raise UserNotAuthorized(
            message="Could not find an active Libby loan matching this URL in your session."
        )

    def _download_listen_url(self, url: str) -> Audiobook:
        parsed_url = parse_url(url)
        hostname = parsed_url.hostname
        prefix = f"https://{hostname}"
        page_html = self._get_page(url).decode("utf-8", errors="replace")
        if re.search(r"(?:signin|Authentication required|loginForm)", page_html, re.IGNORECASE) and "window.bData" not in page_html:
            raise UserNotAuthorized(
                message="OverDrive/Libby session cookies are missing or expired."
            )
        match = re.search(r"window\.bData = (.+?);", page_html)
        if match is None:
            raise UserNotAuthorized
        book_info = json.loads(match.group(1))
        return Audiobook(
            session=self._session,
            files=self.get_files(prefix, book_info),
            metadata=self.get_metadata(book_info, url=url),
            cover=self.get_cover(prefix, book_info),
            chapters=self.get_chapters(book_info),
        )

    def download_from_id(self, book_id: str) -> Audiobook:
        """Download an active Libby loan by loan id, title id, or direct listen URL."""
        target = book_id if "overdrive.com" in book_id else f"https://libbyapp.com/open/loan/{book_id}"
        self._validate_auth_state(target)
        listen_url = target if "libbyapp.com" not in target else self._resolve_libby_open_url(target)
        return self._download_listen_url(listen_url)

    def download(self, url: str) -> Result:
        self._validate_auth_state(url)
        if "libbyapp.com" in url:
            if re.search(r"libbyapp\.com/shelf(?:[/?#]|$)", url, re.IGNORECASE):
                loans = self.list_library()
                if not loans:
                    raise UserNotAuthorized(
                        message="No active Libby loans were found on this shelf."
                    )
                books: List[BookId[str] | Audiobook] = []
                for item in loans:
                    loan_match = re.search(r"/open/loan/([^/?#]+)", item.url)
                    bid = loan_match.group(1) if loan_match else item.url
                    books.append(BookId(bid, title=item.title))
                return Series(title="Libby Shelf", books=books)
            url = self._resolve_libby_open_url(url)
        return self._download_listen_url(url)

    @staticmethod
    def _extract_isbn(book_info: Dict[str, Any]) -> Optional[str]:
        if isinstance(book_info.get("isbn"), str) and book_info["isbn"].strip():
            return book_info["isbn"].strip()
        for ident in book_info.get("identifiers") or []:
            if isinstance(ident, dict):
                if "isbn" in str(ident.get("type") or "").lower() and ident.get("value"):
                    return str(ident["value"]).strip()
        return None

    def get_metadata(self, book_info: Dict[str, Any], url: Optional[str] = None) -> AudiobookMetadata:
        title = book_info["title"]["main"]

        desc_raw = book_info.get("description")
        if isinstance(desc_raw, dict):
            description = str(desc_raw.get("full") or desc_raw.get("short") or "").strip() or None
        elif isinstance(desc_raw, str):
            description = desc_raw.strip() or None
        else:
            description = None
        if description:
            description = " ".join(re.sub(r"<[^>]+>", " ", description).split()) or None

        pub_raw = book_info.get("publisher")
        if isinstance(pub_raw, dict):
            publisher = str(pub_raw.get("name") or "").strip() or None
        elif isinstance(pub_raw, str):
            publisher = pub_raw.strip() or None
        else:
            publisher = None

        series_name: Optional[str] = None
        series_order: Optional[int] = None
        series_raw = book_info.get("series")
        if isinstance(series_raw, dict):
            series_name = str(series_raw.get("name") or series_raw.get("title") or "").strip() or None
            pos = series_raw.get("position") or series_raw.get("number")
            if pos is not None:
                try:
                    series_order = int(float(pos))
                except (TypeError, ValueError):
                    series_order = None
        elif isinstance(series_raw, str) and series_raw.strip():
            series_name = series_raw.strip()
        if not series_name and isinstance(book_info.get("edition"), str) and book_info["edition"].strip():
            series_name = book_info["edition"].strip()

        metadata = AudiobookMetadata(
            title=title,
            description=description,
            publisher=publisher,
            isbn=self._extract_isbn(book_info),
            series=series_name,
            series_order=series_order,
            source="overdrive",
            scrape_url=url,
        )
        for creator in book_info.get("creator") or []:
            if creator["role"] == "author":
                metadata.add_author(creator["name"])
            if creator["role"] == "narrator":
                metadata.add_narrator(creator["name"])
        return metadata

    def get_cover(self, prefix: str, book_info) -> Cover:
        cover_uri = book_info["-odread-furbish-uri"]
        cover_url = urljoin(f"{prefix}/", cover_uri)
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def _compute_part_offsets(self, book_info) -> List[float]:
        """Return cumulative start offsets in seconds for each spine part."""
        offsets: List[float] = []
        elapsed = 0.0
        for part in book_info["spine"]:
            offsets.append(elapsed)
            elapsed += part["audio-duration"]
        return offsets

    def get_chapters(self, book_info) -> List[Chapter]:
        part_offsets = self._compute_part_offsets(book_info)
        chapters = []
        for chapter in book_info["nav"]["toc"]:
            timepoint = 0.0
            if "#" in chapter["path"]:
                timepoint = float(chapter["path"].split("#")[1])
            part_result = re.search(r"(?<=(Part))\d+", chapter["path"])
            if part_result is None:
                continue
            part = int(part_result.group(0)) - 1
            base_offset = part_offsets[part] if 0 <= part < len(part_offsets) else 0.0
            start = int((base_offset + timepoint) * 1000)
            chapters.append(Chapter(start, chapter["title"]))
        return chapters

    @staticmethod
    def _flatten_toc(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flat: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            flat.append(entry)
            nested = entry.get("contents")
            if isinstance(nested, list) and nested:
                flat.extend(OverdriveSource._flatten_toc(nested))
        return flat

    def get_files(self, prefix: str, book_info) -> List[AudiobookFile]:
        toc_entries = (book_info.get("nav") or {}).get("toc") or []
        has_nested = any(isinstance(part, dict) and "contents" in part for part in toc_entries)
        part_titles: Dict[str, str] = {}
        flat_titles: List[str] = []
        for entry in self._flatten_toc(toc_entries):
            title = str(entry.get("title") or "").strip()
            path = str(entry.get("path") or "").split("#", 1)[0].strip()
            if path and title and path not in part_titles:
                part_titles[path] = title
            if title:
                flat_titles.append(title)

        files = []
        for num, part in enumerate(book_info["spine"]):
            part_path = str(part.get("path") or "")
            if has_nested:
                title = (
                    part_titles.get(part_path)
                    or part_titles.get(f"Part{num + 1}.mp3")
                    or (
                        flat_titles[num]
                        if num < len(flat_titles) and len(flat_titles) == len(book_info["spine"])
                        else f"Part {num + 1}"
                    )
                )
            else:
                title = flat_titles[num] if num < len(flat_titles) else f"Part {num + 1}"
            files.append(
                AudiobookFile(
                    url=f"{prefix}/{part['path']}",
                    title=title,
                    ext="mp3",
                )
            )
        return files

    def list_library(self) -> List[CatalogBook]:
        """List active OverDrive/Libby audiobook loans from Sentry chip API."""
        chip = self.get_json("https://sentry-read.svc.overdrive.com/chip")
        results: List[CatalogBook] = []
        for loan in chip.get("loans") or []:
            if not isinstance(loan, dict):
                continue
            loan_id = loan.get("id") or loan.get("titleId")
            title = str(loan.get("title") or "").strip()
            if not loan_id or not title:
                continue
            url = (
                loan.get("openUrl")
                or loan.get("listenUrl")
                or f"https://libbyapp.com/open/loan/{loan_id}"
            )
            results.append(
                CatalogBook(
                    title=title,
                    author=str(loan.get("firstCreatorName") or loan.get("author") or "").strip(),
                    url=str(url),
                    source="overdrive",
                )
            )
        return results

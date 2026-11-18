"""O'Reilly Learning (`learning.oreilly.com`) source adapter for audiobooks, video courses (audio or MP4), and EPUBs."""

from __future__ import annotations

from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
import json
import mimetypes
import posixpath
import re
import threading
from typing import Any
from urllib.parse import quote_plus, urljoin
import zipfile

from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover, Result, Series
from audiobookdl.options_schema import MEDIA_TYPE_CHOICES
from audiobookdl.exceptions import (
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotFound,
    GenericAudiobookDLException,
    NoFilesFound,
    RequestError,
    UserNotAuthorized,
)
from audiobookdl.utils.audiobook import (
    clean_html_description,
    detect_image_extension,
    is_video_extension,
    parse_duration_ms,
    parse_language,
    parse_release_date,
)
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import extract_session_cookies


_BASE_URL = "https://learning.oreilly.com"
_KALTURA_BASE_URL = "https://cdnapisec.kaltura.com"
_DEFAULT_KALTURA_PARTNER_ID = "1926081"
_AUTH_COOKIE_NAMES = frozenset({"orm-jwt", "orm-rt", "groot_sessionid", "sessionid"})
_HLS_CONTENT_TYPES = (
    "application/octet-stream",
    "binary/octet-stream",
    "video/MP2T",
    "video/mp2t",
    "audio/aac",
    "audio/x-aac",
    "audio/mp4",
    "video/mp4",
    "audio/mpeg",
)

_URN_RE = re.compile(r"(urn:orm:(?:audiobook|book|video):[^/?#]+)", re.IGNORECASE)
_PATH_ID_RE = re.compile(
    r"https?://(?:www\.)?learning\.oreilly\.com/"
    r"(?:library/view|audiobooks|videos|course)/(?:[^/]+/)?([^/?#]+)",
    re.IGNORECASE,
)


def _normalize_zip_member_path(raw_path: str) -> str | None:
    """Normalize an archive member path and reject empty or path-traversal entries."""
    cleaned = raw_path.replace("\\", "/").lstrip("/")
    if not cleaned:
        return None
    normalized = posixpath.normpath(cleaned)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        return None
    return normalized


def _extract_names(raw: Any) -> list[str]:
    """Extract display names from O'Reilly contributor/publisher/topic lists or dicts."""
    if not raw:
        return []
    if isinstance(raw, str):
        val = raw.strip()
        return [val] if val else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
            if name:
                out.append(name)
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _extract_description(info: dict[str, Any]) -> str | None:
    """Extract HTML or plain-text description from v2 metadata or epubs payloads."""
    raw = info.get("description") or info.get("descriptions")
    if isinstance(raw, dict):
        raw = raw.get("text/html") or raw.get("text/plain") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return clean_html_description(raw.strip())


def _duration_ms_from_entry(entry: dict[str, Any]) -> int:
    """Parse clip/chapter duration from O'Reilly JSON payloads into integer milliseconds."""
    raw_ms = entry.get("duration_ms") or entry.get("duration_milliseconds")
    if isinstance(raw_ms, (int, float)) and raw_ms > 0:
        return round(float(raw_ms))
    raw_sec = entry.get("duration_seconds")
    if raw_sec is None:
        raw_sec = entry.get("duration")
    return parse_duration_ms(raw_sec) or 0


class OReillySource(Source[str]):
    """Adapter for O'Reilly Learning audiobooks, video-course audio, EPUB ebooks, search, and library."""

    match = [
        r"https?://(?:www\.)?learning\.oreilly\.com/library/view/[^/]+/[^/?#]+",
        r"https?://(?:www\.)?learning\.oreilly\.com/audiobooks/(?:[^/]+/)?[^/?#]+",
        r"https?://(?:www\.)?learning\.oreilly\.com/videos/(?:[^/]+/)?[^/?#]+",
        r"https?://(?:www\.)?learning\.oreilly\.com/course/(?:[^/]+/)?[^/?#]+",
        r"https?://(?:www\.)?learning\.oreilly\.com/api/v2/epubs/urn:orm:[^/?#]+",
        r"https?://(?:www\.)?learning\.oreilly\.com/api/v2/metadata/urn:orm:[^/?#]+",
    ]
    names = ["OReilly", "O'Reilly"]
    _authentication_methods = ["cookies"]
    supports_search = True
    supports_library = True

    def __init__(self, options) -> None:
        super().__init__(options)
        self._kaltura_session_cache: tuple[str, str] | None = None

    @property
    def _media_type(self) -> str:
        raw = getattr(self._options, "media_type", None) or "auto"
        cleaned = str(raw).strip().lower()
        return cleaned if cleaned in MEDIA_TYPE_CHOICES else "auto"

    @staticmethod
    def _api_headers() -> dict[str, str]:
        return {
            "Referer": f"{_BASE_URL}/",
            "Origin": _BASE_URL,
            "Accept": "application/json",
        }

    def _has_auth_cookies(self) -> bool:
        return any(
            cookie.name in _AUTH_COOKIE_NAMES and bool(cookie.value)
            for cookie in extract_session_cookies(self._session)
        )

    def _get_json_any_authed(self, url: str) -> dict[str, Any] | list[Any]:
        """Fetch JSON (`dict` or `list`) from O'Reilly, raising `UserNotAuthorized` on auth failures."""
        resp = self.request("GET", url, headers=self._api_headers())
        if resp.status_code in (401, 403):
            raise UserNotAuthorized(detail=f"HTTP {resp.status_code} from {url}")
        if resp.status_code != 200:
            raise RequestError(detail=f"HTTP {resp.status_code} from {url}")
        content_type = str(getattr(resp, "headers", {}).get("Content-Type") or "").lower()
        raw_url = str(getattr(resp, "url", "") or "").lower()
        if (
            "login" in raw_url
            or "signin" in raw_url
            or ("text/html" in content_type and b"<html" in resp.content[:256].lower())
        ):
            raise UserNotAuthorized(detail="O'Reilly session cookies are missing or expired")
        try:
            payload = resp.json()
        except Exception:
            payload = json.loads(resp.content.decode("utf-8"))
        if not isinstance(payload, (dict, list)):
            raise RequestError(detail=f"Unexpected JSON payload from {url}")
        return payload

    def _get_json_authed(self, url: str) -> dict[str, Any]:
        """Fetch a JSON object from O'Reilly, raising `UserNotAuthorized` on auth redirects."""
        payload = self._get_json_any_authed(url)
        if not isinstance(payload, dict):
            raise RequestError(detail=f"Expected JSON object from {url}")
        return payload

    def verify_auth(self, url: str = "") -> bool:
        """Return True if imported O'Reilly session cookies authenticate against O'Reilly APIs."""
        if not self._has_auth_cookies() and not self.authenticated:
            return False
        for endpoint in (
            f"{_BASE_URL}/api/v1/player/kaltura_session/",
            f"{_BASE_URL}/api/v1/bookmarks/?limit=1",
            f"{_BASE_URL}/api/v3/collections/",
        ):
            try:
                self._get_json_any_authed(endpoint)
                return True
            except UserNotAuthorized:
                return False
            except (RequestError, ValueError):
                continue
        return False

    @staticmethod
    def _candidate_urns_for_url(url: str) -> list[str]:
        urn_match = _URN_RE.search(url)
        if urn_match:
            return [urn_match.group(1)]
        id_match = _PATH_ID_RE.search(url)
        if not id_match:
            raise BookNotFound
        item_id = id_match.group(1).strip()
        lowered = url.lower()
        if "/audiobooks/" in lowered:
            order = ("audiobook", "video", "book")
        elif "/videos/" in lowered or "/course/" in lowered:
            # O'Reilly serves both video courses and many audiobooks under `/videos/-/<id>/`.
            order = ("video", "audiobook", "book")
        else:
            order = ("book", "audiobook", "video")
        return [f"urn:orm:{kind}:{item_id}" for kind in order]

    def _fetch_work_info(self, urn: str) -> dict[str, Any]:
        """Fetch metadata for any O'Reilly URN (`book`, `audiobook`, or `video`)."""
        try:
            return self._get_json_authed(f"{_BASE_URL}/api/v2/metadata/{urn}/")
        except UserNotAuthorized:
            raise
        except RequestError:
            if ":book:" in urn.lower():
                return self._get_json_authed(f"{_BASE_URL}/api/v2/epubs/{urn}/")
            raise

    def _resolve_urn_and_info(self, url: str) -> tuple[str, dict[str, Any]]:
        candidates = self._candidate_urns_for_url(url)
        last_exc: Exception | None = None
        for urn in candidates:
            try:
                info = self._fetch_work_info(urn)
                resolved_urn = str(info.get("ourn") or info.get("archive_id") or urn)
                return resolved_urn, info
            except UserNotAuthorized:
                raise
            except RequestError as exc:
                last_exc = exc
                continue
        # Fallback: query search API by extracted identifier
        item_id = candidates[0].rsplit(":", 1)[-1]
        try:
            search_data = self._get_json_authed(
                f"{_BASE_URL}/api/v2/search/?query={quote_plus(item_id)}&limit=5"
            )
            for hit in search_data.get("results") or []:
                if not isinstance(hit, dict):
                    continue
                hit_urn = str(hit.get("ourn") or hit.get("archive_id") or "").strip()
                if hit_urn.startswith("urn:orm:"):
                    return hit_urn, self._fetch_work_info(hit_urn)
        except UserNotAuthorized:
            raise
        except RequestError:
            pass
        if last_exc is not None:
            raise BookNotFound from last_exc
        raise BookNotFound

    def _fetch_paginated_results(self, initial_url: str, *, max_pages: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = initial_url
        seen: set[str] = set()
        pages = 0
        while next_url and next_url not in seen and pages < max_pages:
            seen.add(next_url)
            pages += 1
            payload = self._get_json_any_authed(next_url)
            if isinstance(payload, list):
                items.extend(entry for entry in payload if isinstance(entry, dict))
                break
            page_results = payload.get("results") or payload.get("items") or payload.get("toc") or []
            if isinstance(page_results, list):
                items.extend(entry for entry in page_results if isinstance(entry, dict))
            raw_next = payload.get("next")
            next_url = urljoin(f"{_BASE_URL}/", str(raw_next)) if raw_next else None
        return items

    @staticmethod
    def _is_playable_toc_entry(entry: dict[str, Any]) -> bool:
        """Skip structural section headers in `/api/v1/videotocs/` that lack playable media."""
        if entry.get("ourn") or entry.get("kaltura_entry_id"):
            return True
        if any(entry.get(k) for k in ("audio_url", "stream_url", "download_url", "media_url", "mp3", "m4a")):
            return True
        return bool(entry.get("reference_id") and entry.get("duration"))

    def _fetch_chapters_raw(self, urn: str, info: dict[str, Any]) -> list[dict[str, Any]]:
        embedded = info.get("chapters")
        if isinstance(embedded, list) and embedded and all(isinstance(c, dict) for c in embedded):
            return embedded
        item_id = str(info.get("identifier") or urn.rsplit(":", 1)[-1]).strip()
        try:
            toc_payload = self._get_json_authed(f"{_BASE_URL}/api/v1/videotocs/{item_id}/")
            toc_entries = toc_payload.get("toc")
            if isinstance(toc_entries, list) and toc_entries:
                playable = [
                    e for e in toc_entries
                    if isinstance(e, dict) and self._is_playable_toc_entry(e)
                ]
                if playable:
                    return playable
        except RequestError:
            pass
        chapters_url = str(info.get("chapters_url") or f"{_BASE_URL}/api/v2/epubs/{urn}/chapters/")
        try:
            return self._fetch_paginated_results(chapters_url)
        except RequestError:
            return []

    def _get_kaltura_session(self) -> tuple[str, str]:
        """Fetch and cache the `(ks_token, partner_id)` tuple for Kaltura HLS requests."""
        if self._kaltura_session_cache is not None:
            return self._kaltura_session_cache
        payload = self._get_json_authed(f"{_BASE_URL}/api/v1/player/kaltura_session/")
        ks = str(payload.get("session") or payload.get("ks") or "").strip()
        if not ks:
            raise UserNotAuthorized(detail="Missing Kaltura session token from O'Reilly player endpoint")
        partner_id = str(payload.get("partner_id") or _DEFAULT_KALTURA_PARTNER_ID).strip()
        self._kaltura_session_cache = (ks, partner_id)
        return ks, partner_id

    def _resolve_kaltura_entry_id(self, clip: dict[str, Any]) -> str | None:
        entry_id = str(clip.get("kaltura_entry_id") or clip.get("entry_id") or "").strip()
        if entry_id:
            return entry_id
        ref_id = str(clip.get("reference_id") or clip.get("video_id") or "").strip()
        if not ref_id:
            return None
        clip_info = self._get_json_authed(f"{_BASE_URL}/api/v1/videoclips/{quote_plus(ref_id)}/")
        resolved = str(clip_info.get("kaltura_entry_id") or clip_info.get("entry_id") or "").strip()
        return resolved or None

    def _kaltura_manifest_url(self, entry_id: str) -> str:
        ks, partner_id = self._get_kaltura_session()
        return (
            f"{_KALTURA_BASE_URL}/p/{partner_id}/sp/{partner_id}00/playManifest"
            f"/entryId/{entry_id}/format/applehttp/protocol/https/ks/{ks}/a.m3u8"
        )

    def _build_metadata(self, urn: str, info: dict[str, Any], *, url: str) -> AudiobookMetadata:
        title = str(info.get("name") or info.get("title") or urn).strip()
        authors: list[str] = []
        narrators: list[str] = []
        talent = info.get("talent")
        if isinstance(talent, dict):
            contributors = talent.get("contributors")
            if isinstance(contributors, list):
                for person in contributors:
                    if not isinstance(person, dict):
                        continue
                    person_name = str(person.get("name") or "").strip()
                    if not person_name:
                        continue
                    role = str(person.get("contributor_type") or "").strip().lower()
                    if role == "narrator":
                        narrators.append(person_name)
                    else:
                        authors.append(person_name)
        if not authors:
            authors = _extract_names(info.get("authors") or info.get("creators"))
        if not narrators:
            narrators = _extract_names(info.get("narrators"))

        publishers = _extract_names(info.get("publishers") or info.get("publisher"))
        publisher = ", ".join(publishers) if publishers else None
        genres = _extract_names(info.get("topics") or info.get("subjects"))
        raw_lang = info.get("language")
        language = parse_language(str(raw_lang)) if raw_lang else None
        raw_date = info.get("issued") or info.get("publication_date") or info.get("created_time")
        release_date = parse_release_date(str(raw_date)) if raw_date else None
        isbn = str(info.get("isbn") or info.get("identifier") or "").strip() or None
        description = _extract_description(info)

        return AudiobookMetadata(
            title=title,
            scrape_url=url,
            authors=authors,
            narrators=narrators,
            genres=genres,
            publisher=publisher,
            release_date=release_date,
            isbn=isbn,
            description=description,
            language=language,
            source="oreilly",
        )

    def _download_cover(self, urn: str, info: dict[str, Any]) -> Cover | None:
        item_id = str(info.get("identifier") or urn.rsplit(":", 1)[-1]).strip()
        explicit_cover = str(info.get("cover") or info.get("cover_url") or "").strip()
        candidates = [
            *( [urljoin(f"{_BASE_URL}/", explicit_cover)] if explicit_cover else [] ),
            f"{_BASE_URL}/covers/{item_id}/",
            f"{_BASE_URL}/library/cover/{item_id}/",
        ]
        for cover_url in candidates:
            try:
                data = self.get(cover_url, headers={"Referer": f"{_BASE_URL}/"})
            except RequestError:
                continue
            if data:
                return Cover(image=data, extension=detect_image_extension(data, default="jpg"))
        return None

    @staticmethod
    def _direct_audio_url(entry: dict[str, Any]) -> str | None:
        for key in ("audio_url", "stream_url", "download_url", "media_url", "mp3", "m4a"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    def _resolve_audio_files_and_chapters(
        self,
        urn: str,
        info: dict[str, Any],
        *,
        prefer_video: bool = False,
    ) -> tuple[list[AudiobookFile], list[Chapter]]:
        raw_chapters = self._fetch_chapters_raw(urn, info)
        entries: list[dict[str, Any]] = list(raw_chapters) if raw_chapters else [info]
        files: list[AudiobookFile] = []
        chapters: list[Chapter] = []
        offset_ms = 0
        kaltura_headers: MutableMapping[str, str | bytes] = {
            "Referer": f"{_BASE_URL}/",
            "Origin": _BASE_URL,
        }
        stream_headers = {"Referer": f"{_BASE_URL}/", "Origin": _BASE_URL}

        # Prime Kaltura session once before concurrent/sequential clip manifest resolution.
        needs_kaltura = any(
            not self._direct_audio_url(e)
            and (e.get("kaltura_entry_id") or e.get("entry_id") or e.get("reference_id") or e.get("video_id"))
            for e in entries
        )
        if needs_kaltura:
            self._get_kaltura_session()

        def _resolve_entry(item: tuple[int, dict[str, Any]]) -> tuple[str, int, list[AudiobookFile]]:
            idx, entry = item
            title = str(entry.get("title") or entry.get("name") or f"Chapter {idx + 1}").strip()
            dur_ms = _duration_ms_from_entry(entry)
            direct_url = self._direct_audio_url(entry)
            resolved_segment_files: list[AudiobookFile] = []

            if direct_url:
                lower_direct = direct_url.lower()
                if ".m3u8" in lower_direct:
                    resolved_segment_files = self.get_stream_files(
                        direct_url,
                        headers=stream_headers,
                        expected_content_type=_HLS_CONTENT_TYPES,
                        prefer_video=prefer_video,
                    )
                else:
                    if ".mp4" in lower_direct:
                        ext = "mp4"
                    elif ".mp3" in lower_direct:
                        ext = "mp3"
                    else:
                        ext = "m4a"
                    resolved_segment_files = [
                        AudiobookFile(
                            url=direct_url,
                            title=title,
                            ext=ext,
                            headers=dict(kaltura_headers),
                        )
                    ]
            else:
                kaltura_id = self._resolve_kaltura_entry_id(entry)
                if kaltura_id:
                    manifest_url = self._kaltura_manifest_url(kaltura_id)
                    resolved_segment_files = self.get_stream_files(
                        manifest_url,
                        headers=stream_headers,
                        expected_content_type=_HLS_CONTENT_TYPES,
                        prefer_video=prefer_video,
                    )
            return title, dur_ms, resolved_segment_files

        indexed_entries = list(enumerate(entries))
        if len(indexed_entries) > 1:
            resolved_entries = run_cancellable_pool(
                _resolve_entry,
                indexed_entries,
                max_workers=min(6, len(indexed_entries)),
            )
        else:
            resolved_entries = [_resolve_entry(item) for item in indexed_entries]

        for title, dur_ms, resolved_segment_files in resolved_entries:
            if not resolved_segment_files:
                continue
            chapter_idx = len(chapters)
            for seg in resolved_segment_files:
                seg.chapter_index = chapter_idx
            chapters.append(Chapter(start=offset_ms, title=title))
            offset_ms += dur_ms
            files.extend(resolved_segment_files)

        if not files:
            raise NoFilesFound
        return files, chapters

    def _build_stream_audiobook(
        self,
        urn: str,
        info: dict[str, Any],
        *,
        url: str,
        video: bool = False,
    ) -> Audiobook:
        files, chapters = self._resolve_audio_files_and_chapters(
            urn, info, prefer_video=video
        )
        return Audiobook(
            session=self._session,
            files=files,
            metadata=self._build_metadata(urn, info, url=url),
            cover=self._download_cover(urn, info),
            chapters=chapters,
            media_kind="video" if video else "audio",
        )

    def _write_epub_archive(
        self,
        urn: str,
        info: dict[str, Any],
        dest_path: str,
    ) -> None:
        """Download O'Reilly v2 EPUB assets and package them into a valid `.epub` zip archive."""
        epub_info = info
        if not info.get("files"):
            try:
                epub_info = self._get_json_authed(f"{_BASE_URL}/api/v2/epubs/{urn}/")
            except RequestError:
                epub_info = info
        files_url = str(epub_info.get("files") or f"{_BASE_URL}/api/v2/epubs/{urn}/files/")
        file_entries = self._fetch_paginated_results(files_url)
        if not file_entries:
            raise BookHasNoEbook

        download_specs: list[tuple[str, str, str]] = []
        for entry in file_entries:
            raw_rel = str(
                entry.get("full_path")
                or entry.get("filename")
                or entry.get("path")
                or ""
            ).strip()
            rel_path = _normalize_zip_member_path(raw_rel)
            asset_url = str(entry.get("url") or "").strip()
            if not rel_path or not asset_url:
                continue
            asset_url = urljoin(f"{_BASE_URL}/", asset_url)
            media_type = str(entry.get("media_type") or "").strip()
            if not media_type:
                guessed, _ = mimetypes.guess_type(rel_path)
                media_type = guessed or "application/octet-stream"
            download_specs.append((rel_path, asset_url, media_type))

        if not download_specs:
            raise BookHasNoEbook

        member_paths = {spec[0] for spec in download_specs}
        has_container = "META-INF/container.xml" in member_paths
        has_opf = any(p.lower().endswith(".opf") for p in member_paths)
        asset_specs = [spec for spec in download_specs if spec[0] != "mimetype"]
        zip_lock = threading.Lock()

        with zipfile.ZipFile(dest_path, "w") as zf:
            # Per EPUB Open Container Format spec, `mimetype` must be first and uncompressed.
            zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)

            if len(asset_specs) <= 1:
                for rel_path, asset_url, _mt in asset_specs:
                    content = self.get(asset_url, headers={"Referer": f"{_BASE_URL}/"})
                    zf.writestr(rel_path, content, compress_type=zipfile.ZIP_DEFLATED)
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(asset_specs))) as pool:
                    futures = {
                        pool.submit(
                            self.get,
                            asset_url,
                            headers={"Referer": f"{_BASE_URL}/"},
                        ): rel_path
                        for rel_path, asset_url, _mt in asset_specs
                    }
                    for fut in as_completed(futures):
                        rel_path = futures[fut]
                        content = fut.result()
                        with zip_lock:
                            zf.writestr(rel_path, content, compress_type=zipfile.ZIP_DEFLATED)

            if not has_container:
                opf_path = next((p for p in member_paths if p.lower().endswith(".opf")), "OEBPS/content.opf")
                container_xml = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                    "  <rootfiles>\n"
                    f'    <rootfile full-path="{escape(opf_path)}" media-type="application/oebps-package+xml"/>\n'
                    "  </rootfiles>\n"
                    "</container>\n"
                )
                zf.writestr("META-INF/container.xml", container_xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            if not has_opf:
                meta = self._build_metadata(urn, epub_info, url="")
                title = meta.title
                authors = meta.authors
                lang = str(epub_info.get("language") or "en")
                creator_tags = "".join(
                    f"    <dc:creator>{escape(author)}</dc:creator>\n" for author in authors
                )
                manifest_items: list[str] = []
                spine_refs: list[str] = []
                for idx, (rel_path, _url, media_type) in enumerate(download_specs):
                    if rel_path in ("mimetype", "META-INF/container.xml"):
                        continue
                    arc_href = rel_path[6:] if rel_path.startswith("OEBPS/") else f"../{rel_path}"
                    item_id = f"item_{idx}"
                    manifest_items.append(
                        f'    <item id="{item_id}" href="{escape(arc_href)}" media-type="{escape(media_type)}"/>'
                    )
                    if media_type in ("application/xhtml+xml", "text/html") or rel_path.lower().endswith(
                        (".xhtml", ".html", ".htm")
                    ):
                        spine_refs.append(f'    <itemref idref="{item_id}"/>')
                opf_xml = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
                    '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
                    f'    <dc:identifier id="bookid">{escape(urn)}</dc:identifier>\n'
                    f"    <dc:title>{escape(title)}</dc:title>\n"
                    f"    <dc:language>{escape(lang)}</dc:language>\n"
                    f"{creator_tags}"
                    "  </metadata>\n"
                    "  <manifest>\n"
                    + "\n".join(manifest_items)
                    + "\n  </manifest>\n"
                    "  <spine>\n"
                    + "\n".join(spine_refs)
                    + "\n  </spine>\n"
                    "</package>\n"
                )
                zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

    def _build_ebook_audiobook(self, urn: str, info: dict[str, Any], *, url: str) -> Audiobook:
        epubs_api_url = f"{_BASE_URL}/api/v2/epubs/{urn}/"
        file_headers: MutableMapping[str, str | bytes] = dict(self._api_headers())
        def _write_epub(dest_path: str, u: str = urn, i: dict[str, Any] = info) -> None:
            self._write_epub_archive(u, i, dest_path)

        return Audiobook(
            session=self._session,
            files=[
                AudiobookFile(
                    url=epubs_api_url,
                    ext="epub",
                    headers=file_headers,
                    file_writer=_write_epub,
                )
            ],
            metadata=self._build_metadata(urn, info, url=url),
            cover=self._download_cover(urn, info),
            chapters=[],
        )

    def download(self, url: str) -> Result:
        """Download an O'Reilly audiobook, video course (video or audio track), or EPUB ebook."""
        urn, info = self._resolve_urn_and_info(url)
        kind = str(
            info.get("type")
            or info.get("display_format")
            or info.get("content_format")
            or info.get("format")
            or ""
        ).strip().lower()
        if not kind:
            if ":audiobook:" in urn.lower():
                kind = "audiobook"
            elif ":video:" in urn.lower():
                kind = "video"
            else:
                kind = "book"

        mode = self._media_type
        item_id = str(info.get("identifier") or urn.rsplit(":", 1)[-1]).strip()
        is_audio_kind = kind in ("audiobook", "video")
        req_fmt = str(getattr(self._options, "output_format", "") or "").strip()
        wants_video_by_format = (kind == "video") and is_video_extension(req_fmt)

        if mode == "video":
            if kind == "video":
                return self._build_stream_audiobook(urn, info, url=url, video=True)
            alt_urn = f"urn:orm:video:{item_id}"
            try:
                alt_info = self._fetch_work_info(alt_urn)
                return self._build_stream_audiobook(alt_urn, alt_info, url=url, video=True)
            except RequestError as exc:
                raise GenericAudiobookDLException(
                    "No video course available",
                    f"'{info.get('title') or info.get('name') or urn}' does not have a video stream.",
                ) from exc

        if mode == "audiobook":
            if is_audio_kind:
                return self._build_stream_audiobook(urn, info, url=url, video=False)
            for alt_kind in ("audiobook", "video"):
                alt_urn = f"urn:orm:{alt_kind}:{item_id}"
                try:
                    alt_info = self._fetch_work_info(alt_urn)
                    return self._build_stream_audiobook(alt_urn, alt_info, url=url, video=False)
                except RequestError:
                    continue
            raise BookHasNoAudiobook

        if mode == "ebook":
            if not is_audio_kind:
                return self._build_ebook_audiobook(urn, info, url=url)
            alt_urn = f"urn:orm:book:{item_id}"
            try:
                alt_info = self._fetch_work_info(alt_urn)
                return self._build_ebook_audiobook(alt_urn, alt_info, url=url)
            except RequestError as exc:
                raise BookHasNoEbook from exc

        if mode == "both":
            items: list[Audiobook] = []
            if is_audio_kind:
                items.append(self._build_stream_audiobook(urn, info, url=url, video=wants_video_by_format))
                try:
                    alt_urn = f"urn:orm:book:{item_id}"
                    items.append(self._build_ebook_audiobook(alt_urn, self._fetch_work_info(alt_urn), url=url))
                except RequestError:
                    pass
            else:
                items.append(self._build_ebook_audiobook(urn, info, url=url))
                for alt_kind in ("audiobook", "video"):
                    try:
                        alt_urn = f"urn:orm:{alt_kind}:{item_id}"
                        alt_video = (alt_kind == "video") and is_video_extension(req_fmt)
                        items.append(
                            self._build_stream_audiobook(
                                alt_urn, self._fetch_work_info(alt_urn), url=url, video=alt_video
                            )
                        )
                        break
                    except RequestError:
                        continue
            if len(items) == 1:
                return items[0]
            return Series(title=items[0].metadata.title, books=items)

        # Default "auto": audiobooks & video courses extract audio (or video when `-f mp4`/`mkv`), books assemble EPUB.
        if is_audio_kind:
            return self._build_stream_audiobook(urn, info, url=url, video=wants_video_by_format)
        return self._build_ebook_audiobook(urn, info, url=url)

    def _catalog_book_from_item(self, entry: dict[str, Any]) -> CatalogBook | None:
        raw_content = entry.get("content")
        item = raw_content if isinstance(raw_content, dict) else entry
        urn = str(item.get("ourn") or item.get("archive_id") or item.get("urn") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            return None
        raw_url = str(item.get("web_url") or item.get("url") or "").strip()
        book_url = urljoin(f"{_BASE_URL}/", raw_url) if raw_url else ""
        if not book_url or not any(re.match(pattern, book_url) for pattern in self.match):
            if urn:
                item_id = urn.rsplit(":", 1)[-1]
                if ":audiobook:" in urn.lower():
                    book_url = f"{_BASE_URL}/audiobooks/-/{item_id}/"
                elif ":video:" in urn.lower():
                    book_url = f"{_BASE_URL}/videos/-/{item_id}/"
                else:
                    book_url = f"{_BASE_URL}/library/view/-/{item_id}/"
            else:
                return None

        authors = _extract_names(item.get("authors") or item.get("creators"))
        fmt_raw = str(
            item.get("format")
            or item.get("content_format")
            or item.get("type")
            or item.get("content_type")
            or ""
        ).strip().lower()
        formats: tuple[str, ...]
        if fmt_raw == "video" or ":video:" in urn.lower():
            formats = ("video", "audiobook")
        elif fmt_raw == "audiobook" or ":audiobook:" in urn.lower():
            formats = ("audiobook",)
        elif fmt_raw == "book" or ":book:" in urn.lower():
            formats = ("ebook",)
        else:
            formats = ("audiobook",)

        raw_cover = str(item.get("cover_url") or item.get("cover") or "").strip()
        if not raw_cover and urn:
            raw_cover = f"{_BASE_URL}/covers/{urn.rsplit(':', 1)[-1]}/"
        elif raw_cover:
            raw_cover = urljoin(f"{_BASE_URL}/", raw_cover)

        return CatalogBook(
            title=title,
            url=book_url,
            author=", ".join(authors),
            source="oreilly",
            cover_url=raw_cover or None,
            formats=formats,
        )

    def search(self, query: str) -> list[CatalogBook]:
        """Search O'Reilly Learning across audiobooks, books, and video courses."""
        cleaned = query.strip()
        if not cleaned:
            return []
        url = f"{_BASE_URL}/api/v2/search/?query={quote_plus(cleaned)}&limit=25"
        mode = self._media_type
        if mode == "audiobook":
            url += "&formats=audiobook&formats=video"
        elif mode == "ebook":
            url += "&formats=book"
        payload = self._get_json_authed(url)
        raw_results = payload.get("results") or []
        out: list[CatalogBook] = []
        seen_urls: set[str] = set()
        for entry in raw_results:
            if not isinstance(entry, dict):
                continue
            book = self._catalog_book_from_item(entry)
            if book is not None and book.url not in seen_urls:
                seen_urls.add(book.url)
                out.append(book)
        return out

    def list_library(self) -> list[CatalogBook]:
        """List deduplicated items from the user's O'Reilly Collections and Bookmarks."""
        collected: list[dict[str, Any]] = []
        any_ok = False
        last_auth_exc: UserNotAuthorized | None = None

        # 1. Collections (v3 then v2)
        for coll_url in (
            f"{_BASE_URL}/api/v3/collections/",
            f"{_BASE_URL}/api/v2/collections/",
        ):
            try:
                collections = self._fetch_paginated_results(coll_url)
                any_ok = True
                for coll in collections:
                    items = coll.get("items") or coll.get("content")
                    if isinstance(items, list) and items:
                        collected.extend(i for i in items if isinstance(i, dict))
                    elif coll.get("id"):
                        coll_id = str(coll["id"]).strip()
                        try:
                            detail = self._get_json_any_authed(
                                f"{coll_url.rstrip('/')}/{quote_plus(coll_id)}/"
                            )
                            if isinstance(detail, dict):
                                sub = detail.get("items") or detail.get("content") or detail.get("results") or []
                                if isinstance(sub, list):
                                    collected.extend(i for i in sub if isinstance(i, dict))
                            elif isinstance(detail, list):
                                collected.extend(i for i in detail if isinstance(i, dict))
                        except RequestError:
                            continue
                break
            except UserNotAuthorized as exc:
                last_auth_exc = exc
                break
            except RequestError:
                continue

        # 2. Bookmarks (v1 then v2)
        for bm_url in (
            f"{_BASE_URL}/api/v1/bookmarks/?limit=100",
            f"{_BASE_URL}/api/v2/bookmarks/?limit=100",
        ):
            try:
                bm_items = self._fetch_paginated_results(bm_url)
                any_ok = True
                collected.extend(bm_items)
                break
            except UserNotAuthorized as exc:
                last_auth_exc = exc
                break
            except RequestError:
                continue

        if not any_ok and last_auth_exc is not None:
            raise last_auth_exc

        out: list[CatalogBook] = []
        seen_urls: set[str] = set()
        for entry in collected:
            book = self._catalog_book_from_item(entry)
            if book is not None and book.url not in seen_urls:
                seen_urls.add(book.url)
                out.append(book)
        return out

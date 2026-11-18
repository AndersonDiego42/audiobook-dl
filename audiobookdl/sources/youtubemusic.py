"""YouTube Music (`music.youtube.com`) source adapter powered by `yt-dlp`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
import re
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from .source import Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, BookId, Chapter, Cover, Result, Series
from audiobookdl.exceptions import (
    BookNotFound,
    MissingBookAccess,
    NoFilesFound,
    RequestError,
    UserNotAuthorized,
)
from audiobookdl.logging import debug as log_debug
from audiobookdl.utils.audiobook import (
    clean_html_description,
    parse_duration_ms,
    parse_language,
    parse_release_date,
    parse_series_order,
)
from audiobookdl.utils.browser_cookies import export_cookies_netscape
from audiobookdl.utils.http import extract_session_cookies, resolve_max_retries

_YTMUSIC_BASE = "https://music.youtube.com"
_AUTH_COOKIE_NAMES = frozenset(
    {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID", "SID", "LOGIN_INFO"}
)
_PREFERRED_AUDIO_FORMAT = (
    "bestaudio[protocol^=http][ext=m4a]/"
    "bestaudio[protocol^=http][acodec^=mp4a]/"
    "bestaudio[protocol^=http]/"
    "bestaudio[ext=m4a]/"
    "bestaudio/best"
)
_UNAVAILABLE_TITLES = frozenset({"[private video]", "[deleted video]", "[unavailable video]"})
_TOPIC_SUFFIX_RE = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)


class _YtdlpLogger:
    """Route `yt-dlp` logs to `audiobookdl` debug output instead of stdout/stderr."""

    def debug(self, msg: str) -> None:
        if msg:
            log_debug(f"[yt-dlp] {msg}")

    def warning(self, msg: str) -> None:
        if msg:
            log_debug(f"[yt-dlp warning] {msg}")

    def error(self, msg: str) -> None:
        if msg:
            log_debug(f"[yt-dlp error] {msg}")


def _clean_author_name(raw: str) -> str:
    """Strip YouTube auto-generated `' - Topic'` channel suffixes and normalize whitespace."""
    cleaned = _TOPIC_SUFFIX_RE.sub("", raw or "").strip()
    return cleaned


def _extract_authors(info: dict[str, Any]) -> list[str]:
    """Extract deduplicated artist/author display names from a `yt-dlp` info dict."""
    candidates: list[str] = []
    raw_artists = info.get("artists")
    if isinstance(raw_artists, list):
        for item in raw_artists:
            if isinstance(item, str) and item.strip():
                candidates.append(item)
    elif isinstance(raw_artists, str) and raw_artists.strip():
        candidates.append(raw_artists)

    for key in ("artist", "album_artist", "creator", "uploader", "channel"):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append(val)

    seen: set[str] = set()
    authors: list[str] = []
    for raw in candidates:
        name = _clean_author_name(raw)
        if not name:
            continue
        lowered = name.lower()
        if lowered not in seen:
            seen.add(lowered)
            authors.append(name)
    return authors


def _pick_thumbnail_url(info: dict[str, Any]) -> str | None:
    """Select the highest-resolution thumbnail URL from a `yt-dlp` info dict."""
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        ranked: list[tuple[int, int, int, str]] = []
        for entry in thumbs:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            pref = int(entry.get("preference") or 0)
            width = int(entry.get("width") or 0)
            height = int(entry.get("height") or 0)
            ranked.append((pref, width * height, width, url))
        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            return ranked[0][3]
    single = str(info.get("thumbnail") or "").strip()
    return single or None


def _normalize_ytm_track_url(entry: dict[str, Any]) -> str | None:
    """Build a canonical `music.youtube.com/watch?v=...` URL from a flat or full entry."""
    raw_id = str(entry.get("id") or "").strip()
    raw_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    if raw_url:
        parts = urlsplit(raw_url)
        if parts.scheme in ("http", "https") and parts.netloc:
            qs = parse_qs(parts.query)
            video_id = (qs.get("v") or [""])[0].strip()
            if video_id:
                return f"{_YTMUSIC_BASE}/watch?v={video_id}"
            if "music.youtube.com" in parts.netloc.lower():
                return raw_url
    if raw_id and not raw_id.startswith(("http://", "https://")):
        return f"{_YTMUSIC_BASE}/watch?v={raw_id}"
    return None


def _select_audio_stream(info: dict[str, Any]) -> tuple[str, str, dict[str, str], bool]:
    """Select direct audio `(stream_url, ext, http_headers, is_hls)` from a `yt-dlp` info dict."""
    chosen: dict[str, Any] = info
    if not chosen.get("url"):
        req_formats = chosen.get("requested_formats")
        if isinstance(req_formats, list) and req_formats:
            audio_reqs = [
                f for f in req_formats if isinstance(f, dict) and f.get("acodec") not in (None, "none")
            ]
            chosen = audio_reqs[0] if audio_reqs else req_formats[0]
        else:
            formats = chosen.get("formats")
            if isinstance(formats, list) and formats:
                audio_only = [
                    f
                    for f in formats
                    if isinstance(f, dict)
                    and f.get("url")
                    and f.get("vcodec") in (None, "none")
                    and f.get("acodec") not in (None, "none")
                ]
                candidates = audio_only or [f for f in formats if isinstance(f, dict) and f.get("url")]
                if candidates:
                    candidates.sort(
                        key=lambda f: (
                            1 if str(f.get("protocol") or "").startswith("http") else 0,
                            1 if str(f.get("ext") or "").lower() == "m4a" else 0,
                            float(f.get("abr") or f.get("tbr") or 0.0),
                        ),
                        reverse=True,
                    )
                    chosen = candidates[0]

    stream_url = str(chosen.get("url") or "").strip()
    if not stream_url:
        raise NoFilesFound

    raw_ext = str(chosen.get("ext") or info.get("ext") or "m4a").strip().lower().lstrip(".")
    acodec = str(chosen.get("acodec") or info.get("acodec") or "").strip().lower()
    if raw_ext in ("mp4", "m4v", "") and (acodec.startswith("mp4a") or chosen.get("vcodec") in (None, "none")):
        ext = "m4a"
    else:
        ext = raw_ext or "m4a"

    raw_headers = chosen.get("http_headers") or info.get("http_headers") or {}
    headers: dict[str, str] = (
        {str(k): str(v) for k, v in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )

    protocol = str(chosen.get("protocol") or "").strip().lower()
    is_hls = "m3u8" in protocol or stream_url.split("?", 1)[0].lower().endswith(".m3u8")
    return stream_url, ext, headers, is_hls


class YouTubeMusicSource(Source[dict[str, Any]]):
    """YouTube Music (`music.youtube.com`) adapter using `yt-dlp` for extraction."""

    names = ["YouTubeMusic", "YouTube Music"]
    _authentication_methods = ["cookies"]
    supports_search = False
    supports_library = False

    match = [
        r"https?://music\.youtube\.com/watch\?(?:[^#]*&)?v=[^&#]+",
        r"https?://music\.youtube\.com/playlist\?(?:[^#]*&)?list=[^&#]+",
        r"https?://music\.youtube\.com/browse/(?:MPREb_|VL|MPSP)[^/?#]+",
        r"https?://music\.youtube\.com/podcast/[^/?#]+",
    ]

    def _has_auth_cookies(self) -> bool:
        return any(
            cookie.name in _AUTH_COOKIE_NAMES and bool(cookie.value)
            for cookie in extract_session_cookies(self._session)
        )

    def verify_auth(self, url: str = "") -> bool:
        """Return True if imported Google/YouTube cookies authenticate a YouTube Music session."""
        if not self._has_auth_cookies():
            return False
        try:
            html_bytes = self.get(
                f"{_YTMUSIC_BASE}/",
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        except (RequestError, UserNotAuthorized, ValueError):
            return False
        html = html_bytes.decode("utf-8", errors="replace")
        return bool(re.search(r'"LOGGED_IN"\s*:\s*true', html, re.IGNORECASE))

    @staticmethod
    def _classify_url(url: str) -> tuple[str, str]:
        """Classify a YouTube Music URL as `('track' | 'album' | 'playlist', canonical_url)`."""
        parts = urlsplit(url.strip())
        path = parts.path.rstrip("/")
        qs = parse_qs(parts.query)

        if path == "/watch":
            video_id = (qs.get("v") or [""])[0].strip()
            if not video_id:
                raise BookNotFound
            return "track", f"{_YTMUSIC_BASE}/watch?v={video_id}"

        if path == "/playlist":
            list_id = (qs.get("list") or [""])[0].strip()
            if not list_id:
                raise BookNotFound
            kind = "album" if list_id.startswith("OLAK5uy_") else "playlist"
            return kind, f"{_YTMUSIC_BASE}/playlist?list={list_id}"

        if path.startswith("/browse/"):
            browse_id = path[len("/browse/"):].split("/", 1)[0].strip()
            if not browse_id:
                raise BookNotFound
            if browse_id.startswith("MPREb_"):
                return "album", f"{_YTMUSIC_BASE}/browse/{browse_id}"
            if browse_id.startswith("VLOLAK5uy_"):
                return "album", f"{_YTMUSIC_BASE}/playlist?list={browse_id[2:]}"
            if browse_id.startswith("VL"):
                return "playlist", f"{_YTMUSIC_BASE}/playlist?list={browse_id[2:]}"
            if browse_id.startswith("MPSP"):
                return "playlist", f"{_YTMUSIC_BASE}/browse/{browse_id}"
            raise BookNotFound

        if path.startswith("/podcast/"):
            podcast_id = path[len("/podcast/"):].split("/", 1)[0].strip()
            if not podcast_id:
                raise BookNotFound
            return "playlist", f"{_YTMUSIC_BASE}/podcast/{podcast_id}"

        raise BookNotFound

    @contextmanager
    def _cookie_file_context(self) -> Iterator[str | None]:
        """Persist active session cookies to a temporary `0600` Netscape file for `yt-dlp`."""
        cookies = extract_session_cookies(self._session)
        if not cookies:
            yield None
            return
        with tempfile.TemporaryDirectory(prefix="audiobookdl_ytm_") as tmp_dir:
            cookie_path = os.path.join(tmp_dir, "cookies.txt")
            export_cookies_netscape(cookies, cookie_path)
            yield cookie_path

    def _build_ydl_opts(
        self,
        *,
        flat: bool = False,
        noplaylist: bool = True,
        cookiefile: str | None = None,
    ) -> dict[str, Any]:
        """Build isolated `yt-dlp` options respecting proxy, timeout, and session cookies."""
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": _YtdlpLogger(),
            "format": _PREFERRED_AUDIO_FORMAT,
            "noplaylist": noplaylist,
            "ignoreerrors": flat,
        }
        if flat:
            opts["extract_flat"] = "in_playlist"
        if cookiefile:
            opts["cookiefile"] = cookiefile
        proxy = getattr(self._options, "proxy", None)
        if proxy:
            opts["proxy"] = proxy
        timeout = getattr(self._options, "timeout", None)
        if timeout:
            opts["socket_timeout"] = float(timeout)
        retries = resolve_max_retries(getattr(self, "_options", None))
        opts["retries"] = retries
        opts["extractor_retries"] = retries
        return opts

    def _map_ytdlp_error(self, exc: Exception) -> Exception:
        """Translate `yt-dlp` extraction failures into `audiobookdl` domain exceptions."""
        msg = str(exc).strip() or "YouTube Music extraction failed"
        lowered = msg.lower()
        if any(marker in lowered for marker in ("sign in", "cookies", "login", "confirm your age")):
            return UserNotAuthorized(detail=msg)
        if any(marker in lowered for marker in ("private", "members-only", "premium")):
            if self.authenticated or self._has_auth_cookies():
                return MissingBookAccess(detail=msg)
            return UserNotAuthorized(detail=msg)
        if any(
            marker in lowered
            for marker in ("unavailable", "does not exist", "not exist", "removed", "http error 404")
        ):
            return BookNotFound(detail=msg)
        return RequestError(detail=msg)

    def _extract_info(
        self,
        url: str,
        *,
        flat: bool = False,
        noplaylist: bool = True,
    ) -> dict[str, Any]:
        """Run `yt_dlp.YoutubeDL.extract_info(..., download=False)` and normalize failures."""
        with self._cookie_file_context() as cookiefile:
            ydl_opts = self._build_ydl_opts(
                flat=flat,
                noplaylist=noplaylist,
                cookiefile=cookiefile,
            )
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    raw = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as exc:
                raise self._map_ytdlp_error(exc) from exc
            except Exception as exc:
                raise RequestError(detail=str(exc)) from exc
        if not isinstance(raw, dict):
            raise BookNotFound
        return raw

    def _make_stream_resolver(
        self,
        track_url: str,
        audio_file: AudiobookFile,
        initial_stream: tuple[str, dict[str, str]] | None = None,
    ) -> Callable[[], str]:
        """Create a lazy `url_resolver` that refreshes expiring stream URLs and headers."""
        cached: list[tuple[str, dict[str, str]] | None] = [initial_stream]

        def _resolve() -> str:
            if cached[0] is not None:
                stream_url, headers = cached[0]
                cached[0] = None
            else:
                info = self._extract_info(track_url, flat=False, noplaylist=True)
                stream_url, _ext, headers, _is_hls = _select_audio_stream(info)
            if headers:
                audio_file.headers.update(headers)
            return stream_url

        return _resolve

    def _build_metadata(
        self,
        info: dict[str, Any],
        *,
        scrape_url: str,
        fallback_title: str = "YouTube Music",
        series_title: str | None = None,
        series_order: int | None = None,
        fallback_authors: list[str] | None = None,
    ) -> AudiobookMetadata:
        """Build normalized `AudiobookMetadata` from a `yt-dlp` track or album info dict."""
        title = str(info.get("track") or info.get("title") or fallback_title).strip() or fallback_title
        meta = AudiobookMetadata(title=title, source="youtubemusic", scrape_url=scrape_url)

        authors = _extract_authors(info) or list(fallback_authors or [])
        if authors:
            meta.add_authors(authors)

        raw_series = series_title if series_title is not None else info.get("album")
        cleaned_series = str(raw_series).strip() if raw_series else ""
        if cleaned_series and cleaned_series.lower() != title.lower():
            meta.series = cleaned_series

        resolved_order = (
            series_order
            if series_order is not None
            else parse_series_order(info.get("track_number"))
        )
        if resolved_order is not None:
            meta.series_order = resolved_order

        raw_date = info.get("release_date") or info.get("upload_date") or info.get("release_year")
        if raw_date is not None:
            meta.release_date = parse_release_date(raw_date)

        raw_genres = info.get("categories") or info.get("genre")
        if isinstance(raw_genres, list):
            for genre in raw_genres:
                if str(genre).strip():
                    meta.add_genre(str(genre).strip())
        elif isinstance(raw_genres, str) and raw_genres.strip():
            meta.add_genre(raw_genres.strip())

        raw_lang = info.get("language")
        if raw_lang:
            meta.language = parse_language(raw_lang)

        desc = clean_html_description(info.get("description"), preserve_newlines=True)
        if desc:
            meta.description = desc

        return meta

    @staticmethod
    def _extract_video_chapters(info: dict[str, Any]) -> list[Chapter]:
        """Convert `yt-dlp` video chapter markers (`start_time` in seconds) to `Chapter`s."""
        raw_chapters = info.get("chapters")
        if not isinstance(raw_chapters, list):
            return []
        chapters: list[Chapter] = []
        for idx, entry in enumerate(raw_chapters, start=1):
            if not isinstance(entry, dict):
                continue
            start_raw = entry.get("start_time")
            if start_raw is None:
                continue
            try:
                start_sec = float(start_raw)
            except (TypeError, ValueError):
                continue
            if start_sec < 0:
                continue
            title = str(entry.get("title") or f"Chapter {idx}").strip() or f"Chapter {idx}"
            chapters.append(Chapter(start=round(start_sec * 1000), title=title))
        return chapters

    def _download_track(
        self,
        track_url: str,
        *,
        fallback_title: str = "YouTube Music Track",
        series_title: str | None = None,
        series_order: int | None = None,
        fallback_authors: list[str] | None = None,
        fallback_cover_url: str | None = None,
    ) -> Audiobook:
        """Resolve a single YouTube Music track or video into an `Audiobook`."""
        info = self._extract_info(track_url, flat=False, noplaylist=True)
        stream_url, ext, headers, is_hls = _select_audio_stream(info)
        canonical_url = _normalize_ytm_track_url(info) or track_url
        meta = self._build_metadata(
            info,
            scrape_url=canonical_url,
            fallback_title=fallback_title,
            series_title=series_title,
            series_order=series_order,
            fallback_authors=fallback_authors,
        )
        if is_hls:
            files = self.get_stream_files(stream_url, headers=headers)
        else:
            audio_file = AudiobookFile(
                url="",
                ext=ext,
                title=meta.title,
                headers=dict(headers),
            )
            audio_file.url_resolver = self._make_stream_resolver(
                canonical_url,
                audio_file,
                initial_stream=(stream_url, headers),
            )
            files = [audio_file]

        cover_url = _pick_thumbnail_url(info) or fallback_cover_url
        cover: Cover | None = self.fetch_cover_safe(cover_url)
        chapters = self._extract_video_chapters(info)
        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

    @staticmethod
    def _collect_valid_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        """Filter playlist/album entries to valid, playable items."""
        raw_entries = list(info.get("entries") or [])
        valid: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            if title.lower() in _UNAVAILABLE_TITLES:
                continue
            if _normalize_ytm_track_url(entry) is None:
                continue
            valid.append(entry)
        return valid

    def _download_album(self, album_url: str) -> Audiobook:
        """Resolve a YouTube Music album into a multi-track `Audiobook` with chapters."""
        info = self._extract_info(album_url, flat=True, noplaylist=False)
        entries = self._collect_valid_entries(info)
        if not entries:
            raise BookNotFound

        album_title = str(info.get("title") or info.get("album") or "YouTube Music Album").strip()
        meta = self._build_metadata(
            info,
            scrape_url=album_url,
            fallback_title=album_title,
            series_title="",
        )
        if not meta.authors:
            first_authors = _extract_authors(entries[0])
            if first_authors:
                meta.add_authors(first_authors)

        files: list[AudiobookFile] = []
        chapters: list[Chapter] = []
        durations_ms = [parse_duration_ms(e.get("duration")) for e in entries]
        all_have_durations = all(d is not None and d > 0 for d in durations_ms)
        running_ms = 0

        for idx, (entry, dur_ms) in enumerate(zip(entries, durations_ms, strict=True), start=1):
            track_url = _normalize_ytm_track_url(entry)
            if not track_url:
                continue
            track_title = str(entry.get("track") or entry.get("title") or f"Track {idx}").strip()
            audio_file = AudiobookFile(
                url="",
                ext="m4a",
                title=track_title,
            )
            audio_file.url_resolver = self._make_stream_resolver(track_url, audio_file)
            files.append(audio_file)
            if all_have_durations and dur_ms is not None:
                chapters.append(Chapter(start=running_ms, title=track_title))
                running_ms += dur_ms

        if not files:
            raise NoFilesFound

        cover_url = _pick_thumbnail_url(info) or _pick_thumbnail_url(entries[0])
        cover = self.fetch_cover_safe(cover_url)
        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

    def _download_playlist(self, playlist_url: str) -> Series[dict[str, Any]]:
        """Resolve a YouTube Music playlist or podcast show into a `Series` of tracks."""
        info = self._extract_info(playlist_url, flat=True, noplaylist=False)
        entries = self._collect_valid_entries(info)
        if not entries:
            raise BookNotFound

        collection_title = str(info.get("title") or "YouTube Music Playlist").strip()
        books: list[BookId[dict[str, Any]]] = []
        for idx, entry in enumerate(entries, start=1):
            track_url = _normalize_ytm_track_url(entry)
            if not track_url:
                continue
            track_title = str(entry.get("track") or entry.get("title") or f"Track {idx}").strip()
            books.append(
                BookId(
                    id={
                        "url": track_url,
                        "title": track_title,
                        "series": collection_title,
                        "series_order": idx,
                        "authors": _extract_authors(entry),
                        "thumbnail": _pick_thumbnail_url(entry),
                    },
                    title=track_title,
                )
            )
        if not books:
            raise BookNotFound
        return Series(title=collection_title, books=books)

    def download_from_id(self, book_id: dict[str, Any]) -> Audiobook:
        """Download an individual track or podcast episode from a `Series` entry."""
        track_url = str(book_id.get("url") or "").strip()
        if not track_url:
            raise BookNotFound
        return self._download_track(
            track_url,
            fallback_title=str(book_id.get("title") or "YouTube Music Track"),
            series_title=str(book_id.get("series") or "") or None,
            series_order=parse_series_order(book_id.get("series_order")),
            fallback_authors=list(book_id.get("authors") or []),
            fallback_cover_url=book_id.get("thumbnail"),
        )

    def download(self, url: str) -> Result:
        """Download a YouTube Music track, album, playlist, or podcast URL."""
        kind, canonical_url = self._classify_url(url)
        if kind == "track":
            return self._download_track(canonical_url)
        if kind == "album":
            return self._download_album(canonical_url)
        return self._download_playlist(canonical_url)

"""Audiobookshelf REST client for library inspection, book upload, and library scans."""

from collections.abc import Callable, Mapping, Sequence
import io
import mimetypes
import os
import time
from typing import Any, IO
from urllib.parse import quote as url_quote
import uuid
import requests

from audiobookdl import logging
from audiobookdl.exceptions import AudiobookDLException
from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    create_configured_http_session,
    warn_if_insecure_token_url,
)

ABS_CREDENTIAL_KEY = "integration:audiobookshelf"
_AUDIO_MEDIA_EXTENSIONS = frozenset({
    ".m4b",
    ".m4a",
    ".mp3",
    ".opus",
    ".ogg",
    ".flac",
    ".aac",
    ".wav",
    ".wma",
})
_UPLOADABLE_EXTENSIONS = _AUDIO_MEDIA_EXTENSIONS | frozenset({
    ".epub",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".json",
    ".opf",
    ".cue",
})


class AudiobookshelfError(AudiobookDLException):
    """Raised when an Audiobookshelf API request fails."""

    error_description: str = "generic"

    def __init__(self, message: str) -> None:
        super().__init__(message=message)
        self.data = {"heading": "Audiobookshelf error", "body": message}


def _normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


_UPLOAD_CHUNK_SIZE = 64 * 1024


def _escape_disposition_value(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").replace('"', '\\"')


class _MultipartUploadStream(io.RawIOBase):
    """Streaming `multipart/form-data` body with exact `Content-Length` and O(64 KiB) RAM usage."""

    def __init__(
        self,
        fields: Mapping[str, Any],
        file_entries: Sequence[tuple[str, str]],
        *,
        boundary: str | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        self.boundary = boundary or f"----audiobookdl-{uuid.uuid4().hex}"
        self._segments: list[bytes | str] = []
        total_len = 0
        b_bytes = self.boundary.encode("ascii")

        for name, raw_val in fields.items():
            safe_name = _escape_disposition_value(str(name))
            val_bytes = str(raw_val).encode("utf-8")
            part = (
                b"--" + b_bytes + b"\r\n"
                + f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode()
                + val_bytes
                + b"\r\n"
            )
            self._segments.append(part)
            total_len += len(part)

        for field_name, fpath in file_entries:
            safe_field = _escape_disposition_value(field_name)
            safe_filename = _escape_disposition_value(os.path.basename(fpath))
            mime_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
            header = (
                b"--" + b_bytes + b"\r\n"
                + (
                    f'Content-Disposition: form-data; name="{safe_field}"; '
                    f'filename="{safe_filename}"\r\n'
                ).encode()
                + f"Content-Type: {mime_type}\r\n\r\n".encode("ascii")
            )
            file_size = os.path.getsize(fpath)
            self._segments.append(header)
            self._segments.append(fpath)
            self._segments.append(b"\r\n")
            total_len += len(header) + file_size + 2

        closing = b"--" + b_bytes + b"--\r\n"
        self._segments.append(closing)
        total_len += len(closing)

        self._total_len = total_len
        self._seg_index = 0
        self._seg_offset = 0
        self._open_file: IO[bytes] | None = None
        self._on_bytes = on_bytes

    def reset(self) -> None:
        """Rewind stream position cleanly for HTTP request retries."""
        self._close_active_file()
        self._seg_index = 0
        self._seg_offset = 0

    def __len__(self) -> int:
        return self._total_len

    def readable(self) -> bool:
        return True

    def _close_active_file(self) -> None:
        if self._open_file is not None:
            try:
                self._open_file.close()
            finally:
                self._open_file = None

    def close(self) -> None:
        self._close_active_file()
        super().close()

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            return b""
        if size == 0:
            return b""
        chunks: list[bytes] = []
        remaining = size if size is not None and size > 0 else None

        while self._seg_index < len(self._segments):
            if remaining is not None and remaining <= 0:
                break
            seg = self._segments[self._seg_index]
            if isinstance(seg, bytes):
                take = len(seg) - self._seg_offset if remaining is None else min(remaining, len(seg) - self._seg_offset)
                if take > 0:
                    chunks.append(seg[self._seg_offset : self._seg_offset + take])
                    self._seg_offset += take
                    if remaining is not None:
                        remaining -= take
                if self._seg_offset >= len(seg):
                    self._seg_index += 1
                    self._seg_offset = 0
            else:
                if self._open_file is None:
                    self._open_file = open(seg, "rb")
                to_read = _UPLOAD_CHUNK_SIZE if remaining is None else min(_UPLOAD_CHUNK_SIZE, remaining)
                data = self._open_file.read(to_read)
                if data:
                    chunks.append(data)
                    if remaining is not None:
                        remaining -= len(data)
                else:
                    self._close_active_file()
                    self._seg_index += 1
                    self._seg_offset = 0
        payload = b"".join(chunks)
        if payload and self._on_bytes is not None:
            self._on_bytes(len(payload))
        return payload


class AudiobookshelfClient:
    """HTTP API client for an Audiobookshelf server."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        session: requests.Session | None = None,
        proxy: str | None = None,
        timeout: Any = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        if not self.base_url:
            raise AudiobookshelfError("Audiobookshelf server URL is empty")
        self.api_token = api_token.strip()
        if not self.api_token:
            raise AudiobookshelfError("Audiobookshelf API token is empty")
        warn_if_insecure_token_url(self.base_url, "Audiobookshelf")
        is_curl_cffi = (
            session is not None
            and (getattr(type(session), "__module__", "") or "").startswith("curl_cffi")
        )
        if session is None or is_curl_cffi:
            effective_proxy = proxy
            if effective_proxy is None and is_curl_cffi:
                raw_proxies = getattr(session, "proxies", None)
                if isinstance(raw_proxies, Mapping):
                    effective_proxy = raw_proxies.get("https") or raw_proxies.get("http")
            self._owns_session = True
            self.session = create_configured_http_session(proxy=effective_proxy)
        else:
            self._owns_session = False
            self.session = session
        self.timeout = timeout

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    _RETRIABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: Any = None,
        files: list[tuple[str, tuple[str, Any]]] | None = None,
        headers: Mapping[str, str] | None = None,
        max_retries: int = 2,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        req_headers = self._headers()
        if headers:
            req_headers.update(headers)
        can_retry = data is None or isinstance(data, (bytes, bytearray, str)) or hasattr(data, "reset")
        attempts = max(1, (max_retries + 1) if can_retry else 1)
        last_exc: requests.RequestException | None = None
        resp: requests.Response | None = None

        for attempt in range(attempts):
            if attempt > 0 and hasattr(data, "reset"):
                data.reset()
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=req_headers,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                    continue
                raise AudiobookshelfError(
                    f"Audiobookshelf request failed ({method} {path}): {exc}"
                ) from exc
            if resp.status_code in self._RETRIABLE_STATUS_CODES and attempt + 1 < attempts:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                continue
            break

        if resp is None:
            raise AudiobookshelfError(
                f"Audiobookshelf request failed ({method} {path}): {last_exc}"
            )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:300]
            raise AudiobookshelfError(
                f"Audiobookshelf API returned HTTP {resp.status_code} for {method} {path}: {detail}"
            )
        return resp

    def book_exists_in_library(
        self,
        library_id: str,
        metadata: AudiobookMetadata | None,
    ) -> bool:
        """Return True if a matching ASIN, ISBN, or title+author already exists in `library_id`."""
        if not library_id.strip() or metadata is None:
            return False
        query = (metadata.asin or metadata.isbn or metadata.title or "").strip()
        if not query:
            return False
        try:
            resp = self._request(
                "GET",
                f"/api/libraries/{library_id}/search?q={url_quote(query)}",
            )
            payload = resp.json()
        except (AudiobookshelfError, ValueError):
            return False
        items = payload.get("book") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return False

        target_asin = (metadata.asin or "").strip().lower()
        target_isbn = (metadata.isbn or "").strip().replace("-", "").lower()
        target_title = " ".join((metadata.title or "").casefold().split())
        target_author = " ".join((metadata.author or "").casefold().split())
        for entry in items:
            lib_item = entry.get("libraryItem") if isinstance(entry, dict) else None
            media = lib_item.get("media") if isinstance(lib_item, dict) else None
            meta = media.get("metadata") if isinstance(media, dict) else None
            if not isinstance(meta, dict):
                continue
            item_asin = str(meta.get("asin") or "").strip().lower()
            item_isbn = str(meta.get("isbn") or "").strip().replace("-", "").lower()
            if target_asin and item_asin and item_asin == target_asin:
                return True
            if target_isbn and item_isbn and item_isbn == target_isbn:
                return True
            item_title = " ".join(str(meta.get("title") or "").casefold().split())
            if target_title and item_title == target_title:
                item_author = " ".join(str(meta.get("authorName") or "").casefold().split())
                if not target_author or not item_author or target_author == item_author:
                    return True
        return False

    def ping(self) -> str:
        """Verify credentials via `/api/authorize` and return authenticated username."""
        resp = self._request("POST", "/api/authorize")
        try:
            payload = resp.json()
        except ValueError:
            return "authenticated"
        if isinstance(payload, dict):
            user = payload.get("user")
            if isinstance(user, dict) and user.get("username"):
                return str(user["username"])
        return "authenticated"

    def list_libraries(self) -> list[tuple[str, str]]:
        """Return `(library_id, name)` tuples configured on the server."""
        resp = self._request("GET", "/api/libraries")
        payload = resp.json()
        libraries = payload.get("libraries") if isinstance(payload, dict) else []
        out: list[tuple[str, str]] = []
        if isinstance(libraries, list):
            for lib in libraries:
                if isinstance(lib, dict) and lib.get("id"):
                    out.append((str(lib["id"]), str(lib.get("name") or lib["id"])))
        return out

    def get_library_folder_id(self, library_id: str) -> str:
        """Resolve the primary root folder ID for `library_id`."""
        resp = self._request("GET", f"/api/libraries/{library_id}")
        payload = resp.json()
        if isinstance(payload, dict):
            lib = payload.get("library") if isinstance(payload.get("library"), dict) else payload
            folders = lib.get("folders") if isinstance(lib, dict) else None
            if isinstance(folders, list):
                for folder in folders:
                    if isinstance(folder, dict) and folder.get("id"):
                        return str(folder["id"])
        raise AudiobookshelfError(f"Library '{library_id}' has no configured folder")

    def scan_library(self, library_id: str, *, force: bool = False) -> bool:
        """Trigger a library scan on Audiobookshelf."""
        if not library_id.strip():
            raise AudiobookshelfError("Audiobookshelf library ID is required to scan")
        suffix = "?force=1" if force else ""
        self._request("POST", f"/api/libraries/{library_id}/scan{suffix}")
        return True

    def _collect_upload_files(self, local_path: str) -> list[str]:
        abs_path = os.path.abspath(local_path)
        if not os.path.exists(abs_path):
            raise AudiobookshelfError(f"Upload path does not exist: {local_path}")
        if os.path.isfile(abs_path):
            files = [abs_path]
            stem = os.path.splitext(abs_path)[0]
            stem_name = os.path.splitext(os.path.basename(abs_path))[0]
            parent = os.path.dirname(abs_path)
            stem_metadata_path = f"{stem}.metadata.json"
            candidates: list[str] = [
                f"{abs_path}.json",
                stem_metadata_path,
                f"{stem}.opf",
                f"{stem}.cue",
                f"{stem}.jpg",
                f"{stem}.jpeg",
                f"{stem}.png",
                f"{stem}.webp",
            ]
            has_sibling_audio = False
            try:
                for entry_name in os.listdir(parent):
                    entry_stem, entry_ext = os.path.splitext(entry_name)
                    if (
                        entry_ext.lower() in _AUDIO_MEDIA_EXTENSIONS
                        and entry_stem.lower() != stem_name.lower()
                        and os.path.isfile(os.path.join(parent, entry_name))
                    ):
                        has_sibling_audio = True
                        break
            except OSError:
                has_sibling_audio = False
            if not has_sibling_audio and not os.path.isfile(stem_metadata_path):
                candidates.extend(
                    [
                        os.path.join(parent, "metadata.json"),
                        os.path.join(parent, "cover.jpg"),
                        os.path.join(parent, "cover.jpeg"),
                        os.path.join(parent, "cover.png"),
                        os.path.join(parent, "cover.webp"),
                    ]
                )
            for candidate in candidates:
                if os.path.isfile(candidate) and candidate not in files:
                    files.append(candidate)
            return files

        collected: list[str] = []
        for walk_root, _dirs, names in os.walk(abs_path):
            for name in sorted(names):
                ext = os.path.splitext(name)[1].lower()
                if ext in _UPLOADABLE_EXTENSIONS:
                    collected.append(os.path.join(walk_root, name))
        if not collected:
            raise AudiobookshelfError(f"No audio or sidecar files found under: {local_path}")
        return collected

    def upload_book(
        self,
        local_path: str,
        *,
        library_id: str,
        metadata: AudiobookMetadata | None = None,
        folder_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        skip_if_exists: bool = False,
    ) -> bool:
        """Upload local audiobook files and sidecars via `POST /api/upload`."""
        if not library_id.strip():
            raise AudiobookshelfError("Audiobookshelf library ID is required to upload")
        if skip_if_exists and self.book_exists_in_library(library_id, metadata):
            logging.log(
                f"Skipping Audiobookshelf upload for '{getattr(metadata, 'title', local_path)}': already in library"
            )
            return False
        resolved_folder = folder_id or self.get_library_folder_id(library_id)
        filepaths = self._collect_upload_files(local_path)
        form_data: dict[str, Any] = {
            "library": library_id,
            "folder": resolved_folder,
            "title": (metadata.title if metadata and metadata.title else os.path.basename(local_path)),
        }
        if metadata is not None:
            if metadata.author:
                form_data["author"] = metadata.author
            if metadata.series:
                form_data["series"] = metadata.series

        file_entries = [(str(idx), fpath) for idx, fpath in enumerate(filepaths)]
        uploaded_bytes = 0
        total_len = 0

        def _track_bytes(delta: int) -> None:
            nonlocal uploaded_bytes
            uploaded_bytes += delta
            if on_progress is not None and total_len > 0:
                on_progress(min(uploaded_bytes, total_len), total_len)

        stream = _MultipartUploadStream(
            form_data,
            file_entries,
            on_bytes=_track_bytes if on_progress is not None else None,
        )
        total_len = len(stream)
        if on_progress is not None and total_len > 0:
            on_progress(0, total_len)
        try:
            self._request(
                "POST",
                "/api/upload",
                data=stream,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={stream.boundary}",
                    "Content-Length": str(total_len),
                },
            )
        finally:
            stream.close()
        return True


def sync_book_to_audiobookshelf(
    local_path: str,
    metadata: AudiobookMetadata | None,
    abs_config: Any,
    *,
    force_upload: bool = False,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: Any = DEFAULT_HTTP_TIMEOUT,
    skip_if_exists: bool = False,
) -> bool:
    """Upload and/or trigger a library scan on Audiobookshelf when configured.

    Returns True when an API action (upload or scan) was performed.
    """
    if abs_config is None:
        return False
    url = str(getattr(abs_config, "url", "") or "").strip()
    token = str(getattr(abs_config, "api_key", "") or "").strip()
    library_id = str(getattr(abs_config, "library_id", "") or "").strip()
    auto_upload = bool(getattr(abs_config, "auto_upload", False))
    auto_scan = bool(getattr(abs_config, "auto_scan", False))
    want_upload = force_upload or auto_upload
    if not (url and token and library_id):
        if force_upload:
            raise AudiobookshelfError(
                "Audiobookshelf upload requested, but url/library_id/api_key are not configured."
            )
        return False
    if not (want_upload or auto_scan):
        return False

    client = AudiobookshelfClient(url, token, session=session, proxy=proxy, timeout=timeout)
    try:
        if want_upload:
            logging.book_update("Uploading to Audiobookshelf")
            with logging.progress() as prog:
                task_id: int | None = None
                reported = 0

                def _on_upload_progress(sent: int, total: int) -> None:
                    nonlocal task_id, reported
                    if total <= 0:
                        return
                    if task_id is None:
                        task_id = prog.add_task("Uploading to Audiobookshelf", total=float(total))
                    delta = sent - reported
                    if delta > 0:
                        reported = sent
                        prog.advance(task_id, float(delta))

                client.upload_book(
                    local_path,
                    library_id=library_id,
                    metadata=metadata,
                    on_progress=_on_upload_progress,
                    skip_if_exists=skip_if_exists,
                )
        if auto_scan:
            client.scan_library(library_id)
        return True
    finally:
        client.close()

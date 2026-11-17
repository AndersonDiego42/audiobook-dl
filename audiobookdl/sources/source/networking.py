from audiobookdl import AudiobookFile, exceptions, logging
from audiobookdl.utils.audiobook import AESEncryption
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    extract_session_cookies,
    is_transient_network_error,
)

import json
import os
import random
import time
from urllib.parse import urlparse
import m3u8
import requests


RETRIABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
MAX_RETRY_DELAY = 30.0


def _retry_delay_seconds(attempt: int, resp: requests.Response | None) -> float:
    """Compute retry backoff with jitter, honoring numeric `Retry-After` when present."""
    if resp is not None:
        raw_retry = resp.headers.get("Retry-After") if hasattr(resp, "headers") and resp.headers else None
        if isinstance(raw_retry, str) and raw_retry.strip().isdigit():
            return min(MAX_RETRY_DELAY * 2, float(int(raw_retry.strip())))
    base = min(MAX_RETRY_DELAY, 0.5 * (2 ** attempt))
    return base + random.uniform(0.0, 0.25 * (attempt + 1))


def _sleep_cancellable(seconds: float) -> None:
    if seconds <= 0:
        return
    ctx = logging.get_job_context()
    cancel_event = ctx.cancel_event if ctx is not None else None
    if cancel_event is not None:
        if cancel_event.wait(seconds):
            raise exceptions.DownloadCancelled
    else:
        time.sleep(seconds)


def request(
    self,
    method: str,
    url: str,
    *,
    force_cookies: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    **kwargs,
) -> requests.Response:
    """Make an HTTP request with retry/backoff for transient errors and configurable timeout."""
    if "timeout" in kwargs:
        timeout = kwargs.pop("timeout")
    else:
        opt_timeout = getattr(getattr(self, "_options", None), "timeout", None)
        timeout = (DEFAULT_HTTP_TIMEOUT[0], float(opt_timeout)) if opt_timeout else DEFAULT_HTTP_TIMEOUT
    if force_cookies:
        kwargs["cookies"] = _get_all_cookies(self._session)

    upper_method = method.upper()

    def _send() -> requests.Response:
        if upper_method == "GET":
            return self._session.get(url, timeout=timeout, **kwargs)
        if upper_method == "POST":
            return self._session.post(url, timeout=timeout, **kwargs)
        return self._session.request(method, url, timeout=timeout, **kwargs)

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = _send()
        except Exception as exc:
            if not is_transient_network_error(exc):
                raise
            last_exc = exc
            if attempt >= max_retries:
                raise exceptions.RequestError from exc
            delay = _retry_delay_seconds(attempt, None)
            logging.debug(
                f"Transient network error for {upper_method} {url} ({exc}); retrying in {delay:.2f}s"
            )
            _sleep_cancellable(delay)
            continue

        if resp.status_code in RETRIABLE_STATUS_CODES and attempt < max_retries:
            delay = _retry_delay_seconds(attempt, resp)
            logging.debug(
                f"HTTP {resp.status_code} for {upper_method} {url}; retrying in {delay:.2f}s"
            )
            try:
                resp.close()
            except Exception:
                pass
            _sleep_cancellable(delay)
            continue
        return resp

    if last_exc is not None:
        raise exceptions.RequestError from last_exc
    raise exceptions.RequestError


def _safe_url_label(url: str) -> str:
    """Return `scheme://host/path` without query tokens or fragments."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    return url.split("?", 1)[0]


def _raise_for_failed_response(url: str, resp: requests.Response) -> None:
    logging.debug(f"Failed to download data from: {url}\nResponse:\n{resp.content}")
    detail = f"HTTP {resp.status_code} from {_safe_url_label(url)}"
    if resp.status_code in (401, 403):
        raise exceptions.UserNotAuthorized(detail=detail)
    raise exceptions.RequestError(detail=detail)


def post(self, url: str, **kwargs) -> bytes:
    """Make post request with `Source` session"""
    resp = self.request("POST", url, **kwargs)
    if resp.status_code == 200:
        return resp.content
    _raise_for_failed_response(url, resp)
    raise exceptions.RequestError  # unreachable, satisfies type checker


def get(self, url: str, force_cookies: bool = False, **kwargs) -> bytes:
    """Make get request with `Source` session"""
    resp = self.request("GET", url, force_cookies=force_cookies, **kwargs)
    if resp.status_code == 200:
        return resp.content
    _raise_for_failed_response(url, resp)
    raise exceptions.RequestError  # unreachable, satisfies type checker


def post_json(self, url: str, **kwargs) -> dict:
    """Downloads data with the given url and converts it to json"""
    resp = self.post(url, **kwargs)
    return json.loads(resp.decode('utf8'))


def get_json(self, url: str, **kwargs) -> dict:
    """Downloads data with the given url and converts it to json"""
    resp = self.get(url, **kwargs)
    return json.loads(resp.decode('utf8'))


def _segment_extension(uri: str) -> str:
    path = urlparse(uri).path
    return os.path.splitext(path)[1][1:]


def _parse_hls_byterange(raw: str, default_offset: int | None = None) -> tuple[int, int]:
    """Parse an HLS `BYTERANGE` value (`len@offset` or `len`) into `(start, end)` inclusive."""
    text = raw.strip()
    if "@" in text:
        length_str, offset_str = text.split("@", 1)
        length = int(length_str)
        start = int(offset_str)
    else:
        if default_offset is None:
            raise ValueError(f"HLS BYTERANGE {raw!r} omits offset with no prior segment offset")
        length = int(text)
        start = default_offset
    if length <= 0 or start < 0:
        raise ValueError(f"Invalid HLS BYTERANGE {raw!r}")
    return start, start + length - 1


def get_stream_files(
    self,
    url: str,
    headers: dict[str, str] | None = None,
    extension=None,
    expected_content_type="application/octet-stream",
) -> list[AudiobookFile]:
    """Creates a list of audio files from an m3u8 file"""
    base_headers = dict(headers) if headers else {}
    content = self.get(url, headers=base_headers).decode("utf-8")
    playlist = m3u8.loads(content, uri=url)

    # Resolve master/variant playlists to the highest-bandwidth stream.
    seen_urls = {url}
    while playlist.is_variant and playlist.playlists:
        best = max(
            playlist.playlists,
            key=lambda p: (p.stream_info.bandwidth or 0) if p.stream_info else 0,
        )
        next_url = best.absolute_uri
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        content = self.get(next_url, headers=base_headers).decode("utf-8")
        playlist = m3u8.loads(content, uri=next_url)

    media_sequence = playlist.media_sequence or 0
    files = []
    init_cache: dict[tuple[str, str | None], bytes] = {}
    key_cache: dict[str, bytes] = {}
    last_byterange_end: dict[str, int] = {}

    unique_key_uris = list(
        dict.fromkeys(
            str(seg.key.absolute_uri)
            for seg in playlist.segments
            if seg.key is not None
            and getattr(seg.key, "method", None) not in (None, "NONE")
            and getattr(seg.key, "absolute_uri", None)
        )
    )
    if len(unique_key_uris) > 1:
        fetched_keys = run_cancellable_pool(
            lambda uri: self._get_page(uri, headers=base_headers),
            unique_key_uris,
            max_workers=min(8, len(unique_key_uris)),
        )
        key_cache.update(zip(unique_key_uris, fetched_keys, strict=True))

    for idx, seg in enumerate(playlist.segments):
        seg_ext = extension if extension is not None else _segment_extension(seg.absolute_uri)
        seg_headers = dict(base_headers)

        raw_byterange = getattr(seg, "byterange", None)
        if raw_byterange:
            prev_end = last_byterange_end.get(seg.absolute_uri)
            start, end = _parse_hls_byterange(str(raw_byterange), default_offset=prev_end)
            last_byterange_end[seg.absolute_uri] = end + 1
            seg_headers["Range"] = f"bytes={start}-{end}"

        init_bytes: bytes | None = None
        init_section = getattr(seg, "init_section", None)
        if init_section is not None and getattr(init_section, "absolute_uri", None):
            init_uri = str(init_section.absolute_uri)
            init_range = getattr(init_section, "byterange", None)
            cache_key = (init_uri, str(init_range) if init_range else None)
            if cache_key not in init_cache:
                init_headers = dict(base_headers)
                if init_range:
                    init_start, init_end = _parse_hls_byterange(str(init_range), default_offset=0)
                    init_headers["Range"] = f"bytes={init_start}-{init_end}"
                    resp = self.request("GET", init_uri, headers=init_headers)
                    if resp.status_code not in (200, 206):
                        _raise_for_failed_response(init_uri, resp)
                    init_cache[cache_key] = resp.content
                else:
                    init_cache[cache_key] = self.get(init_uri, headers=init_headers)
            init_bytes = init_cache[cache_key]

        current = AudiobookFile(
            url=seg.absolute_uri,
            ext=seg_ext,
            headers=seg_headers,
            expected_content_type=expected_content_type,
            init_section=init_bytes,
        )
        if seg.key is not None and getattr(seg.key, "method", None) not in (None, "NONE"):
            if seg.key.iv is not None:
                iv_bytes = int(seg.key.iv, 0).to_bytes(16, byteorder="big")
            else:
                iv_bytes = (media_sequence + idx).to_bytes(16, byteorder="big")
            key_uri = str(seg.key.absolute_uri)
            if key_uri not in key_cache:
                key_cache[key_uri] = self._get_page(key_uri, headers=base_headers)
            current.encryption_method = AESEncryption(
                key=key_cache[key_uri],
                iv=iv_bytes,
            )
        files.append(current)
    return files


def _get_all_cookies(session: requests.Session) -> dict[str, str]:
    """
    Retrieves all cookies from session

    :returns: Dictionary of cookies
    """
    return {
        cookie.name: str(cookie.value)
        for cookie in extract_session_cookies(session)
    }

from audiobookdl import AudiobookFile, exceptions, logging
from audiobookdl.utils.audiobook import (
    AESCBCEncryption,
    AESGCMEncryption,
    CENCDecryption,
    SampleAESEncryption,
    parse_decryption_key_spec,
    reconcile_cenc_decryption,
)
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.pssh import (
    PsshBox,
    PsshParseError,
    extract_cenc_kids_safe,
    parse_hls_key_data,
    parse_pssh_boxes,
)
from audiobookdl.utils.http import (
    AUTH_HEADER_NAMES,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    RETRIABLE_CDN_STATUS_CODES,
    compute_retry_delay,
    extract_session_cookies,
    is_cloudflare_challenge,
    is_transient_network_error,
    read_bounded_prefix,
    resolve_max_retries,
    sleep_or_cancel,
)

import base64
from collections.abc import MutableMapping
import json
import os
import threading
import time
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse
import m3u8
import requests


RETRIABLE_STATUS_CODES = RETRIABLE_CDN_STATUS_CODES


def _is_curl_cffi_session(session: Any) -> bool:
    return (getattr(type(session), "__module__", "") or "").startswith("curl_cffi")


def _neutralize_session_auth_headers(kwargs: dict[str, Any]) -> None:
    """Force session-level auth headers off for this request via `None` header values.

    `requests` drops headers explicitly set to `None`, which removes matching
    `session.headers` entries (e.g. `Authorization`, `X-Login-Token`) from the
    wire request while preserving explicitly provided per-request headers.
    """
    req_headers = dict(kwargs.get("headers") or {})
    present = {str(k).lower() for k in req_headers}
    for name in AUTH_HEADER_NAMES:
        if name not in present:
            req_headers[name] = None
    kwargs["headers"] = req_headers


def request(
    self,
    method: str,
    url: str,
    *,
    force_cookies: bool = False,
    credentials: bool = True,
    max_retries: int | None = None,
    **kwargs,
) -> requests.Response:
    """Make an HTTP request with retry/backoff for transient errors and configurable timeout.

    :param credentials: when False, `self._session` auth headers/cookies are not
        sent to the target host (third-party fetches such as CDN/cover URLs)
    """
    if "timeout" in kwargs:
        timeout = kwargs.pop("timeout")
    else:
        opt_timeout = getattr(getattr(self, "_options", None), "timeout", None)
        timeout = (DEFAULT_HTTP_TIMEOUT[0], float(opt_timeout)) if opt_timeout else DEFAULT_HTTP_TIMEOUT
    session = self._session
    neutralize_auth = False
    if not credentials:
        if _is_curl_cffi_session(session):
            # curl_cffi does not support `None` header removal; use a clean session instead.
            session = self._get_unauthenticated_session()
        else:
            neutralize_auth = True
    if force_cookies and credentials:
        kwargs["cookies"] = _get_all_cookies(self._session)
    if neutralize_auth:
        _neutralize_session_auth_headers(kwargs)

    effective_retries = (
        max(0, int(max_retries))
        if max_retries is not None and not isinstance(max_retries, bool)
        else resolve_max_retries(getattr(self, "_options", None), default=DEFAULT_MAX_RETRIES)
    )
    upper_method = method.upper()

    def _send() -> requests.Response:
        if upper_method == "GET":
            return session.get(url, timeout=timeout, **kwargs)
        if upper_method == "POST":
            return session.post(url, timeout=timeout, **kwargs)
        return session.request(method, url, timeout=timeout, **kwargs)

    def _cancel_event() -> threading.Event | None:
        ctx = logging.get_job_context()
        return ctx.cancel_event if ctx is not None else None

    last_exc: BaseException | None = None
    for attempt in range(effective_retries + 1):
        cancel_event = _cancel_event()
        if cancel_event is not None and cancel_event.is_set():
            raise exceptions.DownloadCancelled
        try:
            resp = _send()
        except Exception as exc:
            if not is_transient_network_error(exc):
                raise
            last_exc = exc
            if attempt >= effective_retries:
                raise exceptions.RequestError from exc
            delay = compute_retry_delay(attempt)
            logging.debug(
                f"Transient network error for {upper_method} {url} ({exc}); retrying in {delay:.2f}s"
            )
            sleep_or_cancel(delay, _cancel_event(), sleep_fn=time.sleep)
            continue

        cancel_event = _cancel_event()
        if cancel_event is not None and cancel_event.is_set():
            try:
                resp.close()
            except Exception:
                pass
            raise exceptions.DownloadCancelled

        if is_cloudflare_challenge(resp):
            try:
                resp.close()
            except Exception:
                pass
            raise exceptions.CloudflareBlocked(
                detail=(
                    f"HTTP {resp.status_code} Cloudflare challenge from {_safe_url_label(url)}. "
                    "Try setting Impersonate (e.g. chrome) in Options -> Network & Performance "
                    "or importing fresh browser cookies on the Accounts page."
                )
            )

        if resp.status_code in RETRIABLE_STATUS_CODES and attempt < effective_retries:
            raw_headers = getattr(resp, "headers", None)
            delay = compute_retry_delay(attempt, headers=raw_headers)
            logging.debug(
                f"HTTP {resp.status_code} for {upper_method} {url}; retrying in {delay:.2f}s"
            )
            try:
                resp.close()
            except Exception:
                pass
            sleep_or_cancel(delay, _cancel_event(), sleep_fn=time.sleep)
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
    if is_cloudflare_challenge(resp):
        raise exceptions.CloudflareBlocked(
            detail=(
                f"HTTP {resp.status_code} Cloudflare challenge from {_safe_url_label(url)}. "
                "Try setting Impersonate (e.g. chrome) in Options -> Network & Performance "
                "or importing fresh browser cookies on the Accounts page."
            )
        )
    try:
        body_preview = read_bounded_prefix(resp, 4096)
    except Exception:
        body_preview = b""
    logging.debug(
        f"Failed to download data from: {url}\nResponse:\n{body_preview!r}"
    )
    detail = f"HTTP {resp.status_code} from {_safe_url_label(url)}"
    if resp.status_code in (401, 403):
        raise exceptions.UserNotAuthorized(detail=detail)
    raise exceptions.RequestError(detail=detail)


def post(self, url: str, credentials: bool = True, **kwargs) -> bytes:
    """Make post request with `Source` session"""
    resp = self.request("POST", url, credentials=credentials, **kwargs)
    if 200 <= resp.status_code < 300:
        return resp.content
    _raise_for_failed_response(url, resp)
    raise exceptions.RequestError  # unreachable, satisfies type checker


def get(self, url: str, force_cookies: bool = False, credentials: bool = True, **kwargs) -> bytes:
    """Make get request with `Source` session"""
    resp = self.request("GET", url, force_cookies=force_cookies, credentials=credentials, **kwargs)
    if 200 <= resp.status_code < 300:
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


def _decode_key_data_uri(uri: str) -> bytes | None:
    """Decode an inline `data:` HLS key URI into raw key bytes, or return `None` for non-data URIs."""
    if not uri.lower().startswith("data:"):
        return None
    header, sep, payload = uri.partition(",")
    if not sep or not payload:
        raise exceptions.GenericAudiobookDLException("Invalid inline data: URI in HLS #EXT-X-KEY")
    if ";base64" in header.lower():
        return base64.b64decode(payload)
    return unquote_to_bytes(payload)


def _decode_playlist_text(raw: bytes) -> str:
    """Decode an HLS playlist payload tolerating UTF-8 BOMs and invalid byte sequences."""
    return raw.decode("utf-8-sig", errors="replace")


def _language_match_rank(candidate_lang: object, preferred_locale: str | None) -> int:
    """Rank how closely an HLS rendition `LANGUAGE` tag matches `preferred_locale` (lower is better)."""
    if not preferred_locale:
        return 0
    pref_norm = preferred_locale.strip().replace("_", "-").lower()
    if not pref_norm:
        return 0
    cand_norm = str(candidate_lang or "").strip().replace("_", "-").lower()
    if not cand_norm:
        return 2
    if cand_norm == pref_norm:
        return 0
    if cand_norm.split("-", 1)[0] == pref_norm.split("-", 1)[0]:
        return 1
    return 3


def _select_variant_playlist_uri(
    playlist: m3u8.M3U8,
    *,
    prefer_video: bool = False,
    locale: str | None = None,
) -> str | None:
    """Select the preferred audio rendition URI (unless `prefer_video`) else highest-bandwidth variant."""
    best = max(
        playlist.playlists,
        key=lambda p: (p.stream_info.bandwidth or 0) if p.stream_info else 0,
    )
    if prefer_video:
        return best.absolute_uri
    stream_info = getattr(best, "stream_info", None)
    audio_group = getattr(stream_info, "audio", None) if stream_info else None
    media_list = getattr(playlist, "media", None) or []
    if audio_group or media_list:
        audio_candidates = [
            m
            for m in media_list
            if str(getattr(m, "type", "") or "").upper() == "AUDIO"
            and getattr(m, "absolute_uri", None)
            and (not audio_group or getattr(m, "group_id", None) == audio_group)
        ]
        if audio_candidates:
            audio_candidates.sort(
                key=lambda m: (
                    _language_match_rank(getattr(m, "language", None), locale),
                    0 if str(getattr(m, "default", "") or "").upper() == "YES" else 1,
                    0 if str(getattr(m, "autoselect", "") or "").upper() == "YES" else 1,
                )
            )
            return str(audio_candidates[0].absolute_uri)
    return best.absolute_uri


_SUPPORTED_HLS_KEY_METHODS = frozenset(
    {
        "AES-128",
        "AES-256",
        "AES-256-GCM",
        "SAMPLE-AES",
        "SAMPLE-AES-CTR",
        "SAMPLE-AES-CENC",
    }
)
_W3C_CLEARKEY_FORMAT = "urn:uuid:1077efec-c0b2-4d02-ace3-3c1e52e2fb4b"
_FMP4_EXTENSIONS = frozenset({"m4s", "mp4", "m4a", "m4b", "m4v", "mov"})


def _hls_keyformat(key_obj) -> str:
    return str(getattr(key_obj, "keyformat", None) or "identity").strip()


def _parse_hls_iv(raw_iv: str | None, sequence_num: int, *, byte_length: int = 16) -> bytes:
    if raw_iv is None:
        return sequence_num.to_bytes(byte_length, byteorder="big")
    value = int(raw_iv, 0)
    # Trim leading zero bytes if a 16-byte hex literal was supplied for a 12-byte GCM nonce, or fit exact size
    needed = max(byte_length, (value.bit_length() + 7) // 8)
    raw_bytes = value.to_bytes(needed, byteorder="big")
    if byte_length == 12 and len(raw_bytes) == 16 and raw_bytes[:4] == b"\x00\x00\x00\x00":
        return raw_bytes[4:]
    return raw_bytes


def _extract_hls_key_attribute_kid(key_obj) -> tuple[bytes, ...]:
    raw_kid = getattr(key_obj, "keyid", None)
    if not raw_kid:
        extra = getattr(key_obj, "_extra_params", None)
        if isinstance(extra, dict):
            raw_kid = extra.get("keyid")
    if not isinstance(raw_kid, str):
        return ()
    cleaned = raw_kid.strip().lower().removeprefix("0x").replace("-", "")
    if len(cleaned) != 32:
        return ()
    try:
        return (bytes.fromhex(cleaned),)
    except ValueError:
        return ()


def _extract_hls_key_pssh_boxes(
    key_obj,
    fmt_lower: str,
    init_bytes: bytes | None = None,
) -> list[PsshBox]:
    boxes: list[PsshBox] = []
    key_uri = str(getattr(key_obj, "absolute_uri", None) or getattr(key_obj, "uri", None) or "").strip()
    if key_uri.lower().startswith("data:"):
        try:
            boxes.extend(parse_hls_key_data(key_uri, key_format=fmt_lower))
        except PsshParseError:
            pass
    if init_bytes:
        try:
            boxes.extend(parse_pssh_boxes(init_bytes))
        except PsshParseError:
            pass
    return boxes


def _extract_hls_cenc_kids(
    key_obj,
    fmt_lower: str,
    init_bytes: bytes | None = None,
) -> tuple[bytes, ...]:
    boxes = _extract_hls_key_pssh_boxes(key_obj, fmt_lower, init_bytes)
    kids: list[bytes] = []
    for box in boxes:
        kids.extend(box.key_ids)
    kids.extend(_extract_hls_key_attribute_kid(key_obj))
    if init_bytes:
        kids.extend(extract_cenc_kids_safe(init_bytes))
    return tuple(dict.fromkeys(kids))


def _format_hls_drm_error(
    key_obj,
    key_format: str,
    init_bytes: bytes | None = None,
) -> str:
    fmt_lower = key_format.lower()
    boxes = _extract_hls_key_pssh_boxes(key_obj, fmt_lower, init_bytes)
    kids = _extract_hls_cenc_kids(key_obj, fmt_lower, init_bytes)
    if boxes:
        systems = ", ".join(dict.fromkeys(box.system_name for box in boxes))
        if kids:
            kids_hex = ", ".join(kid.hex() for kid in kids)
            return (
                f"HLS stream is protected by {systems} (KEYFORMAT '{key_format}', KID {kids_hex}); "
                "supply decryption_key as KID:KEY"
            )
        return (
            f"HLS stream is protected by {systems} (KEYFORMAT '{key_format}'); "
            "supply decryption_key"
        )
    if kids:
        kids_hex = ", ".join(kid.hex() for kid in kids)
        return f"Unsupported or DRM-protected HLS KEYFORMAT '{key_format}' (KID {kids_hex})"
    return f"Unsupported or DRM-protected HLS KEYFORMAT '{key_format}'"


def _validate_hls_key(
    key_obj,
    *,
    has_user_key: bool,
    init_bytes: bytes | None = None,
) -> tuple[str, str]:
    key_method = str(getattr(key_obj, "method", "") or "").strip().upper()
    if key_method not in _SUPPORTED_HLS_KEY_METHODS:
        raise exceptions.GenericAudiobookDLException(
            f"Unsupported HLS encryption method '{key_method}'"
        )
    key_format = _hls_keyformat(key_obj)
    fmt_lower = key_format.lower()
    if fmt_lower not in ("identity", _W3C_CLEARKEY_FORMAT):
        if key_method in ("SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC") and has_user_key:
            return key_method, fmt_lower
        raise exceptions.GenericAudiobookDLException(
            _format_hls_drm_error(key_obj, key_format, init_bytes=init_bytes)
        )
    return key_method, fmt_lower


def _load_m3u8_playlist(
    source_obj: Any,
    target_url: str,
    headers: dict[str, str],
    *,
    credentials: bool = True,
) -> m3u8.M3U8:
    """Fetch an HLS playlist via `source_obj`, honoring post-redirect URLs and rejecting CDN error pages."""
    if "get" in getattr(source_obj, "__dict__", {}) or getattr(type(source_obj), "get", None) is not get:
        raw_bytes = source_obj.get(target_url, headers=headers, credentials=credentials)
        resolved_url = target_url
        resp_obj: Any = None
    else:
        resp_obj = source_obj.request("GET", target_url, headers=headers, credentials=credentials)
        if resp_obj.status_code != 200:
            _raise_for_failed_response(target_url, resp_obj)
        raw_bytes = resp_obj.content
        candidate_url = getattr(resp_obj, "url", None)
        resolved_url = (
            candidate_url
            if isinstance(candidate_url, str) and candidate_url.startswith(("http://", "https://"))
            else target_url
        )
    content = _decode_playlist_text(raw_bytes)
    stripped = content.lstrip()
    if stripped and not stripped.startswith("#EXT") and stripped.startswith(("<", "{")):
        if is_cloudflare_challenge(resp_obj, body=content):
            raise exceptions.CloudflareBlocked(
                detail=(
                    f"Cloudflare challenge returned instead of HLS playlist from {_safe_url_label(resolved_url)}. "
                    "Try setting Impersonate (e.g. chrome) in Options -> Network & Performance "
                    "or importing fresh browser cookies on the Accounts page."
                )
            )
        raise exceptions.RequestError(
            detail=f"Unexpected non-M3U8 playlist response from {_safe_url_label(resolved_url)}"
        )
    return m3u8.loads(content, uri=resolved_url)


def get_stream_files(
    self,
    url: str,
    headers: dict[str, str] | None = None,
    extension=None,
    expected_content_type="application/octet-stream",
    *,
    prefer_video: bool = False,
    credentials: bool = True,
) -> list[AudiobookFile]:
    """Creates a list of stream segment files from an m3u8 playlist"""
    base_headers = dict(headers) if headers else {}
    raw_dec_key = getattr(getattr(self, "_options", None), "decryption_key", None)
    user_default_key: bytes | None = None
    user_key_map: dict[bytes, bytes] = {}
    if isinstance(raw_dec_key, str) and raw_dec_key.strip():
        user_default_key, user_key_map = parse_decryption_key_spec(raw_dec_key)
    has_user_key = user_default_key is not None or bool(user_key_map)

    preferred_locale = getattr(getattr(self, "_options", None), "locale", None)
    playlist = _load_m3u8_playlist(self, url, base_headers, credentials=credentials)

    # Resolve master/variant playlists to the preferred audio or highest-bandwidth video stream.
    seen_urls = {url}
    while playlist.is_variant and playlist.playlists:
        ctx = logging.get_job_context()
        job_cancel = ctx.cancel_event if ctx is not None else None
        if job_cancel is not None and job_cancel.is_set():
            raise exceptions.DownloadCancelled
        next_url = _select_variant_playlist_uri(
            playlist,
            prefer_video=prefer_video,
            locale=preferred_locale,
        )
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        playlist = _load_m3u8_playlist(self, next_url, base_headers, credentials=credentials)

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
            and str(getattr(seg.key, "method", "") or "").strip().upper() in _SUPPORTED_HLS_KEY_METHODS
            and _hls_keyformat(seg.key).lower() in ("identity", _W3C_CLEARKEY_FORMAT)
            and not user_default_key
            and getattr(seg.key, "absolute_uri", None)
            and not str(seg.key.absolute_uri).lower().startswith("data:")
        )
    )
    if len(unique_key_uris) > 1:
        ctx = logging.get_job_context()
        fetched_keys = run_cancellable_pool(
            lambda uri: self._get_page(uri, headers=base_headers, credentials=credentials),
            unique_key_uris,
            max_workers=min(8, len(unique_key_uris)),
            cancel_event=ctx.cancel_event if ctx is not None else None,
        )
        key_cache.update(zip(unique_key_uris, fetched_keys, strict=True))

    for idx, seg in enumerate(playlist.segments):
        seg_ext = extension if extension is not None else _segment_extension(seg.absolute_uri)
        seg_headers: MutableMapping[str, str | bytes] = dict(base_headers)

        seg_size: int | None = None
        raw_byterange = getattr(seg, "byterange", None)
        if raw_byterange:
            prev_end = last_byterange_end.get(seg.absolute_uri)
            start, end = _parse_hls_byterange(str(raw_byterange), default_offset=prev_end)
            last_byterange_end[seg.absolute_uri] = end + 1
            seg_headers["Range"] = f"bytes={start}-{end}"
            seg_size = (end - start) + 1

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
                    resp = self.request("GET", init_uri, headers=init_headers, credentials=credentials)
                    if resp.status_code not in (200, 206):
                        _raise_for_failed_response(init_uri, resp)
                    init_payload = resp.content
                    expected_len = (init_end - init_start) + 1
                    if resp.status_code == 200 and len(init_payload) > expected_len:
                        # CDN ignored Range on #EXT-X-MAP and returned the full asset; slice the requested init range.
                        init_payload = init_payload[init_start : init_end + 1]
                    init_cache[cache_key] = init_payload
                else:
                    init_cache[cache_key] = self.get(init_uri, headers=init_headers, credentials=credentials)
            init_bytes = init_cache[cache_key]

        current = AudiobookFile(
            url=seg.absolute_uri,
            ext=seg_ext,
            headers=seg_headers,
            expected_content_type=expected_content_type,
            init_section=init_bytes,
            size=seg_size,
        )
        if seg.key is not None and getattr(seg.key, "method", None) not in (None, "NONE"):
            key_method, fmt_lower = _validate_hls_key(
                seg.key,
                has_user_key=has_user_key,
                init_bytes=init_bytes,
            )
            is_fmp4 = init_bytes is not None or seg_ext.lstrip(".").lower() in _FMP4_EXTENSIONS
            expected_key_len = 32 if key_method in ("AES-256", "AES-256-GCM") else 16
            resolved_key: bytes | None = None
            if user_default_key is not None and len(user_default_key) == expected_key_len:
                resolved_key = user_default_key
            elif (
                (key_method in ("SAMPLE-AES-CTR", "SAMPLE-AES-CENC") or (key_method == "SAMPLE-AES" and is_fmp4))
                and user_key_map
            ):
                resolved_key = None
            elif fmt_lower in ("identity", _W3C_CLEARKEY_FORMAT) and getattr(seg.key, "absolute_uri", None):
                key_uri = str(seg.key.absolute_uri)
                if key_uri not in key_cache:
                    inline_key = _decode_key_data_uri(key_uri)
                    key_cache[key_uri] = (
                        inline_key
                        if inline_key is not None
                        else self._get_page(key_uri, headers=base_headers, credentials=credentials)
                    )
                resolved_key = key_cache[key_uri]
            elif user_default_key is not None:
                raise exceptions.GenericAudiobookDLException(
                    f"Provided decryption key is {len(user_default_key)} bytes; "
                    f"HLS {key_method} requires {expected_key_len} bytes"
                )
            else:
                raise exceptions.GenericAudiobookDLException(
                    f"HLS {key_method} segment has no key URI and no decryption_key option was provided"
                )

            if resolved_key is not None and len(resolved_key) != expected_key_len:
                raise exceptions.GenericAudiobookDLException(
                    f"Invalid HLS {key_method} key length ({len(resolved_key)} bytes; expected {expected_key_len})"
                )

            if key_method in ("AES-128", "AES-256"):
                assert resolved_key is not None
                iv_bytes = _parse_hls_iv(seg.key.iv, media_sequence + idx, byte_length=16)
                current.encryption_method = AESCBCEncryption(key=resolved_key, iv=iv_bytes)
            elif key_method == "AES-256-GCM":
                assert resolved_key is not None
                nonce_bytes = _parse_hls_iv(seg.key.iv, media_sequence + idx, byte_length=12)
                current.encryption_method = AESGCMEncryption(key=resolved_key, nonce=nonce_bytes)
            elif key_method == "SAMPLE-AES":
                if is_fmp4:
                    cenc_method = CENCDecryption(
                        key=resolved_key,
                        key_map=dict(user_key_map),
                    )
                    reconcile_cenc_decryption(
                        cenc_method,
                        _extract_hls_cenc_kids(seg.key, fmt_lower, init_bytes),
                    )
                    current.encryption_method = cenc_method
                else:
                    assert resolved_key is not None
                    iv_bytes = _parse_hls_iv(seg.key.iv, media_sequence + idx, byte_length=16)
                    current.encryption_method = SampleAESEncryption(key=resolved_key, iv=iv_bytes)
            else:
                # SAMPLE-AES-CTR / SAMPLE-AES-CENC
                cenc_method = CENCDecryption(
                    key=resolved_key,
                    key_map=dict(user_key_map),
                )
                reconcile_cenc_decryption(
                    cenc_method,
                    _extract_hls_cenc_kids(seg.key, fmt_lower, init_bytes),
                )
                current.encryption_method = cenc_method
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

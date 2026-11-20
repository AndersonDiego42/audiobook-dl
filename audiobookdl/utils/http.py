import base64
import binascii
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
import email.utils
import http.client
from http.cookiejar import Cookie, CookieJar
import importlib
import math
import random
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter

from audiobookdl import logging
from audiobookdl.exceptions import DownloadCancelled, GenericAudiobookDLException


DEFAULT_HTTP_TIMEOUT: tuple[int, int] = (15, 60)
DEFAULT_USER_AGENT: str = "audiobook-dl"
DEFAULT_MAX_RETRIES: int = 4
DEFAULT_RETRY_BACKOFF_BASE: float = 1.0
MAX_RETRY_DELAY: float = 60.0

# HTTP status codes indicating transient origin, gateway, rate-limit, or Cloudflare edge failures.
RETRIABLE_CDN_STATUS_CODES: frozenset[int] = frozenset(
    {408, 425, 429, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)

_GENERIC_BINARY_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/octet-stream",
        "binary/octet-stream",
        "application/binary",
        "application/x-binary",
        "application/x-octet-stream",
        "application/force-download",
        "application/download",
        "application/x-download",
        "application/unknown",
    }
)
_SOFT_ERROR_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "application/json",
        "application/problem+json",
        "text/plain",
        "text/css",
        "text/csv",
        "text/javascript",
        "application/javascript",
    }
)
_TEXT_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {"txt", "html", "htm", "xml", "json", "m3u8", "srt", "vtt", "cue", "opf"}
)
_EBOOK_ONLY_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/epub+zip",
        "application/epub",
        "application/pdf",
        "application/x-pdf",
        "application/x-mobipocket-ebook",
        "application/vnd.amazon.ebook",
        "application/x-mobi8-ebook",
        "application/x-cbz",
        "application/vnd.comicbook+zip",
        "application/x-cbr",
        "application/vnd.comicbook-rar",
    }
)
_EBOOK_FILE_EXTENSIONS: frozenset[str] = frozenset({"epub", "pdf", "mobi", "azw3", "cbz", "cbr"})
_MEDIA_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "mp3",
        "m4a",
        "m4b",
        "mp4",
        "m4v",
        "m4s",
        "aac",
        "ogg",
        "oga",
        "opus",
        "flac",
        "wav",
        "wma",
        "ts",
        "webm",
        "mkv",
        "mov",
        "aiff",
        "aif",
        "caf",
    }
)
_STREAM_COMPATIBLE_APP_TYPES: frozenset[str] = frozenset(
    {
        "application/mp4",
        "application/mp2t",
        "application/ogg",
        "application/x-ogg",
        "application/x-mp3",
        "application/mp3",
        "application/x-m4a",
        "application/m4a",
        "application/aac",
        "application/x-aac",
        "application/flac",
        "application/x-flac",
        "application/matroska",
        "application/x-matroska",
        "application/webm",
        "application/mxf",
        "application/dash+xml",
        "application/x-mpegurl",
        "application/vnd.apple.mpegurl",
    }
)
_MEDIA_MIME_EQUIVALENCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "audio/mpeg",
            "audio/mp3",
            "audio/mpeg3",
            "audio/x-mpeg",
            "audio/x-mp3",
            "audio/x-mpeg-3",
            "application/x-mp3",
            "application/mp3",
        }
    ),
    frozenset(
        {
            "audio/mp4",
            "audio/x-m4a",
            "audio/m4a",
            "audio/x-m4b",
            "audio/m4b",
            "audio/x-mp4",
            "application/mp4",
            "application/x-m4a",
            "application/m4a",
            "video/mp4",
            "video/m4v",
            "video/x-m4v",
            "video/iso.segment",
            "audio/iso.segment",
        }
    ),
    frozenset(
        {
            "audio/aac",
            "audio/x-aac",
            "audio/aacp",
            "audio/adts",
            "audio/x-hx-aac-adts",
            "audio/vnd.dlna.adts",
            "application/aac",
            "application/x-aac",
        }
    ),
    frozenset({"video/mp2t", "application/mp2t", "audio/mp2t"}),
    frozenset(
        {
            "audio/ogg",
            "application/ogg",
            "application/x-ogg",
            "audio/opus",
            "audio/vorbis",
            "video/ogg",
        }
    ),
    frozenset({"audio/flac", "audio/x-flac", "application/flac", "application/x-flac"}),
    frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/l16"}),
    frozenset({"audio/webm", "video/webm", "application/webm"}),
    frozenset(
        {
            "audio/matroska",
            "audio/x-matroska",
            "video/x-matroska",
            "application/matroska",
            "application/x-matroska",
        }
    ),
    frozenset({"audio/aiff", "audio/x-aiff"}),
    frozenset({"audio/3gpp", "audio/3gpp2", "audio/amr", "audio/ac3", "audio/eac3", "audio/x-ms-wma"}),
    frozenset({"application/epub+zip", "application/epub", "application/zip", "application/x-zip-compressed"}),
    frozenset({"application/pdf", "application/x-pdf"}),
    frozenset({"application/x-mobipocket-ebook", "application/vnd.amazon.ebook", "application/x-mobi8-ebook"}),
    frozenset({"application/x-cbz", "application/vnd.comicbook+zip", "application/zip", "application/x-zip-compressed"}),
    frozenset({"application/x-cbr", "application/vnd.comicbook-rar", "application/x-rar-compressed", "application/vnd.rar"}),
    frozenset({"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"}),
)
_MEDIA_MIME_TO_GROUPS: dict[str, list[frozenset[str]]] = {}
for _group in _MEDIA_MIME_EQUIVALENCE_GROUPS:
    for _mime in _group:
        _MEDIA_MIME_TO_GROUPS.setdefault(_mime, []).append(_group)

_HEADER_BYTE_RANGE_RE = re.compile(r"^bytes\s*=\s*(\d+)\s*-\s*(\d+)$", re.IGNORECASE)
_CLOUDFLARE_CHALLENGE_MARKERS: tuple[str, ...] = (
    "<title>attention required! | cloudflare</title>",
    "<title>just a moment...</title>",
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
    "enable javascript and cookies to continue",
)

_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?(?:/s)?\s*$", re.IGNORECASE)
_RATE_UNITS = {
    "": 1,
    "k": 1024,
    "m": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
}


def parse_byte_rate(raw: str | None) -> int | None:
    """Parse a human-readable bandwidth cap (e.g. ``'500K'``, ``'2M'``) into bytes/sec."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    match = _RATE_RE.match(cleaned)
    if not match:
        raise ValueError(f"Invalid bandwidth limit {raw!r}; expected forms like '500K', '2M', or '1048576'")
    value = float(match.group(1))
    unit = match.group(2).lower()
    bps = int(value * _RATE_UNITS[unit])
    if bps <= 0:
        raise ValueError(f"Bandwidth limit must be > 0, got {raw!r}")
    return bps


class TokenBucketRateLimiter:
    """Thread-safe aggregate byte-rate limiter shared across concurrent download workers."""

    def __init__(self, rate_bytes_per_sec: int) -> None:
        if rate_bytes_per_sec <= 0:
            raise ValueError("rate_bytes_per_sec must be > 0")
        self.rate = float(rate_bytes_per_sec)
        # Allow bursts up to 1 second of traffic (or one 64 KiB read chunk).
        self.capacity = max(65536.0, self.rate)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(
        self,
        num_bytes: int,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Consume `num_bytes` tokens, sleeping in cancellable increments as needed."""
        if num_bytes <= 0:
            return
        needed = float(num_bytes)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                if elapsed > 0:
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                    self._last_refill = now
                if self._tokens >= needed or needed > self.capacity and self._tokens > 0:
                    consumed = min(needed, self._tokens) if needed > self.capacity else needed
                    self._tokens -= consumed
                    remaining = needed - consumed
                    if remaining <= 0:
                        return
                    needed = remaining
                    wait_seconds = min(0.5, needed / self.rate)
                else:
                    deficit = needed - self._tokens
                    wait_seconds = min(0.5, max(0.005, deficit / self.rate))
            if cancel_event is not None:
                if cancel_event.wait(wait_seconds):
                    raise DownloadCancelled
            else:
                time.sleep(wait_seconds)


def resolve_http_timeout(
    config_or_options: Any = None,
    *,
    default: float | tuple[int, int] = DEFAULT_HTTP_TIMEOUT,
) -> float | tuple[int, int]:
    """Return a validated positive HTTP timeout from `config_or_options`, or `default`."""
    raw = getattr(config_or_options, "timeout", None) if config_or_options is not None else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return default


def resolve_max_retries(
    config_or_options: Any = None,
    *,
    default: int = DEFAULT_MAX_RETRIES,
) -> int:
    """Return a validated non-negative retry count from `config_or_options`, or `default`."""
    raw = getattr(config_or_options, "max_retries", None) if config_or_options is not None else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return default


def get_header_ci(headers: Any, name: str) -> Any | None:
    """Return header value case-insensitively across `CaseInsensitiveDict`, `curl_cffi`, and plain dicts."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if isinstance(value, (str, bytes, int, float)):
            return value
    items_fn = getattr(headers, "items", None)
    if callable(items_fn):
        target = name.lower()
        for key, val in items_fn():
            if str(key).lower() == target:
                return val
    return None


def parse_byte_range_header(range_val: Any) -> tuple[int, int] | None:
    """Parse an HTTP `Range: bytes=start-end` header into `(start, end)` inclusive."""
    if not isinstance(range_val, str):
        return None
    match = _HEADER_BYTE_RANGE_RE.fullmatch(range_val.strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if end < start:
        return None
    return start, end


def normalize_content_type(value: Any) -> str | None:
    """Return the normalized lowercase MIME type (`type/subtype`), stripping parameters and quotes."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    for part in value.split(","):
        media_type = part.split(";", 1)[0].strip().strip("\"'").lower()
        if media_type:
            return media_type
    return None


def _is_audio_video_media_mime(mime: str) -> bool:
    return mime.startswith(("audio/", "video/")) or mime in _STREAM_COMPATIBLE_APP_TYPES


def _matches_expected_mime(actual_ct: str, expected_set: set[str]) -> bool:
    if actual_ct in expected_set or "*/*" in expected_set:
        return True
    for exp in expected_set:
        if exp.endswith("/*") and actual_ct.startswith(exp[:-1]):
            return True
    return False


def is_content_type_compatible(
    actual_raw: Any,
    expected: Any,
    *,
    file_ext: str | None = None,
) -> tuple[bool, str | None]:
    """
    Validate CDN `Content-Type` against `expected` while flexibly accepting valid media types.

    Accepts MIME aliases, wildcards, generic binary/download types, and any valid audio/video
    stream MIME type when expecting media, while rejecting HTML/XML/JSON/text soft-error
    responses and incompatible non-media payloads. Returns `(is_compatible, normalized_actual)`.
    """
    actual_ct = normalize_content_type(actual_raw)

    expected_set: set[str] = set()
    if isinstance(expected, str):
        norm = normalize_content_type(expected)
        if norm:
            expected_set.add(norm)
    elif isinstance(expected, Iterable):
        for item in expected:
            norm = normalize_content_type(item)
            if norm:
                expected_set.add(norm)

    ext = (file_ext or "").lstrip(".").lower()
    if actual_ct in _SOFT_ERROR_MIME_TYPES:
        if actual_ct in expected_set or ext in _TEXT_DOCUMENT_EXTENSIONS:
            return True, actual_ct
        return False, actual_ct

    if not expected_set:
        if actual_ct is not None:
            if ext in _MEDIA_FILE_EXTENSIONS and (
                actual_ct.startswith("image/") or actual_ct in _EBOOK_ONLY_MIME_TYPES
            ):
                return False, actual_ct
            if ext in _EBOOK_FILE_EXTENSIONS and actual_ct.startswith(("audio/", "video/", "image/")):
                return False, actual_ct
        return True, actual_ct

    if actual_ct is None:
        # Many object-storage / signed CDN URLs omit Content-Type on valid 200/206 media responses.
        expects_text_only = all(t in _SOFT_ERROR_MIME_TYPES for t in expected_set)
        return (not expects_text_only), None

    if _matches_expected_mime(actual_ct, expected_set):
        return True, actual_ct

    expects_text_only = all(t in _SOFT_ERROR_MIME_TYPES for t in expected_set)
    if actual_ct in _GENERIC_BINARY_MIME_TYPES and not expects_text_only:
        return True, actual_ct

    actual_groups = _MEDIA_MIME_TO_GROUPS.get(actual_ct)
    if actual_groups and any(exp in group for group in actual_groups for exp in expected_set):
        return True, actual_ct

    if (
        ext in _EBOOK_FILE_EXTENSIONS
        and (_GENERIC_BINARY_MIME_TYPES & expected_set)
        and actual_ct in _EBOOK_ONLY_MIME_TYPES
    ):
        return True, actual_ct

    expects_media = any(
        _is_audio_video_media_mime(exp) or exp in _GENERIC_BINARY_MIME_TYPES
        for exp in expected_set
    )
    if expects_media and ext not in _EBOOK_FILE_EXTENSIONS and _is_audio_video_media_mime(actual_ct):
        return True, actual_ct

    return False, actual_ct


_DIGEST_ALGO_LENGTHS: dict[str, int] = {
    "md5": 16,
    "sha1": 20,
    "sha256": 32,
    "sha512": 64,
}


def parse_content_md5(header_val: Any) -> str | None:
    """
    Parse an HTTP `Content-MD5` header into a lowercase 32-char hex MD5 digest.

    Accepts standard RFC 1864 base64-encoded 16-byte digests as well as 32-char hex strings.
    """
    if isinstance(header_val, (bytes, bytearray)):
        header_val = bytes(header_val).decode("ascii", errors="ignore")
    if not isinstance(header_val, str):
        return None
    cleaned = header_val.strip().strip("\"'")
    if not cleaned:
        return None
    if len(cleaned) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in cleaned):
        return cleaned.lower()
    padded = cleaned + ("=" * ((4 - (len(cleaned) % 4)) % 4))
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 16 else None


def parse_repr_digest(header_val: Any) -> dict[str, str]:
    """
    Parse RFC 9530 `Repr-Digest` or RFC 3230 `Digest` headers into `{algo: hex}`.

    Supports `sha-256=:base64:`, `sha-512=:base64:`, `sha-1=:base64:`, and `md5=:base64:`
    (with or without RFC 8941 byte-sequence `:` delimiters).
    """
    if isinstance(header_val, (bytes, bytearray)):
        header_val = bytes(header_val).decode("ascii", errors="ignore")
    if not isinstance(header_val, str) or not header_val.strip():
        return {}
    digests: dict[str, str] = {}
    for entry in header_val.split(","):
        part = entry.strip()
        if "=" not in part:
            continue
        raw_algo, raw_val = part.split("=", 1)
        algo = raw_algo.strip().lower().replace("-", "")
        if algo == "sha":
            algo = "sha1"
        expected_len = _DIGEST_ALGO_LENGTHS.get(algo)
        if expected_len is None:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val.startswith(":") and val.endswith(":"):
            val = val[1:-1].strip()
        else:
            val = val.strip("\"'")
        if not val:
            continue
        if len(val) == expected_len * 2 and all(ch in "0123456789abcdefABCDEF" for ch in val):
            digests[algo] = val.lower()
            continue
        padded = val + ("=" * ((4 - (len(val) % 4)) % 4))
        try:
            raw_bytes = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw_bytes) == expected_len:
            digests[algo] = raw_bytes.hex()
    return digests


def _iter_response_chunks(resp: Any, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield decoded body chunks from a `requests`-like response."""
    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        yield from iter_content(chunk_size=chunk_size)
        return
    raw_content = getattr(resp, "content", None)
    if isinstance(raw_content, (bytes, bytearray)):
        view = memoryview(raw_content)
        for off in range(0, len(view), chunk_size):
            yield bytes(view[off : off + chunk_size])


def read_bounded_prefix(resp: Any, max_bytes: int = 16384) -> bytes:
    """Consume and return up to `max_bytes` from a response body.

    Only use where the remaining body will not be streamed afterward
    (error paths); the response should be closed by the caller.
    """
    if max_bytes <= 0:
        return b""
    raw = getattr(resp, "_content", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw[:max_bytes])
    buf = bytearray()
    try:
        for chunk in _iter_response_chunks(resp):
            if not chunk:
                continue
            remaining = max_bytes - len(buf)
            if remaining <= 0:
                break
            buf.extend(chunk[:remaining])
            if len(buf) >= max_bytes:
                break
    except (AttributeError, TypeError):
        pass
    if buf:
        return bytes(buf)
    if getattr(resp, "_content", None) is False:
        # Unconsumed `requests` stream: never trigger a full body download.
        return b""
    try:
        content = getattr(resp, "content", None)
    except Exception:
        content = None
    if isinstance(content, (bytes, bytearray)):
        return bytes(content[:max_bytes])
    return b""


def is_cloudflare_challenge(response: Any, *, body: bytes | str | None = None) -> bool:
    """Return `True` when an HTTP response represents an interactive Cloudflare/WAF challenge."""
    if response is None and body is None:
        return False
    status_code = getattr(response, "status_code", None) if response is not None else None
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None
    headers = getattr(response, "headers", None) if response is not None else None

    cf_mitigated = get_header_ci(headers, "cf-mitigated")
    if isinstance(cf_mitigated, str) and cf_mitigated.strip().lower() == "challenge":
        return True

    if status_code is not None and status_code not in (200, 403, 429, 503):
        return False

    server_hdr = get_header_ci(headers, "server")
    has_cf_header = (
        (isinstance(server_hdr, str) and "cloudflare" in server_hdr.lower())
        or isinstance(get_header_ci(headers, "cf-ray"), str)
    )

    sample_text = ""
    if isinstance(body, (bytes, bytearray)):
        sample_text = bytes(body[:16384]).decode("utf-8", errors="ignore").lower()
    elif isinstance(body, str):
        sample_text = body[:16384].lower()
    elif response is not None:
        # Never trigger a full body download on streamed responses: only sample
        # content that is already materialized (`requests` sets `_content` to
        # `False` until consumed; curl_cffi-style responses expose `.content`
        # eagerly for non-streamed requests).
        raw_content = getattr(response, "_content", None)
        unconsumed_stream = raw_content is False
        if unconsumed_stream:
            raw_content = None
        if not isinstance(raw_content, (bytes, bytearray)) and not unconsumed_stream:
            try:
                candidate = getattr(response, "content", None)
            except Exception:
                candidate = None
            if isinstance(candidate, (bytes, bytearray)):
                raw_content = candidate
        if isinstance(raw_content, (bytes, bytearray)) and raw_content:
            sample_text = bytes(raw_content[:16384]).decode("utf-8", errors="ignore").lower()
        elif not unconsumed_stream:
            try:
                raw_text = getattr(response, "text", None)
            except Exception:
                raw_text = None
            if isinstance(raw_text, str):
                sample_text = raw_text[:16384].lower()

    if not sample_text:
        return False
    if "<title>attention required! | cloudflare</title>" in sample_text:
        return True
    if has_cf_header and any(marker in sample_text for marker in _CLOUDFLARE_CHALLENGE_MARKERS):
        return True
    return False


def parse_retry_after(
    value: Any | None,
    *,
    max_delay: float = MAX_RETRY_DELAY,
) -> float | None:
    """Parse an HTTP `Retry-After` header (delta seconds or HTTP-date) clamped to `max_delay`."""
    if value is None or not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, min(max_delay, seconds))


def parse_retry_after_headers(
    headers: Any | None,
    *,
    max_delay: float = MAX_RETRY_DELAY,
) -> float | None:
    """Extract bounded retry delay from `Retry-After`, `X-Retry-After`, `Retry-After-Ms`, or `*RateLimit-Reset`."""
    if headers is None:
        return None
    for hdr_name in ("Retry-After", "X-Retry-After"):
        delay = parse_retry_after(get_header_ci(headers, hdr_name), max_delay=max_delay)
        if delay is not None:
            return delay

    raw_ms = get_header_ci(headers, "Retry-After-Ms")
    if isinstance(raw_ms, (str, int, float)) and not isinstance(raw_ms, bool):
        try:
            ms_val = float(str(raw_ms).strip())
            if math.isfinite(ms_val):
                return max(0.0, min(max_delay, ms_val / 1000.0))
        except ValueError:
            pass

    for reset_name in ("RateLimit-Reset", "X-RateLimit-Reset", "X-Rate-Limit-Reset"):
        raw_reset = get_header_ci(headers, reset_name)
        if isinstance(raw_reset, (str, int, float)) and not isinstance(raw_reset, bool):
            try:
                reset_val = float(str(raw_reset).strip())
            except ValueError:
                continue
            if not math.isfinite(reset_val):
                continue
            seconds = (reset_val - time.time()) if reset_val > 1_000_000_000 else reset_val
            return max(0.0, min(max_delay, seconds))

    return None


def compute_retry_delay(
    attempt: int,
    retry_after: Any | None = None,
    *,
    headers: Any | None = None,
    base_delay: float = DEFAULT_RETRY_BACKOFF_BASE,
    max_delay: float = MAX_RETRY_DELAY,
    jitter_max: float = 0.5,
) -> float:
    """Compute bounded exponential backoff (with jitter) or honor CDN retry/rate-limit headers."""
    header_delay = parse_retry_after(retry_after, max_delay=max_delay)
    if header_delay is None and headers is not None:
        header_delay = parse_retry_after_headers(headers, max_delay=max_delay)
    if header_delay is not None:
        return header_delay
    jitter = random.uniform(0.0, jitter_max) if jitter_max > 0 else 0.0
    return min(max_delay, float(base_delay) * (2 ** max(0, attempt)) + jitter)


def sleep_or_cancel(
    delay: float,
    cancel_event: threading.Event | None = None,
    *,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> None:
    """Sleep for `delay` seconds while respecting `cancel_event`."""
    if delay <= 0:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise DownloadCancelled
    else:
        sleep_fn(delay)


def create_configured_http_session(
    config_or_options: Any = None,
    *,
    proxy: str | None = None,
    pool_connections: int | None = None,
    pool_maxsize: int | None = None,
) -> requests.Session:
    """Build a `requests.Session` configured with `HTTPAdapter` pool sizing and optional proxy."""
    raw_proxy = proxy if proxy is not None else (
        getattr(config_or_options, "proxy", None) if config_or_options is not None else None
    )
    resolved_proxy = str(raw_proxy).strip() if raw_proxy else ""
    workers = getattr(config_or_options, "max_workers", None) if config_or_options is not None else None
    default_pool = max(16, (int(workers) if isinstance(workers, int) and workers > 0 else 6) * 2)
    conns = max(1, int(pool_connections)) if pool_connections is not None else default_pool
    maxsize = max(1, int(pool_maxsize)) if pool_maxsize is not None else default_pool

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=conns, pool_maxsize=maxsize)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if resolved_proxy:
        session.proxies.update({"http": resolved_proxy, "https": resolved_proxy})
    return session


def create_impersonated_session(
    browser: str,
    *,
    proxy: str | None = None,
) -> Any:
    """
    Build a `curl_cffi.requests.Session` impersonating `browser` TLS/HTTP2 fingerprints.

    Requires the optional `curl_cffi` package (`pip install "audiobook-dl[impersonate]"`).
    """
    target = (browser or "").strip()
    if not target:
        raise ValueError("impersonate browser name cannot be empty")
    try:
        curl_requests = importlib.import_module("curl_cffi.requests")
    except ImportError as exc:
        raise GenericAudiobookDLException(
            f"TLS impersonation (impersonate={target!r}) requires the optional 'curl_cffi' package. "
            "Install it with: pip install \"audiobook-dl[impersonate]\""
        ) from exc

    try:
        session = curl_requests.Session(impersonate=target)
    except Exception as exc:
        raise GenericAudiobookDLException(
            f"Failed to initialize curl_cffi session with impersonate={target!r}: {exc}"
        ) from exc

    if proxy:
        proxies = {"http": proxy, "https": proxy}
        if hasattr(session, "proxies") and isinstance(session.proxies, dict):
            session.proxies.update(proxies)
        else:
            session.proxies = proxies
    return session


def extract_session_cookies(session: Any) -> list[Cookie]:
    """Return `Cookie` objects from a `requests.Session` or `curl_cffi` session."""
    raw_cookies = getattr(session, "cookies", None)
    if raw_cookies is None:
        return []
    jar = getattr(raw_cookies, "jar", raw_cookies)
    if isinstance(jar, CookieJar):
        return list(CookieJar.__iter__(jar))
    if isinstance(raw_cookies, CookieJar):
        return list(CookieJar.__iter__(raw_cookies))
    return []


def update_session_cookies(session: Any, cookie_jar: CookieJar) -> None:
    """Copy cookies from `cookie_jar` into either a `requests` or `curl_cffi` session."""
    raw_cookies = getattr(session, "cookies", None)
    if raw_cookies is None:
        return
    jar = getattr(raw_cookies, "jar", None)
    if isinstance(jar, CookieJar):
        for cookie in CookieJar.__iter__(cookie_jar):
            jar.set_cookie(cookie)
        return
    if hasattr(raw_cookies, "update"):
        raw_cookies.update(cookie_jar)


def is_transient_network_error(exc: BaseException) -> bool:
    """Return `True` for retryable timeout or connection errors across `requests` and `curl_cffi`."""
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            http.client.IncompleteRead,
        ),
    ):
        return True
    module_name = getattr(type(exc), "__module__", "") or ""
    if module_name.startswith("urllib3") or module_name.startswith("curl_cffi"):
        cls_name = type(exc).__name__.lower()
        return any(
            token in cls_name
            for token in ("timeout", "connection", "requestexception", "curlerror", "protocolerror", "incompleteread")
        )
    return False


def redirect_of(
    url: str,
    amount: int = 1,
    session: requests.Session | None = None,
    *,
    stop_non_redirect: bool = False,
    target_pattern: str | None = None,
) -> str | None:
    """
    Get the location where a url redirects to.

    :param url: the http endpoint
    :param amount: maximum redirect hops to follow
    :param session: optional `requests.Session` to preserve proxies, headers, and TLS settings
    :param stop_non_redirect: return the last redirected URL if a non-redirect response occurs after >= 1 hop
    :param target_pattern: optional regex; stop immediately once a redirected URL matches it
    :returns: the redirected location
    """
    getter = session.get if session is not None else requests.get
    hops = 0
    for _ in range(amount):
        response = getter(
            url,
            allow_redirects=False,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        try:
            if response.status_code not in (301, 302, 303, 307, 308):
                return url if (stop_non_redirect and hops > 0) else None
            location = get_header_ci(response.headers, "location")
            if not isinstance(location, str) or not location:
                return url if (stop_non_redirect and hops > 0) else None
            next_url = urljoin(url, location)
        finally:
            close_fn = getattr(response, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
        hops += 1
        if next_url == url:
            return next_url
        url = next_url
        if target_pattern and re.search(target_pattern, url):
            return url
    return url


AUTH_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "cookie2",
        "x-login-token",
        "x-profile-token",
        "token",
        "session-key",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)


def strip_auth_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Copy `headers` while stripping authentication/session headers case-insensitively."""
    if not headers:
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_val in headers.items():
        key = raw_key.decode("latin-1") if isinstance(raw_key, bytes) else str(raw_key)
        if key.strip().lower() in AUTH_HEADER_NAMES:
            continue
        val = raw_val.decode("latin-1") if isinstance(raw_val, bytes) else str(raw_val)
        cleaned[key] = val
    return cleaned


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def warn_if_insecure_token_url(url: str, service_label: str) -> bool:
    """Warn if an authenticated HTTP request targets a non-loopback `http://` URL; return True if warned."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "http":
        return False
    host = (parsed.hostname or "").strip().lower()
    if host in _LOOPBACK_HOSTS:
        return False
    logging.warning(
        f"Warning: Sending {service_label} credentials over unencrypted "
        f"http:// to '{host or parsed.netloc}'. Prefer https:// outside trusted local networks."
    )
    return True

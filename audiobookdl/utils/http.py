import re
import threading
import time
from typing import Optional, Tuple
import requests

from audiobookdl.exceptions import DownloadCancelled


DEFAULT_HTTP_TIMEOUT: Tuple[int, int] = (15, 60)
DEFAULT_USER_AGENT: str = "audiobook-dl"

_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?(?:/s)?\s*$", re.IGNORECASE)
_RATE_UNITS = {
    "": 1,
    "k": 1024,
    "m": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
}


def parse_byte_rate(raw: Optional[str]) -> Optional[int]:
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
        cancel_event: Optional[threading.Event] = None,
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


def redirect_of(
    url: str,
    amount: int = 1,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    Get the location where a url redirects to.

    :param url: the http endpoint
    :param amount: the amount of times the function is allowed to redirect
    :param session: optional `requests.Session` to preserve proxies, headers, and TLS settings
    :returns: the redirected location
    """
    getter = session.get if session is not None else requests.get
    for _ in range(amount):
        response = getter(
            url,
            allow_redirects=False,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if response.status_code not in [301, 302]:
            return None
        if "location" not in response.headers:
            return None
        url = response.headers["location"]
    return url

from contextlib import nullcontext
from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import requests

from audiobookdl import __version__, logging
from audiobookdl.utils import subprocess_kwargs
from audiobookdl.utils.fs import atomic_write_text
from audiobookdl.utils.http import DEFAULT_USER_AGENT


_GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/jo1gi/audiobook-dl/releases/latest"
_PYPI_JSON_URL = "https://pypi.org/pypi/audiobook-dl/json"
_RELEASES_PAGE_URL = "https://github.com/jo1gi/audiobook-dl/releases"
DEFAULT_UPDATE_CHECK_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class UpdateInfo:
    """Result of checking upstream release metadata."""

    current_version: str
    latest_version: str
    has_update: bool
    release_url: str = _RELEASES_PAGE_URL


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse a semantic version string (e.g. `'v1.2.3'`) into numeric parts."""
    cleaned = version_str.strip().lstrip("vV")
    parts = [int(m) for m in re.findall(r"\d+", cleaned)]
    return tuple(parts) if parts else (0,)


def is_newer_version(latest: str, current: str) -> bool:
    """Return True when `latest` represents a strictly newer version than `current`."""
    lat = parse_version_tuple(latest)
    cur = parse_version_tuple(current)
    width = max(len(lat), len(cur))
    return lat + (0,) * (width - len(lat)) > cur + (0,) * (width - len(cur))


def updates_disabled() -> bool:
    """Return True if automatic update checks are disabled via environment variable."""
    raw = os.environ.get("AUDIOBOOK_DL_NO_UPDATE_CHECK", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _read_update_cache(cache_path: str) -> dict[str, Any] | None:
    try:
        with open(cache_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _write_update_cache(
    cache_path: str,
    *,
    checked_at: float,
    latest_version: str,
    release_url: str,
    etag: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "checked_at": float(checked_at),
        "latest_version": latest_version,
        "release_url": release_url,
    }
    if etag:
        payload["etag"] = etag
    try:
        atomic_write_text(cache_path, json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        logging.debug(f"Could not write update check cache ({cache_path}): {exc}")


def _info_from_cache(cached: dict[str, Any], current_version: str) -> UpdateInfo | None:
    cached_ver = str(cached.get("latest_version") or "").strip()
    if not cached_ver:
        return None
    cached_url = str(cached.get("release_url") or _RELEASES_PAGE_URL)
    return UpdateInfo(
        current_version=current_version,
        latest_version=cached_ver,
        has_update=is_newer_version(cached_ver, current_version),
        release_url=cached_url,
    )


def check_for_updates(
    current_version: str = __version__,
    *,
    timeout: float = 5.0,
    session: requests.Session | None = None,
    proxy: str | None = None,
    respect_opt_out: bool = True,
    cache_path: str | None = None,
    cache_ttl_seconds: float = DEFAULT_UPDATE_CHECK_TTL_SECONDS,
    force: bool = False,
    now: float | None = None,
) -> UpdateInfo | None:
    """
    Query upstream GitHub Releases (falling back to PyPI) for a newer release.

    Returns `None` when checks are opted out or offline/unparseable. When `cache_path`
    is set and `force` is False, reuses cached metadata for up to `cache_ttl_seconds`.
    """
    if respect_opt_out and updates_disabled():
        return None
    now_ts = time.time() if now is None else float(now)
    cached: dict[str, Any] | None = None
    if cache_path:
        cached = _read_update_cache(cache_path)
        if not force and cached is not None:
            try:
                checked_at = float(cached.get("checked_at", -1.0))
            except (TypeError, ValueError):
                checked_at = -1.0
            if checked_at >= 0.0 and (now_ts - checked_at) < cache_ttl_seconds:
                cached_info = _info_from_cache(cached, current_version)
                if cached_info is not None:
                    return cached_info

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    gh_headers = dict(headers)
    cached_etag = str(cached.get("etag") or "").strip() if (cached and not force) else ""
    if cached_etag:
        gh_headers["If-None-Match"] = cached_etag

    session_cm = nullcontext(session) if session is not None else requests.Session()
    with session_cm as sess:
        # 1. Try GitHub latest release
        try:
            resp = sess.get(_GITHUB_LATEST_RELEASE_URL, timeout=timeout, proxies=proxies, headers=gh_headers)
            if resp.status_code == 304 and cached is not None:
                cached_info = _info_from_cache(cached, current_version)
                if cached_info is not None:
                    if cache_path:
                        _write_update_cache(
                            cache_path,
                            checked_at=now_ts,
                            latest_version=cached_info.latest_version,
                            release_url=cached_info.release_url,
                            etag=cached_etag or None,
                        )
                    return cached_info
            if resp.status_code == 200:
                data = resp.json()
                tag = str(data.get("tag_name") or "").strip().lstrip("vV")
                html_url = str(data.get("html_url") or _RELEASES_PAGE_URL)
                resp_headers = getattr(resp, "headers", None) or {}
                etag = str(resp_headers.get("ETag") or resp_headers.get("etag") or "").strip() or None
                if tag:
                    if cache_path:
                        _write_update_cache(
                            cache_path,
                            checked_at=now_ts,
                            latest_version=tag,
                            release_url=html_url,
                            etag=etag,
                        )
                    return UpdateInfo(
                        current_version=current_version,
                        latest_version=tag,
                        has_update=is_newer_version(tag, current_version),
                        release_url=html_url,
                    )
        except (requests.RequestException, ValueError) as exc:
            logging.debug(f"GitHub update check skipped: {exc}")

        # 2. Fallback to PyPI JSON
        try:
            resp = sess.get(_PYPI_JSON_URL, timeout=timeout, proxies=proxies, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                latest = str(data.get("info", {}).get("version") or "").strip()
                project_url = str(data.get("info", {}).get("project_url") or _RELEASES_PAGE_URL)
                if latest:
                    if cache_path:
                        _write_update_cache(
                            cache_path,
                            checked_at=now_ts,
                            latest_version=latest,
                            release_url=project_url,
                        )
                    return UpdateInfo(
                        current_version=current_version,
                        latest_version=latest,
                        has_update=is_newer_version(latest, current_version),
                        release_url=project_url,
                    )
        except (requests.RequestException, ValueError) as exc:
            logging.debug(f"PyPI update check skipped: {exc}")

    return None


def winget_available() -> bool:
    """Return True on Windows when `winget` is available on PATH."""
    return sys.platform.startswith("win") and shutil.which("winget") is not None


def install_ffmpeg_via_winget() -> tuple[bool, str]:
    """Run `winget install -e --id Gyan.FFmpeg` and return `(success, message)`."""
    winget = shutil.which("winget")
    if not sys.platform.startswith("win") or not winget:
        return False, "winget is not available on this system."
    try:
        proc = subprocess.run(
            [
                winget,
                "install",
                "-e",
                "--id",
                "Gyan.FFmpeg",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            **subprocess_kwargs(capture_output=True),
        )
        if proc.returncode == 0:
            return True, "FFmpeg installed via winget. Restart or locate ffmpeg if not yet on PATH."
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"winget exited with code {proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

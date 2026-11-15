from contextlib import nullcontext
from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import sys
from typing import Optional, Tuple

import requests

from audiobookdl import __version__, logging
from audiobookdl.utils.http import DEFAULT_USER_AGENT


_GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/jo1gi/audiobook-dl/releases/latest"
_PYPI_JSON_URL = "https://pypi.org/pypi/audiobook-dl/json"
_RELEASES_PAGE_URL = "https://github.com/jo1gi/audiobook-dl/releases"


@dataclass(frozen=True)
class UpdateInfo:
    """Result of checking upstream release metadata."""

    current_version: str
    latest_version: str
    has_update: bool
    release_url: str = _RELEASES_PAGE_URL


def parse_version_tuple(version_str: str) -> Tuple[int, ...]:
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


def check_for_updates(
    current_version: str = __version__,
    *,
    timeout: float = 5.0,
    session: Optional[requests.Session] = None,
    proxy: Optional[str] = None,
    respect_opt_out: bool = True,
) -> Optional[UpdateInfo]:
    """
    Query upstream GitHub Releases (falling back to PyPI) for a newer release.

    Returns `None` when checks are opted out or offline/unparseable.
    """
    if respect_opt_out and updates_disabled():
        return None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    session_cm = nullcontext(session) if session is not None else requests.Session()
    with session_cm as sess:
        # 1. Try GitHub latest release
        try:
            resp = sess.get(_GITHUB_LATEST_RELEASE_URL, timeout=timeout, proxies=proxies, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                tag = str(data.get("tag_name") or "").strip().lstrip("vV")
                html_url = str(data.get("html_url") or _RELEASES_PAGE_URL)
                if tag:
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


def install_ffmpeg_via_winget() -> Tuple[bool, str]:
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
        )
        if proc.returncode == 0:
            return True, "FFmpeg installed via winget. Restart or locate ffmpeg if not yet on PATH."
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"winget exited with code {proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

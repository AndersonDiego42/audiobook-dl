import importlib.resources
import math
import os
import re
import subprocess
import sys
from typing import Any, Callable, Dict, Optional
import shutil
from urllib3.poolmanager import PoolManager
from requests.adapters import HTTPAdapter
from ssl import SSLContext


DEFAULT_M4B_BITRATE = "128k"
MIN_AUTO_M4B_BITRATE_KBPS = 64
MAX_AUTO_M4B_BITRATE_KBPS = 192
AUDIO_BITRATE_PRESETS = ("Auto", "64k", "96k", "128k", "192k", "256k")
MP4_CONTAINERS = ("mp4", "m4a", "m4b", "mov")
_BITRATE_RE = re.compile(r"^(\d+)k?$", re.IGNORECASE)


def normalize_audio_bitrate(bitrate: Optional[str]) -> Optional[str]:
    """
    Normalize an AAC bitrate option (`None`/`"auto"` -> `None`, `"128k"`/`"128"` -> `"128k"`).

    Raises `ValueError` for malformed or out-of-range bitrate strings.
    """
    if bitrate is None:
        return None
    cleaned = bitrate.strip()
    if not cleaned or cleaned.lower() == "auto":
        return None
    match = _BITRATE_RE.fullmatch(cleaned)
    if not match:
        raise ValueError(
            f"Invalid audio bitrate {bitrate!r}: expected 'auto' or '<kbps>k' (e.g. '128k')"
        )
    kbps = int(match.group(1))
    if kbps < 8 or kbps > 1024:
        raise ValueError(
            f"Audio bitrate {bitrate!r} out of range (must be between 8k and 1024k)"
        )
    return f"{kbps}k"


def read_asset_file(path: str) -> str:
    """
    Read audiobook-dl asset file.

    :param path: the path of the asset file relative to the audiobook-dl root dir
    :returns: the content of the asset file
    """
    return importlib.resources.files("audiobookdl") \
        .joinpath(path) \
        .read_text(encoding="utf8")


_custom_ffmpeg_dir: Optional[str] = None


def set_ffmpeg_dir(directory: Optional[str]) -> None:
    """Configure an optional directory searched for `ffmpeg` and `ffprobe`."""
    global _custom_ffmpeg_dir
    cleaned = directory.strip() if directory else ""
    _custom_ffmpeg_dir = cleaned or None


def get_ffmpeg_dir() -> Optional[str]:
    """Return the configured custom FFmpeg directory, if any."""
    return _custom_ffmpeg_dir


def resolve_ffmpeg_dir(directory: str) -> Optional[str]:
    """
    Resolve `directory` (or its `bin/` child) if it contains an `ffmpeg` executable.

    Returns the directory containing the executable, or `None` if not found.
    """
    if not directory:
        return None
    exe_names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    for candidate_dir in (directory, os.path.join(directory, "bin")):
        if not os.path.isdir(candidate_dir):
            continue
        for name in exe_names:
            if os.path.isfile(os.path.join(candidate_dir, name)):
                return os.path.abspath(candidate_dir)
    return None


def find_executable(program: str) -> Optional[str]:
    """
    Locate an external executable (e.g. ffmpeg, ffprobe).

    Searches PyInstaller's bundle dir (`_MEIPASS`), the directory containing
    the frozen executable, an optional configured FFmpeg directory, and finally
    the system PATH.
    """
    candidates = [program]
    if os.name == "nt" and not program.lower().endswith(".exe"):
        candidates.append(f"{program}.exe")

    search_dirs = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            search_dirs.append(meipass)
        search_dirs.append(os.path.dirname(sys.executable))

    if _custom_ffmpeg_dir and program.lower().removesuffix(".exe") in ("ffmpeg", "ffprobe"):
        search_dirs.append(_custom_ffmpeg_dir)

    for directory in search_dirs:
        for candidate in candidates:
            full_path = os.path.join(directory, candidate)
            if os.path.isfile(full_path):
                return full_path

    return shutil.which(program)


def program_in_path(program: str) -> bool:
    """Checks whether `program` is available locally or in PATH"""
    return find_executable(program) is not None


def ffprobe_duration(path: str) -> float:
    """Return the duration of `path` in seconds via `ffprobe`, or `0.0` on failure."""
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        return 0.0
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            **subprocess_kwargs(capture_output=True),
        )
    except OSError:
        return 0.0
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return 0.0
    return duration if math.isfinite(duration) and duration > 0 else 0.0


def ffprobe_audio_bitrate(path: str) -> Optional[int]:
    """Return the bitrate of `path` in bits/sec via `ffprobe`, or `None` if unavailable."""
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        return None
    queries = (
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
    )
    for cmd in queries:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                **subprocess_kwargs(capture_output=True),
            )
        except OSError:
            return None
        raw = result.stdout.strip()
        if raw.isdigit():
            bps = int(raw)
            if bps > 0:
                return bps
    return None


def resolve_audio_bitrate(
    source_path: str,
    audio_bitrate: Optional[str] = None,
    probe_bps: Optional[Callable[[str], Optional[int]]] = None,
) -> str:
    """
    Determine the target AAC bitrate when transcoding `source_path` to an MP4 container.

    Uses `audio_bitrate` directly when set (e.g. `"192k"`), or probes `source_path`
    via `ffprobe` in `"auto"`/`None` mode and clamps to `[64k, 192k]` (falling back
    to `128k` if probing fails).
    """
    explicit = normalize_audio_bitrate(audio_bitrate)
    if explicit is not None:
        return explicit
    probe = ffprobe_audio_bitrate if probe_bps is None else probe_bps
    bps = probe(source_path)
    if bps is None or bps <= 0:
        return DEFAULT_M4B_BITRATE
    kbps = max(MIN_AUTO_M4B_BITRATE_KBPS, min(round(bps / 1000), MAX_AUTO_M4B_BITRATE_KBPS))
    return f"{kbps}k"


def subprocess_kwargs(capture_output: bool = True) -> Dict[str, Any]:
    """Return platform-specific subprocess kwargs to avoid console flashes in windowed GUI apps."""
    kwargs: Dict[str, Any] = {}
    if (
        os.name == "nt"
        and hasattr(subprocess, "CREATE_NO_WINDOW")
        and (capture_output or sys.stdout is None)
    ):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


class CustomSSLContextHTTPAdapter(HTTPAdapter):
    """Transport adapter that allows us to use a custom SSLContext."""

    def __init__(self, ssl_context: SSLContext, **kwargs: Any) -> None:
        self.ssl_context: SSLContext = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_context=self.ssl_context,
            **pool_kwargs,
        )

from dataclasses import dataclass
import functools
import importlib.resources
import json
import math
import os
import re
import subprocess
import sys
import threading
from typing import Any
from collections.abc import Callable, Sequence
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


def normalize_audio_bitrate(bitrate: str | None) -> str | None:
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


@functools.lru_cache(maxsize=64)
def read_asset_file(path: str) -> str:
    """
    Read audiobook-dl asset file.

    :param path: the path of the asset file relative to the audiobook-dl root dir
    :returns: the content of the asset file
    """
    return importlib.resources.files("audiobookdl") \
        .joinpath(path) \
        .read_text(encoding="utf8")


def get_ffmpeg_dir() -> str | None:
    """Return the configured custom FFmpeg directory from the active `JobContext`."""
    from audiobookdl import logging

    ctx = logging.get_job_context()
    if ctx is not None and ctx.ffmpeg_dir is not None:
        cleaned = ctx.ffmpeg_dir.strip()
        if cleaned:
            return cleaned
    return None


def resolve_ffmpeg_dir(directory: str) -> str | None:
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


_VALIDATE_EXEC_LOCK = threading.Lock()
_VALIDATE_EXEC_CACHE: dict[tuple[str, int, int, tuple[str, ...]], str | None] = {}


def clear_executable_validation_cache() -> None:
    """Clear cached `validate_executable` results."""
    with _VALIDATE_EXEC_LOCK:
        _VALIDATE_EXEC_CACHE.clear()


def validate_executable(path: str, *, probe_args: Sequence[str] = ("-version",)) -> str | None:
    """
    Verify that `path` can be executed on this host.

    Results are cached by `(abspath, mtime_ns, size, probe_args)` to avoid
    spawning redundant subprocesses during UI refreshes and preflight checks.

    Returns `None` when runnable, or a concise diagnostic message if launching fails
    (for example due to a CPU architecture mismatch, missing DLLs, or permissions).
    """
    cache_key: tuple[str, int, int, tuple[str, ...]] | None = None
    try:
        st = os.stat(path)
        cache_key = (os.path.abspath(path), st.st_mtime_ns, st.st_size, tuple(probe_args))
    except OSError:
        cache_key = None

    if cache_key is not None:
        with _VALIDATE_EXEC_LOCK:
            if cache_key in _VALIDATE_EXEC_CACHE:
                return _VALIDATE_EXEC_CACHE[cache_key]

    err: str | None
    try:
        result = subprocess.run(
            [path, *probe_args],
            capture_output=True,
            text=True,
            timeout=10,
            **subprocess_kwargs(capture_output=True),
        )
    except OSError as exc:
        err = f"{os.path.basename(path)} failed to execute: {exc}"
    except subprocess.SubprocessError as exc:
        err = f"{os.path.basename(path)} failed health check: {exc}"
    else:
        if result.returncode != 0:
            code = result.returncode
            code_text = f"{code} (0x{code & 0xFFFFFFFF:08X})" if (code < -1 or code >= 0x80000000) else str(code)
            detail = ""
            for stream in (result.stderr, result.stdout):
                if stream:
                    first_line = next((ln.strip() for ln in stream.splitlines() if ln.strip()), "")
                    if first_line:
                        detail = first_line[:200]
                        break
            msg = f"{os.path.basename(path)} exited with code {code_text}"
            err = f"{msg}: {detail}" if detail else msg
        else:
            err = None

    if cache_key is not None:
        with _VALIDATE_EXEC_LOCK:
            _VALIDATE_EXEC_CACHE[cache_key] = err
    return err


def validate_ffmpeg_dir(directory: str) -> tuple[str | None, str | None]:
    """
    Resolve `directory` (or its `bin/` child) and verify its `ffmpeg`/`ffprobe` binaries execute.

    Returns `(resolved_dir, None)` on success, `(None, None)` if `ffmpeg` is not found,
    or `(None, error_message)` if a binary exists there but cannot execute.
    """
    resolved = resolve_ffmpeg_dir(directory)
    if resolved is None:
        return None, None
    exe_names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    probe_names = ["ffprobe.exe", "ffprobe"] if os.name == "nt" else ["ffprobe"]
    for candidates in (exe_names, probe_names):
        for name in candidates:
            candidate_path = os.path.join(resolved, name)
            if os.path.isfile(candidate_path):
                err = validate_executable(candidate_path)
                if err is not None:
                    return None, err
                break
    return resolved, None


def find_executable(program: str) -> str | None:
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

    custom_ffmpeg_dir = get_ffmpeg_dir()
    if custom_ffmpeg_dir and program.lower().removesuffix(".exe") in ("ffmpeg", "ffprobe"):
        search_dirs.append(custom_ffmpeg_dir)

    for directory in search_dirs:
        for candidate in candidates:
            full_path = os.path.join(directory, candidate)
            if os.path.isfile(full_path):
                return full_path

    return shutil.which(program)


def program_in_path(program: str) -> bool:
    """Checks whether `program` is available locally or in PATH"""
    return find_executable(program) is not None


@dataclass(frozen=True)
class AudioProbeInfo:
    """Consolidated media metadata returned by a single `ffprobe` JSON call."""

    duration: float = 0.0
    bitrate: int | None = None
    codec_name: str = ""
    format_name: str = ""
    sample_rate: int | None = None
    channels: int | None = None
    probe_error: str | None = None


def _parse_positive_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _parse_positive_float(raw: Any) -> float:
    if raw is None:
        return 0.0
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) and val > 0 else 0.0


def probe_audio_info(path: str) -> AudioProbeInfo:
    """Probe duration, bitrate, codec, and container format in one `ffprobe` call."""
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        return AudioProbeInfo()
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "format=duration,bit_rate,format_name:stream=codec_name,bit_rate,duration,sample_rate,channels",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            **subprocess_kwargs(capture_output=True),
        )
    except OSError as exc:
        return AudioProbeInfo(probe_error=f"ffprobe failed to execute ({ffprobe}): {exc}")
    if result.returncode != 0:
        exec_err = validate_executable(ffprobe)
        if exec_err is not None:
            return AudioProbeInfo(probe_error=f"ffprobe failed to execute ({ffprobe}): {exec_err}")
        return AudioProbeInfo()
    if not result.stdout.strip():
        return AudioProbeInfo()
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return AudioProbeInfo()
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    duration = _parse_positive_float(fmt.get("duration")) or _parse_positive_float(stream.get("duration"))
    bitrate = _parse_positive_int(stream.get("bit_rate")) or _parse_positive_int(fmt.get("bit_rate"))
    codec_name = str(stream.get("codec_name") or "").strip()
    format_name = str(fmt.get("format_name") or "").strip()
    sample_rate = _parse_positive_int(stream.get("sample_rate"))
    channels = _parse_positive_int(stream.get("channels"))
    return AudioProbeInfo(
        duration=duration,
        bitrate=bitrate,
        codec_name=codec_name,
        format_name=format_name,
        sample_rate=sample_rate,
        channels=channels,
    )


def ffprobe_duration(path: str) -> float:
    """Return the duration of `path` in seconds via `ffprobe`, or `0.0` on failure."""
    return probe_audio_info(path).duration


def ffprobe_audio_bitrate(path: str) -> int | None:
    """Return the bitrate of `path` in bits/sec via `ffprobe`, or `None` if unavailable."""
    return probe_audio_info(path).bitrate


def resolve_audio_bitrate(
    source_path: str,
    audio_bitrate: str | None = None,
    probe_bps: Callable[[str], int | None] | None = None,
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


def subprocess_kwargs(capture_output: bool = True) -> dict[str, Any]:
    """Return platform-specific subprocess kwargs to avoid console flashes in windowed GUI apps."""
    kwargs: dict[str, Any] = {}
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

from audiobookdl import logging, AudiobookMetadata
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
    MissingDependency,
)
from audiobookdl.utils import (
    MP4_CONTAINERS,
    ffprobe_audio_bitrate,
    ffprobe_duration,
    find_executable,
    normalize_audio_bitrate,
    resolve_audio_bitrate,
    subprocess_kwargs,
)
from audiobookdl.utils.concurrency import run_cancellable_pool, run_cancellable_subprocess

import os
import re
import shutil
import platform
import subprocess
import threading
from typing import Any, Dict, List, Optional, Sequence, Mapping

LOCATION_DEFAULTS = {
    'album': 'NA',
    'artist': 'NA',
    'title': '',
    'scrape_url': '',
    'series': '',
    'series_order': '',
    'author': '',
    'narrator': '',
    'genre': '',
    'language': '',
    'description': '',
    'isbn': '',
    'publisher': '',
    'release_date': '',
}


class _SafeFormatDict(dict):
    """Mapping for `str.format_map` that substitutes `""` for missing keys."""

    def __missing__(self, key: str) -> str:
        return ""

# Number of parallel ffmpeg remux processes when preparing files for combining
COMBINE_REMUX_THREADS = 16

def gen_output_filename(booktitle: str, file: Mapping[str, str], template: str) -> str:
    """Generates an output filename based on different attributes of the
    file"""
    arguments = {**file, **{"booktitle": booktitle}}
    filename = template.format(**arguments)
    return _fix_output(filename)


def _require_executable(name: str) -> str:
    """Resolve an external executable path or raise `MissingDependency`."""
    path = find_executable(name)
    if path is None:
        raise MissingDependency(dependency=name)
    return path


def _ffmpeg_audio_codec(path: str) -> str:
    """Returns the codec name of the first audio stream in `path` (empty on failure)"""
    ffprobe = find_executable("ffprobe") or "ffprobe"
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True,
        **subprocess_kwargs(capture_output=True),
    )
    return result.stdout.strip()


def _ffprobe_duration(path: str) -> float:
    """Returns the duration of `path` in seconds (0.0 on failure)"""
    return ffprobe_duration(path)


def _ffprobe_audio_bitrate(path: str) -> Optional[int]:
    """Return the bitrate of `path` in bits/sec via `ffprobe`, or `None` if unavailable."""
    return ffprobe_audio_bitrate(path)


def resolve_aac_bitrate(source_path: str, audio_bitrate: Optional[str] = None) -> str:
    """
    Determine the target AAC bitrate when transcoding `source_path` to an MP4 container.

    Uses `audio_bitrate` directly when set (e.g. `"192k"`), or probes `source_path`
    via `ffprobe` in `"auto"`/`None` mode and clamps to `[64k, 192k]` (falling back
    to `128k` if probing fails).
    """
    return resolve_audio_bitrate(source_path, audio_bitrate, probe_bps=_ffprobe_audio_bitrate)


def _remux_to_mpegts(
    source: str,
    ts_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """
    Remux a single audio file into an MPEG-TS container.

    Concatenating raw segments directly (e.g. ADTS-AAC) desyncs the decoder at
    segment boundaries and silently drops most of the audio. Remuxing each
    segment to MPEG-TS first gives every part clean framing and timestamps, so
    the following concatenation is lossless.

    :returns: `True` if a non-empty file was produced
    """
    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    try:
        result = run_cancellable_subprocess(
            [ffmpeg, "-y", "-i", source, "-vn", "-c:a", "copy", "-f", "mpegts", ts_path],
            capture_output=capture,
            cancel_event=cancel_event,
        )
    except DownloadCancelled:
        if os.path.exists(ts_path):
            os.remove(ts_path)
        raise
    if os.path.exists(ts_path) and os.path.getsize(ts_path) > 0:
        return True
    if result.stderr:
        logging.debug(result.stderr.decode("utf8", "replace"))
    return False


def combine_audiofiles(
    filepaths: Sequence[str],
    tmp_dir: str,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
):
    """
    Combines the given audiofiles in `path` into a new file

    :param filepaths: Paths to audio files
    :param tmp_dir: Temporary directory with audio files
    :param output_path: Path of combined audio files
    :param cancel_event: Optional event polled to abort combining cleanly
    """
    if not filepaths:
        raise FailedCombining
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    ffmpeg = _require_executable("ffmpeg")
    output_extension = get_extension(output_path)
    work_dir = f"{os.path.normpath(tmp_dir)}.work"
    ts_dir = os.path.join(work_dir, "ts_parts")
    combined_ts = os.path.join(work_dir, "combined.ts")
    os.makedirs(ts_dir, exist_ok=True)
    try:
        # Remux every segment to MPEG-TS in parallel (lossless copy)
        padding = len(str(len(filepaths)))

        def remux(item):
            index, source = item
            ts_path = os.path.join(ts_dir, f"{str(index).zfill(padding)}.ts")
            return ts_path, _remux_to_mpegts(source, ts_path, cancel_event=cancel_event)

        remuxed = run_cancellable_pool(
            remux,
            list(enumerate(filepaths)),
            max_workers=COMBINE_REMUX_THREADS,
            cancel_event=cancel_event,
        )
        ts_paths = []
        for ts_path, ok in remuxed:
            if not ok:
                raise FailedCombining
            ts_paths.append(ts_path)
        # Binary-concatenate the MPEG-TS parts. The ffmpeg concat *demuxer* aborts
        # early on some segment junctions (returning success while silently dropping
        # most of the audio); MPEG-TS is designed for splicing, so a raw byte
        # concatenation of the remuxed parts is both robust and lossless.
        with open(combined_ts, "wb") as out:
            for ts_path in ts_paths:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled
                with open(ts_path, "rb") as part:
                    shutil.copyfileobj(part, out)
        # Remux the concatenated stream into the requested output container
        command = [ffmpeg, "-y", "-i", combined_ts, "-c", "copy"]
        # AAC streams need the ADTS-to-ASC bitstream filter when written into MP4-family files
        if output_extension in MP4_CONTAINERS:
            if _ffmpeg_audio_codec(ts_paths[0]) == "aac":
                command += ["-bsf:a", "aac_adtstoasc"]
            command += ["-movflags", "+faststart"]
        command.append(output_path)
        capture = not logging.is_ffmpeg_output()
        result = run_cancellable_subprocess(
            command,
            capture_output=capture,
            cancel_event=cancel_event,
        )
        if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
            if result.stderr:
                logging.debug(result.stderr.decode("utf8", "replace"))
            raise FailedCombining
        # Guard against silent truncation: the combined file must be about as long
        # as the concatenated source. A large shortfall means ffmpeg dropped audio.
        expected = _ffprobe_duration(combined_ts)
        actual = _ffprobe_duration(output_path)
        if expected > 0 and actual < expected * 0.98:
            logging.debug(
                f"Combined output is shorter than expected "
                f"({actual:.0f}s vs {expected:.0f}s); combine truncated the audio"
            )
            raise FailedCombining
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)
    except (DownloadCancelled, FailedCombining):
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
    finally:
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


def get_extension(path: str) -> str:
    """
    Get lowercase extension from path

    :param path: Path to get extension from
    :returns: Extension of path
    """
    return os.path.splitext(path)[1][1:].lower()


def combine_supports_direct_output(sample_path: str, output_format: str) -> bool:
    """Return True when `combine_audiofiles` can remux directly into `output_format`."""
    current = get_extension(sample_path).lower()
    target = output_format.lstrip(".").lower()
    if current == target:
        return True
    if target in MP4_CONTAINERS and _ffmpeg_audio_codec(sample_path) == "aac":
        return True
    return False


def can_copy_codec(input_format: str, output_format: str) -> bool:
    """
    Checks whether the codec can be copies to the new output

    :param input_format: Input file filetype
    :param output_format: Output file filetype
    :returns: True if the codec can be copied
    """
    src = input_format.lstrip(".").lower()
    dst = output_format.lstrip(".").lower()
    return dst == "mkv" \
        or dst == "mka" \
        or (src == "ts" and dst == "mp3")


def convert_output(
    filenames: Sequence[str],
    output_format: str,
    cancel_event: Optional[threading.Event] = None,
    audio_bitrate: Optional[str] = None,
):
    """Converts a list of audio files into another format and return new
    files"""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    target_format = output_format.lstrip(".").lower()
    new_paths = []
    for old_path in filenames:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        path_without_ext, old_ext_raw = os.path.splitext(old_path)
        old_ext = old_ext_raw.lstrip(".").lower()
        new_path = f"{path_without_ext}.{target_format}"
        if target_format != old_ext:
            command = [ffmpeg, "-y", "-i", old_path, "-vn"]
            if target_format in MP4_CONTAINERS:
                if _ffmpeg_audio_codec(old_path) == "aac":
                    command += ["-c:a", "copy"]
                    if old_ext in ("ts", "aac"):
                        command += ["-bsf:a", "aac_adtstoasc"]
                else:
                    target_bitrate = explicit_bitrate or resolve_aac_bitrate(old_path)
                    command += ["-c:a", "aac", "-b:a", target_bitrate]
                command += ["-movflags", "+faststart"]
            elif can_copy_codec(old_ext, target_format):
                command += ["-c:a", "copy"]
            command.append(new_path)

            try:
                result = run_cancellable_subprocess(
                    command,
                    capture_output=capture,
                    cancel_event=cancel_event,
                )
            except DownloadCancelled:
                if os.path.exists(new_path):
                    os.remove(new_path)
                raise
            if result.returncode != 0 or not (os.path.exists(new_path) and os.path.getsize(new_path) > 0):
                if result.stderr:
                    logging.debug(result.stderr.decode("utf8", "replace"))
                if os.path.exists(new_path):
                    os.remove(new_path)
                raise GenericAudiobookDLException(
                    f"Failed converting '{os.path.basename(old_path)}' to .{target_format}"
                )
            os.remove(old_path)
        new_paths.append(new_path)
    return new_paths

def get_max_name_length() -> int:
    """
    Get the max length for file names supported by the OS

    :returns: max length for file names
    """
    pathconf = getattr(os, "pathconf", None)
    if callable(pathconf):
        try:
            # should work on Linux/MacOS
            return int(pathconf(".", "PC_NAME_MAX"))
        except OSError:
            pass
    try:
        # Windows
        from ctypes.wintypes import MAX_PATH
        return MAX_PATH
    except Exception:
        # default
        return 255

# Extra length reserved on output_dir/filename for extensions, sidecar json, and
# temp paths such as `.work/ts_parts/0000.ts` (22) or `.m4b.chapters.tmp.txt` (21)
OUTPUT_PATH_EXT_RESERVE = 32
WINDOWS_MAX_PATH = 260
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _sanitize_windows_reserved_name(name: str) -> str:
    """Append `_` to the stem when `name` matches a Windows reserved device name."""
    stripped = name.rstrip(". ")
    stem, dot, rest = stripped.partition(".")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        return f"{stem}_{dot}{rest}"
    return name


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate `text` so its UTF-8 encoding does not exceed `max_bytes` bytes."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _utf16_len(text: str) -> int:
    """Return length of `text` in UTF-16 code units (matching Windows Win32 path limits)."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_utf16_units(text: str, max_units: int) -> str:
    """Truncate `text` so its UTF-16 code unit count does not exceed `max_units`."""
    if max_units <= 0:
        return ""
    if _utf16_len(text) <= max_units:
        return text
    encoded = text.encode("utf-16-le")
    # Walk back by UTF-16 code units if we split a surrogate pair
    truncated = encoded[: max_units * 2].decode("utf-16-le", errors="ignore")
    return truncated


def _sanitize_metadata_dict(
    metadata: Optional[AudiobookMetadata],
    remove_chars: str,
) -> Dict[str, Any]:
    """Return metadata mapping with all string values sanitized for filesystem paths."""
    props = metadata.all_properties_dict() if metadata is not None else {}
    raw: Dict[str, Any] = {**LOCATION_DEFAULTS, **props}
    sanitized: Dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            cleaned = _fix_output(value)
            if remove_chars:
                cleaned = _remove_chars(cleaned, remove_chars)
            sanitized[key] = cleaned
        else:
            sanitized[key] = value
    return _SafeFormatDict(sanitized)


def _join_segments(segments: Sequence[str], seps: Sequence[str]) -> str:
    parts = [segments[0]]
    for sep, seg in zip(seps, segments[1:]):
        parts.append(sep)
        parts.append(seg)
    return "".join(parts)


def gen_output_location(
    template: str,
    metadata: Optional[AudiobookMetadata],
    remove_chars: str,
) -> str:
    """
    Generates the location of the output based on attributes of the audiobook.

    :param template: Python string template audiobook metadata is put into
    :param metadata: Audiobook metadata,
    :param remove_chars: List of characters to be removed from metadata values
    :returns: `template` with sanitized and length-bounded metadata inserted
    """
    metadata_dict = _sanitize_metadata_dict(metadata, remove_chars)
    is_windows = platform.system() == "Windows"
    component_cap = min(get_max_name_length(), 255)

    # Split template on path separators so we sanitize/bound each path segment
    # independently while preserving intentional directory separators in `template`.
    tokens = re.split(r"([/\\])", template)
    raw_segments = tokens[0::2]
    seps = tokens[1::2]
    segments: List[str] = []
    kept_seps: List[str] = []
    dynamic_indices: List[int] = []
    last_idx = len(raw_segments) - 1

    for idx, raw_seg in enumerate(raw_segments):
        if "{" in raw_seg:
            seg = raw_seg.format_map(metadata_dict)
            if is_windows:
                seg = seg.rstrip(". ")
            if seg == "" and idx < last_idx:
                continue
            if seg in ("", ".", ".."):
                seg = "_"
            if is_windows:
                seg = _sanitize_windows_reserved_name(seg)
            reserve = OUTPUT_PATH_EXT_RESERVE if idx == last_idx else 0
            seg_limit = max(1, component_cap - reserve)
            seg = _truncate_utf8(seg, seg_limit)
            if is_windows:
                seg = _sanitize_windows_reserved_name(seg.rstrip(". ") or "_")
            dynamic_indices.append(len(segments))
        else:
            seg = raw_seg
        segments.append(seg)
        if idx < len(seps):
            kept_seps.append(seps[idx])

    truncated_for_length = False
    if is_windows and dynamic_indices:
        abs_path = os.path.abspath(_join_segments(segments, kept_seps))
        max_total_units = WINDOWS_MAX_PATH - 1 - OUTPUT_PATH_EXT_RESERVE
        excess = _utf16_len(abs_path) - max_total_units
        if excess > 0:
            # Trim dynamic segments from right to left (preserving at least 32 units
            # on the final segment when earlier dynamic segments can absorb overflow).
            for pos in range(len(dynamic_indices) - 1, -1, -1):
                if excess <= 0:
                    break
                seg_idx = dynamic_indices[pos]
                seg = segments[seg_idx]
                cur_units = _utf16_len(seg)
                min_keep = 32 if pos > 0 and cur_units > 32 else 1
                reducible = max(0, cur_units - min_keep)
                trim = min(excess, reducible)
                if trim > 0:
                    new_seg = _sanitize_windows_reserved_name(
                        _truncate_utf16_units(seg, cur_units - trim).rstrip(". ") or "_"
                    )
                    excess -= cur_units - _utf16_len(new_seg)
                    segments[seg_idx] = new_seg
                    truncated_for_length = True

    formatted = _join_segments(segments, kept_seps)
    if truncated_for_length:
        logging.log(f"output path too long, using [blue]{segments[-1]}[/blue] as filename base")
    return formatted


def _fix_output(value: str) -> str:
    """Returns `value` without characters or segments the filesystem can't handle safely"""
    value = value.replace("/", "-").replace("\\", "-")
    value = "".join(ch for ch in value if ord(ch) >= 0x20)
    if platform.system() == "Windows":
        value = _remove_chars(value, ':*?<>|"\'’')
        value = _sanitize_windows_reserved_name(value)
    if value in (".", ".."):
        value = "-" * len(value)
    return value


def _remove_chars(s: str, chars: str) -> str:
    """Removes `chars` from `s`"""
    for i in chars:
        s = s.replace(i, "")
    return s

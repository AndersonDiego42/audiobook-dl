from audiobookdl import logging, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
    MissingDependency,
)
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.output.metadata._chapters import normalize_chapters
from audiobookdl.output.metadata._cover import normalize_cover_image
from audiobookdl.utils import (
    AudioProbeInfo,
    MAX_AUTO_M4B_BITRATE_KBPS,
    MIN_AUTO_M4B_BITRATE_KBPS,
    DEFAULT_M4B_BITRATE,
    MP4_CONTAINERS,
    find_executable,
    normalize_audio_bitrate,
    probe_audio_info,
    resolve_audio_bitrate,
)
from audiobookdl.utils.concurrency import (
    ffmpeg_progress_callback,
    run_cancellable_pool,
    run_cancellable_subprocess,
)

import os
import re
import shutil
import platform
import tempfile
import threading
import uuid
from typing import Any
from collections.abc import Sequence, Mapping
from urllib.parse import urlparse

LOCATION_DEFAULTS = {
    'album': 'NA',
    'artist': 'NA',
    'title': '',
    'scrape_url': '',
    'series': '',
    'series_order': '',
    'series_order_padded': '',
    'author': '',
    'narrator': '',
    'genre': '',
    'language': '',
    'description': '',
    'isbn': '',
    'asin': '',
    'publisher': '',
    'release_date': '',
    'year': '',
    'source': '',
}

OUTPUT_TEMPLATE_PRESETS: dict[str, str] = {
    "default": "{title}",
    "audiobookshelf": "{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}",
    "plex": "{author}/{title}/{title}",
    "kavita": "{author}/{series?{series}/}{year?({year}) }{title}",
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


def _probe_audio_info(path: str) -> AudioProbeInfo:
    """Return consolidated media info for `path` via a single `ffprobe` call."""
    return probe_audio_info(path)


_PROBE_CACHE_LOCK = threading.Lock()


def get_cached_probe(
    probe_cache: dict[str, AudioProbeInfo] | None,
    path: str,
) -> AudioProbeInfo | None:
    """Thread-safely return the cached `AudioProbeInfo` for `path`, or `None` on miss."""
    if probe_cache is None:
        return None
    with _PROBE_CACHE_LOCK:
        return probe_cache.get(path)


def store_cached_probe(
    probe_cache: dict[str, AudioProbeInfo] | None,
    path: str,
    info: AudioProbeInfo,
) -> None:
    """Thread-safely record `info` under `path` when `probe_cache` is provided."""
    if probe_cache is None:
        return
    with _PROBE_CACHE_LOCK:
        probe_cache[path] = info


def move_cached_probe(
    probe_cache: dict[str, AudioProbeInfo] | None,
    old_path: str,
    new_path: str,
) -> None:
    """Thread-safely move a cached `AudioProbeInfo` entry from `old_path` to `new_path`."""
    if probe_cache is None or old_path == new_path:
        return
    with _PROBE_CACHE_LOCK:
        info = probe_cache.pop(old_path, None)
        if info is not None:
            probe_cache[new_path] = info


def _get_cached_probe(
    path: str,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> AudioProbeInfo:
    """Return cached `AudioProbeInfo` for `path`, probing once and storing on miss."""
    if probe_cache is not None:
        with _PROBE_CACHE_LOCK:
            cached = probe_cache.get(path)
        if cached is not None:
            return cached
    info = _probe_audio_info(path)
    if probe_cache is not None:
        with _PROBE_CACHE_LOCK:
            probe_cache[path] = info
    return info


def _ffmpeg_audio_codec(path: str) -> str:
    """Returns the codec name of the first audio stream in `path` (empty on failure)"""
    return _probe_audio_info(path).codec_name


def _ffprobe_duration(path: str) -> float:
    """Returns the duration of `path` in seconds (0.0 on failure)"""
    return _probe_audio_info(path).duration


def verify_audio_integrity(
    path: str,
    *,
    expected_duration: float = 0.0,
    min_duration_ratio: float = 0.98,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    require_ffprobe: bool = False,
) -> AudioProbeInfo:
    """
    Validate that `path` exists, is non-empty, has a valid audio stream and positive duration,
    and was not materially truncated relative to `expected_duration`.

    Raises `FailedCombining` on verification failure.
    """
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        raise FailedCombining(message=f"Media file is missing or empty: {path}")

    info = _get_cached_probe(path, probe_cache)
    if info.probe_error:
        logging.debug(
            f"Media integrity check aborted for '{os.path.basename(path)}': {info.probe_error}"
        )
        raise GenericAudiobookDLException(
            "Media integrity verification failed because ffprobe could not run",
            info.probe_error,
        )
    ffprobe_available = require_ffprobe or (find_executable("ffprobe") is not None)

    if expected_duration > 0 and info.duration < expected_duration * min_duration_ratio:
        logging.debug(
            f"Media output '{os.path.basename(path)}' is shorter than expected "
            f"({info.duration:.1f}s vs {expected_duration:.1f}s)"
        )
        raise FailedCombining(
            message=f"Media file appears truncated ({info.duration:.1f}s vs {expected_duration:.1f}s)"
        )

    if not info.codec_name or info.duration <= 0.0:
        if ffprobe_available or info.duration > 0.0 or info.codec_name:
            logging.debug(
                f"Media integrity check failed for '{os.path.basename(path)}': "
                f"codec={info.codec_name!r}, duration={info.duration:.2f}s"
            )
            raise FailedCombining(
                message=f"Invalid or corrupt media output: {os.path.basename(path)}"
            )

    return info


def _ffprobe_audio_bitrate(path: str) -> int | None:
    """Return the bitrate of `path` in bits/sec via `ffprobe`, or `None` if unavailable."""
    return _probe_audio_info(path).bitrate


def _resolve_aac_bitrate_from_info(
    source_path: str,
    info: AudioProbeInfo,
    audio_bitrate: str | None = None,
) -> str:
    """Resolve AAC bitrate using already-probed `info` without re-invoking `ffprobe`."""
    explicit = normalize_audio_bitrate(audio_bitrate)
    if explicit is not None:
        return explicit
    bps = info.bitrate
    if bps is None or bps <= 0:
        return DEFAULT_M4B_BITRATE
    kbps = max(MIN_AUTO_M4B_BITRATE_KBPS, min(round(bps / 1000), MAX_AUTO_M4B_BITRATE_KBPS))
    return f"{kbps}k"


def resolve_aac_bitrate(source_path: str, audio_bitrate: str | None = None) -> str:
    """
    Determine the target AAC bitrate when transcoding `source_path` to an MP4 container.

    Uses `audio_bitrate` directly when set (e.g. `"192k"`), or probes `source_path`
    via `ffprobe` in `"auto"`/`None` mode and clamps to `[64k, 192k]` (falling back
    to `128k` if probing fails).
    """
    return resolve_audio_bitrate(source_path, audio_bitrate, probe_bps=_ffprobe_audio_bitrate)


def _run_mpegts_part(
    source: str,
    ts_path: str,
    codec_args: Sequence[str],
    cancel_event: threading.Event | None = None,
) -> bool:
    """Run an FFmpeg step producing a single MPEG-TS segment at `ts_path`."""
    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    try:
        result = run_cancellable_subprocess(
            [ffmpeg, "-y", "-i", source, "-vn", *codec_args, "-f", "mpegts", ts_path],
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


def _remux_to_mpegts(
    source: str,
    ts_path: str,
    cancel_event: threading.Event | None = None,
) -> bool:
    """
    Remux a single audio file into an MPEG-TS container.

    Concatenating raw segments directly (e.g. ADTS-AAC) desyncs the decoder at
    segment boundaries and silently drops most of the audio. Remuxing each
    segment to MPEG-TS first gives every part clean framing and timestamps, so
    the following concatenation is lossless.

    :returns: `True` if a non-empty file was produced
    """
    return _run_mpegts_part(source, ts_path, ("-c:a", "copy"), cancel_event=cancel_event)


def _encode_to_aac_mpegts(
    source: str,
    ts_path: str,
    target_bitrate: str,
    sample_rate: int | None = None,
    channels: int | None = None,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Transcode a single non-AAC segment directly into an AAC MPEG-TS part."""
    codec_args: list[str] = ["-c:a", "aac", "-b:a", target_bitrate]
    if sample_rate is not None and sample_rate > 0:
        codec_args += ["-ar", str(sample_rate)]
    if channels is not None and channels > 0:
        codec_args += ["-ac", str(channels)]
    return _run_mpegts_part(source, ts_path, codec_args, cancel_event=cancel_event)


def _probe_uncached_audio_info(
    paths: Sequence[str],
    cache: dict[str, AudioProbeInfo],
    cancel_event: threading.Event | None = None,
) -> list[AudioProbeInfo]:
    """Populate `cache` for any uncached `paths` in parallel and return infos in order."""
    with _PROBE_CACHE_LOCK:
        uncached = [p for p in dict.fromkeys(paths) if p not in cache]
    if uncached:
        probed = run_cancellable_pool(
            _probe_audio_info,
            uncached,
            max_workers=COMBINE_REMUX_THREADS,
            cancel_event=cancel_event,
        )
        with _PROBE_CACHE_LOCK:
            for path, info in zip(uncached, probed, strict=True):
                cache[path] = info
    with _PROBE_CACHE_LOCK:
        return [cache[p] for p in paths]


_MPEGTS_PACKET_SIZE = 188
_MPEGTS_SYNC_BYTE = 0x47


def _is_mpegts_file(path: str) -> bool:
    """Return True when `path` is a `.ts` file with valid MPEG-TS 188-byte sync markers."""
    if get_extension(path) != "ts":
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(_MPEGTS_PACKET_SIZE * 3)
    except OSError:
        return False
    if len(header) < _MPEGTS_PACKET_SIZE or header[0] != _MPEGTS_SYNC_BYTE:
        return False
    if len(header) >= _MPEGTS_PACKET_SIZE * 2 and header[_MPEGTS_PACKET_SIZE] != _MPEGTS_SYNC_BYTE:
        return False
    if len(header) >= _MPEGTS_PACKET_SIZE * 3 and header[_MPEGTS_PACKET_SIZE * 2] != _MPEGTS_SYNC_BYTE:
        return False
    return True


def _all_inputs_mpegts(
    filepaths: Sequence[str],
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> bool:
    """Return True when every input segment is already a valid MPEG-TS file."""
    if not filepaths or not all(_is_mpegts_file(path) for path in filepaths):
        return False
    fmt = _get_cached_probe(filepaths[0], probe_cache).format_name
    if fmt and "mpegts" not in fmt.split(","):
        return False
    return True


def _write_ffmetadata(
    chapter_path: str,
    chapters: Sequence[Chapter] | None = None,
    duration_sec: float = 0.0,
    metadata: AudiobookMetadata | None = None,
) -> tuple[bool, bool]:
    """Write FFMETADATA1 global tags and/or bounded chapters; return ``(chapters_written, metadata_written)``."""
    meta_text = ffmpeg_metadata.create_metadata_text(metadata)
    chapters_text = ""
    if chapters and duration_sec > 0:
        bounded = normalize_chapters(chapters, int(duration_sec * 1000))
        if bounded:
            chapters_text = "".join(
                ffmpeg_metadata.create_chapter_text(title=title, start=start, end=end)
                for start, end, title in bounded
            )
    if not meta_text and not chapters_text:
        return False, False
    with open(chapter_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(";FFMETADATA1\n" + meta_text + chapters_text)
    return bool(chapters_text), bool(meta_text)


def _write_ffmetadata_chapters(
    chapter_path: str,
    chapters: Sequence[Chapter],
    duration_sec: float,
) -> bool:
    """Write FFMETADATA1 chapter entries bounded to `duration_sec`; return True if any written."""
    chapters_written, _ = _write_ffmetadata(
        chapter_path,
        chapters=chapters,
        duration_sec=duration_sec,
        metadata=None,
    )
    return chapters_written


def _prepare_ffmpeg_cover_file(
    cover: Cover | None,
    output_ext: str,
    cover_base_path: str,
) -> str | None:
    """Materialize a normalized cover image on disk when inline FFmpeg attachment is supported."""
    if cover is None or output_ext not in MP4_CONTAINERS:
        return None
    image_data, ext = normalize_cover_image(cover)
    cover_path = f"{cover_base_path}.{ext}"
    with open(cover_path, "wb") as f:
        f.write(image_data)
    return cover_path


def _append_ffmetadata_and_cover_args(
    command: list[str],
    meta_file: str,
    chapters_written: bool,
    meta_written: bool,
    cover_path: str | None,
) -> None:
    """Append `-i` and stream mapping flags for inline chapters, metadata tags, and cover art."""
    next_input = 1
    meta_idx: int | None = None
    cover_idx: int | None = None
    if chapters_written or meta_written:
        command += ["-i", meta_file]
        meta_idx = next_input
        next_input += 1
    if cover_path is not None:
        command += ["-i", cover_path]
        cover_idx = next_input
        next_input += 1
    if meta_idx is not None or cover_idx is not None:
        command += ["-map", "0:a"]
    if meta_idx is not None:
        if chapters_written:
            command += ["-map_chapters", str(meta_idx)]
        if meta_written:
            command += ["-map_metadata", str(meta_idx)]
        command += ["-metadata:s:a:0", "title="]
    if cover_idx is not None:
        command += ["-map", f"{cover_idx}:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        command += ["-vn"]


def _build_reencode_codec_args(
    target_ext: str,
    source_path: str,
    info: AudioProbeInfo,
    audio_bitrate: str | None,
    faststart: bool = True,
    *,
    loudnorm: bool = False,
    mono: bool = False,
) -> list[str]:
    """Return FFmpeg codec/container flags forcing re-encode into `target_ext`."""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    filter_args: list[str] = []
    if loudnorm:
        filter_args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    if mono:
        filter_args += ["-ac", "1"]

    if target_ext in MP4_CONTAINERS:
        target_bitrate = _resolve_aac_bitrate_from_info(source_path, info, explicit_bitrate)
        args = [*filter_args, "-c:a", "aac", "-b:a", target_bitrate]
        if faststart:
            args += ["-movflags", "+faststart"]
        return args
    if target_ext == "mp3":
        bitrate = explicit_bitrate or (
            f"{max(64, min(round(info.bitrate / 1000), 320))}k"
            if info.bitrate and info.bitrate > 0
            else "192k"
        )
        return [*filter_args, "-c:a", "libmp3lame", "-b:a", bitrate]
    if explicit_bitrate is not None and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS:
        return [*filter_args, "-b:a", explicit_bitrate]
    return filter_args


def _build_mux_codec_args(
    source_ext: str,
    target_ext: str,
    source_path: str,
    info: AudioProbeInfo,
    audio_bitrate: str | None,
    faststart: bool = True,
    *,
    loudnorm: bool = False,
    mono: bool = False,
) -> list[str]:
    """Return FFmpeg codec/container flags for muxing or transcoding to `target_ext`."""
    if loudnorm or mono:
        return _build_reencode_codec_args(
            target_ext,
            source_path,
            info,
            audio_bitrate,
            faststart=faststart,
            loudnorm=loudnorm,
            mono=mono,
        )
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    codec = info.codec_name
    if target_ext in MP4_CONTAINERS:
        if codec == "aac":
            args = ["-c:a", "copy"]
            if source_ext in ("ts", "aac"):
                args += ["-bsf:a", "aac_adtstoasc"]
        else:
            target_bitrate = _resolve_aac_bitrate_from_info(source_path, info, explicit_bitrate)
            args = ["-c:a", "aac", "-b:a", target_bitrate]
        if faststart:
            args += ["-movflags", "+faststart"]
        return args
    if target_ext == source_ext or target_ext in ("mka", "mkv") or (target_ext == "mp3" and codec == "mp3"):
        return ["-c:a", "copy"]
    if explicit_bitrate is not None and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS:
        return ["-b:a", explicit_bitrate]
    return []


def apply_chapter_offset(chapters: Sequence[Chapter], offset_ms: int) -> list[Chapter]:
    """Shift chapter markers (preserving initial marker at `0`) by `offset_ms` milliseconds."""
    if not chapters or offset_ms == 0:
        return list(chapters)
    shifted: list[Chapter] = []
    for idx, ch in enumerate(chapters):
        if idx == 0 and int(ch.start) == 0:
            shifted.append(Chapter(start=0, title=ch.title))
        else:
            shifted.append(Chapter(start=max(0, int(ch.start) + int(offset_ms)), title=ch.title))
    return shifted


_SILENCE_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")


def detect_silence_chapters(
    filepath: str,
    *,
    noise_db: float = -35.0,
    min_silence_sec: float = 1.8,
    cancel_event: threading.Event | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> list[Chapter]:
    """Run FFmpeg `silencedetect` on `filepath` and synthesize chapter markers at pauses."""
    ffmpeg = _require_executable("ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        filepath,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f",
        "null",
        "-",
    ]
    result = run_cancellable_subprocess(command, capture_output=True, cancel_event=cancel_event)
    if result.returncode != 0:
        return []
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    info = _get_cached_probe(filepath, probe_cache)
    duration_ms = round(info.duration * 1000) if info.duration > 0 else None

    starts = [0]
    for match in _SILENCE_END_RE.finditer(stderr_text):
        ts_ms = round(float(match.group(1)) * 1000)
        if ts_ms <= starts[-1] + 5000:
            continue
        if duration_ms is not None and ts_ms >= max(0, duration_ms - 5000):
            continue
        starts.append(ts_ms)
    return [Chapter(start=ms, title=f"Chapter {idx}") for idx, ms in enumerate(starts, start=1)]


_MAX_CONCAT_ARG_CHARS = 16000
_MAX_CONCAT_PROTOCOL_FILES = 64


def _transcode_pool_workers() -> int:
    """Return bounded worker count for CPU-bound parallel FFmpeg transcodes."""
    return min(COMBINE_REMUX_THREADS, max(1, os.cpu_count() or 4))


def _write_concat_list_file(concat_path: str, filepaths: Sequence[str]) -> None:
    """Write an FFmpeg concat-demuxer input manifest using safe single-quote escaping."""
    with open(concat_path, "w", encoding="utf-8", newline="\n") as f:
        for part in filepaths:
            escaped = os.path.abspath(part).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


def _encode_uniform_concat_part(
    source: str,
    dst_path: str,
    codec: str,
    *,
    bitrate: str | None = None,
    sample_rate: int | None = None,
    channels: int | None = None,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Transcode a heterogeneous input track into a uniform intermediate part for concat demuxing."""
    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    cmd = [ffmpeg, "-y", "-i", source, "-vn", "-c:a", codec]
    if bitrate:
        cmd += ["-b:a", bitrate]
    if sample_rate is not None and sample_rate > 0:
        cmd += ["-ar", str(sample_rate)]
    if channels is not None and channels > 0:
        cmd += ["-ac", str(channels)]
    cmd.append(dst_path)
    try:
        result = run_cancellable_subprocess(
            cmd,
            capture_output=capture,
            cancel_event=cancel_event,
        )
    except DownloadCancelled:
        if os.path.exists(dst_path):
            os.remove(dst_path)
        raise
    if result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
        return True
    if result.stderr:
        logging.debug(result.stderr.decode("utf8", "replace"))
    return False


def combine_audiofiles(
    filepaths: Sequence[str],
    tmp_dir: str,
    output_path: str,
    cancel_event: threading.Event | None = None,
    chapters: Sequence[Chapter] | None = None,
    audio_bitrate: str | None = None,
    metadata: AudiobookMetadata | None = None,
    cover: Cover | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    faststart: bool = True,
    loudnorm: bool = False,
    mono: bool = False,
) -> bool:
    """
    Combines the given audiofiles into `output_path` (optionally transcoding and
    embedding `chapters`, `metadata` tags, and MP4 `cover` art in the primary
    FFmpeg pass).

    :returns: `True` if `chapters` were embedded during the combine pass.
    """
    if not filepaths:
        raise FailedCombining
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    normalize_audio_bitrate(audio_bitrate)
    ffmpeg = _require_executable("ffmpeg")
    cache: dict[str, AudioProbeInfo] = probe_cache if probe_cache is not None else {}
    output_extension = get_extension(output_path)
    work_dir = f"{os.path.normpath(tmp_dir)}.work"
    ts_dir = os.path.join(work_dir, "ts_parts")
    combined_ts = os.path.join(work_dir, "combined.ts")
    chapter_file = os.path.join(work_dir, "ffmetadata.txt")
    cover_base_path = os.path.join(work_dir, "cover")
    cover_tmp_path: str | None = None
    os.makedirs(work_dir, exist_ok=True)
    chapters_embedded = False
    try:
        all_mpegts = _all_inputs_mpegts(filepaths, probe_cache=cache)
        input_infos: list[AudioProbeInfo] | None = None
        mixed_non_mp4_params = False
        if not all_mpegts and len(filepaths) > 1 and output_extension not in MP4_CONTAINERS:
            input_infos = _probe_uncached_audio_info(filepaths, cache, cancel_event=cancel_event)
            codecs = {i.codec_name for i in input_infos if i.codec_name}
            sample_rates = {i.sample_rate for i in input_infos if i.sample_rate}
            channel_counts = {i.channels for i in input_infos if i.channels}
            mixed_non_mp4_params = (
                len(codecs) > 1 or len(sample_rates) > 1 or len(channel_counts) > 1
            )

        all_mp3_uniform = (
            output_extension == "mp3"
            and all(get_extension(p) == "mp3" for p in filepaths)
            and not mixed_non_mp4_params
        )
        use_mpegts_pipeline = (
            all_mpegts
            or output_extension in MP4_CONTAINERS
            or all_mp3_uniform
        )

        concat_demuxer_mode = False
        ffmpeg_input_flags: list[str] = []
        mux_source_ext = get_extension(filepaths[0])
        sample_info: AudioProbeInfo

        if use_mpegts_pipeline:
            parallel_aac_encoded = False
            if all_mpegts:
                ts_paths = list(filepaths)
            else:
                os.makedirs(ts_dir, exist_ok=True)
                target_bitrate = ""
                target_sr: int | None = None
                target_ch: int | None = None
                if len(filepaths) > 1 and output_extension in MP4_CONTAINERS:
                    input_infos = _probe_uncached_audio_info(filepaths, cache, cancel_event=cancel_event)
                    sample_rates = {i.sample_rate for i in input_infos if i.sample_rate}
                    channel_counts = {i.channels for i in input_infos if i.channels}
                    any_non_aac = any(i.codec_name != "aac" for i in input_infos)
                    mixed_stream_params = len(sample_rates) > 1 or len(channel_counts) > 1
                    if any_non_aac or mixed_stream_params:
                        parallel_aac_encoded = True
                        bitrate_info = next((i for i in input_infos if i.bitrate and i.bitrate > 0), input_infos[0])
                        target_bitrate = _resolve_aac_bitrate_from_info(filepaths[0], bitrate_info, audio_bitrate)
                        target_sr = max(sample_rates) if sample_rates else None
                        target_ch = max(channel_counts) if channel_counts else None

                padding = len(str(len(filepaths)))
                total_parts = len(filepaths)
                completed_parts = 0
                part_lock = threading.Lock()
                verb = "Encoding parts" if parallel_aac_encoded else "Remuxing parts"
                pool_workers = _transcode_pool_workers() if parallel_aac_encoded else COMBINE_REMUX_THREADS

                with logging.progress([]) as remux_prog:
                    remux_task = remux_prog.add_task(
                        f"{verb} (0/{total_parts})",
                        total=float(total_parts),
                    )

                    def remux(item):
                        nonlocal completed_parts
                        index, source = item
                        ts_path = os.path.join(ts_dir, f"{str(index).zfill(padding)}.ts")
                        with _PROBE_CACHE_LOCK:
                            src_info = input_infos[index] if input_infos is not None else cache.get(source)
                        needs_encode = parallel_aac_encoded and (
                            src_info is None
                            or src_info.codec_name != "aac"
                            or (target_sr is not None and src_info.sample_rate not in (None, target_sr))
                            or (target_ch is not None and src_info.channels not in (None, target_ch))
                        )
                        if needs_encode:
                            ok = _encode_to_aac_mpegts(
                                source,
                                ts_path,
                                target_bitrate,
                                sample_rate=target_sr,
                                channels=target_ch,
                                cancel_event=cancel_event,
                            )
                        else:
                            ok = _remux_to_mpegts(source, ts_path, cancel_event=cancel_event)
                        if ok:
                            if src_info is None and len(filepaths) > 1:
                                src_info = _get_cached_probe(source, cache)
                            if src_info is not None and src_info.duration > 0:
                                with _PROBE_CACHE_LOCK:
                                    if ts_path not in cache:
                                        cache[ts_path] = AudioProbeInfo(
                                            duration=src_info.duration,
                                            bitrate=src_info.bitrate,
                                            codec_name="aac" if (parallel_aac_encoded or src_info.codec_name == "aac") else src_info.codec_name,
                                            format_name="mpegts",
                                            sample_rate=target_sr if needs_encode else src_info.sample_rate,
                                            channels=target_ch if needs_encode else src_info.channels,
                                        )
                        with part_lock:
                            completed_parts += 1
                            remux_prog.advance(remux_task, 1.0)
                            logging.book_update(f"{verb} ({completed_parts}/{total_parts})")
                        return ts_path, ok

                    remuxed = run_cancellable_pool(
                        remux,
                        list(enumerate(filepaths)),
                        max_workers=pool_workers,
                        cancel_event=cancel_event,
                    )
                ts_paths = []
                for ts_path, ok in remuxed:
                    if not ok:
                        raise FailedCombining
                    ts_paths.append(ts_path)

            if len(ts_paths) == 1:
                ffmpeg_input = ts_paths[0]
            else:
                concat_candidate = "concat:" + "|".join(ts_paths)
                if (
                    len(ts_paths) <= _MAX_CONCAT_PROTOCOL_FILES
                    and "|" not in "".join(ts_paths)
                    and len(concat_candidate) <= _MAX_CONCAT_ARG_CHARS
                ):
                    ffmpeg_input = concat_candidate
                else:
                    with open(combined_ts, "wb") as out:
                        for ts_path in ts_paths:
                            if cancel_event is not None and cancel_event.is_set():
                                raise DownloadCancelled
                            with open(ts_path, "rb") as part:
                                shutil.copyfileobj(part, out)
                    ffmpeg_input = combined_ts

            sample_info = _get_cached_probe(ts_paths[0], cache)
            if len(ts_paths) == 1:
                expected = sample_info.duration
                chapter_duration = expected
            else:
                part_total = 0.0
                if ffmpeg_input != combined_ts or chapters:
                    part_infos = _probe_uncached_audio_info(ts_paths, cache, cancel_event=cancel_event)
                    part_total = sum(info.duration for info in part_infos)
                if ffmpeg_input == combined_ts:
                    expected = _get_cached_probe(combined_ts, cache).duration
                    # Binary-concatenated multi-part MPEG-TS can reset PTS at junctions so ffprobe on
                    # `combined_ts` under-reports duration; sum part durations when bounding chapters.
                    chapter_duration = max(expected, part_total) if chapters else expected
                else:
                    expected = part_total
                    chapter_duration = part_total
            if output_extension in MP4_CONTAINERS:
                mux_source_ext = "ts"
        else:
            # Lossless, Xiph, Matroska, or mixed-parameter non-MP4 combine via FFmpeg concat demuxer
            concat_demuxer_mode = True
            concat_sources = list(filepaths)
            if len(filepaths) > 1 and mixed_non_mp4_params:
                norm_dir = os.path.join(work_dir, "norm_parts")
                os.makedirs(norm_dir, exist_ok=True)
                assert input_infos is not None
                sample_rates = {i.sample_rate for i in input_infos if i.sample_rate}
                channel_counts = {i.channels for i in input_infos if i.channels}
                target_sr = max(sample_rates) if sample_rates else None
                target_ch = 1 if mono else (max(channel_counts) if channel_counts else None)
                if output_extension == "wav":
                    inter_ext, inter_codec, inter_bitrate = "wav", "pcm_s16le", None
                else:
                    inter_ext, inter_codec, inter_bitrate = "flac", "flac", None
                padding = len(str(len(filepaths)))

                def _normalize_part(item: tuple[int, str]) -> tuple[str, bool]:
                    idx, src = item
                    dst = os.path.join(norm_dir, f"{str(idx).zfill(padding)}.{inter_ext}")
                    ok = _encode_uniform_concat_part(
                        src,
                        dst,
                        inter_codec,
                        bitrate=inter_bitrate,
                        sample_rate=target_sr,
                        channels=target_ch,
                        cancel_event=cancel_event,
                    )
                    if ok:
                        src_info = input_infos[idx]
                        store_cached_probe(
                            cache,
                            dst,
                            AudioProbeInfo(
                                duration=src_info.duration,
                                bitrate=src_info.bitrate,
                                codec_name=inter_codec,
                                format_name=inter_ext,
                                sample_rate=target_sr or src_info.sample_rate,
                                channels=target_ch or src_info.channels,
                            ),
                        )
                    return dst, ok

                normalized = run_cancellable_pool(
                    _normalize_part,
                    list(enumerate(filepaths)),
                    max_workers=_transcode_pool_workers(),
                    cancel_event=cancel_event,
                )
                concat_sources = []
                for dst, ok in normalized:
                    if not ok:
                        raise FailedCombining
                    concat_sources.append(dst)

            mux_source_ext = get_extension(concat_sources[0])
            sample_info = _get_cached_probe(concat_sources[0], cache)
            if len(concat_sources) == 1:
                ffmpeg_input = concat_sources[0]
                expected = sample_info.duration
            else:
                concat_list_path = os.path.join(work_dir, "concat_list.txt")
                _write_concat_list_file(concat_list_path, concat_sources)
                ffmpeg_input_flags = ["-f", "concat", "-safe", "0"]
                ffmpeg_input = concat_list_path
                part_infos = _probe_uncached_audio_info(concat_sources, cache, cancel_event=cancel_event)
                expected = sum(info.duration for info in part_infos)
            chapter_duration = expected

        chapters_written, meta_written = _write_ffmetadata(
            chapter_file,
            chapters=chapters,
            duration_sec=chapter_duration,
            metadata=metadata,
        )
        cover_tmp_path = _prepare_ffmpeg_cover_file(cover, output_extension, cover_base_path)

        command = [ffmpeg, "-y", *ffmpeg_input_flags, "-i", ffmpeg_input]
        if chapters_written:
            logging.book_update("Adding chapters")
            chapters_embedded = True
        _append_ffmetadata_and_cover_args(
            command,
            chapter_file,
            chapters_written=chapters_written,
            meta_written=meta_written,
            cover_path=cover_tmp_path,
        )
        # Note: concat_demuxer_mode with mixed non-MP4 inputs converted via lossless FLAC still targets output_extension cleanly
        _ = concat_demuxer_mode
        command += _build_mux_codec_args(
            mux_source_ext,
            output_extension,
            concat_sources[0] if not use_mpegts_pipeline else ts_paths[0],
            sample_info,
            audio_bitrate,
            faststart=faststart,
            loudnorm=loudnorm,
            mono=mono,
        )
        capture = not logging.is_ffmpeg_output()
        if capture and expected > 0:
            command += ["-progress", "pipe:1", "-nostats"]
        command.append(output_path)
        if capture and expected > 0:
            with logging.progress([]) as mux_prog:
                mux_task = mux_prog.add_task(f"Combining to .{output_extension}", total=expected)
                reported = 0.0

                def _on_mux_progress(seconds: float) -> None:
                    nonlocal reported
                    target = min(expected, max(0.0, seconds))
                    delta = target - reported
                    if delta > 0:
                        reported = target
                        mux_prog.advance(mux_task, delta)

                with ffmpeg_progress_callback(_on_mux_progress):
                    result = run_cancellable_subprocess(
                        command,
                        capture_output=capture,
                        cancel_event=cancel_event,
                    )
                if reported < expected:
                    mux_prog.advance(mux_task, expected - reported)
        else:
            result = run_cancellable_subprocess(
                command,
                capture_output=capture,
                cancel_event=cancel_event,
            )
        if (
            result.returncode != 0
            or not (os.path.exists(output_path) and os.path.getsize(output_path) > 0)
        ):
            if result.stderr:
                logging.debug(result.stderr.decode("utf8", "replace"))
            raise FailedCombining
        # Guard against silent truncation or corrupt output before committing.
        verify_audio_integrity(
            output_path,
            expected_duration=expected,
            probe_cache=cache,
        )
        return chapters_embedded
    except (DownloadCancelled, FailedCombining, GenericAudiobookDLException):
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


def combine_supports_direct_output(
    sample_path: str,
    output_format: str,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> bool:
    """Return True when `combine_audiofiles` can remux directly into `output_format`."""
    current = get_extension(sample_path).lower()
    target = output_format.lstrip(".").lower()
    if current == target:
        return True
    if target in MP4_CONTAINERS and _get_cached_probe(sample_path, probe_cache).codec_name == "aac":
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


def convert_single_file(
    old_path: str,
    output_format: str,
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    chapters: Sequence[Chapter] | None = None,
    remove_source: bool = True,
    metadata: AudiobookMetadata | None = None,
    cover: Cover | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    output_path: str | None = None,
    faststart: bool = True,
    loudnorm: bool = False,
    mono: bool = False,
) -> tuple[str, bool]:
    """
    Convert a single file to `output_format`, optionally embedding `chapters`,
    `metadata` tags, and MP4 `cover` art in the same FFmpeg pass.
    Returns `(new_path, chapters_embedded)`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_format = output_format.lstrip(".").lower()
    path_without_ext, old_ext_raw = os.path.splitext(old_path)
    old_ext = old_ext_raw.lstrip(".").lower()
    new_path = output_path or f"{path_without_ext}.{target_format}"
    if (
        not loudnorm
        and not mono
        and target_format == old_ext
        and (output_path is None or os.path.abspath(new_path) == os.path.abspath(old_path))
    ):
        return new_path, False

    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    info = _get_cached_probe(old_path, probe_cache)
    chapter_file = f"{new_path}.chapters.tmp.txt"
    cover_tmp_path: str | None = None
    chapters_embedded = False

    chapters_written, meta_written = _write_ffmetadata(
        chapter_file,
        chapters=chapters,
        duration_sec=info.duration,
        metadata=metadata,
    )
    cover_tmp_path = _prepare_ffmpeg_cover_file(cover, target_format, f"{new_path}.cover.tmp")

    command = [ffmpeg, "-y", "-i", old_path]
    if chapters_written:
        logging.book_update("Adding chapters")
        chapters_embedded = True
    _append_ffmetadata_and_cover_args(
        command,
        chapter_file,
        chapters_written=chapters_written,
        meta_written=meta_written,
        cover_path=cover_tmp_path,
    )
    command += _build_mux_codec_args(
        old_ext,
        target_format,
        old_path,
        info,
        explicit_bitrate,
        faststart=faststart,
        loudnorm=loudnorm,
        mono=mono,
    )
    if capture and info.duration > 0:
        command += ["-progress", "pipe:1", "-nostats"]
    new_dir = os.path.dirname(os.path.abspath(new_path)) or os.getcwd()
    os.makedirs(new_dir, exist_ok=True)
    staged_path = os.path.join(
        new_dir,
        f".{os.path.basename(new_path)}.tmp.{uuid.uuid4().hex[:8]}.{target_format}",
    )
    command.append(staged_path)

    try:
        if capture and info.duration > 0:
            with logging.progress([]) as conv_prog:
                conv_task = conv_prog.add_task(
                    f"Converting {os.path.basename(old_path)}",
                    total=info.duration,
                )
                reported = 0.0

                def _on_conv_progress(seconds: float) -> None:
                    nonlocal reported
                    target = min(info.duration, max(0.0, seconds))
                    delta = target - reported
                    if delta > 0:
                        reported = target
                        conv_prog.advance(conv_task, delta)

                with ffmpeg_progress_callback(_on_conv_progress):
                    result = run_cancellable_subprocess(
                        command,
                        capture_output=capture,
                        cancel_event=cancel_event,
                    )
                if reported < info.duration:
                    conv_prog.advance(conv_task, info.duration - reported)
        else:
            result = run_cancellable_subprocess(
                command,
                capture_output=capture,
                cancel_event=cancel_event,
            )
    except DownloadCancelled:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise
    finally:
        if os.path.exists(chapter_file):
            os.remove(chapter_file)
        if cover_tmp_path is not None and os.path.exists(cover_tmp_path):
            os.remove(cover_tmp_path)
    if result.returncode != 0 or not (os.path.exists(staged_path) and os.path.getsize(staged_path) > 0):
        if result.stderr:
            logging.debug(result.stderr.decode("utf8", "replace"))
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise GenericAudiobookDLException(
            f"Failed converting '{os.path.basename(old_path)}' to .{target_format}"
        )
    try:
        verified_info = verify_audio_integrity(
            staged_path,
            expected_duration=info.duration,
        )
    except FailedCombining:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise GenericAudiobookDLException(
            f"Converted file '{os.path.basename(new_path)}' failed media integrity verification"
        ) from None
    except GenericAudiobookDLException:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise
    os.replace(staged_path, new_path)
    store_cached_probe(probe_cache, new_path, verified_info)
    if remove_source and os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(new_path):
        os.remove(old_path)
    return new_path, chapters_embedded


_HLS_SMART_MERGE_EXTS = {"ts", "aac"}


def should_smart_merge_hls(
    filepaths: Sequence[str],
    chapters: Sequence[Chapter],
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> bool:
    """
    Return True when many uncombined HLS segment files should be grouped into chapters.

    Triggers when the book has multiple chapters (`>= 2`) and strictly more files than
    chapters, and the files appear to be fine-grained stream segments (`.ts`/`.aac` or
    at least twice as many files as chapters with readable audio duration).
    """
    if len(chapters) <= 1 or len(filepaths) <= len(chapters):
        return False
    exts = {get_extension(p) for p in filepaths}
    if exts and exts.issubset(_HLS_SMART_MERGE_EXTS):
        return True
    if len(filepaths) >= max(8, 2 * len(chapters)):
        return _get_cached_probe(filepaths[0], probe_cache).duration > 0
    return False


def _split_single_chapter(
    ffmpeg: str,
    master_path: str,
    out_path: str,
    start_sec: float,
    duration_sec: float | None,
    chapter_index: int,
    chapter_title: str,
    fast_codec_args: Sequence[str],
    reencode_codec_args: Sequence[str],
    format_args: Sequence[str],
    accurate: bool,
    capture: bool,
    cancel_event: threading.Event | None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
) -> str:
    """Export one chapter slice from `master_path` to `out_path`."""
    duration_args = ["-t", f"{duration_sec:.3f}"] if duration_sec is not None else []
    if accurate:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            master_path,
            "-ss",
            f"{start_sec:.3f}",
            *duration_args,
            "-vn",
            *reencode_codec_args,
            *format_args,
            out_path,
        ]
    else:
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-i",
            master_path,
            *duration_args,
            "-avoid_negative_ts",
            "make_zero",
            "-vn",
            *fast_codec_args,
            *format_args,
            out_path,
        ]
    res = run_cancellable_subprocess(
        cmd,
        capture_output=capture,
        cancel_event=cancel_event,
    )
    succeeded = (
        res.returncode == 0
        and os.path.exists(out_path)
        and os.path.getsize(out_path) > 0
    )
    if not succeeded and not accurate and "copy" in fast_codec_args:
        if res.stderr:
            logging.debug(
                f"Fast split chapter {chapter_index + 1} failed, retrying with exact seek/re-encode: "
                + res.stderr.decode("utf8", "replace")
            )
        if os.path.exists(out_path):
            os.remove(out_path)
        fallback_cmd = [
            ffmpeg,
            "-y",
            "-i",
            master_path,
            "-ss",
            f"{start_sec:.3f}",
            *duration_args,
            "-vn",
            *reencode_codec_args,
            *format_args,
            out_path,
        ]
        res = run_cancellable_subprocess(
            fallback_cmd,
            capture_output=capture,
            cancel_event=cancel_event,
        )
        succeeded = (
            res.returncode == 0
            and os.path.exists(out_path)
            and os.path.getsize(out_path) > 0
        )
    if not succeeded:
        if res.stderr:
            logging.debug(res.stderr.decode("utf8", "replace"))
        if os.path.exists(out_path):
            os.remove(out_path)
        raise GenericAudiobookDLException(
            f"Failed splitting chapter {chapter_index + 1}: '{chapter_title}'"
        )
    try:
        verify_audio_integrity(out_path, probe_cache=probe_cache)
    except FailedCombining:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise GenericAudiobookDLException(
            f"Split chapter {chapter_index + 1} ('{chapter_title}') failed integrity check"
        ) from None
    except GenericAudiobookDLException:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
    return out_path


def split_audio_by_chapters(
    master_path: str,
    output_dir: str,
    chapters: Sequence[Chapter],
    output_format: str | None = None,
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    accurate: bool = False,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    loudnorm: bool = False,
    mono: bool = False,
) -> list[str]:
    """
    Split `master_path` into individual zero-padded chapter files inside `output_dir`.
    """
    if not chapters:
        return [master_path]
    ffmpeg = _require_executable("ffmpeg")
    os.makedirs(output_dir, exist_ok=True)
    src_ext = get_extension(master_path)
    target_format = (output_format or src_ext).lstrip(".").lower()
    info = _get_cached_probe(master_path, probe_cache)
    effective_accurate = accurate or loudnorm or mono
    fast_codec_args = _build_mux_codec_args(
        src_ext,
        target_format,
        master_path,
        info,
        audio_bitrate,
        loudnorm=loudnorm,
        mono=mono,
    )
    reencode_codec_args = _build_reencode_codec_args(
        target_format,
        master_path,
        info,
        audio_bitrate,
        loudnorm=loudnorm,
        mono=mono,
    )
    format_args = ["-f", "mp4"] if target_format == "m4b" else []
    capture = not logging.is_ffmpeg_output()
    is_windows = platform.system() == "Windows"

    if info.duration > 0:
        total_ms = max(1, round(info.duration * 1000))
        spans = normalize_chapters(chapters, total_ms)
    else:
        ordered = sorted(chapters, key=lambda ch: int(ch.start))
        spans = []
        total_ms = 0
        for i, ch in enumerate(ordered):
            start_ms = max(0, int(ch.start))
            end_ms = max(start_ms + 50, int(ordered[i + 1].start)) if i + 1 < len(ordered) else start_ms
            spans.append((start_ms, end_ms, ch.title))

    if not spans:
        return [master_path]

    width = max(2, len(str(len(spans))))
    tasks = []
    for idx, (start_ms, end_ms, raw_title) in enumerate(spans):
        start_sec = start_ms / 1000.0
        duration_sec: float | None = None
        if idx + 1 < len(spans) or (total_ms > 0 and end_ms < total_ms):
            duration_sec = max(0.05, (end_ms - start_ms) / 1000.0)
        safe_title = _truncate_utf8(_fix_output(raw_title or "") or f"Chapter {idx + 1}", 160)
        if is_windows:
            safe_title = _sanitize_windows_reserved_name(safe_title.rstrip(". ") or f"Chapter {idx + 1}")
        out_name = f"{idx + 1:0{width}d} - {safe_title}.{target_format}"
        out_path = os.path.join(output_dir, out_name)
        tasks.append((idx, start_sec, duration_sec, out_path, raw_title or f"Chapter {idx + 1}"))

    batch_abort = threading.Event()
    state_lock = threading.Lock()
    created: list[str] = []
    first_error: BaseException | None = None

    class _CancelMonitor:
        def is_set(self) -> bool:
            return batch_abort.is_set() or (cancel_event is not None and cancel_event.is_set())

    effective_cancel: Any = _CancelMonitor()
    logging.book_update(f"Splitting into {len(tasks)} chapters")
    try:
        with logging.progress([]) as split_prog:
            task_bar = split_prog.add_task(f"Splitting ({len(tasks)} chapters)", total=len(tasks))

            def _run_task(item: tuple[int, float, float | None, str, str]) -> str:
                nonlocal first_error
                idx, start_sec, duration_sec, out_path, title = item
                try:
                    exported = _split_single_chapter(
                        ffmpeg=ffmpeg,
                        master_path=master_path,
                        out_path=out_path,
                        start_sec=start_sec,
                        duration_sec=duration_sec,
                        chapter_index=idx,
                        chapter_title=title,
                        fast_codec_args=fast_codec_args,
                        reencode_codec_args=reencode_codec_args,
                        format_args=format_args,
                        accurate=effective_accurate,
                        capture=capture,
                        cancel_event=effective_cancel,
                        probe_cache=probe_cache,
                    )
                except BaseException as exc:
                    with state_lock:
                        if first_error is None:
                            first_error = exc
                    batch_abort.set()
                    raise
                with state_lock:
                    created.append(exported)
                    split_prog.advance(task_bar, 1.0)
                return exported

            ordered = run_cancellable_pool(
                _run_task,
                tasks,
                max_workers=COMBINE_REMUX_THREADS,
                cancel_event=effective_cancel,
            )
            return list(ordered)
    except Exception:
        for path in created:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if first_error is not None and not (cancel_event is not None and cancel_event.is_set()):
            raise first_error from None
        raise


def merge_hls_segments_into_chapters(
    filepaths: Sequence[str],
    output_dir: str,
    chapters: Sequence[Chapter],
    output_format: str | None = None,
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    loudnorm: bool = False,
    mono: bool = False,
) -> list[str]:
    """
    Combine fine-grained HLS segment files seamlessly and split cleanly by `chapters`.
    """
    if not filepaths:
        return []
    cache: dict[str, AudioProbeInfo] = probe_cache if probe_cache is not None else {}
    target_format = (output_format or get_extension(filepaths[0])).lstrip(".").lower()
    master_ext = "m4a" if get_extension(filepaths[0]) in ("ts", "aac") and target_format in MP4_CONTAINERS else target_format
    if master_ext == "ts":
        master_ext = "m4a"
    with tempfile.TemporaryDirectory() as tmp_dir:
        combine_work = os.path.join(tmp_dir, "combine_work")
        os.makedirs(combine_work, exist_ok=True)
        master_path = os.path.join(tmp_dir, f"master.{master_ext}")
        combine_audiofiles(
            filepaths,
            combine_work,
            master_path,
            cancel_event=cancel_event,
            audio_bitrate=audio_bitrate,
            probe_cache=cache,
            loudnorm=loudnorm,
            mono=mono,
        )
        split_paths = split_audio_by_chapters(
            master_path,
            output_dir,
            chapters,
            output_format=target_format,
            cancel_event=cancel_event,
            audio_bitrate=audio_bitrate,
            probe_cache=cache,
        )
    for part in filepaths:
        if part not in split_paths and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    return split_paths


def convert_output(
    filenames: Sequence[str],
    output_format: str,
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    loudnorm: bool = False,
    mono: bool = False,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
):
    """Converts a list of audio files into another format and return new
    files"""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    if not filenames:
        return []
    _require_executable("ffmpeg")
    target_format = output_format.lstrip(".").lower()
    if len(filenames) == 1:
        new_path, _ = convert_single_file(
            filenames[0],
            target_format,
            cancel_event=cancel_event,
            audio_bitrate=explicit_bitrate,
            remove_source=True,
            probe_cache=probe_cache,
            loudnorm=loudnorm,
            mono=mono,
        )
        return [new_path]

    batch_abort = threading.Event()
    error_lock = threading.Lock()
    first_error: BaseException | None = None

    class _CancelMonitor:
        def is_set(self) -> bool:
            return batch_abort.is_set() or (cancel_event is not None and cancel_event.is_set())

    effective_cancel: Any = _CancelMonitor()

    def _convert_one(old_path: str) -> tuple[str, str, bool]:
        nonlocal first_error
        path_without_ext, old_ext_raw = os.path.splitext(old_path)
        old_ext = old_ext_raw.lstrip(".").lower()
        try:
            new_path, _ = convert_single_file(
                old_path,
                target_format,
                cancel_event=effective_cancel,
                audio_bitrate=explicit_bitrate,
                remove_source=False,
                probe_cache=probe_cache,
                loudnorm=loudnorm,
                mono=mono,
            )
        except BaseException as exc:
            with error_lock:
                if first_error is None:
                    first_error = exc
            batch_abort.set()
            raise
        return old_path, new_path, (target_format != old_ext)

    new_paths: list[str] = []
    converted_pairs: list[tuple[str, str]] = []
    try:
        results = run_cancellable_pool(
            _convert_one,
            list(filenames),
            max_workers=COMBINE_REMUX_THREADS,
            cancel_event=effective_cancel,
        )
        for old_path, new_path, converted in results:
            new_paths.append(new_path)
            if converted:
                converted_pairs.append((old_path, new_path))
    except BaseException:
        # Clean up any newly converted files from a partial batch failure; keep all sources.
        for old_path in filenames:
            path_without_ext, old_ext_raw = os.path.splitext(old_path)
            if old_ext_raw.lstrip(".").lower() != target_format:
                candidate = f"{path_without_ext}.{target_format}"
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass
        if first_error is not None and not (cancel_event is not None and cancel_event.is_set()):
            raise first_error from None
        raise

    for old_path, _new_path in converted_pairs:
        if os.path.exists(old_path):
            os.remove(old_path)
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
    metadata: AudiobookMetadata | None,
    remove_chars: str,
) -> dict[str, Any]:
    """Return metadata mapping with all string values sanitized for filesystem paths."""
    props = metadata.all_properties_dict() if metadata is not None else {}
    raw: dict[str, Any] = {**LOCATION_DEFAULTS, **props}
    if metadata is not None:
        if metadata.release_date is not None:
            raw["year"] = str(metadata.release_date.year)
        if (
            metadata.series_order is not None
            and not isinstance(metadata.series_order, bool)
        ):
            raw["series_order_padded"] = f"{int(metadata.series_order):02d}"
        if not raw.get("source") and metadata.scrape_url:
            host = urlparse(metadata.scrape_url).netloc.lower().removeprefix("www.")
            if host:
                raw["source"] = host.split(".")[0]
        if metadata.language is not None:
            raw["language"] = (
                getattr(metadata.language, "alpha_3", None)
                or getattr(metadata.language, "alpha_2", None)
                or str(metadata.language)
            )
    sanitized: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            cleaned = _fix_output(value)
            if remove_chars:
                cleaned = _remove_chars(cleaned, remove_chars)
            sanitized[key] = cleaned
        else:
            sanitized[key] = value
    return _SafeFormatDict(sanitized)


def _expand_conditional_blocks(template: str, metadata_dict: Mapping[str, Any]) -> str:
    """Resolve balanced `{key?true_branch}` / `{key?true_branch|false_branch}` expressions."""
    out: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        # Scan matching '}' tracking nested braces
        depth = 0
        j = i
        q_idx = -1
        pipe_idx = -1
        while j < n:
            c = template[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1:
                if c == "?" and q_idx == -1:
                    q_idx = j
                elif c == "|" and q_idx != -1 and pipe_idx == -1:
                    pipe_idx = j
            j += 1
        if depth != 0 or q_idx == -1:
            # Not a well-formed conditional block; emit literally up to matching close or one char
            if depth == 0 and j < n:
                out.append(template[i : j + 1])
                i = j + 1
            else:
                out.append(ch)
                i += 1
            continue
        key = template[i + 1 : q_idx].strip()
        if pipe_idx != -1:
            true_branch = template[q_idx + 1 : pipe_idx]
            false_branch = template[pipe_idx + 1 : j]
        else:
            true_branch = template[q_idx + 1 : j]
            false_branch = ""
        val = metadata_dict.get(key, "")
        is_truthy = val not in ("", None, 0, False) if key != "series_order" else (val not in ("", None))
        chosen = true_branch if is_truthy else false_branch
        out.append(_expand_conditional_blocks(chosen, metadata_dict))
        i = j + 1
    return "".join(out)


def _join_segments(segments: Sequence[str], seps: Sequence[str]) -> str:
    parts = [segments[0]]
    for sep, seg in zip(seps, segments[1:], strict=False):
        parts.append(sep)
        parts.append(seg)
    return "".join(parts)


def gen_output_location(
    template: str,
    metadata: AudiobookMetadata | None,
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
    resolved_template = _expand_conditional_blocks(template, metadata_dict)
    is_windows = platform.system() == "Windows"
    component_cap = min(get_max_name_length(), 255)

    # Split template on path separators so we sanitize/bound each path segment
    # independently while preserving intentional directory separators in `template`.
    tokens = re.split(r"([/\\])", resolved_template)
    raw_segments = tokens[0::2]
    seps = tokens[1::2]
    segments: list[str] = []
    kept_seps: list[str] = []
    dynamic_indices: list[int] = []
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

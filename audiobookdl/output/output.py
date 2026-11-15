from audiobookdl import logging, AudiobookMetadata, Chapter
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
    MissingDependency,
)
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.output.metadata._chapters import normalize_chapters
from audiobookdl.utils import (
    AudioProbeInfo,
    MAX_AUTO_M4B_BITRATE_KBPS,
    MIN_AUTO_M4B_BITRATE_KBPS,
    DEFAULT_M4B_BITRATE,
    MP4_CONTAINERS,
    ffprobe_audio_bitrate,
    ffprobe_duration,
    find_executable,
    normalize_audio_bitrate,
    probe_audio_info,
    resolve_audio_bitrate,
    subprocess_kwargs,
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
import subprocess
import tempfile
import threading
from typing import Any, Dict, List, Optional, Sequence, Mapping
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
    'publisher': '',
    'release_date': '',
    'year': '',
    'source': '',
}

OUTPUT_TEMPLATE_PRESETS: Dict[str, str] = {
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


def _ffmpeg_audio_codec(path: str) -> str:
    """Returns the codec name of the first audio stream in `path` (empty on failure)"""
    return _probe_audio_info(path).codec_name


def _ffprobe_duration(path: str) -> float:
    """Returns the duration of `path` in seconds (0.0 on failure)"""
    return _probe_audio_info(path).duration


def _ffprobe_audio_bitrate(path: str) -> Optional[int]:
    """Return the bitrate of `path` in bits/sec via `ffprobe`, or `None` if unavailable."""
    return _probe_audio_info(path).bitrate


def _resolve_aac_bitrate_from_info(
    source_path: str,
    info: AudioProbeInfo,
    audio_bitrate: Optional[str] = None,
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


def _all_inputs_mpegts(filepaths: Sequence[str]) -> bool:
    """Return True when every input segment is already a valid MPEG-TS file."""
    if not filepaths or not all(_is_mpegts_file(path) for path in filepaths):
        return False
    fmt = _probe_audio_info(filepaths[0]).format_name
    if fmt and "mpegts" not in fmt.split(","):
        return False
    return True


def _write_ffmetadata_chapters(
    chapter_path: str,
    chapters: Sequence[Chapter],
    duration_sec: float,
) -> bool:
    """Write FFMETADATA1 chapter entries bounded to `duration_sec`; return True if any written."""
    if not chapters or duration_sec <= 0:
        return False
    bounded = normalize_chapters(chapters, int(duration_sec * 1000))
    if not bounded:
        return False
    content = ";FFMETADATA1\n" + "".join(
        ffmpeg_metadata.create_chapter_text(title=title, start=start, end=end)
        for start, end, title in bounded
    )
    with open(chapter_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return True


def _build_mux_codec_args(
    source_ext: str,
    target_ext: str,
    source_path: str,
    info: AudioProbeInfo,
    audio_bitrate: Optional[str],
) -> List[str]:
    """Return FFmpeg codec/container flags for muxing or transcoding to `target_ext`."""
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
        args += ["-movflags", "+faststart"]
        return args
    if target_ext == source_ext or target_ext in ("mka", "mkv") or (target_ext == "mp3" and codec == "mp3"):
        return ["-c:a", "copy"]
    if explicit_bitrate is not None and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS:
        return ["-b:a", explicit_bitrate]
    return []


def combine_audiofiles(
    filepaths: Sequence[str],
    tmp_dir: str,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
    chapters: Optional[Sequence[Chapter]] = None,
    audio_bitrate: Optional[str] = None,
) -> bool:
    """
    Combines the given audiofiles into `output_path` (optionally transcoding and
    embedding `chapters` in the final FFmpeg pass).

    :returns: `True` if `chapters` were embedded during the combine pass.
    """
    if not filepaths:
        raise FailedCombining
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    normalize_audio_bitrate(audio_bitrate)
    ffmpeg = _require_executable("ffmpeg")
    output_extension = get_extension(output_path)
    source_extension = get_extension(filepaths[0])
    work_dir = f"{os.path.normpath(tmp_dir)}.work"
    ts_dir = os.path.join(work_dir, "ts_parts")
    combined_ts = os.path.join(work_dir, "combined.ts")
    chapter_file = os.path.join(work_dir, "ffmetadata.txt")
    os.makedirs(work_dir, exist_ok=True)
    chapters_embedded = False
    try:
        if _all_inputs_mpegts(filepaths):
            ts_paths = list(filepaths)
        else:
            os.makedirs(ts_dir, exist_ok=True)
            # Remux every non-TS segment to MPEG-TS in parallel (lossless copy)
            padding = len(str(len(filepaths)))
            total_parts = len(filepaths)
            completed_parts = 0
            part_lock = threading.Lock()

            with logging.progress([]) as remux_prog:
                remux_task = remux_prog.add_task(
                    f"Remuxing parts (0/{total_parts})",
                    total=float(total_parts),
                )

                def remux(item):
                    nonlocal completed_parts
                    index, source = item
                    ts_path = os.path.join(ts_dir, f"{str(index).zfill(padding)}.ts")
                    ok = _remux_to_mpegts(source, ts_path, cancel_event=cancel_event)
                    with part_lock:
                        completed_parts += 1
                        remux_prog.advance(remux_task, 1.0)
                        logging.book_update(f"Remuxing parts ({completed_parts}/{total_parts})")
                    return ts_path, ok

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

        sample_info = _probe_audio_info(ts_paths[0])
        combined_info = _probe_audio_info(combined_ts)
        expected = combined_info.duration

        command = [ffmpeg, "-y", "-i", combined_ts]
        # Binary-concatenated MPEG-TS resets PTS across segment junctions, causing ffprobe to
        # under-report `combined_ts` duration when len(ts_paths) > 1. Only embed chapters
        # inline when there is a single contiguous TS; multi-part outputs fall through to
        # lossless `add_chapters` where the muxed container's true duration is known.
        if len(ts_paths) == 1 and chapters and _write_ffmetadata_chapters(chapter_file, chapters, expected):
            logging.book_update("Adding chapters")
            command += ["-i", chapter_file, "-map_chapters", "1", "-map", "0:a", "-metadata:s:a:0", "title="]
            chapters_embedded = True
        else:
            command += ["-vn"]
        command += _build_mux_codec_args(
            source_extension,
            output_extension,
            ts_paths[0],
            sample_info,
            audio_bitrate,
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
        if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
            if result.stderr:
                logging.debug(result.stderr.decode("utf8", "replace"))
            raise FailedCombining
        # Guard against silent truncation: the combined file must be about as long
        # as the concatenated source. A large shortfall means ffmpeg dropped audio.
        actual = _probe_audio_info(output_path).duration
        if expected > 0 and actual < expected * 0.98:
            logging.debug(
                f"Combined output is shorter than expected "
                f"({actual:.0f}s vs {expected:.0f}s); combine truncated the audio"
            )
            raise FailedCombining
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)
        return chapters_embedded
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


def convert_single_file(
    old_path: str,
    output_format: str,
    cancel_event: Optional[threading.Event] = None,
    audio_bitrate: Optional[str] = None,
    chapters: Optional[Sequence[Chapter]] = None,
    remove_source: bool = True,
) -> tuple[str, bool]:
    """
    Convert a single file to `output_format`, optionally embedding `chapters` in
    the same FFmpeg pass. Returns `(new_path, chapters_embedded)`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_format = output_format.lstrip(".").lower()
    path_without_ext, old_ext_raw = os.path.splitext(old_path)
    old_ext = old_ext_raw.lstrip(".").lower()
    new_path = f"{path_without_ext}.{target_format}"
    if target_format == old_ext:
        return new_path, False

    ffmpeg = _require_executable("ffmpeg")
    capture = not logging.is_ffmpeg_output()
    info = _probe_audio_info(old_path)
    chapter_file = f"{new_path}.chapters.tmp.txt"
    chapters_embedded = False

    command = [ffmpeg, "-y", "-i", old_path]
    if chapters and _write_ffmetadata_chapters(chapter_file, chapters, info.duration):
        logging.book_update("Adding chapters")
        command += ["-i", chapter_file, "-map_chapters", "1", "-map", "0:a", "-metadata:s:a:0", "title="]
        chapters_embedded = True
    else:
        command += ["-vn"]
    command += _build_mux_codec_args(old_ext, target_format, old_path, info, explicit_bitrate)
    if capture and info.duration > 0:
        command += ["-progress", "pipe:1", "-nostats"]
    command.append(new_path)

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
        if os.path.exists(new_path):
            os.remove(new_path)
        raise
    finally:
        if os.path.exists(chapter_file):
            os.remove(chapter_file)
    if result.returncode != 0 or not (os.path.exists(new_path) and os.path.getsize(new_path) > 0):
        if result.stderr:
            logging.debug(result.stderr.decode("utf8", "replace"))
        if os.path.exists(new_path):
            os.remove(new_path)
        raise GenericAudiobookDLException(
            f"Failed converting '{os.path.basename(old_path)}' to .{target_format}"
        )
    if remove_source and os.path.exists(old_path):
        os.remove(old_path)
    return new_path, chapters_embedded


_HLS_SMART_MERGE_EXTS = {"ts", "aac"}


def should_smart_merge_hls(filepaths: Sequence[str], chapters: Sequence[Chapter]) -> bool:
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
        return _probe_audio_info(filepaths[0]).duration > 0
    return False


def split_audio_by_chapters(
    master_path: str,
    output_dir: str,
    chapters: Sequence[Chapter],
    output_format: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    audio_bitrate: Optional[str] = None,
) -> List[str]:
    """
    Split `master_path` into individual zero-padded chapter files inside `output_dir`.
    """
    if not chapters:
        return [master_path]
    ffmpeg = _require_executable("ffmpeg")
    os.makedirs(output_dir, exist_ok=True)
    src_ext = get_extension(master_path)
    target_format = (output_format or src_ext).lstrip(".").lower()
    info = _probe_audio_info(master_path)
    codec_args = _build_mux_codec_args(
        src_ext,
        target_format,
        master_path,
        info,
        audio_bitrate,
    )
    format_args = ["-f", "mp4"] if target_format == "m4b" else []
    capture = not logging.is_ffmpeg_output()
    width = max(2, len(str(len(chapters))))
    created: List[str] = []
    logging.book_update(f"Splitting into {len(chapters)} chapters")
    try:
        with logging.progress([]) as split_prog:
            task = split_prog.add_task(f"Splitting ({len(chapters)} chapters)", total=len(chapters))
            for idx, chapter in enumerate(chapters):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled
                start_sec = max(0.0, chapter.start / 1000.0)
                duration_sec: Optional[float] = None
                if idx + 1 < len(chapters):
                    next_start = max(0.0, chapters[idx + 1].start / 1000.0)
                    duration_sec = max(0.05, next_start - start_sec)
                safe_title = _truncate_utf8(_fix_output(chapter.title or "") or f"Chapter {idx + 1}", 160)
                if platform.system() == "Windows":
                    safe_title = _sanitize_windows_reserved_name(safe_title.rstrip(". ") or f"Chapter {idx + 1}")
                out_name = f"{idx + 1:0{width}d} - {safe_title}.{target_format}"
                out_path = os.path.join(output_dir, out_name)
                cmd = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{start_sec:.3f}",
                    "-i",
                    master_path,
                    *(["-t", f"{duration_sec:.3f}"] if duration_sec is not None else []),
                    "-vn",
                    *codec_args,
                    *format_args,
                    out_path,
                ]
                res = run_cancellable_subprocess(
                    cmd,
                    capture_output=capture,
                    cancel_event=cancel_event,
                )
                if res.returncode != 0 or not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                    if res.stderr:
                        logging.debug(res.stderr.decode("utf8", "replace"))
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    raise GenericAudiobookDLException(
                        f"Failed splitting chapter {idx + 1}: '{chapter.title}'"
                    )
                created.append(out_path)
                split_prog.advance(task, 1.0)
    except Exception:
        for path in created:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        raise
    return created


def merge_hls_segments_into_chapters(
    filepaths: Sequence[str],
    output_dir: str,
    chapters: Sequence[Chapter],
    output_format: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    audio_bitrate: Optional[str] = None,
) -> List[str]:
    """
    Combine fine-grained HLS segment files seamlessly and split cleanly by `chapters`.
    """
    if not filepaths:
        return []
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
        )
        split_paths = split_audio_by_chapters(
            master_path,
            output_dir,
            chapters,
            output_format=target_format,
            cancel_event=cancel_event,
            audio_bitrate=audio_bitrate,
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
    cancel_event: Optional[threading.Event] = None,
    audio_bitrate: Optional[str] = None,
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
        )
        return [new_path]

    batch_abort = threading.Event()
    error_lock = threading.Lock()
    first_error: Optional[BaseException] = None

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
            )
        except BaseException as exc:
            with error_lock:
                if first_error is None:
                    first_error = exc
            batch_abort.set()
            raise
        return old_path, new_path, (target_format != old_ext)

    new_paths: List[str] = []
    converted_pairs: List[tuple[str, str]] = []
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
    metadata: Optional[AudiobookMetadata],
    remove_chars: str,
) -> Dict[str, Any]:
    """Return metadata mapping with all string values sanitized for filesystem paths."""
    props = metadata.all_properties_dict() if metadata is not None else {}
    raw: Dict[str, Any] = {**LOCATION_DEFAULTS, **props}
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


def _expand_conditional_blocks(template: str, metadata_dict: Mapping[str, Any]) -> str:
    """Resolve balanced `{key?true_branch}` / `{key?true_branch|false_branch}` expressions."""
    out: List[str] = []
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
    resolved_template = _expand_conditional_blocks(template, metadata_dict)
    is_windows = platform.system() == "Windows"
    component_cap = min(get_max_name_length(), 255)

    # Split template on path separators so we sanitize/bound each path segment
    # independently while preserving intentional directory separators in `template`.
    tokens = re.split(r"([/\\])", resolved_template)
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

from audiobookdl import logging, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
    MissingDependency,
)
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.output.metadata._chapters import (
    normalize_chapters,
    resolve_split_chapter_entries,
)
from audiobookdl.output.metadata._cover import normalize_cover_image
from audiobookdl.utils import (
    AudioProbeInfo,
    MAX_AUTO_M4B_BITRATE_KBPS,
    MIN_AUTO_M4B_BITRATE_KBPS,
    DEFAULT_M4B_BITRATE,
    MP4_CONTAINERS,
    find_executable,
    normalize_audio_bitrate,
    normalize_sample_rate,
    probe_audio_info,
)
from audiobookdl.utils.audiobook import is_ebook_extension
from audiobookdl.utils.concurrency import (
    ffmpeg_progress_callback,
    run_cancellable_pool,
    run_cancellable_subprocess,
)

import hashlib
import os
import re
import shutil
import platform
import tempfile
import threading
import uuid
import zipfile
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


def _require_executable(name: str) -> str:
    """Resolve an external executable path or raise `MissingDependency`."""
    path = find_executable(name)
    if path is None:
        raise MissingDependency(dependency=name)
    return path


def _log_ffmpeg_stderr(result: Any, prefix: str = "") -> None:
    """Emit captured FFmpeg stderr via `logging.debug` when debug logging is enabled."""
    if not logging.is_debug_mode():
        return
    raw_stderr = getattr(result, "stderr", None)
    if not raw_stderr:
        return
    text = raw_stderr.decode("utf-8", "replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return
    logging.debug(f"{prefix}{cleaned}" if prefix else cleaned)


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


_TEXT_ERROR_PREFIXES: tuple[bytes, ...] = (
    b"<!doctype",
    b"<html",
    b"<head",
    b"<error",
    b"<?xml",
    b"<rss",
    b"<soap",
)
_ISO_BMFF_BOX_TYPES: frozenset[bytes] = frozenset(
    {b"ftyp", b"styp", b"moov", b"moof", b"sidx", b"free", b"skip", b"mdat", b"wide"}
)


def _looks_like_text_error_payload(head: bytes) -> bool:
    """Return True when leading bytes resemble an HTML, XML, or JSON error payload."""
    if not head:
        return False
    cleaned = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not cleaned:
        return True
    lower = cleaned[:256].lower()
    if lower.startswith(_TEXT_ERROR_PREFIXES):
        return True
    if lower.startswith(b"{") and b":" in lower[:256]:
        # Treat plain printable JSON objects as text payloads rather than binary media.
        sample = cleaned[:256]
        if all(b in (9, 10, 13) or 32 <= b < 127 or b >= 128 for b in sample):
            return True
    return False


def _has_valid_media_magic(path: str, ext: str) -> bool:
    """Lightweight container signature check used when `ffprobe` is unavailable."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if _looks_like_text_error_payload(head):
        return False
    normalized_ext = ext.lstrip(".").lower()
    if normalized_ext == "mp3":
        if head.startswith(b"ID3"):
            return True
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return True
        limit = min(len(head) - 1, 2048)
        for i in range(limit):
            b0, b1 = head[i], head[i + 1]
            if b0 == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 & 0x18) != 0x08 and (b1 & 0x06) != 0x00:
                return True
        return False
    if normalized_ext in ("m4a", "m4b", "mp4", "m4v", "m4s", "mov"):
        return len(head) >= 8 and head[4:8] in _ISO_BMFF_BOX_TYPES
    if normalized_ext in ("ogg", "oga", "opus"):
        return head.startswith(b"OggS")
    if normalized_ext == "flac":
        return head.startswith((b"fLaC", b"ID3"))
    if normalized_ext == "wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if normalized_ext == "ts":
        if len(head) > 188:
            return head[0] == 0x47 and head[188] == 0x47
        return len(head) >= 1 and head[0] == 0x47
    if normalized_ext == "aac":
        if head.startswith((b"ADIF", b"ID3")):
            return True
        return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0
    if normalized_ext in ("mkv", "webm"):
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if normalized_ext in ("aiff", "aif"):
        return len(head) >= 12 and head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC")
    if normalized_ext == "wma":
        return head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")
    return True


def verify_audio_integrity(
    path: str,
    *,
    expected_duration: float = 0.0,
    min_duration_ratio: float = 0.98,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    require_ffprobe: bool = False,
    allow_video: bool = False,
) -> AudioProbeInfo:
    """
    Validate that `path` exists, is non-empty, has a valid audio (or video when
    `allow_video=True`) stream and positive duration, and was not materially
    truncated relative to `expected_duration`.

    Falls back to container magic-byte and soft-error signature validation when
    `ffprobe` is unavailable. Raises `FailedCombining` on verification failure.
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

    detected_codec = info.codec_name or (info.video_codec_name if allow_video else "")
    if not detected_codec or info.duration <= 0.0:
        if ffprobe_available or info.duration > 0.0 or detected_codec:
            logging.debug(
                f"Media integrity check failed for '{os.path.basename(path)}': "
                f"codec={detected_codec!r}, duration={info.duration:.2f}s"
            )
            raise FailedCombining(
                message=f"Invalid or corrupt media output: {os.path.basename(path)}"
            )
        if not _has_valid_media_magic(path, get_extension(path)):
            raise FailedCombining(
                message=f"Invalid or corrupt media file signature: {os.path.basename(path)}"
            )

    return info


def verify_ebook_integrity(path: str, *, ext: str | None = None) -> None:
    """
    Validate that an ebook file exists, is non-empty, is not an HTML/XML/JSON soft error,
    and matches the structural signature of its container format (`.epub`, `.cbz`, `.pdf`,
    `.mobi`, `.azw3`, `.cbr`). Raises `FailedCombining` on verification failure.
    """
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        raise FailedCombining(message=f"Ebook file is missing or empty: {path}")
    file_size = os.path.getsize(path)
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            if file_size > 4096:
                f.seek(max(0, file_size - 4096))
                tail = f.read(4096)
            else:
                tail = head
    except OSError as exc:
        raise FailedCombining(message=f"Failed reading ebook file '{os.path.basename(path)}': {exc}") from exc

    if _looks_like_text_error_payload(head):
        raise FailedCombining(
            message=f"Invalid ebook payload (looks like text/HTML/XML error): {os.path.basename(path)}"
        )

    resolved_ext = (ext or get_extension(path)).lstrip(".").lower()
    if resolved_ext in ("epub", "cbz"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                if not names:
                    raise FailedCombining(
                        message=f"Ebook archive is empty: {os.path.basename(path)}"
                    )
                bad_member = zf.testzip()
                if bad_member is not None:
                    raise FailedCombining(
                        message=f"Corrupt archive entry '{bad_member}' in {os.path.basename(path)}"
                    )
                if resolved_ext == "epub" and not (
                    "META-INF/container.xml" in names or "mimetype" in names
                ):
                    raise FailedCombining(
                        message=f"Invalid EPUB archive structure: {os.path.basename(path)}"
                    )
        except (zipfile.BadZipFile, OSError) as exc:
            raise FailedCombining(
                message=f"Invalid or corrupt {resolved_ext.upper()} archive: {os.path.basename(path)}"
            ) from exc
    elif resolved_ext == "pdf":
        if b"%PDF-" not in head[:1024] or b"%%EOF" not in tail:
            raise FailedCombining(
                message=f"Invalid or truncated PDF document: {os.path.basename(path)}"
            )
    elif resolved_ext in ("mobi", "azw3"):
        if not (len(head) >= 68 and (head[60:68] == b"BOOKMOBI" or head.startswith(b"TPZ"))):
            raise FailedCombining(
                message=f"Invalid or corrupt {resolved_ext.upper()} ebook header: {os.path.basename(path)}"
            )
    elif resolved_ext == "cbr":
        if not head.startswith(b"Rar!\x1a\x07"):
            raise FailedCombining(
                message=f"Invalid or corrupt CBR archive: {os.path.basename(path)}"
            )


def verify_file_checksums(path: str, expected_checksums: Mapping[str, str]) -> None:
    """
    Verify `path` against all `(algorithm, expected_hex)` entries in `expected_checksums`.

    Raises `FailedCombining` on digest mismatch.
    """
    if not expected_checksums:
        return
    if not os.path.isfile(path):
        raise FailedCombining(message=f"File missing for checksum verification: {path}")
    hashers = {algo: hashlib.new(algo) for algo in expected_checksums}
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            for hasher in hashers.values():
                hasher.update(chunk)
    for algo, expected_hex in expected_checksums.items():
        actual_hex = hashers[algo].hexdigest().lower()
        if actual_hex != expected_hex.lower():
            raise FailedCombining(
                message=(
                    f"{algo.upper()} checksum mismatch for '{os.path.basename(path)}': "
                    f"expected {expected_hex.lower()}, got {actual_hex}"
                )
            )


def verify_downloaded_file_integrity(
    path: str,
    *,
    ext: str,
    allow_video: bool = False,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    require_ffprobe: bool = False,
    expected_checksums: Mapping[str, str] | None = None,
) -> AudioProbeInfo | None:
    """
    Validate a downloaded audio, video, or ebook file before committing it.

    Verifies cryptographic `expected_checksums` (when supplied), ebook container
    integrity for ebook extensions, and `verify_audio_integrity` for audio/video media.
    """
    if expected_checksums:
        verify_file_checksums(path, expected_checksums)
    if is_ebook_extension(ext):
        verify_ebook_integrity(path, ext=ext)
        return None
    return verify_audio_integrity(
        path,
        probe_cache=probe_cache,
        require_ffprobe=require_ffprobe,
        allow_video=allow_video,
    )


def _resolve_aac_bitrate_from_info(
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


def _run_mpegts_part(
    source: str,
    ts_path: str,
    codec_args: Sequence[str],
    cancel_event: threading.Event | None = None,
    *,
    preserve_video: bool = False,
) -> bool:
    """Run an FFmpeg step producing a single MPEG-TS segment at `ts_path`."""
    ffmpeg = _require_executable("ffmpeg")
    stream_args = (
        ["-map", "0:v:0?", "-map", "0:a?", "-c:v", "copy"]
        if preserve_video
        else ["-vn"]
    )
    try:
        result = run_cancellable_subprocess(
            [ffmpeg, "-y", "-i", source, *stream_args, *codec_args, "-f", "mpegts", ts_path],
            cancel_event=cancel_event,
        )
    except DownloadCancelled:
        if os.path.exists(ts_path):
            os.remove(ts_path)
        raise
    _log_ffmpeg_stderr(result)
    if os.path.exists(ts_path) and os.path.getsize(ts_path) > 0:
        return True
    return False


def _remux_to_mpegts(
    source: str,
    ts_path: str,
    cancel_event: threading.Event | None = None,
    *,
    preserve_video: bool = False,
) -> bool:
    """
    Remux a single media file into an MPEG-TS container.

    Concatenating raw segments directly (e.g. ADTS-AAC) desyncs the decoder at
    segment boundaries and silently drops most of the audio. Remuxing each
    segment to MPEG-TS first gives every part clean framing and timestamps, so
    the following concatenation is lossless.

    :returns: `True` if a non-empty file was produced
    """
    return _run_mpegts_part(
        source,
        ts_path,
        ("-c:a", "copy"),
        cancel_event=cancel_event,
        preserve_video=preserve_video,
    )


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
    max_workers: int | None = None,
) -> list[AudioProbeInfo]:
    """Populate `cache` for any uncached `paths` in parallel and return infos in order."""
    with _PROBE_CACHE_LOCK:
        uncached = [p for p in dict.fromkeys(paths) if p not in cache]
    if uncached:
        probed = run_cancellable_pool(
            _probe_audio_info,
            uncached,
            max_workers=resolve_ffmpeg_pool_workers(max_workers, cpu_bound=False),
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
    *,
    preserve_video: bool = False,
    has_audio: bool = True,
) -> None:
    """Append `-i` and stream mapping flags for inline chapters, metadata tags, and cover art."""
    next_input = 1
    meta_idx: int | None = None
    cover_idx: int | None = None
    if chapters_written or meta_written:
        command += ["-i", meta_file]
        meta_idx = next_input
        next_input += 1
    if cover_path is not None and not preserve_video:
        command += ["-i", cover_path]
        cover_idx = next_input
        next_input += 1
    if preserve_video:
        command += ["-map", "0:v:0?", "-map", "0:a?"]
    elif meta_idx is not None or cover_idx is not None:
        command += ["-map", "0:a"]
    if meta_idx is not None:
        if chapters_written:
            command += ["-map_chapters", str(meta_idx)]
        if meta_written:
            command += ["-map_metadata", str(meta_idx)]
        if has_audio:
            command += ["-metadata:s:a:0", "title="]
    if cover_idx is not None:
        command += ["-map", f"{cover_idx}:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    elif not preserve_video:
        command += ["-vn"]


def _build_reencode_codec_args(
    target_ext: str,
    info: AudioProbeInfo,
    audio_bitrate: str | None,
    faststart: bool = True,
    *,
    loudnorm: bool = False,
    mono: bool = False,
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
) -> list[str]:
    """Return FFmpeg codec/container flags forcing re-encode into `target_ext`."""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    filter_args: list[str] = []
    if loudnorm:
        filter_args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    if target_sample_rate is not None:
        filter_args += ["-ar", str(target_sample_rate)]
    if mono:
        filter_args += ["-ac", "1"]
    video_args = ["-c:v", "copy"] if preserve_video else []
    video_only = preserve_video and not info.codec_name

    if target_ext in MP4_CONTAINERS:
        if video_only:
            args = [*video_args]
        else:
            target_bitrate = _resolve_aac_bitrate_from_info(info, explicit_bitrate)
            args = [*video_args, *filter_args, "-c:a", "aac", "-b:a", target_bitrate]
        if faststart:
            args += ["-movflags", "+faststart"]
        return args
    if target_ext == "mp3":
        if video_only:
            return [*video_args]
        bitrate = explicit_bitrate or (
            f"{max(64, min(round(info.bitrate / 1000), 320))}k"
            if info.bitrate and info.bitrate > 0
            else "192k"
        )
        return [*video_args, *filter_args, "-c:a", "libmp3lame", "-b:a", bitrate]
    if video_only:
        return [*video_args]
    if target_ext == "opus":
        args = [*video_args, *filter_args, "-c:a", "libopus"]
        if explicit_bitrate is not None:
            args += ["-b:a", explicit_bitrate]
        return args
    if target_ext == "ogg":
        args = [*video_args, *filter_args, "-c:a", "libvorbis"]
        if explicit_bitrate is not None:
            args += ["-b:a", explicit_bitrate]
        return args
    if target_ext == "flac":
        return [*video_args, *filter_args, "-c:a", "flac"]
    if target_ext == "wav":
        return [*video_args, *filter_args, "-c:a", "pcm_s16le"]
    if explicit_bitrate is not None and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS:
        return [*video_args, *filter_args, "-b:a", explicit_bitrate]
    return [*video_args, *filter_args]


def _build_mux_codec_args(
    source_ext: str,
    target_ext: str,
    info: AudioProbeInfo,
    audio_bitrate: str | None,
    faststart: bool = True,
    *,
    loudnorm: bool = False,
    mono: bool = False,
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
) -> list[str]:
    """Return FFmpeg codec/container flags for muxing or transcoding to `target_ext`."""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    video_only = preserve_video and not info.codec_name
    needs_bitrate_reencode = (
        not video_only
        and explicit_bitrate is not None
        and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS
    )
    needs_sample_rate_reencode = (
        not video_only
        and target_sample_rate is not None
        and info.sample_rate != target_sample_rate
    )
    if loudnorm or mono or needs_bitrate_reencode or needs_sample_rate_reencode:
        return _build_reencode_codec_args(
            target_ext,
            info,
            explicit_bitrate,
            faststart=faststart,
            loudnorm=loudnorm,
            mono=mono,
            sample_rate=target_sample_rate,
            preserve_video=preserve_video,
        )
    codec = info.codec_name
    video_args = ["-c:v", "copy"] if preserve_video else []
    if target_ext in MP4_CONTAINERS:
        if video_only:
            args = [*video_args]
        elif codec == "aac":
            args = [*video_args, "-c:a", "copy"]
            if source_ext in ("ts", "aac"):
                args += ["-bsf:a", "aac_adtstoasc"]
        else:
            target_bitrate = _resolve_aac_bitrate_from_info(info, explicit_bitrate)
            args = [*video_args, "-c:a", "aac", "-b:a", target_bitrate]
        if faststart:
            args += ["-movflags", "+faststart"]
        return args
    if target_ext == source_ext or target_ext in ("mka", "mkv") or (target_ext == "mp3" and codec == "mp3"):
        return [*video_args] if video_only else [*video_args, "-c:a", "copy"]
    if explicit_bitrate is not None and target_ext not in ffmpeg_metadata.LOSSLESS_CONTAINERS and not video_only:
        return [*video_args, "-b:a", explicit_bitrate]
    return video_args


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
    show_progress: bool = True,
) -> list[Chapter]:
    """Run FFmpeg `silencedetect` on `filepath` and synthesize chapter markers at pauses."""
    ffmpeg = _require_executable("ffmpeg")
    info = _get_cached_probe(filepath, probe_cache)
    expected = info.duration if info.duration > 0 else 0.0
    duration_ms = round(expected * 1000) if expected > 0 else None
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        filepath,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f",
        "null",
    ]
    if show_progress and expected > 0:
        command += ["-progress", "pipe:1", "-nostats"]
    command.append("-")

    if show_progress and expected > 0:
        with logging.progress([]) as prog:
            task_id = prog.add_task("Detecting silence chapters", total=expected)
            reported = 0.0

            def _on_silence_progress(seconds: float) -> None:
                nonlocal reported
                target = min(expected, max(0.0, seconds))
                delta = target - reported
                if delta > 0:
                    reported = target
                    prog.advance(task_id, delta)

            with ffmpeg_progress_callback(_on_silence_progress):
                result = run_cancellable_subprocess(command, cancel_event=cancel_event)
            if reported < expected:
                prog.advance(task_id, expected - reported)
    else:
        result = run_cancellable_subprocess(command, cancel_event=cancel_event)
    _log_ffmpeg_stderr(result)
    if result.returncode != 0:
        return []
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")

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


def resolve_ffmpeg_pool_workers(
    options_max_workers: int | None = None,
    *,
    cpu_bound: bool,
) -> int:
    """Resolve bounded worker count for FFmpeg/FFprobe pools from `max_workers` and CPU count."""
    cpus = max(1, os.cpu_count() or 4)
    has_explicit = isinstance(options_max_workers, int) and not isinstance(options_max_workers, bool) and options_max_workers > 0
    if cpu_bound:
        default_cap = min(COMBINE_REMUX_THREADS, cpus)
        requested = options_max_workers if has_explicit else default_cap
        return max(1, min(int(requested), cpus))  # type: ignore[arg-type]
    io_cap = max(2, cpus * 2)
    requested_io = options_max_workers if has_explicit else COMBINE_REMUX_THREADS
    return max(1, min(int(requested_io), min(COMBINE_REMUX_THREADS, io_cap)))  # type: ignore[arg-type]


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
            cancel_event=cancel_event,
        )
    except DownloadCancelled:
        if os.path.exists(dst_path):
            os.remove(dst_path)
        raise
    _log_ffmpeg_stderr(result)
    if result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
        return True
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
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
    show_progress: bool = True,
    max_workers: int | None = None,
) -> bool:
    """
    Combines the given audio/video files into `output_path` (optionally transcoding
    and embedding `chapters`, `metadata` tags, and MP4 `cover` art in the primary
    FFmpeg pass).

    :returns: `True` if `chapters` were embedded during the combine pass.
    """
    if not filepaths:
        raise FailedCombining
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    ffmpeg = _require_executable("ffmpeg")
    cache: dict[str, AudioProbeInfo] = probe_cache if probe_cache is not None else {}
    output_extension = get_extension(output_path)
    needs_bitrate_reencode = (
        explicit_bitrate is not None
        and output_extension not in ffmpeg_metadata.LOSSLESS_CONTAINERS
    )
    work_dir = f"{os.path.normpath(tmp_dir)}.work"
    ts_dir = os.path.join(work_dir, "ts_parts")
    chapter_file = os.path.join(work_dir, "ffmetadata.txt")
    cover_base_path = os.path.join(work_dir, "cover")
    cover_tmp_path: str | None = None
    os.makedirs(work_dir, exist_ok=True)
    chapters_embedded = False
    try:
        all_mpegts = _all_inputs_mpegts(filepaths, probe_cache=cache)
        input_infos: list[AudioProbeInfo] | None = None
        mixed_non_mp4_params = False
        if (
            not all_mpegts
            and not preserve_video
            and len(filepaths) > 1
            and output_extension not in MP4_CONTAINERS
        ):
            input_infos = _probe_uncached_audio_info(
                filepaths,
                cache,
                cancel_event=cancel_event,
                max_workers=max_workers,
            )
            codecs = {i.codec_name for i in input_infos if i.codec_name}
            sample_rates = {i.sample_rate for i in input_infos if i.sample_rate}
            channel_counts = {i.channels for i in input_infos if i.channels}
            mixed_non_mp4_params = (
                len(codecs) > 1 or len(sample_rates) > 1 or len(channel_counts) > 1
            )

        all_mp3_uniform = (
            not preserve_video
            and output_extension == "mp3"
            and all(get_extension(p) == "mp3" for p in filepaths)
            and not mixed_non_mp4_params
        )
        use_mpegts_pipeline = (
            all_mpegts
            or (not preserve_video and output_extension in MP4_CONTAINERS)
            or all_mp3_uniform
        )

        concat_demuxer_mode = False
        parts_encoded_audio = False
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
                    input_infos = _probe_uncached_audio_info(
                        filepaths,
                        cache,
                        cancel_event=cancel_event,
                        max_workers=max_workers,
                    )
                    sample_rates = {i.sample_rate for i in input_infos if i.sample_rate}
                    channel_counts = {i.channels for i in input_infos if i.channels}
                    any_non_aac = any(i.codec_name != "aac" for i in input_infos)
                    mixed_stream_params = len(sample_rates) > 1 or len(channel_counts) > 1
                    needs_sr_encode = target_sample_rate is not None and any(
                        i.sample_rate != target_sample_rate for i in input_infos
                    )
                    if any_non_aac or mixed_stream_params or needs_bitrate_reencode or needs_sr_encode:
                        parallel_aac_encoded = True
                        bitrate_info = next((i for i in input_infos if i.bitrate and i.bitrate > 0), input_infos[0])
                        target_bitrate = _resolve_aac_bitrate_from_info(bitrate_info, explicit_bitrate)
                        target_sr = target_sample_rate or (max(sample_rates) if sample_rates else None)
                        target_ch = max(channel_counts) if channel_counts else None

                padding = len(str(len(filepaths)))
                total_parts = len(filepaths)
                completed_parts = 0
                part_lock = threading.Lock()
                verb = "Encoding parts" if parallel_aac_encoded else "Remuxing parts"
                pool_workers = resolve_ffmpeg_pool_workers(
                    max_workers,
                    cpu_bound=parallel_aac_encoded,
                )

                def _run_remux_pool(remux_progress=None, remux_task=None) -> list[tuple[str, bool]]:
                    def remux(item):
                        nonlocal completed_parts
                        index, source = item
                        ts_path = os.path.join(ts_dir, f"{str(index).zfill(padding)}.ts")
                        with _PROBE_CACHE_LOCK:
                            src_info = input_infos[index] if input_infos is not None else cache.get(source)
                        needs_encode = parallel_aac_encoded and (
                            src_info is None
                            or src_info.codec_name != "aac"
                            or needs_bitrate_reencode
                            or (target_sample_rate is not None and src_info.sample_rate != target_sample_rate)
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
                            ok = _remux_to_mpegts(
                                source,
                                ts_path,
                                cancel_event=cancel_event,
                                preserve_video=preserve_video,
                            )
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
                            if remux_progress is not None and remux_task is not None:
                                remux_progress.advance(remux_task, 1.0)
                                logging.book_update(f"{verb} ({completed_parts}/{total_parts})")
                        return ts_path, ok

                    return run_cancellable_pool(
                        remux,
                        list(enumerate(filepaths)),
                        max_workers=pool_workers,
                        cancel_event=cancel_event,
                    )

                if show_progress:
                    with logging.progress([]) as remux_prog:
                        remux_task = remux_prog.add_task(
                            f"{verb} (0/{total_parts})",
                            total=float(total_parts),
                        )
                        remuxed = _run_remux_pool(remux_prog, remux_task)
                else:
                    remuxed = _run_remux_pool()
                ts_paths = []
                for ts_path, ok in remuxed:
                    if not ok:
                        raise FailedCombining
                    ts_paths.append(ts_path)

            parts_encoded_audio = parallel_aac_encoded and not loudnorm and not mono
            sample_info = _get_cached_probe(ts_paths[0], cache)
            if len(ts_paths) == 1:
                ffmpeg_input = ts_paths[0]
                expected = sample_info.duration
            else:
                concat_candidate = "concat:" + "|".join(ts_paths)
                if (
                    len(ts_paths) <= _MAX_CONCAT_PROTOCOL_FILES
                    and "|" not in "".join(ts_paths)
                    and len(concat_candidate) <= _MAX_CONCAT_ARG_CHARS
                ):
                    ffmpeg_input = concat_candidate
                else:
                    concat_list_path = os.path.join(work_dir, "concat_list.txt")
                    _write_concat_list_file(concat_list_path, ts_paths)
                    ffmpeg_input_flags = ["-f", "concat", "-safe", "0"]
                    ffmpeg_input = concat_list_path
                part_infos = _probe_uncached_audio_info(
                    ts_paths,
                    cache,
                    cancel_event=cancel_event,
                    max_workers=max_workers,
                )
                expected = sum(info.duration for info in part_infos)
            chapter_duration = expected
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
                target_sr = target_sample_rate or (max(sample_rates) if sample_rates else None)
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
                    max_workers=resolve_ffmpeg_pool_workers(max_workers, cpu_bound=True),
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
        cover_tmp_path = (
            None
            if preserve_video
            else _prepare_ffmpeg_cover_file(cover, output_extension, cover_base_path)
        )
        has_audio = bool(sample_info.codec_name or not (preserve_video and sample_info.video_codec_name))

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
            preserve_video=preserve_video,
            has_audio=has_audio,
        )
        # Note: concat_demuxer_mode with mixed non-MP4 inputs converted via lossless FLAC still targets output_extension cleanly
        _ = concat_demuxer_mode
        command += _build_mux_codec_args(
            mux_source_ext,
            output_extension,
            sample_info,
            None if parts_encoded_audio else explicit_bitrate,
            faststart=faststart,
            loudnorm=loudnorm,
            mono=mono,
            sample_rate=None if parts_encoded_audio else target_sample_rate,
            preserve_video=preserve_video,
        )
        if show_progress and expected > 0:
            command += ["-progress", "pipe:1", "-nostats"]
        command.append(output_path)
        if show_progress and expected > 0:
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
                        cancel_event=cancel_event,
                    )
                if reported < expected:
                    mux_prog.advance(mux_task, expected - reported)
        else:
            result = run_cancellable_subprocess(
                command,
                cancel_event=cancel_event,
            )
        _log_ffmpeg_stderr(result)
        if (
            result.returncode != 0
            or not (os.path.exists(output_path) and os.path.getsize(output_path) > 0)
        ):
            raise FailedCombining
        # Guard against silent truncation or corrupt output before committing.
        verify_audio_integrity(
            output_path,
            expected_duration=expected,
            probe_cache=cache,
            allow_video=preserve_video,
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
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
    show_progress: bool = True,
) -> tuple[str, bool]:
    """
    Convert a single file to `output_format`, optionally embedding `chapters`,
    `metadata` tags, and MP4 `cover` art in the same FFmpeg pass.
    Returns `(new_path, chapters_embedded)`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    target_format = output_format.lstrip(".").lower()
    path_without_ext, old_ext_raw = os.path.splitext(old_path)
    old_ext = old_ext_raw.lstrip(".").lower()
    new_path = output_path or f"{path_without_ext}.{target_format}"
    needs_bitrate_reencode = (
        explicit_bitrate is not None
        and target_format not in ffmpeg_metadata.LOSSLESS_CONTAINERS
    )
    same_path = output_path is None or os.path.abspath(new_path) == os.path.abspath(old_path)
    if (
        not loudnorm
        and not mono
        and not needs_bitrate_reencode
        and target_format == old_ext
        and same_path
    ):
        if target_sample_rate is None:
            return new_path, False
        pre_info = _get_cached_probe(old_path, probe_cache)
        if pre_info.sample_rate == target_sample_rate:
            return new_path, False

    ffmpeg = _require_executable("ffmpeg")
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
    cover_tmp_path = (
        None
        if preserve_video
        else _prepare_ffmpeg_cover_file(cover, target_format, f"{new_path}.cover.tmp")
    )
    has_audio = bool(info.codec_name or not (preserve_video and info.video_codec_name))

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
        preserve_video=preserve_video,
        has_audio=has_audio,
    )
    command += _build_mux_codec_args(
        old_ext,
        target_format,
        info,
        explicit_bitrate,
        faststart=faststart,
        loudnorm=loudnorm,
        mono=mono,
        sample_rate=target_sample_rate,
        preserve_video=preserve_video,
    )
    if show_progress and info.duration > 0:
        command += ["-progress", "pipe:1", "-nostats"]
    new_dir = os.path.dirname(os.path.abspath(new_path)) or os.getcwd()
    os.makedirs(new_dir, exist_ok=True)
    staged_path = os.path.join(
        new_dir,
        f".{os.path.basename(new_path)}.tmp.{uuid.uuid4().hex[:8]}.{target_format}",
    )
    command.append(staged_path)

    try:
        if show_progress and info.duration > 0:
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
                        cancel_event=cancel_event,
                    )
                if reported < info.duration:
                    conv_prog.advance(conv_task, info.duration - reported)
        else:
            result = run_cancellable_subprocess(
                command,
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
    _log_ffmpeg_stderr(result)
    if result.returncode != 0 or not (os.path.exists(staged_path) and os.path.getsize(staged_path) > 0):
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise GenericAudiobookDLException(
            f"Failed converting '{os.path.basename(old_path)}' to .{target_format}"
        )
    try:
        verified_info = verify_audio_integrity(
            staged_path,
            expected_duration=info.duration,
            allow_video=preserve_video,
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
    cancel_event: threading.Event | None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    *,
    preserve_video: bool = False,
) -> str:
    """Export one chapter slice from `master_path` to `out_path`."""
    duration_args = ["-t", f"{duration_sec:.3f}"] if duration_sec is not None else []
    stream_args = ["-map", "0:v:0?", "-map", "0:a?"] if preserve_video else ["-vn"]
    if accurate:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            master_path,
            "-ss",
            f"{start_sec:.3f}",
            *duration_args,
            *stream_args,
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
            *stream_args,
            *fast_codec_args,
            *format_args,
            out_path,
        ]
    res = run_cancellable_subprocess(
        cmd,
        cancel_event=cancel_event,
    )
    succeeded = (
        res.returncode == 0
        and os.path.exists(out_path)
        and os.path.getsize(out_path) > 0
    )
    if not succeeded and not accurate and "copy" in fast_codec_args:
        _log_ffmpeg_stderr(
            res,
            prefix=f"Fast split chapter {chapter_index + 1} failed, retrying with exact seek/re-encode: ",
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
            *stream_args,
            *reencode_codec_args,
            *format_args,
            out_path,
        ]
        res = run_cancellable_subprocess(
            fallback_cmd,
            cancel_event=cancel_event,
        )
        succeeded = (
            res.returncode == 0
            and os.path.exists(out_path)
            and os.path.getsize(out_path) > 0
        )
        _log_ffmpeg_stderr(res)
    else:
        _log_ffmpeg_stderr(res)
    if not succeeded:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise GenericAudiobookDLException(
            f"Failed splitting chapter {chapter_index + 1}: '{chapter_title}'"
        )
    try:
        verify_audio_integrity(out_path, probe_cache=probe_cache, allow_video=preserve_video)
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


def _chapter_output_filepath(
    output_dir: str,
    file_number: int,
    number_width: int,
    raw_title: str,
    target_format: str,
) -> str:
    """Build zero-padded, filename-safe chapter/lesson output path inside `output_dir`."""
    width = max(2, number_width)
    fallback = "Introduction" if file_number == 0 else f"Chapter {file_number}"
    safe_title = _truncate_utf8(_fix_output(raw_title or "") or fallback, 160)
    if platform.system() == "Windows":
        safe_title = _sanitize_windows_reserved_name(safe_title.rstrip(". ") or fallback)
    out_name = f"{file_number:0{width}d} - {safe_title}.{target_format}"
    return os.path.join(output_dir, out_name)


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
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
    max_workers: int | None = None,
    intro_chapter_mode: str = "auto",
) -> list[str]:
    """
    Split `master_path` into individual zero-padded chapter files inside `output_dir`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    if not chapters:
        return [master_path]
    ffmpeg = _require_executable("ffmpeg")
    os.makedirs(output_dir, exist_ok=True)
    src_ext = get_extension(master_path)
    target_format = (output_format or src_ext).lstrip(".").lower()
    info = _get_cached_probe(master_path, probe_cache)
    needs_bitrate_reencode = (
        explicit_bitrate is not None
        and target_format not in ffmpeg_metadata.LOSSLESS_CONTAINERS
    )
    needs_sample_rate_reencode = (
        target_sample_rate is not None and info.sample_rate != target_sample_rate
    )
    effective_accurate = (
        accurate
        or loudnorm
        or mono
        or needs_bitrate_reencode
        or needs_sample_rate_reencode
    )
    fast_codec_args = _build_mux_codec_args(
        src_ext,
        target_format,
        info,
        explicit_bitrate,
        loudnorm=loudnorm,
        mono=mono,
        sample_rate=target_sample_rate,
        preserve_video=preserve_video,
    )
    reencode_codec_args = _build_reencode_codec_args(
        target_format,
        info,
        explicit_bitrate,
        loudnorm=loudnorm,
        mono=mono,
        sample_rate=target_sample_rate,
        preserve_video=preserve_video,
    )
    format_args = ["-f", "mp4"] if target_format == "m4b" else []

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

    resolved_entries = resolve_split_chapter_entries(
        [raw_title for _s, _e, raw_title in spans],
        intro_mode=intro_chapter_mode,
    )
    number_width = max(2, len(str(resolved_entries[-1][0])))

    tasks = []
    for idx, ((start_ms, end_ms, _raw_title), (file_number, resolved_title)) in enumerate(
        zip(spans, resolved_entries, strict=True)
    ):
        start_sec = start_ms / 1000.0
        duration_sec: float | None = None
        if idx + 1 < len(spans) or (total_ms > 0 and end_ms < total_ms):
            duration_sec = max(0.05, (end_ms - start_ms) / 1000.0)
        out_path = _chapter_output_filepath(
            output_dir, file_number, number_width, resolved_title, target_format
        )
        tasks.append((idx, start_sec, duration_sec, out_path, resolved_title))

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
                        cancel_event=effective_cancel,
                        probe_cache=probe_cache,
                        preserve_video=preserve_video,
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

            exported_paths = run_cancellable_pool(
                _run_task,
                tasks,
                max_workers=resolve_ffmpeg_pool_workers(max_workers, cpu_bound=True),
                cancel_event=effective_cancel,
            )
            return list(exported_paths)
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


def merge_segment_groups_into_chapters(
    segment_groups: Sequence[tuple[str, Sequence[str]]],
    output_dir: str,
    output_format: str | None = None,
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    loudnorm: bool = False,
    mono: bool = False,
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
    max_workers: int | None = None,
    intro_chapter_mode: str = "auto",
) -> list[str]:
    """
    Merge pre-grouped segment lists directly into per-chapter/per-lesson files in `output_dir`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    non_empty = [(title, list(paths)) for title, paths in segment_groups if paths]
    if not non_empty:
        return []
    os.makedirs(output_dir, exist_ok=True)
    cache: dict[str, AudioProbeInfo] = probe_cache if probe_cache is not None else {}
    target_format = (output_format or get_extension(non_empty[0][1][0])).lstrip(".").lower()
    needs_bitrate_reencode = (
        explicit_bitrate is not None
        and target_format not in ffmpeg_metadata.LOSSLESS_CONTAINERS
    )
    total = len(non_empty)
    resolved_entries = resolve_split_chapter_entries(
        [title for title, _paths in non_empty],
        intro_mode=intro_chapter_mode,
    )
    number_width = max(2, len(str(resolved_entries[-1][0])))

    batch_abort = threading.Event()
    state_lock = threading.Lock()
    created: list[str] = []
    first_error: BaseException | None = None

    class _CancelMonitor:
        def is_set(self) -> bool:
            return batch_abort.is_set() or (cancel_event is not None and cancel_event.is_set())

    effective_cancel: Any = _CancelMonitor()
    logging.book_update(f"Merging {total} chapters")
    try:
        with tempfile.TemporaryDirectory() as tmp_root:
            with logging.progress([]) as merge_prog:
                merge_task = merge_prog.add_task(f"Merging ({total} chapters)", total=total)

                def _merge_one(item: tuple[int, tuple[tuple[str, list[str]], tuple[int, str]]]) -> str:
                    nonlocal first_error
                    idx, ((_raw_title, group_paths), (file_number, resolved_title)) = item
                    out_path = _chapter_output_filepath(
                        output_dir, file_number, number_width, resolved_title, target_format
                    )
                    try:
                        if len(group_paths) == 1:
                            src = group_paths[0]
                            can_copy = (
                                not loudnorm
                                and not mono
                                and not needs_bitrate_reencode
                                and get_extension(src) == target_format
                            )
                            if can_copy and target_sample_rate is not None:
                                src_info = _get_cached_probe(src, cache)
                                can_copy = src_info.sample_rate == target_sample_rate
                            if can_copy:
                                shutil.copy2(src, out_path)
                                if not (
                                    os.path.isfile(out_path)
                                    and os.path.getsize(out_path) == os.path.getsize(src)
                                    and os.path.getsize(out_path) > 0
                                ):
                                    if os.path.exists(out_path):
                                        os.remove(out_path)
                                    raise FailedCombining(
                                        message=f"Copied chapter file failed size verification: {os.path.basename(out_path)}"
                                    )
                            else:
                                convert_single_file(
                                    src,
                                    target_format,
                                    cancel_event=effective_cancel,
                                    audio_bitrate=explicit_bitrate,
                                    remove_source=False,
                                    probe_cache=cache,
                                    output_path=out_path,
                                    faststart=True,
                                    loudnorm=loudnorm,
                                    mono=mono,
                                    sample_rate=target_sample_rate,
                                    preserve_video=preserve_video,
                                    show_progress=False,
                                )
                        else:
                            group_tmp = os.path.join(tmp_root, f"grp_{idx}")
                            os.makedirs(group_tmp, exist_ok=True)
                            combine_audiofiles(
                                group_paths,
                                group_tmp,
                                out_path,
                                cancel_event=effective_cancel,
                                audio_bitrate=explicit_bitrate,
                                probe_cache=cache,
                                faststart=True,
                                loudnorm=loudnorm,
                                mono=mono,
                                sample_rate=target_sample_rate,
                                preserve_video=preserve_video,
                                show_progress=False,
                                max_workers=max_workers,
                            )
                    except BaseException as exc:
                        with state_lock:
                            if first_error is None:
                                first_error = exc
                        batch_abort.set()
                        raise
                    with state_lock:
                        created.append(out_path)
                        merge_prog.advance(merge_task, 1.0)
                    return out_path

                ordered = list(
                    run_cancellable_pool(
                        _merge_one,
                        list(enumerate(zip(non_empty, resolved_entries, strict=True))),
                        max_workers=resolve_ffmpeg_pool_workers(max_workers, cpu_bound=False),
                        cancel_event=effective_cancel,
                    )
                )
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

    consumed = {p for _title, paths in non_empty for p in paths}
    for part in consumed:
        if part not in ordered and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    return ordered


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
    sample_rate: int | str | None = None,
    preserve_video: bool = False,
    max_workers: int | None = None,
    intro_chapter_mode: str = "auto",
) -> list[str]:
    """
    Combine fine-grained HLS segment files seamlessly and split cleanly by `chapters`.
    """
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
    if not filepaths:
        return []
    cache: dict[str, AudioProbeInfo] = probe_cache if probe_cache is not None else {}
    target_format = (output_format or get_extension(filepaths[0])).lstrip(".").lower()
    fallback_master = "mp4" if preserve_video else "m4a"
    master_ext = (
        fallback_master
        if get_extension(filepaths[0]) in ("ts", "aac") and target_format in MP4_CONTAINERS
        else target_format
    )
    if master_ext == "ts":
        master_ext = fallback_master
    with tempfile.TemporaryDirectory() as tmp_dir:
        combine_work = os.path.join(tmp_dir, "combine_work")
        os.makedirs(combine_work, exist_ok=True)
        master_path = os.path.join(tmp_dir, f"master.{master_ext}")
        combine_audiofiles(
            filepaths,
            combine_work,
            master_path,
            cancel_event=cancel_event,
            audio_bitrate=explicit_bitrate,
            probe_cache=cache,
            loudnorm=loudnorm,
            mono=mono,
            sample_rate=target_sample_rate,
            preserve_video=preserve_video,
            max_workers=max_workers,
        )
        split_paths = split_audio_by_chapters(
            master_path,
            output_dir,
            chapters,
            output_format=target_format,
            cancel_event=cancel_event,
            probe_cache=cache,
            sample_rate=target_sample_rate,
            preserve_video=preserve_video,
            max_workers=max_workers,
            intro_chapter_mode=intro_chapter_mode,
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
    sample_rate: int | str | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    preserve_video: bool = False,
    max_workers: int | None = None,
):
    """Converts a list of media files into another format and returns new files"""
    explicit_bitrate = normalize_audio_bitrate(audio_bitrate)
    target_sample_rate = normalize_sample_rate(sample_rate)
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
            sample_rate=target_sample_rate,
            preserve_video=preserve_video,
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
                sample_rate=target_sample_rate,
                preserve_video=preserve_video,
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
            max_workers=resolve_ffmpeg_pool_workers(max_workers, cpu_bound=True),
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
        logging.log(f"output path too long, using {segments[-1]} as filename base")
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

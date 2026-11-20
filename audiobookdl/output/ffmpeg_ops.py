import hashlib
import os
import re
import threading
import zipfile
from typing import Any
from collections.abc import Sequence, Mapping

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
    normalize_sample_rate,
    probe_audio_info,
)
from audiobookdl.utils.audiobook import is_ebook_extension
from audiobookdl.utils.concurrency import (
    ffmpeg_progress_callback,
    run_cancellable_pool,
    run_cancellable_subprocess,
)

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

def _probe_audio_info(
    path: str,
    *,
    cancel_event: threading.Event | None = None,
) -> AudioProbeInfo:
    """Return consolidated media info for `path` via a single `ffprobe` call."""
    if cancel_event is None:
        return probe_audio_info(path)
    return probe_audio_info(path, cancel_event=cancel_event)


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
    *,
    cancel_event: threading.Event | None = None,
) -> AudioProbeInfo:
    """Return cached `AudioProbeInfo` for `path`, probing once and storing on miss."""
    if probe_cache is not None:
        with _PROBE_CACHE_LOCK:
            cached = probe_cache.get(path)
        if cached is not None:
            return cached
    info = (
        _probe_audio_info(path, cancel_event=cancel_event)
        if cancel_event is not None
        else _probe_audio_info(path)
    )
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
        probe_fn = (
            (lambda p: _probe_audio_info(p, cancel_event=cancel_event))
            if cancel_event is not None
            else _probe_audio_info
        )
        probed = run_cancellable_pool(
            probe_fn,
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
    info = _get_cached_probe(filepath, probe_cache, cancel_event=cancel_event)
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
        with logging.progress() as prog:
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

def get_extension(path: str) -> str:
    """
    Get lowercase extension from path

    :param path: Path to get extension from
    :returns: Extension of path
    """
    return os.path.splitext(path)[1][1:].lower()


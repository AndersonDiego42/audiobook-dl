from audiobookdl import logging as logging
from audiobookdl import AudiobookMetadata as AudiobookMetadata
from audiobookdl import Chapter as Chapter
from audiobookdl import Cover as Cover
from audiobookdl.exceptions import (
    DownloadCancelled as DownloadCancelled,
    FailedCombining as FailedCombining,
    GenericAudiobookDLException as GenericAudiobookDLException,
    MissingDependency as MissingDependency,
)
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.output.metadata._chapters import (
    normalize_chapters as normalize_chapters,
    resolve_split_chapter_entries as resolve_split_chapter_entries,
)
from audiobookdl.output.metadata._cover import normalize_cover_image as normalize_cover_image
from audiobookdl.utils import (
    AudioProbeInfo as AudioProbeInfo,
    MAX_AUTO_M4B_BITRATE_KBPS as MAX_AUTO_M4B_BITRATE_KBPS,
    MIN_AUTO_M4B_BITRATE_KBPS as MIN_AUTO_M4B_BITRATE_KBPS,
    DEFAULT_M4B_BITRATE as DEFAULT_M4B_BITRATE,
    MP4_CONTAINERS as MP4_CONTAINERS,
    find_executable as find_executable,
    normalize_audio_bitrate as normalize_audio_bitrate,
    normalize_sample_rate as normalize_sample_rate,
    probe_audio_info as probe_audio_info,
)
from audiobookdl.utils.audiobook import is_ebook_extension as is_ebook_extension
from audiobookdl.utils.fs import replace_with_retry as replace_with_retry
from audiobookdl.utils.concurrency import (
    ffmpeg_progress_callback as ffmpeg_progress_callback,
    run_cancellable_pool as run_cancellable_pool,
    run_cancellable_subprocess as run_cancellable_subprocess,
)

import hashlib as hashlib
import os as os
import re as re
import shutil as shutil
import platform as platform
import tempfile as tempfile
import threading as threading
import uuid as uuid
import zipfile as zipfile
from typing import Any as Any
from collections.abc import Sequence as Sequence
from collections.abc import Mapping as Mapping
from urllib.parse import urlparse as urlparse

from audiobookdl.output.ffmpeg_ops import (
    COMBINE_REMUX_THREADS as COMBINE_REMUX_THREADS,
    _PROBE_CACHE_LOCK as _PROBE_CACHE_LOCK,
    _MPEGTS_PACKET_SIZE as _MPEGTS_PACKET_SIZE,
    _MPEGTS_SYNC_BYTE as _MPEGTS_SYNC_BYTE,
    _SILENCE_END_RE as _SILENCE_END_RE,
    _TEXT_ERROR_PREFIXES as _TEXT_ERROR_PREFIXES,
    _ISO_BMFF_BOX_TYPES as _ISO_BMFF_BOX_TYPES,
    _require_executable as _require_executable,
    _log_ffmpeg_stderr as _log_ffmpeg_stderr,
    _probe_audio_info as _probe_audio_info,
    _get_cached_probe as _get_cached_probe,
    _looks_like_text_error_payload as _looks_like_text_error_payload,
    _has_valid_media_magic as _has_valid_media_magic,
    _resolve_aac_bitrate_from_info as _resolve_aac_bitrate_from_info,
    _run_mpegts_part as _run_mpegts_part,
    _remux_to_mpegts as _remux_to_mpegts,
    _encode_to_aac_mpegts as _encode_to_aac_mpegts,
    _probe_uncached_audio_info as _probe_uncached_audio_info,
    _is_mpegts_file as _is_mpegts_file,
    _all_inputs_mpegts as _all_inputs_mpegts,
    _write_ffmetadata as _write_ffmetadata,
    _prepare_ffmpeg_cover_file as _prepare_ffmpeg_cover_file,
    _append_ffmetadata_and_cover_args as _append_ffmetadata_and_cover_args,
    _build_reencode_codec_args as _build_reencode_codec_args,
    _build_mux_codec_args as _build_mux_codec_args,
    _write_concat_list_file as _write_concat_list_file,
    _encode_uniform_concat_part as _encode_uniform_concat_part,
    get_cached_probe as get_cached_probe,
    store_cached_probe as store_cached_probe,
    move_cached_probe as move_cached_probe,
    verify_audio_integrity as verify_audio_integrity,
    verify_ebook_integrity as verify_ebook_integrity,
    verify_file_checksums as verify_file_checksums,
    verify_downloaded_file_integrity as verify_downloaded_file_integrity,
    apply_chapter_offset as apply_chapter_offset,
    detect_silence_chapters as detect_silence_chapters,
    resolve_ffmpeg_pool_workers as resolve_ffmpeg_pool_workers,
    get_extension as get_extension,
)

from audiobookdl.output.templates import (
    _SafeFormatDict as _SafeFormatDict,
    LOCATION_DEFAULTS as LOCATION_DEFAULTS,
    OUTPUT_TEMPLATE_PRESETS as OUTPUT_TEMPLATE_PRESETS,
    OUTPUT_PATH_EXT_RESERVE as OUTPUT_PATH_EXT_RESERVE,
    WINDOWS_MAX_PATH as WINDOWS_MAX_PATH,
    _WINDOWS_RESERVED_NAMES as _WINDOWS_RESERVED_NAMES,
    _sanitize_windows_reserved_name as _sanitize_windows_reserved_name,
    _truncate_utf8 as _truncate_utf8,
    _utf16_len as _utf16_len,
    _truncate_utf16_units as _truncate_utf16_units,
    _sanitize_metadata_dict as _sanitize_metadata_dict,
    _expand_conditional_blocks as _expand_conditional_blocks,
    _join_segments as _join_segments,
    _fix_output as _fix_output,
    _remove_chars as _remove_chars,
    get_max_name_length as get_max_name_length,
    gen_output_location as gen_output_location,
)

_MAX_CONCAT_ARG_CHARS = 16000
_MAX_CONCAT_PROTOCOL_FILES = 64

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
                                src_info = _get_cached_probe(source, cache, cancel_event=cancel_event)
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
                                log_interval = max(1, total_parts // 20)
                                if completed_parts == total_parts or completed_parts % log_interval == 0:
                                    logging.book_update(f"{verb} ({completed_parts}/{total_parts})")
                        return ts_path, ok

                    return run_cancellable_pool(
                        remux,
                        list(enumerate(filepaths)),
                        max_workers=pool_workers,
                        cancel_event=cancel_event,
                    )

                if show_progress:
                    with logging.progress() as remux_prog:
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
            sample_info = _get_cached_probe(ts_paths[0], cache, cancel_event=cancel_event)
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
            sample_info = _get_cached_probe(concat_sources[0], cache, cancel_event=cancel_event)
            if len(concat_sources) == 1:
                ffmpeg_input = concat_sources[0]
                expected = sample_info.duration
            else:
                concat_list_path = os.path.join(work_dir, "concat_list.txt")
                _write_concat_list_file(concat_list_path, concat_sources)
                ffmpeg_input_flags = ["-f", "concat", "-safe", "0"]
                ffmpeg_input = concat_list_path
                part_infos = _probe_uncached_audio_info(
                    concat_sources, cache, cancel_event=cancel_event, max_workers=max_workers
                )
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
            with logging.progress() as mux_prog:
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
        pre_info = _get_cached_probe(old_path, probe_cache, cancel_event=cancel_event)
        if pre_info.sample_rate == target_sample_rate:
            return new_path, False

    ffmpeg = _require_executable("ffmpeg")
    info = _get_cached_probe(old_path, probe_cache, cancel_event=cancel_event)
    unique_suffix = uuid.uuid4().hex[:8]
    chapter_file = f"{new_path}.{unique_suffix}.chapters.tmp.txt"
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
        else _prepare_ffmpeg_cover_file(
            cover, target_format, f"{new_path}.{unique_suffix}.cover.tmp"
        )
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
            with logging.progress() as conv_prog:
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
    replace_with_retry(staged_path, new_path)
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
    try:
        res = run_cancellable_subprocess(
            cmd,
            cancel_event=cancel_event,
        )
    except BaseException:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise
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
        try:
            res = run_cancellable_subprocess(
                fallback_cmd,
                cancel_event=cancel_event,
            )
        except BaseException:
            if os.path.exists(out_path):
                os.remove(out_path)
            raise
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
    info = _get_cached_probe(master_path, probe_cache, cancel_event=cancel_event)
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
        with logging.progress() as split_prog:
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
            with logging.progress() as merge_prog:
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
                                src_info = _get_cached_probe(src, cache, cancel_event=effective_cancel)
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
    progress_lock = threading.Lock()
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
                show_progress=False,
            )
        except BaseException as exc:
            with error_lock:
                if first_error is None:
                    first_error = exc
            batch_abort.set()
            raise
        with progress_lock:
            conv_prog.advance(conv_task, 1.0)
        return old_path, new_path, (target_format != old_ext)

    new_paths: list[str] = []
    converted_pairs: list[tuple[str, str]] = []
    try:
        with logging.progress() as conv_prog:
            conv_task = conv_prog.add_task("Converting files", total=float(len(filenames)))
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


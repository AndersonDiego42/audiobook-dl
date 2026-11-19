from audiobookdl import logging, Audiobook
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.exceptions import (
    CloudflareBlocked,
    DownloadError,
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
)
from audiobookdl.output.encryption import StreamDecryptor
from audiobookdl.output.metadata import id3
from audiobookdl.output.metadata._chapters import (
    apply_intro_chapter_titles,
    resolve_audio_duration_ms,
)
from audiobookdl.output.metadata._cover import apply_cover_postprocessing
from audiobookdl.utils import normalize_audio_bitrate, normalize_sample_rate
from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    TrackTagContext,
    format_bytes,
    is_ebook,
    is_ebook_extension,
    is_video,
    normalize_checksums,
    resolve_audiobook_output_format,
)
from mutagen import File as MutagenFile
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    TokenBucketRateLimiter,
    compute_retry_delay,
    get_header_ci,
    is_cloudflare_challenge,
    is_content_type_compatible,
    is_transient_network_error,
    parse_byte_range_header,
    parse_byte_rate,
    parse_content_md5,
    parse_repr_digest,
    sleep_or_cancel,
)
from . import metadata, output, encryption
from .metadata import sidecars

from dataclasses import dataclass
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import requests
from functools import partial
from typing import Any, cast
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from math import log10


# Default per-file retries after the initial attempt (5 total attempts by default)
DEFAULT_DOWNLOAD_RETRIES = DEFAULT_MAX_RETRIES
DOWNLOAD_ATTEMPTS = DEFAULT_DOWNLOAD_RETRIES + 1
# 64 KiB read chunks balance throughput with prompt cancel_event checks
DOWNLOAD_CHUNK_SIZE = 64 * 1024
# Conservative default parallel workers per book to reduce CDN 429/anti-abuse throttling
DEFAULT_DOWNLOAD_WORKERS = 6
# Upper bound on parallel file download workers (matched to Session pool_maxsize)
MAX_DOWNLOAD_WORKERS = 20


def download(
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    *,
    chapters_already_overridden: bool = False,
):
    """
    Download contents of audiobook

    :param audiobook: Audiobook to download
    :param options: Runtime download options
    :param interaction: Optional handler for confirmation prompts
    :param cancel_event: Optional event checked to cancel download
    :param chapters_already_overridden: Whether upstream enrichment already replaced chapters
    """
    try:
        output_dir = output.gen_output_location(
            options.output_template,
            audiobook.metadata,
            options.remove_chars
        )
        download_audiobook(
            audiobook,
            output_dir,
            options,
            interaction=interaction,
            cancel_event=cancel_event,
            chapters_already_overridden=chapters_already_overridden,
        )
    except KeyboardInterrupt:
        raise DownloadCancelled from None


def _safe_remove(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _all_output_parts_exist(
    audiobook: Audiobook,
    output_dir: str,
    output_format: str | None,
    *,
    verify: bool = False,
    allow_video: bool = False,
    probe_cache: dict[str, Any] | None = None,
) -> bool:
    """Return True when every expected multi-part output file already exists (and passes integrity checks when `verify=True`)."""
    if not os.path.isdir(output_dir) or not audiobook.files:
        return False
    target_ext = output_format.lstrip(".").lower() if output_format else None
    for idx, file_spec in enumerate(audiobook.files):
        raw_path, _ = create_filepath(audiobook, output_dir, idx)
        src_ext = file_spec.ext.lstrip(".").lower()
        if target_ext:
            base, _ = os.path.splitext(raw_path)
            expected_path = f"{base}.{target_ext}"
            part_ext = target_ext
        else:
            expected_path = raw_path
            part_ext = src_ext
        if not (os.path.exists(expected_path) and os.path.getsize(expected_path) > 0):
            return False
        if verify:
            checksums = (
                normalize_checksums(getattr(file_spec, "checksum", None))
                if part_ext == src_ext
                else None
            )
            try:
                info = output.verify_downloaded_file_integrity(
                    expected_path,
                    ext=part_ext,
                    allow_video=allow_video,
                    probe_cache=probe_cache,
                    expected_checksums=checksums or None,
                )
                if info is not None:
                    output.store_cached_probe(probe_cache, expected_path, info)
            except FailedCombining as exc:
                logging.warning(
                    f"Warning: Existing part '{os.path.basename(expected_path)}' "
                    f"failed integrity check ({exc}); will re-download."
                )
                return False
    return True


def _build_chapter_segment_groups(
    audiobook: Audiobook,
    filepaths: Sequence[str],
) -> list[tuple[str, list[str]]] | None:
    """Group downloaded segment paths by `AudiobookFile.chapter_index` when multi-segment chapters exist."""
    if not filepaths or len(filepaths) != len(audiobook.files):
        return None
    if not all(f.chapter_index is not None for f in audiobook.files):
        return None
    grouped: dict[int, list[str]] = {}
    for file_spec, path in zip(audiobook.files, filepaths, strict=True):
        idx = int(file_spec.chapter_index)  # type: ignore[arg-type]
        grouped.setdefault(idx, []).append(path)
    if len(filepaths) <= len(grouped):
        return None
    result: list[tuple[str, list[str]]] = []
    for ch_idx in sorted(grouped):
        title = (
            audiobook.chapters[ch_idx].title
            if 0 <= ch_idx < len(audiobook.chapters) and audiobook.chapters[ch_idx].title
            else f"Chapter {ch_idx + 1}"
        )
        result.append((title, grouped[ch_idx]))
    return result


def download_audiobook(
    audiobook: Audiobook,
    output_dir: str,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    *,
    chapters_already_overridden: bool = False,
):
    """Download, convert, combine, and add metadata to files from `Audiobook` object"""
    # Check if file/dir exists and should be skipped or confirmed before overwriting
    ebook = is_ebook(audiobook)
    video = is_video(audiobook)
    wants_split = (not ebook) and bool(getattr(options, "split_chapters", False))
    override_chapters = (
        bool(getattr(options, "override_chapters", False))
        and not chapters_already_overridden
    )
    may_silence_chapters = bool(
        not ebook
        and not video
        and getattr(options, "chapters_from_silence", False)
        and (not audiobook.chapters or override_chapters)
        and not options.no_chapters
    )
    defer_chapters_for_silence = may_silence_chapters and override_chapters
    splitting = wants_split and not defer_chapters_for_silence and len(audiobook.chapters) > 1
    expected_split_output = splitting or (wants_split and may_silence_chapters)
    is_single_file = (len(audiobook.files) == 1 or (options.combine and not ebook)) and not expected_split_output
    target_path = ""
    if audiobook.files:
        output_format = resolve_audiobook_output_format(audiobook, options.output_format) or ""
        target_path = f"{output_dir}.{output_format}" if output_format else output_dir

    confirmed_target_overwrite = False
    probe_cache: dict[str, Any] = {}
    if options.skip_downloaded or options.resume:
        if is_single_file:
            if target_path and os.path.exists(target_path) and (
                (options.skip_downloaded and not options.resume)
                or os.path.getsize(target_path) > 0
            ):
                skip_ok = True
                if options.resume and not options.skip_downloaded:
                    target_ext = (
                        resolve_audiobook_output_format(audiobook, options.output_format)
                        or os.path.splitext(target_path)[1].lstrip(".").lower()
                    )
                    single_checksums = (
                        normalize_checksums(getattr(audiobook.files[0], "checksum", None))
                        if len(audiobook.files) == 1
                        and target_ext == audiobook.files[0].ext.lstrip(".").lower()
                        else None
                    )
                    try:
                        info = output.verify_downloaded_file_integrity(
                            target_path,
                            ext=target_ext,
                            allow_video=video,
                            probe_cache=probe_cache,
                            expected_checksums=single_checksums or None,
                        )
                        if info is not None:
                            output.store_cached_probe(probe_cache, target_path, info)
                    except FailedCombining as exc:
                        logging.warning(
                            f"Warning: Existing file '{os.path.basename(target_path)}' "
                            f"failed integrity check ({exc}); re-downloading."
                        )
                        skip_ok = False
                if skip_ok:
                    msg = (
                        f"Resuming: skipping already downloaded {audiobook.title}, file already exists."
                        if options.resume and not options.skip_downloaded
                        else f"Skipping {audiobook.title}, file already exists."
                    )
                    logging.log(msg)
                    return
        elif options.resume and not expected_split_output:
            if _all_output_parts_exist(
                audiobook,
                output_dir,
                resolve_audiobook_output_format(audiobook, options.output_format),
                verify=(not options.skip_downloaded),
                allow_video=video,
                probe_cache=probe_cache,
            ):
                msg = (
                    f"Resuming: skipping already downloaded {audiobook.title}, all files already exist."
                    if not options.skip_downloaded
                    else f"Skipping {audiobook.title}, all files already exist."
                )
                logging.log(msg)
                return
        elif os.path.isdir(output_dir):  # multiple files, skip_downloaded without resume
            logging.log(f"Skipping {audiobook.title}, directory already exists.")
            return
    elif is_single_file and target_path:
        confirmed_target_overwrite = os.path.exists(target_path)
        confirm_overwrite_file(
            target_path,
            interaction=interaction,
            overwrite=options.overwrite,
        )

    use_staging_dir = (not options.resume) and (len(audiobook.files) > 1 or expected_split_output)
    stage_dir: str | None = None
    work_output_dir = output_dir
    download_output_dir = output_dir
    staged_combined_path: str | None = None

    if use_staging_dir:
        if not is_single_file and os.path.isdir(output_dir):
            _confirm_overwrite_dir(
                output_dir,
                interaction=interaction,
                overwrite=options.overwrite,
            )
        parent_dir = os.path.dirname(os.path.abspath(output_dir)) or os.getcwd()
        os.makedirs(parent_dir, exist_ok=True)
        stage_base = os.path.basename(output_dir) or "book"
        stage_dir = tempfile.mkdtemp(prefix=f".{stage_base}.staging-", dir=parent_dir)
        work_output_dir = stage_dir
        if len(audiobook.files) == 1:
            download_output_dir = os.path.join(stage_dir, stage_base)
        else:
            download_output_dir = stage_dir

    try:
        # Downloading files
        filepaths = download_files_with_progress(
            audiobook,
            download_output_dir,
            interaction=interaction,
            cancel_event=cancel_event,
            resume=options.resume,
            confirm_overwrite=(
                not use_staging_dir
                and ((not is_single_file) or (len(audiobook.files) > 1 and not confirmed_target_overwrite))
            ),
            overwrite=options.overwrite,
            max_workers=options.max_workers,
            timeout=options.timeout,
            max_retries=getattr(options, "max_retries", None),
            limit_rate=getattr(options, "limit_rate", None),
            probe_cache=probe_cache,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        # Converting & combining files (skipped for ebook files such as .epub)
        requested_post_format = (
            resolve_audiobook_output_format(audiobook, options.output_format)
            if video
            else options.output_format
        )
        current_format, output_format = get_output_audio_format(
            None if ebook else requested_post_format,
            filepaths,
        )
        chapters_embedded = False
        cover_embedded = False
        already_embedded_staged = False
        chapter_offset_ms = int(getattr(options, "chapter_offset_ms", 0) or 0)
        intro_chapter_mode = str(getattr(options, "intro_chapter_mode", "auto") or "auto")
        silence_noise_db = float(getattr(options, "silence_noise_db", -35.0))
        silence_min_seconds = float(getattr(options, "silence_min_seconds", 1.8))
        if not ebook:
            if audiobook.cover is not None and (
                getattr(options, "square_cover", False)
                or getattr(options, "max_cover_size", None)
            ):
                audiobook.cover = apply_cover_postprocessing(
                    audiobook.cover,
                    square=bool(getattr(options, "square_cover", False)),
                    max_size=getattr(options, "max_cover_size", None),
                )
            if audiobook.chapters and chapter_offset_ms != 0:
                audiobook.chapters = output.apply_chapter_offset(
                    audiobook.chapters, chapter_offset_ms
                )
            if may_silence_chapters and len(filepaths) == 1:
                detected = output.detect_silence_chapters(
                    filepaths[0],
                    noise_db=silence_noise_db,
                    min_silence_sec=silence_min_seconds,
                    cancel_event=cancel_event,
                    probe_cache=probe_cache,
                )
                if detected:
                    if chapter_offset_ms != 0:
                        detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                    audiobook.chapters = detected
            if audiobook.chapters and not options.no_chapters:
                audiobook.chapters = apply_intro_chapter_titles(
                    audiobook.chapters, intro_mode=intro_chapter_mode
                )

        # Re-evaluate after silence chapter detection on single-file inputs
        splitting = wants_split and len(audiobook.chapters) > 1

        chapters_for_ffmpeg = (
            audiobook.chapters
            if (
                not ebook
                and audiobook.chapters
                and not options.no_chapters
                and not id3.is_id3_file(f"{output_dir}.{output_format}")
            )
            else None
        )
        cover_for_ffmpeg = (
            audiobook.cover
            if (
                not ebook
                and not video
                and audiobook.cover is not None
                and output_format in output.MP4_CONTAINERS
            )
            else None
        )
        use_loudnorm = bool(getattr(options, "loudnorm", False))
        use_mono = bool(getattr(options, "mono", False))
        explicit_bitrate = normalize_audio_bitrate(getattr(options, "audio_bitrate", None))
        target_sample_rate = normalize_sample_rate(getattr(options, "sample_rate", None))
        audio_filter_kwargs: dict[str, Any] = {}
        if use_loudnorm:
            audio_filter_kwargs["loudnorm"] = True
        if use_mono:
            audio_filter_kwargs["mono"] = True
        if target_sample_rate is not None:
            audio_filter_kwargs["sample_rate"] = target_sample_rate
        segment_groups = (
            _build_chapter_segment_groups(audiobook, filepaths)
            if not ebook and not options.combine and not defer_chapters_for_silence
            else None
        )

        max_workers = getattr(options, "max_workers", None)
        if splitting and segment_groups is not None:
            filepaths = output.merge_segment_groups_into_chapters(
                segment_groups,
                work_output_dir,
                output_format=output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
                probe_cache=probe_cache,
                preserve_video=video,
                max_workers=max_workers,
                intro_chapter_mode=intro_chapter_mode,
                **audio_filter_kwargs,
            )
            current_format = output_format
        elif splitting or (wants_split and may_silence_chapters and len(filepaths) > 1):
            if len(filepaths) > 1:
                fallback_master = "mp4" if video else "m4a"
                master_ext = (
                    fallback_master
                    if current_format in ("ts", "aac") and output_format in output.MP4_CONTAINERS
                    else output_format
                )
                if master_ext == "ts":
                    master_ext = fallback_master
                with tempfile.TemporaryDirectory() as tmp_split_dir:
                    combine_work = os.path.join(tmp_split_dir, "combine_work")
                    os.makedirs(combine_work, exist_ok=True)
                    master_path = os.path.join(tmp_split_dir, f"master.{master_ext}")
                    output.combine_audiofiles(
                        filepaths,
                        combine_work,
                        master_path,
                        cancel_event=cancel_event,
                        audio_bitrate=options.audio_bitrate,
                        probe_cache=probe_cache,
                        preserve_video=video,
                        max_workers=max_workers,
                        **audio_filter_kwargs,
                    )
                    if may_silence_chapters and (not audiobook.chapters or override_chapters):
                        detected = output.detect_silence_chapters(
                            master_path,
                            noise_db=silence_noise_db,
                            min_silence_sec=silence_min_seconds,
                            cancel_event=cancel_event,
                            probe_cache=probe_cache,
                        )
                        if detected:
                            if chapter_offset_ms != 0:
                                detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                            audiobook.chapters = apply_intro_chapter_titles(
                                detected, intro_mode=intro_chapter_mode
                            )
                    splitting = wants_split and len(audiobook.chapters) > 1
                    if splitting:
                        split_filter_kwargs: dict[str, Any] = (
                            {"sample_rate": target_sample_rate}
                            if target_sample_rate is not None
                            else {}
                        )
                        split_result = output.split_audio_by_chapters(
                            master_path,
                            work_output_dir,
                            audiobook.chapters,
                            output_format=output_format,
                            cancel_event=cancel_event,
                            audio_bitrate=None if explicit_bitrate is not None else options.audio_bitrate,
                            probe_cache=probe_cache,
                            preserve_video=video,
                            max_workers=max_workers,
                            intro_chapter_mode=intro_chapter_mode,
                            **split_filter_kwargs,
                        )
                        for src_part in filepaths:
                            if src_part not in split_result and os.path.exists(src_part):
                                try:
                                    os.remove(src_part)
                                except OSError:
                                    pass
                        filepaths = split_result
                    else:
                        # Silence detection yielded <= 1 chapter: promote combined master as single file
                        fallback_path = os.path.join(work_output_dir, f"01 - {os.path.basename(output_dir)}.{output_format}")
                        shutil.move(master_path, fallback_path)
                        output.move_cached_probe(probe_cache, master_path, fallback_path)
                        filepaths = [fallback_path]
            elif len(filepaths) == 1:
                src_single = filepaths[0]
                filepaths = output.split_audio_by_chapters(
                    src_single,
                    work_output_dir,
                    audiobook.chapters,
                    output_format=output_format,
                    cancel_event=cancel_event,
                    audio_bitrate=options.audio_bitrate,
                    probe_cache=probe_cache,
                    preserve_video=video,
                    max_workers=max_workers,
                    intro_chapter_mode=intro_chapter_mode,
                    **audio_filter_kwargs,
                )
                if src_single not in filepaths and os.path.exists(src_single):
                    os.remove(src_single)
            current_format = output_format
        elif options.combine and not ebook and len(filepaths) > 1:
            logging.book_update("Combining files")
            output_path = f"{output_dir}.{output_format}"
            out_parent = os.path.dirname(os.path.abspath(output_path)) or os.getcwd()
            os.makedirs(out_parent, exist_ok=True)
            staged_combined_path = os.path.join(
                out_parent,
                f".{os.path.basename(output_path)}.tmp.{uuid.uuid4().hex[:8]}.{output_format}",
            )
            # Defer MP4 cover embedding when post-combine silence chapter remux will run
            defer_cover_for_silence = may_silence_chapters and cover_for_ffmpeg is not None
            combine_cover = None if defer_cover_for_silence else cover_for_ffmpeg
            combine_chapters = None if may_silence_chapters else chapters_for_ffmpeg
            consumed_parts = list(filepaths)
            try:
                chapters_embedded = output.combine_audiofiles(
                    filepaths,
                    work_output_dir,
                    staged_combined_path,
                    cancel_event=cancel_event,
                    chapters=combine_chapters,
                    audio_bitrate=options.audio_bitrate,
                    metadata=audiobook.metadata,
                    cover=combine_cover,
                    probe_cache=probe_cache,
                    faststart=True,
                    preserve_video=video,
                    max_workers=max_workers,
                    **audio_filter_kwargs,
                )
                if may_silence_chapters and (not audiobook.chapters or override_chapters):
                    detected = output.detect_silence_chapters(
                        staged_combined_path,
                        noise_db=silence_noise_db,
                        min_silence_sec=silence_min_seconds,
                        cancel_event=cancel_event,
                        probe_cache=probe_cache,
                    )
                    if detected:
                        if chapter_offset_ms != 0:
                            detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                        audiobook.chapters = apply_intro_chapter_titles(
                            detected, intro_mode=intro_chapter_mode
                        )
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled
                cover_embedded = combine_cover is not None
                add_metadata_to_file(
                    audiobook,
                    staged_combined_path,
                    options,
                    cancel_event=cancel_event,
                    chapters_embedded=chapters_embedded,
                    cover_embedded=cover_embedded,
                    write_sidecars=False,
                )
                staged_key = staged_combined_path
                os.replace(staged_combined_path, output_path)
                staged_combined_path = None
                output.move_cached_probe(probe_cache, staged_key, output_path)
                if not use_staging_dir:
                    for part_path in consumed_parts:
                        for candidate in (part_path, f"{part_path}.tmp"):
                            if os.path.isfile(candidate):
                                try:
                                    os.remove(candidate)
                                except OSError:
                                    pass
                    if os.path.isdir(work_output_dir):
                        try:
                            if not os.listdir(work_output_dir):
                                os.rmdir(work_output_dir)
                        except OSError:
                            pass
                already_embedded_staged = True
                current_format = output_format
                filepaths = [output_path]
            except DownloadCancelled:
                if use_staging_dir and os.path.isdir(work_output_dir):
                    shutil.rmtree(work_output_dir, ignore_errors=True)
                raise
        elif not options.combine and not ebook and segment_groups is not None:
            filepaths = output.merge_segment_groups_into_chapters(
                segment_groups,
                work_output_dir,
                output_format=output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
                probe_cache=probe_cache,
                preserve_video=video,
                max_workers=max_workers,
                intro_chapter_mode=intro_chapter_mode,
                **audio_filter_kwargs,
            )
            current_format = output_format
        elif (
            not options.combine
            and not ebook
            and not defer_chapters_for_silence
            and len(filepaths) > 1
            and output.should_smart_merge_hls(filepaths, audiobook.chapters, probe_cache=probe_cache)
        ):
            filepaths = output.merge_hls_segments_into_chapters(
                filepaths,
                work_output_dir,
                audiobook.chapters,
                output_format=output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
                probe_cache=probe_cache,
                preserve_video=video,
                max_workers=max_workers,
                intro_chapter_mode=intro_chapter_mode,
                **audio_filter_kwargs,
            )
            current_format = output_format
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        needs_bitrate_reencode = (
            explicit_bitrate is not None
            and output_format not in metadata.ffmpeg.LOSSLESS_CONTAINERS
        )
        needs_audio_reencode = (
            use_loudnorm
            or use_mono
            or needs_bitrate_reencode
            or (target_sample_rate is not None)
        )
        if (
            (current_format != output_format or needs_audio_reencode)
            and not splitting
            and not ebook
            and not already_embedded_staged
        ):
            logging.book_update("Converting files")
            if len(filepaths) == 1:
                converted_path, chapters_embedded = output.convert_single_file(
                    filepaths[0],
                    output_format,
                    cancel_event=cancel_event,
                    audio_bitrate=options.audio_bitrate,
                    chapters=chapters_for_ffmpeg,
                    metadata=audiobook.metadata,
                    cover=cover_for_ffmpeg,
                    probe_cache=probe_cache,
                    faststart=True,
                    preserve_video=video,
                    **audio_filter_kwargs,
                )
                cover_embedded = cover_for_ffmpeg is not None
                filepaths = [converted_path]
            else:
                filepaths = output.convert_output(
                    filepaths,
                    output_format,
                    cancel_event=cancel_event,
                    audio_bitrate=options.audio_bitrate,
                    probe_cache=probe_cache,
                    preserve_video=video,
                    max_workers=max_workers,
                    **audio_filter_kwargs,
                )
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        # If single-file split was expected after silence detection yielded <= 1 chapter, promote cleanly to file
        if wants_split and may_silence_chapters and not splitting and stage_dir is not None and len(filepaths) == 1:
            final_single_path = f"{output_dir}.{output_format}"
            os.makedirs(os.path.dirname(os.path.abspath(final_single_path)) or os.getcwd(), exist_ok=True)
            old_single_path = filepaths[0]
            os.replace(old_single_path, final_single_path)
            output.move_cached_probe(probe_cache, old_single_path, final_single_path)
            filepaths = [final_single_path]
            is_single_file = True
        # Add metadata & commit staged directory atomically
        if len(filepaths) == 1 and not splitting:
            if already_embedded_staged:
                write_file_sidecars(
                    audiobook,
                    filepaths[0],
                    options,
                    probe_cache=probe_cache,
                )
            else:
                add_metadata_to_file(
                    audiobook,
                    filepaths[0],
                    options,
                    cancel_event=cancel_event,
                    chapters_embedded=chapters_embedded,
                    cover_embedded=cover_embedded,
                    probe_cache=probe_cache,
                )
        else:
            add_metadata_to_dir(
                audiobook,
                filepaths,
                work_output_dir,
                options,
                probe_cache=probe_cache,
            )

        if stage_dir is not None and not is_single_file and os.path.isdir(stage_dir):
            _atomic_replace_dir(stage_dir, output_dir)
            stage_dir = None
    finally:
        if staged_combined_path and os.path.exists(staged_combined_path):
            try:
                os.remove(staged_combined_path)
            except OSError:
                pass
        if stage_dir is not None and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)


_AUDIO_MEDIA_EXTENSIONS = frozenset({
    ".m4b",
    ".m4a",
    ".mp4",
    ".mkv",
    ".mov",
    ".m4v",
    ".mp3",
    ".opus",
    ".ogg",
    ".flac",
    ".aac",
    ".wav",
    ".wma",
})


def _resolve_total_duration_seconds(
    paths: Sequence[str],
    probe_cache: dict[str, Any] | None = None,
) -> float | None:
    """Return total duration in seconds across non-ebook audio `paths`, or `None`."""
    total_sec = 0.0
    for path in paths:
        if is_ebook_extension(os.path.splitext(path)[1]):
            continue
        cached = output.get_cached_probe(probe_cache, path)
        if cached is not None:
            dur = getattr(cached, "duration", 0.0)
            if isinstance(dur, (int, float)) and dur > 0:
                total_sec += float(dur)
                continue
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            dur_ms = resolve_audio_duration_ms(path, MutagenFile)
            if dur_ms > 0:
                total_sec += dur_ms / 1000.0
    return total_sec if total_sec > 0 else None


def _resolve_abs_sidecar_path(filepath: str, metadata_obj: AudiobookMetadata) -> str:
    """
    Resolve the Audiobookshelf metadata sidecar path for a single-file output.

    Uses `metadata.json` inside dedicated book folders, and avoids clobbering
    another book's `metadata.json` when multiple books share a flat directory.
    """
    parent_dir = os.path.dirname(os.path.abspath(filepath))
    stem = os.path.splitext(os.path.basename(filepath))[0]
    default_path = os.path.join(parent_dir, "metadata.json")
    shared_conflict = False
    try:
        for entry_name in os.listdir(parent_dir):
            entry_stem, entry_ext = os.path.splitext(entry_name)
            if (
                entry_ext.lower() in _AUDIO_MEDIA_EXTENSIONS
                and entry_stem.lower() != stem.lower()
                and os.path.isfile(os.path.join(parent_dir, entry_name))
            ):
                shared_conflict = True
                break
    except OSError:
        shared_conflict = False
    if not shared_conflict and os.path.isfile(default_path):
        import json

        try:
            with open(default_path, encoding="utf-8") as existing_f:
                existing_payload = json.load(existing_f)
            existing_title = str(existing_payload.get("title") or "").strip().lower()
            current_title = (metadata_obj.title or "").strip().lower()
            if existing_title and current_title and existing_title != current_title:
                shared_conflict = True
        except (OSError, ValueError):
            pass
    if shared_conflict:
        logging.warning(
            f"Warning: Shared output directory detected at '{parent_dir}'; "
            f"writing '{stem}.metadata.json' instead of overwriting 'metadata.json'. "
            "Use per-book folders (e.g. the 'Audiobookshelf' naming preset)."
        )
        return os.path.join(parent_dir, f"{stem}.metadata.json")
    return default_path


def embed_metadata_to_file(
    audiobook: Audiobook,
    filepath: str,
    options: DownloadOptions,
    *,
    cancel_event: threading.Event | None = None,
    chapters_embedded: bool = False,
    cover_embedded: bool = False,
) -> None:
    """Embed chapters, tags, and cover art directly into a media file (safe on staged files)."""
    ebook_file = is_ebook_extension(os.path.splitext(filepath)[1])
    needs_chapters = (
        not ebook_file
        and bool(audiobook.chapters)
        and not options.no_chapters
        and not chapters_embedded
    )
    needs_cover = not ebook_file and audiobook.cover is not None and not cover_embedded
    cover_written_with_other = False
    if id3.is_id3_file(filepath) and not ebook_file:
        logging.book_update("Adding metadata")
        cover_written_with_other = metadata.add_metadata(
            filepath,
            audiobook.metadata,
            cover=audiobook.cover if needs_cover else None,
            chapters=audiobook.chapters if needs_chapters else None,
        )
    else:
        # Chapters must be muxed before container metadata/cover because ffmpeg's remux drops unmapped MP4 atoms
        if needs_chapters:
            logging.book_update("Adding chapters")
            extra_chapter_kwargs: dict[str, Any] = {}
            opt_sample_rate = getattr(options, "sample_rate", None)
            if opt_sample_rate is not None:
                extra_chapter_kwargs["sample_rate"] = opt_sample_rate
            cover_written_with_other = metadata.add_chapters(
                filepath,
                audiobook.chapters,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
                **extra_chapter_kwargs,
            )
        # General metadata (and inline cover for MP4/Vorbis in a single Mutagen save)
        if not ebook_file:
            logging.book_update("Adding metadata")
            cover_for_meta = audiobook.cover if (needs_cover and not cover_written_with_other) else None
            extra_meta_kwargs: dict[str, Any] = {"is_video": True} if is_video(audiobook) else {}
            cover_written_with_other = cover_written_with_other or metadata.add_metadata(
                filepath,
                audiobook.metadata,
                cover=cover_for_meta,
                **extra_meta_kwargs,
            )
    # Fallback cover write if container/mock did not embed it alongside metadata or chapters
    if needs_cover and not cover_written_with_other and audiobook.cover is not None:
        logging.book_update("Embedding cover")
        metadata.embed_cover(filepath, audiobook.cover)


def write_file_sidecars(
    audiobook: Audiobook,
    filepath: str,
    options: DownloadOptions,
    *,
    probe_cache: dict[str, Any] | None = None,
) -> None:
    """Write cover, JSON, Audiobookshelf, OPF, and CUE sidecars beside the committed `filepath`."""
    ebook_file = is_ebook_extension(os.path.splitext(filepath)[1])
    stem = os.path.splitext(filepath)[0]
    cover_sidecar_name: str | None = None
    if audiobook.cover and options.write_cover:
        cover_path = f"{stem}.{audiobook.cover.extension}"
        sidecars.write_sidecar_cover(audiobook.cover, cover_path)
        cover_sidecar_name = os.path.basename(cover_path)
    if options.write_json_metadata:
        with open(f"{filepath}.json", "w", encoding="utf-8") as f:
            f.write(audiobook.metadata.as_json(chapters=audiobook.chapters))
    if getattr(options, "write_abs_metadata", False):
        abs_sidecar_path = _resolve_abs_sidecar_path(filepath, audiobook.metadata)
        duration_sec = (
            None
            if ebook_file
            else _resolve_total_duration_seconds([filepath], probe_cache=probe_cache)
        )
        sidecars.write_sidecar_abs_metadata(
            audiobook.metadata,
            abs_sidecar_path,
            chapters=audiobook.chapters,
            duration_seconds=duration_sec,
        )
    if options.write_opf:
        sidecars.write_sidecar_opf(
            audiobook.metadata,
            f"{stem}.opf",
            cover_filename=cover_sidecar_name,
        )
    if not ebook_file and options.write_cue and audiobook.chapters and not options.no_chapters:
        sidecars.write_sidecar_cue(
            audiobook.metadata,
            os.path.basename(filepath),
            audiobook.chapters,
            f"{stem}.cue",
        )


def add_metadata_to_file(
    audiobook: Audiobook,
    filepath: str,
    options: DownloadOptions,
    cancel_event: threading.Event | None = None,
    chapters_embedded: bool = False,
    cover_embedded: bool = False,
    probe_cache: dict[str, Any] | None = None,
    *,
    embed_tags: bool = True,
    write_sidecars: bool = True,
):
    """
    Embed metadata into a single file and optionally write metadata sidecars.

    :param audiobook: Audiobook object. Stores metadata
    :param filepath: Filepath of output file
    :param options: Runtime download options
    """
    if embed_tags:
        embed_metadata_to_file(
            audiobook,
            filepath,
            options,
            cancel_event=cancel_event,
            chapters_embedded=chapters_embedded,
            cover_embedded=cover_embedded,
        )
    if write_sidecars:
        write_file_sidecars(
            audiobook,
            filepath,
            options,
            probe_cache=probe_cache,
        )


def _resolve_dir_track_context(
    audiobook: Audiobook,
    filepath: str,
    track_number: int,
    total_tracks: int,
    options: DownloadOptions,
) -> TrackTagContext:
    """Resolve per-file title and track numbering for multi-file/split directory outputs."""
    track_title = ""
    is_split_output = bool(getattr(options, "split_chapters", False)) and len(audiobook.chapters) > 1
    if (is_split_output or len(audiobook.files) != total_tracks) and track_number - 1 < len(audiobook.chapters):
        track_title = (audiobook.chapters[track_number - 1].title or "").strip()
    elif track_number - 1 < len(audiobook.files):
        track_title = (audiobook.files[track_number - 1].title or "").strip()
    if not track_title:
        stem = os.path.splitext(os.path.basename(filepath))[0]
        cleaned = re.sub(r"^\s*\d+\s*[-._]+\s*", "", stem).strip()
        track_title = cleaned or stem or audiobook.metadata.title
    return TrackTagContext(
        title=track_title,
        track_number=track_number,
        total_tracks=total_tracks,
        album=audiobook.metadata.title or track_title,
    )


def add_metadata_to_dir(
    audiobook: Audiobook,
    filepaths: Iterable[str],
    output_dir: str,
    options: DownloadOptions,
    probe_cache: dict[str, Any] | None = None,
):
    """
    Add metadata to a directory with audio files

    :param audiobook: Audiobook object. Stores metadata
    :param filepaths: Iterable over filepaths of output files
    :param output_dir: Directory where files are stored
    :param options: Runtime download options
    """
    logging.book_update("Adding metadata")
    file_list = list(filepaths)
    audio_files = [
        fp for fp in file_list if not is_ebook_extension(os.path.splitext(fp)[1])
    ]
    audio_total = len(audio_files)
    extra_meta_kwargs: dict[str, Any] = {"is_video": True} if is_video(audiobook) else {}
    for idx, filepath in enumerate(audio_files, start=1):
        track_ctx = _resolve_dir_track_context(
            audiobook, filepath, idx, audio_total, options
        )
        embedded_cover = metadata.add_metadata(
            filepath,
            audiobook.metadata,
            cover=audiobook.cover,
            track_context=track_ctx,
            **extra_meta_kwargs,
        )
        if audiobook.cover is not None and not embedded_cover:
            metadata.embed_cover(filepath, audiobook.cover)
    cover_sidecar_name: str | None = None
    if audiobook.cover and options.write_cover:
        logging.book_update("Adding cover")
        cover_sidecar_name = f"cover.{audiobook.cover.extension}"
        cover_path = os.path.join(output_dir, cover_sidecar_name)
        sidecars.write_sidecar_cover(audiobook.cover, cover_path)
    write_abs = bool(getattr(options, "write_abs_metadata", False))
    if options.write_json_metadata:
        # Reserve `metadata.json` for Audiobookshelf when both sidecar formats are enabled.
        json_filename = "audiobook.json" if write_abs else "metadata.json"
        metadata_file_path = os.path.join(output_dir, json_filename)
        with open(metadata_file_path, "w", encoding="utf-8") as f:
            f.write(audiobook.metadata.as_json(chapters=audiobook.chapters))
    if write_abs:
        duration_sec = _resolve_total_duration_seconds(file_list, probe_cache=probe_cache)
        sidecars.write_sidecar_abs_metadata(
            audiobook.metadata,
            os.path.join(output_dir, "metadata.json"),
            chapters=audiobook.chapters,
            duration_seconds=duration_sec,
        )
    if options.write_opf:
        sidecars.write_sidecar_opf(
            audiobook.metadata,
            os.path.join(output_dir, "metadata.opf"),
            cover_filename=cover_sidecar_name,
        )
    if options.write_cue and audiobook.chapters and not options.no_chapters and file_list:
        sidecars.write_sidecar_cue(
            audiobook.metadata,
            os.path.basename(file_list[0]),
            audiobook.chapters,
            os.path.join(output_dir, "chapters.cue"),
        )


@dataclass(frozen=True)
class DownloadSizeEstimate:
    """Estimated total transfer size across an `Audiobook`'s files."""

    estimated_bytes: int | None
    known_bytes: int
    known_files: int
    total_files: int
    is_exact: bool
    per_file_bytes: tuple[int | None, ...]


def _known_file_byte_size(file: Any) -> int | None:
    raw_size = getattr(file, "size", None)
    if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0:
        return raw_size
    headers = getattr(file, "headers", None)
    if headers:
        parsed_range = parse_byte_range_header(get_header_ci(headers, "Range"))
        if parsed_range is not None:
            start, end = parsed_range
            return (end - start) + 1
    return None


def _probe_remote_file_size(
    audiobook: Audiobook,
    index: int,
    *,
    timeout: float,
    cancel_event: threading.Event | None,
) -> int | None:
    if cancel_event is not None and cancel_event.is_set():
        return None
    file = audiobook.files[index]
    if file.file_writer is not None or file.url_resolver is not None or not file.url:
        return None
    req_headers = dict(file.headers) if file.headers else {}
    head_fn = getattr(audiobook.session, "head", None)
    if not callable(head_fn):
        return None
    resp = None
    try:
        resp = head_fn(
            file.url,
            headers=req_headers,
            allow_redirects=True,
            timeout=timeout,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (405, 501) and not any(str(k).lower() == "range" for k in req_headers):
            get_fn = getattr(audiobook.session, "get", None)
            if callable(get_fn):
                close_fn = getattr(resp, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
                range_headers = dict(req_headers)
                range_headers["Range"] = "bytes=0-0"
                resp = get_fn(
                    file.url,
                    headers=range_headers,
                    allow_redirects=True,
                    stream=True,
                    timeout=timeout,
                )
                status = int(getattr(resp, "status_code", 0) or 0)
        if status not in (200, 206):
            return None
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        _, _, range_total = _parse_content_range(get_header_ci(headers, "Content-Range"))
        if range_total is not None and range_total > 0:
            return range_total
        if status == 200:
            content_len = _parse_content_length(headers)
            return content_len if content_len > 0 else None
        return None
    except Exception:
        return None
    finally:
        close_fn = getattr(resp, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass


def estimate_download_size(
    audiobook: Audiobook,
    *,
    probe_remote: bool = False,
    timeout: float = 5.0,
    max_probe_files: int = 10,
    cancel_event: threading.Event | None = None,
) -> DownloadSizeEstimate:
    """Estimate expected download bytes from file metadata, `Range` headers, and optional `HEAD` probes."""
    files = list(getattr(audiobook, "files", ()) or ())
    total_files = len(files)
    if total_files == 0:
        return DownloadSizeEstimate(
            estimated_bytes=0,
            known_bytes=0,
            known_files=0,
            total_files=0,
            is_exact=True,
            per_file_bytes=(),
        )

    per_file: list[int | None] = [_known_file_byte_size(f) for f in files]
    if probe_remote:
        unknown_indices = [
            idx
            for idx, sz in enumerate(per_file)
            if sz is None
            and files[idx].file_writer is None
            and files[idx].url_resolver is None
            and bool(files[idx].url)
        ]
        if unknown_indices and max_probe_files > 0:
            if len(unknown_indices) <= max_probe_files:
                probe_indices = unknown_indices
            else:
                step = (len(unknown_indices) - 1) / max(1, max_probe_files - 1)
                probe_indices = [unknown_indices[round(i * step)] for i in range(max_probe_files)]
            if len(probe_indices) == 1:
                idx = probe_indices[0]
                per_file[idx] = _probe_remote_file_size(
                    audiobook,
                    idx,
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
            else:
                probed = run_cancellable_pool(
                    lambda idx: _probe_remote_file_size(
                        audiobook,
                        idx,
                        timeout=timeout,
                        cancel_event=cancel_event,
                    ),
                    probe_indices,
                    max_workers=min(6, len(probe_indices)),
                    cancel_event=cancel_event,
                )
                for idx, sz in zip(probe_indices, probed, strict=True):
                    per_file[idx] = sz

    known_sizes = [sz for sz in per_file if sz is not None and sz > 0]
    known_files = len(known_sizes)
    known_bytes = sum(known_sizes)
    if known_files == 0:
        return DownloadSizeEstimate(
            estimated_bytes=None,
            known_bytes=0,
            known_files=0,
            total_files=total_files,
            is_exact=False,
            per_file_bytes=tuple(per_file),
        )
    is_exact = known_files == total_files
    estimated_bytes = (
        known_bytes if is_exact else round((known_bytes / known_files) * total_files)
    )
    return DownloadSizeEstimate(
        estimated_bytes=estimated_bytes,
        known_bytes=known_bytes,
        known_files=known_files,
        total_files=total_files,
        is_exact=is_exact,
        per_file_bytes=tuple(per_file),
    )


def download_files_with_progress(
    audiobook: Audiobook,
    output_dir: str,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    resume: bool = False,
    confirm_overwrite: bool = True,
    overwrite: bool | None = None,
    max_workers: int | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    limit_rate: str | None = None,
    probe_cache: dict[str, Any] | None = None,
) -> list[str]:
    """
    Download `audiobook` tracks with active job progress reporting

    :param audiobook: Audiobook to download
    :param output_dir: Output directory where files are downloaded to
    :param resume: Reuse completed parts and resume partial `.tmp` files via HTTP Range
    :param confirm_overwrite: Prompt before replacing existing output targets
    :param overwrite: Non-interactive overwrite policy (`True` yes, `False` no, `None` prompt)
    :param max_workers: Optional cap on concurrent download threads
    :param timeout: Optional HTTP read timeout in seconds
    :returns: A list of paths of the downloaded files
    """
    if len(audiobook.files) > 1:
        if resume:
            if os.path.isdir(output_dir):
                logging.book_update("Resuming in existing output dir")
            else:
                logging.book_update("Creating output dir")
                os.makedirs(output_dir, exist_ok=True)
        elif confirm_overwrite:
            setup_download_dir(output_dir, interaction=interaction, overwrite=overwrite)
        else:
            logging.book_update("Creating output dir")
            os.makedirs(output_dir, exist_ok=True)
    else:
        if confirm_overwrite and len(audiobook.files) == 1:
            filepath, _ = create_filepath(audiobook, output_dir, 0)
            if not (resume and os.path.exists(filepath) and os.path.getsize(filepath) > 0):
                confirm_overwrite_file(filepath, interaction=interaction, overwrite=overwrite)
        parent = Path(output_dir).parent
        if not parent.exists():
            os.makedirs(parent, exist_ok=True)
    try:
        size_estimate = estimate_download_size(audiobook, probe_remote=False)
        if size_estimate.estimated_bytes is not None and size_estimate.estimated_bytes > 0:
            prefix = "" if size_estimate.is_exact else "~"
            logging.book_update(
                f"Estimated download size: {prefix}{format_bytes(size_estimate.estimated_bytes)} "
                f"({size_estimate.total_files} file{'s' if size_estimate.total_files != 1 else ''})"
            )
        with logging.progress() as progress:
            task = progress.add_task(
                f"Downloading {audiobook.title}",
                total = len(audiobook.files)
            )
            set_estimates_fn = getattr(progress, "set_file_size_estimates", None)
            if callable(set_estimates_fn):
                set_estimates_fn(task, size_estimate.per_file_bytes)
            update_progress = partial(progress.advance, task)
            record_bytes_fn = getattr(progress, "record_bytes", None)
            record_bytes: Callable[[int], None] | None = None
            if callable(record_bytes_fn):

                def _record_bytes(num_bytes: int, _fn: Any = record_bytes_fn) -> None:
                    _fn(task, num_bytes)

                record_bytes = _record_bytes
            report_size_fn = getattr(progress, "report_file_size", None)
            report_file_size: Callable[[int, int], None] | None = None
            if callable(report_size_fn):

                def _report_file_size(
                    file_index: int, total_bytes: int, _fn: Any = report_size_fn
                ) -> None:
                    _fn(task, file_index, total_bytes)

                report_file_size = _report_file_size
            filepaths = download_files(
                audiobook,
                output_dir,
                update_progress,
                cancel_event=cancel_event,
                record_bytes=record_bytes,
                report_file_size=report_file_size,
                resume=resume,
                max_workers=max_workers,
                timeout=timeout,
                max_retries=max_retries,
                limit_rate=limit_rate,
                probe_cache=probe_cache,
            )
            # Make sure progress bar is at 100%
            remaining_progress: float = progress.tasks[0].remaining or 0
            update_progress(remaining_progress)
            # Return filenames of downloaded files
            return filepaths
    except (KeyboardInterrupt, DownloadCancelled):
        logging.book_update("Stopped download")
        if resume:
            logging.book_update("Keeping partial files for resume")
        else:
            logging.book_update("Cleaning up files")
            if len(audiobook.files) == 1:
                _, filepath_tmp = create_filepath(audiobook, output_dir, 0)
                if os.path.exists(filepath_tmp):
                    os.remove(filepath_tmp)
            elif os.path.isdir(output_dir):
                for idx in range(len(audiobook.files)):
                    part_path, part_tmp = create_filepath(audiobook, output_dir, idx)
                    for candidate in (part_tmp, part_path):
                        if os.path.isfile(candidate):
                            try:
                                os.remove(candidate)
                            except OSError:
                                pass
                try:
                    if not os.listdir(output_dir):
                        os.rmdir(output_dir)
                except OSError:
                    pass
        raise


def create_filepath(audiobook: Audiobook, output_dir: str, index: int) -> tuple[str, str]:
    """
    Create output file path for file number `index` in `audibook`

    :param audiobook: Currently downloading audiobook
    :param output_dir: Directory where file should be stored
    :param index: Index in audiobooks list of files
    :returns: Filepath, Filepath_tmp
    """
    extension = audiobook.files[index].ext.lstrip(".").lower()
    if len(audiobook.files) == 1:
        path = f"{output_dir}.{extension}"
    else:
        padded_index = str(index).zfill(int(log10(len(audiobook.files))) + 1)
        name = f"Part {padded_index}.{extension}"
        path = os.path.join(output_dir, name)
    path_tmp = f"{path}.tmp"
    return path, path_tmp


def _parse_content_length(headers: Any) -> int:
    """Parse Content-Length header safely, tolerating identical proxy duplicates."""
    raw_length = get_header_ci(headers, "Content-Length")
    if isinstance(raw_length, int) and not isinstance(raw_length, bool):
        return raw_length if raw_length > 0 else 0
    if isinstance(raw_length, str):
        parts = [part.strip() for part in raw_length.split(",") if part.strip()]
        if not parts:
            return 0
        if all(part.isdigit() for part in parts):
            unique_vals = {int(part) for part in parts}
            if len(unique_vals) == 1:
                val = next(iter(unique_vals))
                return val if val > 0 else 0
    return 0


_CONTENT_RANGE_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_CONTENT_RANGE_TOTAL_RE = re.compile(r"^bytes\s+\*/(\d+)$", re.IGNORECASE)


def _parse_content_range(
    header_val: Any | None,
) -> tuple[int | None, int | None, int | None]:
    """Parse HTTP `Content-Range` into `(start, end, total)` byte offsets."""
    if not isinstance(header_val, str):
        return None, None, None
    text = header_val.strip()
    match = _CONTENT_RANGE_RANGE_RE.fullmatch(text)
    if match:
        start = int(match.group(1))
        end = int(match.group(2))
        total_raw = match.group(3)
        total = int(total_raw) if total_raw != "*" else None
        return start, end, total
    total_match = _CONTENT_RANGE_TOTAL_RE.fullmatch(text)
    if total_match:
        return None, None, int(total_match.group(1))
    return None, None, None


PROGRESS_FLUSH_BYTES = 256 * 1024
PROGRESS_FLUSH_INTERVAL = 0.05


def _expected_http_digests(headers: Any) -> dict[str, str]:
    """Extract supported HTTP integrity digests (`Content-MD5`, `Repr-Digest`, `Digest`)."""
    digests = parse_repr_digest(get_header_ci(headers, "Repr-Digest"))
    legacy_digest = parse_repr_digest(get_header_ci(headers, "Digest"))
    for algo, val in legacy_digest.items():
        digests.setdefault(algo, val)
    content_md5 = parse_content_md5(get_header_ci(headers, "Content-MD5"))
    if content_md5:
        digests.setdefault("md5", content_md5)
    return digests


def _integrity_failure_heading(filepath: str, ext: str) -> str:
    kind = "ebook" if is_ebook_extension(ext) else "media"
    return f"Downloaded file '{os.path.basename(filepath)}' failed {kind} integrity verification"


def _commit_verified_download(
    *,
    file: Any,
    filepath: str,
    filepath_tmp: str,
    inline_dec_tmp: str | None,
    decrypted_inline: bool,
    ext: str,
    video: bool,
    expected_checksums: Mapping[str, str] | None,
) -> Any:
    """Decrypt (if needed), validate file integrity/checksums, and atomically commit `filepath`."""
    verified_info: Any = None
    if file.encryption_method and not decrypted_inline:
        dec_tmp = f"{filepath}.dec.{uuid.uuid4().hex[:8]}.tmp"
        try:
            encryption.decrypt_file(
                filepath_tmp,
                file.encryption_method,
                output_path=dec_tmp,
                prefix=file.init_section or b"",
            )
            try:
                verified_info = output.verify_downloaded_file_integrity(
                    dec_tmp,
                    ext=ext,
                    allow_video=video,
                    expected_checksums=expected_checksums or None,
                )
            except FailedCombining:
                _safe_remove(filepath_tmp)
                raise
            os.replace(dec_tmp, filepath)
        finally:
            _safe_remove(dec_tmp)
        _safe_remove(filepath_tmp)
    elif inline_dec_tmp is not None:
        try:
            verified_info = output.verify_downloaded_file_integrity(
                inline_dec_tmp,
                ext=ext,
                allow_video=video,
                expected_checksums=expected_checksums or None,
            )
            os.replace(inline_dec_tmp, filepath)
            _safe_remove(filepath_tmp)
        finally:
            _safe_remove(inline_dec_tmp)
    else:
        try:
            verified_info = output.verify_downloaded_file_integrity(
                filepath_tmp,
                ext=ext,
                allow_video=video,
                expected_checksums=expected_checksums or None,
            )
        except FailedCombining:
            _safe_remove(filepath_tmp)
            raise
        os.replace(filepath_tmp, filepath)
    return verified_info


def download_file(
    args: tuple[Audiobook, str, int, Any, threading.Event | None] | tuple[Audiobook, str, int, Any, threading.Event | None, Callable[[int], None] | None],
    *,
    resume: bool = False,
    timeout: float | None = None,
    max_retries: int | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
    probe_cache: dict[str, Any] | None = None,
    report_file_size: Callable[[int, int], None] | None = None,
) -> str:
    # Prepare download
    if len(args) == 6:
        audiobook, output_dir, index, update_progress, cancel_event, record_bytes = args
    else:
        audiobook, output_dir, index, update_progress, cancel_event = args
        record_bytes = None
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    file = audiobook.files[index]
    filepath, filepath_tmp = create_filepath(audiobook, output_dir, index)
    ext = file.ext.lstrip(".").lower()
    video = is_video(audiobook)
    expected_checksums = normalize_checksums(getattr(file, "checksum", None))

    if resume and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        try:
            existing_info = output.verify_downloaded_file_integrity(
                filepath,
                ext=ext,
                allow_video=video,
                probe_cache=probe_cache,
                expected_checksums=expected_checksums or None,
            )
            if existing_info is not None:
                output.store_cached_probe(probe_cache, filepath, existing_info)
            logging.debug(f"Resuming: skipping already downloaded file {filepath}")
            update_progress(1.0)
            return filepath
        except FailedCombining as exc:
            logging.warning(
                f"Warning: Existing file '{os.path.basename(filepath)}' "
                f"failed integrity check ({exc}); re-downloading."
            )
    if file.url_resolver is not None:
        file.url = file.url_resolver()
    if file.encryption_method is None and file.encryption_resolver is not None:
        file.encryption_method = file.encryption_resolver()
    logging.debug(f"Starting downloading file: {file.url}")
    # Retry transient network failures (e.g. a dropped TLS connection) so one
    # flaky segment does not crash the whole batch via the thread pool.
    effective_retries = (
        max(0, int(max_retries))
        if max_retries is not None and not isinstance(max_retries, bool)
        else DEFAULT_DOWNLOAD_RETRIES
    )
    max_attempts = 1 + effective_retries
    attempt = 0
    reset_stale_tmp = False
    allow_ciphertext_range = file.encryption_method is None or len(audiobook.files) == 1
    verified_info: Any = None
    if file.file_writer is not None:
        logging.wait_if_paused(cancel_event=cancel_event)
        try:
            file.file_writer(filepath_tmp)
            verified_info = _commit_verified_download(
                file=file,
                filepath=filepath,
                filepath_tmp=filepath_tmp,
                inline_dec_tmp=None,
                decrypted_inline=False,
                ext=ext,
                video=video,
                expected_checksums=expected_checksums,
            )
        except FailedCombining as exc:
            _safe_remove(filepath_tmp)
            raise GenericAudiobookDLException(_integrity_failure_heading(filepath, ext), str(exc)) from exc
        except BaseException:
            _safe_remove(filepath_tmp)
            raise
        if verified_info is not None:
            output.store_cached_probe(probe_cache, filepath, verified_info)
        update_progress(1.0)
        return filepath
    while attempt < max_attempts:
        logging.wait_if_paused(cancel_event=cancel_event)
        if attempt > 0 and file.url_resolver is not None:
            file.url = file.url_resolver()
        advanced = 0.0
        request = None
        decrypted_inline = False
        inline_dec_tmp = cast(str | None, None)
        request_headers = dict(file.headers) if file.headers else {}
        has_custom_range = any(k.lower() == "range" for k in request_headers)
        existing_bytes = 0
        sent_range = False
        if (
            resume
            and allow_ciphertext_range
            and not has_custom_range
            and file.init_section is None
            and os.path.exists(filepath_tmp)
        ):
            existing_bytes = os.path.getsize(filepath_tmp)
            if existing_bytes > 0:
                request_headers["Range"] = f"bytes={existing_bytes}-"
                sent_range = True
        request_timeout = (
            (DEFAULT_HTTP_TIMEOUT[0], float(timeout))
            if timeout is not None
            else DEFAULT_HTTP_TIMEOUT
        )
        try:
            request = audiobook.session.get(
                file.url,
                headers=request_headers,
                stream=True,
                timeout=request_timeout,
            )
            if sent_range and request.status_code == 416:
                _, _, total_from_range = _parse_content_range(
                    get_header_ci(request.headers, "Content-Range")
                )
                if (
                    total_from_range is not None
                    and total_from_range == existing_bytes
                    and existing_bytes > 0
                ):
                    logging.debug(f"Resumed file already complete in tmp: {filepath_tmp}")
                    update_progress(1.0)
                    advanced = 1.0
                    verified_info = _commit_verified_download(
                        file=file,
                        filepath=filepath,
                        filepath_tmp=filepath_tmp,
                        inline_dec_tmp=None,
                        decrypted_inline=False,
                        ext=ext,
                        video=video,
                        expected_checksums=expected_checksums,
                    )
                    break
                _safe_remove(filepath_tmp)
                if not reset_stale_tmp:
                    reset_stale_tmp = True
                    continue

            content_type: str | None = get_header_ci(request.headers, "Content-Type")
            ct_ok, _ = is_content_type_compatible(
                content_type,
                file.expected_content_type,
                file_ext=file.ext,
            )
            invalid_content_type = not ct_ok
            if (sent_range or has_custom_range) and file.expected_status_code == 200:
                invalid_status_code = request.status_code not in (200, 206)
            else:
                invalid_status_code = bool(
                    file.expected_status_code and file.expected_status_code != request.status_code
                )

            if invalid_status_code or invalid_content_type:
                # Fail fast on interactive Cloudflare/WAF challenges instead of burning retry budget.
                cf_mitigated = get_header_ci(request.headers, "cf-mitigated")
                server_hdr = get_header_ci(request.headers, "server")
                has_cf_marker = (
                    (isinstance(cf_mitigated, str) and cf_mitigated.strip().lower() == "challenge")
                    or (isinstance(server_hdr, str) and "cloudflare" in server_hdr.lower())
                    or isinstance(get_header_ci(request.headers, "cf-ray"), str)
                )
                if has_cf_marker and is_cloudflare_challenge(
                    request,
                    body=(
                        request.content[:4096]
                        if isinstance(getattr(request, "content", None), (bytes, bytearray))
                        else None
                    ),
                ):
                    raise CloudflareBlocked(
                        detail=(
                            f"HTTP {request.status_code} Cloudflare challenge from {file.url}. "
                            "Try --impersonate chrome or fresh browser cookies."
                        )
                    )

            retryable_status = (
                (
                    invalid_status_code
                    and (
                        request.status_code in (408, 425, 429)
                        or 500 <= request.status_code < 600
                        or (file.url_resolver is not None and request.status_code in (401, 403, 410))
                    )
                    and request.status_code != file.expected_status_code
                )
                or (
                    invalid_content_type
                    and not invalid_status_code
                    and file.url_resolver is not None
                )
            )
            if retryable_status and attempt + 1 < max_attempts:
                delay = compute_retry_delay(attempt, headers=request.headers)
                logging.log(
                    f"Retrying {os.path.basename(filepath)} "
                    f"(HTTP {request.status_code}, attempt {attempt + 2}/{max_attempts}, waiting {delay:.1f}s)"
                )
                logging.debug(
                    f"Download attempt {attempt + 1}/{max_attempts} received HTTP "
                    f"{request.status_code} for {file.url}; retrying in {delay:.2f}s"
                )
                sleep_or_cancel(delay, cancel_event, sleep_fn=time.sleep)
                attempt += 1
                continue
            if invalid_content_type or invalid_status_code:
                # Failed-download bodies can be raw audio bytes: truncate before logging
                raw_content = getattr(request, "content", b"")
                raw_slice = bytes(raw_content[:4096]) if isinstance(raw_content, (bytes, bytearray)) else b""
                if is_cloudflare_challenge(request, body=raw_slice):
                    raise CloudflareBlocked(
                        detail=(
                            f"HTTP {request.status_code} Cloudflare challenge from {file.url}. "
                            "Try --impersonate chrome or fresh browser cookies."
                        )
                    )
                raw_body = raw_slice[:512]
                try:
                    body_text = raw_body.decode("utf-8")
                except UnicodeDecodeError:
                    body_text = repr(raw_body)
                raise DownloadError(
                    status_code=request.status_code,
                    content_type=content_type,
                    expected_status_code=file.expected_status_code,
                    expected_content_type=file.expected_content_type,
                    body = body_text,
                    url = file.url
                )

            open_mode = "wb"
            total_filesize = _parse_content_length(request.headers)
            custom_range_bounds = (
                parse_byte_range_header(get_header_ci(request_headers, "Range"))
                if has_custom_range
                else None
            )
            slicing_200_range = False
            slice_start = 0
            slice_end_excl = 0
            if (
                custom_range_bounds is not None
                and request.status_code == 200
                and _parse_content_range(get_header_ci(request.headers, "Content-Range"))
                == (None, None, None)
            ):
                # CDN ignored Range on a custom byte-range request (e.g. HLS BYTERANGE); slice the stream.
                slice_start, range_end = custom_range_bounds
                slice_end_excl = range_end + 1
                slicing_200_range = True
                total_filesize = slice_end_excl - slice_start

            if sent_range and request.status_code == 206:
                range_start, _, range_total = _parse_content_range(
                    get_header_ci(request.headers, "Content-Range")
                )
                if range_start is not None and range_start != existing_bytes:
                    if range_start == 0:
                        open_mode = "wb"
                        if range_total is not None and range_total > 0:
                            total_filesize = range_total
                    else:
                        _safe_remove(filepath_tmp)
                        if not reset_stale_tmp:
                            reset_stale_tmp = True
                            continue
                        raise DownloadError(
                            status_code=request.status_code,
                            content_type=content_type,
                            expected_status_code=file.expected_status_code,
                            expected_content_type=file.expected_content_type,
                            body=f"Unexpected Content-Range start {range_start} (expected {existing_bytes})",
                            url=file.url,
                        )
                else:
                    open_mode = "ab"
                    if range_total is not None and range_total > 0:
                        total_filesize = range_total
                    elif total_filesize > 0:
                        total_filesize = existing_bytes + total_filesize
                    if total_filesize > 0:
                        initial_progress = min(1.0, existing_bytes / total_filesize)
                        update_progress(initial_progress)
                        advanced += initial_progress
            if report_file_size is not None and total_filesize > 0:
                report_file_size(index, total_filesize)

            content_encoding = get_header_ci(request.headers, "Content-Encoding")
            is_identity = not (
                isinstance(content_encoding, str)
                and content_encoding.strip()
                and content_encoding.strip().lower() != "identity"
            )
            expected_http_digests = (
                _expected_http_digests(request.headers)
                if is_identity and request.status_code == 200 and open_mode == "wb" and not slicing_200_range
                else {}
            )
            http_hashers = {algo: hashlib.new(algo) for algo in expected_http_digests}

            # Stream decryption inline into an isolated temp path when reading
            # from byte 0 so `filepath_tmp` only ever contains raw un-decrypted bytes.
            stream_decryptor: StreamDecryptor | None = None
            raw_accept_ranges = get_header_ci(request.headers, "Accept-Ranges")
            accept_ranges = raw_accept_ranges.strip().lower() if isinstance(raw_accept_ranges, str) else ""
            can_inline_decrypt = (
                not resume
                or len(audiobook.files) > 1
                or accept_ranges == "none"
            )
            if (
                can_inline_decrypt
                and open_mode == "wb"
                and file.encryption_method is not None
            ):
                stream_decryptor = encryption.create_stream_decryptor(file.encryption_method)
                if stream_decryptor is not None:
                    decrypted_inline = True
                    inline_dec_tmp = f"{filepath}.inline.{uuid.uuid4().hex[:8]}.tmp"

            # Download file to tmp file
            write_target = inline_dec_tmp if inline_dec_tmp is not None else filepath_tmp
            bytes_written = 0
            pending_bytes = 0
            last_flush = time.monotonic()

            def _flush_progress(file_total_size: int = total_filesize) -> None:
                nonlocal pending_bytes, last_flush, advanced
                if pending_bytes <= 0:
                    return
                if record_bytes is not None:
                    record_bytes(pending_bytes)
                if file_total_size > 0:
                    delta = pending_bytes / file_total_size
                    update_progress(delta)
                    advanced += delta
                pending_bytes = 0
                last_flush = time.monotonic()

            try:
                stream_offset = 0
                with open(write_target, open_mode) as f:
                    if (
                        file.init_section
                        and open_mode == "wb"
                        and (stream_decryptor is not None or not file.encryption_method)
                    ):
                        f.write(file.init_section)
                    for chunk in request.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        logging.wait_if_paused(cancel_event=cancel_event)
                        if not chunk:
                            continue
                        chunk_len = len(chunk)
                        if slicing_200_range:
                            chunk_start = stream_offset
                            stream_offset += chunk_len
                            take_start = max(0, slice_start - chunk_start)
                            take_end = min(chunk_len, slice_end_excl - chunk_start)
                            if take_end <= take_start:
                                if stream_offset >= slice_end_excl:
                                    break
                                continue
                            if take_start > 0 or take_end < chunk_len:
                                chunk = chunk[take_start:take_end]
                                chunk_len = len(chunk)
                        if rate_limiter is not None:
                            rate_limiter.acquire(chunk_len, cancel_event=cancel_event)
                        for hasher in http_hashers.values():
                            hasher.update(chunk)
                        if stream_decryptor is not None:
                            out_chunk = stream_decryptor.decrypt_chunk(chunk)
                            if out_chunk:
                                f.write(out_chunk)
                        else:
                            f.write(chunk)
                        bytes_written += chunk_len
                        pending_bytes += chunk_len
                        if (
                            0 < total_filesize <= PROGRESS_FLUSH_BYTES
                            or pending_bytes >= PROGRESS_FLUSH_BYTES
                            or (time.monotonic() - last_flush) >= PROGRESS_FLUSH_INTERVAL
                        ):
                            _flush_progress()
                        if slicing_200_range and stream_offset >= slice_end_excl:
                            break
                    if stream_decryptor is not None:
                        tail = stream_decryptor.finalize()
                        if tail:
                            f.write(tail)
            finally:
                _flush_progress()
            final_size = (existing_bytes + bytes_written) if open_mode == "ab" else bytes_written
            if (
                request.status_code == 200
                and open_mode == "wb"
                and bytes_written == 0
                and not file.init_section
            ):
                raise requests.exceptions.ChunkedEncodingError(
                    f"Empty 200 OK response from {file.url}"
                )
            if is_identity and total_filesize > 0 and final_size != total_filesize:
                if open_mode == "wb":
                    _safe_remove(write_target)
                raise requests.exceptions.ChunkedEncodingError(
                    f"Incomplete download for {file.url}: "
                    f"received {final_size} of {total_filesize} bytes"
                )
            for algo, expected_hex in expected_http_digests.items():
                actual_hex = http_hashers[algo].hexdigest().lower()
                if actual_hex != expected_hex:
                    if open_mode == "wb":
                        _safe_remove(write_target)
                    raise requests.exceptions.ChunkedEncodingError(
                        f"HTTP {algo.upper()} digest mismatch for {file.url}: "
                        f"expected {expected_hex}, got {actual_hex}"
                    )
            if total_filesize == 0:
                update_progress(1.0)
                advanced = 1.0
            verified_info = _commit_verified_download(
                file=file,
                filepath=filepath,
                filepath_tmp=filepath_tmp,
                inline_dec_tmp=inline_dec_tmp,
                decrypted_inline=decrypted_inline,
                ext=ext,
                video=video,
                expected_checksums=expected_checksums,
            )
            break
        except DownloadCancelled:
            _safe_remove(inline_dec_tmp)
            if not resume:
                _safe_remove(filepath_tmp)
            raise
        except FailedCombining as error:
            _safe_remove(inline_dec_tmp)
            can_retry_integrity = (
                (file.url_resolver is not None or bool(expected_checksums))
                and attempt + 1 < max_attempts
            )
            if not can_retry_integrity:
                raise GenericAudiobookDLException(
                    _integrity_failure_heading(filepath, ext),
                    str(error),
                ) from error
            update_progress(-advanced)
            delay = compute_retry_delay(attempt)
            logging.log(
                f"Retrying {os.path.basename(filepath)} "
                f"(attempt {attempt + 2}/{max_attempts}, waiting {delay:.1f}s): {error}"
            )
            logging.debug(
                f"Download attempt {attempt + 1}/{max_attempts} failed integrity check for "
                f"{file.url} ({error}); retrying in {delay:.2f}s"
            )
            sleep_or_cancel(delay, cancel_event, sleep_fn=time.sleep)
            attempt += 1
        except Exception as error:
            _safe_remove(inline_dec_tmp)
            if not (
                isinstance(error, requests.exceptions.RequestException)
                or is_transient_network_error(error)
            ):
                raise
            # Undo this attempt's progress before retrying so the bar stays accurate
            update_progress(-advanced)
            if attempt + 1 >= max_attempts:
                raise
            delay = compute_retry_delay(attempt)
            logging.log(
                f"Retrying {os.path.basename(filepath)} "
                f"(attempt {attempt + 2}/{max_attempts}, waiting {delay:.1f}s): {error}"
            )
            logging.debug(
                f"Download attempt {attempt + 1}/{max_attempts} failed for "
                f"{file.url} ({error}); retrying in {delay:.2f}s"
            )
            sleep_or_cancel(delay, cancel_event, sleep_fn=time.sleep)
            attempt += 1
        finally:
            if request is not None:
                request.close()
    if verified_info is not None:
        output.store_cached_probe(probe_cache, filepath, verified_info)
    # Return filepath
    return filepath


def download_files(
    audiobook: Audiobook,
    output_dir: str,
    update_progress,
    cancel_event: threading.Event | None = None,
    record_bytes: Callable[[int], None] | None = None,
    report_file_size: Callable[[int, int], None] | None = None,
    resume: bool = False,
    max_workers: int | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    limit_rate: str | None = None,
    probe_cache: dict[str, Any] | None = None,
) -> list[str]:
    """Download files from audiobook and return paths of the downloaded files"""
    file_count = len(audiobook.files)
    if file_count == 0:
        return []
    if record_bytes is not None:
        arguments: list[Any] = [
            (audiobook, output_dir, index, update_progress, cancel_event, record_bytes)
            for index in range(file_count)
        ]
    else:
        arguments = [
            (audiobook, output_dir, index, update_progress, cancel_event)
            for index in range(file_count)
        ]
    worker_kwargs: dict[str, Any] = {}
    if resume:
        worker_kwargs["resume"] = True
    if timeout is not None:
        worker_kwargs["timeout"] = timeout
    if max_retries is not None:
        worker_kwargs["max_retries"] = max_retries
    if probe_cache is not None:
        worker_kwargs["probe_cache"] = probe_cache
    if report_file_size is not None:
        worker_kwargs["report_file_size"] = report_file_size
    rate_bps = parse_byte_rate(limit_rate)
    if rate_bps is not None:
        worker_kwargs["rate_limiter"] = TokenBucketRateLimiter(rate_bps)
    worker_fn: Callable[[Any], str] = partial(download_file, **worker_kwargs) if worker_kwargs else download_file
    workers = (
        min(MAX_DOWNLOAD_WORKERS, max(1, max_workers))
        if max_workers
        else min(DEFAULT_DOWNLOAD_WORKERS, MAX_DOWNLOAD_WORKERS)
    )
    if file_count == 1:
        return [worker_fn(arguments[0])]
    return run_cancellable_pool(
        worker_fn,
        arguments,
        max_workers=workers,
        cancel_event=cancel_event,
    )


def get_output_audio_format(option: str | None, files: Sequence[str]) -> tuple[str, str]:
    """
    Get output format for files

    `option` is used if specified; else it's based on the file extensions
    :param option: User specified value
    :param files: Audio file names
    :returns: A tuple with current format and output format
    """
    current_format = os.path.splitext(files[0])[1][1:].lower()
    if option:
        output_format = option.lstrip(".").lower()
    else:
        output_format = current_format
    return current_format, output_format


def confirm_overwrite_file(
    path: str,
    interaction: logging.InteractionHandler | None = None,
    overwrite: bool | None = None,
) -> None:
    """Prompt before overwriting an existing output file; raise `DownloadCancelled` if declined."""
    if os.path.exists(path):
        if overwrite is True:
            return
        if overwrite is False:
            raise DownloadCancelled
        handler = interaction or logging.NonInteractiveInteractionHandler()
        answer = handler.confirm(
            f"The file '{path}' already exists. Do you want to override it?"
        )
        if not answer:
            raise DownloadCancelled


def _confirm_overwrite_dir(
    path: str,
    interaction: logging.InteractionHandler | None = None,
    overwrite: bool | None = None,
) -> None:
    """Confirm before replacing an existing output directory without deleting it prematurely."""
    if not os.path.isdir(path):
        return
    if overwrite is True:
        answer = True
    elif overwrite is False:
        answer = False
    else:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        answer = handler.confirm(
            f"The folder '{path}' already exists. Do you want to override it?"
        )
    if not answer:
        raise DownloadCancelled


def _atomic_replace_dir(stage_dir: str, target_dir: str) -> None:
    """Replace `target_dir` with `stage_dir`, rolling back if an error occurs."""
    target_abs = os.path.abspath(target_dir)
    if not os.path.exists(target_abs):
        os.replace(stage_dir, target_abs)
        return
    parent = os.path.dirname(target_abs) or os.getcwd()
    backup_dir = os.path.join(
        parent,
        f".{os.path.basename(target_abs)}.old-{uuid.uuid4().hex[:8]}",
    )
    os.replace(target_abs, backup_dir)
    try:
        os.replace(stage_dir, target_abs)
    except Exception:
        if os.path.isdir(backup_dir) and not os.path.exists(target_abs):
            try:
                os.replace(backup_dir, target_abs)
            except OSError:
                pass
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)


def setup_download_dir(
    path: str,
    interaction: logging.InteractionHandler | None = None,
    overwrite: bool | None = None,
) -> None:
    """
    Creates output folder for the audiobook.
    Will give a prompt if the folder already exists.

    :param path: Path of output folder
    :param interaction: Optional handler for confirmation prompts
    :param overwrite: Non-interactive overwrite policy (`True` yes, `False` no, `None` prompt)
    :returns: Nothing
    """
    logging.book_update("Creating output dir")
    if os.path.isdir(path):
        _confirm_overwrite_dir(path, interaction=interaction, overwrite=overwrite)
        shutil.rmtree(path)
    os.makedirs(path)

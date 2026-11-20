from audiobookdl import logging, Audiobook, Cover
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
)
from audiobookdl.output.metadata import id3
from audiobookdl.output.metadata._chapters import (
    apply_intro_chapter_titles,
    resolve_audio_duration_ms,
)
from audiobookdl.output.metadata._cover import (
    apply_cover_postprocessing,
    normalize_cover_image,
)
from audiobookdl.utils import normalize_audio_bitrate, normalize_sample_rate
from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    TrackTagContext,
    is_ebook,
    is_ebook_extension,
    is_video,
    normalize_checksums,
    resolve_audiobook_output_format,
)
from mutagen import File as MutagenFile
from audiobookdl.utils.fs import atomic_write_text, replace_with_retry
from . import metadata as metadata
from . import output as output
from . import encryption as encryption
from .metadata import sidecars as sidecars

from dataclasses import dataclass, field
import os
import re
import shutil
import tempfile
import threading
import time as time
import uuid
from typing import Any
from collections.abc import Iterable, Sequence

from audiobookdl.output.staging import (
    _safe_remove as _safe_remove,
    _all_output_parts_exist as _all_output_parts_exist,
    create_filepath as create_filepath,
    _integrity_failure_heading as _integrity_failure_heading,
    _commit_verified_download as _commit_verified_download,
    confirm_overwrite_file as confirm_overwrite_file,
    _confirm_overwrite_dir as _confirm_overwrite_dir,
    _atomic_replace_dir as _atomic_replace_dir,
    setup_download_dir as setup_download_dir,
)

from audiobookdl.output.http_download import (
    DEFAULT_DOWNLOAD_RETRIES as DEFAULT_DOWNLOAD_RETRIES,
    DOWNLOAD_ATTEMPTS as DOWNLOAD_ATTEMPTS,
    DOWNLOAD_CHUNK_SIZE as DOWNLOAD_CHUNK_SIZE,
    DEFAULT_DOWNLOAD_WORKERS as DEFAULT_DOWNLOAD_WORKERS,
    MAX_DOWNLOAD_WORKERS as MAX_DOWNLOAD_WORKERS,
    DownloadSizeEstimate as DownloadSizeEstimate,
    _known_file_byte_size as _known_file_byte_size,
    _probe_remote_file_size as _probe_remote_file_size,
    estimate_download_size as estimate_download_size,
    download_files_with_progress as download_files_with_progress,
    _parse_content_length as _parse_content_length,
    _CONTENT_RANGE_RANGE_RE as _CONTENT_RANGE_RANGE_RE,
    _CONTENT_RANGE_TOTAL_RE as _CONTENT_RANGE_TOTAL_RE,
    _parse_content_range as _parse_content_range,
    PROGRESS_FLUSH_BYTES as PROGRESS_FLUSH_BYTES,
    PROGRESS_FLUSH_INTERVAL as PROGRESS_FLUSH_INTERVAL,
    _expected_http_digests as _expected_http_digests,
    download_file as download_file,
    download_files as download_files,
)

OUTCOME_DOWNLOADED = "downloaded"
OUTCOME_SKIPPED = "skipped"


@dataclass
class DownloadOutcome:
    """Structured result of a single-book download attempt."""

    status: str = OUTCOME_DOWNLOADED
    output_paths: list[str] = field(default_factory=list)
    bytes_written: int = 0
    audio_format: str | None = None
    output_dir: str = ""


def download(
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    *,
    chapters_already_overridden: bool = False,
) -> DownloadOutcome:
    """
    Download contents of audiobook

    :param audiobook: Audiobook to download
    :param options: Runtime download options
    :param interaction: Optional handler for confirmation prompts
    :param cancel_event: Optional event checked to cancel download
    :param chapters_already_overridden: Whether upstream enrichment already replaced chapters
    :returns: `DownloadOutcome` describing whether files were downloaded or skipped
    """
    try:
        output_dir = output.gen_output_location(
            options.output_template,
            audiobook.metadata,
            options.remove_chars
        )
        return download_audiobook(
            audiobook,
            output_dir,
            options,
            interaction=interaction,
            cancel_event=cancel_event,
            chapters_already_overridden=chapters_already_overridden,
        )
    except KeyboardInterrupt:
        raise DownloadCancelled from None



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
) -> DownloadOutcome:
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
                    return DownloadOutcome(
                        status=OUTCOME_SKIPPED,
                        output_paths=[target_path] if target_path else [],
                        audio_format=output_format or None,
                        output_dir=output_dir,
                    )
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
                return DownloadOutcome(
                    status=OUTCOME_SKIPPED,
                    audio_format=resolve_audiobook_output_format(audiobook, options.output_format),
                    output_dir=output_dir,
                )
        elif os.path.isdir(output_dir):  # multiple files, skip_downloaded without resume
            logging.log(f"Skipping {audiobook.title}, directory already exists.")
            return DownloadOutcome(
                status=OUTCOME_SKIPPED,
                audio_format=resolve_audiobook_output_format(audiobook, options.output_format),
                output_dir=output_dir,
            )
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
                replace_with_retry(staged_combined_path, output_path)
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
            replace_with_retry(old_single_path, final_single_path)
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
            staged_root = stage_dir
            _atomic_replace_dir(stage_dir, output_dir)
            stage_dir = None
            filepaths = [
                os.path.join(output_dir, os.path.relpath(path, staged_root))
                if os.path.abspath(path).startswith(os.path.abspath(staged_root) + os.sep)
                else path
                for path in filepaths
            ]
    finally:
        if staged_combined_path and os.path.exists(staged_combined_path):
            try:
                os.remove(staged_combined_path)
            except OSError:
                pass
        if stage_dir is not None and os.path.isdir(stage_dir):
            shutil.rmtree(stage_dir, ignore_errors=True)

    output_paths = [p for p in filepaths if os.path.isfile(p)]
    return DownloadOutcome(
        status=OUTCOME_DOWNLOADED,
        output_paths=output_paths,
        bytes_written=sum(os.path.getsize(p) for p in output_paths),
        audio_format=output_format or None,
        output_dir=output_dir,
    )



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
        atomic_write_text(
            f"{filepath}.json",
            audiobook.metadata.as_json(chapters=audiobook.chapters),
            encoding="utf-8",
        )
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
    # Normalize the cover once per book so per-track tagging doesn't decode and
    # re-encode the same image for every file.
    if audiobook.cover is not None:
        try:
            cover_data, cover_ext = normalize_cover_image(audiobook.cover)
            if cover_data != audiobook.cover.image or cover_ext != audiobook.cover.extension:
                audiobook.cover = Cover(cover_data, cover_ext)
        except Exception as exc:
            logging.debug(f"Cover normalization skipped: {exc}")
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
        atomic_write_text(
            metadata_file_path,
            audiobook.metadata.as_json(chapters=audiobook.chapters),
            encoding="utf-8",
        )
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



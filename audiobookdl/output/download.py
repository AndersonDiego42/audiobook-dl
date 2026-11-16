from audiobookdl import logging, Audiobook
from audiobookdl.args import DownloadOptions
from audiobookdl.exceptions import (
    DownloadError,
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
)
from audiobookdl.output.encryption import AESStreamDecryptor
from audiobookdl.output.metadata import id3
from audiobookdl.output.metadata._chapters import resolve_audio_duration_ms
from audiobookdl.output.metadata._cover import apply_cover_postprocessing
from audiobookdl.utils.audiobook import (
    AESEncryption,
    AudiobookMetadata,
    TrackTagContext,
    is_ebook,
    is_ebook_extension,
)
from mutagen import File as MutagenFile
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    TokenBucketRateLimiter,
    parse_byte_rate,
)
from . import metadata, output, encryption
from .metadata import sidecars

import email.utils
import os
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
import requests
from functools import partial
from typing import Any
from collections.abc import Callable, Iterable, Sequence
from rich.progress import BarColumn, ProgressColumn, SpinnerColumn, TimeRemainingColumn
from pathlib import Path
from math import log10


DOWNLOAD_PROGRESS: list[str | ProgressColumn] = [
    SpinnerColumn(),
    "{task.description}",
    BarColumn(),
    "[progress.percentage]{task.percentage:>3.0f}%",
    TimeRemainingColumn(),
]

# Per-file download attempts before giving up (retries transient network/TLS errors and 429/5xx)
DOWNLOAD_ATTEMPTS = 5
# 64 KiB read chunks balance throughput with prompt cancel_event checks
DOWNLOAD_CHUNK_SIZE = 64 * 1024
# Conservative default parallel workers per book to reduce CDN 429/anti-abuse throttling
DEFAULT_DOWNLOAD_WORKERS = 6
# Upper bound on parallel file download workers (matched to Session pool_maxsize)
MAX_DOWNLOAD_WORKERS = 20
# Cap exponential backoff and server-supplied Retry-After sleeps
MAX_RETRY_DELAY = 60.0


def download(
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
):
    """
    Download contents of audiobook

    :param audiobook: Audiobook to download
    :param options: Cli options
    :param interaction: Optional handler for confirmation prompts
    :param cancel_event: Optional event checked to cancel download
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
        )
    except KeyboardInterrupt:
        raise DownloadCancelled from None


def _all_output_parts_exist(
    audiobook: Audiobook,
    output_dir: str,
    output_format: str | None,
) -> bool:
    """Return True when every expected multi-part output file already exists on disk."""
    if not os.path.isdir(output_dir) or not audiobook.files:
        return False
    target_ext = output_format.lstrip(".").lower() if output_format else None
    for idx in range(len(audiobook.files)):
        raw_path, _ = create_filepath(audiobook, output_dir, idx)
        if target_ext:
            base, _ = os.path.splitext(raw_path)
            expected_path = f"{base}.{target_ext}"
        else:
            expected_path = raw_path
        if not (os.path.exists(expected_path) and os.path.getsize(expected_path) > 0):
            return False
    return True


def download_audiobook(
    audiobook: Audiobook,
    output_dir: str,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
):
    """Download, convert, combine, and add metadata to files from `Audiobook` object"""
    # Check if file/dir exists and should be skipped or confirmed before overwriting
    ebook = is_ebook(audiobook)
    wants_split = (not ebook) and bool(getattr(options, "split_chapters", False))
    may_silence_chapters = bool(
        not ebook
        and getattr(options, "chapters_from_silence", False)
        and not audiobook.chapters
        and not options.no_chapters
    )
    splitting = wants_split and len(audiobook.chapters) > 1
    expected_split_output = splitting or (wants_split and may_silence_chapters)
    is_single_file = (len(audiobook.files) == 1 or (options.combine and not ebook)) and not expected_split_output
    target_path = ""
    if audiobook.files:
        initial_ext = audiobook.files[0].ext.lstrip(".").lower()
        output_format = (
            initial_ext
            if ebook
            else (options.output_format or initial_ext).lstrip(".").lower()
        )
        target_path = f"{output_dir}.{output_format}"

    confirmed_target_overwrite = False
    if options.skip_downloaded or options.resume:
        if is_single_file:
            if target_path and os.path.exists(target_path) and (
                (options.skip_downloaded and not options.resume)
                or os.path.getsize(target_path) > 0
            ):
                msg = (
                    f"Resuming: skipping already downloaded [blue]{audiobook.title}[/], file already exists."
                    if options.resume and not options.skip_downloaded
                    else f"Skipping [blue]{audiobook.title}[/], file already exists."
                )
                logging.log(msg)
                return
        elif options.resume and not expected_split_output:
            if _all_output_parts_exist(audiobook, output_dir, options.output_format):
                msg = (
                    f"Resuming: skipping already downloaded [blue]{audiobook.title}[/], all files already exist."
                    if not options.skip_downloaded
                    else f"Skipping [blue]{audiobook.title}[/], all files already exist."
                )
                logging.log(msg)
                return
        elif os.path.isdir(output_dir):  # multiple files, skip_downloaded without resume
            logging.log(f"Skipping [blue]{audiobook.title}[/], directory already exists.")
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
        probe_cache: dict[str, Any] = {}
        # Downloading files
        filepaths = download_files_with_cli_output(
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
            limit_rate=getattr(options, "limit_rate", None),
            probe_cache=probe_cache,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        # Converting & combining files (skipped for ebook files such as .epub)
        current_format, output_format = get_output_audio_format(
            None if ebook else options.output_format,
            filepaths,
        )
        chapters_embedded = False
        cover_embedded = False
        chapter_offset_ms = int(getattr(options, "chapter_offset_ms", 0) or 0)
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
                    cancel_event=cancel_event,
                    probe_cache=probe_cache,
                )
                if detected:
                    if chapter_offset_ms != 0:
                        detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                    audiobook.chapters = detected

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
                and audiobook.cover is not None
                and output_format in output.MP4_CONTAINERS
            )
            else None
        )
        use_loudnorm = bool(getattr(options, "loudnorm", False))
        use_mono = bool(getattr(options, "mono", False))
        audio_filter_kwargs: dict[str, Any] = {}
        if use_loudnorm:
            audio_filter_kwargs["loudnorm"] = True
        if use_mono:
            audio_filter_kwargs["mono"] = True

        if splitting or (wants_split and may_silence_chapters and len(filepaths) > 1):
            if len(filepaths) > 1:
                master_ext = (
                    "m4a"
                    if current_format in ("ts", "aac") and output_format in output.MP4_CONTAINERS
                    else output_format
                )
                if master_ext == "ts":
                    master_ext = "m4a"
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
                        **audio_filter_kwargs,
                    )
                    if not audiobook.chapters and may_silence_chapters:
                        detected = output.detect_silence_chapters(
                            master_path,
                            cancel_event=cancel_event,
                            probe_cache=probe_cache,
                        )
                        if detected:
                            if chapter_offset_ms != 0:
                                detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                            audiobook.chapters = detected
                    splitting = wants_split and len(audiobook.chapters) > 1
                    if splitting:
                        split_result = output.split_audio_by_chapters(
                            master_path,
                            work_output_dir,
                            audiobook.chapters,
                            output_format=output_format,
                            cancel_event=cancel_event,
                            audio_bitrate=options.audio_bitrate,
                            probe_cache=probe_cache,
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
            try:
                chapters_embedded = bool(
                    output.combine_audiofiles(
                        filepaths,
                        work_output_dir,
                        staged_combined_path,
                        cancel_event=cancel_event,
                        chapters=chapters_for_ffmpeg,
                        audio_bitrate=options.audio_bitrate,
                        metadata=audiobook.metadata,
                        cover=combine_cover,
                        probe_cache=probe_cache,
                        faststart=True,
                        **audio_filter_kwargs,
                    )
                )
                staged_key = staged_combined_path
                os.replace(staged_combined_path, output_path)
                staged_combined_path = None
                if staged_key in probe_cache:
                    output.store_cached_probe(probe_cache, output_path, probe_cache.pop(staged_key))
                if not use_staging_dir and os.path.isdir(work_output_dir):
                    shutil.rmtree(work_output_dir, ignore_errors=True)
                cover_embedded = combine_cover is not None
                current_format = output_format
                filepaths = [output_path]
                if may_silence_chapters and not audiobook.chapters:
                    detected = output.detect_silence_chapters(
                        output_path,
                        cancel_event=cancel_event,
                        probe_cache=probe_cache,
                    )
                    if detected:
                        if chapter_offset_ms != 0:
                            detected = output.apply_chapter_offset(detected, chapter_offset_ms)
                        audiobook.chapters = detected
            except DownloadCancelled:
                if os.path.isdir(work_output_dir) and (use_staging_dir or not options.resume):
                    shutil.rmtree(work_output_dir, ignore_errors=True)
                raise
        elif (
            not options.combine
            and not ebook
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
                **audio_filter_kwargs,
            )
            current_format = output_format
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        needs_audio_reencode = use_loudnorm or use_mono
        if (current_format != output_format or needs_audio_reencode) and not splitting and not ebook:
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
                    **audio_filter_kwargs,
                )
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        # If single-file split was expected after silence detection yielded <= 1 chapter, promote cleanly to file
        if wants_split and may_silence_chapters and not splitting and stage_dir is not None and len(filepaths) == 1:
            final_single_path = f"{output_dir}.{output_format}"
            os.makedirs(os.path.dirname(os.path.abspath(final_single_path)) or os.getcwd(), exist_ok=True)
            os.replace(filepaths[0], final_single_path)
            filepaths = [final_single_path]
            is_single_file = True
        # Add metadata & commit staged directory atomically
        if len(filepaths) == 1 and not splitting:
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
        if probe_cache is not None and path in probe_cache:
            cached = probe_cache[path]
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
            current_title = str(metadata_obj.title or "").strip().lower()
            if existing_title and current_title and existing_title != current_title:
                shared_conflict = True
        except (OSError, ValueError):
            pass
    if shared_conflict:
        logging.log(
            f"[yellow]Warning:[/yellow] Shared output directory detected at '{parent_dir}'; "
            f"writing '{stem}.metadata.json' instead of overwriting 'metadata.json'. "
            "Use per-book folders (e.g. '--output-preset audiobookshelf')."
        )
        return os.path.join(parent_dir, f"{stem}.metadata.json")
    return default_path


def add_metadata_to_file(
    audiobook: Audiobook,
    filepath: str,
    options: DownloadOptions,
    cancel_event: threading.Event | None = None,
    chapters_embedded: bool = False,
    cover_embedded: bool = False,
    probe_cache: dict[str, Any] | None = None,
):
    """
    Embed metadata into a single file

    :param audiobook: Audiobook object. Stores metadata
    :param filepath: Filepath of output file
    :options: Cli options
    """
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
        cover_written_with_other = bool(
            metadata.add_metadata(
                filepath,
                audiobook.metadata,
                cover=audiobook.cover if needs_cover else None,
                chapters=audiobook.chapters if needs_chapters else None,
            )
        )
    else:
        # Chapters must be muxed before container metadata/cover because ffmpeg's remux drops unmapped MP4 atoms
        if needs_chapters:
            logging.book_update("Adding chapters")
            cover_written_with_other = bool(
                metadata.add_chapters(
                    filepath,
                    audiobook.chapters,
                    cancel_event=cancel_event,
                    audio_bitrate=options.audio_bitrate,
                )
            )
        # General metadata (and inline cover for MP4/Vorbis in a single Mutagen save)
        if not ebook_file:
            logging.book_update("Adding metadata")
            cover_for_meta = audiobook.cover if (needs_cover and not cover_written_with_other) else None
            cover_written_with_other = cover_written_with_other or bool(
                metadata.add_metadata(filepath, audiobook.metadata, cover=cover_for_meta)
            )
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
    # Fallback cover write if container/mock did not embed it alongside metadata or chapters
    if needs_cover and not cover_written_with_other and audiobook.cover is not None:
        logging.book_update("Embedding cover")
        metadata.embed_cover(filepath, audiobook.cover)


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
    :param options: Cli options
    """
    logging.book_update("Adding metadata")
    file_list = list(filepaths)
    audio_files = [
        fp for fp in file_list if not is_ebook_extension(os.path.splitext(fp)[1])
    ]
    audio_total = len(audio_files)
    for idx, filepath in enumerate(audio_files, start=1):
        track_ctx = _resolve_dir_track_context(
            audiobook, filepath, idx, audio_total, options
        )
        embedded_cover = bool(
            metadata.add_metadata(
                filepath,
                audiobook.metadata,
                cover=audiobook.cover,
                track_context=track_ctx,
            )
        )
        if audiobook.cover is not None and not embedded_cover:
            metadata.embed_cover(filepath, audiobook.cover)
    cover_sidecar_name: str | None = None
    if audiobook.cover:
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


def download_files_with_cli_output(
    audiobook: Audiobook,
    output_dir: str,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    resume: bool = False,
    confirm_overwrite: bool = True,
    overwrite: bool | None = None,
    max_workers: int | None = None,
    timeout: float | None = None,
    limit_rate: str | None = None,
    probe_cache: dict[str, Any] | None = None,
) -> list[str]:
    """
    Download `audiobook` with cli progress bar

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
        with logging.progress(DOWNLOAD_PROGRESS) as progress:
            task = progress.add_task(
                f"Downloading [blue]{audiobook.title}",
                total = len(audiobook.files)
            )
            update_progress = partial(progress.advance, task)
            record_bytes_fn = getattr(progress, "record_bytes", None)
            record_bytes = (
                partial(record_bytes_fn, task)
                if callable(record_bytes_fn)
                else None
            )
            filepaths = download_files(
                audiobook,
                output_dir,
                update_progress,
                cancel_event=cancel_event,
                record_bytes=record_bytes,
                resume=resume,
                max_workers=max_workers,
                timeout=timeout,
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
            elif os.path.exists(output_dir):
                shutil.rmtree(output_dir, ignore_errors=True)
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


def _get_header_ci(headers: Any, name: str) -> Any | None:
    """Return header value case-insensitively for both CaseInsensitiveDict and plain dicts."""
    value = headers.get(name)
    if value is not None:
        return value
    target = name.lower()
    for key, val in headers.items():
        if str(key).lower() == target:
            return val
    return None


def _parse_retry_after(value: Any | None) -> float | None:
    """Parse an HTTP `Retry-After` header (delta seconds or HTTP-date) clamped to `MAX_RETRY_DELAY`."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(MAX_RETRY_DELAY, seconds))


def _compute_retry_delay(attempt: int, retry_after: Any | None = None) -> float:
    """Compute bounded exponential backoff (with jitter) or honor `Retry-After`."""
    header_delay = _parse_retry_after(retry_after)
    if header_delay is not None:
        return header_delay
    return min(MAX_RETRY_DELAY, float(2 ** attempt) + random.uniform(0.0, 0.5))


def _sleep_or_cancel(delay: float, cancel_event: threading.Event | None) -> None:
    """Sleep for `delay` seconds while respecting `cancel_event`."""
    if delay <= 0:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise DownloadCancelled
    else:
        time.sleep(delay)


def _parse_content_length(headers: Any) -> int:
    """Parse Content-Length header safely, returning 0 when missing or invalid."""
    raw_length = _get_header_ci(headers, "Content-Length")
    if isinstance(raw_length, int):
        return raw_length if raw_length > 0 else 0
    if isinstance(raw_length, str):
        stripped = raw_length.strip()
        if stripped.isdigit():
            return int(stripped)
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


def download_file(
    args: tuple[Audiobook, str, int, Any, threading.Event | None] | tuple[Audiobook, str, int, Any, threading.Event | None, Callable[[int], None] | None],
    *,
    resume: bool = False,
    timeout: float | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
    probe_cache: dict[str, Any] | None = None,
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
    if resume and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        logging.debug(f"Resuming: skipping already downloaded file {filepath}")
        update_progress(1.0)
        return filepath
    if file.url_resolver is not None:
        file.url = file.url_resolver()
    logging.debug(f"Starting downloading file: {file.url}")
    # Retry transient network failures (e.g. a dropped TLS connection) so one
    # flaky segment does not crash the whole batch via the thread pool.
    attempt = 0
    reset_stale_tmp = False
    decrypted_inline = False
    inline_dec_tmp: str | None = None
    allow_ciphertext_range = not isinstance(file.encryption_method, AESEncryption) or len(audiobook.files) == 1
    while attempt < DOWNLOAD_ATTEMPTS:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        advanced = 0.0
        request = None
        decrypted_inline = False
        inline_dec_tmp = None
        request_headers = dict(file.headers) if file.headers else {}
        has_custom_range = any(str(k).lower() == "range" for k in request_headers)
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
                    _get_header_ci(request.headers, "Content-Range")
                )
                if (
                    total_from_range is not None
                    and total_from_range == existing_bytes
                    and existing_bytes > 0
                ):
                    logging.debug(f"Resumed file already complete in tmp: {filepath_tmp}")
                    update_progress(1.0)
                    advanced = 1.0
                    break
                if os.path.exists(filepath_tmp):
                    try:
                        os.remove(filepath_tmp)
                    except OSError:
                        pass
                if not reset_stale_tmp:
                    reset_stale_tmp = True
                    continue

            content_type: str | None = _get_header_ci(request.headers, "Content-Type")

            expected = file.expected_content_type
            if isinstance(expected, (list, tuple, set)):
                invalid_content_type = content_type not in expected
            else:
                invalid_content_type = expected and expected != content_type
            if (sent_range or has_custom_range) and file.expected_status_code == 200:
                invalid_status_code = request.status_code not in (200, 206)
            else:
                invalid_status_code = bool(
                    file.expected_status_code and file.expected_status_code != request.status_code
                )
            retryable_status = (
                invalid_status_code
                and (request.status_code == 429 or 500 <= request.status_code < 600)
                and request.status_code != file.expected_status_code
            )
            if retryable_status and attempt + 1 < DOWNLOAD_ATTEMPTS:
                delay = _compute_retry_delay(
                    attempt,
                    _get_header_ci(request.headers, "Retry-After"),
                )
                logging.debug(
                    f"Download attempt {attempt + 1}/{DOWNLOAD_ATTEMPTS} received HTTP "
                    f"{request.status_code} for {file.url}; retrying in {delay:.2f}s"
                )
                _sleep_or_cancel(delay, cancel_event)
                attempt += 1
                continue
            if invalid_content_type or invalid_status_code:
                # Failed-download bodies can be raw audio bytes: truncate + escape before rich print
                from rich.markup import escape as _escape
                raw_body = request.content[:512]
                try:
                    body_text = raw_body.decode("utf-8")
                except UnicodeDecodeError:
                    body_text = repr(raw_body)
                safe_body = _escape(body_text)
                raise DownloadError(
                    status_code=request.status_code,
                    content_type=content_type,
                    expected_status_code=file.expected_status_code,
                    expected_content_type=file.expected_content_type,
                    body = safe_body,
                    url = file.url
                )

            open_mode = "wb"
            total_filesize = _parse_content_length(request.headers)
            if sent_range and request.status_code == 206:
                range_start, _, range_total = _parse_content_range(
                    _get_header_ci(request.headers, "Content-Range")
                )
                if range_start is not None and range_start != existing_bytes:
                    if range_start == 0:
                        open_mode = "wb"
                        if range_total is not None and range_total > 0:
                            total_filesize = range_total
                    else:
                        if os.path.exists(filepath_tmp):
                            try:
                                os.remove(filepath_tmp)
                            except OSError:
                                pass
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

            # Stream AES-CBC decryption inline into an isolated temp path when reading
            # from byte 0 so `filepath_tmp` only ever contains raw un-decrypted bytes.
            stream_decryptor: AESStreamDecryptor | None = None
            accept_ranges = (_get_header_ci(request.headers, "Accept-Ranges") or "").strip().lower()
            can_inline_aes = (
                not resume
                or len(audiobook.files) > 1
                or accept_ranges == "none"
            )
            if (
                can_inline_aes
                and open_mode == "wb"
                and isinstance(file.encryption_method, AESEncryption)
            ):
                stream_decryptor = AESStreamDecryptor(
                    file.encryption_method.key,
                    file.encryption_method.iv,
                )
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
                with open(write_target, open_mode) as f:
                    if (
                        file.init_section
                        and open_mode == "wb"
                        and (stream_decryptor is not None or not file.encryption_method)
                    ):
                        f.write(file.init_section)
                    for chunk in request.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled
                        if not chunk:
                            continue
                        if stream_decryptor is not None:
                            out_chunk = stream_decryptor.decrypt_chunk(chunk)
                            if out_chunk:
                                f.write(out_chunk)
                        else:
                            f.write(chunk)
                        chunk_len = len(chunk)
                        if rate_limiter is not None:
                            rate_limiter.acquire(chunk_len, cancel_event=cancel_event)
                        bytes_written += chunk_len
                        pending_bytes += chunk_len
                        if (
                            0 < total_filesize <= PROGRESS_FLUSH_BYTES
                            or pending_bytes >= PROGRESS_FLUSH_BYTES
                            or (time.monotonic() - last_flush) >= PROGRESS_FLUSH_INTERVAL
                        ):
                            _flush_progress()
                    if stream_decryptor is not None:
                        tail = stream_decryptor.finalize()
                        if tail:
                            f.write(tail)
            finally:
                _flush_progress()
            final_size = (existing_bytes + bytes_written) if open_mode == "ab" else bytes_written
            content_encoding = _get_header_ci(request.headers, "Content-Encoding")
            is_identity = not (
                isinstance(content_encoding, str)
                and content_encoding.strip()
                and content_encoding.strip().lower() != "identity"
            )
            if is_identity and total_filesize > 0 and final_size < total_filesize:
                raise requests.exceptions.ChunkedEncodingError(
                    f"Incomplete download for {file.url}: "
                    f"received {final_size} of {total_filesize} bytes"
                )
            if total_filesize == 0:
                update_progress(1.0)
                advanced = 1.0
            break
        except requests.exceptions.RequestException as error:
            if inline_dec_tmp and os.path.exists(inline_dec_tmp):
                try:
                    os.remove(inline_dec_tmp)
                except OSError:
                    pass
            # Undo this attempt's progress before retrying so the bar stays accurate
            update_progress(-advanced)
            if attempt + 1 >= DOWNLOAD_ATTEMPTS:
                raise
            delay = _compute_retry_delay(attempt)
            logging.debug(
                f"Download attempt {attempt + 1}/{DOWNLOAD_ATTEMPTS} failed for "
                f"{file.url} ({error}); retrying in {delay:.2f}s"
            )
            _sleep_or_cancel(delay, cancel_event)
            attempt += 1
        except DownloadCancelled:
            if inline_dec_tmp and os.path.exists(inline_dec_tmp):
                try:
                    os.remove(inline_dec_tmp)
                except OSError:
                    pass
            if not resume and os.path.exists(filepath_tmp):
                try:
                    os.remove(filepath_tmp)
                except OSError:
                    pass
            raise
        except Exception:
            if inline_dec_tmp and os.path.exists(inline_dec_tmp):
                try:
                    os.remove(inline_dec_tmp)
                except OSError:
                    pass
            raise
        finally:
            if request is not None:
                request.close()
    # Decrypt file if necessary (when resume mode wrote raw ciphertext to tmp),
    # verify audio integrity before committing, and keep any existing target intact on failure.
    ext = file.ext.lstrip(".").lower()
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
            if not is_ebook_extension(ext):
                try:
                    verified_info = output.verify_audio_integrity(dec_tmp)
                except FailedCombining as exc:
                    raise GenericAudiobookDLException(
                        f"Downloaded file '{os.path.basename(filepath)}' failed media integrity verification"
                    ) from exc
            os.replace(dec_tmp, filepath)
        finally:
            if os.path.exists(dec_tmp):
                try:
                    os.remove(dec_tmp)
                except OSError:
                    pass
        try:
            os.remove(filepath_tmp)
        except OSError:
            pass
    elif inline_dec_tmp is not None:
        try:
            if not is_ebook_extension(ext):
                try:
                    verified_info = output.verify_audio_integrity(inline_dec_tmp)
                except FailedCombining as exc:
                    raise GenericAudiobookDLException(
                        f"Downloaded file '{os.path.basename(filepath)}' failed media integrity verification"
                    ) from exc
            os.replace(inline_dec_tmp, filepath)
            if os.path.exists(filepath_tmp):
                try:
                    os.remove(filepath_tmp)
                except OSError:
                    pass
        finally:
            if os.path.exists(inline_dec_tmp):
                try:
                    os.remove(inline_dec_tmp)
                except OSError:
                    pass
    else:
        if not is_ebook_extension(ext):
            try:
                verified_info = output.verify_audio_integrity(filepath_tmp)
            except FailedCombining as exc:
                if os.path.exists(filepath_tmp):
                    try:
                        os.remove(filepath_tmp)
                    except OSError:
                        pass
                raise GenericAudiobookDLException(
                    f"Downloaded file '{os.path.basename(filepath)}' failed media integrity verification"
                ) from exc
        # Atomically move into place (overwrites confirmed existing single-file outputs on Windows)
        os.replace(filepath_tmp, filepath)
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
    resume: bool = False,
    max_workers: int | None = None,
    timeout: float | None = None,
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
    if probe_cache is not None:
        worker_kwargs["probe_cache"] = probe_cache
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
        handler = interaction or logging.CliInteractionHandler()
        answer = handler.confirm(
            f"The file '[blue]{path}[/blue]' already exists. Do you want to override it?"
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
        handler = interaction or logging.CliInteractionHandler()
        answer = handler.confirm(
            f"The folder '[blue]{path}[/blue]' already exists. Do you want to override it?"
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

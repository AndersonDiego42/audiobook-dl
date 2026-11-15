from audiobookdl import AudiobookFile, Source, logging, Audiobook
from audiobookdl.args import DownloadOptions
from audiobookdl.exceptions import UserNotAuthorized, NoFilesFound, DownloadError, DownloadCancelled
from audiobookdl.output.encryption import AESStreamDecryptor
from audiobookdl.output.metadata import id3
from audiobookdl.utils.audiobook import AESEncryption, is_ebook, is_ebook_extension
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT
from . import metadata, output, encryption
from .metadata import sidecars

import os
import re
import shutil
import tempfile
import threading
import time
import requests
from functools import partial
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple, Union
from rich.progress import Progress, BarColumn, ProgressColumn, SpinnerColumn
from pathlib import Path
from math import log10


DOWNLOAD_PROGRESS: List[Union[str, ProgressColumn]] = [
    SpinnerColumn(),
    "{task.description}",
    BarColumn(),
    "[progress.percentage]{task.percentage:>3.0f}%"
]

# Per-file download attempts before giving up (retries transient network/TLS errors)
DOWNLOAD_ATTEMPTS = 5
# 64 KiB read chunks balance throughput with prompt cancel_event checks
DOWNLOAD_CHUNK_SIZE = 64 * 1024
# Upper bound on parallel file download workers (matched to Session pool_maxsize)
MAX_DOWNLOAD_WORKERS = 20


def download(
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
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
    output_format: Optional[str],
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
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """Download, convert, combine, and add metadata to files from `Audiobook` object"""
    # Check if file/dir exists and should be skipped or confirmed before overwriting
    ebook = is_ebook(audiobook)
    splitting = (not ebook) and bool(getattr(options, "split_chapters", False) and len(audiobook.chapters) > 1)
    is_single_file = (len(audiobook.files) == 1 or (options.combine and not ebook)) and not splitting
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
        elif options.resume:
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

    # Downloading files
    filepaths = download_files_with_cli_output(
        audiobook,
        output_dir,
        interaction=interaction,
        cancel_event=cancel_event,
        resume=options.resume,
        confirm_overwrite=(not is_single_file) or (len(audiobook.files) > 1 and not confirmed_target_overwrite),
        overwrite=options.overwrite,
        max_workers=options.max_workers,
        timeout=options.timeout,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    # Converting & combining files (skipped for ebook files such as .epub)
    current_format, output_format = get_output_audio_format(
        None if ebook else options.output_format,
        filepaths,
    )
    chapters_embedded = False
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
    if splitting:
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
                )
                split_result = output.split_audio_by_chapters(
                    master_path,
                    output_dir,
                    audiobook.chapters,
                    output_format=output_format,
                    cancel_event=cancel_event,
                    audio_bitrate=options.audio_bitrate,
                )
            for src_part in filepaths:
                if src_part not in split_result and os.path.exists(src_part):
                    try:
                        os.remove(src_part)
                    except OSError:
                        pass
            filepaths = split_result
        elif len(filepaths) == 1:
            src_single = filepaths[0]
            filepaths = output.split_audio_by_chapters(
                src_single,
                output_dir,
                audiobook.chapters,
                output_format=output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
            )
            if src_single not in filepaths and os.path.exists(src_single):
                os.remove(src_single)
        current_format = output_format
    elif options.combine and not ebook and len(filepaths) > 1:
        logging.book_update("Combining files")
        output_path = f"{output_dir}.{output_format}"
        try:
            chapters_embedded = bool(
                output.combine_audiofiles(
                    filepaths,
                    output_dir,
                    output_path,
                    cancel_event=cancel_event,
                    chapters=chapters_for_ffmpeg,
                    audio_bitrate=options.audio_bitrate,
                )
            )
            current_format = output_format
            filepaths = [output_path]
        except DownloadCancelled:
            if os.path.isdir(output_dir) and not options.resume:
                shutil.rmtree(output_dir, ignore_errors=True)
            raise
    elif (
        not options.combine
        and not ebook
        and len(filepaths) > 1
        and output.should_smart_merge_hls(filepaths, audiobook.chapters)
    ):
        filepaths = output.merge_hls_segments_into_chapters(
            filepaths,
            output_dir,
            audiobook.chapters,
            output_format=output_format,
            cancel_event=cancel_event,
            audio_bitrate=options.audio_bitrate,
        )
        current_format = output_format
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    if current_format != output_format and not splitting and not ebook:
        logging.book_update("Converting files")
        if len(filepaths) == 1:
            converted_path, chapters_embedded = output.convert_single_file(
                filepaths[0],
                output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
                chapters=chapters_for_ffmpeg,
            )
            filepaths = [converted_path]
        else:
            filepaths = output.convert_output(
                filepaths,
                output_format,
                cancel_event=cancel_event,
                audio_bitrate=options.audio_bitrate,
            )
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    # Add metadata
    if len(filepaths) == 1:
        add_metadata_to_file(
            audiobook,
            filepaths[0],
            options,
            cancel_event=cancel_event,
            chapters_embedded=chapters_embedded,
        )
    else:
        add_metadata_to_dir(audiobook, filepaths, output_dir, options)


def add_metadata_to_file(
    audiobook: Audiobook,
    filepath: str,
    options: DownloadOptions,
    cancel_event: Optional[threading.Event] = None,
    chapters_embedded: bool = False,
):
    """
    Embed metadata into a single file

    :param audiobook: Audiobook object. Stores metadata
    :param filepath: Filepath of output file
    :options: Cli options
    """
    ebook_file = is_ebook_extension(os.path.splitext(filepath)[1])
    # Chapters must be muxed before container metadata/cover because ffmpeg's remux drops unmapped MP4 atoms
    if not ebook_file and audiobook.chapters and not options.no_chapters and not chapters_embedded:
        logging.book_update("Adding chapters")
        metadata.add_chapters(
            filepath,
            audiobook.chapters,
            cancel_event=cancel_event,
            audio_bitrate=options.audio_bitrate,
        )
    # General metadata
    if not ebook_file:
        logging.book_update("Adding metadata")
        metadata.add_metadata(filepath, audiobook.metadata)
    stem = os.path.splitext(filepath)[0]
    cover_sidecar_name: Optional[str] = None
    if audiobook.cover and options.write_cover:
        cover_path = f"{stem}.{audiobook.cover.extension}"
        sidecars.write_sidecar_cover(audiobook.cover, cover_path)
        cover_sidecar_name = os.path.basename(cover_path)
    if options.write_json_metadata:
        with open(f"{filepath}.json", "w", encoding="utf-8") as f:
            f.write(audiobook.metadata.as_json(chapters=audiobook.chapters))
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
    # Cover
    if not ebook_file and audiobook.cover:
        logging.book_update("Embedding cover")
        metadata.embed_cover(filepath, audiobook.cover)


def add_metadata_to_dir(
    audiobook: Audiobook,
    filepaths: Iterable[str],
    output_dir: str,
    options: DownloadOptions,
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
    for filepath in file_list:
        if not is_ebook_extension(os.path.splitext(filepath)[1]):
            metadata.add_metadata(filepath, audiobook.metadata)
    cover_sidecar_name: Optional[str] = None
    if audiobook.cover:
        logging.book_update("Adding cover")
        cover_sidecar_name = f"cover.{audiobook.cover.extension}"
        cover_path = os.path.join(output_dir, cover_sidecar_name)
        sidecars.write_sidecar_cover(audiobook.cover, cover_path)
    if options.write_json_metadata:
        metadata_file_path = os.path.join(output_dir, "metadata.json")
        with open(metadata_file_path, "w", encoding="utf-8") as f:
            f.write(audiobook.metadata.as_json(chapters=audiobook.chapters))
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
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    resume: bool = False,
    confirm_overwrite: bool = True,
    overwrite: Optional[bool] = None,
    max_workers: Optional[int] = None,
    timeout: Optional[float] = None,
) -> List[str]:
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
            if os.path.isdir(output_dir):
                shutil.rmtree(output_dir)
            os.makedirs(output_dir)
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


def create_filepath(audiobook: Audiobook, output_dir: str, index: int) -> Tuple[str, str]:
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


def _get_header_ci(headers: Any, name: str) -> Optional[Any]:
    """Return header value case-insensitively for both CaseInsensitiveDict and plain dicts."""
    value = headers.get(name)
    if value is not None:
        return value
    target = name.lower()
    for key, val in headers.items():
        if str(key).lower() == target:
            return val
    return None


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
    header_val: Optional[Any],
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
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
    args: Union[
        Tuple[Audiobook, str, int, Any, Optional[threading.Event]],
        Tuple[Audiobook, str, int, Any, Optional[threading.Event], Optional[Callable[[int], None]]],
    ],
    *,
    resume: bool = False,
    timeout: Optional[float] = None,
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
    while attempt < DOWNLOAD_ATTEMPTS:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        advanced = 0.0
        request = None
        decrypted_inline = False
        request_headers = dict(file.headers) if file.headers else {}
        has_custom_range = any(str(k).lower() == "range" for k in request_headers)
        existing_bytes = 0
        sent_range = False
        if resume and not has_custom_range and os.path.exists(filepath_tmp):
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

            content_type: Optional[str] = _get_header_ci(request.headers, "Content-Type")

            expected = file.expected_content_type
            if isinstance(expected, (list, tuple, set)):
                invalid_content_type = content_type not in expected
            else:
                invalid_content_type = expected and expected != content_type
            if sent_range and file.expected_status_code == 200:
                invalid_status_code = request.status_code not in (200, 206)
            else:
                invalid_status_code = bool(
                    file.expected_status_code and file.expected_status_code != request.status_code
                )
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

            # Stream AES-CBC decryption inline when writing from byte 0 without resume
            stream_decryptor: Optional[AESStreamDecryptor] = None
            if (
                not resume
                and open_mode == "wb"
                and isinstance(file.encryption_method, AESEncryption)
            ):
                stream_decryptor = AESStreamDecryptor(
                    file.encryption_method.key,
                    file.encryption_method.iv,
                )
                decrypted_inline = True

            # Download file to tmp file
            bytes_written = 0
            pending_bytes = 0
            last_flush = time.monotonic()

            def _flush_progress() -> None:
                nonlocal pending_bytes, last_flush, advanced
                if pending_bytes <= 0:
                    return
                if record_bytes is not None:
                    record_bytes(pending_bytes)
                if total_filesize > 0:
                    delta = pending_bytes / total_filesize
                    update_progress(delta)
                    advanced += delta
                pending_bytes = 0
                last_flush = time.monotonic()

            try:
                with open(filepath_tmp, open_mode) as f:
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
            # Undo this attempt's progress before retrying so the bar stays accurate
            update_progress(-advanced)
            if attempt + 1 >= DOWNLOAD_ATTEMPTS:
                raise
            delay = 2 ** attempt
            logging.debug(
                f"Download attempt {attempt + 1}/{DOWNLOAD_ATTEMPTS} failed for "
                f"{file.url} ({error}); retrying in {delay}s"
            )
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise DownloadCancelled
            else:
                time.sleep(delay)
            attempt += 1
        except DownloadCancelled:
            if not resume and os.path.exists(filepath_tmp):
                try:
                    os.remove(filepath_tmp)
                except OSError:
                    pass
            raise
        finally:
            if request is not None:
                request.close()
    # Decrypt file if necessary (when resume mode wrote raw ciphertext to tmp),
    # keeping ciphertext in filepath_tmp intact until the decrypted target is in place.
    if file.encryption_method and not decrypted_inline:
        encryption.decrypt_file(filepath_tmp, file.encryption_method, output_path=filepath)
        try:
            os.remove(filepath_tmp)
        except OSError:
            pass
    else:
        # Atomically move into place (overwrites confirmed existing single-file outputs on Windows)
        os.replace(filepath_tmp, filepath)
    # Return filepath
    return filepath


def download_files(
    audiobook: Audiobook,
    output_dir: str,
    update_progress,
    cancel_event: Optional[threading.Event] = None,
    record_bytes: Optional[Callable[[int], None]] = None,
    resume: bool = False,
    max_workers: Optional[int] = None,
    timeout: Optional[float] = None,
) -> List[str]:
    """Download files from audiobook and return paths of the downloaded files"""
    file_count = len(audiobook.files)
    if file_count == 0:
        return []
    if record_bytes is not None:
        arguments: List[Any] = [
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
    worker_fn = partial(download_file, **worker_kwargs) if worker_kwargs else download_file
    workers = min(MAX_DOWNLOAD_WORKERS, max_workers) if max_workers else MAX_DOWNLOAD_WORKERS
    if file_count == 1:
        return [worker_fn(arguments[0])]
    return run_cancellable_pool(
        worker_fn,
        arguments,
        max_workers=workers,
        cancel_event=cancel_event,
    )


def get_output_audio_format(option: Optional[str], files: Sequence[str]) -> Tuple[str, str]:
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
    interaction: Optional[logging.InteractionHandler] = None,
    overwrite: Optional[bool] = None,
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


def setup_download_dir(
    path: str,
    interaction: Optional[logging.InteractionHandler] = None,
    overwrite: Optional[bool] = None,
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
        if overwrite is True:
            answer = True
        elif overwrite is False:
            answer = False
        else:
            handler = interaction or logging.CliInteractionHandler()
            answer = handler.confirm(
                f"The folder '[blue]{path}[/blue]' already exists. Do you want to override it?"
            )
        if answer:
            shutil.rmtree(path)
        else:
            raise DownloadCancelled
    os.makedirs(path)

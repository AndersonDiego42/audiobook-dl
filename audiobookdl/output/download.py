from audiobookdl import AudiobookFile, Source, logging, Audiobook
from audiobookdl.exceptions import UserNotAuthorized, NoFilesFound, DownloadError, DownloadCancelled
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT
from . import metadata, output, encryption

import os
import shutil
import threading
import time
import requests
from functools import partial
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union
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
    options,
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
        return


def download_audiobook(
    audiobook: Audiobook,
    output_dir: str,
    options,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """Download, convert, combine, and add metadata to files from `Audiobook` object"""
    # Check if file/dir exists and should be skipped or confirmed before overwriting
    is_single_file = len(audiobook.files) == 1 or options.combine
    raw_single_path = ""
    target_path = ""
    if audiobook.files:
        initial_ext = audiobook.files[0].ext.lstrip(".").lower()
        output_format = (options.output_format or initial_ext).lstrip(".").lower()
        target_path = f"{output_dir}.{output_format}"
        if len(audiobook.files) == 1:
            raw_single_path, _ = create_filepath(audiobook, output_dir, 0)

    if options.skip_downloaded:
        if is_single_file:
            if target_path and os.path.exists(target_path):
                logging.log(f"Skipping [blue]{audiobook.title}[/], file already exists.")
                return
        elif os.path.isdir(output_dir):  # multiple files, check for directory
            logging.log(f"Skipping [blue]{audiobook.title}[/], directory already exists.")
            return
    elif is_single_file and target_path and target_path != raw_single_path:
        confirm_overwrite_file(target_path, interaction=interaction)

    # Downloading files
    filepaths = download_files_with_cli_output(
        audiobook,
        output_dir,
        interaction=interaction,
        cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    # Converting files
    current_format, output_format = get_output_audio_format(options.output_format, filepaths)
    # Combine files
    if options.combine and len(filepaths) > 1:
        logging.book_update("Combining files")
        try:
            if output_format != current_format and output.combine_supports_direct_output(filepaths[0], output_format):
                output_path = f"{output_dir}.{output_format}"
                output.combine_audiofiles(filepaths, output_dir, output_path, cancel_event=cancel_event)
                current_format = output_format
            else:
                output_path = f"{output_dir}.{current_format}"
                output.combine_audiofiles(filepaths, output_dir, output_path, cancel_event=cancel_event)
            filepaths = [output_path]
        except DownloadCancelled:
            if os.path.isdir(output_dir):
                shutil.rmtree(output_dir, ignore_errors=True)
            raise
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    if current_format != output_format:
        logging.book_update("Converting files")
        filepaths = output.convert_output(filepaths, output_format, cancel_event=cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    # Add metadata
    if len(filepaths) == 1:
        add_metadata_to_file(audiobook, filepaths[0], options, cancel_event=cancel_event)
    else:
        add_metadata_to_dir(audiobook, filepaths, output_dir, options)


def add_metadata_to_file(
    audiobook: Audiobook,
    filepath: str,
    options,
    cancel_event: Optional[threading.Event] = None,
):
    """
    Embed metadata into a single file

    :param audiobook: Audiobook object. Stores metadata
    :param filepath: Filepath of output file
    :options: Cli options
    """
    # General metadata
    logging.book_update("Adding metadata")
    metadata.add_metadata(filepath, audiobook.metadata)
    if options.write_json_metadata:
        with open(f"{filepath}.json", "w") as f:
            f.write(audiobook.metadata.as_json())
    # Chapters
    if audiobook.chapters and not options.no_chapters:
        logging.book_update("Adding chapters")
        metadata.add_chapters(filepath, audiobook.chapters, cancel_event=cancel_event)
    # Cover
    if audiobook.cover:
        logging.book_update("Embedding cover")
        metadata.embed_cover(filepath, audiobook.cover)


def add_metadata_to_dir(audiobook: Audiobook, filepaths: Iterable[str], output_dir: str, options):
    """
    Add metadata to a directory with audio files

    :param audiobook: Audiobook object. Stores metadata
    :param filepaths: Iterable over filepaths of output files
    :param output_dir: Directory where files are stored
    :param optiosn: Cli options
    """
    logging.book_update("Adding metadata")
    for filepath in filepaths:
        metadata.add_metadata(filepath, audiobook.metadata)
    if options.write_json_metadata:
        metadata_file_path = os.path.join(output_dir, "metadata.json")
        with open(metadata_file_path, "w") as f:
            f.write(audiobook.metadata.as_json())
    if audiobook.cover:
        logging.book_update("Adding cover")
        cover_path = os.path.join(output_dir, f"cover.{audiobook.cover.extension}")
        with open(cover_path, "wb") as f:
            f.write(audiobook.cover.image)


def download_files_with_cli_output(
    audiobook: Audiobook,
    output_dir: str,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """
    Download `audiobook` with cli progress bar

    :param audiobook: Audiobook to download
    :param output_dir: Output directory where files are downloaded to
    :returns: A list of paths of the downloaded files
    """
    if len(audiobook.files) > 1:
        setup_download_dir(output_dir, interaction=interaction)
    else:
        if len(audiobook.files) == 1:
            filepath, _ = create_filepath(audiobook, output_dir, 0)
            confirm_overwrite_file(filepath, interaction=interaction)
        parent = Path(output_dir).parent
        if not parent.exists():
            os.makedirs(parent)
    try:
        with logging.progress(DOWNLOAD_PROGRESS) as progress:
            task = progress.add_task(
                f"Downloading [blue]{audiobook.title}",
                total = len(audiobook.files)
            )
            update_progress = partial(progress.advance, task)
            filepaths = download_files(
                audiobook,
                output_dir,
                update_progress,
                cancel_event=cancel_event,
            )
            # Make sure progress bar is at 100%
            remaining_progress: float = progress.tasks[0].remaining or 0
            update_progress(remaining_progress)
            # Return filenames of downloaded files
            return filepaths
    except (KeyboardInterrupt, DownloadCancelled):
        logging.book_update("Stopped download")
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


def download_file(args: Tuple[Audiobook, str, int, Any, Optional[threading.Event]]) -> str:
    # Prepare download
    audiobook, output_dir, index, update_progress, cancel_event = args
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    file = audiobook.files[index]
    filepath, filepath_tmp = create_filepath(audiobook, output_dir, index)
    logging.debug(f"Starting downloading file: {file.url}")
    # Retry transient network failures (e.g. a dropped TLS connection) so one
    # flaky segment does not crash the whole batch via the thread pool.
    for attempt in range(DOWNLOAD_ATTEMPTS):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        advanced = 0.0
        request = None
        try:
            request = audiobook.session.get(
                file.url,
                headers=file.headers,
                stream=True,
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            content_type: Optional[str] = _get_header_ci(request.headers, "Content-Type")

            expected = file.expected_content_type
            if isinstance(expected, (list, tuple, set)):
                invalid_content_type = content_type not in expected
            else:
                invalid_content_type = expected and expected != content_type
            invalid_status_code = file.expected_status_code and file.expected_status_code != request.status_code
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

            total_filesize = _parse_content_length(request.headers)

            # Download file to tmp file
            with open(filepath_tmp, "wb") as f:
                for chunk in request.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled
                    if not chunk:
                        continue
                    f.write(chunk)
                    if total_filesize > 0:
                        download_progress = len(chunk) / total_filesize
                        update_progress(download_progress)
                        advanced += download_progress
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
        except DownloadCancelled:
            if os.path.exists(filepath_tmp):
                try:
                    os.remove(filepath_tmp)
                except OSError:
                    pass
            raise
        finally:
            if request is not None:
                request.close()
    # Decrypt file if necessary
    if file.encryption_method:
        encryption.decrypt_file(filepath_tmp, file.encryption_method)
    # Atomically move into place (overwrites confirmed existing single-file outputs on Windows)
    os.replace(filepath_tmp, filepath)
    # Return filepath
    return filepath


def download_files(
    audiobook: Audiobook,
    output_dir: str,
    update_progress,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """Download files from audiobook and return paths of the downloaded files"""
    file_count = len(audiobook.files)
    if file_count == 0:
        return []
    arguments = [
        (audiobook, output_dir, index, update_progress, cancel_event)
        for index in range(file_count)
    ]
    if file_count == 1:
        return [download_file(arguments[0])]
    return run_cancellable_pool(
        download_file,
        arguments,
        max_workers=MAX_DOWNLOAD_WORKERS,
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
) -> None:
    """Prompt before overwriting an existing output file; raise `DownloadCancelled` if declined."""
    if os.path.exists(path):
        handler = interaction or logging.CliInteractionHandler()
        answer = handler.confirm(
            f"The file '[blue]{path}[/blue]' already exists. Do you want to override it?"
        )
        if not answer:
            raise DownloadCancelled


def setup_download_dir(
    path: str,
    interaction: Optional[logging.InteractionHandler] = None,
) -> None:
    """
    Creates output folder for the audiobook.
    Will give a prompt if the folder already exists.

    :param path: Path of output folder
    :param interaction: Optional handler for confirmation prompts
    :returns: Nothing
    """
    logging.book_update("Creating output dir")
    if os.path.isdir(path):
        handler = interaction or logging.CliInteractionHandler()
        answer = handler.confirm(
            f"The folder '[blue]{path}[/blue]' already exists. Do you want to override it?"
        )
        if answer:
            shutil.rmtree(path)
        else:
            raise DownloadCancelled
    os.makedirs(path)

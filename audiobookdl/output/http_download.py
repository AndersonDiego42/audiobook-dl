from audiobookdl import logging, Audiobook
from audiobookdl.exceptions import (
    CloudflareBlocked,
    DownloadError,
    DownloadCancelled,
    FailedCombining,
    GenericAudiobookDLException,
    RequestError,
)
from audiobookdl.output.encryption import StreamDecryptor
from audiobookdl.utils.audiobook import (
    format_bytes,
    is_video,
    normalize_checksums,
)
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
    read_bounded_prefix,
    sleep_or_cancel,
)
from . import output, encryption
from .staging import (
    _safe_remove,
    _integrity_failure_heading,
    _commit_verified_download,
    create_filepath,
    confirm_overwrite_file,
    setup_download_dir,
)

import hashlib
import os
import re
import threading
import time
import uuid
import requests
from functools import partial
from typing import Any, cast
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass

# Default per-file retries after the initial attempt (5 total attempts by default)
DEFAULT_DOWNLOAD_RETRIES = DEFAULT_MAX_RETRIES
DOWNLOAD_ATTEMPTS = DEFAULT_DOWNLOAD_RETRIES + 1
# 64 KiB read chunks balance throughput with prompt cancel_event checks
DOWNLOAD_CHUNK_SIZE = 64 * 1024
# Conservative default parallel workers per book to reduce CDN 429/anti-abuse throttling
DEFAULT_DOWNLOAD_WORKERS = 6
# Upper bound on parallel file download workers (matched to Session pool_maxsize)
MAX_DOWNLOAD_WORKERS = 20


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
                cancel_event=cancel_event,
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
        error_body_prefix: bytes | None = None
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
                        cancel_event=cancel_event,
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
                if has_cf_marker:
                    if error_body_prefix is None:
                        error_body_prefix = read_bounded_prefix(request, 16384)
                    if is_cloudflare_challenge(request, body=error_body_prefix):
                        raise CloudflareBlocked(
                            detail=(
                                f"HTTP {request.status_code} Cloudflare challenge from {file.url}. "
                                "Try setting Impersonate (e.g. chrome) in Options -> Network & Performance "
                                "or importing fresh browser cookies on the Accounts page."
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
                # Failed-download bodies can be raw audio bytes: truncate before logging.
                # Read a bounded prefix only — `request` is streamed and error bodies
                # are never written to disk below.
                if error_body_prefix is None:
                    error_body_prefix = read_bounded_prefix(request, 16384)
                raw_slice = error_body_prefix[:4096]
                if is_cloudflare_challenge(request, body=raw_slice):
                    try:
                        request.close()
                    except Exception:
                        pass
                    raise CloudflareBlocked(
                        detail=(
                            f"HTTP {request.status_code} Cloudflare challenge from {file.url}. "
                            "Try setting Impersonate (e.g. chrome) in Options -> Network & Performance "
                            "or importing fresh browser cookies on the Accounts page."
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
                cancel_event=cancel_event,
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
                raise RequestError(
                    detail=f"{file.url}: {error} (after {max_attempts} attempts)",
                ) from error
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



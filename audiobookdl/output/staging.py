from audiobookdl import logging, Audiobook
from audiobookdl.exceptions import (
    DownloadCancelled,
    FailedCombining,
)
from audiobookdl.utils.audiobook import is_ebook_extension, normalize_checksums
from audiobookdl.utils.fs import replace_with_retry
from . import output, encryption

import os
import shutil
import threading
import uuid
from math import log10
from typing import Any
from collections.abc import Mapping

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
    cancel_event: threading.Event | None = None,
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
                cancel_event=cancel_event,
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
            replace_with_retry(dec_tmp, filepath)
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
            replace_with_retry(inline_dec_tmp, filepath)
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
        replace_with_retry(filepath_tmp, filepath)
    return verified_info



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
        replace_with_retry(stage_dir, target_abs)
        return
    parent = os.path.dirname(target_abs) or os.getcwd()
    backup_dir = os.path.join(
        parent,
        f".{os.path.basename(target_abs)}.old-{uuid.uuid4().hex[:8]}",
    )
    replace_with_retry(target_abs, backup_dir)
    try:
        replace_with_retry(stage_dir, target_abs)
    except Exception:
        if os.path.isdir(backup_dir) and not os.path.exists(target_abs):
            try:
                replace_with_retry(backup_dir, target_abs)
            except OSError as rollback_exc:
                logging.warning(
                    f"Failed to restore '{target_abs}' from backup '{backup_dir}' "
                    f"({rollback_exc}); the previous output was preserved at the backup path."
                )
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

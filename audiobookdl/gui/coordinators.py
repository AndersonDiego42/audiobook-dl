"""Focused helper functions and coordinators extracted from `AudiobookDLApp`."""

from __future__ import annotations

import os
import re
import tkinter as tk
from typing import Any
from collections.abc import Callable, Sequence

from audiobookdl import output
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.gui.formatting import parse_url_text
from audiobookdl.pack import SUPPORTED_PACK_EXTENSIONS
from audiobookdl.sources import detect_service_key
from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata, BookId


def split_dropped_tokens(raw_data: str) -> list[str]:
    """Split TkDnD `{path with spaces} ...` or whitespace-delimited drop payloads."""
    tokens: list[str] = []
    for match in re.finditer(r"\{([^}]+)\}|(\S+)", raw_data):
        val = (match.group(1) or match.group(2) or "").strip()
        if val:
            tokens.append(val)
    return tokens


def partition_input_tokens(tokens: Sequence[str]) -> tuple[list[str], list[str]]:
    """Partition dropped paths/tokens into `(local_pack_paths, download_urls)`."""
    local_paths: list[str] = []
    url_tokens: list[str] = []
    for tok in tokens:
        cleaned = tok.strip().strip('"').strip("'")
        if not cleaned:
            continue
        if os.path.isfile(cleaned) and cleaned.lower().endswith(".txt"):
            try:
                with open(cleaned, encoding="utf-8-sig") as fh:
                    url_tokens.extend(parse_url_text(fh.read()))
            except OSError:
                pass
            continue
        if os.path.isdir(cleaned) or (
            os.path.isfile(cleaned)
            and os.path.splitext(cleaned)[1].lstrip(".").lower() in SUPPORTED_PACK_EXTENSIONS
        ):
            local_paths.append(cleaned)
        else:
            url_tokens.append(cleaned)
    return local_paths, url_tokens


def series_book_exists(
    book: BookId[Any] | Audiobook,
    *,
    options: DownloadOptions,
    dir_stems_cache: dict[str, set[str]] | None = None,
    archive_titles_cache: set[str] | None = None,
    series_title: str | None = None,
) -> bool:
    """Return True when `book` already appears on disk or in the archive ledger."""
    meta: AudiobookMetadata | None = None
    book_title = ""
    if isinstance(book, Audiobook):
        meta = book.metadata
        book_title = meta.title.strip()
    else:
        raw_title = getattr(book, "title", None)
        if not raw_title and isinstance(getattr(book, "id", None), dict):
            raw_title = book.id.get("title") or book.id.get("name")
        if raw_title:
            book_title = str(raw_title).strip()
            meta = AudiobookMetadata(title=book_title, series=series_title)
    if archive_titles_cache and book_title and book_title.casefold() in archive_titles_cache:
        return True
    if meta is None:
        return False
    try:
        template = options.output_template or "{title}"
        if not isinstance(book, Audiobook):
            if "{author" in template or "{narrator" in template:
                return False
        target = output.gen_output_location(template, meta, options.remove_chars)
        if os.path.exists(target):
            return True
        if options.output_format and os.path.exists(f"{target}.{options.output_format}"):
            return True
        parent = os.path.dirname(target) or "."
        base = os.path.basename(target)
        if dir_stems_cache is not None and parent in dir_stems_cache:
            return base in dir_stems_cache[parent]
        if os.path.isdir(parent):
            stems = {os.path.splitext(entry)[0] for entry in os.listdir(parent)}
            if dir_stems_cache is not None:
                dir_stems_cache[parent] = stems
            return base in stems
    except (OSError, ValueError, KeyError):
        return False
    return False


class AutomationCoordinator:
    """Coordinates periodic clipboard URL capture and scheduled background library syncing."""

    def __init__(
        self,
        root: tk.Misc,
        *,
        is_clipboard_enabled: Callable[[], bool],
        append_urls: Callable[[Sequence[str]], list[str]],
        save_clipboard_pref: Callable[[bool], Any],
        set_status: Callable[[str], None],
        can_run_auto_sync: Callable[[], bool],
        execute_sync_targets: Callable[[Sequence[tuple[str, str]], str], None],
        sync_libraries: Callable[[], None],
    ) -> None:
        self._root = root
        self._is_clipboard_enabled = is_clipboard_enabled
        self._append_urls = append_urls
        self._save_clipboard_pref = save_clipboard_pref
        self._set_status = set_status
        self._can_run_auto_sync = can_run_auto_sync
        self._execute_sync_targets = execute_sync_targets
        self._sync_libraries = sync_libraries

        self.clipboard_after_id: str | None = None
        self.last_seen_clipboard: str = ""
        self.auto_sync_after_id: str | None = None
        self.auto_sync_enabled: bool = False
        self.auto_sync_minutes: int = 60
        self.auto_sync_mode: str = "download_missing"
        self.auto_sync_targets: list[tuple[str, str]] = []

    def cancel_clipboard(self) -> None:
        if self.clipboard_after_id is not None:
            try:
                self._root.after_cancel(self.clipboard_after_id)
            except tk.TclError:
                pass
            self.clipboard_after_id = None

    def apply_clipboard_monitor(self, *, quiet: bool, persist: bool) -> None:
        self.cancel_clipboard()
        enabled = bool(self._is_clipboard_enabled())
        if enabled:
            try:
                self.last_seen_clipboard = str(self._root.clipboard_get() or "")
            except tk.TclError:
                self.last_seen_clipboard = ""
            if not quiet:
                self._set_status("Clipboard URL monitor enabled")
            self.clipboard_after_id = self._root.after(1200, self.poll_clipboard)
        elif not quiet:
            self._set_status("Clipboard URL monitor disabled")
        if persist:
            try:
                self._save_clipboard_pref(enabled)
            except OSError:
                pass

    def poll_clipboard(self) -> None:
        self.clipboard_after_id = None
        if not self._is_clipboard_enabled():
            return
        try:
            if not self._root.winfo_exists():
                return
            raw = str(self._root.clipboard_get() or "")
        except tk.TclError:
            raw = ""
        if raw and raw != self.last_seen_clipboard:
            self.last_seen_clipboard = raw
            candidates = [
                u for u in parse_url_text(raw)
                if u.lower().startswith(("http://", "https://")) and detect_service_key(u)
            ]
            if candidates:
                added = self._append_urls(candidates)
                if added:
                    self._set_status(f"Captured {len(added)} URL(s) from clipboard")
        try:
            if self._root.winfo_exists() and self._is_clipboard_enabled():
                self.clipboard_after_id = self._root.after(1200, self.poll_clipboard)
        except tk.TclError:
            pass

    def cancel_auto_sync(self) -> None:
        if self.auto_sync_after_id is not None:
            try:
                self._root.after_cancel(self.auto_sync_after_id)
            except tk.TclError:
                pass
            self.auto_sync_after_id = None

    def configure_auto_sync_timer(self, enabled: bool, minutes: int) -> None:
        self.auto_sync_enabled = enabled
        self.auto_sync_minutes = max(1, int(minutes))
        self.cancel_auto_sync()
        if enabled:
            delay_ms = self.auto_sync_minutes * 60 * 1000
            self.auto_sync_after_id = self._root.after(delay_ms, self.on_auto_sync_tick)

    def on_auto_sync_tick(self) -> None:
        self.auto_sync_after_id = None
        if not self.auto_sync_enabled:
            return
        if self._can_run_auto_sync():
            targets = list(self.auto_sync_targets or [])
            if targets:
                self._execute_sync_targets(targets, self.auto_sync_mode)
            else:
                self._sync_libraries()
        self.configure_auto_sync_timer(True, self.auto_sync_minutes)

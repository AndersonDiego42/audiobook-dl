"""Focused helper functions and coordinators extracted from `AudiobookDLApp`."""

from __future__ import annotations

from collections import Counter
import hashlib
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, Sequence

from audiobookdl import output
from audiobookdl.config import DEFAULT_AUTO_SYNC_MODE, VALID_AUTO_SYNC_MODES
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.gui.dnd import install_windows_file_drop
from audiobookdl.gui.formatting import parse_url_text
from audiobookdl.pack import SUPPORTED_PACK_EXTENSIONS
from audiobookdl.sources import detect_service_key
from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata, BookId

if TYPE_CHECKING:
    from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
    from audiobookdl.gui.pages import DownloadPage


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


_AUDIO_BOOK_EXTENSIONS = frozenset(
    {
        ".m4b",
        ".m4a",
        ".mp3",
        ".opus",
        ".ogg",
        ".flac",
        ".aac",
        ".wav",
        ".wma",
        ".mp4",
        ".mkv",
        ".webm",
        ".epub",
        ".pdf",
    }
)


def _norm_stem(name: str) -> str:
    return name.casefold() if os.name == "nt" else name


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
        id_dict = book.id if isinstance(getattr(book, "id", None), dict) else {}
        if not raw_title and id_dict:
            raw_title = id_dict.get("title") or id_dict.get("name")
        if raw_title:
            book_title = str(raw_title).strip()
            raw_authors = getattr(book, "authors", None) or id_dict.get("authors") or []
            if isinstance(raw_authors, str):
                authors_list = [raw_authors]
            else:
                authors_list = [str(a) for a in raw_authors if a]
            meta = AudiobookMetadata(
                title=book_title,
                authors=authors_list,
                series=series_title,
                series_order=getattr(book, "series_order", None) or id_dict.get("series_order"),
                isbn=getattr(book, "isbn", None) or id_dict.get("isbn"),
                asin=getattr(book, "asin", None) or id_dict.get("asin"),
                source=getattr(book, "source", None) or id_dict.get("source"),
            )
    if archive_titles_cache and book_title and book_title.casefold() in archive_titles_cache:
        return True
    if meta is None:
        return False
    try:
        template = options.output_template or "{title}"
        if not isinstance(book, Audiobook):
            missing_tokens = (
                ("{author", not meta.authors),
                ("{narrator", not meta.narrators),
                ("{year", meta.release_date is None),
                ("{series_order", meta.series_order is None),
                ("{isbn", not meta.isbn),
                ("{asin", not meta.asin),
                ("{source", not meta.source),
            )
            if any(tok in template and is_missing for tok, is_missing in missing_tokens):
                return False
        target = output.gen_output_location(template, meta, options.remove_chars)
        if os.path.exists(target):
            return True
        ext = (options.output_format or "").lstrip(".")
        if ext and os.path.exists(f"{target}.{ext}"):
            return True
        parent = os.path.dirname(target) or "."
        base = _norm_stem(os.path.basename(target))
        if dir_stems_cache is not None and parent in dir_stems_cache:
            return base in dir_stems_cache[parent]
        if os.path.isdir(parent):
            stems: set[str] = set()
            for entry in os.listdir(parent):
                full_entry = os.path.join(parent, entry)
                if os.path.isdir(full_entry):
                    stems.add(_norm_stem(entry))
                else:
                    stem, file_ext = os.path.splitext(entry)
                    if file_ext.casefold() in _AUDIO_BOOK_EXTENSIONS:
                        stems.add(_norm_stem(stem))
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
        self._last_clipboard_sig: tuple[int, bytes] | None = None
        self.auto_sync_after_id: str | None = None
        self.auto_sync_enabled: bool = False
        self.auto_sync_minutes: int = 60
        self._auto_sync_mode: str = DEFAULT_AUTO_SYNC_MODE
        self.auto_sync_targets: list[tuple[str, str]] = []

    @property
    def auto_sync_mode(self) -> str:
        """Return the configured auto-sync mode (`enqueue`, `scan_only`, or `dry_run`)."""
        return self._auto_sync_mode

    @auto_sync_mode.setter
    def auto_sync_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in VALID_AUTO_SYNC_MODES:
            raise ValueError(
                f"Invalid auto_sync_mode {mode!r}; expected one of {VALID_AUTO_SYNC_MODES}"
            )
        self._auto_sync_mode = normalized

    def cancel_clipboard(self) -> None:
        if self.clipboard_after_id is not None:
            try:
                self._root.after_cancel(self.clipboard_after_id)
            except tk.TclError:
                pass
            self.clipboard_after_id = None

    @staticmethod
    def _clipboard_signature(raw: str) -> tuple[int, bytes]:
        digest = hashlib.blake2b(
            raw.encode("utf-8", errors="surrogatepass"),
            digest_size=16,
        ).digest()
        return (len(raw), digest)

    def apply_clipboard_monitor(self, *, quiet: bool, persist: bool) -> None:
        self.cancel_clipboard()
        enabled = bool(self._is_clipboard_enabled())
        if enabled:
            try:
                raw = str(self._root.clipboard_get() or "")
            except tk.TclError:
                raw = ""
            self._last_clipboard_sig = self._clipboard_signature(raw) if raw else None
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
        if raw:
            sig = self._clipboard_signature(raw)
            if sig != self._last_clipboard_sig:
                self._last_clipboard_sig = sig
                if len(raw) <= 262144 and ("http://" in raw.lower() or "https://" in raw.lower()):
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
            self.is_auto_sync_running = True
            try:
                if targets:
                    self._execute_sync_targets(targets, self.auto_sync_mode)
                else:
                    self._sync_libraries()
            finally:
                self.is_auto_sync_running = False
        self.configure_auto_sync_timer(True, self.auto_sync_minutes)


class UrlInputCoordinator:
    """Manages URL text input, clipboard pasting, .txt file loading, and drag-and-drop."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        status_var: tk.StringVar,
        auth: ServiceAuthCoordinator,
        get_download_page: Callable[[], DownloadPage | None],
        ensure_download_page: Callable[[], DownloadPage],
        navigate_download: Callable[[], object],
        open_pack_with_paths: Callable[[Sequence[str]], object],
        start_download: Callable[[], None],
    ) -> None:
        self._root = root
        self._status_var = status_var
        self._auth = auth
        self._get_download_page = get_download_page
        self._ensure_download_page = ensure_download_page
        self._navigate_download = navigate_download
        self._open_pack_with_paths = open_pack_with_paths
        self._start_download = start_download
        self._url_debounce_after_id: str | None = None
        self._win_drop_installed = False

    def cancel_debounce(self) -> None:
        if self._url_debounce_after_id is not None:
            try:
                self._root.after_cancel(self._url_debounce_after_id)
            except tk.TclError:
                pass
            self._url_debounce_after_id = None

    def bind_drop_target(self, target: Any, *, stop_prop: bool) -> None:
        if not (hasattr(target, "drop_target_register") and hasattr(target, "dnd_bind")):
            return

        def _handler(ev: Any) -> str | None:
            self.on_url_drop(ev)
            return "break" if stop_prop else None

        try:
            target.drop_target_register("DND_Text", "DND_Files")
            target.dnd_bind("<<Drop>>", _handler)
        except Exception:
            try:
                target.drop_target_register("DND_Text")
                target.dnd_bind("<<Drop>>", _handler)
            except Exception:
                pass

    def install_root_drop(self) -> None:
        self.bind_drop_target(self._root, stop_prop=False)
        if hasattr(self._root, "drop_target_register") and hasattr(self._root, "dnd_bind"):
            return

        def _ensure_win_drop(_ev: tk.Event | None = None) -> None:
            if self._win_drop_installed:
                return
            try:
                if not self._root.winfo_exists() or not self._root.winfo_ismapped():
                    return
            except tk.TclError:
                return
            install_windows_file_drop(self._root, self.on_dropped_paths)
            self._win_drop_installed = bool(
                getattr(self._root, "_win_drop_installed_ok", False)
                or getattr(self._root, "_win_drop_callback", None)
            )

        if self._root.winfo_ismapped():
            _ensure_win_drop()
        else:
            self._root.bind("<Map>", _ensure_win_drop, add="+")

    def on_dropped_paths(self, tokens: Sequence[str]) -> None:
        local_paths, url_tokens = partition_input_tokens(tokens)
        if local_paths:
            self._open_pack_with_paths(local_paths)
        if url_tokens:
            self.append_urls_to_input(url_tokens)

    def on_url_drop(self, event: Any) -> None:
        dropped = getattr(event, "data", "")
        if not dropped:
            return
        tokens = split_dropped_tokens(str(dropped))
        self.on_dropped_paths(tokens)

    def append_urls_to_input(self, urls: Sequence[str]) -> list[str]:
        box = self._ensure_download_page().urls_text
        existing_text = box.get("1.0", tk.END)
        existing_set = set(parse_url_text(existing_text))
        added = [u for u in urls if u and u not in existing_set]
        if added:
            box.edit_separator()
            current_stripped = existing_text.rstrip("\r\n")
            prefix = "\n" if current_stripped else ""
            box.insert("end-1c", f"{prefix}{chr(10).join(added)}")
            try:
                box.edit_modified(False)
            except tk.TclError:
                pass
            self.cancel_debounce()
            self.on_urls_changed()
        return added

    def on_urls_modified(self, _event: tk.Event | None = None) -> None:
        urls_text = self._ensure_download_page().urls_text
        try:
            if not urls_text.edit_modified():
                return
            urls_text.edit_modified(False)
        except tk.TclError:
            return
        self.cancel_debounce()
        self._url_debounce_after_id = self._root.after(100, self.on_urls_changed)

    def on_urls_changed(self, _event: tk.Event | None = None) -> None:
        self._url_debounce_after_id = None
        dl_page = self._get_download_page()
        if dl_page is None:
            return
        raw_text = dl_page.urls_text.get("1.0", tk.END)
        urls = parse_url_text(raw_text)
        self._auth.on_urls_changed(urls)

        dl_page.urls_text.tag_remove("warning_line", "1.0", tk.END)
        counts: Counter[str] = Counter()
        valid_count = 0
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            tokens = stripped.split()
            line_valid = True
            for tok in tokens:
                is_http = tok.lower().startswith(("http://", "https://"))
                svc_key = detect_service_key(tok)
                if is_http:
                    valid_count += 1
                    label = self._auth.service_label_for_url(tok) if svc_key else "Generic"
                    counts[label] += 1
                else:
                    line_valid = False
            if not line_valid:
                dl_page.urls_text.tag_add("warning_line", f"{line_no}.0", f"{line_no}.end")

        if valid_count == 0:
            dl_page.url_frame.configure(text="Audiobook URL(s)")
        else:
            breakdown = ", ".join(f"{cnt} {lbl}" for lbl, cnt in counts.most_common(3))
            dl_page.url_frame.configure(
                text=f"Audiobook URL(s) \u2014 {valid_count} URL{'s' if valid_count != 1 else ''} ({breakdown})"
            )

    def browse_url_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self._root,
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                content = f.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("Error", f"Could not read file:\n{exc}", parent=self._root)
            return
        if content:
            self._navigate_download()
            urls = parse_url_text(content)
            if urls:
                self.append_urls_to_input(urls)
            else:
                urls_text = self._ensure_download_page().urls_text
                existing = urls_text.get("1.0", tk.END).strip()
                prefix = "\n" if existing else ""
                urls_text.insert(tk.END, f"{prefix}{content}")
                self.cancel_debounce()
                self.on_urls_changed()

    def clear_urls(self) -> None:
        urls_text = self._ensure_download_page().urls_text
        urls_text.delete("1.0", tk.END)
        self.on_urls_changed()

    def paste_urls_from_clipboard(self) -> list[str]:
        """Insert URLs from the system clipboard into the URL box or open Pack for local paths."""
        try:
            raw = self._root.clipboard_get()
        except tk.TclError:
            self._status_var.set("Clipboard is empty")
            return []
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        local_paths, url_candidates = partition_input_tokens(lines)
        pasted: list[str] = []
        for cand in url_candidates:
            for tok in parse_url_text(cand):
                if tok not in pasted:
                    pasted.append(tok)
        if local_paths:
            self._open_pack_with_paths(local_paths)
        if not pasted:
            if not local_paths:
                self._status_var.set("No URLs found in clipboard")
            return []
        added = self.append_urls_to_input(pasted)
        if added:
            self._status_var.set(f"Pasted {len(added)} URL{'s' if len(added) != 1 else ''}")
        else:
            self._status_var.set("Clipboard URL(s) already listed")
        return added

    def paste_and_queue(self) -> None:
        pasted = self.paste_urls_from_clipboard()
        urls_text = self._ensure_download_page().urls_text
        if pasted or parse_url_text(urls_text.get("1.0", tk.END)):
            self._start_download()

    def send_urls_to_input(self, urls: Sequence[str]) -> None:
        added = self.append_urls_to_input(urls)
        if added:
            self._status_var.set(
                f"Added {len(added)} URL{'s' if len(added) != 1 else ''} to input"
            )
        else:
            self._status_var.set("URL(s) already in input")

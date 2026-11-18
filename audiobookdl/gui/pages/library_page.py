"""Library page combining the Archive Ledger, Catalog Browser, and Library Sync sub-tabs."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any
from collections.abc import Callable, Sequence
import webbrowser

import requests

from audiobookdl.config import (
    DEFAULT_AUTO_SYNC_MODE,
    DEFAULT_PROFILE,
    VALID_AUTO_SYNC_MODES,
    Config,
    list_configured_source_profiles,
)
from audiobookdl.utils.http import create_configured_http_session, resolve_http_timeout
from audiobookdl.gui.formatting import (
    create_placeholder_cover_thumbnail,
    format_catalog_formats,
    format_duration,
    photo_from_image_bytes,
    resize_cover_bytes_aspect_thumbnail,
    resize_cover_file_thumbnail_bytes,
    resize_cover_thumbnail_bytes,
)
from audiobookdl.gui.theme import (
    LIGHT_PALETTE,
    ThemePalette,
    apply_theme_to_widget_tree,
    make_bold_font,
    scale_px,
)
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ModalDialog,
    ScrolledTreeview,
    TreeColumnSpec,
    populate_grouped_tree,
    resolve_selected_leaf_iids,
)
from audiobookdl.sources import ServiceInfo
from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.archive import (
    VALID_LIBRARY_STATUSES,
    ArchiveEntry,
    DownloadArchive,
    parse_tags_input,
)
from audiobookdl.utils.export import export_library_entries
from audiobookdl.utils.library_scanner import LocalScanResult, scan_local_library


class _ArchiveMetadataDialog(ModalDialog):
    """Modal editor for updating title, author, narrator, series, IDs, status, path, and tags."""

    def __init__(
        self,
        parent: tk.Misc,
        entry: ArchiveEntry,
        *,
        palette: ThemePalette,
    ) -> None:
        super().__init__(
            parent,
            f"Edit Metadata — {entry.title}",
            palette=palette,
            default_size=(540, 440),
            min_size=(480, 380),
        )
        self.result_updates: dict[str, Any] | None = None

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._title_var = tk.StringVar(value=entry.title)
        self._author_var = tk.StringVar(value=entry.author)
        self._narrator_var = tk.StringVar(value=entry.narrator)
        self._series_var = tk.StringVar(value=entry.series or "")
        self._series_order_var = tk.StringVar(
            value="" if entry.series_order is None else str(entry.series_order)
        )
        self._asin_var = tk.StringVar(value=entry.asin or "")
        self._isbn_var = tk.StringVar(value=entry.isbn or "")
        self._status_var = tk.StringVar(value=entry.status or "downloaded")
        self._output_path_var = tk.StringVar(value=entry.output_path or "")
        self._tags_var = tk.StringVar(value=" ".join(entry.tags))

        fields: list[tuple[str, tk.StringVar]] = [
            ("Title:", self._title_var),
            ("Author:", self._author_var),
            ("Narrator:", self._narrator_var),
            ("Series:", self._series_var),
            ("Series Order:", self._series_order_var),
            ("ASIN:", self._asin_var),
            ("ISBN:", self._isbn_var),
            ("Tags:", self._tags_var),
        ]
        first_entry: ttk.Entry | None = None
        row_idx = 0
        for label_text, var in fields:
            ttk.Label(body, text=label_text).grid(
                row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
            )
            widget = ttk.Entry(body, textvariable=var)
            widget.grid(row=row_idx, column=1, columnspan=2, sticky="ew", pady=3)
            if first_entry is None:
                first_entry = widget
            row_idx += 1

        ttk.Label(body, text="Status:").grid(
            row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
        )
        status_combo = ttk.Combobox(
            body,
            textvariable=self._status_var,
            values=list(VALID_LIBRARY_STATUSES),
            state="readonly",
            width=18,
        )
        status_combo.grid(row=row_idx, column=1, sticky="w", pady=3)
        row_idx += 1

        ttk.Label(body, text="Output Path:").grid(
            row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(body, textvariable=self._output_path_var).grid(
            row=row_idx, column=1, sticky="ew", pady=3
        )
        ttk.Button(body, text="Browse...", command=self._browse_output_path).grid(
            row=row_idx, column=2, padx=(6, 0), pady=3
        )
        row_idx += 1

        btns = ttk.Frame(body)
        btns.grid(row=row_idx, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._save())
        self.finalize_modal(initial_focus=first_entry)

    def _browse_output_path(self) -> None:
        current = self._output_path_var.get().strip()
        initial_dir = os.path.dirname(current) if current and os.path.dirname(current) else os.getcwd()
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Relink Audiobook File",
            initialdir=initial_dir,
        )
        if chosen:
            self._output_path_var.set(chosen)

    def _save(self) -> None:
        title = self._title_var.get().strip()
        if not title:
            messagebox.showwarning("Invalid Metadata", "Title cannot be empty.", parent=self)
            return
        raw_order = self._series_order_var.get().strip()
        series_order: int | None = None
        if raw_order:
            try:
                series_order = int(raw_order)
            except ValueError:
                messagebox.showwarning(
                    "Invalid Metadata",
                    "Series Order must be a whole number (or empty).",
                    parent=self,
                )
                return
        status = self._status_var.get().strip().lower()
        if status not in VALID_LIBRARY_STATUSES:
            messagebox.showwarning(
                "Invalid Metadata",
                f"Status must be one of: {', '.join(VALID_LIBRARY_STATUSES)}.",
                parent=self,
            )
            return
        self.result_updates = {
            "title": title,
            "author": self._author_var.get().strip(),
            "narrator": self._narrator_var.get().strip(),
            "series": self._series_var.get().strip() or None,
            "series_order": series_order,
            "asin": self._asin_var.get().strip() or None,
            "isbn": self._isbn_var.get().strip() or None,
            "output_path": self._output_path_var.get().strip() or None,
            "status": status,
            "tags": parse_tags_input(self._tags_var.get()),
        }
        self.destroy()


class ArchiveTab(ttk.Frame):
    """Tab hosting the unified library grid and SQLite download ledger."""

    _PAGE_SIZE = 1000

    _COLUMNS = (
        TreeColumnSpec("status", "Status", width=95, stretch=False),
        TreeColumnSpec("source", "Service", width=110, stretch=False),
        TreeColumnSpec("title", "Title", width=180, stretch=True),
        TreeColumnSpec("author", "Author", width=130, stretch=True),
        TreeColumnSpec("series", "Series", width=120, stretch=True),
        TreeColumnSpec("tags", "Tags", width=110, stretch=True),
        TreeColumnSpec("output_path", "Output Path", width=160, stretch=True),
        TreeColumnSpec("downloaded_at", "Date (UTC)", width=135, stretch=False, sort_kind="date"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        archive_path: str,
        *,
        on_queue_urls: Callable[[list[str]], None] | None = None,
        on_open_path: Callable[[str, bool], None] | None = None,
        on_pack_paths: Callable[[list[str]], object] | None = None,
        on_upload_abs: Callable[[ArchiveEntry], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 10))
        self._archive_path = archive_path
        self._ledger: DownloadArchive | None = None
        self._debounce_after_id: str | None = None
        self._on_queue_urls = on_queue_urls
        self._on_open_path = on_open_path
        self._on_pack_paths = on_pack_paths
        self._on_upload_abs = on_upload_abs
        self._entries_by_id: dict[int, ArchiveEntry] = {}
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)
        self._details_visible = True
        self._detail_cover_photo: object | None = None
        self._detail_token = 0
        self._detail_debounce_after_id: str | None = None
        self._detail_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="abdl-archive-detail",
        )
        self._loading_ops = 0
        self._page_offset = 0
        self._total_matches = 0
        self._load_all = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._search_var = tk.StringVar()
        self._group_var = tk.BooleanVar(value=True)
        self._show_hidden_var = tk.BooleanVar(value=False)
        self._show_trash_var = tk.BooleanVar(value=False)
        self._status_var = tk.StringVar(
            value="Supports field filters: author:, tag:, status:, series:, -tag:hidden"
        )

        top_bar = ttk.Frame(self)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(top_bar, textvariable=self._search_var, width=28)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Checkbutton(
            top_bar,
            text="Group Series",
            variable=self._group_var,
            command=self._refresh,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            top_bar,
            text="Show Hidden",
            variable=self._show_hidden_var,
            command=self._on_filter_changed,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            top_bar,
            text="View Trash",
            variable=self._show_trash_var,
            command=self._on_filter_changed,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(top_bar, text="Refresh", command=self._refresh).pack(side=tk.LEFT, padx=(0, 6))
        self._details_toggle_btn = ttk.Button(
            top_bar, text="Hide Details", command=self._toggle_details_pane
        )
        self._details_toggle_btn.pack(side=tk.LEFT)

        self._split = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self._split.grid(row=1, column=0, sticky="nsew")

        self._scrolled_tree = ScrolledTreeview(
            self._split,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
        )
        self._split.add(self._scrolled_tree, weight=3)
        self._tree = self._scrolled_tree.tree
        self._tree.bind("<<TreeviewSelect>>", self._on_entry_selected)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._show_context_menu)
        if sys.platform == "darwin":
            self._tree.bind("<Button-2>", self._show_context_menu)
        self._build_context_menu()

        self._details_pane = ttk.LabelFrame(self._split, text="Book Details", padding=scale_px(self, 8))
        self._details_pane.columnconfigure(1, weight=1)
        self._detail_cover_label = ttk.Label(self._details_pane, text="No Cover", anchor="center")
        self._detail_cover_label.grid(row=0, column=0, columnspan=2, pady=(0, 8))
        self._detail_title_var = tk.StringVar(value="Select a book")
        self._detail_meta_var = tk.StringVar(value="")
        ttk.Label(
            self._details_pane,
            textvariable=self._detail_title_var,
            font=make_bold_font(self, 10),
            wraplength=scale_px(self, 220),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(
            self._details_pane,
            textvariable=self._detail_meta_var,
            wraplength=scale_px(self, 220),
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="nw")
        self._split.add(self._details_pane, weight=1)

        self._loading_bar = ttk.Progressbar(self, mode="indeterminate")
        self._loading_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(self)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        if self._on_queue_urls is not None:
            ttk.Button(btn_bar, text="Add to URLs", command=self._queue_selected).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        ttk.Button(btn_bar, text="Edit Tags...", command=self._edit_selected_tags).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Edit Metadata...", command=self._edit_selected_metadata).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Hide / Unhide", command=self._toggle_hide_selected).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Trash / Restore", command=self._toggle_trash_selected).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Remove Selected", command=self._remove_selected).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self._scan_btn = ttk.Button(
            btn_bar, text="Locate Books...", command=self._prompt_locate_local_books
        )
        self._scan_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._verify_btn = ttk.Button(
            btn_bar, text="Verify Missing", command=self._verify_missing_files
        )
        self._verify_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_bar, text="Export...", command=self._prompt_export).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Clear All", command=self._clear_all).pack(side=tk.LEFT)

        status_row = ttk.Frame(self)
        status_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)
        ttk.Label(status_row, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._pager_frame = ttk.Frame(status_row)
        self._pager_frame.grid(row=0, column=1, sticky="e")
        self._prev_page_btn = ttk.Button(
            self._pager_frame, text="Prev", width=6, command=self._prev_page
        )
        self._prev_page_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._next_page_btn = ttk.Button(
            self._pager_frame, text="Next", width=6, command=self._next_page
        )
        self._next_page_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._load_all_btn = ttk.Button(
            self._pager_frame, text="Load All", command=self._load_all_entries
        )
        self._load_all_btn.pack(side=tk.LEFT)
        self._pager_frame.grid_remove()

        self.bind("<Destroy>", self._on_destroy)
        self._search_var.trace_add("write", self._schedule_refresh)
        self.apply_theme(self._palette)
        self._refresh()

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("downloaded", foreground=palette.success)
        self._tree.tag_configure("pending", foreground=palette.warning)
        self._tree.tag_configure("hidden", foreground=palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=palette.error)
        apply_theme_to_widget_tree(self._context_menu, palette)

    def refresh(self) -> None:
        """Reload entries from the SQLite ledger."""
        self._refresh()

    def refresh_if_open(self) -> None:
        """Refresh the ledger table cleanly when the modeless dialog is open."""
        try:
            if self.winfo_exists():
                self._refresh()
        except tk.TclError:
            pass

    def _toggle_details_pane(self) -> None:
        if self._details_visible:
            self._split.forget(self._details_pane)
            self._details_visible = False
            self._details_toggle_btn.configure(text="Show Details")
        else:
            self._split.add(self._details_pane, weight=1)
            self._details_visible = True
            self._details_toggle_btn.configure(text="Hide Details")

    def _build_context_menu(self) -> None:
        menu = tk.Menu(self._tree, tearoff=0)
        menu.add_command(
            label="Open Audio File",
            command=lambda: self._open_selected_output(reveal=False),
        )
        menu.add_command(
            label="Reveal in Folder",
            command=lambda: self._open_selected_output(reveal=True),
        )
        menu.add_command(
            label="Upload to Audiobookshelf",
            command=self._upload_selected_to_abs,
        )
        menu.add_command(
            label="Repackage in Pack Page...",
            command=self._repackage_selected,
        )
        menu.add_separator()
        if self._on_queue_urls is not None:
            menu.add_command(label="Add to URLs", command=self._queue_selected)
        menu.add_command(label="Edit Metadata...", command=self._edit_selected_metadata)
        menu.add_command(label="Edit Tags...", command=self._edit_selected_tags)
        menu.add_command(label="Verify Missing Files", command=self._verify_missing_files)
        menu.add_separator()
        menu.add_command(label="Copy URL", command=lambda: self._copy_selected_field("url"))
        menu.add_command(label="Copy Output Path", command=lambda: self._copy_selected_field("output_path"))
        menu.add_command(label="Copy ASIN", command=lambda: self._copy_selected_field("asin"))
        menu.add_command(label="Copy ISBN", command=lambda: self._copy_selected_field("isbn"))
        self._context_menu = menu

    def _selected_entries(self) -> list[ArchiveEntry]:
        return [
            self._entries_by_id[eid]
            for eid in self._selected_entry_ids()
            if eid in self._entries_by_id
        ]

    @staticmethod
    def _format_size(
        path: str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
            elif os.path.isdir(path):
                size = 0
                for r, _d, files in os.walk(path):
                    if is_cancelled is not None and is_cancelled():
                        return "--"
                    for fn in files:
                        if is_cancelled is not None and is_cancelled():
                            return "--"
                        size += os.path.getsize(os.path.join(r, fn))
            else:
                return "Missing on disk"
        except OSError:
            return "--"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _find_nearby_cover(output_path: str) -> str | None:
        if not output_path:
            return None
        folder = output_path if os.path.isdir(output_path) else os.path.dirname(output_path)
        if not folder or not os.path.isdir(folder):
            return None
        for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "cover.webp"):
            cand = os.path.join(folder, name)
            if os.path.isfile(cand):
                return cand
        return None

    def _begin_loading(self) -> None:
        self._loading_ops += 1
        if self._loading_ops == 1:
            self._loading_bar.grid()
            self._loading_bar.start(12)

    def _end_loading(self) -> None:
        self._loading_ops = max(0, self._loading_ops - 1)
        if self._loading_ops == 0:
            self._loading_bar.stop()
            self._loading_bar.grid_remove()

    def _on_entry_selected(self, _event: tk.Event | None = None) -> None:
        self._detail_token += 1
        token = self._detail_token
        if self._detail_debounce_after_id is not None:
            try:
                self.after_cancel(self._detail_debounce_after_id)
            except tk.TclError:
                pass
            self._detail_debounce_after_id = None
        entries = self._selected_entries()
        if not entries:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Selection")
            self._detail_title_var.set("Select a book")
            self._detail_meta_var.set("")
            return
        e = entries[0]
        self._detail_title_var.set(e.title)
        base_lines: list[str] = [
            f"Status: {self._format_status(e)}",
            f"Service: {self._format_service(e)}",
        ]
        if e.profile:
            base_lines.append(f"Profile: {e.profile}")
        if e.author:
            base_lines.append(f"Author: {e.author}")
        if e.narrator:
            base_lines.append(f"Narrator: {e.narrator}")
        if e.series:
            s_txt = f"{e.series} #{e.series_order}" if e.series_order is not None else e.series
            base_lines.append(f"Series: {s_txt}")
        if e.duration_seconds:
            base_lines.append(f"Duration: {format_duration(e.duration_seconds)}")
        if e.audio_format:
            base_lines.append(f"Format: {e.audio_format}")
        if e.asin:
            base_lines.append(f"ASIN: {e.asin}")
        if e.isbn:
            base_lines.append(f"ISBN: {e.isbn}")
        if e.tags:
            base_lines.append(f"Tags: {', '.join(e.tags)}")

        output_path = e.output_path or ""
        if not output_path:
            self._detail_meta_var.set("\n".join(base_lines))
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Cover")
            return

        pending_lines = [*base_lines, "Size: Calculating...", f"Path: {output_path}"]
        self._detail_meta_var.set("\n".join(pending_lines))
        self._detail_cover_photo = None
        self._detail_cover_label.configure(image="", text="Loading cover...")

        thumb_max = scale_px(self, 140)

        def _trigger_detail_worker() -> None:
            self._start_entry_detail_worker(token, base_lines, output_path, thumb_max)

        self._detail_debounce_after_id = self.after(75, _trigger_detail_worker)

    def _start_entry_detail_worker(
        self,
        token: int,
        base_lines: list[str],
        output_path: str,
        thumb_max: int,
    ) -> None:
        self._detail_debounce_after_id = None
        if token != self._detail_token:
            return
        self._cb_queue.begin_task()

        def _worker() -> None:
            try:
                if token != self._detail_token:
                    return
                size_text = self._format_size(
                    output_path,
                    is_cancelled=lambda: token != self._detail_token,
                )
                if token != self._detail_token:
                    return
                cover_path = self._find_nearby_cover(output_path)
                png_bytes = (
                    resize_cover_file_thumbnail_bytes(cover_path, max_size=thumb_max)
                    if cover_path
                    else None
                )
                if token == self._detail_token:
                    def _deliver() -> None:
                        self._apply_entry_details(
                            token, base_lines, output_path, size_text, png_bytes
                        )

                    self._cb_queue.post(_deliver)
            finally:
                self._cb_queue.finish_worker()

        self._detail_executor.submit(_worker)

    def _apply_entry_details(
        self,
        token: int,
        base_lines: list[str],
        output_path: str,
        size_text: str,
        png_bytes: bytes | None,
    ) -> None:
        if not self.winfo_exists() or token != self._detail_token:
            return
        self._detail_meta_var.set(
            "\n".join([*base_lines, f"Size: {size_text}", f"Path: {output_path}"])
        )
        photo = photo_from_image_bytes(png_bytes, self) if png_bytes else None
        self._detail_cover_photo = photo
        if photo is not None:
            self._detail_cover_label.configure(image=photo, text="")
        else:
            self._detail_cover_label.configure(image="", text="No Cover")

    def _on_double_click(self, event: tk.Event) -> None:
        if not self._tree.identify_row(event.y):
            return
        self._open_selected_output(reveal=False)

    def _show_context_menu(self, event: tk.Event) -> str:
        row_iid = self._tree.identify_row(event.y)
        if row_iid and row_iid not in self._tree.selection():
            self._tree.selection_set(row_iid)
            self._tree.focus(row_iid)
        entries = self._selected_entries()
        single = entries[0] if len(entries) == 1 else None
        has_path = bool(single and single.output_path and os.path.exists(single.output_path))
        self._context_menu.entryconfigure(
            "Open Audio File",
            state="normal" if (has_path and self._on_open_path is not None) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Reveal in Folder",
            state="normal" if (has_path and self._on_open_path is not None) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Upload to Audiobookshelf",
            state="normal" if (has_path and self._on_upload_abs is not None) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Repackage in Pack Page...",
            state="normal" if (has_path and self._on_pack_paths is not None) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Edit Metadata...",
            state="normal" if single is not None else "disabled",
        )
        self._context_menu.entryconfigure(
            "Copy URL",
            state="normal" if any(bool(e.url) for e in entries) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Copy Output Path",
            state="normal" if any(bool(e.output_path) for e in entries) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Copy ASIN",
            state="normal" if bool(single and single.asin) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Copy ISBN",
            state="normal" if bool(single and single.isbn) else "disabled",
        )
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()
        return "break"

    def _open_selected_output(self, *, reveal: bool) -> None:
        if self._on_open_path is None:
            return
        entries = self._selected_entries()
        if not entries or not entries[0].output_path:
            return
        self._on_open_path(entries[0].output_path, reveal)

    def _upload_selected_to_abs(self) -> None:
        if self._on_upload_abs is None:
            return
        entries = self._selected_entries()
        if not entries:
            return
        self._on_upload_abs(entries[0])

    def _repackage_selected(self) -> None:
        if self._on_pack_paths is None:
            return
        paths = [e.output_path for e in self._selected_entries() if e.output_path and os.path.exists(e.output_path)]
        if paths:
            self._on_pack_paths(paths)

    def _copy_selected_field(self, field_name: str) -> None:
        vals = [str(getattr(e, field_name, "") or "").strip() for e in self._selected_entries()]
        vals = [v for v in vals if v]
        if not vals:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(vals))
        self._status_var.set(f"Copied {len(vals)} {field_name} value(s) to clipboard.")

    def _verify_missing_files(self) -> None:
        if not os.path.exists(self._archive_path):
            return
        self._verify_btn.configure(state="disabled")
        self._status_var.set("Verifying library paths...")
        self._begin_loading()
        self._cb_queue.begin_task()

        def _finish(missing: int, error: str | None) -> None:
            if not self.winfo_exists():
                return
            self._end_loading()
            self._verify_btn.configure(state="normal")
            if error is not None:
                self._status_var.set(f"Verify error: {error}")
                return
            self._refresh()
            self._status_var.set(
                f"Verified library paths: marked {missing} missing file(s) as pending."
            )

        def _worker() -> None:
            worker_ledger = DownloadArchive(self._archive_path, persistent=True)
            try:
                missing = 0
                with worker_ledger.batch_connection():
                    for entry in worker_ledger.list_entries(
                        limit=100000, include_trashed=False, include_hidden=True
                    ):
                        if (
                            entry.status == "downloaded"
                            and entry.output_path
                            and not os.path.exists(entry.output_path)
                        ):
                            worker_ledger.set_status(entry.id, "pending")
                            missing += 1
                def _post_ok() -> None:
                    _finish(missing, None)

                self._cb_queue.post(_post_ok)
            except Exception as exc:
                err_msg = str(exc)

                def _post_err() -> None:
                    _finish(0, err_msg)

                self._cb_queue.post(_post_err)
            finally:
                worker_ledger.close()
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _get_ledger(self, *, create: bool = False) -> DownloadArchive | None:
        if self._ledger is None:
            if not create and not os.path.exists(self._archive_path):
                return None
            self._ledger = DownloadArchive(self._archive_path, persistent=True)
        return self._ledger

    @staticmethod
    def _format_status(entry: ArchiveEntry) -> str:
        base = entry.status.capitalize()
        if entry.is_hidden:
            return f"{base} [Hidden]"
        return base

    @staticmethod
    def _format_service(entry: ArchiveEntry) -> str:
        if entry.profile and entry.profile != "default":
            return f"{entry.source} ({entry.profile})"
        return entry.source

    @classmethod
    def _format_entry_values(cls, entry: ArchiveEntry) -> tuple[str, ...]:
        series_text = entry.series or ""
        if series_text and entry.series_order is not None:
            series_text = f"{series_text} #{entry.series_order}"
        return (
            cls._format_status(entry),
            cls._format_service(entry),
            entry.title,
            entry.author,
            series_text,
            " ".join(entry.tags),
            entry.output_path or "",
            entry.downloaded_at,
        )

    def _on_filter_changed(self) -> None:
        self._page_offset = 0
        self._load_all = False
        self._refresh()

    def _prev_page(self) -> None:
        if self._page_offset <= 0:
            return
        self._load_all = False
        self._page_offset = max(0, self._page_offset - self._PAGE_SIZE)
        self._refresh()

    def _next_page(self) -> None:
        if self._page_offset + self._PAGE_SIZE >= self._total_matches:
            return
        self._load_all = False
        self._page_offset += self._PAGE_SIZE
        self._refresh()

    def _load_all_entries(self) -> None:
        self._page_offset = 0
        self._load_all = True
        self._refresh()

    def _refresh(self, *_args: object) -> None:
        self._debounce_after_id = None
        existing = self._tree.get_children()
        if existing:
            self._tree.delete(*existing)
        self._entries_by_id.clear()
        ledger = self._get_ledger()
        if ledger is None:
            self._total_matches = 0
            self._pager_frame.grid_remove()
            self._status_var.set("No library database yet.")
            self._scrolled_tree.set_empty_state(
                True,
                "No library entries yet. Download audiobooks or scan local folders.",
                action_text="Locate Books...",
                action_command=self._prompt_locate_local_books,
            )
            return
        view_trash = bool(self._show_trash_var.get())
        show_hidden = bool(self._show_hidden_var.get())
        query_text = self._search_var.get()
        status_filter = "trashed" if view_trash else None
        total = ledger.count_entries(
            query=query_text,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status=status_filter,
        )
        self._total_matches = total
        if total == 0:
            self._page_offset = 0
        elif self._load_all:
            self._page_offset = 0
        elif self._page_offset >= total:
            self._page_offset = max(0, ((total - 1) // self._PAGE_SIZE) * self._PAGE_SIZE)

        query_limit = max(total, 1) if self._load_all else self._PAGE_SIZE
        entries = ledger.list_entries(
            query=query_text,
            limit=query_limit,
            offset=self._page_offset,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status=status_filter,
        )
        for entry in entries:
            self._entries_by_id[entry.id] = entry

        def _entry_tags(entry: ArchiveEntry) -> tuple[str, ...]:
            if entry.status == "trashed":
                return ("trashed",)
            if entry.is_hidden:
                return ("hidden",)
            if entry.status in ("downloaded", "pending"):
                return (entry.status,)
            return ()

        populate_grouped_tree(
            self._tree,
            entries,
            grouped=bool(self._group_var.get()),
            get_iid=lambda entry, _idx: str(entry.id),
            get_values=self._format_entry_values,
            get_group_key=lambda entry: entry.series or "",
            format_group_values=lambda key, count: (
                "Series",
                "",
                f"{key} ({count} items)",
                "",
                key,
                "",
                "",
                "",
            ),
            get_tags=_entry_tags,
        )
        count = len(entries)
        self._scrolled_tree.set_empty_state(
            count == 0,
            "No matching entries in library." if query_text.strip() else "Library ledger is empty.",
            action_text="" if query_text.strip() else "Locate Books...",
            action_command=None if query_text.strip() else self._prompt_locate_local_books,
        )
        noun = f"{'trashed ' if view_trash else ''}item{'s' if total != 1 else ''}"
        if total > count or self._page_offset > 0:
            start_num = self._page_offset + 1 if count else 0
            end_num = self._page_offset + count
            count_label = f"Showing {start_num:,}-{end_num:,} of {total:,} {noun}"
            self._pager_frame.grid()
            self._prev_page_btn.configure(
                state="normal" if self._page_offset > 0 else "disabled"
            )
            self._next_page_btn.configure(
                state="normal" if (self._page_offset + count) < total else "disabled"
            )
            self._load_all_btn.configure(state="disabled" if self._load_all else "normal")
        else:
            count_label = f"Showing {count:,} {noun}"
            self._pager_frame.grid_remove()
        self._status_var.set(
            f"{count_label}  •  Filters: author:, tag:, status:, series:, -tag:hidden"
        )

    def _schedule_refresh(self, *_args: object) -> None:
        self._page_offset = 0
        self._load_all = False
        if self._debounce_after_id is not None:
            try:
                self.after_cancel(self._debounce_after_id)
            except tk.TclError:
                pass
        self._debounce_after_id = self.after(180, self._refresh)

    def _selected_entry_ids(self) -> list[int]:
        out: list[int] = []
        for iid in resolve_selected_leaf_iids(self._tree):
            try:
                out.append(int(iid))
            except ValueError:
                continue
        return out

    def _queue_selected(self) -> None:
        if self._on_queue_urls is None:
            return
        urls: list[str] = []
        for entry_id in self._selected_entry_ids():
            entry = self._entries_by_id.get(entry_id)
            if entry and entry.url and entry.url not in urls:
                urls.append(entry.url)
        if not urls:
            messagebox.showinfo(
                "No Queueable URLs",
                "Selected entries do not have downloadable URLs.",
                parent=self.winfo_toplevel(),
            )
            return
        self._on_queue_urls(urls)

    def _edit_selected_tags(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        first_entry = self._entries_by_id.get(selected_ids[0])
        initial = " ".join(first_entry.tags) if first_entry else ""
        raw = simpledialog.askstring(
            "Edit Tags",
            "Space-separated tags (letters, numbers, underscores; use 'hidden' to hide):",
            initialvalue=initial,
            parent=self.winfo_toplevel(),
        )
        if raw is None:
            return
        parsed = parse_tags_input(raw)
        with ledger.batch_connection():
            for entry_id in selected_ids:
                ledger.set_tags(entry_id, parsed)
        self._refresh()

    def _edit_selected_metadata(self) -> None:
        entries = self._selected_entries()
        if len(entries) != 1:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        entry = entries[0]
        dlg = _ArchiveMetadataDialog(self, entry, palette=self._palette)
        self.wait_window(dlg)
        if dlg.result_updates is None:
            return
        updated = ledger.update_entry(entry.id, **dlg.result_updates)
        self._refresh()
        if updated is not None and self._tree.exists(str(updated.id)):
            self._tree.selection_set(str(updated.id))
            self._tree.focus(str(updated.id))
            self._on_entry_selected()
            self._status_var.set(f"Updated metadata for '{updated.title}'.")

    def _toggle_hide_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        with ledger.batch_connection():
            for entry_id in selected_ids:
                entry = self._entries_by_id.get(entry_id)
                if entry is not None and entry.is_hidden:
                    ledger.remove_tags(entry_id, ["hidden"])
                else:
                    ledger.add_tags(entry_id, ["hidden"])
        self._refresh()

    def _toggle_trash_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        with ledger.batch_connection():
            for entry_id in selected_ids:
                entry = self._entries_by_id.get(entry_id)
                if entry is None:
                    continue
                if entry.status == "trashed":
                    restored_status = "downloaded" if entry.output_path else "pending"
                    ledger.set_status(entry_id, restored_status)
                else:
                    ledger.set_status(entry_id, "trashed")
        self._refresh()

    def _remove_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        with ledger.batch_connection():
            for entry_id in selected_ids:
                ledger.delete_entry(entry_id)
                if self._tree.exists(str(entry_id)):
                    self._tree.delete(str(entry_id))
        self._refresh()

    def _clear_all(self) -> None:
        ledger = self._get_ledger()
        if ledger is None:
            return
        if not messagebox.askyesno(
            "Clear Ledger",
            "Delete all entries from the download history ledger?",
            parent=self.winfo_toplevel(),
        ):
            return
        ledger.clear()
        self._refresh()

    def _prompt_locate_local_books(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            title="Select Local Audiobook Folder to Scan",
        )
        if not folder:
            return
        self.scan_folder(folder, background=True)

    def scan_folder(self, folder: str, *, background: bool = False) -> LocalScanResult | None:
        """Scan `folder` into `downloads.db` and refresh the library grid."""
        self._get_ledger(create=True)
        self._scan_btn.configure(state="disabled")
        self._status_var.set(f"Scanning '{folder}'...")
        if background:
            self._begin_loading()

        def _finish(result: LocalScanResult | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
            if background:
                self._end_loading()
            self._scan_btn.configure(state="normal")
            if error is not None:
                self._status_var.set(f"Scan error: {error}")
                return
            self._refresh()
            if result is not None:
                self._status_var.set(
                    f"Scan complete: {result.scanned_books} book(s) checked, "
                    f"{result.matched} matched, {result.registered} newly registered, "
                    f"{result.missing} missing."
                )

        if not background:
            ledger = self._get_ledger(create=True)
            assert ledger is not None
            res = scan_local_library([folder], ledger, verify_missing=True)
            _finish(res, None)
            return res

        self._cb_queue.begin_task()

        def _worker() -> None:
            # Open a dedicated sqlite connection in the worker thread.
            worker_ledger = DownloadArchive(self._archive_path, persistent=True)
            try:
                res = scan_local_library([folder], worker_ledger, verify_missing=True)

                def _post_ok() -> None:
                    _finish(res, None)

                self._cb_queue.post(_post_ok)
            except Exception as exc:
                err_msg = str(exc)

                def _post_err() -> None:
                    _finish(None, err_msg)

                self._cb_queue.post(_post_err)
            finally:
                worker_ledger.close()
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()
        return None

    def _prompt_export(self) -> None:
        output_path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="Export Library Ledger",
            defaultextension=".csv",
            filetypes=[
                ("CSV spreadsheet", "*.csv"),
                ("JSON file", "*.json"),
                ("Excel workbook", "*.xlsx"),
            ],
        )
        if not output_path:
            return
        try:
            count = self.export_to_path(output_path)
            self._status_var.set(f"Exported {count} book(s) to {output_path}")
        except Exception as exc:
            messagebox.showerror("Export Error", str(exc), parent=self.winfo_toplevel())

    def export_to_path(self, output_path: str) -> int:
        """Export currently filtered library entries to `output_path`."""
        ledger = self._get_ledger()
        if ledger is None:
            return export_library_entries([], output_path)
        view_trash = bool(self._show_trash_var.get())
        show_hidden = bool(self._show_hidden_var.get())
        entries = ledger.list_entries(
            query=self._search_var.get(),
            limit=100000,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status="trashed" if view_trash else None,
        )
        return export_library_entries(entries, output_path)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._detail_token += 1
        if self._detail_debounce_after_id is not None:
            try:
                self.after_cancel(self._detail_debounce_after_id)
            except tk.TclError:
                pass
            self._detail_debounce_after_id = None
        if self._debounce_after_id is not None:
            try:
                self.after_cancel(self._debounce_after_id)
            except tk.TclError:
                pass
            self._debounce_after_id = None
        self._detail_executor.shutdown(wait=False, cancel_futures=True)
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None


class CatalogTab(ttk.Frame):
    """Tab hosting service catalog search and remote library browsing."""

    _MAX_COVER_CACHE = 2000
    _MAX_DETAIL_COVER_CACHE = 128
    _MAX_COVER_BYTES = 5 * 1024 * 1024
    _TREE_WIDTH_GROUPED = 102
    _TREE_WIDTH_FLAT = 84
    _TREE_MINWIDTH = 84
    _COLUMNS = (
        TreeColumnSpec("title", "Title", width=250, stretch=True),
        TreeColumnSpec("author", "Author", width=155, stretch=True),
        TreeColumnSpec("series", "Series", width=135, stretch=True),
        TreeColumnSpec("status", "Library", width=100, stretch=False),
        TreeColumnSpec("formats", "Formats", width=125, stretch=False),
        TreeColumnSpec("url", "URL", width=165, stretch=False),
    )

    def __init__(
        self,
        parent: tk.Misc,
        services: Sequence[ServiceInfo],
        selected_service: ServiceInfo | None,
        *,
        on_fetch: Callable[[str, ServiceInfo, str, CatalogTab], None],
        on_add_urls: Callable[[list[str]], None],
        on_download_urls: Callable[[list[str], str, str], None] | None = None,
        on_save_to_library: Callable[[str, str, Sequence[CatalogBook]], tuple[int, int]] | None = None,
        on_sync_libraries: Callable[[str], None] | None = None,
        profile_resolver: Callable[[str], Sequence[str]] | None = None,
        status_resolver: Callable[[str, Sequence[CatalogBook]], Sequence[str]] | None = None,
        cover_fetcher: Callable[[str], bytes | None] | None = None,
        on_cancel_fetch: Callable[[], object] | None = None,
        on_manage_account: Callable[[str, str], object] | None = None,
        config_provider: Callable[[], Config] | None = None,
        selected_profile: str = "default",
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 10))
        browseable = [svc for svc in services if svc.supports_search or svc.supports_library]
        self._services_by_label: dict[str, ServiceInfo] = {svc.label: svc for svc in browseable}
        self._on_fetch = on_fetch
        self._on_add_urls = on_add_urls
        self._on_download_urls = on_download_urls
        self._on_save_to_library = on_save_to_library
        self._on_sync_libraries = on_sync_libraries
        self._profile_resolver = profile_resolver
        self._status_resolver = status_resolver
        self._cover_fetcher = cover_fetcher
        self._on_cancel_fetch = on_cancel_fetch
        self._on_manage_account = on_manage_account
        self._config_provider = config_provider
        self._cover_thread_local = threading.local()
        self._cover_sessions_lock = threading.Lock()
        self._cover_sessions: list[requests.Session] = []
        self._loaded_books: list[CatalogBook] = []
        self._book_statuses: list[str] = []
        self._visible_count = 0
        self._loaded_service_key: str = ""
        self._loaded_profile: str = "default"
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)
        self._cover_fetch_token = 0
        self._detail_token = 0
        self._filter_after_id: str | None = None
        self._thumb_size = scale_px(self, 48)
        self._detail_thumb_size = scale_px(self, 160)
        self._cover_png_cache: OrderedDict[str, bytes | None] = OrderedDict()
        self._detail_cover_png_cache: OrderedDict[str, bytes | None] = OrderedDict()
        self._url_photos: OrderedDict[str, Any] = OrderedDict()
        self._row_photos: dict[str, Any] = {}
        self._detail_cover_photo: Any | None = None
        self._placeholder_photo: Any | None = None
        self._details_visible = True
        self.bind("<Destroy>", self._on_destroy, add="+")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        default_label = (
            selected_service.label
            if selected_service is not None
            and (selected_service.supports_search or selected_service.supports_library)
            else (browseable[0].label if browseable else "")
        )
        self._service_var = tk.StringVar(value=default_label)
        self._profile_var = tk.StringVar(value=(selected_profile or "default").strip().lower() or "default")
        self._query_var = tk.StringVar()
        self._local_filter_var = tk.StringVar()
        self._status_var = tk.StringVar(value="Choose a service and click Search or List Library.")

        top_bar = ttk.Frame(self)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top_bar, text="Service:").pack(side=tk.LEFT, padx=(0, 6))
        svc_combo = ttk.Combobox(
            top_bar,
            textvariable=self._service_var,
            values=[svc.label for svc in browseable],
            state="readonly",
            width=18,
        )
        svc_combo.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(top_bar, text="Profile:").pack(side=tk.LEFT, padx=(0, 4))
        self._profile_combo = ttk.Combobox(
            top_bar,
            textvariable=self._profile_var,
            values=["default"],
            state="readonly",
            width=11,
        )
        self._profile_combo.pack(side=tk.LEFT, padx=(0, 6))
        self._account_btn: ttk.Button | None = None
        if self._on_manage_account is not None:
            self._account_btn = ttk.Button(
                top_bar,
                text="Account...",
                command=self._open_selected_account,
            )
            self._account_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._query_entry = ttk.Entry(top_bar, textvariable=self._query_var, width=24)
        self._query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._search_btn = ttk.Button(top_bar, text="Search", command=lambda: self.run_fetch("search"))
        self._search_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._library_btn = ttk.Button(
            top_bar, text="List Library", command=lambda: self.run_fetch("library")
        )
        self._library_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cancel_btn = ttk.Button(
            top_bar, text="Cancel", command=self.cancel_fetch, state="disabled"
        )
        self._cancel_btn.pack(side=tk.LEFT)

        self._group_var = tk.BooleanVar(value=True)
        self._hide_downloaded_var = tk.BooleanVar(value=False)
        self._has_ebook_var = tk.BooleanVar(value=False)

        filter_bar = ttk.Frame(self)
        filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(filter_bar, text="Filter:").pack(side=tk.LEFT, padx=(0, 6))
        self._local_filter_entry = ttk.Entry(
            filter_bar,
            textvariable=self._local_filter_var,
            width=22,
        )
        self._local_filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._clear_filter_btn = ttk.Button(
            filter_bar,
            text="\u00d7",
            width=3,
            command=self._clear_local_filter,
            state="disabled",
        )
        self._clear_filter_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            filter_bar,
            text="Group Series",
            variable=self._group_var,
            command=self._on_group_toggled,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            filter_bar,
            text="Hide Downloaded",
            variable=self._hide_downloaded_var,
            command=self._on_filter_toggled,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            filter_bar,
            text="Has Ebook",
            variable=self._has_ebook_var,
            command=self._on_filter_toggled,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._expand_all_btn = ttk.Button(
            filter_bar,
            text="Expand All",
            command=self.expand_all_groups,
            state="disabled",
        )
        self._expand_all_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._collapse_all_btn = ttk.Button(
            filter_bar,
            text="Collapse All",
            command=self.collapse_all_groups,
            state="disabled",
        )
        self._collapse_all_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._details_toggle_btn = ttk.Button(
            filter_bar,
            text="Hide Details",
            command=self._toggle_details_pane,
        )
        self._details_toggle_btn.pack(side=tk.LEFT)

        self._split = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self._split.grid(row=2, column=0, sticky="nsew")

        self._scrolled_tree = ScrolledTreeview(
            self._split,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
            tree_heading="Cover",
            tree_width=self._TREE_WIDTH_GROUPED,
            tree_minwidth=self._TREE_MINWIDTH,
            tree_style="Catalog.Treeview",
        )
        self._split.add(self._scrolled_tree, weight=3)
        self._tree = self._scrolled_tree.tree
        self._scrolled_tree.set_empty_state(
            True,
            "Choose a service above and click Search or List Library.",
        )

        self._details_pane = ttk.LabelFrame(
            self._split, text="Book Details", padding=scale_px(self, 8)
        )
        self._details_pane.columnconfigure(0, weight=1)
        self._detail_cover_label = ttk.Label(
            self._details_pane, text="No Cover", anchor="center"
        )
        self._detail_cover_label.grid(row=0, column=0, pady=(0, 8))
        self._detail_title_var = tk.StringVar(value="Select a book")
        self._detail_meta_var = tk.StringVar(value="")
        ttk.Label(
            self._details_pane,
            textvariable=self._detail_title_var,
            font=make_bold_font(self, 10),
            wraplength=scale_px(self, 230),
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(
            self._details_pane,
            textvariable=self._detail_meta_var,
            wraplength=scale_px(self, 230),
            justify="left",
        ).grid(row=2, column=0, sticky="nw")

        detail_actions = ttk.Frame(self._details_pane)
        detail_actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        detail_actions.columnconfigure(0, weight=1)
        detail_actions.columnconfigure(1, weight=1)
        self._detail_download_btn: ttk.Button | None = None
        if self._on_download_urls is not None:
            detail_download_btn = ttk.Button(
                detail_actions,
                text="Download",
                command=self.download_selected,
                style="Accent.TButton",
                state="disabled",
            )
            detail_download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=(0, 4))
            self._detail_download_btn = detail_download_btn
        self._detail_add_btn = ttk.Button(
            detail_actions,
            text="Add to URLs",
            command=self.add_selected,
            state="disabled",
        )
        self._detail_add_btn.grid(
            row=0,
            column=1 if self._detail_download_btn is not None else 0,
            columnspan=1 if self._detail_download_btn is not None else 2,
            sticky="ew",
            padx=(3 if self._detail_download_btn is not None else 0, 0),
            pady=(0, 4),
        )
        self._detail_open_url_btn = ttk.Button(
            detail_actions,
            text="Open URL",
            command=self._open_selected_url,
            state="disabled",
        )
        self._detail_open_url_btn.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self._detail_copy_url_btn = ttk.Button(
            detail_actions,
            text="Copy URL",
            command=self._copy_selected_urls,
            state="disabled",
        )
        self._detail_copy_url_btn.grid(row=1, column=1, sticky="ew", padx=(3, 0))

        self._split.add(self._details_pane, weight=1)

        self._build_context_menu()
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_selected)
        self._tree.bind("<Double-1>", self._on_tree_double_click)
        self._tree.bind("<Button-3>", self._show_context_menu)
        self._tree.bind("<Control-a>", self._select_all_visible)
        self._tree.bind("<Control-A>", self._select_all_visible)
        self._tree.bind("<Control-f>", self._focus_local_filter)
        self._tree.bind("<Control-F>", self._focus_local_filter)
        self._local_filter_entry.bind("<Escape>", self._on_filter_escape)
        if sys.platform == "darwin":
            self._tree.bind("<Button-2>", self._show_context_menu)

        self._loading_bar = ttk.Progressbar(self, mode="indeterminate")
        self._loading_bar.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(self)
        btn_bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._select_all_btn = ttk.Button(
            btn_bar,
            text="Select All",
            command=self.select_all_visible,
            state="disabled",
        )
        self._select_all_btn.pack(side=tk.LEFT)
        self._download_btn: ttk.Button | None = None
        if self._on_download_urls is not None:
            download_btn = ttk.Button(
                btn_bar,
                text="Download Selected",
                command=self.download_selected,
                style="Accent.TButton",
            )
            download_btn.pack(side=tk.RIGHT)
            self._download_btn = download_btn
        ttk.Button(btn_bar, text="Add Selected to URLs", command=self.add_selected).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self._save_lib_btn: ttk.Button | None = None
        if self._on_save_to_library is not None:
            save_lib_btn = ttk.Button(
                btn_bar,
                text="Save to Library DB",
                command=self.save_to_library,
                state="disabled",
            )
            save_lib_btn.pack(side=tk.RIGHT, padx=(0, 6))
            self._save_lib_btn = save_lib_btn
        if self._on_sync_libraries is not None:
            ttk.Button(
                btn_bar,
                text="Sync Libraries",
                command=self._trigger_sync,
            ).pack(side=tk.RIGHT, padx=(0, 6))

        status_row = ttk.Frame(self)
        status_row.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)
        ttk.Label(status_row, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        self._query_entry.bind("<Return>", lambda _e: self.run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", self.sync_buttons)
        self._local_filter_var.trace_add("write", self._schedule_local_filter)
        self.apply_theme(self._palette)
        self.sync_buttons()

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("downloaded", foreground=palette.success)
        self._tree.tag_configure("pending", foreground=palette.warning)
        self._tree.tag_configure("hidden", foreground=palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=palette.fg_muted)
        self._placeholder_photo = create_placeholder_cover_thumbnail(
            self,
            size=self._thumb_size,
            bg_hex=palette.surface_alt,
            border_hex=palette.border,
            glyph_hex=palette.fg_muted,
        )
        if self._loaded_books:
            self._render_tree()
        apply_theme_to_widget_tree(self._context_menu, palette)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        self._cover_fetch_token += 1
        self._detail_token += 1
        self._row_photos.clear()
        self._url_photos.clear()
        self._detail_cover_photo = None
        self._placeholder_photo = None
        with self._cover_sessions_lock:
            sessions = list(self._cover_sessions)
            self._cover_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _build_context_menu(self) -> None:
        menu = tk.Menu(self._tree, tearoff=0)
        if self._on_download_urls is not None:
            menu.add_command(label="Download Selected", command=self.download_selected)
        menu.add_command(label="Add Selected to URLs", command=self.add_selected)
        if self._on_save_to_library is not None:
            menu.add_command(label="Save to Library DB", command=self.save_to_library)
        menu.add_separator()
        menu.add_command(label="Filter by Author", command=self._filter_by_selected_author)
        menu.add_command(label="Filter by Series", command=self._filter_by_selected_series)
        menu.add_command(label="Expand All Series", command=self.expand_all_groups)
        menu.add_command(label="Collapse All Series", command=self.collapse_all_groups)
        menu.add_separator()
        menu.add_command(label="Copy Title & Author", command=self._copy_selected_titles)
        menu.add_command(label="Copy URL(s)", command=self._copy_selected_urls)
        menu.add_command(label="Open URL in Browser", command=self._open_selected_url)
        self._context_menu = menu

    def _toggle_details_pane(self) -> None:
        if self._details_visible:
            self._split.forget(self._details_pane)
            self._details_visible = False
            self._details_toggle_btn.configure(text="Show Details")
        else:
            self._split.add(self._details_pane, weight=1)
            self._details_visible = True
            self._details_toggle_btn.configure(text="Hide Details")

    def _focus_local_filter(self, _event: tk.Event | None = None) -> str:
        self._local_filter_entry.focus_set()
        self._local_filter_entry.selection_range(0, tk.END)
        return "break"

    def _on_filter_escape(self, _event: tk.Event | None = None) -> str | None:
        if self._local_filter_var.get():
            self._clear_local_filter()
            return "break"
        return None

    def _clear_local_filter(self) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        if self._local_filter_var.get():
            self._local_filter_var.set("")
        self._on_filter_toggled()

    def _schedule_local_filter(self, *_args: object) -> None:
        has_text = bool(self._local_filter_var.get().strip())
        self._clear_filter_btn.configure(state="normal" if has_text else "disabled")
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
        self._filter_after_id = self.after(150, self._apply_scheduled_filter)

    def _apply_scheduled_filter(self) -> None:
        self._filter_after_id = None
        if self._loaded_books:
            self._on_filter_toggled()

    def _on_group_toggled(self) -> None:
        grouped = bool(self._group_var.get())
        self._scrolled_tree.set_tree_column_width(
            self._TREE_WIDTH_GROUPED if grouped else self._TREE_WIDTH_FLAT,
            minwidth=self._TREE_MINWIDTH,
        )
        visible_count = self._render_tree()
        if self._loaded_books:
            self._update_count_status(visible_count)
        self._update_details_pane()

    def expand_all_groups(self) -> None:
        """Expand all series/podcast parent rows in the catalog tree."""
        for iid in self._tree.get_children(""):
            if self._tree.get_children(iid):
                self._tree.item(iid, open=True)

    def collapse_all_groups(self) -> None:
        """Collapse all series/podcast parent rows in the catalog tree."""
        for iid in self._tree.get_children(""):
            if self._tree.get_children(iid):
                self._tree.item(iid, open=False)

    def select_all_visible(self) -> None:
        """Select all currently visible leaf book items in the catalog tree."""
        leaf_iids: list[str] = []

        def _collect(parent_iid: str) -> None:
            for iid in self._tree.get_children(parent_iid):
                children = self._tree.get_children(iid)
                if children:
                    _collect(iid)
                elif iid.startswith("book-"):
                    leaf_iids.append(iid)

        _collect("")
        if leaf_iids:
            self._tree.selection_set(leaf_iids)
            self._tree.focus(leaf_iids[0])
            self._on_tree_selected()

    def _select_all_visible(self, _event: tk.Event | None = None) -> str:
        self.select_all_visible()
        return "break"

    def _active_service_and_profile(self) -> tuple[str, str]:
        svc = self._services_by_label.get(self._service_var.get())
        service_key = self._loaded_service_key or (svc.key if svc is not None else "")
        profile = self._loaded_profile or self.selected_profile
        return service_key, profile

    def _open_selected_account(self) -> None:
        if self._on_manage_account is None:
            return
        svc = self._services_by_label.get(self._service_var.get())
        service_key = svc.key if svc is not None else self._loaded_service_key
        self._on_manage_account(service_key, self.selected_profile)

    @staticmethod
    def _format_series_cell(book: CatalogBook) -> str:
        series = (book.series or "").strip()
        if not series:
            return ""
        if book.series_order is not None:
            return f"{series} #{book.series_order}"
        return series

    @staticmethod
    def _matches_local_filter(book: CatalogBook, status_label: str, tokens: list[str]) -> bool:
        if not tokens:
            return True
        haystack = " ".join(
            part
            for part in (
                book.title,
                book.author,
                book.series or "",
                status_label,
                format_catalog_formats(book),
                book.url,
            )
            if part
        ).casefold()
        return all(tok in haystack for tok in tokens)

    @staticmethod
    def _order_books_for_grouped_tree(
        indexed_books: list[tuple[int, CatalogBook, str]],
    ) -> list[tuple[int, CatalogBook, str]]:
        """Sort books within multi-item series by `series_order` while preserving group first-seen order."""
        by_series: dict[str, list[tuple[int, int, CatalogBook, str]]] = {}
        for pos, entry in enumerate(indexed_books):
            key = (entry[1].series or "").strip()
            if key:
                by_series.setdefault(key, []).append((pos, entry[0], entry[1], entry[2]))

        sorted_iterators: dict[str, list[tuple[int, CatalogBook, str]]] = {}
        for key, items in by_series.items():
            if len(items) >= 2:
                ordered = sorted(
                    items,
                    key=lambda row: (
                        row[2].series_order is None,
                        row[2].series_order or 0,
                        row[0],
                    ),
                )
                sorted_iterators[key] = [(idx, b, st) for _pos, idx, b, st in ordered]

        cursors: dict[str, int] = {}
        result: list[tuple[int, CatalogBook, str]] = []
        for entry in indexed_books:
            key = (entry[1].series or "").strip()
            if key in sorted_iterators:
                idx = cursors.get(key, 0)
                cursors[key] = idx + 1
                result.append(sorted_iterators[key][idx])
            else:
                result.append(entry)
        return result

    @staticmethod
    def _status_tags(status_label: str) -> tuple[str, ...]:
        lower = status_label.strip().lower()
        if lower.startswith("downloaded"):
            return ("downloaded",)
        if lower == "pending":
            return ("pending",)
        if lower == "hidden":
            return ("hidden",)
        if lower == "trashed":
            return ("trashed",)
        return ()

    def _refresh_statuses(self) -> None:
        if self._status_resolver is None or not self._loaded_books:
            self._book_statuses = [""] * len(self._loaded_books)
            return
        service_key, _profile = self._active_service_and_profile()
        resolved = list(self._status_resolver(service_key, self._loaded_books))
        if len(resolved) < len(self._loaded_books):
            resolved.extend([""] * (len(self._loaded_books) - len(resolved)))
        self._book_statuses = [str(s or "") for s in resolved[: len(self._loaded_books)]]

    def _update_count_status(self, visible_count: int) -> None:
        self._visible_count = visible_count
        total = len(self._loaded_books)
        downloaded = sum(1 for s in self._book_statuses if s.lower().startswith("downloaded"))
        ebooks = sum(1 for b in self._loaded_books if getattr(b, "has_ebook", False))
        details: list[str] = []
        if ebooks > 0:
            details.append(f"{ebooks} with ebook")
        if self._hide_downloaded_var.get() and downloaded > 0:
            details.append(f"{downloaded} downloaded hidden")
        elif downloaded > 0:
            details.append(f"{downloaded} in library")
        suffix = f" ({', '.join(details)})" if details else ""
        if visible_count < total:
            base = (
                f"Showing {visible_count} of {total} book{'s' if total != 1 else ''}{suffix}."
            )
        else:
            base = f"Found {total} book{'s' if total != 1 else ''}{suffix}."
        selected_count = len(self._selected_books())
        if selected_count > 0:
            base = f"{base}  \u2022  {selected_count} selected"
        self._status_var.set(base)

    def _on_filter_toggled(self) -> None:
        visible_count = self._render_tree()
        if self._loaded_books:
            self._update_count_status(visible_count)
        self._update_details_pane()

    @property
    def selected_profile(self) -> str:
        return (self._profile_var.get() or "default").strip().lower() or "default"

    def sync_buttons(self, *_args: object) -> None:
        svc = self._services_by_label.get(self._service_var.get())
        if svc is None:
            self._profile_combo.configure(values=["default"], state="disabled")
            self._query_entry.configure(state="disabled")
            self._search_btn.configure(state="disabled")
            self._library_btn.configure(state="disabled")
            return
        profiles = list(self._profile_resolver(svc.key)) if self._profile_resolver else ["default"]
        if "default" not in profiles:
            profiles = ["default", *profiles]
        self._profile_combo.configure(values=profiles, state="readonly")
        if self._profile_var.get() not in profiles:
            self._profile_var.set("default")
        self._query_entry.configure(state="normal" if svc.supports_search else "disabled")
        self._search_btn.configure(state="normal" if svc.supports_search else "disabled")
        self._library_btn.configure(state="normal" if svc.supports_library else "disabled")

    def run_fetch(self, mode: str) -> None:
        svc = self._services_by_label.get(self._service_var.get())
        if svc is None:
            return
        query_text = self._query_var.get().strip()
        if mode == "search" and not query_text:
            self._status_var.set("Enter a search term first.")
            return
        self._search_btn.configure(state="disabled")
        self._library_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="disabled")
        self._loaded_service_key = svc.key
        self._loaded_profile = self.selected_profile
        self._status_var.set(f"{'Searching' if mode == 'search' else 'Loading library for'} {svc.label}...")
        self._scrolled_tree.set_empty_state(False)
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._on_fetch(mode, svc, query_text, self)

    def cancel_fetch(self) -> None:
        """Cancel the active catalog search or library listing request."""
        if self._on_cancel_fetch is not None:
            self._on_cancel_fetch()
        self._stop_loading()
        self.sync_buttons()
        self._status_var.set("Catalog request cancelled.")
        if not self._loaded_books:
            self._scrolled_tree.set_empty_state(
                True,
                "Catalog request cancelled.",
            )

    def _stop_loading(self) -> None:
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._cancel_btn.configure(state="disabled")

    def set_error_status(self, message: str = "Error loading items") -> None:
        self._stop_loading()
        self.sync_buttons()
        self._status_var.set(message)
        if not self._loaded_books:
            self._scrolled_tree.set_empty_state(True, message)

    @staticmethod
    def _lru_set(cache: OrderedDict[str, Any], key: str, value: Any, max_size: int) -> None:
        if key in cache:
            cache.move_to_end(key)
        cache[key] = value
        while len(cache) > max_size:
            cache.popitem(last=False)

    def _photo_for_book(self, book: CatalogBook) -> Any | None:
        cover_url = (getattr(book, "cover_url", None) or "").strip()
        if cover_url:
            photo = self._url_photos.get(cover_url)
            if photo is not None:
                self._url_photos.move_to_end(cover_url)
                return photo
            if cover_url in self._cover_png_cache:
                cached_png = self._cover_png_cache[cover_url]
                self._cover_png_cache.move_to_end(cover_url)
                if cached_png:
                    photo = photo_from_image_bytes(cached_png, self)
                    if photo is not None:
                        self._lru_set(self._url_photos, cover_url, photo, self._MAX_COVER_CACHE)
                        return photo
        return self._placeholder_photo

    def _get_cover_http_session(self, cfg: Config | None) -> requests.Session:
        desired_proxy = (getattr(cfg, "proxy", None) or "").strip() or None
        session: requests.Session | None = getattr(self._cover_thread_local, "session", None)
        if session is None:
            session = create_configured_http_session(
                cfg,
                proxy=desired_proxy,
                pool_connections=8,
                pool_maxsize=8,
            )
            self._cover_thread_local.session = session
            with self._cover_sessions_lock:
                self._cover_sessions.append(session)
        else:
            if desired_proxy:
                session.proxies["http"] = desired_proxy
                session.proxies["https"] = desired_proxy
            else:
                session.proxies.pop("http", None)
                session.proxies.pop("https", None)
        return session

    def _default_fetch_cover_bytes(self, url: str) -> bytes | None:
        if not url.lower().startswith(("http://", "https://")):
            return None
        cfg = self._config_provider() if self._config_provider is not None else None
        session = self._get_cover_http_session(cfg)
        timeout = resolve_http_timeout(cfg, default=10)
        try:
            with session.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "audiobook-dl"},
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                raw_len = resp.headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        if int(str(raw_len).strip()) > self._MAX_COVER_BYTES:
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._MAX_COVER_BYTES:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data if data else None
        except Exception:
            return None

    def _start_cover_loading(self, token: int) -> None:
        urls_to_fetch: list[str] = []
        seen: set[str] = set()
        for book in self._loaded_books:
            cover_url = (getattr(book, "cover_url", None) or "").strip()
            if not cover_url or cover_url in seen or cover_url in self._cover_png_cache:
                continue
            seen.add(cover_url)
            urls_to_fetch.append(cover_url)
        if not urls_to_fetch:
            return

        fetcher = self._cover_fetcher or self._default_fetch_cover_bytes
        thumb_size = self._thumb_size
        self._cb_queue.begin_task()

        def _fetch_one(url: str) -> None:
            if token != self._cover_fetch_token:
                return
            raw = fetcher(url)
            png_bytes = resize_cover_thumbnail_bytes(raw, size=thumb_size) if raw else None
            if token == self._cover_fetch_token:
                def _post_cover() -> None:
                    self._on_cover_bytes_ready(token, url, png_bytes)

                self._cb_queue.post(_post_cover)

        def _dispatcher() -> None:
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(_fetch_one, urls_to_fetch))
            finally:
                self._cb_queue.finish_worker()

        threading.Thread(target=_dispatcher, daemon=True).start()

    def _on_cover_bytes_ready(self, token: int, url: str, png_bytes: bytes | None) -> None:
        if not self.winfo_exists() or token != self._cover_fetch_token:
            return
        if not png_bytes:
            self._lru_set(self._cover_png_cache, url, None, self._MAX_COVER_CACHE)
            return
        if len(self._url_photos) >= self._MAX_COVER_CACHE and url not in self._url_photos:
            evicted_url, _ = self._url_photos.popitem(last=False)
            self._cover_png_cache.pop(evicted_url, None)
        self._lru_set(self._cover_png_cache, url, png_bytes, self._MAX_COVER_CACHE)
        photo = photo_from_image_bytes(png_bytes, self)
        if photo is None:
            return
        self._lru_set(self._url_photos, url, photo, self._MAX_COVER_CACHE)
        for idx, book in enumerate(self._loaded_books):
            if (getattr(book, "cover_url", None) or "").strip() != url:
                continue
            iid = f"book-{idx}"
            if self._tree.exists(iid):
                self._row_photos[iid] = photo
                self._tree.item(iid, image=photo)

    def _render_tree(self) -> int:
        hide_downloaded = bool(self._hide_downloaded_var.get())
        only_has_ebook = bool(self._has_ebook_var.get())
        filter_tokens = [
            tok for tok in self._local_filter_var.get().strip().casefold().split() if tok
        ]
        indexed_books: list[tuple[int, CatalogBook, str]] = []
        for idx, book in enumerate(self._loaded_books):
            status_label = self._book_statuses[idx] if idx < len(self._book_statuses) else ""
            if hide_downloaded and status_label.lower().startswith("downloaded"):
                continue
            if only_has_ebook and not getattr(book, "has_ebook", False):
                continue
            if filter_tokens and not self._matches_local_filter(book, status_label, filter_tokens):
                continue
            indexed_books.append((idx, book, status_label))

        grouped = bool(self._group_var.get())
        if grouped and indexed_books:
            indexed_books = self._order_books_for_grouped_tree(indexed_books)

        group_meta: dict[str, tuple[str, str, str]] = {}
        if grouped:
            grouped_entries: dict[str, list[tuple[int, CatalogBook, str]]] = {}
            for entry in indexed_books:
                key = (entry[1].series or "").strip()
                if key:
                    grouped_entries.setdefault(key, []).append(entry)
            for key, items in grouped_entries.items():
                authors = {b.author.strip() for _, b, _ in items if b.author and b.author.strip()}
                author_text = next(iter(authors)) if len(authors) == 1 else ""
                downloaded_cnt = sum(
                    1 for _, _, st in items if st.lower().startswith("downloaded")
                )
                pending_cnt = sum(1 for _, _, st in items if st.lower() == "pending")
                if downloaded_cnt:
                    lib_text = f"{downloaded_cnt}/{len(items)} downloaded"
                elif pending_cnt:
                    lib_text = f"{pending_cnt}/{len(items)} pending"
                else:
                    lib_text = ""
                ebook_cnt = sum(1 for _, b, _ in items if getattr(b, "has_ebook", False))
                fmt_text = f"{ebook_cnt} w/ Ebook" if ebook_cnt else ""
                group_meta[key] = (author_text, lib_text, fmt_text)

        self._row_photos.clear()

        def _row_image(entry: tuple[int, CatalogBook, str]) -> Any | None:
            photo = self._photo_for_book(entry[1])
            if photo is not None:
                self._row_photos[f"book-{entry[0]}"] = photo
            return photo

        def _format_group(key: str, count: int) -> tuple[str, ...]:
            author_text, lib_text, fmt_text = group_meta.get(key, ("", "", ""))
            return (f"{key} ({count} items)", author_text, "", lib_text, fmt_text, "")

        group_count = populate_grouped_tree(
            self._tree,
            indexed_books,
            grouped=grouped,
            get_iid=lambda entry, _pos: f"book-{entry[0]}",
            get_values=lambda entry: (
                entry[1].title,
                entry[1].author,
                self._format_series_cell(entry[1]),
                entry[2],
                format_catalog_formats(entry[1]),
                entry[1].url,
            ),
            get_group_key=lambda entry: entry[1].series or "",
            format_group_values=_format_group,
            should_group=lambda entry, count: count >= 2 or entry[1].item_type == "podcast",
            get_tags=lambda entry: self._status_tags(entry[2]),
            get_image=_row_image,
        )
        has_groups = grouped and group_count > 0
        self._expand_all_btn.configure(state="normal" if has_groups else "disabled")
        self._collapse_all_btn.configure(state="normal" if has_groups else "disabled")
        self._select_all_btn.configure(state="normal" if indexed_books else "disabled")
        self._scrolled_tree.set_empty_state(
            len(indexed_books) == 0,
            "No matching books found." if self._loaded_books else "No books returned.",
        )
        return len(indexed_books)

    def populate_items(
        self,
        books: Sequence[CatalogBook],
        *,
        statuses: Sequence[str] | None = None,
    ) -> None:
        self._stop_loading()
        self._cover_fetch_token += 1
        token = self._cover_fetch_token
        self._loaded_books = list(books)
        if statuses is not None:
            resolved = list(statuses)
            if len(resolved) < len(self._loaded_books):
                resolved.extend([""] * (len(self._loaded_books) - len(resolved)))
            self._book_statuses = [str(s or "") for s in resolved[: len(self._loaded_books)]]
        else:
            self._refresh_statuses()
        visible_count = self._render_tree()
        self.sync_buttons()
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="normal" if self._loaded_books else "disabled")
        self._update_count_status(visible_count)
        self._update_details_pane()
        self._start_cover_loading(token)

    def refresh_statuses(self) -> None:
        """Re-query library statuses for loaded books and refresh the tree."""
        if not self._loaded_books:
            return
        self._refresh_statuses()
        visible_count = self._render_tree()
        self._update_count_status(visible_count)
        self._update_details_pane()

    def _selected_book_indices(self) -> list[int]:
        chosen: list[int] = []
        for iid in resolve_selected_leaf_iids(self._tree):
            if iid.startswith("book-"):
                try:
                    idx = int(iid.split("-", 1)[1])
                except ValueError:
                    continue
                if 0 <= idx < len(self._loaded_books):
                    chosen.append(idx)
        return chosen

    def _selected_books(self) -> list[CatalogBook]:
        return [self._loaded_books[idx] for idx in self._selected_book_indices()]

    def _selected_urls(self) -> list[str]:
        urls: list[str] = []
        for book in self._selected_books():
            url = (book.url or "").strip()
            if url and url not in urls:
                urls.append(url)
        return urls

    def _on_tree_selected(self, _event: tk.Event | None = None) -> None:
        self._update_details_pane()
        if self._loaded_books:
            self._update_count_status(self._visible_count)

    def _set_detail_buttons_state(self, *, has_books: bool, has_http_url: bool) -> None:
        state_books = "normal" if has_books else "disabled"
        if self._detail_download_btn is not None:
            self._detail_download_btn.configure(state=state_books)
        self._detail_add_btn.configure(state=state_books)
        self._detail_copy_url_btn.configure(state=state_books)
        self._detail_open_url_btn.configure(state="normal" if has_http_url else "disabled")

    def _update_details_pane(self) -> None:
        self._detail_token += 1
        token = self._detail_token
        indices = self._selected_book_indices()
        if not indices:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Selection")
            self._detail_title_var.set("Select a book")
            self._detail_meta_var.set("")
            self._set_detail_buttons_state(has_books=False, has_http_url=False)
            return

        if len(indices) > 1:
            books = [self._loaded_books[i] for i in indices]
            sel_iids = self._tree.selection()
            first_sel = sel_iids[0] if sel_iids else ""
            if len(sel_iids) == 1 and first_sel and self._tree.get_children(first_sel):
                vals = self._tree.item(first_sel, "values")
                header = str(vals[0]) if vals else f"{len(books)} items selected"
                self._detail_title_var.set(header)
            else:
                self._detail_title_var.set(f"{len(books)} books selected")
            authors = sorted({b.author.strip() for b in books if b.author and b.author.strip()})
            ebooks = sum(1 for b in books if getattr(b, "has_ebook", False))
            downloaded = sum(
                1
                for i in indices
                if i < len(self._book_statuses)
                and self._book_statuses[i].lower().startswith("downloaded")
            )
            summary_lines = [f"Items: {len(books)}"]
            if len(authors) == 1:
                summary_lines.append(f"Author: {authors[0]}")
            elif authors:
                summary_lines.append(f"Authors: {len(authors)} distinct")
            if downloaded:
                summary_lines.append(f"Downloaded: {downloaded}/{len(books)}")
            if ebooks:
                summary_lines.append(f"With Ebook: {ebooks}")
            self._detail_meta_var.set("\n".join(summary_lines))
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text=f"{len(books)} Selected")
            first_url = (books[0].url or "").strip().lower()
            self._set_detail_buttons_state(
                has_books=bool(self._selected_urls()),
                has_http_url=first_url.startswith(("http://", "https://")),
            )
            return

        idx = indices[0]
        book = self._loaded_books[idx]
        status_label = self._book_statuses[idx] if idx < len(self._book_statuses) else ""
        self._detail_title_var.set(book.title)
        meta_lines: list[str] = []
        if book.author:
            meta_lines.append(f"Author: {book.author}")
        series_text = self._format_series_cell(book)
        if series_text:
            meta_lines.append(f"Series: {series_text}")
        fmt_text = format_catalog_formats(book)
        if fmt_text:
            meta_lines.append(f"Formats: {fmt_text}")
        meta_lines.append(f"Library: {status_label or 'Not in DB'}")
        if book.item_type and book.item_type != "book":
            meta_lines.append(f"Type: {book.item_type.capitalize()}")
        if book.url:
            meta_lines.append(f"URL: {book.url}")
        self._detail_meta_var.set("\n".join(meta_lines))

        url_clean = (book.url or "").strip()
        self._set_detail_buttons_state(
            has_books=bool(url_clean),
            has_http_url=url_clean.lower().startswith(("http://", "https://")),
        )

        cover_url = (getattr(book, "cover_url", None) or "").strip()
        if not cover_url:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Cover")
            return

        if cover_url in self._detail_cover_png_cache:
            cached_detail = self._detail_cover_png_cache[cover_url]
            self._detail_cover_png_cache.move_to_end(cover_url)
            self._apply_detail_cover(token, cover_url, cached_detail)
            return

        # Show 48px thumbnail immediately if already cached while fetching larger detail cover.
        small_photo = self._url_photos.get(cover_url)
        if small_photo is not None:
            self._detail_cover_photo = small_photo
            self._detail_cover_label.configure(image=small_photo, text="")
        else:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="Loading cover...")

        fetcher = self._cover_fetcher or self._default_fetch_cover_bytes
        detail_size = self._detail_thumb_size
        self._cb_queue.begin_task()

        def _worker() -> None:
            try:
                if token != self._detail_token:
                    return
                raw = fetcher(cover_url)
                if token != self._detail_token:
                    return
                png_bytes = (
                    resize_cover_bytes_aspect_thumbnail(raw, max_size=detail_size) if raw else None
                )
                if token == self._detail_token:
                    def _post_detail() -> None:
                        self._apply_detail_cover(token, cover_url, png_bytes, cache_result=True)

                    self._cb_queue.post(_post_detail)
            finally:
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_detail_cover(
        self,
        token: int,
        cover_url: str,
        png_bytes: bytes | None,
        *,
        cache_result: bool = False,
    ) -> None:
        if not self.winfo_exists() or token != self._detail_token:
            return
        if cache_result:
            self._lru_set(
                self._detail_cover_png_cache,
                cover_url,
                png_bytes,
                self._MAX_DETAIL_COVER_CACHE,
            )
        photo = photo_from_image_bytes(png_bytes, self) if png_bytes else None
        if photo is not None:
            self._detail_cover_photo = photo
            self._detail_cover_label.configure(image=photo, text="")
        elif cover_url in self._url_photos:
            small = self._url_photos[cover_url]
            self._detail_cover_photo = small
            self._detail_cover_label.configure(image=small, text="")
        else:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Cover")

    def _copy_selected_urls(self) -> None:
        urls = self._selected_urls()
        if not urls:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(urls))
        self._status_var.set(f"Copied {len(urls)} URL{'s' if len(urls) != 1 else ''}.")

    def _copy_selected_titles(self) -> None:
        lines = [
            f"{b.title} — {b.author}" if b.author else b.title
            for b in self._selected_books()
        ]
        if not lines:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self._status_var.set(
            f"Copied {len(lines)} book title{'s' if len(lines) != 1 else ''}."
        )

    def _open_selected_url(self) -> None:
        for url in self._selected_urls():
            if url.lower().startswith(("http://", "https://")):
                webbrowser.open(url)
                return

    def _filter_by_selected_author(self) -> None:
        books = self._selected_books()
        if not books or not books[0].author:
            return
        self._local_filter_var.set(books[0].author)
        self._clear_local_filter_btn_sync()
        self._on_filter_toggled()

    def _filter_by_selected_series(self) -> None:
        books = self._selected_books()
        if not books or not books[0].series:
            return
        self._local_filter_var.set(books[0].series)
        self._clear_local_filter_btn_sync()
        self._on_filter_toggled()

    def _clear_local_filter_btn_sync(self) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        has_text = bool(self._local_filter_var.get().strip())
        self._clear_filter_btn.configure(state="normal" if has_text else "disabled")

    def _show_context_menu(self, event: tk.Event) -> str:
        row_iid = self._tree.identify_row(event.y)
        if row_iid:
            if row_iid not in self._tree.selection():
                self._tree.selection_set(row_iid)
            self._tree.focus(row_iid)
        selected_books = self._selected_books()
        has_urls = bool(self._selected_urls())
        has_http = any(
            u.lower().startswith(("http://", "https://")) for u in self._selected_urls()
        )
        has_groups = bool(self._group_var.get()) and any(
            bool(self._tree.get_children(iid)) for iid in self._tree.get_children("")
        )
        menu = self._context_menu
        if self._on_download_urls is not None:
            menu.entryconfigure("Download Selected", state="normal" if has_urls else "disabled")
        menu.entryconfigure("Add Selected to URLs", state="normal" if has_urls else "disabled")
        if self._on_save_to_library is not None:
            menu.entryconfigure(
                "Save to Library DB",
                state="normal" if bool(self._loaded_books) else "disabled",
            )
        menu.entryconfigure(
            "Filter by Author",
            state="normal" if bool(selected_books and selected_books[0].author) else "disabled",
        )
        menu.entryconfigure(
            "Filter by Series",
            state="normal" if bool(selected_books and selected_books[0].series) else "disabled",
        )
        menu.entryconfigure("Expand All Series", state="normal" if has_groups else "disabled")
        menu.entryconfigure("Collapse All Series", state="normal" if has_groups else "disabled")
        menu.entryconfigure(
            "Copy Title & Author",
            state="normal" if bool(selected_books) else "disabled",
        )
        menu.entryconfigure("Copy URL(s)", state="normal" if has_urls else "disabled")
        menu.entryconfigure("Open URL in Browser", state="normal" if has_http else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _on_tree_double_click(self, event: tk.Event) -> None:
        row_iid = self._tree.identify_row(event.y)
        if not row_iid or not row_iid.startswith("book-"):
            return
        self._tree.selection_set(row_iid)
        self._tree.focus(row_iid)
        if self._on_download_urls is not None:
            self.download_selected()
        else:
            self.add_selected()

    def save_to_library(self) -> None:
        if self._on_save_to_library is None or not self._loaded_books:
            return
        service_key, profile = self._active_service_and_profile()
        chosen = self._selected_books() or list(self._loaded_books)
        if not chosen or not service_key:
            return
        inserted, updated = self._on_save_to_library(service_key, profile, chosen)
        self._refresh_statuses()
        self._render_tree()
        self._update_details_pane()
        self._status_var.set(f"Saved to Library DB: +{inserted} new, {updated} updated.")

    def _trigger_sync(self) -> None:
        if self._on_sync_libraries is None:
            return
        svc = self._services_by_label.get(self._service_var.get())
        self._on_sync_libraries(svc.key if svc is not None else "")

    def add_selected(self) -> None:
        urls_to_add = self._selected_urls()
        if not urls_to_add:
            self._status_var.set("Select one or more books first.")
            return
        self._on_add_urls(urls_to_add)
        self._status_var.set(f"Added {len(urls_to_add)} URL(s) to Download input.")

    def download_selected(self) -> None:
        """Queue or start downloading the selected catalog books in one click."""
        if self._on_download_urls is None:
            self.add_selected()
            return
        urls_to_download = self._selected_urls()
        if not urls_to_download:
            self._status_var.set("Select one or more books first.")
            return
        service_key, profile = self._active_service_and_profile()
        self._on_download_urls(urls_to_download, service_key, profile)


class SyncTab(ttk.Frame):
    """Tab configuring library synchronization targets, mode, and auto-sync schedule."""

    def __init__(
        self,
        parent: tk.Misc,
        config_provider: Callable[[], Config],
        services: Sequence[ServiceInfo],
        *,
        auto_sync_enabled: bool = False,
        auto_sync_minutes: int = 30,
        auto_sync_mode: str = DEFAULT_AUTO_SYNC_MODE,
        auto_sync_targets: Sequence[tuple[str, str]] | None = None,
        on_run_sync: Callable[[list[tuple[str, str]], str], None],
        on_auto_sync_changed: Callable[[bool, int], None] | None = None,
        on_save_schedule: Callable[[bool, int, str, list[tuple[str, str]]], None] | None = None,
        on_manage_accounts: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._config_provider = config_provider
        self._services = list(services)
        self._services_by_key = {s.key: s for s in self._services}
        self._on_run_sync = on_run_sync
        self._on_auto_sync_changed = on_auto_sync_changed
        self._on_save_schedule = on_save_schedule
        self._on_manage_accounts = on_manage_accounts
        self._initial_targets: set[tuple[str, str]] | None = (
            {(svc, prof) for svc, prof in auto_sync_targets}
            if auto_sync_targets is not None
            else None
        )
        self._targets_initialized = False
        self._target_vars: list[tuple[str, str, tk.BooleanVar]] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Select accounts to synchronize and choose how discovered pending audiobooks should be handled.",
            style="Muted.TLabel",
            wraplength=scale_px(self, 620),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._accounts_frame = ttk.LabelFrame(self, text="Accounts", padding=scale_px(self, 8))
        self._accounts_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        self._accounts_frame.columnconfigure(0, weight=1)
        self.refresh_targets()

        mode_frame = ttk.LabelFrame(self, text="Sync Mode", padding=scale_px(self, 8))
        mode_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        initial_mode = (
            auto_sync_mode if auto_sync_mode in VALID_AUTO_SYNC_MODES else DEFAULT_AUTO_SYNC_MODE
        )
        self.mode_var = tk.StringVar(value=initial_mode)
        ttk.Radiobutton(
            mode_frame,
            text="Scan & Enqueue Pending Books (uses main download queue)",
            variable=self.mode_var,
            value="enqueue",
        ).pack(anchor="w", pady=1)
        ttk.Radiobutton(
            mode_frame,
            text="Scan Only (update library database without downloading)",
            variable=self.mode_var,
            value="scan_only",
        ).pack(anchor="w", pady=1)
        ttk.Radiobutton(
            mode_frame,
            text="Dry Run (preview new books without modifying database)",
            variable=self.mode_var,
            value="dry_run",
        ).pack(anchor="w", pady=1)

        sched_frame = ttk.LabelFrame(self, text="Background Auto-Sync", padding=scale_px(self, 8))
        sched_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.auto_sync_var = tk.BooleanVar(value=auto_sync_enabled)
        self.interval_var = tk.StringVar(value=str(max(1, int(auto_sync_minutes))))
        ttk.Checkbutton(
            sched_frame,
            text="Auto-sync while GUI is open every",
            variable=self.auto_sync_var,
        ).pack(side=tk.LEFT)
        ttk.Spinbox(
            sched_frame,
            from_=1,
            to=1440,
            textvariable=self.interval_var,
            width=5,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(sched_frame, text="minutes").pack(side=tk.LEFT)

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, sticky="ew")
        if self._on_manage_accounts is not None:
            ttk.Button(btns, text="Manage Accounts...", command=self._on_manage_accounts).pack(
                side=tk.LEFT
            )
        ttk.Button(btns, text="Save Schedule", command=self._save_schedule).pack(side=tk.RIGHT)
        ttk.Button(
            btns,
            text="Sync Now",
            command=self._start_sync,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT, padx=(0, 8))

    def refresh_targets(self) -> None:
        prev_states = {(svc, prof): bool(var.get()) for svc, prof, var in self._target_vars}
        for child in self._accounts_frame.winfo_children():
            child.destroy()
        self._target_vars.clear()

        cfg = self._config_provider()
        configured = [
            (svc, prof)
            for svc, prof, _src_cfg in list_configured_source_profiles(cfg)
            if (svc in self._services_by_key and self._services_by_key[svc].supports_library)
        ]
        if not configured:
            configured = [
                (s.key, DEFAULT_PROFILE)
                for s in self._services
                if s.supports_library
            ]
        if not configured:
            ttk.Label(
                self._accounts_frame,
                text="No library-capable services found.",
                style="Muted.TLabel",
            ).grid(row=0, column=0, sticky="w")
            return
        for row_idx, (svc_key, prof) in enumerate(configured):
            svc_info = self._services_by_key.get(svc_key)
            label = svc_info.label if svc_info else svc_key
            if self._targets_initialized:
                checked = prev_states.get((svc_key, prof), True)
            elif self._initial_targets is not None:
                checked = (svc_key, prof) in self._initial_targets
            else:
                checked = True
            var = tk.BooleanVar(value=checked)
            self._target_vars.append((svc_key, prof, var))
            ttk.Checkbutton(
                self._accounts_frame,
                text=f"{label} ({prof})",
                variable=var,
            ).grid(row=row_idx, column=0, sticky="w", pady=2)
        self._targets_initialized = True

    def selected_targets(self) -> list[tuple[str, str]]:
        """Return currently checked `(service_key, profile)` sync target pairs."""
        return [(svc, prof) for svc, prof, var in self._target_vars if var.get()]

    def apply_saved_schedule(
        self,
        *,
        enabled: bool,
        minutes: int,
        mode: str,
        targets: Sequence[tuple[str, str]] | None,
    ) -> None:
        """Update SyncTab controls from reloaded configuration."""
        self.auto_sync_var.set(bool(enabled))
        self.interval_var.set(str(max(1, int(minutes))))
        if mode in ("enqueue", "scan_only", "dry_run"):
            self.mode_var.set(mode)
        self._initial_targets = (
            {(svc, prof) for svc, prof in targets} if targets is not None else None
        )
        self._targets_initialized = False
        self.refresh_targets()

    def _read_schedule(self) -> tuple[bool, int] | None:
        enabled = bool(self.auto_sync_var.get())
        raw = self.interval_var.get().strip() or "30"
        try:
            mins = int(raw)
        except ValueError:
            messagebox.showwarning(
                "Invalid Interval",
                "Interval must be a whole number of minutes.",
                parent=self.winfo_toplevel(),
            )
            return None
        if mins < 1:
            messagebox.showwarning(
                "Invalid Interval",
                "Interval must be at least 1 minute.",
                parent=self.winfo_toplevel(),
            )
            return None
        return enabled, mins

    def _save_schedule(self) -> None:
        sched = self._read_schedule()
        if sched is None:
            return
        targets = self.selected_targets()
        mode = self.mode_var.get()
        if self._on_save_schedule is not None:
            self._on_save_schedule(sched[0], sched[1], mode, targets)
        elif self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])

    def _start_sync(self) -> None:
        targets = [(svc, prof) for svc, prof, var in self._target_vars if var.get()]
        if not targets:
            messagebox.showwarning(
                "No Accounts Selected",
                "Select at least one account to sync.",
                parent=self.winfo_toplevel(),
            )
            return
        sched = self._read_schedule()
        if sched is None:
            return
        if self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])
        self._on_run_sync(targets, self.mode_var.get())


class LibraryPage(ttk.Frame):
    """Unified Library page housing Archive Ledger, Catalog Browser, and Library Sync sub-tabs."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        archive_path_provider: Callable[[], str],
        config_provider: Callable[[], Config],
        services: Sequence[ServiceInfo],
        selected_service_provider: Callable[[], ServiceInfo | None],
        selected_profile_provider: Callable[[], str],
        on_queue_urls: Callable[[list[str]], None],
        on_open_path: Callable[[str, bool], None],
        on_pack_paths: Callable[[list[str]], object],
        on_upload_abs: Callable[[ArchiveEntry], None],
        on_catalog_fetch: Callable[[str, ServiceInfo, str, CatalogTab], None],
        on_catalog_add_urls: Callable[[list[str]], None],
        on_catalog_download_urls: Callable[[list[str], str, str], None],
        on_save_to_library: Callable[[str, str, Sequence[CatalogBook]], tuple[int, int]],
        on_sync_from_catalog: Callable[[str], None],
        profile_resolver: Callable[[str], Sequence[str]],
        status_resolver: Callable[[str, Sequence[CatalogBook]], Sequence[str]],
        cover_fetcher: Callable[[str], bytes | None] | None = None,
        on_catalog_cancel: Callable[[], object] | None = None,
        auto_sync_enabled: bool = False,
        auto_sync_minutes: int = 30,
        auto_sync_mode: str = DEFAULT_AUTO_SYNC_MODE,
        auto_sync_targets: Sequence[tuple[str, str]] | None = None,
        on_run_sync: Callable[[list[tuple[str, str]], str], None],
        on_auto_sync_changed: Callable[[bool, int], None],
        on_save_schedule: Callable[[bool, int, str, list[tuple[str, str]]], None] | None = None,
        on_manage_account: Callable[[str, str], object] | None = None,
        on_manage_accounts: Callable[[], object] | None = None,
        on_tab_changed: Callable[[str], None] | None = None,
        palette_provider: Callable[[], ThemePalette],
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._palette_provider = palette_provider
        self._on_tab_changed = on_tab_changed
        self._suppress_tab_event = False
        self._tab_events_armed = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Library", font=make_bold_font(self, 13)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Manage downloaded audiobooks, search service catalogs, and sync remote libraries.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        palette = palette_provider()
        self.archive_tab = ArchiveTab(
            self.notebook,
            archive_path_provider(),
            on_queue_urls=on_queue_urls,
            on_open_path=on_open_path,
            on_pack_paths=on_pack_paths,
            on_upload_abs=on_upload_abs,
            palette=palette,
        )
        self.catalog_tab = CatalogTab(
            self.notebook,
            services,
            selected_service_provider(),
            on_fetch=on_catalog_fetch,
            on_add_urls=on_catalog_add_urls,
            on_download_urls=on_catalog_download_urls,
            on_save_to_library=on_save_to_library,
            on_sync_libraries=on_sync_from_catalog,
            profile_resolver=profile_resolver,
            status_resolver=status_resolver,
            cover_fetcher=cover_fetcher,
            on_cancel_fetch=on_catalog_cancel,
            on_manage_account=on_manage_account,
            config_provider=config_provider,
            selected_profile=selected_profile_provider(),
            palette=palette,
        )
        self.sync_tab = SyncTab(
            self.notebook,
            config_provider,
            services,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
            on_run_sync=on_run_sync,
            on_auto_sync_changed=on_auto_sync_changed,
            on_save_schedule=on_save_schedule,
            on_manage_accounts=on_manage_accounts,
        )

        self.notebook.add(self.archive_tab, text="Archive Ledger")
        self.notebook.add(self.catalog_tab, text="Catalog & Search")
        self.notebook.add(self.sync_tab, text="Library Sync")
        self._tabs_by_key = {
            "archive": self.archive_tab,
            "catalog": self.catalog_tab,
            "sync": self.sync_tab,
        }
        self._keys_by_tab = {str(tab): key for key, tab in self._tabs_by_key.items()}
        self.notebook.bind("<<NotebookTabChanged>>", self._handle_notebook_tab_changed, add="+")

    def current_sub_tab(self) -> str:
        """Return the active Library sub-tab key (`'archive'`, `'catalog'`, or `'sync'`)."""
        try:
            selected = str(self.notebook.select())  # type: ignore[no-untyped-call]
        except tk.TclError:
            return "archive"
        return self._keys_by_tab.get(selected, "archive")

    def select_tab(self, tab_key: str) -> None:
        tab = self._tabs_by_key.get(tab_key)
        if tab is not None:
            self._suppress_tab_event = True
            try:
                self.notebook.select(tab)  # type: ignore[no-untyped-call]
            finally:
                self._suppress_tab_event = False

    def _handle_notebook_tab_changed(self, _event: tk.Event | None = None) -> None:
        if self._suppress_tab_event or not self._tab_events_armed:
            return
        if self._on_tab_changed is not None:
            self._on_tab_changed(self.current_sub_tab())

    def on_show(self) -> None:
        """Refresh archive, catalog statuses, and sync targets whenever Library is opened."""
        self._tab_events_armed = True
        self.archive_tab.refresh()
        self.catalog_tab.sync_buttons()
        self.catalog_tab.refresh_statuses()
        self.sync_tab.refresh_targets()

    def apply_theme(self, palette: ThemePalette) -> None:
        self.archive_tab.apply_theme(palette)
        self.catalog_tab.apply_theme(palette)

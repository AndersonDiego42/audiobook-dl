"""Archive ledger tab hosting the SQLite library grid, local scanner, and book details."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import sqlite3
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import TypeVar

from audiobookdl.gui.formatting import format_duration, photo_from_image_bytes, resize_cover_file_thumbnail_bytes
from audiobookdl.gui.pages.archive_metadata_dialog import _ArchiveMetadataDialog
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_widget_tree, make_bold_font, scale_px
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ResponsiveMode,
    ScrollableFrame,
    ScrolledTreeview,
    Tooltip,
    TreeColumnSpec,
    ask_text_input,
    populate_grouped_tree,
    resolve_responsive_mode,
    resolve_selected_leaf_iids,
    update_label_wraplength,
)
from audiobookdl.utils.archive import ArchiveEntry, DownloadArchive, parse_tags_input
from audiobookdl.utils.export import export_library_entries
from audiobookdl.utils.library_scanner import LocalScanResult, scan_local_library

_T = TypeVar("_T")


@dataclass(frozen=True)
class _ArchiveQueryResult:
    has_ledger: bool
    total: int
    page_offset: int
    entries: list[ArchiveEntry]
    query_text: str
    view_trash: bool


class ArchiveTab(ttk.Frame):
    """Tab hosting the unified library grid and SQLite download ledger."""

    _PAGE_SIZE = 1000
    _TOP_BAR_WIDE_ENTER_DP = 620
    _TOP_BAR_WIDE_EXIT_DP = 590
    _BTN_BAR_WIDE_ENTER_DP = 740
    _BTN_BAR_WIDE_EXIT_DP = 700

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
        on_queue_urls: Callable[[list[str]], None] | None=None,
        on_open_path: Callable[[str, bool], None] | None=None,
        on_pack_paths: Callable[[list[str]], object] | None=None,
        on_upload_abs: Callable[[ArchiveEntry], None] | None=None,
        palette: ThemePalette | None=None,
        db_executor: ThreadPoolExecutor | None=None,
        defer_initial_refresh: bool=False,
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
        self._owns_db_executor = db_executor is None
        self._db_executor = db_executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="abdl-archive-db",
        )
        self._refresh_token = 0
        self._destroyed = False
        self._loading_ops = 0
        self._page_offset = 0
        self._total_matches = 0
        self._load_all = False
        self._sort_col: str | None = None
        self._sort_desc = False
        self._top_bar_mode: ResponsiveMode = "wide"
        self._btn_bar_mode: ResponsiveMode = "wide"

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._search_var = tk.StringVar()
        self._group_var = tk.BooleanVar(value=True)
        self._show_hidden_var = tk.BooleanVar(value=False)
        self._show_trash_var = tk.BooleanVar(value=False)
        self._status_var = tk.StringVar(
            value="Supports field filters: author:, tag:, status:, series:, -tag:hidden"
        )

        self._build_toolbar()
        self._build_tree_and_details()

        self._loading_bar = ttk.Progressbar(self, mode="indeterminate")
        self._loading_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        self._build_button_bar()
        self._build_status_bar()
        self._bind_keyboard_shortcuts()
        self._attach_tooltips()
        self._sync_selection_buttons()

        self.bind("<Destroy>", self._on_destroy)
        self._search_var.trace_add("write", self._schedule_refresh)
        self.apply_theme(self._palette)
        if not defer_initial_refresh:
            self._refresh()

    def _build_toolbar(self) -> None:
        top_bar = ttk.Frame(self)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, scale_px(self, 8)))
        self._top_bar = top_bar
        self._search_label = ttk.Label(top_bar, text="Search:")
        self._search_entry = ttk.Entry(top_bar, textvariable=self._search_var, width=28)
        self._group_cb = ttk.Checkbutton(
            top_bar,
            text="Group Series",
            variable=self._group_var,
            command=self._refresh,
        )
        self._hidden_cb = ttk.Checkbutton(
            top_bar,
            text="Show Hidden",
            variable=self._show_hidden_var,
            command=self._on_filter_changed,
        )
        self._trash_cb = ttk.Checkbutton(
            top_bar,
            text="View Trash",
            variable=self._show_trash_var,
            command=self._on_filter_changed,
        )
        self._refresh_btn = ttk.Button(top_bar, text="Refresh", command=self._refresh)
        self._details_toggle_btn = ttk.Button(
            top_bar, text="Hide Details", command=self._toggle_details_pane
        )
        self._apply_top_bar_layout("wide")
        top_bar.bind("<Configure>", self._on_top_bar_configure, add="+")

    def _on_top_bar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._top_bar.winfo_width())
        self.sync_responsive_toolbars(w)

    def _on_btn_bar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._btn_bar.winfo_width())
        self.sync_responsive_toolbars(w)

    def sync_responsive_toolbars(self, width: int | None=None) -> None:
        """Reflow archive top filter bar and action button bar for the available width."""
        w = int(width if width is not None else self.winfo_width())
        if w <= 1:
            return
        top_mode = resolve_responsive_mode(
            self._top_bar_mode,
            w,
            scale_px(self, self._TOP_BAR_WIDE_ENTER_DP),
            scale_px(self, self._TOP_BAR_WIDE_EXIT_DP),
        )
        if top_mode != self._top_bar_mode:
            self._apply_top_bar_layout(top_mode)
        btn_mode = resolve_responsive_mode(
            self._btn_bar_mode,
            w,
            scale_px(self, self._BTN_BAR_WIDE_ENTER_DP),
            scale_px(self, self._BTN_BAR_WIDE_EXIT_DP),
        )
        if btn_mode != self._btn_bar_mode:
            self._apply_btn_bar_layout(btn_mode)

    def _apply_top_bar_layout(self, mode: ResponsiveMode) -> None:
        self._top_bar_mode = mode
        for col in range(7):
            self._top_bar.columnconfigure(col, weight=0)
        p4 = scale_px(self, 4)
        p6 = scale_px(self, 6)
        p8 = scale_px(self, 8)
        self._search_label.grid(row=0, column=0, sticky="w", padx=(0, p6), pady=0)
        if mode == "wide":
            self._top_bar.columnconfigure(1, weight=1)
            self._search_entry.grid(row=0, column=1, columnspan=1, sticky="ew", padx=(0, p8), pady=0)
            self._group_cb.grid(row=0, column=2, sticky="w", padx=(0, p8), pady=0)
            self._hidden_cb.grid(row=0, column=3, sticky="w", padx=(0, p8), pady=0)
            self._trash_cb.grid(row=0, column=4, sticky="w", padx=(0, p8), pady=0)
            self._refresh_btn.grid(row=0, column=5, sticky="e", padx=(0, p6), pady=0)
            self._details_toggle_btn.grid(row=0, column=6, sticky="e", padx=0, pady=0)
        else:
            self._top_bar.columnconfigure(1, weight=1)
            self._search_entry.grid(row=0, column=1, columnspan=4, sticky="ew", padx=(0, p6), pady=0)
            self._refresh_btn.grid(row=0, column=5, sticky="e", padx=(0, p6), pady=0)
            self._details_toggle_btn.grid(row=0, column=6, sticky="e", padx=0, pady=0)
            self._group_cb.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, p8), pady=(p4, 0))
            self._hidden_cb.grid(row=1, column=2, columnspan=2, sticky="w", padx=(0, p8), pady=(p4, 0))
            self._trash_cb.grid(row=1, column=4, columnspan=3, sticky="w", padx=0, pady=(p4, 0))

    def _build_tree_and_details(self) -> None:
        self._split = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self._split.grid(row=1, column=0, sticky="nsew")

        self._scrolled_tree = ScrolledTreeview(
            self._split,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
            on_sort_column=self._on_column_sort,
        )
        self._split.add(self._scrolled_tree, weight=3)
        self._tree = self._scrolled_tree.tree
        self._tree.bind("<<TreeviewSelect>>", self._on_entry_selected)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._show_context_menu)
        if sys.platform == "darwin":
            self._tree.bind("<Button-2>", self._show_context_menu)
        self._build_context_menu()

        self._details_pane = ttk.LabelFrame(
            self._split, text="Book Details", padding=scale_px(self, 6)
        )
        self._details_pane.columnconfigure(0, weight=1)
        self._details_pane.rowconfigure(0, weight=1)
        self._details_scroll = ScrollableFrame(
            self._details_pane,
            content_padding=(2, 2),
        )
        self._details_scroll.grid(row=0, column=0, sticky="nsew")
        details_body = self._details_scroll.content_frame
        details_body.columnconfigure(0, weight=1)

        self._detail_cover_label = ttk.Label(details_body, text="No Cover", anchor="center")
        self._detail_cover_label.grid(row=0, column=0, pady=(0, scale_px(self, 8)))
        self._detail_title_var = tk.StringVar(value="Select a book")
        self._detail_meta_var = tk.StringVar(value="")
        self._detail_title_label = ttk.Label(
            details_body,
            textvariable=self._detail_title_var,
            font=make_bold_font(self, 1),
            wraplength=scale_px(self, 220),
            justify="left",
        )
        self._detail_title_label.grid(row=1, column=0, sticky="w", pady=(0, scale_px(self, 6)))
        self._detail_meta_label = ttk.Label(
            details_body,
            textvariable=self._detail_meta_var,
            wraplength=scale_px(self, 220),
            justify="left",
        )
        self._detail_meta_label.grid(row=2, column=0, sticky="nw")
        self._copy_details_btn = ttk.Button(
            details_body,
            text="Copy Details",
            command=self._copy_detail_summary,
            state="disabled",
        )
        self._copy_details_btn.grid(row=3, column=0, sticky="w", pady=(scale_px(self, 8), 0))
        self._details_pane.bind("<Configure>", self._on_details_pane_configure, add="+")
        self._split.add(self._details_pane, weight=1)

    def _on_details_pane_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._details_pane.winfo_width())
        if w <= 1:
            return
        min_w = scale_px(self, 160)
        target_w = w - scale_px(self, 32)
        update_label_wraplength(self._detail_title_label, target_w, min_px=min_w)
        update_label_wraplength(self._detail_meta_label, target_w, min_px=min_w)

    def _build_button_bar(self) -> None:
        btn_bar = ttk.Frame(self)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(scale_px(self, 8), 0))
        self._btn_bar = btn_bar
        self._action_buttons: list[ttk.Button] = []
        self._add_urls_btn: ttk.Button | None = None
        if self._on_queue_urls is not None:
            self._add_urls_btn = ttk.Button(
                btn_bar, text="Add to URLs", command=self._queue_selected, state="disabled"
            )
            self._action_buttons.append(self._add_urls_btn)
        self._edit_tags_btn = ttk.Button(
            btn_bar, text="Edit Tags...", command=self._edit_selected_tags, state="disabled"
        )
        self._edit_meta_btn = ttk.Button(
            btn_bar,
            text="Edit Metadata...",
            command=self._edit_selected_metadata,
            state="disabled",
        )
        self._hide_btn = ttk.Button(
            btn_bar, text="Hide / Unhide", command=self._toggle_hide_selected, state="disabled"
        )
        self._trash_btn = ttk.Button(
            btn_bar, text="Trash / Restore", command=self._toggle_trash_selected, state="disabled"
        )
        self._remove_btn = ttk.Button(
            btn_bar, text="Remove Selected", command=self._remove_selected, state="disabled"
        )
        self._action_buttons.extend(
            [
                self._edit_tags_btn,
                self._edit_meta_btn,
                self._hide_btn,
                self._trash_btn,
                self._remove_btn,
            ]
        )
        self._scan_btn = ttk.Button(
            btn_bar, text="Locate Books...", command=self._prompt_locate_local_books
        )
        self._verify_btn = ttk.Button(
            btn_bar, text="Verify Missing", command=self._verify_missing_files
        )
        self._export_btn = ttk.Button(btn_bar, text="Export...", command=self._prompt_export)
        self._clear_all_btn = ttk.Button(btn_bar, text="Clear All", command=self._clear_all)
        self._action_buttons.extend(
            [
                self._scan_btn,
                self._verify_btn,
                self._export_btn,
                self._clear_all_btn,
            ]
        )
        self._apply_btn_bar_layout("wide")
        btn_bar.bind("<Configure>", self._on_btn_bar_configure, add="+")

    def _apply_btn_bar_layout(self, mode: ResponsiveMode) -> None:
        self._btn_bar_mode = mode
        p4 = scale_px(self, 4)
        p6 = scale_px(self, 6)
        btns = self._action_buttons
        if mode == "wide":
            for idx, btn in enumerate(btns):
                btn.grid(
                    row=0,
                    column=idx,
                    sticky="w",
                    padx=(0, p6) if idx < len(btns) - 1 else 0,
                    pady=0,
                )
            return
        split_at = (len(btns) + 1) // 2
        for idx, btn in enumerate(btns):
            row = 0 if idx < split_at else 1
            col = idx if idx < split_at else idx - split_at
            is_last_in_row = (idx == split_at - 1) or (idx == len(btns) - 1)
            btn.grid(
                row=row,
                column=col,
                sticky="w",
                padx=(0, 0 if is_last_in_row else p6),
                pady=(p4, 0) if row == 1 else 0,
            )

    def _build_status_bar(self) -> None:
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

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("downloaded", foreground=palette.success)
        self._tree.tag_configure("pending", foreground=palette.warning)
        self._tree.tag_configure("hidden", foreground=palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=palette.error)
        self._details_scroll.apply_theme(palette)
        apply_theme_to_widget_tree(self._context_menu, palette)

    def set_archive_path(self, archive_path: str) -> None:
        """Update ledger file path when `database_directory` changes cleanly."""
        if archive_path == self._archive_path:
            return
        self._archive_path = archive_path
        self._refresh()

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
            self._detail_token += 1
            if self._detail_debounce_after_id is not None:
                try:
                    self.after_cancel(self._detail_debounce_after_id)
                except tk.TclError:
                    pass
                self._detail_debounce_after_id = None
        else:
            self._split.add(self._details_pane, weight=1)
            self._details_visible = True
            self._details_toggle_btn.configure(text="Hide Details")
            self._on_entry_selected()

    def _sync_selection_buttons(self) -> None:
        entries = self._selected_entries()
        has_sel = bool(entries)
        single = len(entries) == 1
        any_url = any(bool(e.url) for e in entries)
        has_rows = bool(self._entries_by_id)
        has_backing = self._has_archive_backing()
        if self._add_urls_btn is not None:
            self._add_urls_btn.configure(state="normal" if any_url else "disabled")
        self._edit_tags_btn.configure(state="normal" if (has_sel and has_backing) else "disabled")
        self._edit_meta_btn.configure(state="normal" if (single and has_backing) else "disabled")
        self._hide_btn.configure(state="normal" if (has_sel and has_backing) else "disabled")
        self._trash_btn.configure(state="normal" if (has_sel and has_backing) else "disabled")
        self._remove_btn.configure(state="normal" if (has_sel and has_backing) else "disabled")
        self._verify_btn.configure(state="normal" if has_backing else "disabled")
        self._export_btn.configure(state="normal" if has_rows else "disabled")
        self._clear_all_btn.configure(state="normal" if (has_rows and has_backing) else "disabled")
        if hasattr(self, "_copy_details_btn"):
            self._copy_details_btn.configure(state="normal" if has_sel else "disabled")

    def _on_column_sort(self, col_key: str, descending: bool) -> None:
        self._sort_col = col_key
        self._sort_desc = descending
        self._page_offset = 0
        self._refresh()

    def _collect_leaf_iids(self, parent_iid: str = "") -> list[str]:
        leaves: list[str] = []
        for iid in self._tree.get_children(parent_iid):
            if self._tree.get_children(iid):
                leaves.extend(self._collect_leaf_iids(iid))
            else:
                leaves.append(iid)
        return leaves

    def _bind_keyboard_shortcuts(self) -> None:
        def _focus_search(_e: tk.Event) -> str:
            self._search_entry.focus_set()
            self._search_entry.selection_range(0, tk.END)
            return "break"

        def _clear_search(_e: tk.Event) -> str | None:
            if self._search_var.get():
                self._search_var.set("")
                return "break"
            return None

        def _select_all_rows(_e: tk.Event) -> str:
            leaves = self._collect_leaf_iids()
            if leaves:
                self._tree.selection_set(leaves)
                self._tree.focus(leaves[0])
            return "break"

        def _on_delete(_e: tk.Event) -> str:
            self._remove_selected()
            return "break"

        def _on_return(_e: tk.Event) -> str:
            self._open_selected_output(reveal=False)
            return "break"

        def _on_f2(_e: tk.Event) -> str:
            self._edit_selected_metadata()
            return "break"

        for target in (self, self._tree):
            target.bind("<Control-f>", _focus_search, add="+")
            target.bind("<Control-F>", _focus_search, add="+")
        self._search_entry.bind("<Escape>", _clear_search, add="+")
        self._tree.bind("<Control-a>", _select_all_rows, add="+")
        self._tree.bind("<Control-A>", _select_all_rows, add="+")
        self._tree.bind("<Delete>", _on_delete, add="+")
        self._tree.bind("<Return>", _on_return, add="+")
        self._tree.bind("<F2>", _on_f2, add="+")
        for seq in ("<Shift-F10>", "<Menu>"):
            try:
                self._tree.bind(seq, self._show_context_menu_from_key, add="+")
            except tk.TclError:
                pass

    def _attach_tooltips(self) -> None:
        pal = lambda: self._palette
        self._tooltips = [
            Tooltip(
                self._search_entry,
                "Search title, author, series, or filters: author:, tag:, status:, series:, -tag:hidden (Ctrl+F)",
                palette_provider=pal,
            ),
            Tooltip(
                self._group_cb,
                "Group books sharing the same series under collapsible rows",
                palette_provider=pal,
            ),
            Tooltip(
                self._hidden_cb,
                "Include books marked hidden from the default view",
                palette_provider=pal,
            ),
            Tooltip(
                self._trash_cb,
                "Show books moved to trash instead of active entries",
                palette_provider=pal,
            ),
            Tooltip(
                self._verify_btn,
                "Check whether downloaded output paths still exist on disk and mark missing files pending",
                palette_provider=pal,
            ),
        ]

    def _copy_detail_summary(self) -> None:
        title = self._detail_title_var.get().strip()
        meta = self._detail_meta_var.get().strip()
        text = "\n\n".join(part for part in (title, meta) if part)
        if not text or title == "Select a book":
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._status_var.set("Copied book details to clipboard.")

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
        menu.add_command(label="Hide / Unhide", command=self._toggle_hide_selected)
        menu.add_command(label="Trash / Restore", command=self._toggle_trash_selected)
        menu.add_command(label="Remove Selected", command=self._remove_selected)
        menu.add_command(label="Verify Missing Files", command=self._verify_missing_files)
        menu.add_separator()
        menu.add_command(label="Copy URL", command=lambda: self._copy_selected_field("url"))
        menu.add_command(
            label="Copy Output Path", command=lambda: self._copy_selected_field("output_path")
        )
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
        is_cancelled: Callable[[], bool] | None=None,
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

    def _on_entry_selected(self, _event: tk.Event | None=None) -> None:
        self._sync_selection_buttons()
        self._detail_token += 1
        token = self._detail_token
        if self._detail_debounce_after_id is not None:
            try:
                self.after_cancel(self._detail_debounce_after_id)
            except tk.TclError:
                pass
            self._detail_debounce_after_id = None
        if not self._details_visible:
            return
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
        row_iid = self._tree.identify_row(event.y)
        if not row_iid or self._tree.get_children(row_iid):
            return
        self._open_selected_output(reveal=False)

    def _show_context_menu_from_key(self, _event: tk.Event | None = None) -> str:
        sel = self._tree.selection()
        focus_iid = self._tree.focus() or (sel[0] if sel else "")
        try:
            bbox = self._tree.bbox(focus_iid) if focus_iid else None
            if bbox and len(bbox) == 4:
                x_root = self._tree.winfo_rootx() + bbox[0] + 16
                y_root = self._tree.winfo_rooty() + bbox[1] + bbox[3]
            else:
                x_root = self._tree.winfo_rootx() + 16
                y_root = self._tree.winfo_rooty() + 16
        except tk.TclError:
            return "break"
        return self._popup_context_menu_at(x_root, y_root)

    def _show_context_menu(self, event: tk.Event) -> str:
        row_iid = self._tree.identify_row(event.y)
        if row_iid and row_iid not in self._tree.selection():
            self._tree.selection_set(row_iid)
            self._tree.focus(row_iid)
        return self._popup_context_menu_at(event.x_root, event.y_root)

    def _popup_context_menu_at(self, x_root: int, y_root: int) -> str:
        entries = self._selected_entries()
        single = entries[0] if len(entries) == 1 else None
        has_sel = bool(entries)
        has_backing = self._has_archive_backing()
        has_path = bool(single and single.output_path and os.path.exists(single.output_path))
        any_path = any(bool(e.output_path and os.path.exists(e.output_path)) for e in entries)
        any_url = any(bool(e.url) for e in entries)
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
            state="normal" if (any_path and self._on_pack_paths is not None) else "disabled",
        )
        if self._on_queue_urls is not None:
            self._context_menu.entryconfigure(
                "Add to URLs",
                state="normal" if any_url else "disabled",
            )
        self._context_menu.entryconfigure(
            "Edit Metadata...",
            state="normal" if (single is not None and has_backing) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Edit Tags...",
            state="normal" if (has_sel and has_backing) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Hide / Unhide",
            state="normal" if (has_sel and has_backing) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Trash / Restore",
            state="normal" if (has_sel and has_backing) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Remove Selected",
            state="normal" if (has_sel and has_backing) else "disabled",
        )
        self._context_menu.entryconfigure(
            "Copy URL",
            state="normal" if any_url else "disabled",
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
            self._context_menu.tk_popup(x_root, y_root)
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
        paths = [
            e.output_path
            for e in self._selected_entries()
            if e.output_path and os.path.exists(e.output_path)
        ]
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
        archive_path = self._archive_path
        if not os.path.exists(archive_path):
            return
        self._verify_btn.configure(state="disabled")
        self._status_var.set("Verifying library paths...")
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params(archive_path)

        def _work() -> tuple[int, _ArchiveQueryResult]:
            ledger = self._get_worker_ledger(archive_path, create=False)
            missing = 0
            if ledger is not None:
                with ledger.batch_connection():
                    for entry in ledger.list_entries(
                        limit=100000, include_trashed=False, include_hidden=True
                    ):
                        if (
                            entry.status == "downloaded"
                            and entry.output_path
                            and not os.path.exists(entry.output_path)
                        ):
                            ledger.set_status(entry.id, "pending")
                            missing += 1
            return missing, self._query_page_sync(*params)

        def _on_done(payload: tuple[int, _ArchiveQueryResult]) -> None:
            missing, query_res = payload
            self._verify_btn.configure(state="normal")
            if token == self._refresh_token:
                self._apply_refresh_result(query_res)
            self._status_var.set(
                f"Verified library paths: marked {missing} missing file(s) as pending."
            )

        def _on_err(exc: Exception) -> None:
            self._verify_btn.configure(state="normal")
            self._status_var.set(f"Verify error: {exc}")

        self._submit_db_task(_work, _on_done, on_error=_on_err, show_loading=True)

    def _get_ledger(self, *, create: bool=False) -> DownloadArchive | None:
        return self._get_worker_ledger(self._archive_path, create=create)

    def _get_worker_ledger(
        self, archive_path: str, *, create: bool=False
    ) -> DownloadArchive | None:
        abs_path = os.path.abspath(archive_path)
        if self._ledger is not None and self._ledger.db_path != abs_path:
            try:
                self._ledger.close()
            except sqlite3.Error:
                pass
            self._ledger = None
        if self._ledger is None:
            if not create and not os.path.exists(archive_path):
                return None
            self._ledger = DownloadArchive(archive_path, persistent=True)
        return self._ledger

    def _close_worker_ledger(self) -> None:
        if self._ledger is not None:
            try:
                self._ledger.close()
            except sqlite3.Error:
                pass
            self._ledger = None

    def _submit_db_task(
        self,
        work: Callable[[], _T],
        on_success: Callable[[_T], None],
        *,
        on_error: Callable[[Exception], None] | None=None,
        show_loading: bool=False,
    ) -> None:
        if self._destroyed:
            return
        if show_loading:
            self._begin_loading()
        self._cb_queue.begin_task()

        def _runner() -> None:
            try:
                result = work()
            except Exception as exc:
                err_exc = exc

                def _post_err() -> None:
                    if self._destroyed or not self.winfo_exists():
                        return
                    if show_loading:
                        self._end_loading()
                    if on_error is not None:
                        on_error(err_exc)
                    else:
                        self._status_var.set(f"Database error: {err_exc}")

                self._cb_queue.post(_post_err)
            else:

                def _post_ok() -> None:
                    if self._destroyed or not self.winfo_exists():
                        return
                    if show_loading:
                        self._end_loading()
                    on_success(result)

                self._cb_queue.post(_post_ok)
            finally:
                self._cb_queue.finish_worker()

        try:
            self._db_executor.submit(_runner)
        except RuntimeError:
            self._cb_queue.finish_worker()
            if show_loading and self.winfo_exists():
                self._end_loading()

    def _snapshot_query_params(
        self, archive_path: str | None=None
    ) -> tuple[str, bool, bool, str, int, bool, int, str, bool]:
        sort_by = self._sort_col or "id"
        descending = self._sort_desc if self._sort_col is not None else True
        return (
            archive_path if archive_path is not None else self._archive_path,
            bool(self._show_trash_var.get()),
            bool(self._show_hidden_var.get()),
            self._search_var.get(),
            self._page_offset,
            self._load_all,
            self._PAGE_SIZE,
            sort_by,
            descending,
        )

    def _query_page_sync(
        self,
        archive_path: str,
        view_trash: bool,
        show_hidden: bool,
        query_text: str,
        page_offset: int,
        load_all: bool,
        page_size: int,
        sort_by: str = "id",
        descending: bool = True,
    ) -> _ArchiveQueryResult:
        ledger = self._get_worker_ledger(archive_path, create=False)
        if ledger is None:
            return _ArchiveQueryResult(
                has_ledger=False,
                total=0,
                page_offset=0,
                entries=[],
                query_text=query_text,
                view_trash=view_trash,
            )
        status_filter = "trashed" if view_trash else None
        total = ledger.count_entries(
            query=query_text,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status=status_filter,
        )
        resolved_offset = page_offset
        if total == 0 or load_all:
            resolved_offset = 0
        elif resolved_offset >= total:
            resolved_offset = max(0, ((total - 1) // page_size) * page_size)

        query_limit = max(total, 1) if load_all else page_size
        entries = ledger.list_entries(
            query=query_text,
            limit=query_limit,
            offset=resolved_offset,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status=status_filter,
            sort_by=sort_by,
            descending=descending,
        )
        return _ArchiveQueryResult(
            has_ledger=True,
            total=total,
            page_offset=resolved_offset,
            entries=entries,
            query_text=query_text,
            view_trash=view_trash,
        )

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
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token != self._refresh_token:
                return
            self._apply_refresh_result(result)

        def _on_err(exc: Exception) -> None:
            if token != self._refresh_token:
                return
            self._status_var.set(f"Database error: {exc}")

        self._submit_db_task(_work, _on_done, on_error=_on_err)

    def _apply_refresh_result(self, result: _ArchiveQueryResult) -> None:
        existing = self._tree.get_children()
        if existing:
            self._tree.delete(*existing)
        self._entries_by_id.clear()
        if not result.has_ledger:
            self._total_matches = 0
            self._page_offset = 0
            self._pager_frame.grid_remove()
            self._status_var.set("No library database yet.")
            self._scrolled_tree.set_empty_state(
                True,
                "No library entries yet. Download audiobooks or scan local folders.",
                action_text="Locate Books...",
                action_command=self._prompt_locate_local_books,
            )
            self._scrolled_tree.set_sort_indicator(self._sort_col, self._sort_desc)
            self._sync_selection_buttons()
            return

        total = result.total
        self._total_matches = total
        self._page_offset = result.page_offset
        entries = result.entries
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
        query_text = result.query_text
        view_trash = result.view_trash
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
        self._scrolled_tree.set_sort_indicator(self._sort_col, self._sort_desc)
        self._sync_selection_buttons()

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

    def _has_archive_backing(self) -> bool:
        return self._ledger is not None or os.path.exists(self._archive_path)

    def _edit_selected_tags(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids or not self._has_archive_backing():
            return
        entries = [self._entries_by_id[eid] for eid in selected_ids if eid in self._entries_by_id]
        if not entries:
            return
        updates: list[tuple[int, list[str]]] = []
        if len(entries) == 1:
            initial = " ".join(entries[0].tags)
            prompt = "Space-separated tags (letters, numbers, underscores; use 'hidden' to hide):"
            raw = ask_text_input(
                self.winfo_toplevel(),
                "Edit Tags",
                prompt,
                initialvalue=initial,
                palette=self._palette,
                askstring_override=simpledialog.askstring,
            )
            if raw is None:
                return
            updates.append((entries[0].id, parse_tags_input(raw)))
        else:
            tag_sets = [set(e.tags) for e in entries]
            common_set = set.intersection(*tag_sets) if tag_sets else set()
            common_ordered = [t for t in entries[0].tags if t in common_set]
            raw = ask_text_input(
                self.winfo_toplevel(),
                "Edit Tags (Multi-Select)",
                f"Editing common tags across {len(entries)} books (distinct per-book tags are preserved):",
                initialvalue=" ".join(common_ordered),
                palette=self._palette,
                askstring_override=simpledialog.askstring,
            )
            if raw is None:
                return
            parsed = parse_tags_input(raw)
            parsed_set = set(parsed)
            added = [t for t in parsed if t not in common_set]
            removed = common_set - parsed_set
            for entry in entries:
                kept = [t for t in entry.tags if t not in removed]
                for tag in added:
                    if tag not in kept:
                        kept.append(tag)
                updates.append((entry.id, kept))

        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            ledger = self._get_worker_ledger(params[0], create=False)
            if ledger is not None:
                with ledger.batch_connection():
                    for entry_id, tags in updates:
                        ledger.set_tags(entry_id, tags)
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token == self._refresh_token:
                self._apply_refresh_result(result)

        self._submit_db_task(_work, _on_done)

    def _edit_selected_metadata(self) -> None:
        entries = self._selected_entries()
        if len(entries) != 1 or not self._has_archive_backing():
            return
        entry = entries[0]
        dlg = _ArchiveMetadataDialog(self, entry, palette=self._palette)
        self.wait_window(dlg)
        if dlg.result_updates is None:
            return
        result_updates = dict(dlg.result_updates)
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> tuple[ArchiveEntry | None, _ArchiveQueryResult]:
            ledger = self._get_worker_ledger(params[0], create=False)
            updated = ledger.update_entry(entry.id, **result_updates) if ledger is not None else None
            return updated, self._query_page_sync(*params)

        def _on_done(payload: tuple[ArchiveEntry | None, _ArchiveQueryResult]) -> None:
            updated, query_res = payload
            if token == self._refresh_token:
                self._apply_refresh_result(query_res)
            if updated is not None and self._tree.exists(str(updated.id)):
                self._tree.selection_set(str(updated.id))
                self._tree.focus(str(updated.id))
                self._on_entry_selected()
                self._status_var.set(f"Updated metadata for '{updated.title}'.")

        self._submit_db_task(_work, _on_done)

    def _toggle_hide_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids or not self._has_archive_backing():
            return
        ops = [
            (entry_id, bool((entry := self._entries_by_id.get(entry_id)) is not None and entry.is_hidden))
            for entry_id in selected_ids
        ]
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            ledger = self._get_worker_ledger(params[0], create=False)
            if ledger is not None:
                with ledger.batch_connection():
                    for entry_id, is_hidden in ops:
                        if is_hidden:
                            ledger.remove_tags(entry_id, ["hidden"])
                        else:
                            ledger.add_tags(entry_id, ["hidden"])
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token == self._refresh_token:
                self._apply_refresh_result(result)

        self._submit_db_task(_work, _on_done)

    def _toggle_trash_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids or not self._has_archive_backing():
            return
        ops: list[tuple[int, str]] = []
        for entry_id in selected_ids:
            entry = self._entries_by_id.get(entry_id)
            if entry is None:
                continue
            if entry.status == "trashed":
                ops.append((entry_id, "downloaded" if entry.output_path else "pending"))
            else:
                ops.append((entry_id, "trashed"))
        if not ops:
            return
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            ledger = self._get_worker_ledger(params[0], create=False)
            if ledger is not None:
                with ledger.batch_connection():
                    for entry_id, new_status in ops:
                        ledger.set_status(entry_id, new_status)
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token == self._refresh_token:
                self._apply_refresh_result(result)

        self._submit_db_task(_work, _on_done)

    def _remove_selected(self) -> None:
        selected_ids = self._selected_entry_ids()
        if not selected_ids or not self._has_archive_backing():
            return
        count = len(selected_ids)
        noun = "entry" if count == 1 else "entries"
        if not messagebox.askyesno(
            "Remove Selected",
            f"Permanently delete {count} selected {noun} from the download history ledger?",
            parent=self.winfo_toplevel(),
        ):
            return
        for entry_id in selected_ids:
            if self._tree.exists(str(entry_id)):
                self._tree.delete(str(entry_id))
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            ledger = self._get_worker_ledger(params[0], create=False)
            if ledger is not None:
                with ledger.batch_connection():
                    for entry_id in selected_ids:
                        ledger.delete_entry(entry_id)
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token == self._refresh_token:
                self._apply_refresh_result(result)

        self._submit_db_task(_work, _on_done)

    def _clear_all(self) -> None:
        if not self._has_archive_backing():
            return
        if not messagebox.askyesno(
            "Clear Ledger",
            "Delete all entries from the download history ledger?",
            parent=self.winfo_toplevel(),
        ):
            return
        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params()

        def _work() -> _ArchiveQueryResult:
            ledger = self._get_worker_ledger(params[0], create=False)
            if ledger is not None:
                ledger.clear()
            return self._query_page_sync(*params)

        def _on_done(result: _ArchiveQueryResult) -> None:
            if token == self._refresh_token:
                self._apply_refresh_result(result)

        self._submit_db_task(_work, _on_done)

    def _prompt_locate_local_books(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            title="Select Local Audiobook Folder to Scan",
        )
        if not folder:
            return
        self.scan_folder(folder, background=True)

    def scan_folder(self, folder: str, *, background: bool=False) -> LocalScanResult | None:
        """Scan `folder` into `downloads.db` and refresh the library grid."""
        self._scan_btn.configure(state="disabled")
        self._status_var.set(f"Scanning '{folder}'...")
        archive_path = self._archive_path

        def _scan_status(result: LocalScanResult | None) -> None:
            if result is not None:
                self._status_var.set(
                    f"Scan complete: {result.scanned_books} book(s) checked, "
                    f"{result.matched} matched, {result.registered} newly registered, "
                    f"{result.missing} missing."
                )

        if not background:
            ledger = self._get_worker_ledger(archive_path, create=True)
            assert ledger is not None
            res = scan_local_library([folder], ledger, verify_missing=True)
            query_res = self._query_page_sync(*self._snapshot_query_params(archive_path))
            self._scan_btn.configure(state="normal")
            self._apply_refresh_result(query_res)
            _scan_status(res)
            return res

        self._refresh_token += 1
        token = self._refresh_token
        params = self._snapshot_query_params(archive_path)

        def _work() -> tuple[LocalScanResult, _ArchiveQueryResult]:
            ledger = self._get_worker_ledger(archive_path, create=True)
            assert ledger is not None
            res = scan_local_library([folder], ledger, verify_missing=True)
            return res, self._query_page_sync(*params)

        def _on_done(payload: tuple[LocalScanResult, _ArchiveQueryResult]) -> None:
            res, query_res = payload
            self._scan_btn.configure(state="normal")
            if token == self._refresh_token:
                self._apply_refresh_result(query_res)
            _scan_status(res)

        def _on_err(exc: Exception) -> None:
            self._scan_btn.configure(state="normal")
            self._status_var.set(f"Scan error: {exc}")

        self._submit_db_task(_work, _on_done, on_error=_on_err, show_loading=True)
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
        archive_path = self._archive_path
        query_text = self._search_var.get()
        view_trash = bool(self._show_trash_var.get())
        show_hidden = bool(self._show_hidden_var.get())
        self._status_var.set("Exporting library ledger...")

        def _work() -> int:
            ledger = self._get_worker_ledger(archive_path, create=False)
            if ledger is None:
                return export_library_entries([], output_path)
            entries = ledger.list_entries(
                query=query_text,
                limit=100000,
                include_trashed=view_trash,
                include_hidden=show_hidden,
                status="trashed" if view_trash else None,
            )
            return export_library_entries(entries, output_path)

        def _on_done(count: int) -> None:
            self._status_var.set(f"Exported {count} book(s) to {output_path}")

        def _on_err(exc: Exception) -> None:
            self._status_var.set("Export failed.")
            messagebox.showerror("Export Error", str(exc), parent=self.winfo_toplevel())

        self._submit_db_task(_work, _on_done, on_error=_on_err, show_loading=True)

    def export_to_path(self, output_path: str) -> int:
        """Export currently filtered library entries to `output_path`."""
        ledger = self._get_worker_ledger(self._archive_path, create=False)
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
        self._destroyed = True
        self._detail_token += 1
        self._refresh_token += 1
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
        try:
            self._db_executor.submit(self._close_worker_ledger)
        except RuntimeError:
            self._close_worker_ledger()
        if self._owns_db_executor:
            self._db_executor.shutdown(wait=(self._loading_ops == 0), cancel_futures=False)

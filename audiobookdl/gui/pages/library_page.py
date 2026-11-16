"""Modal dialog displaying and managing the unified library and SQLite download ledger."""

import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from collections.abc import Callable

from audiobookdl.gui.formatting import format_duration, load_cover_from_file, render_cover_thumbnail
from audiobookdl.gui.theme import (
    LIGHT_PALETTE,
    ThemePalette,
    apply_theme_to_window,
    make_bold_font,
    scale_px,
)
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ScrolledTreeview,
    TreeColumnSpec,
    populate_grouped_tree,
    resolve_selected_leaf_iids,
)
from audiobookdl.utils.archive import ArchiveEntry, DownloadArchive, parse_tags_input
from audiobookdl.utils.export import export_library_entries
from audiobookdl.utils.library_scanner import LocalScanResult, scan_local_library


class ArchiveLedgerDialog(tk.Toplevel):
    """Modal `Toplevel` window hosting the unified library grid and download ledger."""

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
        super().__init__(parent)
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

        self.title("Library & Download Ledger")
        self.transient(parent)  # type: ignore[arg-type]
        self.geometry(f"{scale_px(self, 1040)}x{scale_px(self, 520)}")
        self.minsize(scale_px(self, 720), scale_px(self, 360))
        self.bind("<Escape>", lambda _e: self.destroy())

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self._search_var = tk.StringVar()
        self._group_var = tk.BooleanVar(value=True)
        self._show_hidden_var = tk.BooleanVar(value=False)
        self._show_trash_var = tk.BooleanVar(value=False)
        self._status_var = tk.StringVar(value="Supports field filters: author:, tag:, status:, series:, -tag:hidden")

        top_bar = ttk.Frame(body)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(top_bar, textvariable=self._search_var, width=34)
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
            command=self._refresh,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            top_bar,
            text="View Trash",
            variable=self._show_trash_var,
            command=self._refresh,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(top_bar, text="Refresh", command=self._refresh).pack(side=tk.LEFT, padx=(0, 6))
        self._details_toggle_btn = ttk.Button(
            top_bar, text="Hide Details", command=self._toggle_details_pane
        )
        self._details_toggle_btn.pack(side=tk.LEFT)

        self._split = ttk.PanedWindow(body, orient=tk.HORIZONTAL)
        self._split.grid(row=1, column=0, sticky="nsew")

        self._scrolled_tree = ScrolledTreeview(
            self._split,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
        )
        self._split.add(self._scrolled_tree, weight=3)
        self._tree = self._scrolled_tree.tree
        self._tree.tag_configure("downloaded", foreground=self._palette.success)
        self._tree.tag_configure("pending", foreground=self._palette.warning)
        self._tree.tag_configure("hidden", foreground=self._palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=self._palette.error)
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
            wraplength=scale_px(self, 230),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(
            self._details_pane,
            textvariable=self._detail_meta_var,
            wraplength=scale_px(self, 230),
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="nw")
        self._split.add(self._details_pane, weight=1)

        self._loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self._loading_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        if self._on_queue_urls is not None:
            ttk.Button(btn_bar, text="Queue Selected", command=self._queue_selected).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        ttk.Button(btn_bar, text="Edit Tags...", command=self._edit_selected_tags).pack(
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
        ttk.Button(btn_bar, text="Verify Missing", command=self._verify_missing_files).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Export...", command=self._prompt_export).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Clear All", command=self._clear_all).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        ttk.Label(body, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )

        self.bind("<Destroy>", self._on_destroy)
        self._search_var.trace_add("write", self._schedule_refresh)
        self._refresh()
        apply_theme_to_window(self, self._palette)

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
            label="Repackage in Pack Dialog...",
            command=self._repackage_selected,
        )
        menu.add_separator()
        if self._on_queue_urls is not None:
            menu.add_command(label="Queue Selected", command=self._queue_selected)
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
    def _format_size(path: str) -> str:
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
            elif os.path.isdir(path):
                size = sum(
                    os.path.getsize(os.path.join(r, fn))
                    for r, _d, files in os.walk(path)
                    for fn in files
                )
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

    def _on_entry_selected(self, _event: tk.Event | None = None) -> None:
        entries = self._selected_entries()
        if not entries:
            self._detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Selection")
            self._detail_title_var.set("Select a book")
            self._detail_meta_var.set("")
            return
        e = entries[0]
        self._detail_title_var.set(e.title)
        lines: list[str] = [
            f"Status: {self._format_status(e)}",
            f"Service: {self._format_service(e)}",
        ]
        if e.author:
            lines.append(f"Author: {e.author}")
        if e.narrator:
            lines.append(f"Narrator: {e.narrator}")
        if e.series:
            s_txt = f"{e.series} #{e.series_order}" if e.series_order is not None else e.series
            lines.append(f"Series: {s_txt}")
        if e.duration_seconds:
            lines.append(f"Duration: {format_duration(e.duration_seconds)}")
        if e.audio_format:
            lines.append(f"Format: {e.audio_format}")
        if e.asin:
            lines.append(f"ASIN: {e.asin}")
        if e.isbn:
            lines.append(f"ISBN: {e.isbn}")
        if e.tags:
            lines.append(f"Tags: {', '.join(e.tags)}")
        if e.output_path:
            lines.append(f"Size: {self._format_size(e.output_path)}")
            lines.append(f"Path: {e.output_path}")
        self._detail_meta_var.set("\n".join(lines))

        cover_path = self._find_nearby_cover(e.output_path or "")
        photo = None
        if cover_path:
            try:
                cov = load_cover_from_file(cover_path)
                photo = render_cover_thumbnail(cov, self, max_size=scale_px(self, 140))
            except Exception:
                photo = None
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
            "Repackage in Pack Dialog...",
            state="normal" if (has_path and self._on_pack_paths is not None) else "disabled",
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
        ledger = self._get_ledger()
        if ledger is None:
            return
        missing = 0
        with ledger.batch_connection():
            for entry in ledger.list_entries(limit=100000, include_trashed=False, include_hidden=True):
                if entry.status == "downloaded" and entry.output_path and not os.path.exists(entry.output_path):
                    ledger.set_status(entry.id, "pending")
                    missing += 1
        self._refresh()
        self._status_var.set(
            f"Verified library paths: marked {missing} missing file(s) as pending."
        )

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

    def _refresh(self, *_args: object) -> None:
        self._debounce_after_id = None
        existing = self._tree.get_children()
        if existing:
            self._tree.delete(*existing)
        self._entries_by_id.clear()
        ledger = self._get_ledger()
        if ledger is None:
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
        entries = ledger.list_entries(
            query=self._search_var.get(),
            limit=1000,
            include_trashed=view_trash,
            include_hidden=show_hidden,
            status="trashed" if view_trash else None,
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
            "No matching entries in library." if self._search_var.get().strip() else "Library ledger is empty.",
            action_text="" if self._search_var.get().strip() else "Locate Books...",
            action_command=None if self._search_var.get().strip() else self._prompt_locate_local_books,
        )
        self._status_var.set(
            f"Showing {count} {'trashed ' if view_trash else ''}item{'s' if count != 1 else ''}"
            "  •  Filters: author:, tag:, status:, series:, -tag:hidden"
        )

    def _schedule_refresh(self, *_args: object) -> None:
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
                parent=self,
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
            parent=self,
        )
        if raw is None:
            return
        parsed = parse_tags_input(raw)
        with ledger.batch_connection():
            for entry_id in selected_ids:
                ledger.set_tags(entry_id, parsed)
        self._refresh()

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
            parent=self,
        ):
            return
        ledger.clear()
        self._refresh()

    def _prompt_locate_local_books(self) -> None:
        folder = filedialog.askdirectory(
            parent=self,
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
            self._loading_bar.grid()
            self._loading_bar.start(12)

        def _finish(result: LocalScanResult | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
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
                self._cb_queue.post(lambda r=res: _finish(r, None))
            except Exception as exc:
                self._cb_queue.post(lambda err=str(exc): _finish(None, err))
            finally:
                worker_ledger.close()
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()
        return None

    def _prompt_export(self) -> None:
        output_path = filedialog.asksaveasfilename(
            parent=self,
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
            messagebox.showerror("Export Error", str(exc), parent=self)

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
        if self._debounce_after_id is not None:
            try:
                self.after_cancel(self._debounce_after_id)
            except tk.TclError:
                pass
            self._debounce_after_id = None
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None

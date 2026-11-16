"""Modal dialog displaying and managing the unified library and SQLite download ledger."""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from collections.abc import Callable

from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
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
        TreeColumnSpec("downloaded_at", "Date (UTC)", width=135, stretch=False),
    )

    def __init__(
        self,
        parent: tk.Misc,
        archive_path: str,
        *,
        on_queue_urls: Callable[[list[str]], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        self._archive_path = archive_path
        self._ledger: DownloadArchive | None = None
        self._debounce_after_id: str | None = None
        self._on_queue_urls = on_queue_urls
        self._entries_by_id: dict[int, ArchiveEntry] = {}
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)

        self.title("Library & Download Ledger")
        self.transient(parent)  # type: ignore[arg-type]
        self.geometry(f"{scale_px(self, 960)}x{scale_px(self, 480)}")
        self.minsize(scale_px(self, 680), scale_px(self, 340))

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
        ttk.Button(top_bar, text="Refresh", command=self._refresh).pack(side=tk.LEFT)

        scrolled_tree = ScrolledTreeview(
            body,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
        )
        scrolled_tree.grid(row=1, column=0, sticky="nsew")
        self._tree = scrolled_tree.tree

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
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
        ttk.Button(btn_bar, text="Export...", command=self._prompt_export).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Clear All", command=self._clear_all).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        ttk.Label(body, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

        self.bind("<Destroy>", self._on_destroy)
        self._search_var.trace_add("write", self._schedule_refresh)
        self._refresh()
        apply_theme_to_window(self, self._palette)

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
        )
        count = len(entries)
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

        def _finish(result: LocalScanResult | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
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

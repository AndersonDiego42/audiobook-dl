"""Modal dialog displaying and managing the SQLite download history ledger."""

import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
from audiobookdl.utils.archive import DownloadArchive


class ArchiveLedgerDialog(tk.Toplevel):
    """Modal `Toplevel` window hosting the SQLite download history ledger."""

    _COLUMNS = (
        TreeColumnSpec("downloaded_at", "Date (UTC)", width=140, stretch=False),
        TreeColumnSpec("source", "Service", width=90, stretch=False),
        TreeColumnSpec("title", "Title", width=180, stretch=True),
        TreeColumnSpec("author", "Author", width=130, stretch=True),
        TreeColumnSpec("output_path", "Output Path", width=180, stretch=True),
    )

    def __init__(self, parent: tk.Misc, archive_path: str) -> None:
        super().__init__(parent)
        self._archive_path = archive_path
        self._ledger: Optional[DownloadArchive] = None
        self._debounce_after_id: Optional[str] = None

        self.title("Download History Ledger")
        self.transient(parent)  # type: ignore[arg-type]
        self.geometry("760x440")
        self.minsize(560, 320)

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self._search_var = tk.StringVar()
        top_bar = ttk.Frame(body)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(top_bar, textvariable=self._search_var, width=32)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(top_bar, text="Refresh", command=self._refresh).pack(side=tk.LEFT)

        scrolled_tree = ScrolledTreeview(body, self._COLUMNS, selectmode="browse")
        scrolled_tree.grid(row=1, column=0, sticky="nsew")
        self._tree = scrolled_tree.tree

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btn_bar, text="Remove Selected", command=self._remove_selected).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Clear All", command=self._clear_all).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        self.bind("<Destroy>", self._on_destroy)
        self._search_var.trace_add("write", self._schedule_refresh)
        self._refresh()

    def _get_ledger(self) -> Optional[DownloadArchive]:
        if self._ledger is None:
            if not os.path.exists(self._archive_path):
                return None
            self._ledger = DownloadArchive(self._archive_path, persistent=True)
        return self._ledger

    def _refresh(self, *_args: object) -> None:
        self._debounce_after_id = None
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        ledger = self._get_ledger()
        if ledger is None:
            return
        for entry in ledger.list_entries(query=self._search_var.get(), limit=500):
            self._tree.insert(
                "",
                tk.END,
                iid=str(entry.id),
                values=(
                    entry.downloaded_at,
                    entry.source,
                    entry.title,
                    entry.author,
                    entry.output_path or "",
                ),
            )

    def _schedule_refresh(self, *_args: object) -> None:
        if self._debounce_after_id is not None:
            try:
                self.after_cancel(self._debounce_after_id)
            except tk.TclError:
                pass
        self._debounce_after_id = self.after(180, self._refresh)

    def _remove_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        ledger = self._get_ledger()
        if ledger is None:
            return
        entry_id = int(sel[0])
        ledger.delete_entry(entry_id)
        self._tree.delete(sel[0])

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

"""Modal dialog for searching service catalogs and browsing saved libraries."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Sequence

from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
from audiobookdl.sources import ServiceInfo
from audiobookdl.sources.source import CatalogBook


class CatalogBrowserDialog(tk.Toplevel):
    """Modal `Toplevel` window hosting service catalog search and library browsing."""

    _COLUMNS = (
        TreeColumnSpec("title", "Title", width=220, stretch=True),
        TreeColumnSpec("author", "Author", width=150, stretch=True),
        TreeColumnSpec("series", "Series", width=130, stretch=True),
        TreeColumnSpec("url", "URL", width=220, stretch=True),
    )

    def __init__(
        self,
        parent: tk.Misc,
        services: Sequence[ServiceInfo],
        selected_service: Optional[ServiceInfo],
        *,
        on_fetch: Callable[[str, ServiceInfo, str, "CatalogBrowserDialog"], None],
        on_add_urls: Callable[[List[str]], None],
    ) -> None:
        super().__init__(parent)
        browseable = [svc for svc in services if svc.supports_search or svc.supports_library]
        self._services_by_label: Dict[str, ServiceInfo] = {svc.label: svc for svc in browseable}
        self._on_fetch = on_fetch
        self._on_add_urls = on_add_urls

        self.title("Browse Library / Search")
        self.transient(parent)  # type: ignore[arg-type]
        self.geometry("760x460")
        self.minsize(560, 340)

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        default_label = (
            selected_service.label
            if selected_service is not None
            and (selected_service.supports_search or selected_service.supports_library)
            else (browseable[0].label if browseable else "")
        )
        self._service_var = tk.StringVar(value=default_label)
        self._query_var = tk.StringVar()
        self._status_var = tk.StringVar(value="Choose a service and click Search or List Library.")

        top_bar = ttk.Frame(body)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Service:").pack(side=tk.LEFT, padx=(0, 6))
        svc_combo = ttk.Combobox(
            top_bar,
            textvariable=self._service_var,
            values=[svc.label for svc in browseable],
            state="readonly",
            width=22,
        )
        svc_combo.pack(side=tk.LEFT, padx=(0, 8))

        self._query_entry = ttk.Entry(top_bar, textvariable=self._query_var, width=28)
        self._query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._search_btn = ttk.Button(top_bar, text="Search", command=lambda: self.run_fetch("search"))
        self._search_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._library_btn = ttk.Button(
            top_bar, text="List Library", command=lambda: self.run_fetch("library")
        )
        self._library_btn.pack(side=tk.LEFT)

        scrolled_tree = ScrolledTreeview(body, self._COLUMNS, selectmode="extended")
        scrolled_tree.grid(row=1, column=0, sticky="nsew")
        self._tree = scrolled_tree.tree

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(btn_bar, textvariable=self._status_var, foreground="#555555").pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Add Selected to URLs", command=self.add_selected).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        self._query_entry.bind("<Return>", lambda _e: self.run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", self.sync_buttons)
        self.sync_buttons()

    def sync_buttons(self, *_args: object) -> None:
        svc = self._services_by_label.get(self._service_var.get())
        if svc is None:
            self._query_entry.configure(state="disabled")
            self._search_btn.configure(state="disabled")
            self._library_btn.configure(state="disabled")
            return
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
        self._status_var.set(f"{'Searching' if mode == 'search' else 'Loading library for'} {svc.label}...")
        self._on_fetch(mode, svc, query_text, self)

    def set_error_status(self, message: str = "Error loading items") -> None:
        self.sync_buttons()
        self._status_var.set(message)

    def populate_items(self, books: Sequence[CatalogBook]) -> None:
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for idx, book in enumerate(books):
            self._tree.insert(
                "",
                tk.END,
                iid=f"book-{idx}",
                values=(book.title, book.author, book.series, book.url),
            )
        self.sync_buttons()
        count = len(books)
        self._status_var.set(f"Found {count} book{'s' if count != 1 else ''}.")

    def add_selected(self) -> None:
        selected_iids = self._tree.selection()
        if not selected_iids:
            selected_iids = self._tree.get_children()
        urls_to_add: List[str] = []
        for iid in selected_iids:
            values = self._tree.item(iid, "values")
            if values and len(values) >= 4 and str(values[3]).strip():
                urls_to_add.append(str(values[3]).strip())
        if not urls_to_add:
            return
        self._on_add_urls(urls_to_add)
        self.destroy()

"""Modal dialog for searching service catalogs and browsing saved libraries."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.gui.widgets import (
    ScrolledTreeview,
    TreeColumnSpec,
    populate_grouped_tree,
    resolve_selected_leaf_iids,
)
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
        on_save_to_library: Optional[Callable[[str, str, Sequence[CatalogBook]], Tuple[int, int]]] = None,
        profile_resolver: Optional[Callable[[str], Sequence[str]]] = None,
        selected_profile: str = "default",
        palette: Optional[ThemePalette] = None,
    ) -> None:
        super().__init__(parent)
        browseable = [svc for svc in services if svc.supports_search or svc.supports_library]
        self._services_by_label: Dict[str, ServiceInfo] = {svc.label: svc for svc in browseable}
        self._on_fetch = on_fetch
        self._on_add_urls = on_add_urls
        self._on_save_to_library = on_save_to_library
        self._profile_resolver = profile_resolver
        self._loaded_books: List[CatalogBook] = []
        self._loaded_service_key: str = ""
        self._loaded_profile: str = "default"
        self._palette = palette or LIGHT_PALETTE

        self.title("Browse Library / Search")
        self.transient(parent)  # type: ignore[arg-type]
        self.geometry(f"{scale_px(self, 760)}x{scale_px(self, 460)}")
        self.minsize(scale_px(self, 560), scale_px(self, 340))

        body = ttk.Frame(self, padding=scale_px(self, 12))
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
        self._profile_var = tk.StringVar(value=(selected_profile or "default").strip().lower() or "default")
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
        self._profile_combo.pack(side=tk.LEFT, padx=(0, 8))

        self._query_entry = ttk.Entry(top_bar, textvariable=self._query_var, width=24)
        self._query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._search_btn = ttk.Button(top_bar, text="Search", command=lambda: self.run_fetch("search"))
        self._search_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._library_btn = ttk.Button(
            top_bar, text="List Library", command=lambda: self.run_fetch("library")
        )
        self._library_btn.pack(side=tk.LEFT)

        self._group_var = tk.BooleanVar(value=True)
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
        ttk.Label(btn_bar, textvariable=self._status_var, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Checkbutton(
            btn_bar,
            text="Group Series",
            variable=self._group_var,
            command=self._render_tree,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Add Selected to URLs", command=self.add_selected).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self._save_lib_btn: Optional[ttk.Button] = None
        if self._on_save_to_library is not None:
            self._save_lib_btn = ttk.Button(
                btn_bar,
                text="Save to Library DB",
                command=self.save_to_library,
                state="disabled",
            )
            self._save_lib_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._query_entry.bind("<Return>", lambda _e: self.run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", self.sync_buttons)
        self.sync_buttons()
        apply_theme_to_window(self, self._palette)

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
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="disabled")
        self._loaded_service_key = svc.key
        self._loaded_profile = self.selected_profile
        self._status_var.set(f"{'Searching' if mode == 'search' else 'Loading library for'} {svc.label}...")
        self._on_fetch(mode, svc, query_text, self)

    def set_error_status(self, message: str = "Error loading items") -> None:
        self.sync_buttons()
        self._status_var.set(message)

    def _render_tree(self) -> None:
        populate_grouped_tree(
            self._tree,
            self._loaded_books,
            grouped=bool(self._group_var.get()),
            get_iid=lambda _book, idx: f"book-{idx}",
            get_values=lambda book: (book.title, book.author, book.series or "", book.url),
            get_group_key=lambda book: book.series or "",
            format_group_values=lambda key, count: (f"{key} ({count} items)", "", key, ""),
            should_group=lambda book, count: count >= 2 or book.item_type == "podcast",
        )

    def populate_items(self, books: Sequence[CatalogBook]) -> None:
        self._loaded_books = list(books)
        self._render_tree()
        self.sync_buttons()
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="normal" if self._loaded_books else "disabled")
        count = len(books)
        self._status_var.set(f"Found {count} book{'s' if count != 1 else ''}.")

    def save_to_library(self) -> None:
        if self._on_save_to_library is None or not self._loaded_books:
            return
        svc = self._services_by_label.get(self._service_var.get())
        service_key = self._loaded_service_key or (svc.key if svc is not None else "")
        profile = self._loaded_profile or self.selected_profile
        selected_iids = resolve_selected_leaf_iids(self._tree)
        if selected_iids:
            chosen = []
            for iid in selected_iids:
                if iid.startswith("book-"):
                    idx = int(iid.split("-", 1)[1])
                    if 0 <= idx < len(self._loaded_books):
                        chosen.append(self._loaded_books[idx])
        else:
            chosen = list(self._loaded_books)
        if not chosen or not service_key:
            return
        inserted, updated = self._on_save_to_library(service_key, profile, chosen)
        self._status_var.set(f"Saved to Library DB: +{inserted} new, {updated} updated.")

    def add_selected(self) -> None:
        selected_iids = resolve_selected_leaf_iids(self._tree)
        if not selected_iids:
            selected_iids = [f"book-{idx}" for idx in range(len(self._loaded_books))]
        urls_to_add: List[str] = []
        for iid in selected_iids:
            values = self._tree.item(iid, "values")
            if values and len(values) >= 4 and str(values[3]).strip():
                urls_to_add.append(str(values[3]).strip())
        if not urls_to_add:
            return
        self._on_add_urls(urls_to_add)
        self.destroy()

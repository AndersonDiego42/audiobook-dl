"""Modal dialog for searching service catalogs and browsing saved libraries."""

import sys
import tkinter as tk
from tkinter import ttk
from collections.abc import Callable, Sequence

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
        TreeColumnSpec("title", "Title", width=210, stretch=True),
        TreeColumnSpec("author", "Author", width=140, stretch=True),
        TreeColumnSpec("series", "Series", width=120, stretch=True),
        TreeColumnSpec("status", "Library", width=110, stretch=False),
        TreeColumnSpec("url", "URL", width=200, stretch=True),
    )

    def __init__(
        self,
        parent: tk.Misc,
        services: Sequence[ServiceInfo],
        selected_service: ServiceInfo | None,
        *,
        on_fetch: Callable[[str, ServiceInfo, str, "CatalogBrowserDialog"], None],
        on_add_urls: Callable[[list[str]], None],
        on_download_urls: Callable[[list[str], str, str], None] | None = None,
        on_save_to_library: Callable[[str, str, Sequence[CatalogBook]], tuple[int, int]] | None = None,
        on_sync_libraries: Callable[[str], None] | None = None,
        profile_resolver: Callable[[str], Sequence[str]] | None = None,
        status_resolver: Callable[[str, Sequence[CatalogBook]], Sequence[str]] | None = None,
        selected_profile: str = "default",
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        browseable = [svc for svc in services if svc.supports_search or svc.supports_library]
        self._services_by_label: dict[str, ServiceInfo] = {svc.label: svc for svc in browseable}
        self._on_fetch = on_fetch
        self._on_add_urls = on_add_urls
        self._on_download_urls = on_download_urls
        self._on_save_to_library = on_save_to_library
        self._on_sync_libraries = on_sync_libraries
        self._profile_resolver = profile_resolver
        self._status_resolver = status_resolver
        self._loaded_books: list[CatalogBook] = []
        self._book_statuses: list[str] = []
        self._loaded_service_key: str = ""
        self._loaded_profile: str = "default"
        self._palette = palette or LIGHT_PALETTE

        self.title("Browse Library / Search")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self.geometry(f"{scale_px(self, 860)}x{scale_px(self, 480)}")
        self.minsize(scale_px(self, 600), scale_px(self, 340))
        self.bind("<Escape>", lambda _e: self.destroy())

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
        self._hide_downloaded_var = tk.BooleanVar(value=False)
        self._scrolled_tree = ScrolledTreeview(
            body,
            self._COLUMNS,
            selectmode="extended",
            show_tree=True,
        )
        self._scrolled_tree.grid(row=1, column=0, sticky="nsew")
        self._tree = self._scrolled_tree.tree
        self._tree.tag_configure("downloaded", foreground=self._palette.success)
        self._tree.tag_configure("pending", foreground=self._palette.warning)
        self._tree.tag_configure("hidden", foreground=self._palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=self._palette.fg_muted)
        self._scrolled_tree.set_empty_state(
            True,
            "Choose a service above and click Search or List Library.",
        )
        self._build_context_menu()
        self._tree.bind("<Double-1>", self._on_tree_double_click)
        self._tree.bind("<Button-3>", self._show_context_menu)
        if sys.platform == "darwin":
            self._tree.bind("<Button-2>", self._show_context_menu)

        self._loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self._loading_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(btn_bar, textvariable=self._status_var, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Checkbutton(
            btn_bar,
            text="Group Series",
            variable=self._group_var,
            command=self._render_tree,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(
            btn_bar,
            text="Hide Downloaded",
            variable=self._hide_downloaded_var,
            command=self._on_filter_toggled,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._download_btn: ttk.Button | None = None
        if self._on_download_urls is not None:
            self._download_btn = ttk.Button(
                btn_bar,
                text="Download Selected",
                command=self.download_selected,
                style="Accent.TButton",
            )
            self._download_btn.pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btn_bar, text="Add Selected to URLs", command=self.add_selected).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self._save_lib_btn: ttk.Button | None = None
        if self._on_save_to_library is not None:
            self._save_lib_btn = ttk.Button(
                btn_bar,
                text="Save to Library DB",
                command=self.save_to_library,
                state="disabled",
            )
            self._save_lib_btn.pack(side=tk.RIGHT, padx=(0, 6))
        if self._on_sync_libraries is not None:
            ttk.Button(
                btn_bar,
                text="Sync Libraries",
                command=self._trigger_sync,
            ).pack(side=tk.RIGHT, padx=(0, 6))

        self._query_entry.bind("<Return>", lambda _e: self.run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", self.sync_buttons)
        self.sync_buttons()
        apply_theme_to_window(self, self._palette)

    def _build_context_menu(self) -> None:
        menu = tk.Menu(self._tree, tearoff=0)
        if self._on_download_urls is not None:
            menu.add_command(label="Download Selected", command=self.download_selected)
        menu.add_command(label="Add Selected to URLs", command=self.add_selected)
        if self._on_save_to_library is not None:
            menu.add_command(label="Save to Library DB", command=self.save_to_library)
        menu.add_separator()
        menu.add_command(label="Copy URL(s)", command=self._copy_selected_urls)
        self._context_menu = menu

    def _active_service_and_profile(self) -> tuple[str, str]:
        svc = self._services_by_label.get(self._service_var.get())
        service_key = self._loaded_service_key or (svc.key if svc is not None else "")
        profile = self._loaded_profile or self.selected_profile
        return service_key, profile

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
        total = len(self._loaded_books)
        downloaded = sum(1 for s in self._book_statuses if s.lower().startswith("downloaded"))
        if self._hide_downloaded_var.get() and visible_count < total:
            self._status_var.set(
                f"Showing {visible_count} of {total} book{'s' if total != 1 else ''} "
                f"({downloaded} downloaded hidden)."
            )
        elif downloaded > 0:
            self._status_var.set(
                f"Found {total} book{'s' if total != 1 else ''} ({downloaded} in library)."
            )
        else:
            self._status_var.set(f"Found {total} book{'s' if total != 1 else ''}.")

    def _on_filter_toggled(self) -> None:
        visible_count = self._render_tree()
        if self._loaded_books:
            self._update_count_status(visible_count)

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
        self._scrolled_tree.set_empty_state(False)
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._on_fetch(mode, svc, query_text, self)

    def _stop_loading(self) -> None:
        self._loading_bar.stop()
        self._loading_bar.grid_remove()

    def set_error_status(self, message: str = "Error loading items") -> None:
        self._stop_loading()
        self.sync_buttons()
        self._status_var.set(message)
        if not self._loaded_books:
            self._scrolled_tree.set_empty_state(True, message)

    def _render_tree(self) -> int:
        hide_downloaded = bool(self._hide_downloaded_var.get())
        indexed_books: list[tuple[int, CatalogBook, str]] = []
        for idx, book in enumerate(self._loaded_books):
            status_label = self._book_statuses[idx] if idx < len(self._book_statuses) else ""
            if hide_downloaded and status_label.lower().startswith("downloaded"):
                continue
            indexed_books.append((idx, book, status_label))

        populate_grouped_tree(
            self._tree,
            indexed_books,
            grouped=bool(self._group_var.get()),
            get_iid=lambda entry, _pos: f"book-{entry[0]}",
            get_values=lambda entry: (
                entry[1].title,
                entry[1].author,
                entry[1].series or "",
                entry[2],
                entry[1].url,
            ),
            get_group_key=lambda entry: entry[1].series or "",
            format_group_values=lambda key, count: (f"{key} ({count} items)", "", key, "", ""),
            should_group=lambda entry, count: count >= 2 or entry[1].item_type == "podcast",
            get_tags=lambda entry: self._status_tags(entry[2]),
        )
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

    def refresh_statuses(self) -> None:
        """Re-query library statuses for loaded books and refresh the tree."""
        if not self._loaded_books:
            return
        self._refresh_statuses()
        visible_count = self._render_tree()
        self._update_count_status(visible_count)

    def _selected_books(self) -> list[CatalogBook]:
        chosen: list[CatalogBook] = []
        for iid in resolve_selected_leaf_iids(self._tree):
            if iid.startswith("book-"):
                try:
                    idx = int(iid.split("-", 1)[1])
                except ValueError:
                    continue
                if 0 <= idx < len(self._loaded_books):
                    chosen.append(self._loaded_books[idx])
        return chosen

    def _selected_urls(self) -> list[str]:
        urls: list[str] = []
        for book in self._selected_books():
            url = (book.url or "").strip()
            if url and url not in urls:
                urls.append(url)
        return urls

    def _copy_selected_urls(self) -> None:
        urls = self._selected_urls()
        if not urls:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(urls))
        self._status_var.set(f"Copied {len(urls)} URL{'s' if len(urls) != 1 else ''}.")

    def _show_context_menu(self, event: tk.Event) -> str:
        row_iid = self._tree.identify_row(event.y)
        if row_iid:
            if row_iid not in self._tree.selection():
                self._tree.selection_set(row_iid)
            self._tree.focus(row_iid)
        has_urls = bool(self._selected_urls())
        menu = self._context_menu
        if self._on_download_urls is not None:
            menu.entryconfigure("Download Selected", state="normal" if has_urls else "disabled")
        menu.entryconfigure("Add Selected to URLs", state="normal" if has_urls else "disabled")
        if self._on_save_to_library is not None:
            menu.entryconfigure(
                "Save to Library DB",
                state="normal" if bool(self._loaded_books) else "disabled",
            )
        menu.entryconfigure("Copy URL(s)", state="normal" if has_urls else "disabled")
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
        self.destroy()

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
        self.destroy()
        self._on_download_urls(urls_to_download, service_key, profile)

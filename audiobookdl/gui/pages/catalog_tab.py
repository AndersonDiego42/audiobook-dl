"""Catalog browser tab hosting remote search, library listing, filtering, and book details."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any
import webbrowser

from audiobookdl.config import Config
from audiobookdl.gui.formatting import format_catalog_formats
from audiobookdl.gui.pages.catalog_covers import CatalogCoverLoader
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_widget_tree, make_bold_font, scale_px
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ScrolledTreeview,
    TreeColumnSpec,
    populate_grouped_tree,
    resolve_selected_leaf_iids,
)
from audiobookdl.sources import ServiceInfo
from audiobookdl.sources.source import CatalogBook


class CatalogTab(ttk.Frame):
    """Tab hosting service catalog search and remote library browsing."""

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
        self._on_cancel_fetch = on_cancel_fetch
        self._on_manage_account = on_manage_account
        self._loaded_books: list[CatalogBook] = []
        self._book_statuses: list[str] = []
        self._visible_count = 0
        self._is_fetching = False
        self._pending_fetch_service_key: str = ""
        self._pending_fetch_profile: str = "default"
        self._loaded_service_key: str = ""
        self._loaded_profile: str = "default"
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)
        self._covers = CatalogCoverLoader(
            self,
            self._cb_queue,
            thumb_size=scale_px(self, 48),
            detail_thumb_size=scale_px(self, 160),
            cover_fetcher=cover_fetcher,
            config_provider=config_provider,
        )
        self._filter_after_id: str | None = None
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
        self._profile_var = tk.StringVar(
            value=(selected_profile or "default").strip().lower() or "default"
        )
        self._query_var = tk.StringVar()
        self._local_filter_var = tk.StringVar()
        self._status_var = tk.StringVar(value="Choose a service and click Search or List Library.")
        self._group_var = tk.BooleanVar(value=True)
        self._hide_downloaded_var = tk.BooleanVar(value=False)
        self._has_ebook_var = tk.BooleanVar(value=False)

        svc_combo = self._build_top_bar(browseable)
        self._build_filter_bar()
        self._build_tree_and_details()

        self._loading_bar = ttk.Progressbar(self, mode="indeterminate")
        self._loading_bar.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        self._build_button_bar()
        self._build_status_bar()

        self._query_entry.bind("<Return>", lambda _e: self.run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", self.sync_buttons)
        self._local_filter_var.trace_add("write", self._schedule_local_filter)
        self.apply_theme(self._palette)
        self.sync_buttons()

    def _build_top_bar(self, browseable: Sequence[ServiceInfo]) -> ttk.Combobox:
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
        self._search_btn = ttk.Button(
            top_bar, text="Search", command=lambda: self.run_fetch("search")
        )
        self._search_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._library_btn = ttk.Button(
            top_bar, text="List Library", command=lambda: self.run_fetch("library")
        )
        self._library_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cancel_btn = ttk.Button(
            top_bar, text="Cancel", command=self.cancel_fetch, state="disabled"
        )
        self._cancel_btn.pack(side=tk.LEFT)
        return svc_combo

    def _build_filter_bar(self) -> None:
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

    def _build_tree_and_details(self) -> None:
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

    def _build_button_bar(self) -> None:
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

    def _build_status_bar(self) -> None:
        status_row = ttk.Frame(self)
        status_row.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)
        ttk.Label(status_row, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("downloaded", foreground=palette.success)
        self._tree.tag_configure("pending", foreground=palette.warning)
        self._tree.tag_configure("hidden", foreground=palette.fg_muted)
        self._tree.tag_configure("trashed", foreground=palette.fg_muted)
        self._covers.refresh_placeholder(palette)
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
        self._covers.close()

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
        if self._profile_var.get() not in profiles:
            self._profile_var.set("default")
        if self._is_fetching:
            self._profile_combo.configure(values=profiles, state="disabled")
            self._query_entry.configure(state="disabled")
            self._search_btn.configure(state="disabled")
            self._library_btn.configure(state="disabled")
            return
        self._profile_combo.configure(values=profiles, state="readonly")
        self._query_entry.configure(state="normal" if svc.supports_search else "disabled")
        self._search_btn.configure(state="normal" if svc.supports_search else "disabled")
        self._library_btn.configure(state="normal" if svc.supports_library else "disabled")

    def run_fetch(self, mode: str) -> None:
        if self._is_fetching:
            return
        svc = self._services_by_label.get(self._service_var.get())
        if svc is None:
            return
        query_text = self._query_var.get().strip()
        if mode == "search" and not query_text:
            self._status_var.set("Enter a search term first.")
            return
        self._is_fetching = True
        self._profile_combo.configure(state="disabled")
        self._query_entry.configure(state="disabled")
        self._search_btn.configure(state="disabled")
        self._library_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="disabled")
        self._pending_fetch_service_key = svc.key
        self._pending_fetch_profile = self.selected_profile
        self._status_var.set(
            f"{'Searching' if mode == 'search' else 'Loading library for'} {svc.label}..."
        )
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
        self._is_fetching = False
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._cancel_btn.configure(state="disabled")
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="normal" if self._loaded_books else "disabled")

    def set_error_status(self, message: str = "Error loading items") -> None:
        self._stop_loading()
        self.sync_buttons()
        self._status_var.set(message)
        if not self._loaded_books:
            self._scrolled_tree.set_empty_state(True, message)

    def _on_cover_bytes_ready(self, token: int, url: str, png_bytes: bytes | None) -> None:
        if not self.winfo_exists() or token != self._covers.cover_fetch_token:
            return
        photo = self._covers.store_thumbnail_bytes(url, png_bytes)
        if photo is None:
            return
        for idx, book in enumerate(self._loaded_books):
            if (getattr(book, "cover_url", None) or "").strip() != url:
                continue
            iid = f"book-{idx}"
            if self._tree.exists(iid):
                self._covers.row_photos[iid] = photo
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

        self._covers.row_photos.clear()

        def _row_image(entry: tuple[int, CatalogBook, str]) -> Any | None:
            photo = self._covers.photo_for_book(entry[1])
            if photo is not None:
                self._covers.row_photos[f"book-{entry[0]}"] = photo
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
        if self._pending_fetch_service_key:
            self._loaded_service_key = self._pending_fetch_service_key
            self._loaded_profile = self._pending_fetch_profile
        self._covers.cover_fetch_token += 1
        token = self._covers.cover_fetch_token
        self._loaded_books = list(books)
        if statuses is not None:
            resolved = list(statuses)
            if len(resolved) < len(self._loaded_books):
                resolved.extend([""] * (len(self._loaded_books) - len(resolved)))
            self._book_statuses = [str(s or "") for s in resolved[: len(self._loaded_books)]]
        else:
            self._refresh_statuses()
        visible_count = self._render_tree()
        self._visible_count = visible_count
        self.sync_buttons()
        if self._save_lib_btn is not None:
            self._save_lib_btn.configure(state="normal" if self._loaded_books else "disabled")
        self._update_count_status(visible_count)
        self._update_details_pane()
        self._covers.start_thumbnail_loading(self._loaded_books, token, self._on_cover_bytes_ready)

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
        self._covers.detail_token += 1
        token = self._covers.detail_token
        indices = self._selected_book_indices()
        if not indices:
            self._covers.detail_cover_photo = None
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
            self._covers.detail_cover_photo = None
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
            self._covers.detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="No Cover")
            return

        if cover_url in self._covers.detail_cover_png_cache:
            cached_detail = self._covers.detail_cover_png_cache[cover_url]
            self._covers.detail_cover_png_cache.move_to_end(cover_url)
            self._apply_detail_cover(token, cover_url, cached_detail)
            return

        # Show 48px thumbnail immediately if already cached while fetching larger detail cover.
        small_photo = self._covers.url_photos.get(cover_url)
        if small_photo is not None:
            self._covers.detail_cover_photo = small_photo
            self._detail_cover_label.configure(image=small_photo, text="")
        else:
            self._covers.detail_cover_photo = None
            self._detail_cover_label.configure(image="", text="Loading cover...")

        self._covers.fetch_detail_cover(
            token,
            cover_url,
            lambda t, u, b: self._apply_detail_cover(t, u, b, cache_result=True),
        )

    def _apply_detail_cover(
        self,
        token: int,
        cover_url: str,
        png_bytes: bytes | None,
        *,
        cache_result: bool = False,
    ) -> None:
        if not self.winfo_exists() or token != self._covers.detail_token:
            return
        photo = self._covers.resolve_detail_photo(
            cover_url, png_bytes, cache_result=cache_result
        )
        if photo is not None:
            self._detail_cover_label.configure(image=photo, text="")
        else:
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
                state="normal" if (bool(self._loaded_books) and not self._is_fetching) else "disabled",
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
        self._on_tree_selected()
        urls = self._selected_urls()
        if not urls:
            self._status_var.set("Selected book has no downloadable URL.")
            return
        if self._on_download_urls is not None:
            books = self._selected_books()
            label = books[0].title if books else urls[0]
            if not messagebox.askyesno(
                "Download Catalog Book",
                f"Download '{label}' now?",
                parent=self.winfo_toplevel(),
            ):
                return
            self.download_selected()
        else:
            self.add_selected()

    def save_to_library(self) -> None:
        if self._on_save_to_library is None or not self._loaded_books or self._is_fetching:
            return
        service_key, profile = self._active_service_and_profile()
        chosen = self._selected_books()
        if not chosen:
            if not messagebox.askyesno(
                "Save All Loaded Books",
                f"No books are selected. Save all {len(self._loaded_books)} loaded book(s) to Library DB?",
                parent=self.winfo_toplevel(),
            ):
                return
            chosen = list(self._loaded_books)
        if not chosen or not service_key:
            return
        inserted, updated = self._on_save_to_library(service_key, profile, chosen)
        self._refresh_statuses()
        self._visible_count = self._render_tree()
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

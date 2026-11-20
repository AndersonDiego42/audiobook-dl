"""Library page combining the Archive Ledger, Catalog Browser, and Library Sync sub-tabs."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from audiobookdl.gui.page_deps import LibraryPageDeps
from audiobookdl.gui.pages.archive_tab import ArchiveTab
from audiobookdl.gui.pages.catalog_tab import CatalogTab
from audiobookdl.gui.pages.sync_tab import SyncTab
from audiobookdl.gui.theme import ThemePalette, make_bold_font, scale_px


class LibraryPage(ttk.Frame):
    """Unified Library page housing Archive Ledger, Catalog Browser, and Library Sync sub-tabs."""

    def __init__(
        self,
        parent: tk.Misc,
        deps: LibraryPageDeps,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._archive_path_provider = deps.archive_path_provider
        self._palette_provider = deps.palette_provider
        self._on_tab_changed = deps.on_tab_changed
        self._on_library_saved = deps.on_library_saved
        self._suppress_tab_event = False
        self._tab_events_armed = False
        self._dirty_tabs: set[str] = {"archive", "catalog", "sync"}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, scale_px(self, 8)))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Library", font=make_bold_font(self, 4)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Manage downloaded audiobooks, search service catalogs, and sync remote libraries.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.notebook.enable_traversal()

        palette = deps.palette_provider()
        self.archive_tab = ArchiveTab(
            self.notebook,
            deps.archive_path_provider(),
            on_queue_urls=deps.on_queue_urls,
            on_open_path=deps.on_open_path,
            on_pack_paths=deps.on_pack_paths,
            on_upload_abs=deps.on_upload_abs,
            palette=palette,
            db_executor=deps.db_executor,
            defer_initial_refresh=True,
        )

        def _handle_library_saved(inserted: int, updated: int) -> None:
            self._dirty_tabs.add("archive")
            if self._on_library_saved is not None:
                self._on_library_saved(inserted, updated)

        self.catalog_tab = CatalogTab(
            self.notebook,
            deps.services,
            deps.selected_service_provider(),
            on_fetch=deps.on_catalog_fetch,
            on_add_urls=deps.on_catalog_add_urls,
            on_download_urls=deps.on_catalog_download_urls,
            on_save_to_library=deps.on_save_to_library,
            on_sync_libraries=deps.on_sync_from_catalog,
            profile_resolver=deps.profile_resolver,
            status_resolver=deps.status_resolver,
            cover_fetcher=deps.cover_fetcher,
            on_cancel_fetch=deps.on_catalog_cancel,
            on_manage_account=deps.on_manage_account,
            config_provider=deps.config_provider,
            selected_profile=deps.selected_profile_provider(),
            palette=palette,
            db_executor=deps.db_executor,
            on_library_saved=_handle_library_saved,
        )
        self.sync_tab = SyncTab(
            self.notebook,
            deps.config_provider,
            deps.services,
            auto_sync_enabled=deps.auto_sync_enabled,
            auto_sync_minutes=deps.auto_sync_minutes,
            auto_sync_mode=deps.auto_sync_mode,
            auto_sync_targets=deps.auto_sync_targets,
            on_run_sync=deps.on_run_sync,
            on_auto_sync_changed=deps.on_auto_sync_changed,
            on_save_schedule=deps.on_save_schedule,
            on_manage_accounts=deps.on_manage_accounts,
            on_cancel_sync=deps.on_cancel_sync,
            defer_initial_refresh=True,
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
            if self._tab_events_armed and tab_key in self._dirty_tabs:
                self._refresh_tab(tab_key)

    def _refresh_tab(self, tab_key: str) -> None:
        self._dirty_tabs.discard(tab_key)
        if tab_key == "archive":
            latest_path = self._archive_path_provider()
            if latest_path != self.archive_tab._archive_path:
                self.archive_tab.set_archive_path(latest_path)
            else:
                self.archive_tab.refresh()
        elif tab_key == "catalog":
            self.catalog_tab.sync_buttons()
            self.catalog_tab.refresh_statuses()
        elif tab_key == "sync":
            self.sync_tab.refresh_targets()

    def _handle_notebook_tab_changed(self, _event: tk.Event | None = None) -> None:
        if self._suppress_tab_event or not self._tab_events_armed:
            return
        active_tab = self.current_sub_tab()
        if active_tab in self._dirty_tabs:
            self._refresh_tab(active_tab)
        if self._on_tab_changed is not None:
            self._on_tab_changed(active_tab)

    def on_show(self) -> None:
        """Mark all sub-tabs dirty and refresh only the currently visible sub-tab."""
        self._tab_events_armed = True
        self._dirty_tabs.update(("archive", "catalog", "sync"))
        self._refresh_tab(self.current_sub_tab())

    def apply_theme(self, palette: ThemePalette) -> None:
        self.archive_tab.apply_theme(palette)
        self.catalog_tab.apply_theme(palette)
        self.sync_tab.apply_theme(palette)

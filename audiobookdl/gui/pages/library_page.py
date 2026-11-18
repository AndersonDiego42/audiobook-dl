"""Library page combining the Archive Ledger, Catalog Browser, and Library Sync sub-tabs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import tkinter as tk
from tkinter import ttk

from audiobookdl.config import DEFAULT_AUTO_SYNC_MODE, Config
from audiobookdl.gui.pages.archive_tab import ArchiveTab
from audiobookdl.gui.pages.catalog_tab import CatalogTab
from audiobookdl.gui.pages.sync_tab import SyncTab
from audiobookdl.gui.theme import ThemePalette, make_bold_font, scale_px
from audiobookdl.sources import ServiceInfo
from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.archive import ArchiveEntry


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

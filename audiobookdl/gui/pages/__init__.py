"""Top-level lazy pages mounted inside `SidebarShell`."""

from audiobookdl.gui.pages.accounts_page import AccountsPage
from audiobookdl.gui.pages.archive_tab import ArchiveTab
from audiobookdl.gui.pages.catalog_tab import CatalogTab
from audiobookdl.gui.pages.download_page import DownloadPage
from audiobookdl.gui.pages.library_page import LibraryPage
from audiobookdl.gui.pages.pack_page import PackPage
from audiobookdl.gui.pages.settings_page import SettingsPage
from audiobookdl.gui.pages.sync_tab import SyncTab

__all__ = [
    "AccountsPage",
    "ArchiveTab",
    "CatalogTab",
    "DownloadPage",
    "LibraryPage",
    "PackPage",
    "SettingsPage",
    "SyncTab",
]

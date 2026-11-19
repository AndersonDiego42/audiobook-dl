"""Top-level lazy pages mounted inside `SidebarShell`."""

from __future__ import annotations

import importlib
from typing import Any

_PAGE_EXPORTS: dict[str, tuple[str, str]] = {
    "AccountsPage": ("audiobookdl.gui.pages.accounts_page", "AccountsPage"),
    "ArchiveTab": ("audiobookdl.gui.pages.archive_tab", "ArchiveTab"),
    "CatalogTab": ("audiobookdl.gui.pages.catalog_tab", "CatalogTab"),
    "DownloadPage": ("audiobookdl.gui.pages.download_page", "DownloadPage"),
    "LibraryPage": ("audiobookdl.gui.pages.library_page", "LibraryPage"),
    "PackPage": ("audiobookdl.gui.pages.pack_page", "PackPage"),
    "SettingsPage": ("audiobookdl.gui.pages.settings_page", "SettingsPage"),
    "SyncTab": ("audiobookdl.gui.pages.sync_tab", "SyncTab"),
}

__all__ = list(_PAGE_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _PAGE_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    module = importlib.import_module(mod_name)
    return getattr(module, attr_name)

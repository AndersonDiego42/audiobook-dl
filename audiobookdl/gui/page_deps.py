"""Typed dependency dataclasses injected into lazily-built GUI pages.

`page_factories.py` (the composition root) assembles these objects from
`AudiobookDLApp` coordinators so page constructors take a single typed `deps`
argument instead of 25-36 loose parameters and an untyped `app` back-reference.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from audiobookdl.config import DEFAULT_AUTO_SYNC_MODE

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import ttk

    from audiobookdl.config import (
        AudiobookshelfConfig,
        Config,
        CustomMetadataProviderConfig,
    )
    from audiobookdl.gui.controller import GuiController
    from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
    from audiobookdl.gui.options_view_model import OptionsViewModel
    from audiobookdl.gui.pages.catalog_tab import CatalogTab
    from audiobookdl.gui.theme import ThemePalette
    from audiobookdl.sources import ServiceInfo
    from audiobookdl.sources.source import CatalogBook
    from audiobookdl.utils.archive import ArchiveEntry


@dataclass(frozen=True, slots=True)
class QueuePanelDeps:
    """Dependencies for `ActivityQueuePanel`."""

    controller: GuiController
    status_var: tk.StringVar
    batch_var: tk.StringVar
    metrics_var: tk.StringVar
    on_start_download: Callable[[], None]
    on_cancel_download: Callable[[], None]
    on_preview_metadata: Callable[[], None]
    on_pack_local: Callable[[], Any]
    on_open_folder: Callable[[], None]
    on_retry_failed: Callable[[], None]
    service_label_for_url: Callable[[str], str]
    on_retry_selected: Callable[[Sequence[str]], None] | None = None
    on_preview_url: Callable[[str], None] | None = None
    on_send_to_urls: Callable[[Sequence[str]], None] | None = None
    on_open_path: Callable[[str, bool], None] | None = None
    on_import_queue: Callable[[list[dict[str, Any]]], None] | None = None
    on_queue_summary_changed: Callable[[int, int], None] | None = None
    on_progress_mode_changed: Callable[[str], None] | None = None
    progress_var: tk.DoubleVar | None = None
    active_job_var: tk.StringVar | None = None
    concurrency_var: tk.IntVar | None = None
    on_concurrency_changed: Callable[[int], None] | None = None
    palette: ThemePalette | None = None


@dataclass(frozen=True, slots=True)
class DownloadPageDeps:
    """Dependencies for `DownloadPage`."""

    root: tk.Tk
    style: ttk.Style
    vars: OptionsViewModel
    queue: QueuePanelDeps
    palette_provider: Callable[[], ThemePalette]
    on_locate_ffmpeg: Callable[[], object]
    on_open_accounts: Callable[[], None]
    on_open_folder: Callable[[], None]
    on_browse_output_dir: Callable[[], None]
    on_paste_urls: Callable[[], object]
    on_paste_and_queue: Callable[[], None]
    on_browse_url_file: Callable[[], None]
    on_clear_urls: Callable[[], None]
    on_urls_modified: Callable[[tk.Event | None], None]
    on_start_download: Callable[[], None]
    on_m4b_toggled: Callable[[], None]
    on_format_selected: Callable[[tk.Event | None], None]
    initial_split_ratio: float | None = None
    options_collapsed: bool = True
    output_naming_collapsed: bool = True
    on_open_drm_inspector: Callable[[tk.Misc], None] | None = None
    profile_choices_provider: Callable[[], list[str]] | None = None
    on_select_profile: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class LibraryPageDeps:
    """Dependencies for `LibraryPage`."""

    archive_path_provider: Callable[[], str]
    config_provider: Callable[[], Config]
    services: Sequence[ServiceInfo]
    selected_service_provider: Callable[[], ServiceInfo | None]
    selected_profile_provider: Callable[[], str]
    on_queue_urls: Callable[[list[str]], None]
    on_open_path: Callable[[str, bool], None]
    on_pack_paths: Callable[[list[str]], object]
    on_upload_abs: Callable[[ArchiveEntry], None]
    on_catalog_fetch: Callable[[str, ServiceInfo, str, CatalogTab], None]
    on_catalog_add_urls: Callable[[list[str]], None]
    on_catalog_download_urls: Callable[[list[str], str, str], None]
    on_save_to_library: Callable[[str, str, Sequence[CatalogBook]], tuple[int, int]]
    on_sync_from_catalog: Callable[[str], None]
    profile_resolver: Callable[[str], Sequence[str]]
    status_resolver: Callable[[str, Sequence[CatalogBook]], Sequence[str]]
    palette_provider: Callable[[], ThemePalette]
    on_run_sync: Callable[[list[tuple[str, str]], str], None]
    on_auto_sync_changed: Callable[[bool, int], None]
    cover_fetcher: Callable[[str], bytes | None] | None = None
    on_catalog_cancel: Callable[[], object] | None = None
    auto_sync_enabled: bool = False
    auto_sync_minutes: int = 30
    auto_sync_mode: str = DEFAULT_AUTO_SYNC_MODE
    auto_sync_targets: Sequence[tuple[str, str]] | None = None
    on_save_schedule: Callable[[bool, int, str, list[tuple[str, str]]], None] | None = None
    on_manage_account: Callable[[str, str], object] | None = None
    on_manage_accounts: Callable[[], object] | None = None
    on_cancel_sync: Callable[[], None] | None = None
    on_tab_changed: Callable[[str], None] | None = None
    db_executor: ThreadPoolExecutor | None = None
    on_library_saved: Callable[[int, int], None] | None = None


@dataclass(frozen=True, slots=True)
class SettingsPageDeps:
    """Dependencies for `SettingsPage`."""

    theme_var: tk.StringVar
    notify_on_complete_var: tk.BooleanVar
    clipboard_monitor_var: tk.BooleanVar
    on_theme_changed: Callable[[str], object]
    on_toggle_clipboard_monitor: Callable[[], None]
    on_reload_config: Callable[[], None]
    on_check_updates: Callable[[], None]
    ffmpeg_assistant: FFmpegAssistant
    abs_config_provider: Callable[[], AudiobookshelfConfig]
    config_location: str | None = None
    check_updates_var: tk.BooleanVar | None = None
    minimize_to_tray_var: tk.BooleanVar | None = None
    tray_available: bool = False
    on_toggle_minimize_to_tray: Callable[[], None] | None = None
    on_toggle_notify_on_complete: Callable[[], None] | None = None
    on_toggle_check_updates: Callable[[], None] | None = None
    on_save_abs_config: Callable[..., object] | None = None
    on_abs_saved: Callable[[AudiobookshelfConfig], None] | None = None
    custom_providers_provider: Callable[[], Mapping[str, CustomMetadataProviderConfig]] | None = None
    on_save_custom_provider: Callable[..., object] | None = None
    on_delete_custom_provider: Callable[[str], object] | None = None
    http_proxy_provider: Callable[[], str | None] | None = None
    http_timeout_provider: Callable[[], float | None] | None = None
    palette: ThemePalette | None = None
    style: ttk.Style | None = None

"""Lazy page factory builders connecting sidebar pages to `AudiobookDLApp` coordinators."""

from __future__ import annotations

from collections.abc import Sequence
import os
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from audiobookdl.config import DEFAULT_PROFILE, list_source_profiles, normalize_profile_name
from audiobookdl.gui.formatting import default_output_dir
from audiobookdl.gui.pages import (
    AccountsPage,
    CatalogTab,
    DownloadPage,
    LibraryPage,
    PackPage,
    SettingsPage,
)
from audiobookdl.gui.window_manager import open_path_in_os
from audiobookdl.sources import ServiceInfo
from audiobookdl.utils.archive import DownloadArchive

if TYPE_CHECKING:
    from audiobookdl.gui.app import AudiobookDLApp


def create_download_page(app: AudiobookDLApp, parent: ttk.Frame) -> DownloadPage:
    """Build and wire the lazy `DownloadPage`."""

    def _open_path(path: str, reveal: bool = False) -> None:
        open_path_in_os(app.root, path, reveal_in_folder=reveal)

    def _active_service_profiles() -> list[str]:
        svc = app._auth.selected_service()
        if svc is None:
            return [DEFAULT_PROFILE]
        cfg = app.controller.config_snapshot()
        profiles = list_source_profiles(cfg, svc.key)
        return [DEFAULT_PROFILE] + [p for p in profiles if p != DEFAULT_PROFILE]

    def _select_active_profile(prof: str) -> None:
        svc = app._auth.selected_service()
        if svc is not None:
            app._auth.select_service_profile(svc.key, prof)
        else:
            app.profile_var.set(normalize_profile_name(prof))
        app._options.update_download_summaries(page)

    cfg_gui = app.controller.config.gui
    options_collapsed = True if cfg_gui.options_collapsed is None else cfg_gui.options_collapsed
    naming_collapsed = (
        True if cfg_gui.output_naming_collapsed is None else cfg_gui.output_naming_collapsed
    )
    page = DownloadPage(
        parent,
        app.root,
        app,
        style=app._style,
        palette_provider=lambda: app._window.palette,
        initial_split_ratio=cfg_gui.download_split_ratio,
        options_collapsed=options_collapsed,
        output_naming_collapsed=naming_collapsed,
        on_locate_ffmpeg=app._ffmpeg_assistant.browse_dir,
        on_open_accounts=app._open_accounts_from_download,
        on_open_folder=app._options.open_output_folder,
        on_browse_output_dir=app._options.browse_output_dir,
        on_paste_urls=app._urls.paste_urls_from_clipboard,
        on_paste_and_queue=app._urls.paste_and_queue,
        on_browse_url_file=app._urls.browse_url_file,
        on_clear_urls=app._urls.clear_urls,
        on_urls_modified=app._urls.on_urls_modified,
        on_start_download=app.start_download,
        on_cancel_download=app.cancel_download,
        on_preview_metadata=app.preview_metadata,
        on_pack_local=lambda: app.navigate("pack"),
        on_retry_failed=app._runs.retry_failed_queue_items,
        on_retry_selected=app._runs.retry_failed_queue_items,
        service_label_for_url=app._auth.service_label_for_url,
        on_preview_url=lambda u: app.preview_metadata(url_override=u),
        on_send_to_urls=app._urls.send_urls_to_input,
        on_open_path=_open_path,
        on_import_queue=app._runs.enqueue_imported_queue_records,
        on_queue_summary_changed=app.shell.set_download_badge,
        on_progress_mode_changed=app.shell.set_footer_progress_mode,
        on_m4b_toggled=app._options.on_m4b_toggled,
        on_format_selected=app._options.on_format_selected,
        progress_var=app.progress_var,
        active_job_var=app.active_job_var,
        concurrency_var=app.queue_concurrency_var,
        on_concurrency_changed=app._runs.on_queue_concurrency_changed,
        profile_choices_provider=_active_service_profiles,
        on_select_profile=_select_active_profile,
    )
    page.preset_combo.bind("<<ComboboxSelected>>", app._options.on_preset_selected)
    app._urls.bind_drop_target(page.urls_text, stop_prop=True)
    page.queue_panel.restore_saved_queue_history()
    app._events.flush_pending_logs(page.queue_panel)
    page.queue_panel.set_running_state(
        app.is_running,
        is_previewing=app.is_previewing,
        is_syncing=app.is_syncing,
    )
    page.update_ffmpeg_banner(app._last_ffmpeg_available, app._last_ffmpeg_status_text)
    app._runs.apply_download_page_running_state(page, app.is_running)
    app._options.update_download_summaries(page)
    app._options.sync_custom_providers_ui(download_page=page)
    return page


def create_library_page(app: AudiobookDLApp, parent: ttk.Frame) -> LibraryPage:
    """Build and wire the lazy `LibraryPage`."""

    def _on_queue_urls(urls_to_add: list[str]) -> None:
        added = app._urls.append_urls_to_input(urls_to_add)
        app.status_var.set(
            f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from library"
        )
        app._show_view_in_download_action()

    def _on_fetch(mode: str, svc: ServiceInfo, query_text: str, tab: CatalogTab) -> None:
        options = app._options.try_build_options(urls_override=[])
        if options is None:
            tab.set_error_status("Invalid option value")
            return
        chosen_profile = normalize_profile_name(tab.selected_profile)
        options.profile = None if chosen_profile == DEFAULT_PROFILE else chosen_profile
        auth_service = app._auth.selected_service()
        auth_profile = normalize_profile_name(app.profile_var.get())
        if auth_service is None or auth_service.key != svc.key or auth_profile != chosen_profile:
            options.username = None
            options.password = None
            options.library = None
            options.cookie_file = None
        app.controller.start_catalog_fetch(
            mode,
            svc.key,
            query_text,
            options,
            tab,
            archive_path=app._sync.archive_ledger_path(),
        )

    def _on_add_urls(urls_to_add: list[str]) -> None:
        added = app._urls.append_urls_to_input(urls_to_add)
        app.status_var.set(
            f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from catalog"
        )
        app._show_view_in_download_action()

    def _profile_resolver(service_key: str) -> list[str]:
        return list_source_profiles(app.controller.config_snapshot(), service_key)

    def _on_save_to_library(svc_key: str, prof: str, books: Sequence[Any]) -> tuple[int, int]:
        with DownloadArchive(app._sync.archive_ledger_path()) as archive:
            inserted, updated = archive.upsert_catalog_items(
                svc_key,
                list(books),
                profile=normalize_profile_name(prof),
            )
        app.status_var.set(f"Library DB: +{inserted} new, {updated} refreshed")
        return inserted, updated

    def _status_resolver(svc_key: str, books: Sequence[Any]) -> list[str]:
        archive_path = app._sync.archive_ledger_path()
        if not os.path.exists(archive_path):
            return [""] * len(books)
        with DownloadArchive(archive_path, persistent=True) as archive:
            return archive.lookup_catalog_statuses(svc_key, list(books))

    def _on_download_urls(urls_to_download: list[str], svc_key: str, prof: str) -> None:
        norm_prof = normalize_profile_name(prof)
        if not app.is_running and svc_key:
            app._auth.select_service_profile(svc_key, norm_prof)
        app._runs.start_or_enqueue_urls(
            urls_to_download,
            profile_override=None if norm_prof == DEFAULT_PROFILE else norm_prof,
        )
        app._show_view_in_download_action()

    def _on_run_sync(targets: list[tuple[str, str]], mode: str) -> None:
        app._automation.auto_sync_targets = list(targets)
        app._automation.auto_sync_mode = mode
        app._sync.execute_sync_targets(targets, mode)

    def _open_path(path: str, reveal: bool = False) -> None:
        open_path_in_os(app.root, path, reveal_in_folder=reveal)

    return LibraryPage(
        parent,
        archive_path_provider=app._sync.archive_ledger_path,
        config_provider=app.controller.config_snapshot,
        services=app._services,
        selected_service_provider=app._auth.selected_service,
        selected_profile_provider=lambda: normalize_profile_name(app.profile_var.get()),
        on_queue_urls=_on_queue_urls,
        on_open_path=_open_path,
        on_pack_paths=app.open_pack_with_paths,
        on_upload_abs=app._sync.upload_archive_entry_to_abs,
        on_catalog_fetch=_on_fetch,
        on_catalog_add_urls=_on_add_urls,
        on_catalog_download_urls=_on_download_urls,
        on_save_to_library=_on_save_to_library,
        on_sync_from_catalog=lambda svc_key: app._sync.sync_libraries(
            services=[svc_key] if svc_key else ()
        ),
        profile_resolver=_profile_resolver,
        status_resolver=_status_resolver,
        cover_fetcher=lambda url: app.controller.fetch_cover_bytes(
            url,
            proxy=app._options.cached_proxy,
            timeout=app._options.cached_timeout,
        ),
        on_catalog_cancel=app.controller.cancel_catalog_fetch,
        auto_sync_enabled=app._automation.auto_sync_enabled,
        auto_sync_minutes=app._automation.auto_sync_minutes,
        auto_sync_mode=app._automation.auto_sync_mode,
        auto_sync_targets=app._automation.auto_sync_targets or None,
        on_run_sync=_on_run_sync,
        on_auto_sync_changed=app._automation.configure_auto_sync_timer,
        on_save_schedule=app._sync.save_sync_schedule,
        on_manage_account=lambda sk, prof: app.open_accounts(sk or None, prof),
        on_manage_accounts=lambda: app.open_accounts(),
        on_tab_changed=app.shell.sync_sub_tab,
        palette_provider=lambda: app._window.palette,
    )


def create_pack_page(app: AudiobookDLApp, parent: ttk.Frame) -> PackPage:
    """Build and wire the lazy `PackPage`."""
    return PackPage(
        parent,
        app.controller,
        options_provider=lambda: app._options.try_build_options(urls_override=[]),
        output_dir_provider=lambda: app.output_dir_var.get().strip() or default_output_dir(),
        ensure_ffmpeg=app._ffmpeg_assistant.ensure_available,
        on_submit=app._runs.on_pack_submitted,
        palette=app._window.palette,
    )


def create_accounts_page(app: AudiobookDLApp, parent: ttk.Frame) -> AccountsPage:
    """Build and wire the lazy `AccountsPage`."""
    return AccountsPage(
        parent,
        app.controller,
        app._services,
        app._auth,
        on_import_cookies=lambda sk, prof=None: app._auth.show_cookie_import_dialog(
            svc_key_override=sk,
            profile_override=prof,
        ),
        on_context_label=app.shell.set_breadcrumb_detail,
        palette=app._window.palette,
    )


def create_settings_page(app: AudiobookDLApp, parent: ttk.Frame) -> SettingsPage:
    """Build and wire the lazy `SettingsPage`."""
    config_loc = app.controller.config_location

    def _on_abs_saved(_updated: Any) -> None:
        app.controller.reload_config()
        app.status_var.set("Saved Audiobookshelf configuration.")

    def _on_save_custom_provider(
        name: str,
        *,
        url: str,
        api_key: str | None = None,
        enabled: bool = True,
    ) -> None:
        app.controller.save_custom_metadata_provider(
            name,
            url=url,
            api_key=api_key,
            enabled=enabled,
        )
        app._options.sync_custom_providers_ui()
        app.status_var.set(f"Saved custom metadata provider '{name}'.")

    def _on_delete_custom_provider(name: str) -> None:
        app.controller.delete_custom_metadata_provider(name)
        app._options.sync_custom_providers_ui()
        app.status_var.set(f"Removed custom metadata provider '{name}'.")

    return SettingsPage(
        parent,
        theme_var=app._window.theme_var,
        notify_on_complete_var=app.notify_on_complete_var,
        clipboard_monitor_var=app.clipboard_monitor_var,
        check_updates_var=app.check_updates_var,
        minimize_to_tray_var=app.minimize_to_tray_var,
        tray_available=app._window.tray_controller.is_available(),
        on_toggle_minimize_to_tray=app._window.toggle_minimize_to_tray,
        on_theme_changed=app.set_theme,
        on_toggle_notify_on_complete=app._toggle_notify_on_complete,
        on_toggle_clipboard_monitor=app._toggle_clipboard_monitor,
        on_toggle_check_updates=app._toggle_check_updates,
        on_reload_config=app._options.reload_config,
        on_check_updates=app.check_updates_interactive,
        ffmpeg_assistant=app._ffmpeg_assistant,
        abs_config_provider=lambda: app.controller.config_snapshot().audiobookshelf,
        config_location=config_loc,
        on_save_abs_config=app.controller.save_audiobookshelf_config,
        on_abs_saved=_on_abs_saved,
        custom_providers_provider=lambda: app.controller.config_snapshot().custom_metadata_providers,
        on_save_custom_provider=_on_save_custom_provider,
        on_delete_custom_provider=_on_delete_custom_provider,
        http_proxy_provider=lambda: (
            app.proxy_var.get().strip() or app.controller.config_snapshot().proxy
        ),
        http_timeout_provider=lambda: app.controller.config_snapshot().timeout,
        palette=app._window.palette,
        style=app._style,
    )

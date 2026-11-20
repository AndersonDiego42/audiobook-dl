"""Main Tkinter desktop window coordinator and launcher entry point for audiobook-dl."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
import sys
import tempfile
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from audiobookdl import __version__
from audiobookdl.config import (
    DEFAULT_AUTO_SYNC_MODE,
    VALID_AUTO_SYNC_MODES,
    normalize_profile_name,
    split_source_profile_key,
)
from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.coordinators import AutomationCoordinator, UrlInputCoordinator
from audiobookdl.gui.dnd import create_tk_root
from audiobookdl.gui.event_dispatcher import EventDispatcher
from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
from audiobookdl.gui.notifications import set_windows_process_app_id
from audiobookdl.gui.options_state import OptionsCoordinator
from audiobookdl.gui.options_view_model import OptionsViewModel
from audiobookdl.gui.page_factories import (
    create_accounts_page,
    create_download_page,
    create_library_page,
    create_pack_page,
    create_settings_page,
)
from audiobookdl.gui.preview_coordinator import PreviewCoordinator
from audiobookdl.gui.queue_panel import ActivityQueuePanel
from audiobookdl.gui.run_coordinator import RunCoordinator
from audiobookdl.gui.shell import NAV_ITEMS, NavRoute, SidebarShell
from audiobookdl.gui.sync_coordinator import SyncCoordinator
from audiobookdl.gui.theme import (
    ThemePalette,
    apply_theme_to_widget_tree,
    enable_high_dpi_awareness,
)
from audiobookdl.gui.window_manager import WindowManager, build_menu_bar
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.sources import ServiceInfo, get_available_services
from audiobookdl.utils import read_asset_file

if TYPE_CHECKING:
    from audiobookdl.gui.pages.accounts_page import AccountsPage
    from audiobookdl.gui.pages.download_page import DownloadPage
    from audiobookdl.gui.pages.library_page import LibraryPage
    from audiobookdl.gui.pages.pack_page import PackPage


class AudiobookDLApp:
    """Main Tkinter desktop window coordinator for audiobook-dl."""

    MAX_LOG_LINES = ActivityQueuePanel.MAX_LOG_LINES

    # Populated dynamically from `GUI_OPTION_SPECS` via `OptionsCoordinator.init_variables`.
    combine_var: tk.BooleanVar
    remove_chars_var: tk.StringVar
    debug_var: tk.BooleanVar
    cover_only_var: tk.BooleanVar
    no_chapters_var: tk.BooleanVar
    split_chapters_var: tk.BooleanVar
    output_format_var: tk.StringVar
    media_type_var: tk.StringVar
    skip_downloaded_var: tk.BooleanVar
    skip_unavailable_var: tk.BooleanVar
    download_archive_var: tk.BooleanVar
    write_json_var: tk.BooleanVar
    write_cover_var: tk.BooleanVar
    write_opf_var: tk.BooleanVar
    write_abs_metadata_var: tk.BooleanVar
    write_cue_var: tk.BooleanVar
    resume_var: tk.BooleanVar
    audio_bitrate_var: tk.StringVar
    sample_rate_var: tk.StringVar
    overwrite_var: tk.StringVar
    max_workers_var: tk.StringVar
    book_delay_var: tk.StringVar
    limit_rate_var: tk.StringVar
    proxy_var: tk.StringVar
    timeout_var: tk.StringVar
    max_retries_var: tk.StringVar
    impersonate_var: tk.StringVar
    decryption_key_var: tk.StringVar
    enrich_metadata_var: tk.BooleanVar
    enrich_providers_var: tk.StringVar
    enrich_interactive_var: tk.BooleanVar
    loudnorm_var: tk.BooleanVar
    mono_var: tk.BooleanVar
    chapters_from_silence_var: tk.BooleanVar
    override_chapters_var: tk.BooleanVar
    chapter_offset_ms_var: tk.StringVar
    intro_chapter_mode_var: tk.StringVar
    square_cover_var: tk.BooleanVar
    max_cover_size_var: tk.StringVar
    post_exec_var: tk.StringVar
    upload_to_abs_var: tk.BooleanVar

    # Non-spec GUI variables populated in `OptionsCoordinator.init_variables`.
    output_dir_var: tk.StringVar
    template_var: tk.StringVar
    template_preset_var: tk.StringVar
    template_preview_var: tk.StringVar
    template_standalone_preview_var: tk.StringVar
    service_label_var: tk.StringVar
    profile_var: tk.StringVar
    service_hint_var: tk.StringVar
    saved_badge_var: tk.StringVar
    cookie_file_var: tk.StringVar
    username_var: tk.StringVar
    password_var: tk.StringVar
    library_var: tk.StringVar
    show_password_var: tk.BooleanVar
    remember_login_var: tk.BooleanVar
    m4b_var: tk.BooleanVar
    notify_on_complete_var: tk.BooleanVar
    clipboard_monitor_var: tk.BooleanVar
    minimize_to_tray_var: tk.BooleanVar
    check_updates_var: tk.BooleanVar
    status_var: tk.StringVar
    active_job_var: tk.StringVar
    progress_var: tk.DoubleVar
    batch_var: tk.StringVar
    metrics_var: tk.StringVar
    ffmpeg_status_var: tk.StringVar

    def __init__(self, root: tk.Tk, config_location: str | None = None) -> None:
        self.root = root
        self._controller = GuiController(config_location)
        self.root.title(f"audiobook-dl {__version__}")

        self._style = ttk.Style()
        self.queue_concurrency_var = tk.IntVar(value=self._controller.queue_concurrency)
        self._services: list[ServiceInfo] = get_available_services()
        self._last_ffmpeg_available = True
        self._last_ffmpeg_status_text = ""

        self._options = OptionsCoordinator(
            self.root,
            self,
            self._controller,
            self._services,
            on_log=lambda msg: self._events.append_log(msg),
        )
        self._options.init_variables()

        self._window = WindowManager(
            self.root,
            self._controller,
            self._style,
            services_count=len(self._services),
            status_var=self.status_var,
            minimize_to_tray_var=self.minimize_to_tray_var,
            on_sync_now=lambda: self._sync.sync_from_tray(),
            on_exit=self._on_tray_exit,
            on_theme_applied=self._on_theme_applied,
            on_tray_started=lambda: self._events.schedule_poll_soon(250),
            on_dpi_changed=self._on_dpi_changed,
        )

        cfg_gui = self._controller.config.gui
        self._automation = AutomationCoordinator(
            self.root,
            is_clipboard_enabled=lambda: self.clipboard_monitor_var.get(),
            append_urls=lambda urls: self._urls.append_urls_to_input(urls),
            save_clipboard_pref=lambda en: self._controller.save_gui_preferences(clipboard_monitor=en),
            set_status=self.status_var.set,
            can_run_auto_sync=lambda: (
                not self.is_running and not self.is_syncing and not self.is_previewing
            ),
            execute_sync_targets=lambda targets, mode: self._sync.execute_sync_targets(targets, mode),
            sync_libraries=lambda: self._sync.sync_libraries(),
        )
        self._automation.auto_sync_enabled = bool(cfg_gui.auto_sync_enabled)
        self._automation.auto_sync_minutes = (
            max(1, cfg_gui.auto_sync_minutes)
            if cfg_gui.auto_sync_minutes is not None
            else 30
        )
        self._automation.auto_sync_mode = (
            cfg_gui.auto_sync_mode
            if cfg_gui.auto_sync_mode in VALID_AUTO_SYNC_MODES
            else DEFAULT_AUTO_SYNC_MODE
        )
        self._automation.auto_sync_targets = (
            [split_source_profile_key(t) for t in cfg_gui.auto_sync_targets]
            if cfg_gui.auto_sync_targets is not None
            else []
        )

        self._auth = ServiceAuthCoordinator(
            self.root,
            self._controller,
            self._services,
            status_var=self.status_var,
            service_label_var=self.service_label_var,
            profile_var=self.profile_var,
            service_hint_var=self.service_hint_var,
            saved_badge_var=self.saved_badge_var,
            cookie_file_var=self.cookie_file_var,
            username_var=self.username_var,
            password_var=self.password_var,
            library_var=self.library_var,
            show_password_var=self.show_password_var,
            remember_login_var=self.remember_login_var,
            palette_provider=lambda: self._window.palette,
            build_verify_options=lambda: self._options.try_build_options(urls_override=[]),
            is_running_provider=lambda: self.is_running,
            on_log=lambda msg: self._events.append_log(msg),
            on_credentials_changed=self._on_credentials_changed,
        )

        self._ffmpeg_assistant = FFmpegAssistant(
            self.root,
            self._controller,
            status_var=self.status_var,
            ffmpeg_status_var=self.ffmpeg_status_var,
            on_log=lambda msg: self._events.append_log(msg),
            on_status_changed=self._on_ffmpeg_status_changed,
        )

        self.shell = SidebarShell(
            self.root,
            palette_provider=lambda: self._window.palette,
            status_var=self.status_var,
            active_job_var=self.active_job_var,
            progress_var=self.progress_var,
            on_toggle_pause=self.toggle_pause_queue,
            on_cancel=self.cancel_download,
        )
        self.shell.pack(fill=tk.BOTH, expand=True)

        self._options.bind_collaborators(
            shell=self.shell,
            auth=self._auth,
            automation=self._automation,
            set_theme=self.set_theme,
        )

        self._urls = UrlInputCoordinator(
            self.root,
            status_var=self.status_var,
            auth=self._auth,
            get_download_page=lambda: self.shell.get_page("download"),
            ensure_download_page=lambda: self.download_page,
            navigate_download=lambda: self.navigate("download"),
            open_pack_with_paths=lambda paths: self.open_pack_with_paths(paths),
            start_download=lambda: self.start_download(),
        )
        self._runs = RunCoordinator(
            self.root,
            self._controller,
            auth=self._auth,
            ffmpeg_assistant=self._ffmpeg_assistant,
            options=self._options,
            shell=self.shell,
            status_var=self.status_var,
            queue_concurrency_var=self.queue_concurrency_var,
            notify_on_complete_var=self.notify_on_complete_var,
            is_previewing_provider=lambda: self.is_previewing,
            is_syncing_provider=lambda: self.is_syncing,
            schedule_poll_soon=lambda: self._events.schedule_poll_soon(),
            show_view_in_download_action=self._show_view_in_download_action,
        )
        self._preview = PreviewCoordinator(
            self.root,
            self._controller,
            auth=self._auth,
            ffmpeg_assistant=self._ffmpeg_assistant,
            options=self._options,
            shell=self.shell,
            status_var=self.status_var,
            active_job_var=self.active_job_var,
            remember_login_var=self.remember_login_var,
            skip_downloaded_var=self.skip_downloaded_var,
            download_archive_var=self.download_archive_var,
            palette_provider=lambda: self._window.palette,
            is_running_provider=lambda: self.is_running,
            is_syncing_provider=lambda: self.is_syncing,
            schedule_poll_soon=lambda: self._events.schedule_poll_soon(),
            start_new_run=self._runs.start_new_run,
        )
        self._sync = SyncCoordinator(
            self.root,
            self._controller,
            automation=self._automation,
            ffmpeg_assistant=self._ffmpeg_assistant,
            options=self._options,
            shell=self.shell,
            status_var=self.status_var,
            is_running_provider=lambda: self.is_running,
            is_previewing_provider=lambda: self.is_previewing,
            is_in_tray_provider=lambda: self._window.tray_controller.is_running,
            schedule_poll_soon=lambda: self._events.schedule_poll_soon(),
            append_log=lambda msg: self._events.append_log(msg),
            register_enqueued_batch=self._runs.register_enqueued_batch,
        )
        self._events = EventDispatcher(
            self.root,
            self._controller,
            shell=self.shell,
            status_var=self.status_var,
            active_job_var=self.active_job_var,
            progress_var=self.progress_var,
            check_updates_var=self.check_updates_var,
            palette_provider=lambda: self._window.palette,
            is_running_provider=lambda: self.is_running,
            is_previewing_provider=lambda: self.is_previewing,
            is_syncing_provider=lambda: self.is_syncing,
            on_preview_done=self._preview.handle_preview_done,
            on_auth_verified=self._auth.handle_auth_verified,
            on_oauth_connected=self._auth.handle_oauth_connected,
            on_oauth_disconnected=self._auth.handle_oauth_disconnected,
            on_ffmpeg_install_done=self._ffmpeg_assistant.handle_install_done,
            on_sync_done=self._sync.handle_sync_done,
            on_sync_enqueue_ready=self._sync.handle_sync_enqueue_ready,
            on_run_done=self._runs.finish_run,
            is_tray_running_provider=lambda: self._window.tray_controller.is_running,
            drain_tray_actions=lambda: self._window.tray_controller.drain_pending_actions(),
        )

        self.shell.register_page("download", lambda p: create_download_page(self, p))
        self.shell.register_page("library", lambda p: create_library_page(self, p))
        self.shell.register_page("pack", lambda p: create_pack_page(self, p))
        self.shell.register_page("accounts", lambda p: create_accounts_page(self, p))
        self.shell.register_page("settings", lambda p: create_settings_page(self, p))

        build_menu_bar(
            self.root,
            theme_var=self._window.theme_var,
            on_browse_url_file=self._urls.browse_url_file,
            on_open_output_folder=self._options.open_output_folder,
            on_reload_config=self._options.reload_config,
            on_exit=lambda: self._on_close(force=True),
            on_go_back=self.shell.go_back,
            on_go_forward=self.shell.go_forward,
            on_set_theme=self.set_theme,
            on_navigate=self.navigate,
            on_open_library_tab=self.open_library_tab,
            on_check_updates=self.check_updates_interactive,
            on_show_about=self._window.show_about_dialog,
            on_preview_metadata=self.preview_metadata,
            on_start_download=self.start_download,
        )
        self._urls.install_root_drop()
        self._options.wire_preview_traces()

        self._auth.load_service_into_form()
        self._ffmpeg_assistant.update_status()

        saved_route = NavRoute.parse(self._controller.config.gui.active_page or "")
        valid_pages = {spec.page_id for spec in NAV_ITEMS}
        if saved_route.page_id not in valid_pages:
            initial_route = NavRoute("download")
        elif saved_route.page_id == "library" and saved_route.sub_tab not in (
            None,
            "archive",
            "catalog",
            "sync",
        ):
            initial_route = NavRoute("library")
        else:
            initial_route = saved_route
        self.shell.navigate(initial_route, record_history=False, apply_theme_on_create=False)
        apply_theme_to_widget_tree(self.root, self._window.palette)
        if initial_route.page_id != "download":
            self._events.refresh_saved_queue_badge()

        self.root.bind("<Configure>", self._window.on_root_configure, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._events.start_polling()
        self._window.sync_system_theme_watch()
        if self.clipboard_monitor_var.get():
            self._automation.apply_clipboard_monitor(quiet=True, persist=False)
        if self._automation.auto_sync_enabled:
            self._automation.configure_auto_sync_timer(True, self._automation.auto_sync_minutes)

    @property
    def controller(self) -> GuiController:
        return self._controller

    @property
    def view_model(self) -> OptionsViewModel:
        """Typed holder for all GUI option/status Tk variables."""
        vm = self._options.view_model
        assert vm is not None
        return vm

    def __getattr__(self, name: str) -> Any:
        # Delegate ``<name>_var`` lookups to the OptionsViewModel so call sites
        # keep using ``app.<name>_var`` without dynamic setattr stamping.
        options = self.__dict__.get("_options")
        if options is None:
            raise AttributeError(name)
        view_model = options.view_model
        if view_model is None:
            raise AttributeError(name)
        return getattr(view_model, name)

    @property
    def theme_mode(self) -> str:
        return self._window.theme_mode

    @property
    def is_running(self) -> bool:
        return self._runs.is_running

    @property
    def is_previewing(self) -> bool:
        return self._preview.is_previewing

    @property
    def is_syncing(self) -> bool:
        return self._sync.is_syncing

    @property
    def download_page(self) -> DownloadPage:
        return self.shell.ensure_page("download")

    def set_theme(self, mode: str) -> ThemePalette:
        """Switch between `'system'`, `'light'`, and `'dark'` themes live."""
        return self._window.set_theme(mode)

    def navigate(
        self,
        target: str | NavRoute,
        *,
        record_history: bool = True,
    ) -> Any:
        """Navigate to a sidebar page (`download`, `library`, `library:catalog`, etc.)."""
        return self.shell.navigate(target, record_history=record_history)

    def open_library_tab(self, tab_key: str) -> LibraryPage:
        """Navigate to the Library page and select `archive`, `catalog`, or `sync`."""
        return self.navigate(NavRoute("library", sub_tab=tab_key))

    def open_accounts(
        self,
        service_key: str | None = None,
        profile: str | None = None,
    ) -> AccountsPage:
        """Navigate to Accounts, optionally pre-selecting `service_key` and `profile`."""
        return self.navigate(NavRoute("accounts", service_key=service_key, profile=profile))

    def open_pack_with_paths(self, paths: Sequence[str]) -> PackPage:
        """Navigate to the Pack page and append `paths` for scanning."""
        page = self.navigate("pack")
        if paths:
            page.add_initial_paths(paths)
        return page

    def build_options(self, urls_override: Sequence[str] | None = None) -> DownloadOptions:
        """Build `DownloadOptions` from current UI field values."""
        return self._options.build_options(urls_override=urls_override)

    def start_download(self) -> None:
        self._runs.start_download()

    def preview_metadata(self, url_override: str | None = None) -> None:
        """Fetch book or series metadata for `url_override` or the first entered URL."""
        self._preview.preview_metadata(url_override=url_override)

    def cancel_download(self) -> None:
        self._runs.cancel_download()

    def toggle_pause_queue(self) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        self._runs.toggle_pause_queue()

    def check_updates_interactive(self) -> None:
        """Check for newer releases."""
        self._events.check_updates_interactive()

    def _on_theme_applied(self, _palette: ThemePalette) -> None:
        self._ffmpeg_assistant.update_status()

    def _on_dpi_changed(self) -> None:
        if hasattr(self, "shell"):
            self.shell.update_dpi_scaling()

    def _open_accounts_from_download(self) -> None:
        svc = self._auth.selected_service()
        prof = normalize_profile_name(self.profile_var.get())
        self.open_accounts(svc.key if svc is not None else None, prof)

    def _show_view_in_download_action(self) -> None:
        if self.shell.active_page_id != "download":
            self.shell.set_context_action(
                "View in Download \u2192",
                lambda: self.navigate("download"),
            )

    def _on_ffmpeg_status_changed(self, available: bool, status_text: str) -> None:
        self._last_ffmpeg_available = available
        self._last_ffmpeg_status_text = status_text
        page = self.shell.get_page("download")
        if page is not None:
            page.update_ffmpeg_banner(available, status_text)

    def _on_credentials_changed(
        self,
        service_key: str | None = None,
        profile: str | None = None,
    ) -> None:
        acct_page = self.shell.get_page("accounts")
        if acct_page is not None:
            if service_key is not None and profile is not None:
                acct_page.invalidate_verification(service_key, profile)
            acct_page.refresh()

    def _toggle_notify_on_complete(self) -> None:
        enabled = bool(self.notify_on_complete_var.get())
        try:
            self._controller.save_gui_preferences(notify_on_complete=enabled)
        except OSError:
            pass
        self.status_var.set(
            "Completion notifications enabled"
            if enabled
            else "Completion notifications disabled"
        )

    def _toggle_clipboard_monitor(self) -> None:
        self._automation.apply_clipboard_monitor(quiet=False, persist=True)

    def _toggle_check_updates(self) -> None:
        enabled = self.check_updates_var.get()
        try:
            self._controller.set_check_updates(enabled)
        except OSError:
            pass
        self.status_var.set(
            "Automatic startup update checks enabled"
            if enabled
            else "Automatic startup update checks disabled"
        )

    def _on_root_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self.root:
            self._events.cancel_poll_timer()
            self._window.cancel_theme_watch()
            self._urls.cancel_debounce()
            self._window.tray_controller.stop()
            self._automation.cancel_auto_sync()
            self._automation.cancel_clipboard()

    def _on_tray_exit(self) -> None:
        self._on_close(force=False, bypass_tray_minimize=True)

    def _has_active_background_operations(self) -> bool:
        return bool(
            self.is_running
            or self.is_syncing
            or self.is_previewing
            or self._controller.is_uploading_abs
            or self._ffmpeg_assistant.is_installing
        )

    def _on_close(
        self,
        *,
        force: bool = False,
        bypass_tray_minimize: bool = False,
    ) -> None:
        if not bypass_tray_minimize and self._window.should_minimize_on_close(force=force):
            return
        if not force and self._has_active_background_operations():
            if bypass_tray_minimize:
                self._window.restore_from_tray()
            if not messagebox.askyesno(
                "Operation in Progress",
                "A download, preview, sync, upload, or installation is currently active. Cancel and exit anyway?",
                parent=self.root,
            ):
                return
        try:
            try:
                saved_geom, maximized = self._window.collect_geometry_snapshot()
                dl_page = self.shell.get_page("download")
                split_ratio = (
                    dl_page.get_split_ratio()
                    if dl_page is not None
                    else self._controller.config.gui.download_split_ratio
                )
                options_collapsed = (
                    dl_page.options_section.is_collapsed
                    if dl_page is not None
                    else self._controller.config.gui.options_collapsed
                )
                naming_collapsed = (
                    dl_page.naming_section.is_collapsed
                    if dl_page is not None
                    else self._controller.config.gui.output_naming_collapsed
                )
                active_route = self.shell.active_route
                if (
                    active_route is not None
                    and active_route.page_id == "library"
                    and active_route.sub_tab in ("catalog", "sync")
                ):
                    saved_active_page: str | None = active_route.to_key()
                else:
                    saved_active_page = self.shell.active_page_id
                self._controller.save_window_preferences(
                    window_geometry=saved_geom,
                    window_maximized=maximized,
                    download_split_ratio=split_ratio,
                    active_page=saved_active_page,
                    options_collapsed=options_collapsed,
                    output_naming_collapsed=naming_collapsed,
                    output_dir=self.output_dir_var.get().strip(),
                    output_template=self.template_var.get().strip(),
                    use_download_archive=self.download_archive_var.get(),
                    theme=self._window.theme_mode,
                    notify_on_complete=self.notify_on_complete_var.get(),
                    clipboard_monitor=self.clipboard_monitor_var.get(),
                    queue_concurrency=self._controller.queue_concurrency,
                    minimize_to_tray=self.minimize_to_tray_var.get(),
                    output_prefs=self._options.read_persisted_gui_preferences(),
                )
            except (OSError, tk.TclError):
                pass
        finally:
            self._events.cancel_poll_timer()
            self._window.cancel_theme_watch()
            self._urls.cancel_debounce()
            self._window.tray_controller.stop()
            self._automation.cancel_auto_sync()
            self._automation.cancel_clipboard()
            self._controller.shutdown(
                is_running=self.is_running,
                is_previewing=self.is_previewing,
            )
            try:
                if self.root.winfo_exists():
                    self.root.destroy()
            except tk.TclError:
                pass


class _WindowedArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never crashes when sys.stdout/sys.stderr are None (--windowed builds)."""

    def _print_message(self, message: str, file: Any = None) -> None:
        if not message:
            return
        target = sys.stderr if file is None else file
        if target is None:
            return
        target.write(message)


def parse_gui_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse startup arguments supported by the desktop GUI application."""
    parser = _WindowedArgumentParser(prog="audiobook-dl", exit_on_error=False)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate bundled assets and GUI initialization without opening a window",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_location",
        default=None,
        help="Path to an audiobook-dl TOML configuration file",
    )
    try:
        parsed, _unknown = parser.parse_known_args(list(argv) if argv is not None else None)
    except argparse.ArgumentError as exc:
        parser.error(str(exc))
    return parsed


def run_self_test(check_gui: bool = True) -> None:
    """Validate bundled assets, dynamic packages, and GUI startup for frozen builds."""
    from Crypto.Cipher import AES
    import pycountry
    from audiobookdl.config import tomllib

    chapter_tpl = read_asset_file("assets/ffmpeg_chapter_template.txt")
    if "{title}" not in chapter_tpl:
        raise RuntimeError("Invalid assets/ffmpeg_chapter_template.txt")
    if pycountry.languages.get(alpha_2="en") is None:
        raise RuntimeError("pycountry language database unavailable")
    parsed_toml = tomllib.loads('output_template = "{title}"\n')
    if parsed_toml.get("output_template") != "{title}":
        raise RuntimeError("TOML parser check failed")
    cipher = AES.new(b"0123456789abcdef", AES.MODE_CBC, b"fedcba9876543210")
    if len(cipher.encrypt(b"0123456789abcdef")) != 16:
        raise RuntimeError("Cryptodome AES check failed")

    if check_gui:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = tk.Tk()
            root.withdraw()
            try:
                app = AudiobookDLApp(
                    root,
                    config_location=os.path.join(tmpdir, "audiobook-dl.toml"),
                )
                root.update_idletasks()
                if not root.title().startswith("audiobook-dl"):
                    raise RuntimeError(f"Unexpected window title: {root.title()!r}")
                _ = app.build_options()
            finally:
                root.destroy()


def main(argv: Sequence[str] | None = None) -> None:
    """Launch the audiobook-dl desktop GUI."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parsed = parse_gui_args(raw_argv)
    if parsed.self_test:
        run_self_test(check_gui=True)
        return
    enable_high_dpi_awareness()
    set_windows_process_app_id()
    root = create_tk_root()
    app = AudiobookDLApp(root, config_location=parsed.config_location)
    app._options.prompt_pending_post_exec()
    app._events.schedule_update_check()
    root.mainloop()


if __name__ == "__main__":
    main()

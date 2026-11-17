"""Main Tkinter desktop window coordinator and launcher entry point for audiobook-dl."""

import argparse
from collections import Counter, deque
import dataclasses
import importlib.resources
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any
from collections.abc import Mapping, Sequence

from audiobookdl import __version__, args, output
from audiobookdl.config import (
    DEFAULT_PROFILE,
    is_portable_mode,
    list_source_profiles,
    make_source_profile_key,
    normalize_profile_name,
    split_source_profile_key,
)
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.gui.coordinators import (
    AutomationCoordinator,
    partition_input_tokens,
    series_book_exists,
    split_dropped_tokens,
)
from audiobookdl.gui.dnd import create_tk_root, install_windows_file_drop
from audiobookdl.gui.events import (
    AbsUploadDoneEvent,
    AuthVerifiedEvent,
    BatchItemEvent,
    CatalogLoadedEvent,
    ControllerEvent,
    FfmpegInstallDoneEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogBatchEvent,
    LogEvent,
    PreviewDoneEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
    PromptChoiceEvent,
    PromptConfirmEvent,
    PromptInputEvent,
    RunDoneEvent,
    SyncDoneEvent,
    SyncEnqueueReadyEvent,
    UpdateCheckedEvent,
)
from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
from audiobookdl.gui.formatting import (
    PRESET_LABEL_TO_TEMPLATE,
    SAMPLE_PREVIEW_METADATA,
    TEMPLATE_TO_PRESET_LABEL,
    clamp_geometry_to_monitors,
    clamp_rect_to_monitor,
    default_output_dir,
    format_geometry,
    get_monitor_work_areas,
    is_valid_geometry,
    parse_url_text,
)
from audiobookdl.gui.notifications import set_windows_process_app_id
from audiobookdl.gui.pages import (
    AccountsPage,
    CatalogTab,
    DownloadPage,
    LibraryPage,
    PackPage,
    SettingsPage,
)
from audiobookdl.gui.preview_dialog import ChoiceDialog, PreviewDialog
from audiobookdl.gui.queue_panel import ActivityQueuePanel
from audiobookdl.gui.shell import NAV_ITEMS, SidebarShell
from audiobookdl.gui.theme import (
    THEME_LABELS,
    THEME_MODES,
    ThemePalette,
    apply_theme,
    apply_theme_to_widget_tree,
    apply_theme_to_window,
    enable_high_dpi_awareness,
    make_bold_font,
    normalize_theme_mode,
    scale_px,
)
from audiobookdl.gui.widgets import ModalDialog
from audiobookdl.options_schema import GUI_OPTION_SPECS, PERSISTED_GUI_OPTION_SPECS
from audiobookdl.pack import SUPPORTED_PACK_EXTENSIONS
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources import ServiceInfo, detect_service_key, get_available_services
from audiobookdl.sources.source import Source
from audiobookdl.utils import read_asset_file
from audiobookdl.utils.archive import DownloadArchive, resolve_archive_path
from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata, Result, Series
from audiobookdl.utils.updater import UpdateInfo

SAMPLE_STANDALONE_METADATA = AudiobookMetadata(
    title="Project Hail Mary",
    authors=["Andy Weir"],
    source="audible",
)


class AboutDialog(ModalDialog):
    """Themed modal About dialog displaying version, mode, and project link."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        services_count: int,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "About audiobook-dl",
            palette=palette,
            resizable=(False, False),
        )
        body = ttk.Frame(self, padding=scale_px(self, 16))
        body.pack(fill=tk.BOTH, expand=True)

        mode_suffix = " [Portable]" if is_portable_mode() else ""
        ttk.Label(
            body,
            text=f"audiobook-dl  v{__version__}{mode_suffix}",
            font=make_bold_font(self, 12),
        ).pack(anchor="w", pady=(0, 6))

        self._info_text = (
            f"audiobook-dl v{__version__}{mode_suffix}\n"
            f"Supported services: {services_count}\n"
            "https://github.com/jo1gi/audiobook-dl"
        )
        ttk.Label(
            body,
            text=f"Supported services: {services_count}\nhttps://github.com/jo1gi/audiobook-dl",
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Copy Info", command=self._copy_info).pack(side=tk.LEFT)
        close_btn = ttk.Button(btns, text="Close", command=self.destroy, style="Accent.TButton")
        close_btn.pack(side=tk.RIGHT)
        self.finalize_modal(initial_focus=close_btn)

    def _copy_info(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._info_text)


class AudiobookDLApp:
    """Main Tkinter desktop window coordinator for audiobook-dl."""

    MAX_LOG_LINES = ActivityQueuePanel.MAX_LOG_LINES
    DEFAULT_WIDTH_DP = 960
    DEFAULT_HEIGHT_DP = 680
    MIN_WIDTH_DP = 760
    MIN_HEIGHT_DP = 520

    # Populated dynamically from `GUI_OPTION_SPECS` in `_init_variables`.
    combine_var: tk.BooleanVar
    remove_chars_var: tk.StringVar
    debug_var: tk.BooleanVar
    cover_only_var: tk.BooleanVar
    no_chapters_var: tk.BooleanVar
    split_chapters_var: tk.BooleanVar
    output_format_var: tk.StringVar
    media_type_var: tk.StringVar
    skip_downloaded_var: tk.BooleanVar
    download_archive_var: tk.BooleanVar
    write_json_var: tk.BooleanVar
    write_cover_var: tk.BooleanVar
    write_opf_var: tk.BooleanVar
    write_abs_metadata_var: tk.BooleanVar
    write_cue_var: tk.BooleanVar
    resume_var: tk.BooleanVar
    audio_bitrate_var: tk.StringVar
    overwrite_var: tk.StringVar
    max_workers_var: tk.StringVar
    book_delay_var: tk.StringVar
    limit_rate_var: tk.StringVar
    proxy_var: tk.StringVar
    timeout_var: tk.StringVar
    impersonate_var: tk.StringVar
    enrich_metadata_var: tk.BooleanVar
    enrich_providers_var: tk.StringVar
    enrich_interactive_var: tk.BooleanVar
    loudnorm_var: tk.BooleanVar
    mono_var: tk.BooleanVar
    chapters_from_silence_var: tk.BooleanVar
    chapter_offset_ms_var: tk.StringVar
    square_cover_var: tk.BooleanVar
    max_cover_size_var: tk.StringVar
    post_exec_var: tk.StringVar
    upload_to_abs_var: tk.BooleanVar

    def __init__(self, root: tk.Tk, config_location: str | None = None) -> None:
        self.root = root
        self._controller = GuiController(config_location)
        self.root.title(f"audiobook-dl {__version__}")

        self._normal_geometry: str | None = None
        self._is_maximized = False
        self._restore_window_geometry()
        self._apply_window_icon()

        self._style = ttk.Style()
        self._theme_mode = normalize_theme_mode(self._controller.config.gui.theme)
        self._theme_var = tk.StringVar(value=self._theme_mode)
        self._palette: ThemePalette = apply_theme(self.root, self._style, self._theme_mode)

        cfg_gui = self._controller.config.gui
        self._services: list[ServiceInfo] = get_available_services()
        self._is_running = False
        self._is_previewing = False
        self._is_syncing = False
        self._preview_dialog: PreviewDialog | None = None
        self._about_dialog: AboutDialog | None = None
        self._last_ffmpeg_available = True
        self._last_ffmpeg_status_text = ""
        self._pending_log_entries: deque[tuple[str | None, str]] = deque(
            maxlen=ActivityQueuePanel.MAX_LOG_LINES
        )

        self._init_variables()

        self._automation = AutomationCoordinator(
            self.root,
            is_clipboard_enabled=lambda: bool(self.clipboard_monitor_var.get()),
            append_urls=self._append_urls_to_input,
            save_clipboard_pref=lambda en: self._controller.save_gui_preferences(clipboard_monitor=en),
            set_status=self.status_var.set,
            can_run_auto_sync=lambda: (
                not self._is_running and not self._is_syncing and not self._is_previewing
            ),
            execute_sync_targets=self._execute_sync_targets,
            sync_libraries=self._sync_libraries,
        )
        self._automation.auto_sync_enabled = bool(cfg_gui.auto_sync_enabled)
        self._automation.auto_sync_minutes = (
            max(1, int(cfg_gui.auto_sync_minutes))
            if cfg_gui.auto_sync_minutes is not None
            else 30
        )
        self._automation.auto_sync_mode = (
            cfg_gui.auto_sync_mode
            if cfg_gui.auto_sync_mode in ("enqueue", "scan_only", "dry_run")
            else "enqueue"
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
            palette_provider=lambda: self._palette,
            build_verify_options=lambda: self._try_build_options(urls_override=[]),
            is_running_provider=lambda: self._is_running,
            on_log=self._append_log,
            on_credentials_changed=self._on_credentials_changed,
        )

        self._ffmpeg_assistant = FFmpegAssistant(
            self.root,
            self._controller,
            status_var=self.status_var,
            ffmpeg_status_var=self.ffmpeg_status_var,
            on_log=self._append_log,
            on_status_changed=self._on_ffmpeg_status_changed,
        )

        self._build_menu_bar()
        self._build_shell()
        apply_theme_to_widget_tree(self.root, self._palette)

        self._auth.load_service_into_form()
        self._ffmpeg_assistant.update_status()

        saved_page = (self._controller.config.gui.active_page or "").strip().lower()
        valid_pages = {spec.page_id for spec in NAV_ITEMS}
        initial_page = saved_page if saved_page in valid_pages else "download"
        self.navigate(initial_page)
        if initial_page != "download":
            self._refresh_saved_queue_badge()

        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._poll_after_id: str | None = None
        self._url_debounce_after_id: str | None = None
        self._modal_prompt_active = False
        self._deferred_modal_events: deque[ControllerEvent] = deque()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._poll_after_id = self.root.after(50, self._poll_queue)
        if self.clipboard_monitor_var.get():
            self._automation.apply_clipboard_monitor(quiet=True, persist=False)
        if self._automation.auto_sync_enabled:
            self._automation.configure_auto_sync_timer(True, self._automation.auto_sync_minutes)

    def _restore_window_geometry(self) -> None:
        min_w = scale_px(self.root, self.MIN_WIDTH_DP)
        min_h = scale_px(self.root, self.MIN_HEIGHT_DP)
        self.root.minsize(min_w, min_h)

        monitors = get_monitor_work_areas(self.root)
        saved_geometry = (self._controller.config.gui.window_geometry or "").strip()
        clamped = (
            clamp_geometry_to_monitors(saved_geometry, monitors, min_width=min_w, min_height=min_h)
            if saved_geometry
            else None
        )
        if clamped is not None:
            self._normal_geometry = clamped
            self.root.geometry(clamped)
        else:
            default_w = scale_px(self.root, self.DEFAULT_WIDTH_DP)
            default_h = scale_px(self.root, self.DEFAULT_HEIGHT_DP)
            cw, ch, cx, cy = clamp_rect_to_monitor(
                default_w,
                default_h,
                None,
                None,
                monitors,
                min_width=min_w,
                min_height=min_h,
            )
            geom = format_geometry(cw, ch, cx, cy)
            self._normal_geometry = geom
            self.root.geometry(geom)

        self._is_maximized = bool(self._controller.config.gui.window_maximized)
        if self._is_maximized:
            self._apply_maximized_state()

    def _apply_maximized_state(self) -> None:
        try:
            if self.root.state() == "withdrawn":
                return
        except tk.TclError:
            return
        try:
            self.root.state("zoomed")
            return
        except tk.TclError:
            pass
        try:
            self.root.attributes("-zoomed", True)
        except tk.TclError:
            pass

    def _is_window_maximized(self) -> bool:
        try:
            if self.root.state() == "zoomed":
                return True
        except tk.TclError:
            pass
        try:
            if bool(self.root.attributes("-zoomed")):
                return True
        except tk.TclError:
            pass
        try:
            if self.root.state() == "withdrawn":
                return self._is_maximized
        except tk.TclError:
            pass
        return False

    def _on_root_configure(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self.root:
            return
        try:
            state = self.root.state()
            if state == "zoomed":
                self._is_maximized = True
                return
            if state == "normal":
                self._is_maximized = False
                geom = self.root.geometry()
                if is_valid_geometry(geom):
                    self._normal_geometry = geom
        except tk.TclError:
            pass

    @property
    def is_maximized(self) -> bool:
        return self._is_window_maximized()

    @property
    def normal_geometry(self) -> str | None:
        return self._normal_geometry

    @property
    def theme_mode(self) -> str:
        return self._theme_mode

    def set_theme(self, mode: str) -> ThemePalette:
        """Switch between `'system'`, `'light'`, and `'dark'` themes live."""
        self._theme_mode = normalize_theme_mode(mode)
        self._theme_var.set(self._theme_mode)
        self._palette = apply_theme(self.root, self._style, self._theme_mode)
        self.shell.apply_theme(self._palette)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                apply_theme_to_window(child, self._palette)
        self._ffmpeg_assistant.update_status()
        return self._palette

    @property
    def controller(self) -> GuiController:
        return self._controller

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_previewing(self) -> bool:
        return self._is_previewing

    @property
    def download_page(self) -> DownloadPage:
        return self.shell.ensure_page("download")

    @property
    def urls_text(self) -> tk.Text:
        return self.download_page.urls_text

    @property
    def queue_tree(self) -> ttk.Treeview:
        return self.download_page.queue_panel.queue_tree

    @property
    def progressbar(self) -> ttk.Progressbar:
        return self.download_page.queue_panel.progressbar

    @property
    def log_text(self) -> Any:
        return self.download_page.queue_panel.log_text

    @property
    def download_btn(self) -> ttk.Button:
        return self.download_page.queue_panel.download_btn

    @property
    def pause_btn(self) -> ttk.Button:
        return self.download_page.queue_panel.pause_btn

    @property
    def cancel_btn(self) -> ttk.Button:
        return self.download_page.queue_panel.cancel_btn

    @property
    def preview_btn(self) -> ttk.Button:
        return self.download_page.queue_panel.preview_btn

    @property
    def pack_btn(self) -> ttk.Button:
        return self.download_page.queue_panel.pack_btn

    @property
    def _queue_panel(self) -> ActivityQueuePanel:
        return self.download_page.queue_panel

    @property
    def _options_panel(self) -> Any:
        return self.download_page.options_panel

    def _init_variables(self) -> None:
        cfg = self._controller.config
        saved_output_dir = (cfg.gui.output_dir or "").strip()
        self.output_dir_var = tk.StringVar(value=saved_output_dir or default_output_dir())
        saved_template = (cfg.gui.output_template or "").strip() or "{title}"
        self.template_var = tk.StringVar(value=saved_template)
        self.template_preset_var = tk.StringVar(
            value=TEMPLATE_TO_PRESET_LABEL.get(saved_template, "Custom")
        )
        self.template_preview_var = tk.StringVar(value="")
        self.template_standalone_preview_var = tk.StringVar(value="")

        first_label = self._services[0].label if self._services else ""
        self.service_label_var = tk.StringVar(value=first_label)
        self.profile_var = tk.StringVar(value=DEFAULT_PROFILE)
        self.service_hint_var = tk.StringVar(value="")
        self.saved_badge_var = tk.StringVar(value="")

        self.cookie_file_var = tk.StringVar(value="")
        self.username_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.library_var = tk.StringVar(value="")
        self.show_password_var = tk.BooleanVar(value=False)
        self.remember_login_var = tk.BooleanVar(value=True)

        for spec in GUI_OPTION_SPECS:
            binding = spec.gui
            if binding is None:
                continue
            cfg_val = getattr(cfg, spec.config_key, None) if spec.config_key else None
            initial = binding.init_value(cfg_val)
            var: tk.Variable = (
                tk.BooleanVar(value=bool(initial))
                if binding.var_type == "bool"
                else tk.StringVar(value=str(initial))
            )
            setattr(self, binding.var_attr, var)
        if cfg.gui.use_download_archive is not None:
            self.download_archive_var.set(bool(cfg.gui.use_download_archive))
        self.m4b_var = tk.BooleanVar(value=(self.output_format_var.get() == "m4b"))
        notify_default = True if cfg.gui.notify_on_complete is None else bool(cfg.gui.notify_on_complete)
        self.notify_on_complete_var = tk.BooleanVar(value=notify_default)

        self.clipboard_monitor_var = tk.BooleanVar(value=bool(cfg.gui.clipboard_monitor))
        self.check_updates_var = tk.BooleanVar(value=(cfg.check_updates is not False))
        self.status_var = tk.StringVar(value="Ready")
        self.active_job_var = tk.StringVar(value="Idle")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.batch_var = tk.StringVar(value="")
        self.metrics_var = tk.StringVar(value="")
        self.ffmpeg_status_var = tk.StringVar(value="")
        self._cached_proxy: str | None = None
        self._cached_timeout: float | None = None
        self._sync_cached_network_prefs()
        self.proxy_var.trace_add("write", lambda *_args: self._sync_cached_network_prefs())
        self.timeout_var.trace_add("write", lambda *_args: self._sync_cached_network_prefs())

    def _sync_cached_network_prefs(self) -> None:
        raw_proxy = self.proxy_var.get().strip()
        self._cached_proxy = raw_proxy or None
        raw_timeout = self.timeout_var.get().strip()
        try:
            parsed_timeout = float(raw_timeout) if raw_timeout else None
            self._cached_timeout = parsed_timeout if (parsed_timeout and parsed_timeout > 0) else None
        except ValueError:
            self._cached_timeout = None

    def _build_shell(self) -> None:
        self.shell = SidebarShell(
            self.root,
            palette_provider=lambda: self._palette,
            status_var=self.status_var,
            active_job_var=self.active_job_var,
            progress_var=self.progress_var,
            on_toggle_pause=self.toggle_pause_queue,
            on_cancel=self.cancel_download,
        )
        self.shell.pack(fill=tk.BOTH, expand=True)

        self.shell.register_page("download", self._create_download_page)
        self.shell.register_page("library", self._create_library_page)
        self.shell.register_page("pack", self._create_pack_page)
        self.shell.register_page("accounts", self._create_accounts_page)
        self.shell.register_page("settings", self._create_settings_page)

        self._bind_drop_target(self.root, stop_prop=False)
        if not (hasattr(self.root, "drop_target_register") and hasattr(self.root, "dnd_bind")):
            self._win_drop_installed = False

            def _ensure_win_drop(_ev: tk.Event | None = None) -> None:
                if self._win_drop_installed:
                    return
                try:
                    if not self.root.winfo_exists() or not self.root.winfo_ismapped():
                        return
                except tk.TclError:
                    return
                self._win_drop_installed = True
                install_windows_file_drop(self.root, self._on_dropped_paths)

            if self.root.winfo_ismapped():
                _ensure_win_drop()
            else:
                self.root.bind("<Map>", _ensure_win_drop, add="+")

        for var in (
            self.output_dir_var,
            self.template_var,
            self.output_format_var,
            self.combine_var,
            self.remove_chars_var,
            self.cover_only_var,
            self.split_chapters_var,
        ):
            var.trace_add("write", lambda *_args: self._update_template_preview())
        self.template_var.trace_add("write", lambda *_args: self._sync_preset_from_template())
        self._update_template_preview()

        for summary_var in (
            self.service_label_var,
            self.profile_var,
            self.saved_badge_var,
            self.output_format_var,
            self.combine_var,
            self.enrich_metadata_var,
            self.cover_only_var,
            self.template_var,
            self.template_preset_var,
        ):
            summary_var.trace_add("write", lambda *_args: self._update_download_summaries())

    def _create_download_page(self, parent: ttk.Frame) -> DownloadPage:
        cfg_gui = self._controller.config.gui
        options_collapsed = True if cfg_gui.options_collapsed is None else bool(cfg_gui.options_collapsed)
        naming_collapsed = (
            True if cfg_gui.output_naming_collapsed is None else bool(cfg_gui.output_naming_collapsed)
        )
        page = DownloadPage(
            parent,
            self.root,
            self,
            style=self._style,
            palette_provider=lambda: self._palette,
            initial_split_ratio=cfg_gui.download_split_ratio,
            options_collapsed=options_collapsed,
            output_naming_collapsed=naming_collapsed,
            on_locate_ffmpeg=self.browse_ffmpeg_dir,
            on_open_accounts=lambda: self.navigate("accounts"),
            on_open_folder=self._open_output_folder,
            on_browse_output_dir=self._browse_output_dir,
            on_paste_urls=self._paste_urls_from_clipboard,
            on_paste_and_queue=self._paste_and_queue,
            on_browse_url_file=self._browse_url_file,
            on_clear_urls=self._clear_urls,
            on_urls_modified=self._on_urls_modified,
            on_start_download=self.start_download,
            on_cancel_download=self.cancel_download,
            on_preview_metadata=self.preview_metadata,
            on_pack_local=lambda: self.navigate("pack"),
            on_retry_failed=self._retry_failed_queue_items,
            service_label_for_url=self._auth.service_label_for_url,
            on_preview_url=lambda u: self.preview_metadata(url_override=u),
            on_send_to_urls=self._send_urls_to_input,
            on_open_path=self._open_path_in_os,
            on_import_queue=self._enqueue_imported_queue_records,
            on_queue_summary_changed=self.shell.set_download_badge,
            on_progress_mode_changed=self.shell.set_footer_progress_mode,
            progress_var=self.progress_var,
            active_job_var=self.active_job_var,
        )
        page.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        self._bind_drop_target(page.urls_text, stop_prop=True)
        page.queue_panel.restore_saved_queue_history()
        self._flush_pending_logs(page.queue_panel)
        page.queue_panel.set_running_state(
            self._is_running,
            is_previewing=self._is_previewing,
            is_syncing=self._is_syncing,
        )
        page.update_ffmpeg_banner(self._last_ffmpeg_available, self._last_ffmpeg_status_text)
        self._apply_download_page_running_state(page, self._is_running)
        self._update_download_summaries(page)
        return page

    def _create_library_page(self, parent: ttk.Frame) -> LibraryPage:
        def _on_queue_urls(urls_to_add: list[str]) -> None:
            added = self._append_urls_to_input(urls_to_add)
            self.status_var.set(
                f"Queued {len(added)} URL{'s' if len(added) != 1 else ''} from library"
            )

        def _on_fetch(mode: str, svc: ServiceInfo, query_text: str, tab: CatalogTab) -> None:
            options = self._try_build_options(urls_override=[])
            if options is None:
                tab.set_error_status("Invalid option value")
                return
            chosen_profile = normalize_profile_name(tab.selected_profile)
            options.profile = None if chosen_profile == DEFAULT_PROFILE else chosen_profile
            auth_service = self._auth.selected_service()
            auth_profile = normalize_profile_name(self.profile_var.get())
            if auth_service is None or auth_service.key != svc.key or auth_profile != chosen_profile:
                options.username = None
                options.password = None
                options.library = None
                options.cookie_file = None
            self._controller.start_catalog_fetch(
                mode,
                svc.key,
                query_text,
                options,
                tab,
                archive_path=self._archive_ledger_path(),
            )

        def _on_add_urls(urls_to_add: list[str]) -> None:
            added = self._append_urls_to_input(urls_to_add)
            self.status_var.set(
                f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from catalog"
            )

        def _profile_resolver(service_key: str) -> list[str]:
            return list_source_profiles(self._controller.config_snapshot(), service_key)

        def _on_save_to_library(svc_key: str, prof: str, books: Sequence[Any]) -> tuple[int, int]:
            archive = DownloadArchive(self._archive_ledger_path())
            inserted, updated = archive.upsert_catalog_items(
                svc_key,
                list(books),
                profile=normalize_profile_name(prof),
            )
            self.status_var.set(f"Library DB: +{inserted} new, {updated} refreshed")
            return inserted, updated

        def _status_resolver(svc_key: str, books: Sequence[Any]) -> list[str]:
            archive_path = self._archive_ledger_path()
            if not os.path.exists(archive_path):
                return [""] * len(books)
            with DownloadArchive(archive_path, persistent=True) as archive:
                return archive.lookup_catalog_statuses(svc_key, list(books))

        def _on_download_urls(urls_to_download: list[str], svc_key: str, prof: str) -> None:
            norm_prof = normalize_profile_name(prof)
            if not self._is_running and svc_key:
                self._auth.select_service_profile(svc_key, norm_prof)
            self._start_or_enqueue_urls(
                urls_to_download,
                profile_override=None if norm_prof == DEFAULT_PROFILE else norm_prof,
            )

        def _on_run_sync(targets: list[tuple[str, str]], mode: str) -> None:
            self._automation.auto_sync_targets = list(targets)
            self._automation.auto_sync_mode = mode
            self._execute_sync_targets(targets, mode)

        return LibraryPage(
            parent,
            archive_path_provider=self._archive_ledger_path,
            config_provider=self._controller.config_snapshot,
            services=self._services,
            selected_service_provider=self._auth.selected_service,
            selected_profile_provider=lambda: normalize_profile_name(self.profile_var.get()),
            on_queue_urls=_on_queue_urls,
            on_open_path=self._open_path_in_os,
            on_pack_paths=self.open_pack_with_paths,
            on_upload_abs=self._upload_archive_entry_to_abs,
            on_catalog_fetch=_on_fetch,
            on_catalog_add_urls=_on_add_urls,
            on_catalog_download_urls=_on_download_urls,
            on_save_to_library=_on_save_to_library,
            on_sync_from_catalog=lambda svc_key: self._sync_libraries(
                services=[svc_key] if svc_key else ()
            ),
            profile_resolver=_profile_resolver,
            status_resolver=_status_resolver,
            cover_fetcher=lambda url: self._controller.fetch_cover_bytes(
                url,
                proxy=self._cached_proxy,
                timeout=self._cached_timeout,
            ),
            on_catalog_cancel=self._controller.cancel_catalog_fetch,
            auto_sync_enabled=self._automation.auto_sync_enabled,
            auto_sync_minutes=self._automation.auto_sync_minutes,
            auto_sync_mode=self._automation.auto_sync_mode,
            auto_sync_targets=self._automation.auto_sync_targets or None,
            on_run_sync=_on_run_sync,
            on_auto_sync_changed=self._automation.configure_auto_sync_timer,
            on_save_schedule=self._save_sync_schedule,
            palette_provider=lambda: self._palette,
        )

    def _create_pack_page(self, parent: ttk.Frame) -> PackPage:
        return PackPage(
            parent,
            self._controller,
            options_provider=lambda: self._try_build_options(urls_override=[]),
            output_dir_provider=lambda: self.output_dir_var.get().strip() or default_output_dir(),
            ensure_ffmpeg=self._ffmpeg_assistant.ensure_available,
            on_submit=self._on_pack_submitted,
            palette=self._palette,
        )

    def _create_accounts_page(self, parent: ttk.Frame) -> AccountsPage:
        return AccountsPage(
            parent,
            self._controller,
            self._services,
            self._auth,
            on_import_cookies=lambda sk: self._auth.show_cookie_import_dialog(svc_key_override=sk),
            palette=self._palette,
        )

    def _create_settings_page(self, parent: ttk.Frame) -> SettingsPage:
        from audiobookdl.config import load_config

        config_loc = self._controller.config_location

        def _on_abs_saved(_updated: Any) -> None:
            self._controller.config = load_config(config_loc)
            self.status_var.set("Saved Audiobookshelf configuration.")

        return SettingsPage(
            parent,
            theme_var=self._theme_var,
            notify_on_complete_var=self.notify_on_complete_var,
            clipboard_monitor_var=self.clipboard_monitor_var,
            check_updates_var=self.check_updates_var,
            on_theme_changed=self.set_theme,
            on_toggle_clipboard_monitor=self._toggle_clipboard_monitor,
            on_toggle_check_updates=self._toggle_check_updates,
            on_reload_config=self._reload_config,
            on_check_updates=self.check_updates_interactive,
            ffmpeg_assistant=self._ffmpeg_assistant,
            abs_config_provider=lambda: self._controller.config_snapshot().audiobookshelf,
            config_location=config_loc,
            on_abs_saved=_on_abs_saved,
            palette=self._palette,
            style=self._style,
        )

    def navigate(self, page_id: str) -> Any:
        """Navigate to a sidebar page (`download`, `library`, `pack`, `accounts`, `settings`)."""
        return self.shell.navigate(page_id)

    def open_library_tab(self, tab_key: str) -> LibraryPage:
        """Navigate to the Library page and select `archive`, `catalog`, or `sync`."""
        page = self.navigate("library")
        page.select_tab(tab_key)
        return page

    def open_pack_with_paths(self, paths: Sequence[str]) -> PackPage:
        """Navigate to the Pack page and append `paths` for scanning."""
        page = self.navigate("pack")
        if paths:
            page.add_initial_paths(paths)
        return page

    def _refresh_saved_queue_badge(self) -> None:
        records = self._controller.load_saved_queue_records()
        self._update_download_badge_from_records(records)

    def _update_download_badge_from_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        active = sum(1 for r in records if str(r.get("status") or "") in ("Running", "Queued", "Paused"))
        failed = sum(1 for r in records if str(r.get("status") or "") in ("Failed", "Interrupted"))
        self.shell.set_download_badge(active, failed)

    def _append_log(self, text: str, *, level: str | None = None) -> None:
        dl_page = self.shell.get_page("download")
        if dl_page is not None:
            if level is None:
                dl_page.queue_panel.append_log(text)
            else:
                dl_page.queue_panel.append_log_batch([(level, text)])
            return
        self._pending_log_entries.append((level, text))

    def _flush_pending_logs(self, panel: ActivityQueuePanel) -> None:
        if not self._pending_log_entries:
            return
        entries: list[tuple[str, str]] = []
        for level, text in self._pending_log_entries:
            if level is None:
                tag = panel._classify_log_tag("INFO", text)
                resolved = {"error": "ERROR", "warn": "WARNING", "debug": "DEBUG"}.get(tag, "INFO")
                entries.append((resolved, text))
            else:
                entries.append((level, text))
        self._pending_log_entries.clear()
        panel.append_log_batch(entries)

    def _handle_deferred_activity_event(self, item: ControllerEvent) -> bool:
        """Handle activity events without forcing lazy `DownloadPage` creation."""
        if isinstance(item, LogEvent):
            prefix = f"[{item.level}] " if item.level in ("DEBUG", "ERROR") else ""
            self._append_log(f"{prefix}{item.message}", level=str(item.level))
            if item.level == "INFO" and str(item.message).strip():
                self.status_var.set(str(item.message).strip().splitlines()[0][:80])
            return True
        if isinstance(item, LogBatchEvent):
            last_info = ""
            for level, message in item.entries:
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                self._append_log(f"{prefix}{message}", level=str(level))
                if level == "INFO" and str(message).strip():
                    last_info = str(message)
            if last_info:
                self.status_var.set(last_info.strip().splitlines()[0][:80])
            return True
        if isinstance(item, JobStatusEvent):
            self._controller.update_queue_record(item.job_id, item.status, item.detail)
            with self._controller.queue_lock:
                records = list(self._controller.queue_records.values())
            self._update_download_badge_from_records(records)
            return True
        if isinstance(item, JobMetaEvent):
            self._controller.update_queue_metadata(
                item.job_id,
                title=item.title or None,
                output_path=item.output_path or None,
            )
            if item.title:
                self.active_job_var.set(str(item.title).strip()[:60])
            return True
        if isinstance(item, ProgressStartEvent):
            self.progress_var.set(0.0)
            self.status_var.set(str(item.description)[:80])
            if str(item.description).strip():
                self.active_job_var.set(str(item.description).strip()[:60])
            return True
        if isinstance(item, ProgressAdvanceEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self.progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressMetricsEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self.progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressFinishEvent):
            self.progress_var.set(100.0)
            return True
        return isinstance(item, (BatchItemEvent, ItemProgressEvent))

    def _bind_drop_target(self, target: Any, *, stop_prop: bool) -> None:
        if not (hasattr(target, "drop_target_register") and hasattr(target, "dnd_bind")):
            return

        def _handler(ev: Any) -> str | None:
            self._on_url_drop(ev)
            return "break" if stop_prop else None

        try:
            target.drop_target_register("DND_Text", "DND_Files")
            target.dnd_bind("<<Drop>>", _handler)
        except Exception:
            try:
                target.drop_target_register("DND_Text")
                target.dnd_bind("<<Drop>>", _handler)
            except Exception:
                pass

    def _build_menu_bar(self) -> None:
        menubar = tk.Menu(self.root, tearoff=0)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="Load URLs from .txt...", command=self._browse_url_file, accelerator="Ctrl+O"
        )
        file_menu.add_command(label="Open Output Folder", command=self._open_output_folder)
        file_menu.add_command(label="Reload Config", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        theme_menu = tk.Menu(view_menu, tearoff=0)
        for mode in THEME_MODES:

            def _theme_cb(m: str = mode) -> None:
                self.set_theme(m)

            theme_menu.add_radiobutton(
                label=THEME_LABELS[mode],
                variable=self._theme_var,
                value=mode,
                command=_theme_cb,
            )
        view_menu.add_cascade(label="Theme", menu=theme_menu)
        view_menu.add_separator()
        for spec in NAV_ITEMS:
            view_menu.add_command(
                label=spec.label,
                command=lambda pid=spec.page_id: self.navigate(pid),
                accelerator=spec.shortcut_hint,
            )
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Check for Updates...", command=self.check_updates_interactive)
        help_menu.add_separator()
        help_menu.add_command(label="About audiobook-dl", command=self._show_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self._browse_url_file())
        self.root.bind("<Control-q>", lambda _e: self._on_close())
        self.root.bind("<Control-p>", lambda _e: self.preview_metadata())
        self.root.bind("<F5>", lambda _e: self.start_download())
        for idx, spec in enumerate(NAV_ITEMS, start=1):
            self.root.bind(f"<Control-Key-{idx}>", lambda _e, pid=spec.page_id: self.navigate(pid))

    def _on_ffmpeg_status_changed(self, available: bool, status_text: str) -> None:
        self._last_ffmpeg_available = available
        self._last_ffmpeg_status_text = status_text
        page = self.shell.get_page("download")
        if page is not None:
            page.update_ffmpeg_banner(available, status_text)

    def _on_credentials_changed(self) -> None:
        acct_page = self.shell.get_page("accounts")
        if acct_page is not None:
            acct_page.refresh()

    def browse_ffmpeg_dir(self) -> bool:
        """Let the user choose a folder containing `ffmpeg` and persist it in config."""
        return self._ffmpeg_assistant.browse_dir()

    def reset_ffmpeg_dir(self) -> None:
        """Clear any custom `ffmpeg_dir` setting and fall back to system PATH."""
        self._ffmpeg_assistant.reset_dir()

    def save_current_credentials(self, quiet: bool = False) -> None:
        """Persist the selected service's login, password, library, and cookie path."""
        self._auth.save_current_credentials(quiet=quiet)

    def forget_current_credentials(self) -> None:
        """Remove saved credentials for the currently selected service and profile."""
        self._auth.forget_current_credentials()

    def verify_current_auth(self) -> None:
        """Test authentication for the currently selected service in the background."""
        self._auth.verify_current_auth()

    def toggle_pause_queue(self) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        self.download_page.queue_panel.toggle_pause(is_running=self._is_running)
        self.shell.set_footer_running_state(
            running=self._is_running,
            paused=self._controller.is_paused(),
            previewing=self._is_previewing,
            syncing=self._is_syncing,
        )

    def _on_dropped_paths(self, tokens: Sequence[str]) -> None:
        local_paths, url_tokens = partition_input_tokens(tokens)
        if local_paths:
            self.open_pack_with_paths(local_paths)
        if url_tokens:
            self._append_urls_to_input(url_tokens)

    def _on_url_drop(self, event: Any) -> None:
        dropped = getattr(event, "data", "")
        if not dropped:
            return
        tokens = split_dropped_tokens(str(dropped))
        self._on_dropped_paths(tokens)

    def _toggle_clipboard_monitor(self) -> None:
        self._automation.apply_clipboard_monitor(quiet=False, persist=True)

    def _toggle_check_updates(self) -> None:
        enabled = bool(self.check_updates_var.get())
        try:
            self._controller.set_check_updates(enabled)
        except OSError:
            pass
        self.status_var.set(
            "Automatic startup update checks enabled"
            if enabled
            else "Automatic startup update checks disabled"
        )

    def _append_urls_to_input(self, urls: Sequence[str]) -> list[str]:
        box = self.urls_text
        existing = parse_url_text(box.get("1.0", tk.END))
        existing_set = set(existing)
        added = [u for u in urls if u not in existing_set]
        if added:
            combined = "\n".join([*existing, *added])
            box.delete("1.0", tk.END)
            box.insert("1.0", combined)
            self._on_urls_changed()
        return added

    def _on_urls_modified(self, _event: tk.Event | None = None) -> None:
        try:
            if not self.urls_text.edit_modified():
                return
            self.urls_text.edit_modified(False)
        except tk.TclError:
            return
        if self._url_debounce_after_id is not None:
            try:
                self.root.after_cancel(self._url_debounce_after_id)
            except tk.TclError:
                pass
        self._url_debounce_after_id = self.root.after(100, self._on_urls_changed)

    def _on_urls_changed(self, _event: tk.Event | None = None) -> None:
        self._url_debounce_after_id = None
        dl_page = self.shell.get_page("download")
        if dl_page is None:
            return
        raw_text = dl_page.urls_text.get("1.0", tk.END)
        urls = parse_url_text(raw_text)
        self._auth.on_urls_changed(urls)

        dl_page.urls_text.tag_remove("warning_line", "1.0", tk.END)
        counts: Counter[str] = Counter()
        valid_count = 0
        for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            tokens = stripped.split()
            line_valid = True
            for tok in tokens:
                is_http = tok.lower().startswith(("http://", "https://"))
                svc_key = detect_service_key(tok)
                if is_http:
                    valid_count += 1
                    label = self._auth.service_label_for_url(tok) if svc_key else "Generic"
                    counts[label] += 1
                else:
                    line_valid = False
            if not line_valid:
                dl_page.urls_text.tag_add("warning_line", f"{line_no}.0", f"{line_no}.end")

        if valid_count == 0:
            dl_page.url_frame.configure(text="Audiobook URL(s)")
        else:
            breakdown = ", ".join(f"{cnt} {lbl}" for lbl, cnt in counts.most_common(3))
            dl_page.url_frame.configure(
                text=f"Audiobook URL(s) \u2014 {valid_count} URL{'s' if valid_count != 1 else ''} ({breakdown})"
            )

    def _update_download_summaries(self, page: DownloadPage | None = None) -> None:
        dl_page = page if page is not None else self.shell.get_page("download")
        if dl_page is None:
            return
        dl_page.update_account_chip(
            self.service_label_var.get(),
            self.profile_var.get() or DEFAULT_PROFILE,
            self.saved_badge_var.get(),
        )
        preset = self.template_preset_var.get() or "Custom"
        tpl = self.template_var.get().strip() or "{title}"
        dl_page.naming_section.set_summary(f"{preset} ({tpl})")

        fmt = self.output_format_var.get() or "Original"
        flags: list[str] = []
        if self.combine_var.get():
            flags.append("Combine")
        if self.enrich_metadata_var.get():
            flags.append("Enrich")
        if self.cover_only_var.get():
            flags.append("Cover only")
        suffix = f" ({', '.join(flags)})" if flags else ""
        dl_page.options_section.set_summary(f"Format: {fmt}{suffix}")

    def _reload_config(self) -> None:
        try:
            cfg = self._controller.reload_config()
            if (cfg.gui.output_dir or "").strip():
                self.output_dir_var.set((cfg.gui.output_dir or "").strip())
            if (cfg.gui.output_template or "").strip():
                self.template_var.set((cfg.gui.output_template or "").strip())
            for spec in GUI_OPTION_SPECS:
                binding = spec.gui
                if binding is None or not spec.config_key:
                    continue
                cfg_val = getattr(cfg, spec.config_key, None)
                getattr(self, binding.var_attr).set(binding.init_value(cfg_val))
            if cfg.gui.use_download_archive is not None:
                self.download_archive_var.set(bool(cfg.gui.use_download_archive))
            if cfg.gui.notify_on_complete is not None:
                self.notify_on_complete_var.set(bool(cfg.gui.notify_on_complete))
            if cfg.gui.clipboard_monitor is not None:
                self.clipboard_monitor_var.set(bool(cfg.gui.clipboard_monitor))
                self._automation.apply_clipboard_monitor(quiet=True, persist=False)
            self.check_updates_var.set(cfg.check_updates is not False)
            if cfg.gui.auto_sync_enabled is not None:
                self._automation.auto_sync_enabled = bool(cfg.gui.auto_sync_enabled)
            if cfg.gui.auto_sync_minutes is not None:
                self._automation.auto_sync_minutes = max(1, int(cfg.gui.auto_sync_minutes))
            if cfg.gui.auto_sync_mode in ("enqueue", "scan_only", "dry_run"):
                self._automation.auto_sync_mode = cfg.gui.auto_sync_mode
            if cfg.gui.auto_sync_targets is not None:
                self._automation.auto_sync_targets = [
                    split_source_profile_key(t) for t in cfg.gui.auto_sync_targets
                ]
            self._automation.configure_auto_sync_timer(
                self._automation.auto_sync_enabled,
                self._automation.auto_sync_minutes,
            )
            lib_page = self.shell.get_page("library")
            if lib_page is not None:
                lib_page.sync_tab.apply_saved_schedule(
                    enabled=self._automation.auto_sync_enabled,
                    minutes=self._automation.auto_sync_minutes,
                    mode=self._automation.auto_sync_mode,
                    targets=self._automation.auto_sync_targets or None,
                )
            self.set_theme(cfg.gui.theme or "system")
            self.m4b_var.set(self.output_format_var.get() == "m4b")
            self._auth.load_service_into_form()
            self._auth.update_field_states()
            self.status_var.set("Config reloaded")
            self._append_log("Reloaded configuration.")
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc), parent=self.root)

    def _show_about_dialog(self) -> AboutDialog:
        if self._about_dialog is not None:
            try:
                if self._about_dialog.winfo_exists():
                    self._about_dialog.lift()
                    self._about_dialog.focus_set()
                    return self._about_dialog
            except tk.TclError:
                pass
        self._about_dialog = AboutDialog(
            self.root,
            services_count=len(self._services),
            palette=self._palette,
        )
        return self._about_dialog

    def _archive_ledger_path(self) -> str:
        return resolve_archive_path(self._controller.config_snapshot())

    def _upload_archive_entry_to_abs(self, entry: Any) -> None:
        if not entry.output_path:
            return
        self.status_var.set(f"Uploading '{entry.title}' to Audiobookshelf...")
        self._append_log(f"Uploading to Audiobookshelf: {entry.title}")
        authors = [a.strip() for a in (entry.author or "").split(";") if a.strip()]
        self._controller.start_abs_upload(
            entry.output_path,
            title=entry.title,
            authors=authors,
            series=entry.series,
        )
        self._schedule_poll_soon()

    def _apply_window_icon(self) -> None:
        try:
            icon_ref = importlib.resources.files("audiobookdl").joinpath("assets/icon.ico")
            with importlib.resources.as_file(icon_ref) as icon_path:
                if icon_path.is_file():
                    self.root.iconbitmap(default=str(icon_path))  # type: ignore[no-untyped-call]
        except Exception:
            pass

    def _schedule_update_check(self) -> None:
        """Run a quiet background update check at launch unless opted out."""
        if not self.check_updates_var.get() or self._controller.config.check_updates is False:
            return
        self._controller.start_update_check(interactive=False, respect_opt_out=True)

    def check_updates_interactive(self) -> None:
        """Check for newer releases."""
        self.status_var.set("Checking for updates...")
        self._controller.start_update_check(interactive=True, respect_opt_out=False)

    def _on_m4b_toggled(self) -> None:
        if self.m4b_var.get():
            self.output_format_var.set("m4b")
            self.combine_var.set(True)
        elif self.output_format_var.get() == "m4b":
            self.output_format_var.set("Original")

    def _on_format_selected(self, _event: tk.Event | None = None) -> None:
        is_m4b = self.output_format_var.get() == "m4b"
        self.m4b_var.set(is_m4b)
        if is_m4b:
            self.combine_var.set(True)

    def _on_preset_selected(self, _event: tk.Event | None = None) -> None:
        chosen = self.template_preset_var.get()
        tpl = PRESET_LABEL_TO_TEMPLATE.get(chosen)
        if tpl:
            self.template_var.set(tpl)

    def _sync_preset_from_template(self) -> None:
        tpl = self.template_var.get().strip()
        target_label = TEMPLATE_TO_PRESET_LABEL.get(tpl, "Custom")
        if self.template_preset_var.get() != target_label:
            self.template_preset_var.set(target_label)

    def _update_template_preview(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        tpl = self.template_var.get().strip() or "{title}"
        full_tpl = os.path.join(folder, tpl)
        fmt = self.output_format_var.get().strip()
        ext = "m4b" if fmt in ("", "Original") else fmt
        remove_chars = self.remove_chars_var.get()
        try:
            rendered_series = output.gen_output_location(
                full_tpl, SAMPLE_PREVIEW_METADATA, remove_chars
            )
            rendered_standalone = output.gen_output_location(
                full_tpl, SAMPLE_STANDALONE_METADATA, remove_chars
            )
            if self.cover_only_var.get():
                suffix = ".jpg"
            elif self.split_chapters_var.get() or not self.combine_var.get():
                suffix = f"/01.{ext}"
            else:
                suffix = f".{ext}"
            self.template_preview_var.set(f"{rendered_series}{suffix}")
            self.template_standalone_preview_var.set(f"{rendered_standalone}{suffix}")
        except Exception as exc:
            self.template_preview_var.set(f"(invalid template: {exc})")
            self.template_standalone_preview_var.set("")

    def _browse_url_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as exc:
            messagebox.showerror("Error", f"Could not read file:\n{exc}", parent=self.root)
            return
        if content:
            self.navigate("download")
            existing = self.urls_text.get("1.0", tk.END).strip()
            combined = f"{existing}\n{content}" if existing else content
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()

    def _clear_urls(self) -> None:
        self.urls_text.delete("1.0", tk.END)
        self._on_urls_changed()

    def _paste_urls_from_clipboard(self) -> list[str]:
        """Insert URLs from the system clipboard into the URL box or open Pack for local paths."""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            self.status_var.set("Clipboard is empty")
            return []
        lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
        local_paths, _url_candidates = partition_input_tokens(lines)
        pasted = [
            u
            for u in parse_url_text(raw)
            if not (
                os.path.isdir(u)
                or (
                    os.path.isfile(u)
                    and os.path.splitext(u)[1].lstrip(".").lower() in SUPPORTED_PACK_EXTENSIONS
                )
            )
        ]
        if local_paths:
            self.open_pack_with_paths(local_paths)
        if not pasted:
            if not local_paths:
                self.status_var.set("No URLs found in clipboard")
            return []
        added = self._append_urls_to_input(pasted)
        if added:
            self.status_var.set(f"Pasted {len(added)} URL{'s' if len(added) != 1 else ''}")
        else:
            self.status_var.set("Clipboard URL(s) already listed")
        return added

    def _enqueue_imported_queue_records(self, items: Sequence[dict[str, Any]]) -> None:
        base_opts = self._try_build_options(urls_override=[])
        if base_opts is None:
            return
        dl_specs: list[tuple[str, args.DownloadOptions, list[int] | None, None]] = []
        pack_specs: list[tuple[PackJobRequest, args.DownloadOptions]] = []
        for rec in items:
            job_kind = str(rec.get("job_kind") or "download")
            if job_kind == "pack" and isinstance(rec.get("pack_request"), dict):
                req = PackJobRequest.from_dict(rec["pack_request"])
                if req is not None:
                    pack_specs.append((req, self._build_retry_pack_options(base_opts, req)))
            else:
                url = str(rec.get("url") or "").strip()
                if url:
                    dl_specs.append(self._build_retry_download_spec(base_opts, rec))
        if not dl_specs and not pack_specs:
            self.status_var.set("No valid jobs found in imported queue")
            return
        if not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs,
            pack_requests=pack_specs,
        )
        self._register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Imported {len(queued_jobs)} queued job(s) ---",
            append_status=f"Imported +{len(queued_jobs)} job(s) (total {new_total})",
            append_log_msg=f"--- Imported {len(queued_jobs)} job(s) into queue (total {new_total}) ---",
        )

    def _paste_and_queue(self) -> None:
        pasted = self._paste_urls_from_clipboard()
        if pasted or parse_url_text(self.urls_text.get("1.0", tk.END)):
            self.start_download()

    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Select output folder",
            initialdir=self.output_dir_var.get() or default_output_dir(),
        )
        if chosen:
            self.output_dir_var.set(chosen)

    def _open_path_in_os(self, target_path: str, reveal_in_folder: bool = False) -> None:
        """Open `target_path` or reveal it in the system file manager."""
        cleaned = (target_path or "").strip()
        if not cleaned or not os.path.exists(cleaned):
            messagebox.showinfo(
                "Path Missing",
                f"Path does not exist:\n{cleaned}",
                parent=self.root,
            )
            return
        try:
            if reveal_in_folder and os.path.isfile(cleaned):
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", "/select,", os.path.abspath(cleaned)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", cleaned])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(os.path.abspath(cleaned)) or "."])
                return
            open_target = (
                os.path.dirname(os.path.abspath(cleaned)) or "."
                if reveal_in_folder and not os.path.isdir(cleaned)
                else cleaned
            )
            if sys.platform.startswith("win") and hasattr(os, "startfile"):
                os.startfile(open_target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", open_target])
            else:
                subprocess.Popen(["xdg-open", open_target])
        except OSError as exc:
            messagebox.showerror(
                "Open Path Failed",
                f"Could not open:\n{cleaned}\n\n{exc}",
                parent=self.root,
            )

    def _open_output_folder(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        if not os.path.isdir(folder):
            messagebox.showinfo(
                "Folder Missing", f"Folder does not exist yet:\n{folder}", parent=self.root
            )
            return
        self._open_path_in_os(folder, reveal_in_folder=False)

    def _send_urls_to_input(self, urls: Sequence[str]) -> None:
        added = self._append_urls_to_input(urls)
        if added:
            self.status_var.set(
                f"Added {len(added)} URL{'s' if len(added) != 1 else ''} to input"
            )
        else:
            self.status_var.set("URL(s) already in input")

    def _apply_download_page_running_state(self, dl_page: DownloadPage, running: bool) -> None:
        for widget in dl_page.output_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state="disabled" if running else "normal")
        dl_page.options_panel.set_enabled(not running)

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        dl_page = self.shell.get_page("download")
        if dl_page is not None:
            self._apply_download_page_running_state(dl_page, running)
            dl_page.queue_panel.set_running_state(
                running,
                is_previewing=self._is_previewing,
                is_syncing=self._is_syncing,
            )
        self._auth.set_running_state(running)
        self.shell.set_footer_running_state(
            running=running,
            paused=self._controller.is_paused(),
            previewing=self._is_previewing,
            syncing=self._is_syncing,
        )
        if running:
            self._schedule_poll_soon()

    def _set_syncing_state(self, syncing: bool) -> None:
        self._is_syncing = syncing
        dl_page = self.shell.get_page("download")
        if dl_page is not None:
            dl_page.queue_panel.set_running_state(
                self._is_running,
                is_previewing=self._is_previewing,
                is_syncing=syncing,
            )
        self.shell.set_footer_running_state(
            running=self._is_running,
            paused=self._controller.is_paused(),
            previewing=self._is_previewing,
            syncing=syncing,
        )
        if syncing:
            self._schedule_poll_soon()

    def _try_build_options(
        self,
        urls_override: Sequence[str] | None = None,
        parent: tk.Misc | None = None,
    ) -> args.DownloadOptions | None:
        """Build `DownloadOptions`, displaying a validation dialog on invalid numeric/text input."""
        try:
            return self.build_options(urls_override=urls_override)
        except ValueError as exc:
            self.status_var.set("Invalid option value")
            messagebox.showerror(
                "Invalid Option",
                str(exc),
                parent=parent or self.root,
            )
            return None

    def build_options(self, urls_override: Sequence[str] | None = None) -> args.DownloadOptions:
        """Build `DownloadOptions` from current UI field values."""
        output_dir = self.output_dir_var.get().strip() or default_output_dir()
        template = self.template_var.get().strip() or "{title}"
        output_template = os.path.join(output_dir, template)
        option_values = {
            spec.name: spec.gui.read_option(getattr(self, spec.gui.var_attr).get())
            for spec in GUI_OPTION_SPECS
            if spec.gui is not None
        }
        use_download_archive = bool(option_values.pop("download_archive", False))
        download_archive = self._archive_ledger_path() if use_download_archive else None

        if urls_override is not None:
            urls = list(urls_override)
        else:
            dl_page = self.shell.get_page("download")
            urls = parse_url_text(dl_page.urls_text.get("1.0", tk.END)) if dl_page is not None else []
        auth_kwargs = self._auth.extract_options_auth(urls)
        config_loc = self._controller.config_location
        effective_config_location = (
            config_loc if (config_loc and os.path.exists(config_loc)) else None
        )

        return args.build_default_options(
            urls=urls,
            output_template=output_template,
            download_archive=download_archive,
            config_location=effective_config_location,
            **auth_kwargs,
            **option_values,
        )

    def _read_persisted_gui_preferences(self) -> dict[str, Any]:
        """Read valid persisted GUI toggle/option values without failing on transient invalid input."""
        prefs: dict[str, Any] = {}
        for spec in PERSISTED_GUI_OPTION_SPECS:
            binding = spec.gui
            if binding is None:
                continue
            raw_val = getattr(self, binding.var_attr).get()
            try:
                prefs[spec.name] = binding.read_option(raw_val)
            except ValueError:
                continue
        return prefs

    def _save_run_preferences(self, options: args.DownloadOptions) -> None:
        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)
        self._controller.save_run_preferences(
            options,
            output_dir=self.output_dir_var.get().strip() or default_output_dir(),
            output_template=self.template_var.get().strip() or "{title}",
            use_download_archive=self.download_archive_var.get(),
        )

    def _start_new_run(
        self,
        job_specs: Sequence[
            tuple[str, args.DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ],
    ) -> None:
        """Initialize UI and queue state for a fresh download run."""
        run_id, queued_jobs = self._controller.start_new_run(job_specs)
        self._set_running_state(True)
        self._queue_panel.prepare_run_ui_state(
            run_id,
            len(job_specs),
            f"--- Starting download ({len(job_specs)} URL{'s' if len(job_specs) != 1 else ''}) ---",
        )
        for job in queued_jobs:
            self._queue_panel.register_queue_job(job, persist=False)
        if queued_jobs:
            self._controller.save_queue_state()

    def _enqueue_additional_urls(
        self,
        urls: list[str],
        *,
        allow_existing: bool = False,
        profile_override: str | None = None,
    ) -> bool:
        """Append new URLs (or retried URLs when `allow_existing=True`) to the active queue."""
        if not self._auth.validate_cookie_file():
            return True
        if self._try_build_options(urls_override=[]) is None:
            return True

        def _build_for_url(u: str) -> args.DownloadOptions:
            opts = self.build_options(urls_override=[u])
            if profile_override:
                opts.profile = profile_override
            return opts

        has_active_run, added_jobs, new_total = self._controller.enqueue_additional_urls(
            urls,
            _build_for_url,
            allow_existing=allow_existing,
        )
        if not has_active_run:
            return False
        if not added_jobs:
            self.status_var.set("URL(s) already in active queue")
            self._queue_panel.append_log("All entered URL(s) are already in the active queue.")
            return True
        for job in added_jobs:
            self._queue_panel.register_queue_job(job, persist=False)
        self._controller.save_queue_state()
        self._queue_panel.ui_item_total = new_total
        self._queue_panel.refresh_batch_label()
        self.status_var.set(f"Queued +{len(added_jobs)} (total {new_total})")
        self._queue_panel.append_log(
            f"--- Added {len(added_jobs)} URL{'s' if len(added_jobs) != 1 else ''} to queue (total {new_total}) ---"
        )
        return True

    def _register_enqueued_batch(
        self,
        *,
        was_appended: bool,
        run_id: int,
        queued_jobs: Sequence[Any],
        new_total: int,
        start_header: str,
        append_status: str,
        append_log_msg: str,
    ) -> None:
        if was_appended:
            for job in queued_jobs:
                self._queue_panel.register_queue_job(job, persist=False)
            if queued_jobs:
                self._controller.save_queue_state()
            self._queue_panel.ui_item_total = new_total
            self._queue_panel.refresh_batch_label()
            self.status_var.set(append_status)
            self._queue_panel.append_log(append_log_msg)
            return

        self._set_running_state(True)
        self._queue_panel.prepare_run_ui_state(
            run_id,
            len(queued_jobs),
            start_header,
        )
        for job in queued_jobs:
            self._queue_panel.register_queue_job(job, persist=False)
        if queued_jobs:
            self._controller.save_queue_state()

    def _build_retry_download_spec(
        self,
        base_opts: args.DownloadOptions,
        rec: dict[str, Any],
    ) -> tuple[str, args.DownloadOptions, list[int] | None, None]:
        url = str(rec.get("url") or "").strip()
        opts = self.build_options(urls_override=[url])
        profile = str(rec.get("profile") or "").strip()
        if profile:
            opts.profile = profile
        job_overrides = rec.get("job_overrides")
        if isinstance(job_overrides, dict):
            explicit = set(getattr(opts, "_explicit_fields", set()))
            for attr in ("output_format", "combine", "output_template", "split_chapters", "media_type"):
                if attr in job_overrides:
                    setattr(opts, attr, job_overrides[attr])
                    explicit.add(attr)
            opts._explicit_fields = frozenset(explicit)
        raw_indices = rec.get("selected_indices")
        selected_indices = (
            [int(i) for i in raw_indices if isinstance(i, int) and not isinstance(i, bool)]
            if isinstance(raw_indices, list)
            else None
        )
        return (url, opts, selected_indices, None)

    @staticmethod
    def _build_retry_pack_options(
        base_opts: args.DownloadOptions,
        req: PackJobRequest,
    ) -> args.DownloadOptions:
        fmt = req.output_format or base_opts.output_format or "m4b"
        opts = dataclasses.replace(base_opts, output_format=fmt)
        opts._explicit_fields = frozenset(base_opts._explicit_fields | {"output_format"})
        return opts

    def _retry_failed_queue_items(self, job_ids: Sequence[str] | None = None) -> None:
        target_ids = list(job_ids) if job_ids is not None else list(self.queue_tree.get_children())
        retry_records = self._controller.retryable_jobs(target_ids)
        if not retry_records:
            self.status_var.set("No failed or cancelled jobs to retry")
            return

        download_recs: list[dict[str, Any]] = []
        seen_download_keys: set[tuple[str, str, tuple[int, ...] | None]] = set()
        pack_requests: list[PackJobRequest] = []
        for rec in retry_records:
            if rec.get("job_kind", "download") == "pack":
                parsed = PackJobRequest.from_dict(rec.get("pack_request"))
                if parsed is not None:
                    pack_requests.append(parsed)
            else:
                url = str(rec.get("url") or "").strip()
                if not url:
                    continue
                prof = str(rec.get("profile") or "").strip()
                raw_idx = rec.get("selected_indices")
                idx_key = (
                    tuple(int(i) for i in raw_idx if isinstance(i, int))
                    if isinstance(raw_idx, list)
                    else None
                )
                dedup_key = (url, prof, idx_key)
                if dedup_key not in seen_download_keys:
                    seen_download_keys.add(dedup_key)
                    download_recs.append(rec)

        if not download_recs and not pack_requests:
            self.status_var.set("No failed or cancelled jobs to retry")
            return

        if self._is_running:
            if self._controller.cancel_event.is_set():
                return
            base_opts = self._try_build_options(urls_override=[])
            if base_opts is None:
                return
            dl_specs = [self._build_retry_download_spec(base_opts, rec) for rec in download_recs]
            pack_pairs = [
                (req, self._build_retry_pack_options(base_opts, req)) for req in pack_requests
            ]
            was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
                download_specs=dl_specs,
                pack_requests=pack_pairs,
            )
            self._register_enqueued_batch(
                was_appended=was_appended,
                run_id=run_id,
                queued_jobs=queued_jobs,
                new_total=new_total,
                start_header=f"--- Retrying {len(queued_jobs)} job(s) ---",
                append_status=f"Queued +{len(queued_jobs)} retry job(s) (total {new_total})",
                append_log_msg=f"--- Added {len(queued_jobs)} retry job(s) to queue (total {new_total}) ---",
            )
            return

        base_opts = self._try_build_options(urls_override=[])
        if base_opts is None:
            return
        if download_recs and not pack_requests:
            if not self._ffmpeg_assistant.ensure_available(base_opts):
                return
            self._save_run_preferences(base_opts)
            dl_specs = [self._build_retry_download_spec(base_opts, rec) for rec in download_recs]
            self._start_new_run(dl_specs)
            return

        pack_pairs = []
        for req in pack_requests:
            fmt = req.output_format or base_opts.output_format or "m4b"
            opts = dataclasses.replace(base_opts, output_format=fmt)
            opts._explicit_fields = frozenset(base_opts._explicit_fields | {"output_format"})
            if not self._ffmpeg_assistant.ensure_available(opts):
                return
            pack_pairs.append((req, opts))
        if download_recs and not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        self._save_run_preferences(base_opts)
        dl_specs = [self._build_retry_download_spec(base_opts, rec) for rec in download_recs]
        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs,
            pack_requests=pack_pairs,
        )
        self._register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Retrying {len(queued_jobs)} job(s) ---",
            append_status=f"Queued +{len(queued_jobs)} retry job(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} retry job(s) to queue (total {new_total}) ---",
        )

    def _execute_sync_targets(self, targets: Sequence[tuple[str, str]], mode: str) -> None:
        if mode == "enqueue":
            self._sync_and_enqueue_targets(targets)
        elif mode == "dry_run":
            self._sync_libraries(
                services=[s for s, _p in targets],
                scan_only=True,
                dry_run=True,
            )
        else:
            self._sync_libraries(
                services=[s for s, _p in targets],
                scan_only=True,
            )

    def _save_sync_schedule(
        self,
        enabled: bool,
        minutes: int,
        mode: str,
        targets: list[tuple[str, str]],
    ) -> None:
        norm_minutes = max(1, int(minutes))
        norm_mode = mode if mode in ("enqueue", "scan_only", "dry_run") else "enqueue"
        self._automation.auto_sync_mode = norm_mode
        self._automation.auto_sync_targets = list(targets)
        self._automation.configure_auto_sync_timer(enabled, norm_minutes)
        serialized_targets = [make_source_profile_key(s, p) for s, p in targets]
        try:
            self._controller.save_gui_preferences(
                auto_sync_enabled=bool(enabled),
                auto_sync_minutes=norm_minutes,
                auto_sync_mode=norm_mode,
                auto_sync_targets=serialized_targets,
            )
        except OSError as exc:
            self.status_var.set(f"Failed to save auto-sync schedule: {exc}")
            return
        if enabled:
            self.status_var.set(f"Saved auto-sync schedule (every {norm_minutes} min)")
        else:
            self.status_var.set("Saved auto-sync schedule (disabled)")

    def _sync_and_enqueue_targets(self, targets: Sequence[tuple[str, str]]) -> None:
        if self._is_running:
            self.status_var.set("Wait for active downloads before syncing libraries")
            return
        if self._is_syncing:
            self.status_var.set("Library sync already in progress")
            return
        options = self._try_build_options(urls_override=[])
        if options is None:
            return
        self._set_syncing_state(True)
        self.status_var.set("Scanning libraries for pending books...")
        self._append_log("--- Scanning libraries for pending books ---")
        sync_opts = args.SyncOptions(
            services=[s for s, _p in targets],
            scan_only=True,
            output_template=options.output_template,
            output_format=options.output_format,
            archive_path=self._archive_ledger_path(),
            config_location=self._controller.config_location,
        )
        self._controller.start_sync_enqueue_discovery(targets, sync_opts)

    def _sync_libraries(
        self,
        *,
        services: Sequence[str] = (),
        scan_only: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Run one library synchronization cycle in the background."""
        if self._is_running:
            self.status_var.set("Wait for active downloads before syncing libraries")
            return
        if self._is_syncing:
            self.status_var.set("Library sync already in progress")
            return
        options = self._try_build_options(urls_override=[])
        if options is None:
            return
        self._set_syncing_state(True)
        self.status_var.set("Syncing libraries...")
        self._append_log("--- Starting library sync ---")
        sync_opts = args.SyncOptions(
            services=list(services),
            scan_only=scan_only,
            dry_run=dry_run,
            output_template=options.output_template,
            output_format=options.output_format,
            archive_path=self._archive_ledger_path(),
            config_location=self._controller.config_location,
        )
        self._controller.start_library_sync(sync_opts)

    def _handle_sync_done(self, cycle: Any, exc: Exception | None) -> None:
        self._set_syncing_state(False)
        if exc is not None:
            self.status_var.set("Library sync failed")
            self._append_log(f"[ERROR] Library sync failed: {exc}")
            messagebox.showerror("Library Sync Failed", str(exc), parent=self.root)
            return
        if cycle is None:
            self.status_var.set("Library sync finished")
            return
        if getattr(cycle, "cancelled", False):
            self.status_var.set("Library sync cancelled")
            self._append_log("--- Library sync cancelled ---")
            return
        msg = (
            f"Discovered {cycle.books_discovered}, new {cycle.newly_added}, "
            f"downloaded {cycle.downloaded}, skipped {cycle.skipped}, failed {cycle.failed}"
        )
        self.status_var.set(f"Sync complete: {msg}")
        self._append_log(f"--- Library sync finished: {msg} ---")
        for err in getattr(cycle, "errors", ()):
            self._append_log(f"[WARN] {err}")
        lib_page = self.shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()
        if cycle.failed > 0 or getattr(cycle, "errors", ()):
            messagebox.showwarning("Library Sync Finished with Warnings", msg, parent=self.root)
        else:
            messagebox.showinfo("Library Sync Complete", msg, parent=self.root)

    def _on_pack_submitted(self, requests_list: list[PackJobRequest], output_format: str) -> None:
        """Queue one or more local repackaging requests onto `GuiController`."""
        if not requests_list:
            return
        base_opts = self._try_build_options(urls_override=[])
        if base_opts is None:
            return
        self._save_run_preferences(base_opts)
        pairs: list[tuple[PackJobRequest, args.DownloadOptions]] = []
        for req in requests_list:
            fmt = output_format or req.output_format or "m4b"
            opts = dataclasses.replace(base_opts, output_format=fmt)
            opts._explicit_fields = frozenset(base_opts._explicit_fields | {"output_format"})
            pairs.append((req, opts))

        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_pack_jobs(
            pairs
        )
        self._register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Starting local pack ({len(queued_jobs)} book{'s' if len(queued_jobs) != 1 else ''}) ---",
            append_status=f"Queued +{len(queued_jobs)} pack job(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} local pack job(s) to queue ---",
        )

    def _start_or_enqueue_urls(
        self,
        urls: list[str],
        *,
        profile_override: str | None = None,
    ) -> None:
        if self._is_previewing or not urls:
            return
        if self._is_running:
            if self._controller.cancel_event.is_set():
                return
            if self._enqueue_additional_urls(urls, profile_override=profile_override):
                return

        if not self._auth.validate_cookie_file():
            return
        if not self._auth.warn_mixed_service_credentials(urls):
            return

        options = self._try_build_options(urls_override=urls)
        if options is None:
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            return
        self._save_run_preferences(options)

        unique_urls = list(dict.fromkeys(urls))
        job_specs: list[
            tuple[str, args.DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ] = []
        for u in unique_urls:
            job_opts = self.build_options(urls_override=[u])
            if profile_override:
                job_opts.profile = profile_override
            job_specs.append((u, job_opts, None, None))
        self._start_new_run(job_specs)

    def start_download(self) -> None:
        if self._is_previewing:
            return
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if self._is_running:
            if self._controller.cancel_event.is_set():
                return
            if not urls:
                self.status_var.set("Enter at least one URL to queue")
                self.urls_text.focus_set()
                return
            if self._enqueue_additional_urls(urls):
                return

        if not urls:
            self.status_var.set("Enter at least one URL")
            messagebox.showwarning(
                "Missing URL", "Please enter at least one audiobook URL.", parent=self.root
            )
            self.urls_text.focus_set()
            return

        self._start_or_enqueue_urls(urls)

    def _set_previewing_state(self, previewing: bool) -> None:
        self._is_previewing = previewing
        dl_page = self.shell.get_page("download")
        if dl_page is not None:
            dl_page.queue_panel.set_previewing_state(previewing, is_running=self._is_running)
        self.shell.set_footer_running_state(
            running=self._is_running,
            paused=self._controller.is_paused(),
            previewing=previewing,
            syncing=self._is_syncing,
        )
        if previewing:
            self._schedule_poll_soon()

    def preview_metadata(self, url_override: str | None = None) -> None:
        """Fetch book or series metadata for `url_override` or the first entered URL."""
        if self._is_running or self._is_previewing:
            return
        urls = (
            [url_override.strip()]
            if (url_override and url_override.strip())
            else parse_url_text(self.urls_text.get("1.0", tk.END))
        )
        if not urls:
            self.status_var.set("Enter a URL to preview")
            messagebox.showwarning(
                "Missing URL",
                "Please enter an audiobook or series URL to preview.",
                parent=self.root,
            )
            self.urls_text.focus_set()
            return
        if not self._auth.validate_cookie_file():
            return

        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)

        target_url = urls[0]
        if len(urls) > 1:
            self._queue_panel.append_log(f"Previewing first URL: {target_url}")

        options = self._try_build_options(urls_override=[target_url])
        if options is None:
            return
        self._set_previewing_state(True)
        self._queue_panel._set_progress(0.0, indeterminate=True)
        self.status_var.set("Fetching preview...")
        self.active_job_var.set("Fetching preview...")
        self._queue_panel.append_log(f"--- Fetching preview: {target_url} ---")
        self._controller.start_preview(target_url, options)

    def _handle_preview_done(
        self,
        url: str,
        prepared: tuple[Source, Result] | None,
        exc: Exception | None,
    ) -> None:
        self._set_previewing_state(False)
        self._controller.preview_interaction = None
        self._queue_panel._set_progress(0.0)
        if not self._is_running:
            self.active_job_var.set("Idle")

        if exc is not None:
            if isinstance(exc, DownloadCancelled):
                self.status_var.set("Preview cancelled")
                return
            self.status_var.set("Preview failed")
            self._queue_panel.append_log(f"[ERROR] {exc}")
            messagebox.showerror("Preview Failed", str(exc), parent=self.root)
            return

        if prepared is None:
            self.status_var.set("Preview failed")
            return

        source, result = prepared
        try:
            root_exists = self.root.winfo_exists()
        except tk.TclError:
            root_exists = False
        if not root_exists:
            self._close_prefetched_source(prepared)
            return
        self.status_var.set(f"Preview ready: {result.title[:60]}")
        self._show_preview_dialog(url, source, result)

    @staticmethod
    def _close_prefetched_source(prefetched: tuple[Source, Result] | None) -> None:
        if prefetched is None:
            return
        close_fn = getattr(prefetched[0], "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def _show_preview_dialog(self, url: str, source: Source, result: Result) -> PreviewDialog:
        """Display resolved `Audiobook` metadata/cover or a selectable `Series` checklist."""
        if self._preview_dialog is not None and self._preview_dialog.winfo_exists():
            self._preview_dialog.destroy()

        def _search_enrichment(book: Audiobook):
            opts = self.build_options(urls_override=[url])
            return self._controller.search_enrichment_for_audiobook(book, opts)

        def _apply_enrichment(book: Audiobook, candidate):
            opts = self.build_options(urls_override=[url])
            return self._controller.apply_enrichment_candidate(book, candidate, opts)

        cached_series_opts = self.build_options() if isinstance(result, Series) else None
        dir_stems_cache: dict[str, set[str]] = {}
        archive_titles_cache: set[str] | None = None
        if isinstance(result, Series) and cached_series_opts and cached_series_opts.download_archive:
            if os.path.exists(cached_series_opts.download_archive):
                with DownloadArchive(cached_series_opts.download_archive, persistent=True) as archive:
                    archive_titles_cache = {
                        e.title.strip().casefold()
                        for e in archive.list_entries(limit=5000, include_hidden=True)
                        if e.status == "downloaded" and e.title and e.title.strip()
                    }
        series_title = result.title if isinstance(result, Series) else None
        dialog = PreviewDialog(
            self.root,
            url,
            source,
            result,
            is_running=self._is_running,
            skip_downloaded=self.skip_downloaded_var.get(),
            series_book_exists=lambda book: (
                series_book_exists(
                    book,
                    options=cached_series_opts,
                    dir_stems_cache=dir_stems_cache,
                    archive_titles_cache=archive_titles_cache,
                    series_title=series_title,
                )
                if cached_series_opts is not None
                else False
            ),
            on_confirm=self._enqueue_from_preview,
            palette=self._palette,
            on_search_enrichment=_search_enrichment,
            on_apply_enrichment=_apply_enrichment,
        )
        self._preview_dialog = dialog
        return dialog

    def _enqueue_from_preview(
        self,
        url: str,
        prefetched: tuple[Source, Result],
        selected_indices: list[int] | None,
    ) -> None:
        """Start or queue a download from an inspected preview result."""
        options = self._try_build_options(urls_override=[url])
        if options is None:
            self._close_prefetched_source(prefetched)
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            self._close_prefetched_source(prefetched)
            return
        if self._is_running:
            enqueued = self._controller.enqueue_from_preview(
                url, options, prefetched, selected_indices
            )
            if enqueued is not None:
                added_job, new_total = enqueued
                self._queue_panel.register_queue_job(added_job)
                self._queue_panel.ui_item_total = new_total
                self._queue_panel.refresh_batch_label()
                self.status_var.set(f"Queued preview item (total {new_total})")
                return

        self._save_run_preferences(options)
        self._start_new_run(
            [(url, self.build_options(urls_override=[url]), selected_indices, prefetched)]
        )

    def cancel_download(self) -> None:
        if self._is_previewing and not self._is_running:
            self.status_var.set("Cancelling preview...")
            self.cancel_btn.configure(state="disabled")
            self.shell.footer_cancel_btn.configure(state="disabled")
            self._queue_panel.append_log("Cancelling metadata preview...")
            self._controller.cancel_preview()
            return
        if self._is_syncing and not self._is_running:
            self.status_var.set("Cancelling library sync...")
            self.cancel_btn.configure(state="disabled")
            self.shell.footer_cancel_btn.configure(state="disabled")
            self._queue_panel.append_log("Cancelling library sync...")
            self._controller.cancel_library_sync()
            return
        if not self._is_running:
            return
        self.status_var.set("Cancelling...")
        self.cancel_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self.shell.footer_cancel_btn.configure(state="disabled")
        self._controller.cancel_active_run()

    def _schedule_poll_soon(self, delay_ms: int = 10) -> None:
        self._cancel_poll_timer()
        try:
            if self.root.winfo_exists():
                self._poll_after_id = self.root.after(delay_ms, self._poll_queue)
        except tk.TclError:
            self._poll_after_id = None

    def _poll_queue(self) -> None:
        self._poll_after_id = None
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            self._poll_after_id = self.root.after(50, self._poll_queue)
        except tk.TclError:
            self._poll_after_id = None
            return
        processed = self._controller.drain_events(
            self._handle_event,
            max_batch=self._controller.MAX_QUEUE_BATCH,
        )
        while not self._modal_prompt_active and self._deferred_modal_events:
            self._handle_event(self._deferred_modal_events.popleft())
        if self._is_running:
            dl_page = self.shell.get_page("download")
            if dl_page is not None:
                dl_page.queue_panel.update_metrics_display(is_running=True)
        if processed >= self._controller.MAX_QUEUE_BATCH:
            self._schedule_poll_soon(10)
        elif (
            processed == 0
            and not self._is_running
            and not self._is_previewing
            and not self._is_syncing
            and not self._deferred_modal_events
        ):
            self._schedule_poll_soon(250)

    def _cancel_poll_timer(self) -> None:
        after_id = getattr(self, "_poll_after_id", None)
        self._poll_after_id = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _on_root_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self.root:
            self._cancel_poll_timer()
        self._automation.cancel_auto_sync()
        self._automation.cancel_clipboard()

    def _handle_event(self, item: ControllerEvent) -> None:
        if isinstance(
            item,
            (
                LogEvent,
                LogBatchEvent,
                BatchItemEvent,
                JobStatusEvent,
                JobMetaEvent,
                ItemProgressEvent,
                ProgressStartEvent,
                ProgressAdvanceEvent,
                ProgressMetricsEvent,
                ProgressFinishEvent,
            ),
        ):
            dl_page = self.shell.get_page("download")
            if dl_page is not None:
                if dl_page.queue_panel.handle_activity_event(item):
                    return
            elif self._handle_deferred_activity_event(item):
                return
        if isinstance(item, (PromptInputEvent, PromptConfirmEvent, PromptChoiceEvent)):
            if self._modal_prompt_active:
                self._deferred_modal_events.append(item)
                return
            self._modal_prompt_active = True
            try:
                if isinstance(item, PromptInputEvent):
                    item.holder["value"] = simpledialog.askstring(
                        "Authentication Required",
                        f"{item.prompt}:",
                        show="*" if item.hidden else None,
                        parent=self.root,
                    )
                    item.done.set()
                elif isinstance(item, PromptConfirmEvent):
                    item.holder["value"] = messagebox.askyesno(
                        "Confirm", item.message, parent=self.root
                    )
                    item.done.set()
                elif isinstance(item, PromptChoiceEvent):
                    dlg = ChoiceDialog(
                        self.root,
                        item.title,
                        item.choices,
                        default=item.default_index,
                        palette=self._palette,
                    )
                    self.root.wait_window(dlg)
                    item.holder["value"] = dlg.selected_index
                    item.done.set()
            finally:
                self._modal_prompt_active = False
        elif isinstance(item, PreviewDoneEvent):
            self._handle_preview_done(item.url, item.prepared, item.error)
        elif isinstance(item, AuthVerifiedEvent):
            self._auth.handle_auth_verified(item.service_label, item.ok, item.error)
        elif isinstance(item, CatalogLoadedEvent):
            if item.request_id != self._controller.catalog_request_id:
                return
            catalog_tab: CatalogTab = item.target_tab
            try:
                if not catalog_tab.winfo_exists():
                    return
            except tk.TclError:
                return
            if item.error is not None:
                err_text = str(item.error).strip() or type(item.error).__name__
                first_line = err_text.splitlines()[0].strip()
                catalog_tab.set_error_status(f"Error: {first_line}")
                self._append_log(f"[ERROR] Catalog fetch failed: {err_text}")
                messagebox.showerror("Catalog Error", err_text, parent=self.root)
            else:
                if isinstance(item.payload, tuple) and len(item.payload) == 2:
                    books, statuses = item.payload
                else:
                    books, statuses = item.payload, None
                catalog_tab.populate_items(books, statuses=statuses)
        elif isinstance(item, FfmpegInstallDoneEvent):
            self._ffmpeg_assistant.handle_install_done(item.ok, item.detail)
        elif isinstance(item, UpdateCheckedEvent):
            self._handle_update_checked(item.info, item.interactive)
        elif isinstance(item, SyncDoneEvent):
            self._handle_sync_done(item.cycle, item.error)
        elif isinstance(item, SyncEnqueueReadyEvent):
            self._handle_sync_enqueue_ready(item.pending_groups, item.stats, item.error)
        elif isinstance(item, AbsUploadDoneEvent):
            if item.ok:
                self.status_var.set(f"Uploaded '{item.title}' to Audiobookshelf")
                self._append_log(f"Uploaded '{item.title}' to Audiobookshelf")
            else:
                self.status_var.set("Audiobookshelf upload failed")
                self._append_log(f"[ERROR] Audiobookshelf upload failed: {item.error_message}")
                messagebox.showerror(
                    "Audiobookshelf Upload Failed",
                    str(item.error_message),
                    parent=self.root,
                )
        elif isinstance(item, RunDoneEvent):
            dl_page = self.shell.get_page("download")
            if dl_page is None or item.run_id == dl_page.queue_panel.current_ui_run_id:
                self._finish_run(item.summary, item.error)

    def _handle_sync_enqueue_ready(
        self,
        pending_groups: Sequence[tuple[str, str, list[str]]],
        stats: dict[str, Any],
        exc: Exception | None,
    ) -> None:
        self._set_syncing_state(False)
        if exc is not None:
            self.status_var.set("Library sync failed")
            self._append_log(f"[ERROR] Library sync failed: {exc}")
            messagebox.showerror("Library Sync Failed", str(exc), parent=self.root)
            return
        if stats.get("cancelled"):
            self.status_var.set("Library sync cancelled")
            self._append_log("--- Library sync cancelled ---")
            return
        for err in stats.get("errors") or ():
            self._append_log(f"[WARN] {err}")
        lib_page = self.shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()
        total_urls = sum(len(urls) for _svc, _prof, urls in pending_groups)
        if total_urls == 0:
            discovered = stats.get("discovered", 0)
            self.status_var.set(f"Sync complete: {discovered} discovered, 0 pending")
            self._append_log(
                f"--- Library sync finished: {discovered} discovered, 0 pending ---"
            )
            return
        base_opts = self._try_build_options(urls_override=[])
        if base_opts is None:
            return
        if not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        dl_specs: list[tuple[str, args.DownloadOptions, list[int] | None, None]] = []
        for _svc, prof, urls in pending_groups:
            for u in urls:
                opts = self.build_options(urls_override=[u])
                if prof:
                    opts.profile = prof
                dl_specs.append((u, opts, None, None))
        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs
        )
        self._register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Sync queued {len(queued_jobs)} pending book(s) ---",
            append_status=f"Queued +{len(queued_jobs)} synced book(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} synced book(s) to queue (total {new_total}) ---",
        )

    def _handle_update_checked(self, info: UpdateInfo | None, interactive: bool) -> None:
        if info is None:
            if interactive:
                self.status_var.set("Update check failed")
                messagebox.showwarning(
                    "Update Check",
                    "Could not reach update server to check for new releases.",
                    parent=self.root,
                )
            return
        if info.has_update:
            msg = (
                f"A new version of audiobook-dl is available: v{info.latest_version} "
                f"(current: v{info.current_version}).\n\n"
                f"Download: {info.release_url}"
            )
            self.status_var.set(f"Update available: v{info.latest_version}")
            self._append_log(
                f"Update available: v{info.latest_version} ({info.release_url})"
            )
            if interactive:
                messagebox.showinfo("Update Available", msg, parent=self.root)
        elif interactive:
            self.status_var.set(f"Up to date (v{info.current_version})")
            messagebox.showinfo(
                "Up to Date",
                f"You are running the latest version (v{info.current_version}).",
                parent=self.root,
            )

    def _finish_run(self, summary: DownloadSummary | None, exc: Exception | None) -> None:
        self._set_running_state(False)
        self._controller.clear_seen_urls()
        self._queue_panel.finish_run(
            summary,
            exc,
            notify_enabled=self.notify_on_complete_var.get(),
        )
        lib_page = self.shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()

    def _on_close(self, *, force: bool = False) -> None:
        if not force and (self._is_running or self._is_syncing):
            if not messagebox.askyesno(
                "Operation in Progress",
                "A download or library sync is currently active. Cancel and exit anyway?",
                parent=self.root,
            ):
                return
        try:
            maximized = self._is_window_maximized()
            if not maximized:
                current_geom = self.root.geometry()
                if is_valid_geometry(current_geom):
                    self._normal_geometry = current_geom
            saved_geom = self._normal_geometry if (self._normal_geometry and is_valid_geometry(self._normal_geometry)) else None
            dl_page = self.shell.get_page("download")
            split_ratio = dl_page.get_split_ratio() if dl_page is not None else self._controller.config.gui.download_split_ratio
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
            self._controller.save_window_preferences(
                window_geometry=saved_geom,
                window_maximized=maximized,
                download_split_ratio=split_ratio,
                active_page=self.shell.active_page_id,
                options_collapsed=options_collapsed,
                output_naming_collapsed=naming_collapsed,
                output_dir=self.output_dir_var.get().strip() or None,
                output_template=self.template_var.get().strip() or None,
                use_download_archive=self.download_archive_var.get(),
                theme=self._theme_mode,
                notify_on_complete=self.notify_on_complete_var.get(),
                clipboard_monitor=self.clipboard_monitor_var.get(),
                output_prefs=self._read_persisted_gui_preferences(),
            )
        except OSError:
            pass
        self._cancel_poll_timer()
        self._automation.cancel_auto_sync()
        self._automation.cancel_clipboard()
        self._controller.shutdown(
            is_running=self._is_running,
            is_previewing=self._is_previewing,
        )
        self.root.destroy()


def _enable_high_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows when supported by the OS."""
    enable_high_dpi_awareness()


def _is_gui_invocation(raw_argv: Sequence[str]) -> bool:
    """
    Return `True` when `raw_argv` only targets the desktop GUI launcher.

    Any URL, CLI subcommand (`pack`, `library`, `scan`, `export`, `sync`),
    or CLI option (such as `--version`, `--help`, `--search`, `-o`) routes
    through `audiobookdl.__main__.main()` instead.
    """
    if not raw_argv:
        return True
    idx = 0
    while idx < len(raw_argv):
        token = raw_argv[idx]
        if token in ("--self-test", "--gui"):
            idx += 1
            continue
        if token in ("-c", "--config"):
            if idx + 1 >= len(raw_argv):
                return True
            idx += 2
            continue
        if token.startswith("--config=") or (token.startswith("-c") and len(token) > 2):
            idx += 1
            continue
        return False
    return True


def _attach_parent_console_windows() -> None:
    """Attach a Windows GUI-subsystem binary to its parent console when invoked from CLI."""
    if os.name != "nt":
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):
            return
        if sys.stdout is None:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        if sys.stderr is None:
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdin is None:
            sys.stdin = open("CONIN$", encoding="utf-8", errors="replace")
    except Exception:
        pass


def parse_gui_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments supported by the desktop GUI launcher."""
    parser = argparse.ArgumentParser(prog="audiobook-dl-gui")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the desktop GUI window explicitly",
    )
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
    return parser.parse_args(list(argv) if argv is not None else None)


def run_self_test(check_gui: bool = True) -> None:
    """Validate bundled assets, dynamic packages, and GUI startup for frozen builds."""
    from Crypto.Cipher import AES
    import pycountry
    from audiobookdl.config import tomllib

    help_text = read_asset_file("assets/simple_help.txt")
    if not help_text.strip():
        raise RuntimeError("Missing assets/simple_help.txt")
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
    """Launch the audiobook-dl desktop GUI, or dispatch CLI invocations."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not _is_gui_invocation(raw_argv):
        _attach_parent_console_windows()
        from audiobookdl.__main__ import main as cli_main

        cli_main(raw_argv)
        return
    parsed = parse_gui_args(raw_argv)
    if parsed.self_test:
        run_self_test(check_gui=True)
        return
    _enable_high_dpi_awareness()
    set_windows_process_app_id()
    root = create_tk_root()
    app = AudiobookDLApp(root, config_location=parsed.config_location)
    app._schedule_update_check()
    root.mainloop()


if __name__ == "__main__":
    main()

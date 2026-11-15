"""Main Tkinter desktop window view and launcher entry point for audiobook-dl."""

import argparse
import importlib.resources
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from audiobookdl import __version__, args, output
from audiobookdl.config import SourceConfig, is_portable_mode
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.archive_dialog import ArchiveLedgerDialog
from audiobookdl.gui.catalog_dialog import CatalogBrowserDialog
from audiobookdl.gui.controller import GuiController, QueuedJob
from audiobookdl.gui.cookie_dialog import CookieImportDialog
from audiobookdl.gui.formatting import (
    MEDIA_TYPE_OPTIONS,
    OUTPUT_FORMATS,
    OVERWRITE_OPTIONS,
    PRESET_LABEL_TO_TEMPLATE,
    SAMPLE_PREVIEW_METADATA,
    TEMPLATE_PRESET_LABELS,
    TEMPLATE_TO_PRESET_LABEL,
    default_output_dir,
    format_duration,
    format_speed,
    is_valid_geometry,
    parse_url_text,
)
from audiobookdl.gui.notifications import format_completion_notification, send_os_notification_async
from audiobookdl.gui.preview_dialog import ChoiceDialog, PreviewDialog
from audiobookdl.gui.theme import (
    THEME_LABELS,
    THEME_MODES,
    ThemePalette,
    apply_theme,
    apply_theme_to_widget_tree,
    apply_theme_to_window,
    enable_high_dpi_awareness,
    normalize_theme_mode,
    scale_px,
)
from audiobookdl.gui.widgets import (
    ScrollableFrame,
    ScrolledTreeview,
    TreeColumnSpec,
    attach_url_context_menu,
    bind_text_tab_navigation,
)
from audiobookdl.options_schema import GUI_OPTION_SPECS, PERSISTED_GUI_OPTION_SPECS
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources import ServiceInfo, detect_service_key, get_available_services
from audiobookdl.sources.source import Source
from audiobookdl.utils import (
    AUDIO_BITRATE_PRESETS,
    find_executable,
    read_asset_file,
    resolve_ffmpeg_dir,
)
from audiobookdl.utils.archive import default_archive_path
from audiobookdl.utils.audiobook import Audiobook, BookId, Result
from audiobookdl.utils.updater import UpdateInfo, winget_available


class AudiobookDLApp:
    """Main Tkinter desktop window for audiobook-dl."""

    MAX_LOG_LINES = 5000

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
    write_cue_var: tk.BooleanVar
    resume_var: tk.BooleanVar
    audio_bitrate_var: tk.StringVar
    overwrite_var: tk.StringVar
    max_workers_var: tk.StringVar
    book_delay_var: tk.StringVar
    proxy_var: tk.StringVar
    timeout_var: tk.StringVar
    enrich_metadata_var: tk.BooleanVar
    enrich_providers_var: tk.StringVar
    enrich_interactive_var: tk.BooleanVar

    def __init__(self, root: tk.Tk, config_location: Optional[str] = None) -> None:
        self.root = root
        self._controller = GuiController(config_location)
        self.root.title(f"audiobook-dl {__version__}")

        saved_geometry = (self._controller.config.gui.window_geometry or "").strip()
        if saved_geometry and is_valid_geometry(saved_geometry):
            self.root.geometry(saved_geometry)
        else:
            min_w = scale_px(self.root, 660)
            min_h = scale_px(self.root, 480)
            screen_w = max(min_w, self.root.winfo_screenwidth())
            screen_h = max(min_h, self.root.winfo_screenheight())
            default_w = min(scale_px(self.root, 780), max(min_w, screen_w - 40))
            default_h = min(scale_px(self.root, 780), max(min_h, screen_h - 80))
            self.root.geometry(f"{default_w}x{default_h}")
        self.root.minsize(scale_px(self.root, 660), scale_px(self.root, 480))
        self._apply_window_icon()

        self._style = ttk.Style()
        self._theme_mode = normalize_theme_mode(self._controller.config.gui.theme)
        self._theme_var = tk.StringVar(value=self._theme_mode)
        self._palette: ThemePalette = apply_theme(self.root, self._style, self._theme_mode)

        self._services: List[ServiceInfo] = get_available_services()
        self._services_by_label = {s.label: s for s in self._services}
        self._services_by_key = {s.key: s for s in self._services}
        self._last_detected_service: Optional[str] = None

        self._current_ui_run_id = 0
        self._ui_item_index = 0
        self._ui_item_total = 0
        self._ui_book_index = 0
        self._ui_book_total = 0
        self._run_start_time: Optional[float] = None
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds: Optional[float] = None
        self._last_metrics_render_at = 0.0
        self._is_running = False
        self._is_previewing = False
        self._is_verifying_auth = False
        self._preview_dialog: Optional[PreviewDialog] = None
        self._cookie_dialog: Optional[CookieImportDialog] = None
        self._interactive_widgets: List[
            Union[ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox, ttk.Spinbox]
        ] = []

        self._init_variables()
        self._build_menu_bar()
        self._build_ui()
        apply_theme_to_widget_tree(self.root, self._palette)
        self._load_service_into_form()
        self._update_ffmpeg_status()
        self._set_running_state(False)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._poll_queue)

    @property
    def theme_mode(self) -> str:
        return self._theme_mode

    def set_theme(self, mode: str) -> ThemePalette:
        """Switch between `'system'`, `'light'`, and `'dark'` themes live."""
        self._theme_mode = normalize_theme_mode(mode)
        self._theme_var.set(self._theme_mode)
        self._palette = apply_theme(self.root, self._style, self._theme_mode)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                apply_theme_to_window(child, self._palette)
        self._update_ffmpeg_status()
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

        first_label = self._services[0].label if self._services else ""
        self.service_label_var = tk.StringVar(value=first_label)
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

        self.status_var = tk.StringVar(value="Ready")
        self.batch_var = tk.StringVar(value="")
        self.metrics_var = tk.StringVar(value="")
        self.ffmpeg_status_var = tk.StringVar(value="")

    def _build_ui(self) -> None:
        self._scroll_frame = ScrollableFrame(
            self.root,
            style=self._style,
            content_padding=(scale_px(self.root, 12), scale_px(self.root, 8)),
            inner_scrollables=lambda: (self.urls_text, self.log_text, self.queue_tree),
        )
        self._scroll_frame.pack(fill=tk.BOTH, expand=True)
        container = self._scroll_frame.content_frame
        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)

        # Header
        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        title_text = f"audiobook-dl  v{__version__}"
        if is_portable_mode():
            title_text += "  [Portable]"
        ttk.Label(
            header,
            text=title_text,
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ffmpeg_box = ttk.Frame(header)
        ffmpeg_box.grid(row=0, column=1, sticky="e")
        self._ffmpeg_label = ttk.Label(
            ffmpeg_box,
            textvariable=self.ffmpeg_status_var,
            takefocus=True,
        )
        self._ffmpeg_label.pack(side=tk.LEFT, padx=(0, 8))
        self._ffmpeg_label.bind("<Button-1>", lambda _e: self._show_ffmpeg_guidance())
        self._ffmpeg_label.bind("<Return>", lambda _e: self._show_ffmpeg_guidance())
        self._ffmpeg_label.bind("<space>", lambda _e: self._show_ffmpeg_guidance())

        self.ffmpeg_locate_btn = ttk.Button(
            ffmpeg_box,
            text="Locate FFmpeg...",
            command=self.browse_ffmpeg_dir,
        )
        self.ffmpeg_locate_btn.pack(side=tk.LEFT)

        # URLs
        url_frame = ttk.LabelFrame(container, text="Audiobook URL(s)", padding=(10, 6))
        url_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        url_frame.columnconfigure(0, weight=1)

        self.urls_text = tk.Text(url_frame, height=2, width=40, wrap="word")
        self.urls_text.grid(row=0, column=0, sticky="ew")
        bind_text_tab_navigation(self.urls_text)
        self.urls_text.bind("<Control-Return>", lambda _e: self.start_download())
        self.urls_text.bind("<KeyRelease>", self._on_urls_changed)
        self.urls_text.bind("<<Paste>>", lambda _e: self.root.after(10, self._on_urls_changed))
        self.urls_text.bind("<ButtonRelease-1>", lambda _e: self.root.after(10, self._on_urls_changed))
        self._url_menu = attach_url_context_menu(
            self.urls_text,
            on_paste=self._paste_urls_from_clipboard,
            on_paste_and_queue=self._paste_and_queue,
            on_clear=self._clear_urls,
        )

        url_footer = ttk.Frame(url_frame)
        url_footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        url_footer.columnconfigure(0, weight=1)
        ttk.Label(
            url_footer,
            text="One URL per line \u2022 Right-click: Paste & Queue \u2022 Ctrl+Enter to start",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        url_btn_row = ttk.Frame(url_footer)
        url_btn_row.grid(row=0, column=1, sticky="e")
        ttk.Button(url_btn_row, text="Paste", command=self._paste_urls_from_clipboard).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(url_btn_row, text="Load .txt...", command=self._browse_url_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(url_btn_row, text="Clear", command=self._clear_urls).pack(side=tk.LEFT)

        # Output Location
        out_frame = ttk.LabelFrame(container, text="Output", padding=(10, 6))
        out_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Folder:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        out_entry = ttk.Entry(out_frame, textvariable=self.output_dir_var)
        out_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        out_browse = ttk.Button(out_frame, text="Browse...", command=self._browse_output_dir)
        out_browse.grid(row=0, column=2, padx=(8, 0), pady=(0, 4))

        ttk.Label(out_frame, text="Template:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        tpl_row = ttk.Frame(out_frame)
        tpl_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 4))
        tpl_row.columnconfigure(0, weight=1)
        tpl_entry = ttk.Entry(tpl_row, textvariable=self.template_var)
        tpl_entry.grid(row=0, column=0, sticky="ew")
        self.preset_combo = ttk.Combobox(
            tpl_row,
            textvariable=self.template_preset_var,
            values=[label for label, _tpl in TEMPLATE_PRESET_LABELS],
            state="readonly",
            width=16,
        )
        self.preset_combo.grid(row=0, column=1, padx=(8, 0))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        ttk.Label(out_frame, text="Preview:").grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Label(out_frame, textvariable=self.template_preview_var, style="Muted.TLabel").grid(
            row=2, column=1, columnspan=2, sticky="w"
        )
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

        # Service & Authentication
        auth_frame = ttk.LabelFrame(container, text="Service & Authentication", padding=(10, 6))
        auth_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        auth_frame.columnconfigure(1, weight=1)
        auth_frame.columnconfigure(3, weight=1)

        ttk.Label(auth_frame, text="Service:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        service_row = ttk.Frame(auth_frame)
        service_row.grid(row=0, column=1, columnspan=4, sticky="ew", pady=(0, 4))
        service_row.columnconfigure(1, weight=1)

        self.service_combo = ttk.Combobox(
            service_row,
            textvariable=self.service_label_var,
            values=[s.label for s in self._services],
            state="readonly",
            width=24,
        )
        self.service_combo.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.service_combo.bind("<<ComboboxSelected>>", self._on_service_selected)

        ttk.Label(service_row, textvariable=self.service_hint_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(service_row, textvariable=self.saved_badge_var, style="Success.TLabel").grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )

        ttk.Label(auth_frame, text="Cookies File:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.cookie_entry = ttk.Entry(auth_frame, textvariable=self.cookie_file_var)
        self.cookie_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(0, 4))
        cookie_btn_frame = ttk.Frame(auth_frame)
        cookie_btn_frame.grid(row=1, column=3, columnspan=2, sticky="e", pady=(0, 4))
        self.cookie_browse = ttk.Button(cookie_btn_frame, text="Browse...", command=self._browse_cookie_file)
        self.cookie_browse.pack(side=tk.LEFT)
        self.cookie_import_btn = ttk.Button(
            cookie_btn_frame,
            text="Import...",
            command=self._show_cookie_import_dialog,
        )
        self.cookie_import_btn.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(auth_frame, text="Username:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.user_entry = ttk.Entry(auth_frame, textvariable=self.username_var)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 4))

        ttk.Label(auth_frame, text="Password:").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(0, 4))
        self.password_entry = ttk.Entry(auth_frame, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=2, column=3, sticky="ew", pady=(0, 4))
        self.show_pw_btn = ttk.Checkbutton(
            auth_frame,
            text="Show",
            variable=self.show_password_var,
            command=self._toggle_password_visibility,
        )
        self.show_pw_btn.grid(row=2, column=4, padx=(8, 0), pady=(0, 4))

        ttk.Label(auth_frame, text="Library:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.lib_entry = ttk.Entry(auth_frame, textvariable=self.library_var, width=14)
        self.lib_entry.grid(row=3, column=1, sticky="ew", padx=(0, 10))

        auth_actions = ttk.Frame(auth_frame)
        auth_actions.grid(row=3, column=2, columnspan=3, sticky="ew")
        self.remember_cb = ttk.Checkbutton(
            auth_actions,
            text="Remember login",
            variable=self.remember_login_var,
        )
        self.remember_cb.pack(side=tk.LEFT)
        self.forget_auth_btn = ttk.Button(auth_actions, text="Forget", command=self.forget_current_credentials)
        self.forget_auth_btn.pack(side=tk.RIGHT)
        self.save_auth_btn = ttk.Button(auth_actions, text="Save", command=self.save_current_credentials)
        self.save_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.test_auth_btn = ttk.Button(auth_actions, text="Test Login", command=self.verify_current_auth)
        self.test_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))

        # Options
        opt_frame = ttk.LabelFrame(container, text="Options", padding=(8, 4))
        opt_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        opt_frame.columnconfigure(0, weight=1)

        self.options_notebook = ttk.Notebook(opt_frame)
        self.options_notebook.grid(row=0, column=0, sticky="ew")

        general_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        self.options_notebook.add(general_tab, text="General")

        cb_m4b = ttk.Checkbutton(
            general_tab,
            text="Download as .m4b",
            variable=self.m4b_var,
            command=self._on_m4b_toggled,
        )
        cb_m4b.grid(row=0, column=0, columnspan=2, sticky="w", padx=(0, 12))

        ttk.Label(general_tab, text="Format:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        format_combo = ttk.Combobox(
            general_tab,
            textvariable=self.output_format_var,
            values=OUTPUT_FORMATS,
            state="readonly",
            width=10,
        )
        format_combo.grid(row=0, column=3, sticky="w", padx=(0, 12))
        format_combo.bind("<<ComboboxSelected>>", self._on_format_selected)

        cb_combine = ttk.Checkbutton(general_tab, text="Combine parts", variable=self.combine_var)
        cb_combine.grid(row=0, column=4, sticky="w", padx=(0, 12))

        cb_skip = ttk.Checkbutton(general_tab, text="Skip downloaded", variable=self.skip_downloaded_var)
        cb_skip.grid(row=0, column=5, sticky="w")

        cb_resume = ttk.Checkbutton(general_tab, text="Resume partial", variable=self.resume_var)
        cb_resume.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 12), pady=(4, 0))

        ttk.Label(general_tab, text="Overwrite:").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=(4, 0))
        overwrite_combo = ttk.Combobox(
            general_tab,
            textvariable=self.overwrite_var,
            values=OVERWRITE_OPTIONS,
            state="readonly",
            width=12,
        )
        overwrite_combo.grid(row=1, column=3, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_archive = ttk.Checkbutton(
            general_tab,
            text="Use archive ledger",
            variable=self.download_archive_var,
        )
        cb_archive.grid(row=1, column=4, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_cover_only = ttk.Checkbutton(
            general_tab,
            text="Cover only",
            variable=self.cover_only_var,
        )
        cb_cover_only.grid(row=1, column=5, sticky="w", pady=(4, 0))

        ttk.Label(general_tab, text="Media:").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        media_combo = ttk.Combobox(
            general_tab,
            textvariable=self.media_type_var,
            values=MEDIA_TYPE_OPTIONS,
            state="readonly",
            width=10,
        )
        media_combo.grid(row=2, column=1, sticky="w", padx=(0, 12), pady=(4, 0))

        ttk.Label(general_tab, text="M4B Quality:").grid(row=2, column=2, sticky="w", padx=(0, 4), pady=(4, 0))
        bitrate_values = list(AUDIO_BITRATE_PRESETS)
        if self.audio_bitrate_var.get() not in bitrate_values:
            bitrate_values.append(self.audio_bitrate_var.get())
        bitrate_combo = ttk.Combobox(
            general_tab,
            textvariable=self.audio_bitrate_var,
            values=bitrate_values,
            state="readonly",
            width=10,
        )
        bitrate_combo.grid(row=2, column=3, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_enrich = ttk.Checkbutton(
            general_tab,
            text="Enrich metadata",
            variable=self.enrich_metadata_var,
        )
        cb_enrich.grid(row=2, column=4, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_debug = ttk.Checkbutton(general_tab, text="Debug logs", variable=self.debug_var)
        cb_debug.grid(row=2, column=5, sticky="w", pady=(4, 0))

        self.notify_cb = ttk.Checkbutton(
            general_tab,
            text="Notify on finish",
            variable=self.notify_on_complete_var,
        )
        self.notify_cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        cb_enrich_interactive = ttk.Checkbutton(
            general_tab,
            text="Interactive enrich",
            variable=self.enrich_interactive_var,
        )
        cb_enrich_interactive.grid(row=3, column=2, columnspan=2, sticky="w", padx=(0, 12), pady=(4, 0))

        enrich_row = ttk.Frame(general_tab)
        enrich_row.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(4, 0))
        ttk.Label(enrich_row, text="Enrich providers:").pack(side=tk.LEFT, padx=(0, 6))
        enrich_providers_entry = ttk.Entry(
            enrich_row,
            textvariable=self.enrich_providers_var,
            width=26,
        )
        enrich_providers_entry.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            enrich_row,
            text="audnexus, itunes, google, openlibrary",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        sidecars_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        sidecars_tab.columnconfigure(3, weight=1)
        self.options_notebook.add(sidecars_tab, text="Chapters & Sidecars")

        cb_no_chapters = ttk.Checkbutton(sidecars_tab, text="No chapters", variable=self.no_chapters_var)
        cb_no_chapters.grid(row=0, column=0, sticky="w", padx=(0, 14))

        cb_split_chapters = ttk.Checkbutton(
            sidecars_tab,
            text="Split chapters",
            variable=self.split_chapters_var,
        )
        cb_split_chapters.grid(row=0, column=1, columnspan=2, sticky="w")

        cb_json = ttk.Checkbutton(sidecars_tab, text="Write JSON metadata", variable=self.write_json_var)
        cb_json.grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_opf = ttk.Checkbutton(sidecars_tab, text="Write OPF", variable=self.write_opf_var)
        cb_opf.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_cue = ttk.Checkbutton(sidecars_tab, text="Write CUE", variable=self.write_cue_var)
        cb_cue.grid(row=1, column=2, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_write_cover = ttk.Checkbutton(sidecars_tab, text="Write cover", variable=self.write_cover_var)
        cb_write_cover.grid(row=1, column=3, sticky="w", pady=(4, 0))

        remove_row = ttk.Frame(sidecars_tab)
        remove_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(remove_row, text="Remove chars:").pack(side=tk.LEFT, padx=(0, 6))
        remove_chars_entry = ttk.Entry(remove_row, textvariable=self.remove_chars_var, width=18)
        remove_chars_entry.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            remove_row,
            text="Characters stripped from output paths",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        network_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        network_tab.columnconfigure(5, weight=1)
        self.options_notebook.add(network_tab, text="Network & Performance")

        ttk.Label(network_tab, text="Max workers:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        max_workers_spin = ttk.Spinbox(
            network_tab,
            from_=1,
            to=20,
            textvariable=self.max_workers_var,
            width=6,
        )
        max_workers_spin.grid(row=0, column=1, sticky="w", padx=(0, 14))

        ttk.Label(network_tab, text="Book delay (s):").grid(row=0, column=2, sticky="w", padx=(0, 4))
        book_delay_spin = ttk.Spinbox(
            network_tab,
            from_=0,
            to=3600,
            increment=0.5,
            textvariable=self.book_delay_var,
            width=7,
        )
        book_delay_spin.grid(row=0, column=3, sticky="w", padx=(0, 14))

        ttk.Label(network_tab, text="Timeout (s):").grid(row=0, column=4, sticky="w", padx=(0, 4))
        timeout_spin = ttk.Spinbox(
            network_tab,
            from_=1,
            to=3600,
            increment=5,
            textvariable=self.timeout_var,
            width=7,
        )
        timeout_spin.grid(row=0, column=5, sticky="w")

        ttk.Label(network_tab, text="Proxy URL:").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        proxy_entry = ttk.Entry(network_tab, textvariable=self.proxy_var)
        proxy_entry.grid(row=1, column=1, columnspan=5, sticky="ew", pady=(6, 0))

        ttk.Label(
            network_tab,
            text="Leave Max workers / Timeout / Proxy blank for defaults (6 workers, e.g. http://127.0.0.1:8080)",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # Actions, Progress & Log
        log_frame = ttk.LabelFrame(container, text="Activity", padding=(10, 6))
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(3, weight=1)

        action_bar = ttk.Frame(log_frame)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        action_bar.columnconfigure(5, weight=1)

        self.download_btn = ttk.Button(action_bar, text="Download", command=self.start_download)
        self.download_btn.grid(row=0, column=0, padx=(0, 6))

        self.cancel_btn = ttk.Button(action_bar, text="Cancel", command=self.cancel_download)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 6))

        self.preview_btn = ttk.Button(action_bar, text="Preview", command=self.preview_metadata)
        self.preview_btn.grid(row=0, column=2, padx=(0, 6))

        self.open_folder_btn = ttk.Button(action_bar, text="Open Folder", command=self._open_output_folder)
        self.open_folder_btn.grid(row=0, column=3, padx=(0, 6))

        ttk.Button(action_bar, text="Clear Log", command=self._clear_log).grid(row=0, column=4)

        self.status_label = ttk.Label(action_bar, textvariable=self.status_var, anchor="e")
        self.status_label.grid(row=0, column=5, sticky="e")

        meta_bar = ttk.Frame(log_frame)
        meta_bar.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        meta_bar.columnconfigure(1, weight=1)

        self.batch_label = ttk.Label(meta_bar, textvariable=self.batch_var, style="Secondary.TLabel")
        self.batch_label.grid(row=0, column=0, sticky="w")

        self.metrics_label = ttk.Label(meta_bar, textvariable=self.metrics_var, style="Muted.TLabel", anchor="e")
        self.metrics_label.grid(row=0, column=1, sticky="e")

        self.progressbar = ttk.Progressbar(log_frame, mode="determinate", maximum=100)
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self.activity_notebook = ttk.Notebook(log_frame)
        self.activity_notebook.grid(row=3, column=0, sticky="nsew")

        log_tab = ttk.Frame(self.activity_notebook)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(log_tab, height=4, width=40, state="disabled", wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.activity_notebook.add(log_tab, text="Activity Log")

        queue_tab = ttk.Frame(self.activity_notebook, padding=4)
        queue_tab.columnconfigure(0, weight=1)
        queue_tab.rowconfigure(1, weight=1)

        queue_toolbar = ttk.Frame(queue_tab)
        queue_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.queue_remove_btn = ttk.Button(queue_toolbar, text="Remove", command=self._remove_selected_queue_item)
        self.queue_remove_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_up_btn = ttk.Button(queue_toolbar, text="Move Up", command=lambda: self._move_selected_queue_item(-1))
        self.queue_up_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_down_btn = ttk.Button(queue_toolbar, text="Move Down", command=lambda: self._move_selected_queue_item(1))
        self.queue_down_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.queue_retry_btn = ttk.Button(queue_toolbar, text="Retry Failed", command=self._retry_failed_queue_items)
        self.queue_retry_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_clear_btn = ttk.Button(queue_toolbar, text="Clear Completed", command=self._clear_completed_history)
        self.queue_clear_btn.pack(side=tk.RIGHT)

        queue_columns = (
            TreeColumnSpec("service", "Service", width=90, stretch=False),
            TreeColumnSpec("url", "URL / Title", width=220, stretch=True),
            TreeColumnSpec("status", "Status", width=90, stretch=False),
            TreeColumnSpec("detail", "Detail", width=140, stretch=True),
        )
        scrolled_queue = ScrolledTreeview(queue_tab, queue_columns, selectmode="browse", height=3)
        scrolled_queue.grid(row=1, column=0, sticky="nsew")
        self.queue_tree = scrolled_queue.tree
        self.activity_notebook.add(queue_tab, text="Queue & History")

        # Bind optional drag-and-drop URL support when tkinterdnd2 is available on host Tk.
        if hasattr(self.urls_text, "drop_target_register") and hasattr(self.urls_text, "dnd_bind"):
            try:
                self.urls_text.drop_target_register("DND_Text")  # type: ignore[attr-defined]
                self.urls_text.dnd_bind("<<Drop>>", self._on_url_drop)  # type: ignore[attr-defined]
            except Exception:
                pass

        self._interactive_widgets = [
            out_entry,
            out_browse,
            tpl_entry,
            self.preset_combo,
            self.service_combo,
            self.cookie_entry,
            self.cookie_browse,
            self.cookie_import_btn,
            self.user_entry,
            self.password_entry,
            self.show_pw_btn,
            self.lib_entry,
            self.remember_cb,
            self.test_auth_btn,
            self.save_auth_btn,
            self.forget_auth_btn,
            cb_m4b,
            format_combo,
            overwrite_combo,
            bitrate_combo,
            media_combo,
            cb_combine,
            cb_skip,
            cb_resume,
            cb_archive,
            cb_cover_only,
            cb_no_chapters,
            cb_split_chapters,
            cb_json,
            cb_opf,
            cb_cue,
            cb_write_cover,
            remove_chars_entry,
            max_workers_spin,
            book_delay_spin,
            timeout_spin,
            proxy_entry,
            cb_enrich,
            cb_enrich_interactive,
            enrich_providers_entry,
            cb_debug,
        ]

    def _build_menu_bar(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load URLs from .txt...", command=self._browse_url_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Open Output Folder", command=self._open_output_folder)
        file_menu.add_command(label="Reload Config", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        theme_menu = tk.Menu(view_menu, tearoff=0)
        for mode in THEME_MODES:
            theme_menu.add_radiobutton(
                label=THEME_LABELS[mode],
                variable=self._theme_var,
                value=mode,
                command=lambda m=mode: self.set_theme(m),
            )
        view_menu.add_cascade(label="Theme", menu=theme_menu)
        view_menu.add_separator()
        view_menu.add_checkbutton(
            label="Notify on Download Finish",
            variable=self.notify_on_complete_var,
        )
        menubar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Save Credentials", command=self.save_current_credentials)
        tools_menu.add_command(label="Test Login", command=self.verify_current_auth)
        tools_menu.add_command(
            label="Import Browser Cookies...",
            command=self._show_cookie_import_dialog,
        )
        tools_menu.add_command(label="Browse Library / Search...", command=self._show_catalog_browser_dialog)
        tools_menu.add_command(label="Locate FFmpeg Folder...", command=self.browse_ffmpeg_dir)
        tools_menu.add_command(label="Download History Ledger...", command=self._show_archive_ledger_dialog)
        tools_menu.add_separator()
        tools_menu.add_command(label="Retry Failed Jobs", command=self._retry_failed_queue_items)
        tools_menu.add_command(label="Clear Completed History", command=self._clear_completed_history)
        tools_menu.add_command(label="Clear Log", command=self._clear_log)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="FFmpeg Setup Guide", command=self._show_ffmpeg_guidance)
        if sys.platform.startswith("win"):
            help_menu.add_command(label="Install FFmpeg via winget...", command=self._install_ffmpeg_winget)
        help_menu.add_command(label="Check for Updates...", command=self.check_updates_interactive)
        help_menu.add_separator()
        help_menu.add_command(label="About audiobook-dl", command=self._show_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self._browse_url_file())
        self.root.bind("<Control-q>", lambda _e: self._on_close())

    def _on_url_drop(self, event: Any) -> None:
        dropped = getattr(event, "data", "")
        if not dropped:
            return
        urls = parse_url_text(str(dropped).strip("{}"))
        if urls:
            self._append_urls_to_input(urls)

    def _append_urls_to_input(self, urls: Sequence[str]) -> List[str]:
        existing = parse_url_text(self.urls_text.get("1.0", tk.END))
        existing_set = set(existing)
        added = [u for u in urls if u not in existing_set]
        if added:
            combined = "\n".join([*existing, *added])
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()
        return added

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
            self.set_theme(cfg.gui.theme or "system")
            self.m4b_var.set(self.output_format_var.get() == "m4b")
            self._load_service_into_form()
            self._update_service_field_states()
            self._update_ffmpeg_status()
            self.status_var.set("Config reloaded")
            self._append_log("Reloaded configuration.")
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc), parent=self.root)

    def _show_about_dialog(self) -> None:
        messagebox.showinfo(
            "About audiobook-dl",
            f"audiobook-dl v{__version__}\n"
            f"Supported services: {len(self._services)}\n"
            "https://github.com/jo1gi/audiobook-dl",
            parent=self.root,
        )

    def _archive_ledger_path(self) -> str:
        cfg = self._controller.config_snapshot()
        if cfg.download_archive:
            return cfg.download_archive
        db_dir = cfg.database_directory or args.default_database_directory()
        return default_archive_path(db_dir)

    def _show_archive_ledger_dialog(self) -> ArchiveLedgerDialog:
        return ArchiveLedgerDialog(
            self.root,
            self._archive_ledger_path(),
            palette=self._palette,
        )

    def _show_catalog_browser_dialog(self) -> CatalogBrowserDialog:
        def _on_fetch(mode: str, svc: ServiceInfo, query_text: str, dialog: CatalogBrowserDialog) -> None:
            options = self._try_build_options(urls_override=[], parent=dialog)
            if options is None:
                dialog.set_error_status("Invalid option value")
                return
            self._controller.start_catalog_fetch(mode, svc.key, query_text, options, dialog)

        def _on_add_urls(urls_to_add: List[str]) -> None:
            added = self._append_urls_to_input(urls_to_add)
            self.status_var.set(f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from catalog")

        return CatalogBrowserDialog(
            self.root,
            self._services,
            self._selected_service(),
            on_fetch=_on_fetch,
            on_add_urls=_on_add_urls,
            palette=self._palette,
        )

    def _apply_window_icon(self) -> None:
        try:
            icon_ref = importlib.resources.files("audiobookdl").joinpath("assets/icon.ico")
            with importlib.resources.as_file(icon_ref) as icon_path:
                if icon_path.is_file():
                    self.root.iconbitmap(default=str(icon_path))
        except Exception:
            pass

    def _update_ffmpeg_status(self) -> None:
        has_ffmpeg = find_executable("ffmpeg") is not None
        has_ffprobe = find_executable("ffprobe") is not None
        if has_ffmpeg and has_ffprobe:
            self.ffmpeg_status_var.set("FFmpeg: Ready")
            self._ffmpeg_label.configure(style="Success.TLabel", cursor="")
            self.ffmpeg_locate_btn.configure(text="FFmpeg Folder...")
        elif has_ffmpeg:
            self.ffmpeg_status_var.set("FFmpeg: Ready (ffprobe missing - click for help)")
            self._ffmpeg_label.configure(style="Warning.TLabel", cursor="hand2")
            self.ffmpeg_locate_btn.configure(text="Locate FFmpeg...")
        else:
            self.ffmpeg_status_var.set("FFmpeg: Not found (click for setup help)")
            self._ffmpeg_label.configure(style="Error.TLabel", cursor="hand2")
            self.ffmpeg_locate_btn.configure(text="Locate FFmpeg...")

    def browse_ffmpeg_dir(self) -> bool:
        """Let the user choose a folder containing `ffmpeg` and persist it in config."""
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Select folder containing ffmpeg / ffprobe",
            initialdir=self._controller.config.ffmpeg_dir or os.getcwd(),
        )
        if not chosen:
            return False
        resolved = resolve_ffmpeg_dir(chosen)
        if resolved is None:
            exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            messagebox.showwarning(
                "FFmpeg Not Found",
                f"Could not find {exe_name} inside:\n{chosen}\n\n"
                "Please choose the folder (or its bin subfolder) containing ffmpeg.",
                parent=self.root,
            )
            return False
        self._controller.save_ffmpeg_dir(resolved)
        self._update_ffmpeg_status()
        self.status_var.set("FFmpeg folder updated")
        self._append_log(f"Using FFmpeg from: {resolved}")
        return True

    def _ensure_ffmpeg_available(self, options: args.DownloadOptions) -> bool:
        """Validate FFmpeg/FFprobe up front when combine, split, or format conversion is enabled."""
        if getattr(options, "cover", False):
            return True
        if getattr(options, "media_type", "auto") == "ebook":
            return True
        needs_ffmpeg = bool(options.combine or options.output_format or options.split_chapters)
        if not needs_ffmpeg:
            return True
        needs_ffprobe = bool(options.combine or options.split_chapters)
        has_ffmpeg = find_executable("ffmpeg") is not None
        has_ffprobe = find_executable("ffprobe") is not None
        if has_ffmpeg and (not needs_ffprobe or has_ffprobe):
            return True

        missing_parts = []
        if not has_ffmpeg:
            missing_parts.append("ffmpeg")
        if needs_ffprobe and not has_ffprobe:
            missing_parts.append("ffprobe")
        missing_text = " and ".join(missing_parts)

        reasons = []
        if options.combine:
            reasons.append("combine parts")
        if options.split_chapters:
            reasons.append("split chapters")
        if options.output_format:
            reasons.append(f"convert to .{options.output_format}")
        reason_text = " and ".join(reasons)

        choice = messagebox.askyesnocancel(
            "FFmpeg Required",
            f"{missing_text} was not found, which is needed to {reason_text}.\n\n"
            "Yes: Locate FFmpeg folder now\n"
            "No: Continue anyway (may fail after downloading parts)\n"
            "Cancel: Stop and view setup instructions",
            parent=self.root,
        )
        if choice is True:
            if self.browse_ffmpeg_dir():
                has_ffmpeg = find_executable("ffmpeg") is not None
                has_ffprobe = find_executable("ffprobe") is not None
                return has_ffmpeg and (not needs_ffprobe or has_ffprobe)
            return False
        if choice is False:
            return True
        self._show_ffmpeg_guidance()
        return False

    def _install_ffmpeg_winget(self) -> None:
        """Install FFmpeg via Windows winget (`Gyan.FFmpeg`) on a background thread."""
        if not sys.platform.startswith("win") or not winget_available():
            messagebox.showinfo(
                "winget Unavailable",
                "Windows Package Manager (winget) was not found on PATH.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Install FFmpeg via winget",
            "Run `winget install -e --id Gyan.FFmpeg` to install FFmpeg?",
            parent=self.root,
        ):
            return
        self.status_var.set("Installing FFmpeg via winget...")
        self._append_log("Installing FFmpeg via winget (Gyan.FFmpeg)...")
        self._controller.start_winget_ffmpeg_install()

    def _show_ffmpeg_guidance(self) -> None:
        ffmpeg_path = find_executable("ffmpeg")
        ffprobe_path = find_executable("ffprobe")
        if ffmpeg_path and ffprobe_path:
            messagebox.showinfo(
                "FFmpeg Ready",
                f"ffmpeg: {ffmpeg_path}\nffprobe: {ffprobe_path}",
                parent=self.root,
            )
            return
        exe_dir = (
            os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.getcwd()
        )
        messagebox.showinfo(
            "FFmpeg Setup Guide",
            "FFmpeg and FFprobe are needed to combine multi-part books or convert to .m4b/.mp3.\n\n"
            "Quickest setup:\n"
            "1. Click 'Locate FFmpeg...' in the top-right to point to an existing ffmpeg folder.\n"
            f"2. Or place ffmpeg.exe and ffprobe.exe in:\n   {exe_dir}\n\n"
            "Install via package manager:\n"
            "   Windows: winget install Gyan.FFmpeg\n"
            "   macOS:   brew install ffmpeg\n"
            "   Linux:   sudo apt install ffmpeg\n\n"
            "Download builds: https://ffmpeg.org/download.html",
            parent=self.root,
        )

    def _selected_service(self) -> Optional[ServiceInfo]:
        return self._services_by_label.get(self.service_label_var.get())

    def _on_service_selected(self, _event: Optional[tk.Event] = None) -> None:
        self._load_service_into_form()
        self._update_service_field_states()

    def _on_urls_changed(self, _event: Optional[tk.Event] = None) -> None:
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if not urls:
            self._last_detected_service = None
            return
        detected = detect_service_key(urls[0])
        if detected and detected != self._last_detected_service:
            self._last_detected_service = detected
            service = self._services_by_key.get(detected)
            if service and self.service_label_var.get() != service.label:
                self.service_label_var.set(service.label)
                self._load_service_into_form()
                self._update_service_field_states()

    def _load_service_into_form(self) -> None:
        service = self._selected_service()
        if service is None:
            return
        saved: Optional[SourceConfig] = self._controller.config.sources.get(service.key)
        self.username_var.set((saved.username or "") if saved else "")
        self.password_var.set((saved.password or "") if saved else "")
        self.library_var.set((saved.library or "") if saved else "")
        self.cookie_file_var.set((saved.cookie_file or "") if saved else "")

        has_saved = bool(
            saved and any((saved.username, saved.password, saved.library, saved.cookie_file))
        )
        self.saved_badge_var.set("Saved" if has_saved else "")

        supports_cookies = "cookies" in service.auth_methods
        supports_login = "login" in service.auth_methods
        needs_library = "library" in service.login_fields
        if not service.auth_methods:
            self.service_hint_var.set("No authentication required")
        elif supports_cookies and supports_login:
            suffix = " (+ Library)" if needs_library else ""
            self.service_hint_var.set(f"Login{suffix} or Cookies file")
        elif supports_login:
            suffix = " (+ Library)" if needs_library else ""
            self.service_hint_var.set(f"Username & Password{suffix}")
        else:
            self.service_hint_var.set("Cookies file required")

    def _update_service_field_states(self) -> None:
        if self._is_running:
            return
        service = self._selected_service()
        if service is None:
            return
        supports_cookies = "cookies" in service.auth_methods
        supports_login = "login" in service.auth_methods
        needs_library = supports_login and "library" in service.login_fields

        cookie_state = "normal" if supports_cookies else "disabled"
        login_state = "normal" if supports_login else "disabled"
        library_state = "normal" if needs_library else "disabled"
        save_state = "normal" if service.auth_methods else "disabled"

        self.cookie_entry.configure(state=cookie_state)
        self.cookie_browse.configure(state=cookie_state)
        self.cookie_import_btn.configure(state=cookie_state)
        self.user_entry.configure(state=login_state)
        self.password_entry.configure(state=login_state)
        self.show_pw_btn.configure(state=login_state)
        self.lib_entry.configure(state=library_state)
        self.test_auth_btn.configure(
            state="disabled" if self._is_verifying_auth else save_state
        )
        self.save_auth_btn.configure(state=save_state)
        has_saved = service.key in self._controller.config.sources
        self.forget_auth_btn.configure(state="normal" if has_saved else "disabled")

    def verify_current_auth(self) -> None:
        """Test authentication for the currently selected service in the background."""
        if self._is_verifying_auth or self._is_running:
            return
        service = self._selected_service()
        if service is None or not service.auth_methods:
            return
        options = self._try_build_options(urls_override=[])
        if options is None:
            return
        self._is_verifying_auth = True
        self.test_auth_btn.configure(state="disabled")
        self.status_var.set(f"Testing login for {service.label}...")
        self._controller.start_auth_verification(service.key, service.label, options)

    def _schedule_update_check(self) -> None:
        """Run a quiet background update check at launch unless opted out."""
        if self._controller.config.check_updates is False:
            return
        self._controller.start_update_check(interactive=False, respect_opt_out=True)

    def check_updates_interactive(self) -> None:
        """Check for newer releases from the Help menu."""
        self.status_var.set("Checking for updates...")
        self._controller.start_update_check(interactive=True, respect_opt_out=False)

    def save_current_credentials(self, quiet: bool = False) -> None:
        """Persist the selected service's login, password, library, and cookie path."""
        service = self._selected_service()
        if service is None or not service.auth_methods:
            return
        supports_cookies = "cookies" in service.auth_methods
        supports_login = "login" in service.auth_methods
        needs_library = supports_login and "library" in service.login_fields

        username = self.username_var.get().strip() or None if supports_login else None
        password = self.password_var.get() or None if supports_login else None
        library = self.library_var.get().strip() or None if needs_library else None
        cookie_file = self.cookie_file_var.get().strip() or None if supports_cookies else None

        if quiet and not any((username, password, library, cookie_file)):
            return

        cfg = self._controller.save_source_credentials(
            service.key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
        )
        has_saved = service.key in cfg.sources
        self.saved_badge_var.set("Saved" if has_saved else "")
        self._update_service_field_states()
        if not quiet:
            if has_saved:
                self.status_var.set(f"Saved credentials for {service.label}")
                self._append_log(f"Saved login settings for {service.label}.")
            else:
                self.status_var.set(f"Cleared credentials for {service.label}")

    def forget_current_credentials(self) -> None:
        """Remove saved credentials for the currently selected service."""
        service = self._selected_service()
        if service is None:
            return
        self._controller.save_source_credentials(service.key)
        self.username_var.set("")
        self.password_var.set("")
        self.library_var.set("")
        self.cookie_file_var.set("")
        self.saved_badge_var.set("")
        self._update_service_field_states()
        self.status_var.set(f"Removed saved login for {service.label}")
        self._append_log(f"Removed saved login settings for {service.label}.")

    def _on_m4b_toggled(self) -> None:
        if self.m4b_var.get():
            self.output_format_var.set("m4b")
            self.combine_var.set(True)
        elif self.output_format_var.get() == "m4b":
            self.output_format_var.set("Original")

    def _on_format_selected(self, _event: Optional[tk.Event] = None) -> None:
        is_m4b = self.output_format_var.get() == "m4b"
        self.m4b_var.set(is_m4b)
        if is_m4b:
            self.combine_var.set(True)

    def _on_preset_selected(self, _event: Optional[tk.Event] = None) -> None:
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
            rendered = output.gen_output_location(full_tpl, SAMPLE_PREVIEW_METADATA, remove_chars)
            if self.cover_only_var.get():
                suffix = ".jpg"
            elif self.split_chapters_var.get() or not self.combine_var.get():
                suffix = f"/01.{ext}"
            else:
                suffix = f".{ext}"
            self.template_preview_var.set(f"{rendered}{suffix}")
        except Exception as exc:
            self.template_preview_var.set(f"(invalid template: {exc})")

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def _browse_url_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as exc:
            messagebox.showerror("Error", f"Could not read file:\n{exc}", parent=self.root)
            return
        if content:
            existing = self.urls_text.get("1.0", tk.END).strip()
            combined = f"{existing}\n{content}" if existing else content
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()

    def _clear_urls(self) -> None:
        self.urls_text.delete("1.0", tk.END)
        self._on_urls_changed()

    def _paste_urls_from_clipboard(self) -> List[str]:
        """Insert URLs from the system clipboard into the URL box."""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            self.status_var.set("Clipboard is empty")
            return []
        pasted = parse_url_text(raw)
        if not pasted:
            self.status_var.set("No URLs found in clipboard")
            return []
        added = self._append_urls_to_input(pasted)
        if added:
            self.status_var.set(f"Pasted {len(added)} URL{'s' if len(added) != 1 else ''}")
        else:
            self.status_var.set("Clipboard URL(s) already listed")
        return added

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

    def _browse_cookie_file(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Select Netscape cookie file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self.cookie_file_var.set(chosen)

    def _show_cookie_import_dialog(self) -> None:
        """Open the interactive browser-cookie importer dialog."""
        if self._cookie_dialog is not None and self._cookie_dialog.winfo_exists():
            self._cookie_dialog.lift()
            self._cookie_dialog.focus_set()
            return
        selected = self._selected_service()
        initial_key = selected.key if (selected and "cookies" in selected.auth_methods) else ""
        initial_path = self.cookie_file_var.get().strip() if initial_key else ""
        self._cookie_dialog = CookieImportDialog(
            self.root,
            self._services,
            initial_service_key=initial_key,
            initial_cookie_path=initial_path,
            on_apply=self._on_cookies_imported,
            palette=self._palette,
        )

    def _on_cookies_imported(self, service_key: str, cookie_path: str, count: int) -> None:
        """Apply an imported Netscape cookie file to the selected service form."""
        service = self._services_by_key.get(service_key)
        if service is not None and self.service_label_var.get() != service.label:
            self.service_label_var.set(service.label)
            self._load_service_into_form()
            self._update_service_field_states()
        self.cookie_file_var.set(cookie_path)
        if self.remember_login_var.get() and service is not None and "cookies" in service.auth_methods:
            self.save_current_credentials(quiet=True)
        self.status_var.set(f"Imported {count} cookies to {os.path.basename(cookie_path)}")
        self._append_log(f"Imported {count} browser cookie(s) to {cookie_path}")

    def _open_output_folder(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        if not os.path.isdir(folder):
            messagebox.showinfo("Folder Missing", f"Folder does not exist yet:\n{folder}", parent=self.root)
            return
        try:
            if sys.platform.startswith("win") and hasattr(os, "startfile"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            messagebox.showerror(
                "Open Folder Failed",
                f"Could not open folder:\n{folder}\n\n{exc}",
                parent=self.root,
            )

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            excess = line_count - self.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        widget_state = "disabled" if running else "normal"
        for widget in self._interactive_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state=widget_state)
        if running:
            self.download_btn.configure(text="Add to Queue", state="normal")
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        else:
            self.download_btn.configure(text="Download", state="normal")
            self.cancel_btn.configure(state="disabled")
            self.preview_btn.configure(state="disabled" if self._is_previewing else "normal")
            self._update_service_field_states()

    def _refresh_batch_label(self) -> None:
        parts = []
        if self._ui_item_total > 0 and self._ui_item_index > 0:
            parts.append(f"Item {self._ui_item_index} / {self._ui_item_total}")
        if self._ui_book_total > 1 and self._ui_book_index > 0:
            parts.append(f"Book {self._ui_book_index} / {self._ui_book_total}")
        self.batch_var.set("  \u2022  ".join(parts))

    def _update_metrics_display(self, force: bool = False) -> None:
        if not self._is_running or self._run_start_time is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_metrics_render_at) < 0.5:
            return
        self._last_metrics_render_at = now
        elapsed = max(0.0, now - self._run_start_time)
        parts = []
        if self._last_pct > 0:
            parts.append(f"{self._last_pct:.0f}%")
        if self._last_speed_bps > 0:
            parts.append(format_speed(self._last_speed_bps))
        parts.append(f"Elapsed {format_duration(elapsed)}")
        if self._last_eta_seconds is not None:
            parts.append(f"ETA {format_duration(self._last_eta_seconds)}")
        self.metrics_var.set("  \u2022  ".join(parts))

    def _try_build_options(
        self,
        urls_override: Optional[Sequence[str]] = None,
        parent: Optional[tk.Misc] = None,
    ) -> Optional[args.DownloadOptions]:
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

    def build_options(self, urls_override: Optional[Sequence[str]] = None) -> args.DownloadOptions:
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

        urls = (
            list(urls_override)
            if urls_override is not None
            else parse_url_text(self.urls_text.get("1.0", tk.END))
        )
        detected_keys = {k for k in (detect_service_key(u) for u in urls) if k}
        service = self._selected_service()
        apply_form_auth = (
            service is not None
            and (len(detected_keys) <= 1)
            and (not detected_keys or service.key in detected_keys)
        )

        username = None
        password = None
        library = None
        cookie_file = None
        if apply_form_auth and service is not None:
            if "cookies" in service.auth_methods:
                cookie_file = self.cookie_file_var.get().strip() or None
            if "login" in service.auth_methods:
                username = self.username_var.get().strip() or None
                password = self.password_var.get() or None
                if "library" in service.login_fields:
                    library = self.library_var.get().strip() or None

        config_loc = self._controller.config_location
        effective_config_location = (
            config_loc if (config_loc and os.path.exists(config_loc)) else None
        )

        return args.build_default_options(
            urls=urls,
            cookie_file=cookie_file,
            output_template=output_template,
            username=username,
            password=password,
            library=library,
            download_archive=download_archive,
            config_location=effective_config_location,
            **option_values,
        )

    def _validate_cookie_file(self) -> bool:
        service = self._selected_service()
        cookie_file = (
            self.cookie_file_var.get().strip()
            if (service is None or "cookies" in service.auth_methods)
            else ""
        )
        if cookie_file and not os.path.isfile(cookie_file):
            self.status_var.set("Cookie file not found")
            messagebox.showerror("Invalid Cookie File", f"File not found:\n{cookie_file}", parent=self.root)
            return False
        return True

    def _service_label_for_url(self, url: str) -> str:
        detected = detect_service_key(url)
        if detected and detected in self._services_by_key:
            return self._services_by_key[detected].label
        return detected or "Unknown"

    def _register_queue_job(self, job: QueuedJob) -> None:
        """Insert a queued job row into the Queue & History table and internal index."""
        service_label = self._service_label_for_url(job.url)
        self._controller.register_queue_record(job, service_label)
        self.queue_tree.insert(
            "",
            tk.END,
            iid=job.job_id,
            values=(service_label, job.url, "Queued", ""),
        )

    def _update_queue_row(self, job_id: str, status: str, detail: str = "") -> None:
        rec = self._controller.update_queue_record(job_id, status, detail)
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=(rec["service"], rec["url"], status, detail),
            )

    def _remove_selected_queue_item(self) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        job_id = selected[0]
        removed, new_total, is_active_running = self._controller.remove_pending_job(job_id)
        if is_active_running:
            messagebox.showinfo(
                "Job Active",
                "The currently running download cannot be removed; use Cancel instead.",
                parent=self.root,
            )
            return
        if not removed:
            return
        if new_total is not None:
            self._ui_item_total = max(self._ui_item_index, new_total)
            self._refresh_batch_label()
        if self.queue_tree.exists(job_id):
            self.queue_tree.delete(job_id)

    def _move_selected_queue_item(self, direction: int) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        job_id = selected[0]
        neighbor_id = self._controller.move_pending_job(job_id, direction)
        if neighbor_id is None:
            return
        if self.queue_tree.exists(job_id) and self.queue_tree.exists(neighbor_id):
            idx_a = self.queue_tree.index(job_id)
            idx_b = self.queue_tree.index(neighbor_id)
            self.queue_tree.move(job_id, "", idx_b)
            self.queue_tree.move(neighbor_id, "", idx_a)

    def _retry_failed_queue_items(self) -> None:
        failed_urls = self._controller.retryable_urls(self.queue_tree.get_children())
        if not failed_urls:
            self.status_var.set("No failed or cancelled jobs to retry")
            return
        if self._is_running:
            if not self._controller.cancel_event.is_set():
                self._enqueue_additional_urls(failed_urls)
            return
        options = self._try_build_options()
        if options is None:
            return
        if not self._ensure_ffmpeg_available(options):
            return
        self._save_run_preferences(options)
        unique_urls = list(dict.fromkeys(failed_urls))
        job_specs = [
            (u, self.build_options(urls_override=[u]), None, None)
            for u in unique_urls
        ]
        self._start_new_run(job_specs)

    def _clear_completed_history(self) -> None:
        for iid in self._controller.clear_completed_records(self.queue_tree.get_children()):
            if self.queue_tree.exists(iid):
                self.queue_tree.delete(iid)

    def _enqueue_additional_urls(self, urls: List[str]) -> bool:
        """Append new URLs to the currently active download queue."""
        if not self._validate_cookie_file():
            return True
        if self._try_build_options(urls_override=[]) is None:
            return True
        has_active_run, added_jobs, new_total = self._controller.enqueue_additional_urls(
            urls,
            lambda u: self.build_options(urls_override=[u]),
        )
        if not has_active_run:
            return False
        if not added_jobs:
            self.status_var.set("URL(s) already in active queue")
            self._append_log("All entered URL(s) are already in the active queue.")
            return True
        for job in added_jobs:
            self._register_queue_job(job)
        self._ui_item_total = new_total
        self._refresh_batch_label()
        self.status_var.set(f"Queued +{len(added_jobs)} (total {new_total})")
        self._append_log(
            f"--- Added {len(added_jobs)} URL{'s' if len(added_jobs) != 1 else ''} to queue (total {new_total}) ---"
        )
        return True

    def _read_persisted_gui_preferences(self) -> Dict[str, Any]:
        """Read valid persisted GUI toggle/option values without failing on transient invalid input."""
        prefs: Dict[str, Any] = {}
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
        job_specs: Sequence[Tuple[str, args.DownloadOptions, Optional[List[int]], Optional[Tuple[Source, Result]]]],
    ) -> None:
        """Initialize UI and queue state for a fresh download run."""
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)
        self.status_var.set("Starting...")
        self._run_start_time = time.monotonic()
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds = None
        self._last_metrics_render_at = 0.0
        self._set_running_state(True)

        run_id, queued_jobs = self._controller.start_new_run(job_specs)
        self._current_ui_run_id = run_id

        for job in queued_jobs:
            self._register_queue_job(job)

        self._ui_item_index = 1
        self._ui_item_total = len(job_specs)
        self._ui_book_index = 0
        self._ui_book_total = 0
        self._refresh_batch_label()
        self._update_metrics_display(force=True)
        self._append_log(
            f"--- Starting download ({len(job_specs)} URL{'s' if len(job_specs) != 1 else ''}) ---"
        )

    def _warn_mixed_service_credentials(self, urls: Sequence[str]) -> bool:
        """Warn before starting if batch URLs target an unconfigured auth service alongside another."""
        selected = self._selected_service()
        selected_key = selected.key if selected else None
        detected_keys: List[str] = []
        for url in urls:
            key = detect_service_key(url)
            if key and key not in detected_keys:
                detected_keys.append(key)
        if len(detected_keys) <= 1 and (not detected_keys or detected_keys[0] == selected_key):
            return True
        cfg = self._controller.config_snapshot()
        missing: List[str] = []
        for key in detected_keys:
            if key == selected_key:
                continue
            info = self._services_by_key.get(key)
            if info is None or not info.auth_methods:
                continue
            src_cfg = cfg.sources.get(key)
            has_creds = src_cfg is not None and any((
                src_cfg.username,
                src_cfg.password,
                src_cfg.cookie_file,
                src_cfg.library,
            ))
            if not has_creds:
                missing.append(info.label)
        if not missing:
            return True
        return messagebox.askyesno(
            "Missing Credentials for Batch Service",
            "Some entered URLs target services without saved credentials:\n"
            f"  {', '.join(missing)}\n\n"
            "Continue anyway?",
            parent=self.root,
        )

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
            messagebox.showwarning("Missing URL", "Please enter at least one audiobook URL.", parent=self.root)
            self.urls_text.focus_set()
            return

        if not self._validate_cookie_file():
            return
        if not self._warn_mixed_service_credentials(urls):
            return

        options = self._try_build_options()
        if options is None:
            return
        if not self._ensure_ffmpeg_available(options):
            return
        self._save_run_preferences(options)

        unique_urls = list(dict.fromkeys(urls))
        job_specs = [
            (u, self.build_options(urls_override=[u]), None, None)
            for u in unique_urls
        ]
        self._start_new_run(job_specs)

    def _set_previewing_state(self, previewing: bool) -> None:
        self._is_previewing = previewing
        if previewing:
            self.preview_btn.configure(state="disabled")
            self.download_btn.configure(state="disabled")
        else:
            self.preview_btn.configure(state="disabled" if self._is_running else "normal")
            self.download_btn.configure(state="normal")

    def preview_metadata(self) -> None:
        """Fetch book or series metadata for the first entered URL without downloading audio."""
        if self._is_running or self._is_previewing:
            return
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if not urls:
            self.status_var.set("Enter a URL to preview")
            messagebox.showwarning(
                "Missing URL",
                "Please enter an audiobook or series URL to preview.",
                parent=self.root,
            )
            self.urls_text.focus_set()
            return
        if not self._validate_cookie_file():
            return

        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)

        target_url = urls[0]
        if len(urls) > 1:
            self._append_log(f"Previewing first URL: {target_url}")

        options = self._try_build_options(urls_override=[target_url])
        if options is None:
            return
        self._set_previewing_state(True)
        self.progressbar.configure(mode="indeterminate", value=0)
        self.progressbar.start(12)
        self.status_var.set("Fetching preview...")
        self._append_log(f"--- Fetching preview: {target_url} ---")
        self._controller.start_preview(target_url, options)

    def _handle_preview_done(
        self,
        url: str,
        prepared: Optional[Tuple[Source, Result]],
        exc: Optional[Exception],
    ) -> None:
        self._set_previewing_state(False)
        self._controller.preview_interaction = None
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)

        if exc is not None:
            if isinstance(exc, DownloadCancelled):
                self.status_var.set("Preview cancelled")
                return
            self.status_var.set("Preview failed")
            self._append_log(f"[ERROR] {exc}")
            messagebox.showerror("Preview Failed", str(exc), parent=self.root)
            return

        if prepared is None:
            self.status_var.set("Preview failed")
            return

        source, result = prepared
        self.status_var.set(f"Preview ready: {result.title[:60]}")
        self._show_preview_dialog(url, source, result)

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

        dialog = PreviewDialog(
            self.root,
            url,
            source,
            result,
            is_running=self._is_running,
            skip_downloaded=self.skip_downloaded_var.get(),
            series_book_exists=self._series_book_exists,
            on_confirm=self._enqueue_from_preview,
            palette=self._palette,
            on_search_enrichment=_search_enrichment,
            on_apply_enrichment=_apply_enrichment,
        )
        self._preview_dialog = dialog
        return dialog

    def _series_book_exists(self, book: Union[BookId[Any], Audiobook]) -> bool:
        """Return True when `book` already appears on disk at the configured output path."""
        if not isinstance(book, Audiobook):
            return False
        try:
            opts = self.build_options()
            template = opts.output_template or "{title}"
            target = output.gen_output_location(template, book.metadata, opts.remove_chars)
            if os.path.exists(target):
                return True
            if opts.output_format and os.path.exists(f"{target}.{opts.output_format}"):
                return True
            parent = os.path.dirname(target) or "."
            base = os.path.basename(target)
            if os.path.isdir(parent):
                for entry in os.listdir(parent):
                    stem, _ext = os.path.splitext(entry)
                    if stem == base:
                        return True
        except (OSError, ValueError, KeyError):
            return False
        return False

    def _enqueue_from_preview(
        self,
        url: str,
        prefetched: Tuple[Source, Result],
        selected_indices: Optional[List[int]],
    ) -> None:
        """Start or queue a download from an inspected preview result."""
        options = self._try_build_options(urls_override=[url])
        if options is None:
            return
        if not self._ensure_ffmpeg_available(options):
            return
        if self._is_running:
            enqueued = self._controller.enqueue_from_preview(url, options, prefetched, selected_indices)
            if enqueued is not None:
                added_job, new_total = enqueued
                self._register_queue_job(added_job)
                self._ui_item_total = new_total
                self._refresh_batch_label()
                self.status_var.set(f"Queued preview item (total {new_total})")
                return

        self._save_run_preferences(options)
        self._start_new_run([(url, self.build_options(urls_override=[url]), selected_indices, prefetched)])

    def cancel_download(self) -> None:
        if not self._is_running:
            return
        self.status_var.set("Cancelling...")
        self.cancel_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self._controller.cancel_active_run()

    def _poll_queue(self) -> None:
        processed = self._controller.drain_events(
            self._handle_event,
            max_batch=self._controller.MAX_QUEUE_BATCH,
        )
        if self._is_running:
            self._update_metrics_display()
        delay_ms = 10 if processed >= self._controller.MAX_QUEUE_BATCH else 50
        self.root.after(delay_ms, self._poll_queue)

    def _handle_event(self, item: tuple) -> None:
        kind = item[0]
        if kind == "log":
            _, level, message = item
            prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
            self._append_log(f"{prefix}{message}")
            if level == "INFO" and message.strip():
                self.status_var.set(message.strip().splitlines()[0][:80])
        elif kind == "batch_item":
            _, run_id, item_index, item_total, _url = item
            if run_id == self._current_ui_run_id:
                self._ui_item_index = item_index
                self._ui_item_total = max(self._ui_item_total, item_total)
                self._ui_book_index = 0
                self._ui_book_total = 0
                self._refresh_batch_label()
        elif kind == "job_status":
            _, job_id, status_text, detail = item
            self._update_queue_row(job_id, status_text, detail)
        elif kind == "item_progress":
            _, run_id, book_index, book_total, _title = item
            if run_id == self._current_ui_run_id:
                self._ui_book_index = book_index
                self._ui_book_total = book_total
                self._refresh_batch_label()
        elif kind == "progress_start":
            _, description, _total = item
            self.progressbar.stop()
            self.progressbar.configure(mode="determinate", value=0)
            self._last_pct = 0.0
            self._last_speed_bps = 0.0
            self._last_eta_seconds = None
            self._update_metrics_display(force=True)
            self.status_var.set(description[:80])
        elif kind == "progress_advance":
            _, completed, total = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            clamped = min(100.0, max(0.0, pct))
            self._last_pct = clamped
            self.progressbar.configure(value=clamped)
        elif kind == "progress_metrics":
            _, completed, total, _bytes_downloaded, speed_bps, _elapsed, eta_seconds = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            self._last_pct = min(100.0, max(0.0, pct))
            self._last_speed_bps = speed_bps
            self._last_eta_seconds = eta_seconds
            self._update_metrics_display(force=True)
        elif kind == "progress_finish":
            self._last_pct = 100.0
            self._last_eta_seconds = 0.0
            self.progressbar.configure(value=100.0)
            self._update_metrics_display(force=True)
        elif kind == "prompt_input":
            _, prompt, hidden, holder, done = item
            holder["value"] = simpledialog.askstring(
                "Authentication Required",
                f"{prompt}:",
                show="*" if hidden else None,
                parent=self.root,
            )
            done.set()
        elif kind == "prompt_confirm":
            _, message, holder, done = item
            holder["value"] = messagebox.askyesno("Confirm", message, parent=self.root)
            done.set()
        elif kind == "prompt_choice":
            _, choice_title, choices, default_idx, holder, done = item
            dlg = ChoiceDialog(
                self.root,
                choice_title,
                choices,
                default=default_idx,
                palette=self._palette,
            )
            self.root.wait_window(dlg)
            holder["value"] = dlg.selected_index
            done.set()
        elif kind == "preview_done":
            _, preview_url, prepared, preview_exc = item
            self._handle_preview_done(preview_url, prepared, preview_exc)
        elif kind == "auth_verified":
            _, service_label, ok, auth_exc = item
            self._is_verifying_auth = False
            self._update_service_field_states()
            if auth_exc is not None:
                self.status_var.set(f"Login failed for {service_label}")
                self._append_log(f"[ERROR] Login check failed for {service_label}: {auth_exc}")
                messagebox.showerror("Test Login Failed", str(auth_exc), parent=self.root)
            elif ok:
                self.status_var.set(f"Login OK: {service_label}")
                self._append_log(f"Verified login for {service_label}.")
                messagebox.showinfo("Test Login", f"Authentication succeeded for {service_label}.", parent=self.root)
            else:
                self.status_var.set(f"Login invalid: {service_label}")
                messagebox.showwarning("Test Login", f"Authentication failed for {service_label}.", parent=self.root)
        elif kind == "catalog_loaded":
            _, req_id, dialog, books, cat_exc = item
            if req_id != self._controller.catalog_request_id:
                return
            catalog_dialog: CatalogBrowserDialog = dialog
            try:
                if not catalog_dialog.winfo_exists():
                    return
            except tk.TclError:
                return
            if cat_exc is not None:
                catalog_dialog.set_error_status("Error loading items")
                messagebox.showerror("Catalog Error", str(cat_exc), parent=catalog_dialog)
            else:
                catalog_dialog.populate_items(books)
        elif kind == "ffmpeg_install_done":
            _, ok, detail = item
            self._update_ffmpeg_status()
            if ok:
                self.status_var.set("FFmpeg installed")
                self._append_log(detail)
                messagebox.showinfo("FFmpeg Installed", detail, parent=self.root)
            else:
                self.status_var.set("FFmpeg install failed")
                self._append_log(f"[ERROR] {detail}")
                messagebox.showerror("FFmpeg Install Failed", detail, parent=self.root)
        elif kind == "update_checked":
            _, info, interactive = item
            self._handle_update_checked(info, interactive)
        elif kind == "done":
            _, run_id, summary, exc = item
            if run_id == self._current_ui_run_id:
                self._finish_run(summary, exc)

    def _handle_update_checked(self, info: Optional[UpdateInfo], interactive: bool) -> None:
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
            self._append_log(f"Update available: v{info.latest_version} ({info.release_url})")
            if interactive:
                messagebox.showinfo("Update Available", msg, parent=self.root)
        elif interactive:
            self.status_var.set(f"Up to date (v{info.current_version})")
            messagebox.showinfo(
                "Up to Date",
                f"You are running the latest version (v{info.current_version}).",
                parent=self.root,
            )

    def _notify_run_finished(self, summary: Optional[DownloadSummary], exc: Optional[Exception]) -> None:
        if not self.notify_on_complete_var.get():
            return
        payload = format_completion_notification(summary, exc)
        if payload is not None:
            send_os_notification_async(payload[0], payload[1])

    def _finish_run(self, summary: Optional[DownloadSummary], exc: Optional[Exception]) -> None:
        self._set_running_state(False)
        self.progressbar.stop()
        self._controller.clear_seen_urls()
        final_elapsed = (
            format_duration(max(0.0, time.monotonic() - self._run_start_time))
            if self._run_start_time is not None
            else None
        )
        self._run_start_time = None
        self.batch_var.set("")
        if exc is not None:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Error")
            self._append_log(f"[ERROR] {exc}")
            self._notify_run_finished(summary, exc)
            messagebox.showerror("Download Failed", str(exc), parent=self.root)
            return

        if summary is None or summary.cancelled:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Cancelled")
            return

        if summary.failed > 0 and summary.completed == 0:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Failed")
        elif summary.failed > 0:
            self.progressbar.configure(value=100)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
        else:
            self.progressbar.configure(value=100)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set("Completed")
            self._append_log("--- Completed ---")
        self._notify_run_finished(summary, None)

    def _on_close(self) -> None:
        try:
            geom = self.root.geometry()
            self._controller.save_window_preferences(
                window_geometry=geom if is_valid_geometry(geom) else None,
                output_dir=self.output_dir_var.get().strip() or None,
                output_template=self.template_var.get().strip() or None,
                use_download_archive=self.download_archive_var.get(),
                theme=self._theme_mode,
                notify_on_complete=self.notify_on_complete_var.get(),
                output_prefs=self._read_persisted_gui_preferences(),
            )
        except OSError:
            pass
        self._controller.shutdown(
            is_running=self._is_running,
            is_previewing=self._is_previewing,
        )
        self.root.destroy()


def _enable_high_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows when supported by the OS."""
    enable_high_dpi_awareness()


def parse_gui_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments supported by the desktop GUI launcher."""
    parser = argparse.ArgumentParser(prog="audiobook-dl-gui")
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


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Launch the audiobook-dl desktop GUI."""
    parsed = parse_gui_args(argv)
    if parsed.self_test:
        run_self_test(check_gui=True)
        return
    _enable_high_dpi_awareness()
    root = tk.Tk()
    app = AudiobookDLApp(root, config_location=parsed.config_location)
    app._schedule_update_check()
    root.mainloop()


if __name__ == "__main__":
    main()

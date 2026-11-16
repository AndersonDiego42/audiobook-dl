"""Main Tkinter desktop window coordinator and launcher entry point for audiobook-dl."""

import argparse
from collections import deque
import dataclasses
import importlib.resources
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any
from collections.abc import Sequence

from audiobookdl import __version__, args, output
from audiobookdl.config import (
    DEFAULT_PROFILE,
    is_portable_mode,
    list_source_profiles,
    normalize_profile_name,
)
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.archive_dialog import ArchiveLedgerDialog
from audiobookdl.gui.auth_panel import ServiceAuthPanel
from audiobookdl.gui.catalog_dialog import CatalogBrowserDialog
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
from audiobookdl.gui.formatting import (
    PRESET_LABEL_TO_TEMPLATE,
    SAMPLE_PREVIEW_METADATA,
    TEMPLATE_PRESET_LABELS,
    TEMPLATE_TO_PRESET_LABEL,
    default_output_dir,
    is_valid_geometry,
    parse_url_text,
)
from audiobookdl.gui.options_panel import OptionsPanel
from audiobookdl.gui.pack_dialog import PackDialog
from audiobookdl.gui.preview_dialog import ChoiceDialog, PreviewDialog
from audiobookdl.gui.queue_panel import ActivityQueuePanel
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
    attach_url_context_menu,
    bind_text_tab_navigation,
)
from audiobookdl.options_schema import GUI_OPTION_SPECS, PERSISTED_GUI_OPTION_SPECS
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources import ServiceInfo, get_available_services
from audiobookdl.sources.source import Source
from audiobookdl.utils import read_asset_file
from audiobookdl.utils.archive import default_archive_path
from audiobookdl.utils.audiobook import Audiobook, BookId, Result, Series
from audiobookdl.utils.updater import UpdateInfo


class AudiobookDLApp:
    """Main Tkinter desktop window coordinator for audiobook-dl."""

    MAX_LOG_LINES = ActivityQueuePanel.MAX_LOG_LINES

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

    def __init__(self, root: tk.Tk, config_location: str | None = None) -> None:
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

        self._services: list[ServiceInfo] = get_available_services()
        self._is_running = False
        self._is_previewing = False
        self._preview_dialog: PreviewDialog | None = None
        self._output_widgets: list[ttk.Entry | ttk.Button | ttk.Combobox] = []

        self._init_variables()
        self._ffmpeg_assistant = FFmpegAssistant(
            self.root,
            self._controller,
            status_var=self.status_var,
            ffmpeg_status_var=self.ffmpeg_status_var,
            on_log=lambda msg: self._queue_panel.append_log(msg),
        )
        self._build_menu_bar()
        self._build_ui()
        apply_theme_to_widget_tree(self.root, self._palette)
        self._auth_panel.load_service_into_form()
        self._ffmpeg_assistant.update_status()
        self._queue_panel.restore_saved_queue_history()
        self._set_running_state(False)

        self._poll_after_id: str | None = None
        self._modal_prompt_active = False
        self._deferred_modal_events: deque[tuple] = deque()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._poll_after_id = self.root.after(50, self._poll_queue)

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
        ttk.Label(header, text=title_text, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ffmpeg_box = self._ffmpeg_assistant.build_controls(header)
        ffmpeg_box.grid(row=0, column=1, sticky="e")
        assert self._ffmpeg_assistant.locate_btn is not None
        self.ffmpeg_locate_btn = self._ffmpeg_assistant.locate_btn

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

        ttk.Label(out_frame, text="Template:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )
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
        self._output_widgets = [out_entry, out_browse, tpl_entry, self.preset_combo]

        # Service & Authentication
        self._auth_panel = ServiceAuthPanel(
            container,
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
            on_log=lambda msg: self._queue_panel.append_log(msg),
        )
        self._auth_panel.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        self.service_combo = self._auth_panel.service_combo
        self.user_entry = self._auth_panel.user_entry

        # Options
        self._options_panel = OptionsPanel(
            container,
            self,
            on_m4b_toggled=self._on_m4b_toggled,
            on_format_selected=self._on_format_selected,
        )
        self._options_panel.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.options_notebook = self._options_panel.options_notebook

        # Actions, Progress & Queue
        self._queue_panel = ActivityQueuePanel(
            container,
            self.root,
            self._controller,
            status_var=self.status_var,
            batch_var=self.batch_var,
            metrics_var=self.metrics_var,
            on_start_download=self.start_download,
            on_cancel_download=self.cancel_download,
            on_preview_metadata=self.preview_metadata,
            on_pack_local=self._show_pack_dialog,
            on_open_folder=self._open_output_folder,
            on_retry_failed=self._retry_failed_queue_items,
            service_label_for_url=self._auth_panel.service_label_for_url,
        )
        self._queue_panel.grid(row=5, column=0, sticky="nsew")
        self.download_btn = self._queue_panel.download_btn
        self.pause_btn = self._queue_panel.pause_btn
        self.cancel_btn = self._queue_panel.cancel_btn
        self.preview_btn = self._queue_panel.preview_btn
        self.pack_btn = self._queue_panel.pack_btn
        self.progressbar = self._queue_panel.progressbar
        self.log_text = self._queue_panel.log_text
        self.queue_tree = self._queue_panel.queue_tree

        # Optional drag-and-drop URL support when tkinterdnd2 is available on host Tk.
        if hasattr(self.urls_text, "drop_target_register") and hasattr(self.urls_text, "dnd_bind"):
            try:
                self.urls_text.drop_target_register("DND_Text")  # type: ignore[attr-defined]
                self.urls_text.dnd_bind("<<Drop>>", self._on_url_drop)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _build_menu_bar(self) -> None:
        menubar = tk.Menu(self.root)
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
            command=lambda: self._auth_panel.show_cookie_import_dialog(),
        )
        tools_menu.add_command(
            label="Browse Library / Search...", command=self._show_catalog_browser_dialog
        )
        tools_menu.add_command(label="Pack Local Audiobooks...", command=self._show_pack_dialog)
        tools_menu.add_command(label="Locate FFmpeg Folder...", command=self.browse_ffmpeg_dir)
        tools_menu.add_command(
            label="Reset FFmpeg Folder to System PATH", command=self.reset_ffmpeg_dir
        )
        tools_menu.add_command(
            label="Library & Download Ledger...", command=self._show_archive_ledger_dialog
        )
        tools_menu.add_command(
            label="Audiobookshelf Integration...", command=self._show_audiobookshelf_dialog
        )
        tools_menu.add_separator()
        tools_menu.add_command(label="Retry Failed Jobs", command=self._retry_failed_queue_items)
        tools_menu.add_command(
            label="Clear Completed History",
            command=lambda: self._queue_panel.clear_completed_history(),
        )
        tools_menu.add_command(label="Clear Log", command=lambda: self._queue_panel.clear_log())
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(
            label="FFmpeg Setup Guide", command=lambda: self._ffmpeg_assistant.show_guidance()
        )
        if sys.platform.startswith("win"):
            help_menu.add_command(
                label="Install FFmpeg via winget...",
                command=lambda: self._ffmpeg_assistant.install_via_winget(),
            )
        help_menu.add_command(label="Check for Updates...", command=self.check_updates_interactive)
        help_menu.add_separator()
        help_menu.add_command(label="About audiobook-dl", command=self._show_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self._browse_url_file())
        self.root.bind("<Control-q>", lambda _e: self._on_close())

    def browse_ffmpeg_dir(self) -> bool:
        """Let the user choose a folder containing `ffmpeg` and persist it in config."""
        return self._ffmpeg_assistant.browse_dir()

    def reset_ffmpeg_dir(self) -> None:
        """Clear any custom `ffmpeg_dir` setting and fall back to system PATH."""
        self._ffmpeg_assistant.reset_dir()

    def save_current_credentials(self, quiet: bool = False) -> None:
        """Persist the selected service's login, password, library, and cookie path."""
        self._auth_panel.save_current_credentials(quiet=quiet)

    def forget_current_credentials(self) -> None:
        """Remove saved credentials for the currently selected service and profile."""
        self._auth_panel.forget_current_credentials()

    def verify_current_auth(self) -> None:
        """Test authentication for the currently selected service in the background."""
        self._auth_panel.verify_current_auth()

    def toggle_pause_queue(self) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        self._queue_panel.toggle_pause(is_running=self._is_running)

    def _on_url_drop(self, event: Any) -> None:
        dropped = getattr(event, "data", "")
        if not dropped:
            return
        urls = parse_url_text(str(dropped).strip("{}"))
        if urls:
            self._append_urls_to_input(urls)

    def _append_urls_to_input(self, urls: Sequence[str]) -> list[str]:
        existing = parse_url_text(self.urls_text.get("1.0", tk.END))
        existing_set = set(existing)
        added = [u for u in urls if u not in existing_set]
        if added:
            combined = "\n".join([*existing, *added])
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()
        return added

    def _on_urls_changed(self, _event: tk.Event | None = None) -> None:
        self._auth_panel.on_urls_changed(parse_url_text(self.urls_text.get("1.0", tk.END)))

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
            self._auth_panel.load_service_into_form()
            self._auth_panel.update_field_states()
            self._ffmpeg_assistant.update_status()
            self.status_var.set("Config reloaded")
            self._queue_panel.append_log("Reloaded configuration.")
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
        def _on_queue_urls(urls_to_add: list[str]) -> None:
            added = self._append_urls_to_input(urls_to_add)
            self.status_var.set(
                f"Queued {len(added)} URL{'s' if len(added) != 1 else ''} from library"
            )

        return ArchiveLedgerDialog(
            self.root,
            self._archive_ledger_path(),
            on_queue_urls=_on_queue_urls,
            palette=self._palette,
        )

    def _show_audiobookshelf_dialog(self):
        from audiobookdl.config import load_config
        from audiobookdl.gui.abs_dialog import AudiobookshelfDialog

        cfg = self._controller.config_snapshot()
        config_loc = self._controller.config_location

        def _on_saved(_updated) -> None:
            self._controller.config = load_config(config_loc)
            self.status_var.set("Saved Audiobookshelf configuration.")

        return AudiobookshelfDialog(
            self.root,
            cfg.audiobookshelf,
            config_location=config_loc,
            on_saved=_on_saved,
            palette=self._palette,
        )

    def _show_catalog_browser_dialog(self) -> CatalogBrowserDialog:
        def _on_fetch(mode: str, svc: ServiceInfo, query_text: str, dialog: CatalogBrowserDialog) -> None:
            options = self._try_build_options(urls_override=[], parent=dialog)
            if options is None:
                dialog.set_error_status("Invalid option value")
                return
            chosen_profile = normalize_profile_name(dialog.selected_profile)
            options.profile = None if chosen_profile == DEFAULT_PROFILE else chosen_profile
            self._controller.start_catalog_fetch(mode, svc.key, query_text, options, dialog)

        def _on_add_urls(urls_to_add: list[str]) -> None:
            added = self._append_urls_to_input(urls_to_add)
            self.status_var.set(
                f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from catalog"
            )

        def _profile_resolver(service_key: str) -> list[str]:
            return list_source_profiles(self._controller.config_snapshot(), service_key)

        def _on_save_to_library(svc_key: str, prof: str, books: Sequence[Any]) -> tuple[int, int]:
            from audiobookdl.utils.archive import DownloadArchive

            archive = DownloadArchive(self._archive_ledger_path())
            inserted, updated = archive.upsert_catalog_items(
                svc_key,
                list(books),
                profile=normalize_profile_name(prof),
            )
            self.status_var.set(f"Library DB: +{inserted} new, {updated} refreshed")
            return inserted, updated

        return CatalogBrowserDialog(
            self.root,
            self._services,
            self._auth_panel.selected_service(),
            on_fetch=_on_fetch,
            on_add_urls=_on_add_urls,
            on_save_to_library=_on_save_to_library,
            profile_resolver=_profile_resolver,
            selected_profile=normalize_profile_name(self.profile_var.get()),
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

    def _schedule_update_check(self) -> None:
        """Run a quiet background update check at launch unless opted out."""
        if self._controller.config.check_updates is False:
            return
        self._controller.start_update_check(interactive=False, respect_opt_out=True)

    def check_updates_interactive(self) -> None:
        """Check for newer releases from the Help menu."""
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
            existing = self.urls_text.get("1.0", tk.END).strip()
            combined = f"{existing}\n{content}" if existing else content
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()

    def _clear_urls(self) -> None:
        self.urls_text.delete("1.0", tk.END)
        self._on_urls_changed()

    def _paste_urls_from_clipboard(self) -> list[str]:
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

    def _open_output_folder(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        if not os.path.isdir(folder):
            messagebox.showinfo(
                "Folder Missing", f"Folder does not exist yet:\n{folder}", parent=self.root
            )
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

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        for widget in self._output_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state="disabled" if running else "normal")
        self._options_panel.set_enabled(not running)
        self._auth_panel.set_running_state(running)
        self._queue_panel.set_running_state(running, is_previewing=self._is_previewing)

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

        urls = (
            list(urls_override)
            if urls_override is not None
            else parse_url_text(self.urls_text.get("1.0", tk.END))
        )
        auth_kwargs = self._auth_panel.extract_options_auth(urls)
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
    ) -> bool:
        """Append new URLs (or retried URLs when `allow_existing=True`) to the active queue."""
        if not self._auth_panel.validate_cookie_file():
            return True
        if self._try_build_options(urls_override=[]) is None:
            return True
        has_active_run, added_jobs, new_total = self._controller.enqueue_additional_urls(
            urls,
            lambda u: self.build_options(urls_override=[u]),
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

    def _retry_failed_queue_items(self) -> None:
        failed_urls = self._controller.retryable_urls(self.queue_tree.get_children())
        if not failed_urls:
            self.status_var.set("No failed or cancelled jobs to retry")
            return
        if self._is_running:
            if not self._controller.cancel_event.is_set():
                self._enqueue_additional_urls(failed_urls, allow_existing=True)
            return
        options = self._try_build_options()
        if options is None:
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            return
        self._save_run_preferences(options)
        unique_urls = list(dict.fromkeys(failed_urls))
        job_specs = [
            (u, self.build_options(urls_override=[u]), None, None)
            for u in unique_urls
        ]
        self._start_new_run(job_specs)

    def _show_pack_dialog(self) -> None:
        """Open the modal Local Audiobook Repackager dialog."""
        options = self._try_build_options(urls_override=[])
        if options is None:
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            return
        output_dir = self.output_dir_var.get().strip() or default_output_dir()
        PackDialog(
            self.root,
            self._controller,
            options,
            output_dir=output_dir,
            on_submit=self._on_pack_submitted,
            palette=self._palette,
        )

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
            opts = dataclasses.replace(base_opts, output_format=output_format)
            opts._explicit_fields = frozenset(base_opts._explicit_fields | {"output_format"})
            pairs.append((req, opts))

        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_pack_jobs(
            pairs
        )
        if was_appended:
            for job in queued_jobs:
                self._queue_panel.register_queue_job(job, persist=False)
            if queued_jobs:
                self._controller.save_queue_state()
            self._queue_panel.ui_item_total = new_total
            self._queue_panel.refresh_batch_label()
            self.status_var.set(f"Queued +{len(queued_jobs)} pack job(s) (total {new_total})")
            self._queue_panel.append_log(
                f"--- Added {len(queued_jobs)} local pack job(s) to queue ---"
            )
            return

        self._set_running_state(True)
        self._queue_panel.prepare_run_ui_state(
            run_id,
            len(queued_jobs),
            f"--- Starting local pack ({len(queued_jobs)} book{'s' if len(queued_jobs) != 1 else ''}) ---",
        )
        for job in queued_jobs:
            self._queue_panel.register_queue_job(job, persist=False)
        if queued_jobs:
            self._controller.save_queue_state()

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

        if not self._auth_panel.validate_cookie_file():
            return
        if not self._auth_panel.warn_mixed_service_credentials(urls):
            return

        options = self._try_build_options()
        if options is None:
            return
        if not self._ffmpeg_assistant.ensure_available(options):
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
        self._queue_panel.set_previewing_state(previewing, is_running=self._is_running)

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
        if not self._auth_panel.validate_cookie_file():
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
        self.progressbar.configure(mode="indeterminate", value=0)
        self.progressbar.start(12)
        self.status_var.set("Fetching preview...")
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
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)

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
        dialog = PreviewDialog(
            self.root,
            url,
            source,
            result,
            is_running=self._is_running,
            skip_downloaded=self.skip_downloaded_var.get(),
            series_book_exists=lambda book: self._series_book_exists(book, options=cached_series_opts),
            on_confirm=self._enqueue_from_preview,
            palette=self._palette,
            on_search_enrichment=_search_enrichment,
            on_apply_enrichment=_apply_enrichment,
        )
        self._preview_dialog = dialog
        return dialog

    def _series_book_exists(
        self,
        book: BookId[Any] | Audiobook,
        *,
        options: args.DownloadOptions | None = None,
    ) -> bool:
        """Return True when `book` already appears on disk at the configured output path."""
        if not isinstance(book, Audiobook):
            return False
        try:
            opts = options if options is not None else self.build_options()
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
            self._queue_panel.append_log("Cancelling metadata preview...")
            self._controller.cancel_preview()
            return
        if not self._is_running:
            return
        self.status_var.set("Cancelling...")
        self.cancel_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self._controller.cancel_active_run()

    def _poll_queue(self) -> None:
        self._poll_after_id = None
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        # Schedule the next poll before draining so nested modal dialogs
        # (`askstring`, `askyesno`, `wait_window`) do not freeze log/progress updates.
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
            self._queue_panel.update_metrics_display(is_running=True)
        if processed >= self._controller.MAX_QUEUE_BATCH and self._poll_after_id is not None:
            self._cancel_poll_timer()
            try:
                if self.root.winfo_exists():
                    self._poll_after_id = self.root.after(10, self._poll_queue)
            except tk.TclError:
                self._poll_after_id = None

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

    def _handle_event(self, item: tuple) -> None:
        if self._queue_panel.handle_activity_event(item):
            return
        kind = item[0]
        if kind in ("prompt_input", "prompt_confirm", "prompt_choice"):
            if self._modal_prompt_active:
                self._deferred_modal_events.append(item)
                return
            self._modal_prompt_active = True
            try:
                if kind == "prompt_input":
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
            finally:
                self._modal_prompt_active = False
        elif kind == "preview_done":
            _, preview_url, prepared, preview_exc = item
            self._handle_preview_done(preview_url, prepared, preview_exc)
        elif kind == "auth_verified":
            _, service_label, ok, auth_exc = item
            self._auth_panel.handle_auth_verified(service_label, ok, auth_exc)
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
            self._ffmpeg_assistant.handle_install_done(ok, detail)
        elif kind == "update_checked":
            _, info, interactive = item
            self._handle_update_checked(info, interactive)
        elif kind == "done":
            _, run_id, summary, exc = item
            if run_id == self._queue_panel.current_ui_run_id:
                self._finish_run(summary, exc)

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
            self._queue_panel.append_log(
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
        self._cancel_poll_timer()
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
    root = tk.Tk()
    app = AudiobookDLApp(root, config_location=parsed.config_location)
    app._schedule_update_check()
    root.mainloop()


if __name__ == "__main__":
    main()

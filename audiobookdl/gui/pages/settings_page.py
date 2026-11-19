"""Settings page for theme, notifications, clipboard monitor, FFmpeg, Audiobookshelf, and updates."""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable, Mapping

from audiobookdl import __version__
from audiobookdl.config import (
    AudiobookshelfConfig,
    CustomMetadataProviderConfig,
    get_config_location,
    save_audiobookshelf_config,
)
from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
from audiobookdl.gui.window_manager import open_path_in_os
from audiobookdl.gui.theme import (
    LIGHT_PALETTE,
    THEME_LABELS,
    THEME_MODES,
    ThemePalette,
    make_bold_font,
    scale_px,
)
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ModalDialog,
    ScrollableFrame,
    ScrolledTreeview,
    TreeColumnSpec,
    bind_adaptive_wraplength,
    tree_item_values,
)
from audiobookdl.integrations.audiobookshelf import AudiobookshelfClient
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT, create_configured_http_session
from audiobookdl.utils.enrichment import (
    normalize_custom_provider_name,
    test_custom_metadata_provider,
    validate_custom_provider_url,
)


class _CustomProviderDialog(ModalDialog):
    """Modal dialog for creating or editing a custom ABS-spec metadata provider."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        existing: CustomMetadataProviderConfig | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        title = (
            f"Edit Provider \u2014 {existing.name}"
            if existing is not None
            else "Add Custom Metadata Provider"
        )
        super().__init__(
            parent,
            title,
            palette=palette,
            default_size=(480, 250),
            min_size=(420, 220),
            resizable=(True, True),
        )
        self.result: tuple[str, str, str | None, bool] | None = None
        self._is_edit = existing is not None

        self._name_var = tk.StringVar(value=existing.name if existing is not None else "")
        self._url_var = tk.StringVar(value=existing.url if existing is not None else "")
        self._key_var = tk.StringVar(value=(existing.api_key or "") if existing is not None else "")
        self._show_key_var = tk.BooleanVar(value=False)
        self._enabled_var = tk.BooleanVar(value=existing.enabled if existing is not None else True)

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Name:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self._name_entry = ttk.Entry(body, textvariable=self._name_var)
        self._name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        if self._is_edit:
            self._name_entry.configure(state="disabled")

        ttk.Label(body, text="Base URL:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self._url_entry = ttk.Entry(body, textvariable=self._url_var)
        self._url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(body, text="API Key (optional):").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self._key_entry = ttk.Entry(body, textvariable=self._key_var, show="*")
        self._key_entry.grid(row=2, column=1, sticky="ew", pady=4, padx=(0, 6))
        ttk.Checkbutton(
            body,
            text="Show",
            variable=self._show_key_var,
            command=self._toggle_key_visibility,
        ).grid(row=2, column=2, sticky="e", pady=4)

        ttk.Checkbutton(
            body,
            text="Enabled for metadata enrichment",
            variable=self._enabled_var,
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 6))

        ttk.Label(
            body,
            text="Implements Audiobookshelf custom provider API: GET <Base URL>/search?query=...&author=...",
            style="Muted.TLabel",
            wraplength=scale_px(self, 440),
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))

        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=3, sticky="ew")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save, style="Accent.TButton").pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        self.bind("<Return>", lambda _e: self._save())
        self.finalize_modal(
            initial_focus=self._url_entry if self._is_edit else self._name_entry
        )

    def _toggle_key_visibility(self) -> None:
        self._key_entry.configure(show="" if self._show_key_var.get() else "*")

    def _save(self) -> None:
        try:
            slug = normalize_custom_provider_name(self._name_var.get())
            clean_url = validate_custom_provider_url(self._url_var.get())
        except ValueError as exc:
            messagebox.showwarning("Invalid Custom Provider", str(exc), parent=self)
            return
        api_key = self._key_var.get().strip()
        self.result = (slug, clean_url, api_key, bool(self._enabled_var.get()))
        self.destroy()


class SettingsPage(ttk.Frame):
    """App-level preferences, FFmpeg setup, Audiobookshelf integration, and updates."""

    _CUSTOM_PROVIDER_COLUMNS = (
        TreeColumnSpec("name", "Name", width=110, stretch=False),
        TreeColumnSpec("url", "Base URL", width=230, stretch=True),
        TreeColumnSpec("auth", "API Key", width=85, stretch=False, anchor="center"),
        TreeColumnSpec("enabled", "Enabled", width=70, stretch=False, anchor="center"),
        TreeColumnSpec("status", "Status", width=165, stretch=True),
    )

    def __init__(
        self,
        parent: tk.Misc,
        *,
        theme_var: tk.StringVar,
        notify_on_complete_var: tk.BooleanVar,
        clipboard_monitor_var: tk.BooleanVar,
        check_updates_var: tk.BooleanVar | None = None,
        minimize_to_tray_var: tk.BooleanVar | None = None,
        tray_available: bool = False,
        on_toggle_minimize_to_tray: Callable[[], None] | None = None,
        on_theme_changed: Callable[[str], object],
        on_toggle_notify_on_complete: Callable[[], None] | None = None,
        on_toggle_clipboard_monitor: Callable[[], None],
        on_toggle_check_updates: Callable[[], None] | None = None,
        on_reload_config: Callable[[], None],
        on_check_updates: Callable[[], None],
        ffmpeg_assistant: FFmpegAssistant,
        abs_config_provider: Callable[[], AudiobookshelfConfig],
        config_location: str | None = None,
        on_save_abs_config: Callable[..., object] | None = None,
        on_abs_saved: Callable[[AudiobookshelfConfig], None] | None = None,
        custom_providers_provider: Callable[[], Mapping[str, CustomMetadataProviderConfig]] | None = None,
        on_save_custom_provider: Callable[..., object] | None = None,
        on_delete_custom_provider: Callable[[str], object] | None = None,
        http_proxy_provider: Callable[[], str | None] | None = None,
        http_timeout_provider: Callable[[], float | None] | None = None,
        palette: ThemePalette | None = None,
        style: ttk.Style | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._config_location = config_location
        self._on_save_abs_config = on_save_abs_config
        self._on_abs_saved = on_abs_saved
        self._abs_config_provider = abs_config_provider
        self._loading_abs = False
        self._abs_dirty = False
        self._custom_providers_provider = custom_providers_provider
        self._on_save_custom_provider = on_save_custom_provider
        self._on_delete_custom_provider = on_delete_custom_provider
        self._http_proxy_provider = http_proxy_provider
        self._http_timeout_provider = http_timeout_provider
        self._ffmpeg = ffmpeg_assistant
        self._palette = palette or LIGHT_PALETTE
        self._libraries: list[tuple[str, str]] = []
        self._testing_custom_provider = False
        self._cb_queue = MainThreadCallbackQueue(self)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, scale_px(self, 8)))
        ttk.Label(header, text="Settings", font=make_bold_font(self, 4)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Application preferences, FFmpeg tools, Audiobookshelf server, and updates.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        self._scroll = ScrollableFrame(
            self,
            style=style,
            content_padding=(scale_px(self, 2), scale_px(self, 2)),
            inner_scrollables=lambda: (self._custom_tree,),
        )
        self._scroll.grid(row=1, column=0, sticky="nsew")
        body = self._scroll.content_frame
        body.columnconfigure(0, weight=1)

        # 1. Appearance & Behavior
        app_frame = ttk.LabelFrame(body, text="Appearance & Behavior", padding=scale_px(self, 10))
        app_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        app_frame.columnconfigure(1, weight=1)

        ttk.Label(app_frame, text="Theme:").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=2)
        theme_row = ttk.Frame(app_frame)
        theme_row.grid(row=0, column=1, sticky="w", pady=2)
        for mode in THEME_MODES:
            def _make_theme_cb(selected_mode: str) -> Callable[[], None]:
                def _cb() -> None:
                    on_theme_changed(selected_mode)

                return _cb

            ttk.Radiobutton(
                theme_row,
                text=THEME_LABELS[mode],
                variable=theme_var,
                value=mode,
                command=_make_theme_cb(mode),
            ).pack(side=tk.LEFT, padx=(0, 12))

        notify_kwargs: dict[str, object] = {
            "text": "Send OS notification when a download run finishes",
            "variable": notify_on_complete_var,
        }
        if on_toggle_notify_on_complete is not None:
            notify_kwargs["command"] = on_toggle_notify_on_complete
        ttk.Checkbutton(app_frame, **notify_kwargs).grid(  # type: ignore[arg-type]
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 2)
        )

        ttk.Checkbutton(
            app_frame,
            text="Monitor clipboard for audiobook URLs",
            variable=clipboard_monitor_var,
            command=on_toggle_clipboard_monitor,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

        tray_var = minimize_to_tray_var if minimize_to_tray_var is not None else tk.BooleanVar(value=False)
        tray_label = (
            "Minimize to system tray when closing window (keeps scheduled sync active)"
            if tray_available
            else "Minimize to system tray when closing window (install 'pystray' to enable)"
        )
        tray_kwargs: dict[str, object] = {
            "text": tray_label,
            "variable": tray_var,
            "state": "normal" if tray_available else "disabled",
        }
        if on_toggle_minimize_to_tray is not None:
            tray_kwargs["command"] = on_toggle_minimize_to_tray
        ttk.Checkbutton(app_frame, **tray_kwargs).grid(  # type: ignore[arg-type]
            row=3, column=0, columnspan=2, sticky="w", pady=2
        )

        # 2. FFmpeg & Media Tools
        ffmpeg_frame = ttk.LabelFrame(body, text="FFmpeg & FFprobe", padding=scale_px(self, 10))
        ffmpeg_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ffmpeg_frame.columnconfigure(0, weight=1)

        ff_controls = self._ffmpeg.build_controls(ffmpeg_frame)
        ff_controls.grid(row=0, column=0, sticky="w", pady=(0, 8))

        ff_btns = ttk.Frame(ffmpeg_frame)
        ff_btns.grid(row=1, column=0, sticky="w")
        ttk.Button(
            ff_btns,
            text="Reset to System PATH",
            command=self._ffmpeg.reset_dir,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            ff_btns,
            text="Setup Guide...",
            command=self._ffmpeg.show_guidance,
        ).pack(side=tk.LEFT, padx=(0, 6))
        if sys.platform.startswith("win"):
            ttk.Button(
                ff_btns,
                text="Install via winget...",
                command=self._ffmpeg.install_via_winget,
            ).pack(side=tk.LEFT)
        self._ffmpeg.update_status()

        # 3. Audiobookshelf Integration
        abs_frame = ttk.LabelFrame(body, text="Audiobookshelf Integration", padding=scale_px(self, 10))
        abs_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        abs_frame.columnconfigure(1, weight=1)

        current = abs_config_provider()
        self._url_var = tk.StringVar(value=current.url or "")
        self._token_var = tk.StringVar(value=current.api_key or "")
        self._library_var = tk.StringVar(value=current.library_id or "")
        self._auto_upload_var = tk.BooleanVar(value=bool(current.auto_upload))
        self._auto_scan_var = tk.BooleanVar(value=bool(current.auto_scan))
        for abs_var in (
            self._url_var,
            self._token_var,
            self._library_var,
            self._auto_upload_var,
            self._auto_scan_var,
        ):
            abs_var.trace_add("write", lambda *_args: self._mark_abs_dirty())
        self._status_var = tk.StringVar(value="Enter your Audiobookshelf URL and API token.")

        ttk.Label(abs_frame, text="Server URL:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(abs_frame, textvariable=self._url_var, width=42).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(abs_frame, text="API Token:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self._show_token_var = tk.BooleanVar(value=False)
        self._token_entry = ttk.Entry(abs_frame, textvariable=self._token_var, width=42, show="*")
        self._token_entry.grid(row=1, column=1, sticky="ew", pady=4, padx=(0, 6))
        ttk.Checkbutton(
            abs_frame,
            text="Show",
            variable=self._show_token_var,
            command=self._toggle_token_visibility,
        ).grid(row=1, column=2, sticky="e", pady=4)

        ttk.Label(abs_frame, text="Library ID:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self._lib_combo = ttk.Combobox(abs_frame, textvariable=self._library_var, width=28)
        self._lib_combo.grid(row=2, column=1, sticky="ew", pady=4, padx=(0, 6))
        self._test_btn = ttk.Button(
            abs_frame, text="Test & Load Libraries", command=self._test_connection
        )
        self._test_btn.grid(row=2, column=2, sticky="e", pady=4)

        ttk.Checkbutton(
            abs_frame,
            text="Automatically upload completed/packed books to Audiobookshelf",
            variable=self._auto_upload_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Checkbutton(
            abs_frame,
            text="Trigger Audiobookshelf library scan after sync",
            variable=self._auto_scan_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 8))

        ttk.Label(abs_frame, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        self._loading_bar = ttk.Progressbar(abs_frame, mode="indeterminate")
        self._loading_bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self._loading_bar.grid_remove()

        abs_btns = ttk.Frame(abs_frame)
        abs_btns.grid(row=7, column=0, columnspan=3, sticky="ew")
        ttk.Button(
            abs_btns,
            text="Save Audiobookshelf Settings",
            command=self._save,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)

        # 4. Custom Metadata Providers (Audiobookshelf Spec)
        providers_frame = ttk.LabelFrame(
            body, text="Custom Metadata Providers", padding=scale_px(self, 10)
        )
        providers_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        providers_frame.columnconfigure(0, weight=1)

        ttk.Label(
            providers_frame,
            text="Connect self-hosted or community Audiobookshelf-compatible metadata servers for enrichment.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        self._custom_scrolled = ScrolledTreeview(
            providers_frame,
            self._CUSTOM_PROVIDER_COLUMNS,
            selectmode="browse",
            height=4,
            enable_sorting=False,
        )
        self._custom_scrolled.grid(row=1, column=0, sticky="ew")
        self._custom_tree = self._custom_scrolled.tree
        self._custom_tree.bind(
            "<<TreeviewSelect>>", lambda _e: self._sync_custom_provider_buttons(), add="+"
        )
        self._custom_tree.bind("<Double-1>", lambda _e: self._edit_custom_provider())
        self._custom_tree.bind("<Delete>", lambda _e: self._remove_custom_provider())
        self._custom_tree.bind("<Return>", lambda _e: self._edit_custom_provider())
        self._custom_tree.bind("<F2>", lambda _e: self._edit_custom_provider())

        self._custom_status_var = tk.StringVar(value="")
        self._custom_loading_bar = ttk.Progressbar(providers_frame, mode="indeterminate")
        self._custom_loading_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._custom_loading_bar.grid_remove()

        custom_btns = ttk.Frame(providers_frame)
        custom_btns.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(custom_btns, text="Add...", command=self._add_custom_provider).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self._custom_edit_btn = ttk.Button(
            custom_btns, text="Edit...", command=self._edit_custom_provider, state="disabled"
        )
        self._custom_edit_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._custom_toggle_btn = ttk.Button(
            custom_btns,
            text="Toggle Enabled",
            command=self._toggle_custom_provider,
            state="disabled",
        )
        self._custom_toggle_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._custom_test_btn = ttk.Button(
            custom_btns,
            text="Test Connection",
            command=self._test_custom_provider,
            state="disabled",
        )
        self._custom_test_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._custom_remove_btn = ttk.Button(
            custom_btns, text="Remove", command=self._remove_custom_provider, state="disabled"
        )
        self._custom_remove_btn.pack(side=tk.LEFT)

        ttk.Label(providers_frame, textvariable=self._custom_status_var, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=(6, 0)
        )

        # 5. Configuration & Updates
        cfg_frame = ttk.LabelFrame(body, text="Configuration & Updates", padding=scale_px(self, 10))
        cfg_frame.grid(row=4, column=0, sticky="ew")
        cfg_frame.columnconfigure(0, weight=1)

        resolved_cfg = config_location or get_config_location(None)
        self._resolved_cfg_path = resolved_cfg
        self._cfg_file_label = ttk.Label(
            cfg_frame,
            text=f"Config file: {resolved_cfg}",
            style="Muted.TLabel",
            wraplength=scale_px(self, 560),
            justify="left",
        )
        self._cfg_file_label.grid(row=0, column=0, sticky="w", pady=(0, scale_px(self, 6)))
        bind_adaptive_wraplength(cfg_frame, self._cfg_file_label, margin_dp=24, min_dp=280)

        updates_var = check_updates_var if check_updates_var is not None else tk.BooleanVar(value=True)
        chk_kwargs: dict[str, object] = {
            "text": "Automatically check for updates at startup",
            "variable": updates_var,
        }
        if on_toggle_check_updates is not None:
            chk_kwargs["command"] = on_toggle_check_updates
        ttk.Checkbutton(cfg_frame, **chk_kwargs).grid(row=1, column=0, sticky="w", pady=(0, 6))  # type: ignore[arg-type]

        cfg_btns = ttk.Frame(cfg_frame)
        cfg_btns.grid(row=2, column=0, sticky="w")
        ttk.Button(
            cfg_btns,
            text="Reload Config",
            command=lambda: self._reload_config_and_reset_abs(on_reload_config),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            cfg_btns,
            text="Open Config Folder",
            command=self._open_config_folder,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            cfg_btns,
            text="Copy Path",
            command=self._copy_config_path,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cfg_btns, text="Check for Updates...", command=on_check_updates).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        ttk.Label(cfg_btns, text=f"v{__version__}", style="Muted.TLabel").pack(side=tk.LEFT)
        self._refresh_custom_providers()
        self._scroll.guard_scroll_inputs()
        self.apply_theme(self._palette)

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._custom_tree.tag_configure("ok", foreground=palette.success)
        self._custom_tree.tag_configure("err", foreground=palette.error)
        self._scroll.apply_theme(palette)

    def _toggle_token_visibility(self) -> None:
        self._token_entry.configure(show="" if self._show_token_var.get() else "*")

    def _open_config_folder(self) -> None:
        target = self._resolved_cfg_path
        if os.path.isfile(target):
            open_path_in_os(self.winfo_toplevel(), target, reveal_in_folder=True)
        else:
            parent_dir = os.path.dirname(os.path.abspath(target)) or "."
            open_path_in_os(self.winfo_toplevel(), parent_dir, reveal_in_folder=False)

    def _copy_config_path(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._resolved_cfg_path)
        self._status_var.set("Copied config file path to clipboard.")

    def _sync_custom_provider_buttons(self) -> None:
        has_sel = bool(self._custom_tree.selection())
        state = "normal" if has_sel else "disabled"
        self._custom_edit_btn.configure(state=state)
        self._custom_toggle_btn.configure(state=state)
        self._custom_test_btn.configure(
            state="normal" if (has_sel and not self._testing_custom_provider) else "disabled"
        )
        self._custom_remove_btn.configure(state=state)

    def _mark_abs_dirty(self) -> None:
        if not self._loading_abs:
            self._abs_dirty = True

    def _reload_config_and_reset_abs(self, on_reload_config: Callable[[], None]) -> None:
        self._abs_dirty = False
        on_reload_config()

    def on_show(self) -> None:
        """Refresh FFmpeg, Audiobookshelf, and custom provider values from current config."""
        self._ffmpeg.update_status()
        if not self._abs_dirty:
            current = self._abs_config_provider()
            self._loading_abs = True
            try:
                self._url_var.set(current.url or "")
                self._token_var.set(current.api_key or "")
                self._library_var.set(current.library_id or "")
                self._auto_upload_var.set(bool(current.auto_upload))
                self._auto_scan_var.set(bool(current.auto_scan))
            finally:
                self._loading_abs = False
        self._refresh_custom_providers()

    def _get_custom_providers(self) -> dict[str, CustomMetadataProviderConfig]:
        if self._custom_providers_provider is None:
            return {}
        return dict(self._custom_providers_provider() or {})

    def _refresh_custom_providers(
        self,
        *,
        status_overrides: Mapping[str, tuple[str, tuple[str, ...]]] | None = None,
    ) -> None:
        existing_status: dict[str, tuple[str, tuple[str, ...]]] = {}
        for iid in self._custom_tree.get_children():
            vals = tree_item_values(self._custom_tree, iid)
            tags = tuple(self._custom_tree.item(iid, "tags") or ())
            if len(vals) >= 5 and vals[4]:
                existing_status[iid] = (str(vals[4]), tags)
        if status_overrides:
            existing_status.update(status_overrides)

        existing = self._custom_tree.get_children()
        if existing:
            self._custom_tree.delete(*existing)

        providers = self._get_custom_providers()
        for slug, cfg in providers.items():
            status_text, tags = existing_status.get(
                slug,
                ("Active" if cfg.enabled else "Disabled", ()),
            )
            self._custom_tree.insert(
                "",
                tk.END,
                iid=slug,
                values=(
                    slug,
                    cfg.url,
                    "Configured" if cfg.api_key else "None",
                    "Yes" if cfg.enabled else "No",
                    status_text,
                ),
                tags=tags,
            )
        self._custom_scrolled.set_empty_state(
            len(providers) == 0,
            "No custom metadata providers configured.",
            action_text="Add Provider...",
            action_command=self._add_custom_provider,
        )
        if not self._testing_custom_provider:
            if providers:
                self._custom_status_var.set(
                    f"{len(providers)} custom provider{'s' if len(providers) != 1 else ''} configured."
                )
            else:
                self._custom_status_var.set("No custom metadata providers configured.")
        self._sync_custom_provider_buttons()

    def _selected_custom_provider(self) -> CustomMetadataProviderConfig | None:
        sel = self._custom_tree.selection()
        if not sel:
            return None
        return self._get_custom_providers().get(sel[0])

    def _persist_custom_provider(
        self,
        name: str,
        *,
        url: str,
        api_key: str | None,
        enabled: bool,
    ) -> bool:
        if self._on_save_custom_provider is None:
            return False
        try:
            self._on_save_custom_provider(
                name,
                url=url,
                api_key=api_key,
                enabled=enabled,
            )
            return True
        except Exception as exc:
            messagebox.showerror("Save Provider Failed", str(exc), parent=self.winfo_toplevel())
            return False

    def _add_custom_provider(self) -> None:
        dlg = _CustomProviderDialog(self.winfo_toplevel(), palette=self._palette)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        slug, url, api_key, enabled = dlg.result
        if slug in self._get_custom_providers():
            if not messagebox.askyesno(
                "Overwrite Provider",
                f"A custom provider named '{slug}' already exists. Replace it?",
                parent=self.winfo_toplevel(),
            ):
                return
        if self._persist_custom_provider(slug, url=url, api_key=api_key, enabled=enabled):
            self._refresh_custom_providers(status_overrides={slug: ("Saved", ("ok",))})
            self._custom_status_var.set(f"Saved custom provider '{slug}'.")

    def _edit_custom_provider(self) -> None:
        current = self._selected_custom_provider()
        if current is None:
            self._custom_status_var.set("Select a custom provider to edit.")
            return
        dlg = _CustomProviderDialog(
            self.winfo_toplevel(),
            existing=current,
            palette=self._palette,
        )
        self.wait_window(dlg)
        if dlg.result is None:
            return
        slug, url, api_key, enabled = dlg.result
        if self._persist_custom_provider(slug, url=url, api_key=api_key, enabled=enabled):
            self._refresh_custom_providers(status_overrides={slug: ("Updated", ("ok",))})
            self._custom_status_var.set(f"Updated custom provider '{slug}'.")

    def _toggle_custom_provider(self) -> None:
        current = self._selected_custom_provider()
        if current is None:
            self._custom_status_var.set("Select a custom provider first.")
            return
        new_enabled = not current.enabled
        if self._persist_custom_provider(
            current.name,
            url=current.url,
            api_key=current.api_key,
            enabled=new_enabled,
        ):
            label = "Active" if new_enabled else "Disabled"
            self._refresh_custom_providers(status_overrides={current.name: (label, ())})
            self._custom_status_var.set(
                f"{'Enabled' if new_enabled else 'Disabled'} custom provider '{current.name}'."
            )

    def _remove_custom_provider(self) -> None:
        current = self._selected_custom_provider()
        if current is None:
            self._custom_status_var.set("Select a custom provider to remove.")
            return
        if not messagebox.askyesno(
            "Remove Custom Provider",
            f"Delete custom metadata provider '{current.name}' and its stored API key?",
            parent=self.winfo_toplevel(),
        ):
            return
        if self._on_delete_custom_provider is None:
            return
        try:
            self._on_delete_custom_provider(current.name)
        except Exception as exc:
            messagebox.showerror("Remove Failed", str(exc), parent=self.winfo_toplevel())
            return
        self._refresh_custom_providers()
        self._custom_status_var.set(f"Removed custom provider '{current.name}'.")

    def _test_custom_provider(self) -> None:
        if self._testing_custom_provider:
            return
        current = self._selected_custom_provider()
        if current is None:
            self._custom_status_var.set("Select a custom provider to test.")
            return
        self._testing_custom_provider = True
        self._custom_test_btn.configure(state="disabled")
        self._custom_loading_bar.grid()
        self._custom_loading_bar.start(12)
        self._custom_status_var.set(f"Testing '{current.name}' ({current.url})...")

        proxy = self._http_proxy_provider() if self._http_proxy_provider is not None else None
        raw_timeout = self._http_timeout_provider() if self._http_timeout_provider is not None else None
        timeout = float(raw_timeout) if raw_timeout and float(raw_timeout) > 0 else 10.0

        def _done(summary: str | None, error: str | None) -> None:
            if not self.winfo_exists():
                return
            self._testing_custom_provider = False
            self._custom_loading_bar.stop()
            self._custom_loading_bar.grid_remove()
            self._custom_test_btn.configure(state="normal")
            if error is not None:
                self._custom_status_var.set(f"Test failed for '{current.name}': {error}")
                if self._custom_tree.exists(current.name):
                    vals = tree_item_values(self._custom_tree, current.name)
                    if len(vals) >= 5:
                        vals[4] = f"Error: {error}"
                        self._custom_tree.item(current.name, values=tuple(vals), tags=("err",))
                return
            ok_text = summary or "Connected"
            self._custom_status_var.set(f"'{current.name}': {ok_text}")
            if self._custom_tree.exists(current.name):
                vals = tree_item_values(self._custom_tree, current.name)
                if len(vals) >= 5:
                    vals[4] = ok_text
                    self._custom_tree.item(current.name, values=tuple(vals), tags=("ok",))

        self._cb_queue.begin_task()

        def _worker() -> None:
            summary: str | None = None
            err: str | None = None
            try:
                _count, summary = test_custom_metadata_provider(
                    current.url,
                    api_key=current.api_key,
                    proxy=proxy,
                    timeout=timeout,
                )
            except Exception as exc:
                err = str(exc) or type(exc).__name__
            finally:
                def _post() -> None:
                    _done(summary, err)

                self._cb_queue.post(_post)
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _resolve_library_id_value(self) -> str:
        raw = self._library_var.get().strip()
        for lib_id, name in self._libraries:
            if raw == f"{name} ({lib_id})":
                return lib_id
        return raw

    def _test_connection(self) -> None:
        url = self._url_var.get().strip()
        token = self._token_var.get().strip()
        if not url or not token:
            self._status_var.set("Server URL and API Token are required.")
            return
        if not url.lower().startswith(("http://", "https://")):
            self._status_var.set("Server URL must start with http:// or https://.")
            return
        self._test_btn.configure(state="disabled")
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._status_var.set("Connecting to Audiobookshelf...")

        def _done(username: str, libs: list[tuple[str, str]], error: str | None) -> None:
            if not self.winfo_exists():
                return
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
            self._test_btn.configure(state="normal")
            if error is not None:
                self._status_var.set(f"Connection failed: {error}")
                return
            self._libraries = libs
            labels = [f"{name} ({lib_id})" for lib_id, name in libs]
            self._lib_combo.configure(values=labels)
            if libs and not self._library_var.get().strip():
                self._library_var.set(labels[0])
            self._status_var.set(f"Connected as {username} ({len(libs)} libraries found).")

        proxy = self._http_proxy_provider() if self._http_proxy_provider is not None else None
        raw_timeout = self._http_timeout_provider() if self._http_timeout_provider is not None else None
        timeout: float | tuple[int, int] = (
            float(raw_timeout)
            if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and float(raw_timeout) > 0
            else DEFAULT_HTTP_TIMEOUT
        )
        self._cb_queue.begin_task()

        def _worker() -> None:
            session = create_configured_http_session(proxy=proxy)
            client = AudiobookshelfClient(url, token, session=session, timeout=timeout)
            try:
                username = client.ping()
                libs = client.list_libraries()

                def _post_ok() -> None:
                    _done(username, libs, None)

                self._cb_queue.post(_post_ok)
            except Exception as exc:
                err_msg = str(exc)

                def _post_err() -> None:
                    _done("", [], err_msg)

                self._cb_queue.post(_post_err)
            finally:
                client.close()
                session.close()
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _save(self) -> None:
        url = self._url_var.get().strip().rstrip("/")
        token = self._token_var.get().strip()
        if url and not url.lower().startswith(("http://", "https://")):
            self._status_var.set("Server URL must start with http:// or https://.")
            messagebox.showwarning(
                "Invalid URL",
                "Audiobookshelf Server URL must start with http:// or https://.",
                parent=self.winfo_toplevel(),
            )
            return
        lib_id = self._resolve_library_id_value()
        auto_upload = bool(self._auto_upload_var.get())
        auto_scan = bool(self._auto_scan_var.get())
        try:
            if self._on_save_abs_config is not None:
                self._on_save_abs_config(
                    url=url,
                    library_id=lib_id,
                    api_key=token,
                    auto_upload=auto_upload,
                    auto_scan=auto_scan,
                )
            else:
                save_audiobookshelf_config(
                    url=url,
                    library_id=lib_id,
                    api_key=token,
                    auto_upload=auto_upload,
                    auto_scan=auto_scan,
                    overwrite=self._config_location,
                )
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc), parent=self.winfo_toplevel())
            return
        self._abs_dirty = False
        updated = AudiobookshelfConfig(
            url=url or None,
            api_key=token or None,
            library_id=lib_id or None,
            auto_upload=auto_upload,
            auto_scan=auto_scan,
        )
        if self._on_abs_saved is not None:
            self._on_abs_saved(updated)
        self._status_var.set("Saved Audiobookshelf configuration.")

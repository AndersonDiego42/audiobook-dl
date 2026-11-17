"""Settings page for theme, notifications, clipboard monitor, FFmpeg, Audiobookshelf, and updates."""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable

from audiobookdl import __version__
from audiobookdl.config import (
    AudiobookshelfConfig,
    get_config_location,
    save_audiobookshelf_config,
)
from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
from audiobookdl.gui.theme import (
    LIGHT_PALETTE,
    THEME_LABELS,
    THEME_MODES,
    ThemePalette,
    make_bold_font,
    scale_px,
)
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ScrollableFrame
from audiobookdl.integrations.audiobookshelf import AudiobookshelfClient


class SettingsPage(ttk.Frame):
    """App-level preferences, FFmpeg setup, Audiobookshelf integration, and updates."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        theme_var: tk.StringVar,
        notify_on_complete_var: tk.BooleanVar,
        clipboard_monitor_var: tk.BooleanVar,
        check_updates_var: tk.BooleanVar | None = None,
        on_theme_changed: Callable[[str], object],
        on_toggle_clipboard_monitor: Callable[[], None],
        on_toggle_check_updates: Callable[[], None] | None = None,
        on_reload_config: Callable[[], None],
        on_check_updates: Callable[[], None],
        ffmpeg_assistant: FFmpegAssistant,
        abs_config_provider: Callable[[], AudiobookshelfConfig],
        config_location: str | None = None,
        on_abs_saved: Callable[[AudiobookshelfConfig], None] | None = None,
        palette: ThemePalette | None = None,
        style: ttk.Style | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._config_location = config_location
        self._on_abs_saved = on_abs_saved
        self._abs_config_provider = abs_config_provider
        self._ffmpeg = ffmpeg_assistant
        self._palette = palette or LIGHT_PALETTE
        self._libraries: list[tuple[str, str]] = []
        self._cb_queue = MainThreadCallbackQueue(self)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="Settings", font=make_bold_font(self, 13)).grid(
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
            ttk.Radiobutton(
                theme_row,
                text=THEME_LABELS[mode],
                variable=theme_var,
                value=mode,
                command=lambda m=mode: on_theme_changed(m),
            ).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Checkbutton(
            app_frame,
            text="Send OS notification when a download run finishes",
            variable=notify_on_complete_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 2))

        ttk.Checkbutton(
            app_frame,
            text="Monitor clipboard for audiobook URLs",
            variable=clipboard_monitor_var,
            command=on_toggle_clipboard_monitor,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

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
        self._status_var = tk.StringVar(value="Enter your Audiobookshelf URL and API token.")

        ttk.Label(abs_frame, text="Server URL:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(abs_frame, textvariable=self._url_var, width=42).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(abs_frame, text="API Token:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(abs_frame, textvariable=self._token_var, width=42, show="*").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=4
        )

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

        # 4. Configuration & Updates
        cfg_frame = ttk.LabelFrame(body, text="Configuration & Updates", padding=scale_px(self, 10))
        cfg_frame.grid(row=3, column=0, sticky="ew")
        cfg_frame.columnconfigure(0, weight=1)

        resolved_cfg = config_location or get_config_location(None)
        ttk.Label(
            cfg_frame,
            text=f"Config file: {resolved_cfg}",
            style="Muted.TLabel",
            wraplength=scale_px(self, 560),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

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
        ttk.Button(cfg_btns, text="Reload Config", command=on_reload_config).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(cfg_btns, text="Check for Updates...", command=on_check_updates).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        ttk.Label(cfg_btns, text=f"v{__version__}", style="Muted.TLabel").pack(side=tk.LEFT)
        self._scroll.guard_scroll_inputs()

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._scroll.apply_theme(palette)

    def on_show(self) -> None:
        """Refresh FFmpeg and Audiobookshelf field values from current config."""
        self._ffmpeg.update_status()
        current = self._abs_config_provider()
        self._url_var.set(current.url or "")
        self._token_var.set(current.api_key or "")
        self._library_var.set(current.library_id or "")
        self._auto_upload_var.set(bool(current.auto_upload))
        self._auto_scan_var.set(bool(current.auto_scan))

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

        self._cb_queue.begin_task()

        def _worker() -> None:
            client = AudiobookshelfClient(url, token)
            try:
                username = client.ping()
                libs = client.list_libraries()
                self._cb_queue.post(lambda u=username, l=libs: _done(u, l, None))
            except Exception as exc:
                self._cb_queue.post(lambda err=str(exc): _done("", [], err))
            finally:
                client.close()
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _save(self) -> None:
        url = self._url_var.get().strip().rstrip("/")
        token = self._token_var.get().strip()
        lib_id = self._resolve_library_id_value()
        auto_upload = bool(self._auto_upload_var.get())
        auto_scan = bool(self._auto_scan_var.get())
        try:
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

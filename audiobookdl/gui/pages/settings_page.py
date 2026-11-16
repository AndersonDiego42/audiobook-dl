"""Modal dialog for configuring Audiobookshelf server connection and sync settings."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable

from audiobookdl.config import AudiobookshelfConfig, save_audiobookshelf_config
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue
from audiobookdl.integrations.audiobookshelf import AudiobookshelfClient


class AudiobookshelfDialog(tk.Toplevel):
    """Modal configuration dialog for Audiobookshelf integration."""

    def __init__(
        self,
        parent: tk.Misc,
        current: AudiobookshelfConfig,
        *,
        config_location: str | None = None,
        on_saved: Callable[[AudiobookshelfConfig], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_location = config_location
        self._on_saved = on_saved
        self._palette = palette or LIGHT_PALETTE
        self._libraries: list[tuple[str, str]] = []

        self.title("Audiobookshelf Integration")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self.resizable(False, False)
        self.bind("<Escape>", lambda _e: self.destroy())

        body = ttk.Frame(self, padding=scale_px(self, 14))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._url_var = tk.StringVar(value=current.url or "")
        self._token_var = tk.StringVar(value=current.api_key or "")
        self._library_var = tk.StringVar(value=current.library_id or "")
        self._auto_upload_var = tk.BooleanVar(value=bool(current.auto_upload))
        self._auto_scan_var = tk.BooleanVar(value=bool(current.auto_scan))
        self._status_var = tk.StringVar(value="Enter your Audiobookshelf URL and API token.")

        ttk.Label(body, text="Server URL:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(body, textvariable=self._url_var, width=42).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(body, text="API Token:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(body, textvariable=self._token_var, width=42, show="*").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(body, text="Library ID:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self._lib_combo = ttk.Combobox(body, textvariable=self._library_var, width=28)
        self._lib_combo.grid(row=2, column=1, sticky="ew", pady=4, padx=(0, 6))
        self._test_btn = ttk.Button(body, text="Test & Load Libraries", command=self._test_connection)
        self._test_btn.grid(row=2, column=2, sticky="e", pady=4)

        ttk.Checkbutton(
            body,
            text="Automatically upload completed/packed books to Audiobookshelf",
            variable=self._auto_upload_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Checkbutton(
            body,
            text="Trigger Audiobookshelf library scan after sync",
            variable=self._auto_scan_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 8))

        ttk.Label(body, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        self._loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self._loading_bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self._loading_bar.grid_remove()

        btns = ttk.Frame(body)
        btns.grid(row=7, column=0, columnspan=3, sticky="ew")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

        apply_theme_to_window(self, self._palette)
        self._cb_queue = MainThreadCallbackQueue(self)

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
            messagebox.showerror("Save Error", str(exc), parent=self)
            return
        updated = AudiobookshelfConfig(
            url=url or None,
            api_key=token or None,
            library_id=lib_id or None,
            auto_upload=auto_upload,
            auto_scan=auto_scan,
        )
        if self._on_saved is not None:
            self._on_saved(updated)
        self.destroy()

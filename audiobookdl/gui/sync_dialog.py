"""Modal Library Sync Manager dialog for multi-account sync, dry-run, and auto-sync scheduling."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl.config import (
    DEFAULT_PROFILE,
    Config,
    list_configured_source_profiles,
)
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.sources import ServiceInfo


class LibrarySyncDialog(tk.Toplevel):
    """Configure targets, sync mode, and background auto-sync interval."""

    def __init__(
        self,
        parent: tk.Misc,
        config: Config,
        services: Sequence[ServiceInfo],
        *,
        auto_sync_enabled: bool = False,
        auto_sync_minutes: int = 30,
        on_run_sync: Callable[[list[tuple[str, str]], str], None],
        on_auto_sync_changed: Callable[[bool, int], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or LIGHT_PALETTE
        self._on_run_sync = on_run_sync
        self._on_auto_sync_changed = on_auto_sync_changed
        self._services_by_key = {s.key: s for s in services}

        self.title("Library Sync Manager")
        self.transient(parent)  # type: ignore[call-overload]
        self.grab_set()
        self.minsize(scale_px(self, 520), scale_px(self, 380))
        self.bind("<Escape>", lambda _e: self.destroy())

        body = ttk.Frame(self, padding=scale_px(self, 14))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        ttk.Label(
            body,
            text="Select accounts to synchronize and choose how discovered pending audiobooks should be handled.",
            style="Muted.TLabel",
            wraplength=scale_px(self, 490),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        accounts_frame = ttk.LabelFrame(body, text="Accounts", padding=scale_px(self, 8))
        accounts_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        accounts_frame.columnconfigure(0, weight=1)

        self._target_vars: list[tuple[str, str, tk.BooleanVar]] = []
        configured = [
            (svc, prof)
            for svc, prof, _src_cfg in list_configured_source_profiles(config)
            if (svc in self._services_by_key and self._services_by_key[svc].supports_library)
        ]
        if not configured:
            configured = [
                (s.key, DEFAULT_PROFILE)
                for s in services
                if s.supports_library
            ]

        if not configured:
            ttk.Label(
                accounts_frame,
                text="No library-capable services found.",
                style="Muted.TLabel",
            ).grid(row=0, column=0, sticky="w")
        else:
            for row_idx, (svc_key, prof) in enumerate(configured):
                svc_info = self._services_by_key.get(svc_key)
                label = svc_info.label if svc_info else svc_key
                var = tk.BooleanVar(value=True)
                self._target_vars.append((svc_key, prof, var))
                ttk.Checkbutton(
                    accounts_frame,
                    text=f"{label} ({prof})",
                    variable=var,
                ).grid(row=row_idx, column=0, sticky="w", pady=2)

        mode_frame = ttk.LabelFrame(body, text="Sync Mode", padding=scale_px(self, 8))
        mode_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.mode_var = tk.StringVar(value="enqueue")
        ttk.Radiobutton(
            mode_frame,
            text="Scan & Enqueue Pending Books (uses main download queue)",
            variable=self.mode_var,
            value="enqueue",
        ).pack(anchor="w", pady=1)
        ttk.Radiobutton(
            mode_frame,
            text="Scan Only (update library database without downloading)",
            variable=self.mode_var,
            value="scan_only",
        ).pack(anchor="w", pady=1)
        ttk.Radiobutton(
            mode_frame,
            text="Dry Run (preview new books without modifying database)",
            variable=self.mode_var,
            value="dry_run",
        ).pack(anchor="w", pady=1)

        sched_frame = ttk.LabelFrame(body, text="Background Auto-Sync", padding=scale_px(self, 8))
        sched_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.auto_sync_var = tk.BooleanVar(value=auto_sync_enabled)
        self.interval_var = tk.StringVar(value=str(max(1, int(auto_sync_minutes))))
        ttk.Checkbutton(
            sched_frame,
            text="Auto-sync while GUI is open every",
            variable=self.auto_sync_var,
        ).pack(side=tk.LEFT)
        ttk.Spinbox(
            sched_frame,
            from_=1,
            to=1440,
            textvariable=self.interval_var,
            width=5,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(sched_frame, text="minutes").pack(side=tk.LEFT)

        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, sticky="ew")
        ttk.Button(btns, text="Close", command=self._save_schedule_and_close).pack(side=tk.RIGHT)
        ttk.Button(
            btns,
            text="Sync Now",
            command=self._start_sync,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT, padx=(0, 8))

        apply_theme_to_window(self, self._palette)

    def _read_schedule(self) -> tuple[bool, int] | None:
        enabled = bool(self.auto_sync_var.get())
        raw = self.interval_var.get().strip() or "30"
        try:
            mins = int(raw)
        except ValueError:
            messagebox.showwarning("Invalid Interval", "Interval must be a whole number of minutes.", parent=self)
            return None
        if mins < 1:
            messagebox.showwarning("Invalid Interval", "Interval must be at least 1 minute.", parent=self)
            return None
        return enabled, mins

    def _save_schedule_and_close(self) -> None:
        sched = self._read_schedule()
        if sched is None:
            return
        if self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])
        self.destroy()

    def _start_sync(self) -> None:
        targets = [(svc, prof) for svc, prof, var in self._target_vars if var.get()]
        if not targets:
            messagebox.showwarning("No Accounts Selected", "Select at least one account to sync.", parent=self)
            return
        sched = self._read_schedule()
        if sched is None:
            return
        if self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])
        mode = self.mode_var.get()
        self.destroy()
        self._on_run_sync(targets, mode)

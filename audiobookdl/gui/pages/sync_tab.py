"""Library synchronization tab configuring sync targets, mode, and auto-sync schedule."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import tkinter as tk
from tkinter import messagebox, ttk

from audiobookdl.config import (
    DEFAULT_AUTO_SYNC_MODE,
    DEFAULT_PROFILE,
    VALID_AUTO_SYNC_MODES,
    Config,
    list_configured_source_profiles,
)
from audiobookdl.gui.theme import scale_px
from audiobookdl.sources import ServiceInfo


class SyncTab(ttk.Frame):
    """Tab configuring library synchronization targets, mode, and auto-sync schedule."""

    def __init__(
        self,
        parent: tk.Misc,
        config_provider: Callable[[], Config],
        services: Sequence[ServiceInfo],
        *,
        auto_sync_enabled: bool = False,
        auto_sync_minutes: int = 30,
        auto_sync_mode: str = DEFAULT_AUTO_SYNC_MODE,
        auto_sync_targets: Sequence[tuple[str, str]] | None = None,
        on_run_sync: Callable[[list[tuple[str, str]], str], None],
        on_auto_sync_changed: Callable[[bool, int], None] | None = None,
        on_save_schedule: Callable[[bool, int, str, list[tuple[str, str]]], None] | None = None,
        on_manage_accounts: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._config_provider = config_provider
        self._services = list(services)
        self._services_by_key = {s.key: s for s in self._services}
        self._on_run_sync = on_run_sync
        self._on_auto_sync_changed = on_auto_sync_changed
        self._on_save_schedule = on_save_schedule
        self._on_manage_accounts = on_manage_accounts
        self._initial_targets: set[tuple[str, str]] | None = (
            {(svc, prof) for svc, prof in auto_sync_targets}
            if auto_sync_targets is not None
            else None
        )
        self._targets_initialized = False
        self._target_vars: list[tuple[str, str, tk.BooleanVar]] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Select accounts to synchronize and choose how discovered pending audiobooks should be handled.",
            style="Muted.TLabel",
            wraplength=scale_px(self, 620),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._accounts_frame = ttk.LabelFrame(self, text="Accounts", padding=scale_px(self, 8))
        self._accounts_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        self._accounts_frame.columnconfigure(0, weight=1)
        self.refresh_targets()

        mode_frame = ttk.LabelFrame(self, text="Sync Mode", padding=scale_px(self, 8))
        mode_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        initial_mode = (
            auto_sync_mode if auto_sync_mode in VALID_AUTO_SYNC_MODES else DEFAULT_AUTO_SYNC_MODE
        )
        self.mode_var = tk.StringVar(value=initial_mode)
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

        sched_frame = ttk.LabelFrame(self, text="Background Auto-Sync", padding=scale_px(self, 8))
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

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, sticky="ew")
        if self._on_manage_accounts is not None:
            ttk.Button(btns, text="Manage Accounts...", command=self._on_manage_accounts).pack(
                side=tk.LEFT
            )
        ttk.Button(btns, text="Save Schedule", command=self._save_schedule).pack(side=tk.RIGHT)
        ttk.Button(
            btns,
            text="Sync Now",
            command=self._start_sync,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT, padx=(0, 8))

    def refresh_targets(self) -> None:
        prev_states = {(svc, prof): bool(var.get()) for svc, prof, var in self._target_vars}
        for child in self._accounts_frame.winfo_children():
            child.destroy()
        self._target_vars.clear()

        cfg = self._config_provider()
        configured = [
            (svc, prof)
            for svc, prof, _src_cfg in list_configured_source_profiles(cfg)
            if (svc in self._services_by_key and self._services_by_key[svc].supports_library)
        ]
        if not configured:
            configured = [
                (s.key, DEFAULT_PROFILE)
                for s in self._services
                if s.supports_library
            ]
        if not configured:
            ttk.Label(
                self._accounts_frame,
                text="No library-capable services found.",
                style="Muted.TLabel",
            ).grid(row=0, column=0, sticky="w")
            return
        for row_idx, (svc_key, prof) in enumerate(configured):
            svc_info = self._services_by_key.get(svc_key)
            label = svc_info.label if svc_info else svc_key
            if self._targets_initialized:
                checked = prev_states.get((svc_key, prof), True)
            elif self._initial_targets is not None:
                checked = (svc_key, prof) in self._initial_targets
            else:
                checked = True
            var = tk.BooleanVar(value=checked)
            self._target_vars.append((svc_key, prof, var))
            ttk.Checkbutton(
                self._accounts_frame,
                text=f"{label} ({prof})",
                variable=var,
            ).grid(row=row_idx, column=0, sticky="w", pady=2)
        self._targets_initialized = True

    def selected_targets(self) -> list[tuple[str, str]]:
        """Return currently checked `(service_key, profile)` sync target pairs."""
        return [(svc, prof) for svc, prof, var in self._target_vars if var.get()]

    def apply_saved_schedule(
        self,
        *,
        enabled: bool,
        minutes: int,
        mode: str,
        targets: Sequence[tuple[str, str]] | None,
    ) -> None:
        """Update SyncTab controls from reloaded configuration."""
        self.auto_sync_var.set(bool(enabled))
        self.interval_var.set(str(max(1, int(minutes))))
        if mode in ("enqueue", "scan_only", "dry_run"):
            self.mode_var.set(mode)
        self._initial_targets = (
            {(svc, prof) for svc, prof in targets} if targets is not None else None
        )
        self._targets_initialized = False
        self.refresh_targets()

    def _read_schedule(self) -> tuple[bool, int] | None:
        enabled = bool(self.auto_sync_var.get())
        raw = self.interval_var.get().strip() or "30"
        try:
            mins = int(raw)
        except ValueError:
            messagebox.showwarning(
                "Invalid Interval",
                "Interval must be a whole number of minutes.",
                parent=self.winfo_toplevel(),
            )
            return None
        if mins < 1:
            messagebox.showwarning(
                "Invalid Interval",
                "Interval must be at least 1 minute.",
                parent=self.winfo_toplevel(),
            )
            return None
        return enabled, mins

    def _save_schedule(self) -> None:
        sched = self._read_schedule()
        if sched is None:
            return
        targets = self.selected_targets()
        mode = self.mode_var.get()
        if self._on_save_schedule is not None:
            self._on_save_schedule(sched[0], sched[1], mode, targets)
        elif self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])

    def _start_sync(self) -> None:
        targets = [(svc, prof) for svc, prof, var in self._target_vars if var.get()]
        if not targets:
            messagebox.showwarning(
                "No Accounts Selected",
                "Select at least one account to sync.",
                parent=self.winfo_toplevel(),
            )
            return
        sched = self._read_schedule()
        if sched is None:
            return
        if self._on_auto_sync_changed is not None:
            self._on_auto_sync_changed(sched[0], sched[1])
        self._on_run_sync(targets, self.mode_var.get())

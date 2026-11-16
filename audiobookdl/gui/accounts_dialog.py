"""Overview dialog listing all services, configured profiles, vault backends, and quick actions."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl import args
from audiobookdl.config import (
    DEFAULT_PROFILE,
    list_source_profiles,
    make_source_profile_key,
)
from audiobookdl.credentials import get_credential_store
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ScrolledTreeview, TreeColumnSpec
from audiobookdl.pipeline import verify_service_auth
from audiobookdl.sources import ServiceInfo


class AccountsOverviewDialog(tk.Toplevel):
    """Manage all supported audiobook services, profiles, credentials, and login verification."""

    _COLUMNS = (
        TreeColumnSpec("service", "Service", width=165, stretch=True),
        TreeColumnSpec("profile", "Profile", width=90, stretch=False),
        TreeColumnSpec("auth", "Auth Type", width=110, stretch=False),
        TreeColumnSpec("caps", "Capabilities", width=155, stretch=True),
        TreeColumnSpec("vault", "Credential Vault", width=115, stretch=False),
        TreeColumnSpec("status", "Status", width=120, stretch=False),
    )

    def __init__(
        self,
        parent: tk.Misc,
        controller: GuiController,
        services: Sequence[ServiceInfo],
        *,
        on_import_cookies: Callable[[str], None] | None = None,
        on_select_account: Callable[[str, str], None] | None = None,
        on_credentials_changed: Callable[[], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._services = list(services)
        self._services_by_key = {s.key: s for s in self._services}
        self._on_import_cookies = on_import_cookies
        self._on_select_account = on_select_account
        self._on_credentials_changed = on_credentials_changed
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)
        self._rows: list[tuple[str, str]] = []
        self._is_testing = False

        self.title("Manage All Accounts & Services")
        self.transient(parent)  # type: ignore[call-overload]
        self.geometry(f"{scale_px(self, 820)}x{scale_px(self, 460)}")
        self.minsize(scale_px(self, 640), scale_px(self, 320))
        self.bind("<Escape>", lambda _e: self.destroy())

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._scrolled = ScrolledTreeview(body, self._COLUMNS, selectmode="browse")
        self._scrolled.grid(row=0, column=0, sticky="nsew")
        self._tree = self._scrolled.tree
        self._tree.tag_configure("ok", foreground=self._palette.success)
        self._tree.tag_configure("err", foreground=self._palette.error)

        self._status_var = tk.StringVar(value="")
        self._loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self._loading_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._test_all_btn = ttk.Button(btn_bar, text="Test All Logins", command=self._test_all_logins)
        self._test_all_btn.pack(side=tk.LEFT, padx=(0, 6))
        if self._on_import_cookies is not None:
            ttk.Button(btn_bar, text="Import Cookies...", command=self._import_cookies).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        ttk.Button(btn_bar, text="Delete Profile", command=self._delete_selected_profile).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        if self._on_select_account is not None:
            ttk.Button(btn_bar, text="Select in Main Window", command=self._select_in_main).pack(
                side=tk.LEFT
            )
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        ttk.Label(body, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

        self.refresh()
        apply_theme_to_window(self, self._palette)

    def _vault_backend_label(self) -> str:
        loc = self._controller.config_location
        if not loc:
            from audiobookdl.config import get_config_location

            loc = get_config_location(None)
        store = get_credential_store(loc)
        cls_name = type(store).__name__
        if "Dpapi" in cls_name:
            return "DPAPI"
        if "Keyring" in cls_name:
            return "Keyring"
        return "Plaintext" if store.is_secure is False else cls_name

    def refresh(self) -> None:
        existing = self._tree.get_children()
        if existing:
            self._tree.delete(*existing)
        self._rows.clear()
        cfg = self._controller.config_snapshot()
        vault_label = self._vault_backend_label()

        for svc in self._services:
            profiles = list_source_profiles(cfg, svc.key)
            if not profiles:
                profiles = [DEFAULT_PROFILE]
            auth_label = ", ".join(m.capitalize() for m in svc.auth_methods) if svc.auth_methods else "None"
            caps: list[str] = []
            if svc.supports_search:
                caps.append("Search")
            if svc.supports_library:
                caps.append("Library")
            if "cookies" in svc.auth_methods:
                caps.append("Cookies")
            caps_label = ", ".join(caps) if caps else "URL Only"

            for prof in profiles:
                src_cfg = cfg.sources.get(make_source_profile_key(svc.key, prof))
                has_creds = bool(
                    src_cfg and any((src_cfg.username, src_cfg.password, src_cfg.library, src_cfg.cookie_file))
                )
                status = "Configured" if has_creds else ("No auth needed" if not svc.auth_methods else "Unconfigured")
                iid = f"{svc.key}:{prof}"
                self._rows.append((svc.key, prof))
                self._tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(svc.label, prof, auth_label, caps_label, vault_label, status),
                )
        self._status_var.set(f"{len(self._rows)} account entries across {len(self._services)} services.")

    def _selected_service_profile(self) -> tuple[str, str] | None:
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if ":" in iid:
            svc_key, prof = iid.split(":", 1)
            return svc_key, prof
        return None

    def _import_cookies(self) -> None:
        if self._on_import_cookies is None:
            return
        picked = self._selected_service_profile()
        self._on_import_cookies(picked[0] if picked else "")
        self.refresh()

    def _select_in_main(self) -> None:
        if self._on_select_account is None:
            return
        picked = self._selected_service_profile()
        if picked is None:
            return
        self._on_select_account(picked[0], picked[1])
        self.destroy()

    def _delete_selected_profile(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        svc_key, prof = picked
        if not messagebox.askyesno(
            "Delete Credentials",
            f"Remove saved credentials for {svc_key} ({prof})?",
            parent=self,
        ):
            return
        self._controller.save_source_credentials(svc_key, profile=prof)
        if self._on_credentials_changed is not None:
            self._on_credentials_changed()
        self.refresh()
        self._status_var.set(f"Removed credentials for {svc_key} ({prof}).")

    def _test_all_logins(self) -> None:
        if self._is_testing:
            return
        cfg = self._controller.config_snapshot()
        targets: list[tuple[str, str]] = []
        for svc in self._services:
            if not svc.auth_methods:
                continue
            for prof in list_source_profiles(cfg, svc.key):
                src_cfg = cfg.sources.get(make_source_profile_key(svc.key, prof))
                if src_cfg and any((src_cfg.username, src_cfg.password, src_cfg.cookie_file)):
                    targets.append((svc.key, prof))
        if not targets:
            self._status_var.set("No configured accounts with credentials to verify.")
            return
        self._is_testing = True
        self._test_all_btn.configure(state="disabled")
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._status_var.set(f"Verifying {len(targets)} configured account(s)...")

        def _update_row(svc_key: str, prof: str, ok: bool, err: str | None) -> None:
            if not self.winfo_exists():
                return
            iid = f"{svc_key}:{prof}"
            if self._tree.exists(iid):
                vals = list(self._tree.item(iid, "values"))
                if len(vals) >= 6:
                    vals[5] = "Valid" if ok else (f"Failed: {err}" if err else "Failed")
                    self._tree.item(iid, values=tuple(vals), tags=("ok" if ok else "err",))

        def _finish(valid_count: int, total: int) -> None:
            if not self.winfo_exists():
                return
            self._is_testing = False
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
            self._test_all_btn.configure(state="normal")
            self._status_var.set(f"Verified {valid_count}/{total} configured account(s).")

        self._cb_queue.begin_task()

        def _worker() -> None:
            valid = 0
            try:
                for svc_key, prof in targets:
                    opts = args.build_default_options(
                        config_location=self._controller.config_location,
                        profile=None if prof == DEFAULT_PROFILE else prof,
                    )
                    ok = False
                    err: str | None = None
                    try:
                        ok = verify_service_auth(svc_key, opts, cfg)
                    except Exception as exc:
                        err = str(exc)
                    if ok:
                        valid += 1

                    def _emit_row(
                        sk: str = svc_key,
                        pf: str = prof,
                        res: bool = ok,
                        er: str | None = err,
                    ) -> None:
                        _update_row(sk, pf, res, er)

                    self._cb_queue.post(_emit_row)
            finally:
                total = len(targets)

                def _emit_finish(v: int = valid, tot: int = total) -> None:
                    _finish(v, tot)

                self._cb_queue.post(_emit_finish)
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

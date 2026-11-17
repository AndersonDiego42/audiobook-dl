"""Accounts page hosting service credential editing and the multi-profile accounts overview."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl.options_schema import build_default_options
from audiobookdl.config import (
    DEFAULT_PROFILE,
    list_source_profiles,
    make_source_profile_key,
)
from audiobookdl.credentials import get_credential_store
from audiobookdl.gui.auth_panel import ServiceAuthCoordinator, ServiceAuthPanel
from audiobookdl.gui.controller import GuiController, GuiInteractionHandler
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ScrolledTreeview, TreeColumnSpec, tree_item_values
from audiobookdl.pipeline import verify_service_auth
from audiobookdl.sources import ServiceInfo


class AccountsPage(ttk.Frame):
    """Manage service authentication credentials, profiles, cookie import, and verification."""

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
        auth_coordinator: ServiceAuthCoordinator,
        *,
        on_import_cookies: Callable[[str], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._controller = controller
        self._services = list(services)
        self._services_by_key = {s.key: s for s in self._services}
        self._auth_coord = auth_coordinator
        self._on_import_cookies = on_import_cookies
        self._palette = palette or LIGHT_PALETTE
        self._cb_queue = MainThreadCallbackQueue(self)
        self._rows: list[tuple[str, str]] = []
        self._is_testing = False
        self._test_cancel_event: threading.Event | None = None
        self._test_interaction: GuiInteractionHandler | None = None
        self.bind("<Destroy>", self._on_destroy)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Accounts & Services", font=make_bold_font(self, 13)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Configure logins, profiles, and session cookies across supported audiobook services.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        self.auth_panel = ServiceAuthPanel(self, auth_coordinator)
        self.auth_panel.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        overview_frame = ttk.LabelFrame(self, text="All Services & Profiles", padding=scale_px(self, 8))
        overview_frame.grid(row=2, column=0, sticky="nsew")
        overview_frame.columnconfigure(0, weight=1)
        overview_frame.rowconfigure(0, weight=1)

        self._scrolled = ScrolledTreeview(overview_frame, self._COLUMNS, selectmode="browse")
        self._scrolled.grid(row=0, column=0, sticky="nsew")
        self._tree = self._scrolled.tree
        self._tree.bind("<Double-1>", lambda _e: self._select_in_form())

        self._status_var = tk.StringVar(value="")
        self._loading_bar = ttk.Progressbar(overview_frame, mode="indeterminate")
        self._loading_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(overview_frame)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._test_all_btn = ttk.Button(btn_bar, text="Test All Logins", command=self._test_all_logins)
        self._test_all_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cancel_test_btn = ttk.Button(
            btn_bar,
            text="Cancel Test",
            command=self._cancel_test_logins,
            state="disabled",
        )
        self._cancel_test_btn.pack(side=tk.LEFT, padx=(0, 6))
        if self._on_import_cookies is not None:
            ttk.Button(btn_bar, text="Import Cookies...", command=self._import_cookies).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        ttk.Button(btn_bar, text="Delete Profile", command=self._delete_selected_profile).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Edit Selected", command=self._select_in_form).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_bar, text="Refresh", command=self.refresh).pack(side=tk.RIGHT)

        ttk.Label(overview_frame, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

        self.apply_theme(self._palette)
        self.refresh()

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("ok", foreground=palette.success)
        self._tree.tag_configure("err", foreground=palette.error)

    def on_show(self) -> None:
        """Refresh profile statuses and active service form when Accounts is visited."""
        self._auth_coord.load_service_into_form()
        self._auth_coord.update_field_states()
        self.refresh()

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

    def _select_in_form(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        self._auth_coord.select_service_profile(picked[0], picked[1])

    def _delete_selected_profile(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        svc_key, prof = picked
        if not messagebox.askyesno(
            "Delete Credentials",
            f"Remove saved credentials for {svc_key} ({prof})?",
            parent=self.winfo_toplevel(),
        ):
            return
        self._controller.save_source_credentials(svc_key, profile=prof)
        self._auth_coord.load_service_into_form()
        self._auth_coord.update_field_states()
        self.refresh()
        self._status_var.set(f"Removed credentials for {svc_key} ({prof}).")

    def _cancel_test_logins(self) -> None:
        if not self._is_testing:
            return
        self._cancel_test_btn.configure(state="disabled")
        self._status_var.set("Cancelling login verification...")
        if self._test_cancel_event is not None:
            self._test_cancel_event.set()
        if self._test_interaction is not None:
            self._test_interaction.release_all()

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
        self._cancel_test_btn.configure(state="normal")
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._status_var.set(f"Verifying {len(targets)} configured account(s)...")

        def _update_row(svc_key: str, prof: str, ok: bool, err: str | None) -> None:
            if not self.winfo_exists():
                return
            iid = f"{svc_key}:{prof}"
            if self._tree.exists(iid):
                vals = tree_item_values(self._tree, iid)
                if len(vals) >= 6:
                    vals[5] = "Valid" if ok else (f"Failed: {err}" if err else "Failed")
                    self._tree.item(iid, values=tuple(vals), tags=("ok" if ok else "err",))

        def _finish(valid_count: int, checked_count: int, total: int, cancelled: bool) -> None:
            if not self.winfo_exists():
                return
            self._is_testing = False
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
            self._cancel_test_btn.configure(state="disabled")
            self._test_all_btn.configure(state="normal")
            if cancelled:
                self._status_var.set(
                    f"Verification cancelled ({checked_count}/{total} checked, {valid_count} valid)."
                )
            else:
                self._status_var.set(f"Verified {valid_count}/{total} configured account(s).")

        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._controller.event_queue, cancel_ev)
        self._test_cancel_event = cancel_ev
        self._test_interaction = interaction
        self._controller._register_aux_interaction(interaction)
        self._cb_queue.begin_task()

        def _worker() -> None:
            valid = 0
            checked = 0
            was_cancelled = False
            counter_lock = threading.Lock()

            def _check_one(svc_key: str, prof: str) -> None:
                nonlocal valid, checked, was_cancelled
                if cancel_ev.is_set():
                    return
                opts = build_default_options(
                    config_location=self._controller.config_location,
                    profile=None if prof == DEFAULT_PROFILE else prof,
                )
                ok = False
                err: str | None = None
                try:
                    ok = verify_service_auth(
                        svc_key,
                        opts,
                        cfg,
                        interaction=interaction,
                    )
                except Exception as exc:
                    err = str(exc) or type(exc).__name__
                if cancel_ev.is_set():
                    with counter_lock:
                        was_cancelled = True
                    return
                with counter_lock:
                    checked += 1
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

            try:
                max_workers = min(4, len(targets))
                if max_workers <= 1:
                    for svc_key, prof in targets:
                        if cancel_ev.is_set():
                            was_cancelled = True
                            break
                        _check_one(svc_key, prof)
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        futures = [pool.submit(_check_one, svc_key, prof) for svc_key, prof in targets]
                        for fut in as_completed(futures):
                            if cancel_ev.is_set():
                                was_cancelled = True
                                for pending_fut in futures:
                                    pending_fut.cancel()
                                break
                            fut.result()
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._controller._unregister_aux_interaction(interaction)
                self._test_cancel_event = None
                self._test_interaction = None
                total = len(targets)

                def _emit_finish(
                    v: int = valid,
                    chk: int = checked,
                    tot: int = total,
                    can: bool = was_cancelled,
                ) -> None:
                    _finish(v, chk, tot, can)

                self._cb_queue.post(_emit_finish)
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._test_cancel_event is not None:
            self._test_cancel_event.set()
        if self._test_interaction is not None:
            self._test_interaction.release_all()

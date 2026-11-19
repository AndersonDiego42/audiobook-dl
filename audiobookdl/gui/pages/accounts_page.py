"""Accounts page hosting service credential editing and the multi-profile accounts overview."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from collections.abc import Callable, Sequence
from typing import Any

from audiobookdl.config import (
    DEFAULT_PROFILE,
    list_source_profiles,
    make_source_profile_key,
    normalize_profile_name,
)
from audiobookdl.credentials import get_credential_store
from audiobookdl.gui.auth_panel import ServiceAuthCoordinator, ServiceAuthPanel
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import (
    ResponsiveMode,
    ScrolledTreeview,
    TreeColumnSpec,
    resolve_responsive_mode,
    tree_item_values,
)
from audiobookdl.sources import ServiceInfo


class AccountsPage(ttk.Frame):
    """Manage service authentication credentials, profiles, cookie import, and verification."""

    _BTN_BAR_WIDE_ENTER_DP = 680
    _BTN_BAR_WIDE_EXIT_DP = 640

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
        on_import_cookies: Callable[..., None] | None=None,
        on_context_label: Callable[[str], None] | None=None,
        palette: ThemePalette | None=None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._controller = controller
        self._services = list(services)
        self._services_by_key = {s.key: s for s in self._services}
        self._auth_coord = auth_coordinator
        self._on_import_cookies = on_import_cookies
        self._on_context_label = on_context_label
        self._palette = palette or LIGHT_PALETTE
        self._rows: list[tuple[str, str]] = []
        self._is_testing = False
        self._active_test_run_id: int | None = None
        self._verification_cache: dict[tuple[str, str], tuple[str, str]] = {}
        self._btn_bar_mode: ResponsiveMode = "wide"
        self.bind("<Destroy>", self._on_destroy)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, scale_px(self, 8)))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Accounts & Services", font=make_bold_font(self, 4)).grid(
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
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_selection_buttons(), add="+")
        self._tree.bind("<Double-1>", lambda _e: self._select_in_form())
        self._tree.bind("<Return>", lambda _e: self._select_in_form(), add="+")
        self._tree.bind("<Delete>", lambda _e: self._delete_selected_profile(), add="+")
        self._tree.bind("<F2>", lambda _e: self._rename_selected_profile(), add="+")

        self._status_var = tk.StringVar(value="")
        self._loading_bar = ttk.Progressbar(overview_frame, mode="indeterminate")
        self._loading_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._loading_bar.grid_remove()

        btn_bar = ttk.Frame(overview_frame)
        btn_bar.grid(row=2, column=0, sticky="ew", pady=(scale_px(self, 8), 0))
        self._btn_bar = btn_bar
        self._test_all_btn = ttk.Button(btn_bar, text="Test All Logins", command=self._test_all_logins)
        self._cancel_test_btn = ttk.Button(
            btn_bar,
            text="Cancel Test",
            command=self._cancel_test_logins,
            state="disabled",
        )
        self._left_action_btns: list[ttk.Button] = [
            self._test_all_btn,
            self._cancel_test_btn,
        ]
        if self._on_import_cookies is not None:
            self._left_action_btns.append(
                ttk.Button(btn_bar, text="Import Cookies...", command=self._import_cookies)
            )
        self._new_profile_btn = ttk.Button(
            btn_bar, text="+ New Profile...", command=self._new_profile
        )
        self._rename_profile_btn = ttk.Button(
            btn_bar,
            text="Rename Profile...",
            command=self._rename_selected_profile,
            state="disabled",
        )
        self._delete_profile_btn = ttk.Button(
            btn_bar,
            text="Delete Profile",
            command=self._delete_selected_profile,
            state="disabled",
        )
        self._edit_selected_btn = ttk.Button(
            btn_bar, text="Edit Selected", command=self._select_in_form, state="disabled"
        )
        self._left_action_btns.extend(
            [
                self._new_profile_btn,
                self._rename_profile_btn,
                self._delete_profile_btn,
                self._edit_selected_btn,
            ]
        )
        self._refresh_btn = ttk.Button(btn_bar, text="Refresh", command=self.refresh)
        self._apply_btn_bar_layout("wide")
        btn_bar.bind("<Configure>", self._on_btn_bar_configure, add="+")

        ttk.Label(overview_frame, textvariable=self._status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(scale_px(self, 6), 0)
        )

        self.apply_theme(self._palette)
        self.refresh()

    def _on_btn_bar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._btn_bar.winfo_width())
        self.sync_responsive_toolbars(w)

    def sync_responsive_toolbars(self, width: int | None=None) -> None:
        """Reflow overview action buttons into 2 rows when narrow."""
        w = int(width if width is not None else self._btn_bar.winfo_width())
        if w <= 1:
            return
        mode = resolve_responsive_mode(
            self._btn_bar_mode,
            w,
            scale_px(self, self._BTN_BAR_WIDE_ENTER_DP),
            scale_px(self, self._BTN_BAR_WIDE_EXIT_DP),
        )
        if mode != self._btn_bar_mode:
            self._apply_btn_bar_layout(mode)

    def _apply_btn_bar_layout(self, mode: ResponsiveMode) -> None:
        self._btn_bar_mode = mode
        for col in range(9):
            self._btn_bar.columnconfigure(col, weight=0)
        p4 = scale_px(self, 4)
        p6 = scale_px(self, 6)
        btns = self._left_action_btns
        if mode == "wide":
            for idx, btn in enumerate(btns):
                btn.grid(row=0, column=idx, sticky="w", padx=(0, p6), pady=0)
            self._btn_bar.columnconfigure(len(btns), weight=1)
            self._refresh_btn.grid(row=0, column=len(btns) + 1, sticky="e", padx=0, pady=0)
            return
        split_at = (len(btns) + 1) // 2
        for idx, btn in enumerate(btns):
            row = 0 if idx < split_at else 1
            col = idx if idx < split_at else idx - split_at
            btn.grid(
                row=row,
                column=col,
                sticky="w",
                padx=(0, p6),
                pady=(p4, 0) if row == 1 else 0,
            )
        self._btn_bar.columnconfigure(split_at, weight=1)
        self._refresh_btn.grid(row=1, column=split_at + 1, sticky="e", padx=0, pady=(p4, 0))

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._tree.tag_configure("ok", foreground=palette.success)
        self._tree.tag_configure("err", foreground=palette.error)

    def apply_route_context(self, route: Any) -> None:
        """Select the target service and profile carried by an incoming `NavRoute`."""
        svc_key = getattr(route, "service_key", None)
        if svc_key:
            prof = normalize_profile_name(getattr(route, "profile", None) or DEFAULT_PROFILE)
            self._auth_coord.select_service_profile(svc_key, prof)

    def _sync_active_row_and_breadcrumb(self) -> None:
        svc = self._auth_coord.selected_service()
        prof = normalize_profile_name(self._auth_coord.profile_var.get())
        if svc is None:
            return
        iid = f"{svc.key}:{prof}"
        if self._tree.exists(iid):
            self._tree.selection_set(iid)
            self._tree.focus(iid)
            self._tree.see(iid)
        if self._on_context_label is not None:
            self._on_context_label(f"{svc.label} ({prof})")
        self._sync_selection_buttons()

    def on_show(self) -> None:
        """Refresh profile statuses and active service form when Accounts is visited."""
        self._auth_coord.load_service_into_form()
        self._auth_coord.update_field_states()
        self.refresh()
        self._sync_active_row_and_breadcrumb()

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

    def invalidate_verification(self, service_key: str, profile: str | None=None) -> None:
        """Clear cached login verification status for `(service_key, profile)` or all of `service_key`."""
        if profile is None:
            to_drop = [k for k in self._verification_cache if k[0] == service_key]
            for k in to_drop:
                self._verification_cache.pop(k, None)
        else:
            self._verification_cache.pop((service_key, normalize_profile_name(profile)), None)

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
            auth_label = ", ".join(m.upper() if m == "oauth" else m.capitalize() for m in svc.auth_methods) if svc.auth_methods else "None"
            caps: list[str] = []
            if svc.supports_search:
                caps.append("Search")
            if svc.supports_library:
                caps.append("Library")
            if "cookies" in svc.auth_methods:
                caps.append("Cookies")
            if "oauth" in svc.auth_methods:
                caps.append("OAuth")
            caps_label = ", ".join(caps) if caps else "URL Only"

            for prof in profiles:
                src_cfg = cfg.sources.get(make_source_profile_key(svc.key, prof))
                oauth_connected = bool(src_cfg and getattr(src_cfg, "oauth_token_present", False))
                has_creds = bool(
                    src_cfg
                    and any(
                        (
                            src_cfg.username,
                            src_cfg.password,
                            src_cfg.library,
                            src_cfg.cookie_file,
                            src_cfg.oauth_client_id,
                            oauth_connected,
                        )
                    )
                )
                cached = self._verification_cache.get((svc.key, prof))
                if cached is not None and has_creds:
                    status, row_tag = cached
                elif oauth_connected and src_cfg and not any((src_cfg.username, src_cfg.password, src_cfg.cookie_file)):
                    status, row_tag = "OAuth Connected", ""
                elif oauth_connected:
                    status, row_tag = "Configured (OAuth)", ""
                elif has_creds:
                    status, row_tag = "Configured", ""
                elif not svc.auth_methods:
                    status, row_tag = "No auth needed", ""
                else:
                    status, row_tag = "Unconfigured", ""
                iid = f"{svc.key}:{prof}"
                self._rows.append((svc.key, prof))
                self._tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(svc.label, prof, auth_label, caps_label, vault_label, status),
                    tags=(row_tag,) if row_tag else (),
                )
        self._status_var.set(f"{len(self._rows)} account entries across {len(self._services)} services.")
        self._sync_selection_buttons()

    def _sync_selection_buttons(self) -> None:
        has_sel = self._selected_service_profile() is not None
        state = "normal" if has_sel else "disabled"
        self._rename_profile_btn.configure(state=state)
        self._delete_profile_btn.configure(state=state)
        self._edit_selected_btn.configure(state=state)

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
        svc_arg = picked[0] if picked else ""
        prof_arg = picked[1] if picked else None
        try:
            self._on_import_cookies(svc_arg, prof_arg)
        except TypeError:
            self._on_import_cookies(svc_arg)

    def _select_in_form(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        self._auth_coord.select_service_profile(picked[0], picked[1])
        self._sync_active_row_and_breadcrumb()

    def _new_profile(self) -> None:
        picked = self._selected_service_profile()
        if picked is not None:
            self._auth_coord.select_service_profile(picked[0], picked[1])
        if self._auth_coord.create_new_profile():
            self.refresh()
            self._sync_active_row_and_breadcrumb()

    def _rename_selected_profile(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        self._auth_coord.select_service_profile(picked[0], picked[1])
        if self._auth_coord.rename_current_profile():
            self.invalidate_verification(picked[0], picked[1])
            self.refresh()
            self._sync_active_row_and_breadcrumb()

    def _delete_selected_profile(self) -> None:
        picked = self._selected_service_profile()
        if picked is None:
            return
        svc_key, prof = picked
        if not messagebox.askyesno(
            "Delete Credentials",
            f"Remove saved credentials and OAuth tokens for {svc_key} ({prof})?",
            parent=self.winfo_toplevel(),
        ):
            return
        self.invalidate_verification(svc_key, prof)
        self._controller.delete_source_profile(svc_key, profile=prof)
        self._auth_coord.load_service_into_form()
        self._auth_coord.update_field_states()
        self.refresh()
        self._status_var.set(f"Removed credentials for {svc_key} ({prof}).")

    def _cancel_test_logins(self) -> None:
        if not self._is_testing:
            return
        self._cancel_test_btn.configure(state="disabled")
        self._status_var.set("Cancelling login verification...")
        self._controller.cancel_accounts_verification()

    def _test_all_logins(self) -> None:
        if self._is_testing:
            return
        cfg = self._controller.config_snapshot()
        targets: list[tuple[str, str, str]] = []
        for svc in self._services:
            if not svc.auth_methods:
                continue
            for prof in list_source_profiles(cfg, svc.key):
                src_cfg = cfg.sources.get(make_source_profile_key(svc.key, prof))
                if src_cfg and any(
                    (
                        src_cfg.username,
                        src_cfg.password,
                        src_cfg.cookie_file,
                        getattr(src_cfg, "oauth_token_present", False),
                    )
                ):
                    targets.append((svc.key, svc.label, prof))
        if not targets:
            self._status_var.set("No configured accounts with credentials to verify.")
            return
        self._is_testing = True
        self._test_all_btn.configure(state="disabled")
        self._cancel_test_btn.configure(state="normal")
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._status_var.set(f"Verifying {len(targets)} configured account(s)...")
        self._active_test_run_id = self._controller.start_accounts_verification(targets)

    def handle_verification_item(
        self,
        run_id: int,
        svc_key: str,
        prof: str,
        ok: bool,
        err: str | None,
    ) -> None:
        if run_id != self._active_test_run_id or not self.winfo_exists():
            return
        status_text = "Valid" if ok else (f"Failed: {err}" if err else "Failed")
        tag = "ok" if ok else "err"
        self._verification_cache[(svc_key, prof)] = (status_text, tag)
        iid = f"{svc_key}:{prof}"
        if self._tree.exists(iid):
            vals = tree_item_values(self._tree, iid)
            if len(vals) >= 6:
                vals[5] = status_text
                self._tree.item(iid, values=tuple(vals), tags=(tag,))

    def handle_verification_done(
        self,
        run_id: int,
        valid_count: int,
        checked_count: int,
        total_count: int,
        cancelled: bool,
    ) -> None:
        if run_id != self._active_test_run_id or not self.winfo_exists():
            return
        self._is_testing = False
        self._active_test_run_id = None
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._cancel_test_btn.configure(state="disabled")
        self._test_all_btn.configure(state="normal")
        if cancelled:
            self._status_var.set(
                f"Verification cancelled ({checked_count}/{total_count} checked, {valid_count} valid)."
            )
        else:
            self._status_var.set(f"Verified {valid_count}/{total_count} configured account(s).")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._is_testing:
            self._controller.cancel_accounts_verification()

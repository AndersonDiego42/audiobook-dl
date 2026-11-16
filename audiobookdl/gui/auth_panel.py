"""Service selection, multi-profile credentials, and cookie import panel."""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Sequence

from audiobookdl import args
from audiobookdl.config import (
    DEFAULT_PROFILE,
    SourceConfig,
    get_source_config,
    list_source_profiles,
    normalize_profile_name,
)
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.cookie_dialog import CookieImportDialog
from audiobookdl.gui.theme import ThemePalette
from audiobookdl.sources import ServiceInfo, detect_service_key


class ServiceAuthPanel(ttk.LabelFrame):
    """LabelFrame managing service/profile selection, login fields, and browser cookie import."""

    def __init__(
        self,
        master: tk.Misc,
        root: tk.Tk,
        controller: GuiController,
        services: Sequence[ServiceInfo],
        *,
        status_var: tk.StringVar,
        service_label_var: tk.StringVar,
        profile_var: tk.StringVar,
        service_hint_var: tk.StringVar,
        saved_badge_var: tk.StringVar,
        cookie_file_var: tk.StringVar,
        username_var: tk.StringVar,
        password_var: tk.StringVar,
        library_var: tk.StringVar,
        show_password_var: tk.BooleanVar,
        remember_login_var: tk.BooleanVar,
        palette_provider: Callable[[], ThemePalette],
        build_verify_options: Callable[[], Optional[args.DownloadOptions]],
        is_running_provider: Callable[[], bool],
        on_log: Callable[[str], None],
    ) -> None:
        super().__init__(master, text="Service & Authentication", padding=(10, 6))
        self._window = root
        self._controller = controller
        self._services = list(services)
        self._services_by_label = {s.label: s for s in self._services}
        self._services_by_key = {s.key: s for s in self._services}
        self._last_detected_service: Optional[str] = None
        self._is_verifying_auth = False
        self._cookie_dialog: Optional[CookieImportDialog] = None

        self._status_var = status_var
        self.service_label_var = service_label_var
        self.profile_var = profile_var
        self.service_hint_var = service_hint_var
        self.saved_badge_var = saved_badge_var
        self.cookie_file_var = cookie_file_var
        self.username_var = username_var
        self.password_var = password_var
        self.library_var = library_var
        self.show_password_var = show_password_var
        self.remember_login_var = remember_login_var

        self._palette_provider = palette_provider
        self._build_verify_options = build_verify_options
        self._is_running_provider = is_running_provider
        self._on_log = on_log

        self._build_widgets()

    @property
    def cookie_dialog(self) -> Optional[CookieImportDialog]:
        return self._cookie_dialog

    @cookie_dialog.setter
    def cookie_dialog(self, dlg: Optional[CookieImportDialog]) -> None:
        self._cookie_dialog = dlg

    @property
    def is_verifying_auth(self) -> bool:
        return self._is_verifying_auth

    @is_verifying_auth.setter
    def is_verifying_auth(self, value: bool) -> None:
        self._is_verifying_auth = value

    def _build_widgets(self) -> None:
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        ttk.Label(self, text="Service:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        service_row = ttk.Frame(self)
        service_row.grid(row=0, column=1, columnspan=4, sticky="ew", pady=(0, 4))
        service_row.columnconfigure(3, weight=1)

        self.service_combo = ttk.Combobox(
            service_row,
            textvariable=self.service_label_var,
            values=[s.label for s in self._services],
            state="readonly",
            width=22,
        )
        self.service_combo.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.service_combo.bind("<<ComboboxSelected>>", self.on_service_selected)

        ttk.Label(service_row, text="Profile:").grid(row=0, column=1, sticky="w", padx=(0, 4))
        self.profile_combo = ttk.Combobox(
            service_row,
            textvariable=self.profile_var,
            values=[DEFAULT_PROFILE],
            state="normal",
            width=12,
        )
        self.profile_combo.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_changed)
        self.profile_combo.bind("<Return>", self.on_profile_changed)
        self.profile_combo.bind("<FocusOut>", self.on_profile_changed)

        ttk.Label(service_row, textvariable=self.service_hint_var, style="Muted.TLabel").grid(
            row=0, column=3, sticky="w"
        )
        ttk.Label(service_row, textvariable=self.saved_badge_var, style="Success.TLabel").grid(
            row=0, column=4, sticky="e", padx=(8, 0)
        )

        ttk.Label(self, text="Cookies File:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.cookie_entry = ttk.Entry(self, textvariable=self.cookie_file_var)
        self.cookie_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(0, 4))
        cookie_btn_frame = ttk.Frame(self)
        cookie_btn_frame.grid(row=1, column=3, columnspan=2, sticky="e", pady=(0, 4))
        self.cookie_browse = ttk.Button(cookie_btn_frame, text="Browse...", command=self.browse_cookie_file)
        self.cookie_browse.pack(side=tk.LEFT)
        self.cookie_import_btn = ttk.Button(
            cookie_btn_frame,
            text="Import...",
            command=self.show_cookie_import_dialog,
        )
        self.cookie_import_btn.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(self, text="Username:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.user_entry = ttk.Entry(self, textvariable=self.username_var)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 4))

        ttk.Label(self, text="Password:").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(0, 4))
        self.password_entry = ttk.Entry(self, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=2, column=3, sticky="ew", pady=(0, 4))
        self.show_pw_btn = ttk.Checkbutton(
            self,
            text="Show",
            variable=self.show_password_var,
            command=self.toggle_password_visibility,
        )
        self.show_pw_btn.grid(row=2, column=4, padx=(8, 0), pady=(0, 4))

        ttk.Label(self, text="Library:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.lib_entry = ttk.Entry(self, textvariable=self.library_var, width=14)
        self.lib_entry.grid(row=3, column=1, sticky="ew", padx=(0, 10))

        auth_actions = ttk.Frame(self)
        auth_actions.grid(row=3, column=2, columnspan=3, sticky="ew")
        self.remember_cb = ttk.Checkbutton(
            auth_actions,
            text="Remember login",
            variable=self.remember_login_var,
        )
        self.remember_cb.pack(side=tk.LEFT)
        self.forget_auth_btn = ttk.Button(
            auth_actions, text="Forget", command=self.forget_current_credentials
        )
        self.forget_auth_btn.pack(side=tk.RIGHT)
        self.save_auth_btn = ttk.Button(
            auth_actions, text="Save", command=self.save_current_credentials
        )
        self.save_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.test_auth_btn = ttk.Button(
            auth_actions, text="Test Login", command=self.verify_current_auth
        )
        self.test_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))

    def selected_service(self) -> Optional[ServiceInfo]:
        return self._services_by_label.get(self.service_label_var.get())

    def service_label_for_url(self, url: str) -> str:
        detected = detect_service_key(url)
        if detected and detected in self._services_by_key:
            return self._services_by_key[detected].label
        return detected or "Unknown"

    def on_service_selected(self, _event: Optional[tk.Event] = None) -> None:
        self.profile_var.set(DEFAULT_PROFILE)
        self.load_service_into_form()
        self.update_field_states()

    def on_profile_changed(self, _event: Optional[tk.Event] = None) -> None:
        normalized = normalize_profile_name(self.profile_var.get())
        if self.profile_var.get() != normalized:
            self.profile_var.set(normalized)
        self.load_service_into_form()
        self.update_field_states()

    def on_urls_changed(self, urls: Sequence[str]) -> None:
        if not urls:
            self._last_detected_service = None
            return
        detected = detect_service_key(urls[0])
        if detected and detected != self._last_detected_service:
            self._last_detected_service = detected
            service = self._services_by_key.get(detected)
            if service and self.service_label_var.get() != service.label:
                self.service_label_var.set(service.label)
                self.profile_var.set(DEFAULT_PROFILE)
                self.load_service_into_form()
                self.update_field_states()

    def load_service_into_form(self) -> None:
        """Populate form fields and badge from saved config for the selected service/profile."""
        service = self.selected_service()
        if service is None:
            return
        cfg = self._controller.config
        configured_profiles = list_source_profiles(cfg, service.key)
        combo_values = [DEFAULT_PROFILE] + [p for p in configured_profiles if p != DEFAULT_PROFILE]
        self.profile_combo.configure(values=combo_values)
        active_profile = normalize_profile_name(self.profile_var.get())
        if self.profile_var.get() != active_profile:
            self.profile_var.set(active_profile)

        saved: Optional[SourceConfig] = get_source_config(cfg, service.key, active_profile)
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

    def set_running_state(self, running: bool) -> None:
        """Disable all auth controls while running, or restore per-service states when idle."""
        if running:
            for widget in (
                self.service_combo,
                self.profile_combo,
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
            ):
                widget.configure(state="disabled")
        else:
            self.service_combo.configure(state="readonly")
            self.remember_cb.configure(state="normal")
            self.update_field_states()

    def update_field_states(self) -> None:
        """Enable/disable credential fields based on the active service's auth requirements."""
        if self._is_running_provider():
            return
        service = self.selected_service()
        if service is None:
            return
        supports_cookies = "cookies" in service.auth_methods
        supports_login = "login" in service.auth_methods
        needs_library = supports_login and "library" in service.login_fields
        has_auth = bool(service.auth_methods)

        cookie_state = "normal" if supports_cookies else "disabled"
        login_state = "normal" if supports_login else "disabled"
        library_state = "normal" if needs_library else "disabled"
        save_state = "normal" if has_auth else "disabled"

        self.profile_combo.configure(state="normal" if has_auth else "disabled")
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
        active_profile = normalize_profile_name(self.profile_var.get())
        has_saved = get_source_config(self._controller.config, service.key, active_profile) is not None
        self.forget_auth_btn.configure(state="normal" if has_saved else "disabled")

    def toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def browse_cookie_file(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self._window,
            title="Select Netscape cookie file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self.cookie_file_var.set(chosen)

    def show_cookie_import_dialog(self) -> None:
        """Open or raise the interactive browser-cookie importer dialog."""
        if self._cookie_dialog is not None and self._cookie_dialog.winfo_exists():
            self._cookie_dialog.lift()
            self._cookie_dialog.focus_set()
            return
        selected = self.selected_service()
        initial_key = selected.key if (selected and "cookies" in selected.auth_methods) else ""
        initial_path = self.cookie_file_var.get().strip() if initial_key else ""
        self._cookie_dialog = CookieImportDialog(
            self._window,
            self._services,
            initial_service_key=initial_key,
            initial_cookie_path=initial_path,
            on_apply=self.on_cookies_imported,
            palette=self._palette_provider(),
        )

    def on_cookies_imported(self, service_key: str, cookie_path: str, count: int) -> None:
        """Apply an imported Netscape cookie file to the selected service form."""
        service = self._services_by_key.get(service_key)
        if service is not None and self.service_label_var.get() != service.label:
            self.service_label_var.set(service.label)
            self.load_service_into_form()
            self.update_field_states()
        self.cookie_file_var.set(cookie_path)
        if self.remember_login_var.get() and service is not None and "cookies" in service.auth_methods:
            self.save_current_credentials(quiet=True)
        self._status_var.set(f"Imported {count} cookies to {os.path.basename(cookie_path)}")
        self._on_log(f"Imported {count} browser cookie(s) to {cookie_path}")

    def save_current_credentials(self, quiet: bool = False) -> None:
        """Persist the selected service's login, password, library, and cookie path."""
        service = self.selected_service()
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

        active_profile = normalize_profile_name(self.profile_var.get())
        cfg = self._controller.save_source_credentials(
            service.key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            profile=active_profile,
        )
        has_saved = get_source_config(cfg, service.key, active_profile) is not None
        self.saved_badge_var.set("Saved" if has_saved else "")
        self.load_service_into_form()
        self.update_field_states()
        profile_suffix = f" ({active_profile})" if active_profile != DEFAULT_PROFILE else ""
        if not quiet:
            if has_saved:
                self._status_var.set(f"Saved credentials for {service.label}{profile_suffix}")
                self._on_log(f"Saved login settings for {service.label}{profile_suffix}.")
            else:
                self._status_var.set(f"Cleared credentials for {service.label}{profile_suffix}")

    def forget_current_credentials(self) -> None:
        """Remove saved credentials for the currently selected service and profile."""
        service = self.selected_service()
        if service is None:
            return
        active_profile = normalize_profile_name(self.profile_var.get())
        self._controller.save_source_credentials(service.key, profile=active_profile)
        self.username_var.set("")
        self.password_var.set("")
        self.library_var.set("")
        self.cookie_file_var.set("")
        self.saved_badge_var.set("")
        self.load_service_into_form()
        self.update_field_states()
        profile_suffix = f" ({active_profile})" if active_profile != DEFAULT_PROFILE else ""
        self._status_var.set(f"Removed saved login for {service.label}{profile_suffix}")
        self._on_log(f"Removed saved login settings for {service.label}{profile_suffix}.")

    def verify_current_auth(self) -> None:
        """Test authentication for the currently selected service in the background."""
        if self._is_verifying_auth or self._is_running_provider():
            return
        service = self.selected_service()
        if service is None or not service.auth_methods:
            return
        options = self._build_verify_options()
        if options is None:
            return
        self._is_verifying_auth = True
        self.test_auth_btn.configure(state="disabled")
        self._status_var.set(f"Testing login for {service.label}...")
        self._controller.start_auth_verification(service.key, service.label, options)

    def handle_auth_verified(
        self,
        service_label: str,
        ok: bool,
        auth_exc: Optional[Exception],
    ) -> None:
        """Finish background login verification and notify the user."""
        self._is_verifying_auth = False
        self.update_field_states()
        if auth_exc is not None:
            self._status_var.set(f"Login failed for {service_label}")
            self._on_log(f"[ERROR] Login check failed for {service_label}: {auth_exc}")
            messagebox.showerror("Test Login Failed", str(auth_exc), parent=self._window)
        elif ok:
            self._status_var.set(f"Login OK: {service_label}")
            self._on_log(f"Verified login for {service_label}.")
            messagebox.showinfo("Test Login", f"Authentication succeeded for {service_label}.", parent=self._window)
        else:
            self._status_var.set(f"Login invalid: {service_label}")
            messagebox.showwarning("Test Login", f"Authentication failed for {service_label}.", parent=self._window)

    def validate_cookie_file(self) -> bool:
        """Verify that any entered cookie file exists on disk."""
        service = self.selected_service()
        cookie_file = (
            self.cookie_file_var.get().strip()
            if (service is None or "cookies" in service.auth_methods)
            else ""
        )
        if cookie_file and not os.path.isfile(cookie_file):
            self._status_var.set("Cookie file not found")
            messagebox.showerror("Invalid Cookie File", f"File not found:\n{cookie_file}", parent=self._window)
            return False
        return True

    def warn_mixed_service_credentials(self, urls: Sequence[str]) -> bool:
        """Warn before starting if batch URLs target an unconfigured auth service alongside another."""
        selected = self.selected_service()
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
            src_cfg = get_source_config(cfg, key)
            if src_cfg is None:
                for prof in list_source_profiles(cfg, key):
                    src_cfg = get_source_config(cfg, key, prof)
                    if src_cfg is not None:
                        break
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
            parent=self._window,
        )

    def extract_options_auth(self, urls: Sequence[str]) -> Dict[str, Optional[str]]:
        """Extract credential overrides when `urls` match or do not conflict with selected service."""
        detected_keys = {k for k in (detect_service_key(u) for u in urls) if k}
        service = self.selected_service()
        apply_form_auth = (
            service is not None
            and (len(detected_keys) <= 1)
            and (not detected_keys or service.key in detected_keys)
        )
        result: Dict[str, Optional[str]] = {
            "username": None,
            "password": None,
            "library": None,
            "cookie_file": None,
            "profile": None,
        }
        if apply_form_auth and service is not None:
            norm_profile = normalize_profile_name(self.profile_var.get())
            if norm_profile != DEFAULT_PROFILE:
                result["profile"] = norm_profile
            if "cookies" in service.auth_methods:
                result["cookie_file"] = self.cookie_file_var.get().strip() or None
            if "login" in service.auth_methods:
                result["username"] = self.username_var.get().strip() or None
                result["password"] = self.password_var.get() or None
                if "library" in service.login_fields:
                    result["library"] = self.library_var.get().strip() or None
        return result

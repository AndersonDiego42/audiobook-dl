"""Interactive modal dialog for importing browser cookies into a Netscape cookie file."""

from __future__ import annotations

from datetime import datetime, timezone
import http.cookiejar
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl.gui.theme import ThemePalette, scale_px
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ModalDialog,
    ScrolledTreeview,
    TreeColumnSpec,
)
from audiobookdl.sources import ServiceInfo
from audiobookdl.utils.browser_cookies import (
    BROWSER_LABELS,
    CookieImportResult,
    default_domains_for_service,
    default_imported_cookie_path,
    detect_available_browsers,
    export_cookies_netscape,
    extract_browser_cookies,
    parse_domain_filter,
)


def _format_expiry(expires: int | None) -> str:
    if expires is None or expires <= 0:
        return "Session"
    try:
        dt = datetime.fromtimestamp(expires, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return str(expires)


def _format_cookie_flags(cookie: http.cookiejar.Cookie) -> str:
    flags: list[str] = []
    if cookie.secure:
        flags.append("Secure")
    if "HttpOnly" in getattr(cookie, "_rest", {}):
        flags.append("HttpOnly")
    return ", ".join(flags) if flags else "Standard"


class CookieImportDialog(ModalDialog):
    """Modal `Toplevel` allowing users to scan browser profiles and export Netscape cookies."""

    _COLUMNS = (
        TreeColumnSpec("domain", "Domain", width=170, stretch=True, anchor="w"),
        TreeColumnSpec("name", "Cookie Name", width=150, stretch=True, anchor="w"),
        TreeColumnSpec("flags", "Flags", width=110, stretch=False, anchor="w"),
        TreeColumnSpec("expires", "Expires", width=95, stretch=False, anchor="center"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        services: Sequence[ServiceInfo],
        *,
        initial_service_key: str = "",
        initial_cookie_path: str = "",
        on_apply: Callable[[str, str, int], None],
        palette: ThemePalette | None = None,
        extractor: Callable[[str, Sequence[str]], CookieImportResult] = extract_browser_cookies,
        browser_detector: Callable[[], dict[str, bool]] = detect_available_browsers,
    ) -> None:
        super().__init__(
            parent,
            "Import Browser Cookies",
            palette=palette,
            default_size=(660, 480),
            min_size=(620, 440),
        )
        self._cookie_services = [s for s in services if "cookies" in s.auth_methods]
        if not self._cookie_services:
            self._cookie_services = list(services)
        self._services_by_label = {s.label: s for s in self._cookie_services}
        self._services_by_key = {s.key: s for s in self._cookie_services}
        self._on_apply = on_apply
        self._extractor = extractor
        self._detected_browsers = browser_detector()

        self._matched_cookies: list[http.cookiejar.Cookie] = []
        self._is_scanning = False
        self._cb_queue = MainThreadCallbackQueue(self)

        self._browser_key_by_display: dict[str, str] = {}
        self._browser_displays: list[str] = []
        default_browser_display = ""
        for key, label in BROWSER_LABELS:
            display = f"{label} (Detected)" if self._detected_browsers.get(key) else label
            self._browser_key_by_display[display] = key
            self._browser_displays.append(display)
            if not default_browser_display and self._detected_browsers.get(key):
                default_browser_display = display
        if not default_browser_display and self._browser_displays:
            default_browser_display = self._browser_displays[0]

        initial_service = self._services_by_key.get(initial_service_key)
        if initial_service is None and self._cookie_services:
            initial_service = self._cookie_services[0]

        self._service_var = tk.StringVar(value=initial_service.label if initial_service else "")
        self._browser_var = tk.StringVar(value=default_browser_display)
        initial_domains = (
            ", ".join(default_domains_for_service(initial_service.key))
            if initial_service
            else ""
        )
        self._domains_var = tk.StringVar(value=initial_domains)
        auto_default_path = default_imported_cookie_path(initial_service.key) if initial_service else ""
        default_path = initial_cookie_path.strip() or auto_default_path
        self._last_auto_output_path = auto_default_path
        self._custom_output_path = bool(
            initial_cookie_path.strip() and initial_cookie_path.strip() != auto_default_path
        )
        self._output_var = tk.StringVar(value=default_path)
        self._status_var = tk.StringVar(
            value="Select your browser and click 'Scan Browser' to preview matching cookies."
        )

        body = ttk.Frame(self, padding=scale_px(self, 14))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(5, weight=1)

        ttk.Label(
            body,
            text="Extract session cookies for cookie-based audiobook services into a private Netscape cookies.txt file.",
            style="Muted.TLabel",
            wraplength=scale_px(self, 580),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, scale_px(self, 10)))

        ttk.Label(body, text="Service:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self._service_combo = ttk.Combobox(
            body,
            textvariable=self._service_var,
            values=[s.label for s in self._cookie_services],
            state="readonly",
            width=26,
        )
        self._service_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 6))
        self._service_combo.bind("<<ComboboxSelected>>", self._on_service_changed)

        ttk.Label(body, text="Browser:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self._browser_combo = ttk.Combobox(
            body,
            textvariable=self._browser_var,
            values=self._browser_displays,
            state="readonly",
        )
        self._browser_combo.grid(row=2, column=1, sticky="ew", pady=(0, 6))
        self._scan_btn = ttk.Button(body, text="Scan Browser", command=self.start_scan)
        self._scan_btn.grid(row=2, column=2, padx=(8, 0), pady=(0, 6))

        ttk.Label(body, text="Domains:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self._domains_entry = ttk.Entry(body, textvariable=self._domains_var)
        self._domains_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(body, text="Save To:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        self._output_entry = ttk.Entry(body, textvariable=self._output_var)
        self._output_entry.grid(row=4, column=1, sticky="ew", pady=(0, 8))
        self._output_entry.bind("<KeyRelease>", self._on_output_edited)
        self._browse_btn = ttk.Button(body, text="Browse...", command=self._browse_output)
        self._browse_btn.grid(row=4, column=2, padx=(8, 0), pady=(0, 8))

        preview = ScrolledTreeview(body, self._COLUMNS, selectmode="browse", height=9)
        preview.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        self._preview = preview
        self._tree = preview.tree

        self._status_label = ttk.Label(
            body,
            textvariable=self._status_var,
            style="Muted.TLabel",
            wraplength=scale_px(self, 580),
        )
        self._status_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self._loading_bar.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self._loading_bar.grid_remove()

        btn_row = ttk.Frame(body)
        btn_row.grid(row=8, column=0, columnspan=3, sticky="ew")
        ttk.Button(btn_row, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._save_btn = ttk.Button(
            btn_row,
            text="Save & Apply",
            command=self.apply_and_close,
            state="disabled",
            style="Accent.TButton",
        )
        self._save_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._preview.set_empty_state(
            True, "Click 'Scan Browser' to preview matching cookies."
        )

        self.finalize_modal(initial_focus=self._scan_btn)

    def _current_service(self) -> ServiceInfo | None:
        return self._services_by_label.get(self._service_var.get())

    def _selected_browser_key(self) -> str:
        return self._browser_key_by_display.get(self._browser_var.get(), "firefox")

    def select_service(self, service_key: str) -> None:
        """Select `service_key` programmatically and refresh domains, output path, and preview."""
        service = self._services_by_key.get(service_key)
        if service is None:
            return
        self._service_var.set(service.label)
        self._on_service_changed()

    def _on_output_edited(self, _event: tk.Event | None = None) -> None:
        current = self._output_var.get().strip()
        self._custom_output_path = bool(current and current != self._last_auto_output_path)

    def _on_service_changed(self, _event: tk.Event | None = None) -> None:
        service = self._current_service()
        if service is None:
            return
        self._domains_var.set(", ".join(default_domains_for_service(service.key)))
        current_output = self._output_var.get().strip()
        is_custom = bool(current_output and current_output != self._last_auto_output_path)
        new_auto_path = default_imported_cookie_path(service.key)
        self._last_auto_output_path = new_auto_path
        if not is_custom:
            self._custom_output_path = False
            self._output_var.set(new_auto_path)
        else:
            self._custom_output_path = True
        self._clear_preview()
        self._set_status(
            "Service changed. Click 'Scan Browser' to look for matching cookies.",
            "Muted.TLabel",
        )

    def _browse_output(self) -> None:
        current = self._output_var.get().strip()
        initial_dir = os.path.dirname(current) if current else os.path.expanduser("~")
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Save imported Netscape cookie file",
            defaultextension=".txt",
            initialdir=initial_dir if os.path.isdir(initial_dir) else os.path.expanduser("~"),
            initialfile=os.path.basename(current) if current else "cookies.txt",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self._custom_output_path = True
            self._output_var.set(chosen)

    def _set_status(self, message: str, style_name: str) -> None:
        self._status_var.set(message)
        self._status_label.configure(style=style_name)

    def _clear_preview(
        self,
        *,
        empty_text: str | None = "Click 'Scan Browser' to preview matching cookies.",
    ) -> None:
        self._matched_cookies = []
        for item_id in self._tree.get_children():
            self._tree.delete(item_id)
        self._save_btn.configure(state="disabled")
        if hasattr(self, "_preview"):
            if empty_text:
                self._preview.set_empty_state(True, empty_text)
            else:
                self._preview.set_empty_state(False)

    def start_scan(self) -> None:
        """Validate domain filters and begin scanning the selected browser asynchronously."""
        if self._is_scanning:
            return
        domains = parse_domain_filter(self._domains_var.get())
        if not domains:
            self._set_status(
                "Enter at least one domain to match (for example: overdrive.com, libbyapp.com).",
                "Error.TLabel",
            )
            return

        browser_key = self._selected_browser_key()
        self._is_scanning = True
        self._clear_preview(empty_text=None)
        self._scan_btn.configure(state="disabled")
        self._service_combo.configure(state="disabled")
        self._browser_combo.configure(state="disabled")
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self._set_status(f"Scanning {self._browser_var.get()} cookies...", "Muted.TLabel")

        self._cb_queue.begin_task()

        def _worker() -> None:
            result: CookieImportResult | None = None
            err: Exception | None = None
            try:
                result = self._extractor(browser_key, domains)
            except Exception as exc:
                err = exc
            finally:
                def _post_finish() -> None:
                    self._on_scan_finished(result, err)

                self._cb_queue.post(_post_finish)
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True, name="abdl-cookie-scan").start()

    def _on_scan_finished(
        self,
        result: CookieImportResult | None,
        error: Exception | None,
    ) -> None:
        if not self.winfo_exists():
            return
        self._is_scanning = False
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._scan_btn.configure(state="normal")
        self._service_combo.configure(state="readonly")
        self._browser_combo.configure(state="readonly")

        if error is not None:
            self._clear_preview(empty_text="Scan failed. Check status above and try again.")
            self._set_status(str(error), "Error.TLabel")
            return

        if result is None or not result.cookies:
            self._clear_preview(
                empty_text="No matching cookies found. Sign in to the service in your browser and scan again."
            )
            domains_text = ", ".join(result.domains) if result and result.domains else self._domains_var.get()
            self._set_status(
                f"No cookies matched ({domains_text}). Sign in to the service in {self._browser_var.get()} first or adjust the domains.",
                "Error.TLabel",
            )
            return

        self._preview.set_empty_state(False)
        self._matched_cookies = list(result.cookies)
        for item_id in self._tree.get_children():
            self._tree.delete(item_id)
        for idx, cookie in enumerate(self._matched_cookies):
            self._tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    cookie.domain or "",
                    cookie.name or "",
                    _format_cookie_flags(cookie),
                    _format_expiry(cookie.expires),
                ),
            )
        self._save_btn.configure(state="normal")
        summary = f"Found {len(self._matched_cookies)} matching cookie{'s' if len(self._matched_cookies) != 1 else ''}"
        if result.profile_used:
            summary += f" (profile: {result.profile_used})"
        if result.warnings:
            summary += f". Note: {result.warnings[0]}"
        else:
            summary += ". Click 'Save & Apply' to use them."
        self._set_status(summary, "Success.TLabel")

    def apply_and_close(self) -> None:
        """Write matched cookies to the Netscape target file and apply to the main form."""
        if not self._matched_cookies:
            self._set_status("Scan and match at least one cookie before saving.", "Error.TLabel")
            return
        target = self._output_var.get().strip()
        if not target:
            self._set_status("Choose an output file path for the Netscape cookies.", "Error.TLabel")
            return
        service = self._current_service()
        service_key = service.key if service else ""
        try:
            written = export_cookies_netscape(self._matched_cookies, target)
        except OSError as exc:
            self._set_status(f"Failed to write cookie file: {exc}", "Error.TLabel")
            messagebox.showerror(
                "Cookie Export Failed",
                f"Could not write cookies to:\n{target}\n\n{exc}",
                parent=self,
            )
            return
        count = len(self._matched_cookies)
        self._on_apply(service_key, written, count)
        self.destroy()

"""Modal dialog for inspecting DRM / PSSH / tenc protection metadata of a file or URL."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Literal

from audiobookdl.exceptions import AudiobookDLException
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ModalDialog,
    bind_adaptive_wraplength,
)
from audiobookdl.utils.drm_inspect import (
    ProtectionReport,
    format_report,
    inspect_file,
    inspect_url,
)
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    create_configured_http_session,
    create_impersonated_session,
)

_EMPTY_HINT = (
    "Enter a local file path (.mp4, .m4b, .m4s, .m3u8, .mpd) or an HTTP(S) URL,\n"
    "then press Enter or click Inspect to view DRM systems, PSSH boxes, and KIDs."
)


class DrmInspectDialog(ModalDialog):
    """Modal dialog inspecting PSSH, `tenc`, HLS, or DASH MPD protection metadata."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        palette: ThemePalette | None = None,
        initial_target: str = "",
        proxy: str | None = None,
        timeout: float | None = None,
        impersonate: str | None = None,
    ) -> None:
        super().__init__(
            parent,
            "Inspect DRM / PSSH",
            palette=palette or LIGHT_PALETTE,
            default_size=(640, 460),
            min_size=(520, 360),
        )
        self._proxy = proxy
        self._timeout = timeout
        self._impersonate = impersonate
        self._callbacks = MainThreadCallbackQueue(self)
        self._request_seq = 0
        self._last_report: ProtectionReport | None = None
        self._state: Literal["empty", "loading", "error", "success"] = "empty"

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(4, weight=1)

        ttk.Label(
            body,
            text="DRM / PSSH Inspector (parse-only; no license acquisition)",
            font=make_bold_font(self, 1),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, scale_px(self, 8)))

        ttk.Label(body, text="File or URL:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
        )
        self.target_var = tk.StringVar(value=initial_target)
        self.target_entry = ttk.Entry(body, textvariable=self.target_var)
        self.target_entry.grid(row=1, column=1, sticky="ew", pady=(0, 6))
        self.target_entry.bind("<Return>", lambda _e: self.inspect_target())

        self.browse_btn = ttk.Button(body, text="Browse...", command=self._browse_file)
        self.browse_btn.grid(row=1, column=2, padx=(6, 0), pady=(0, 6))

        self.inspect_btn = ttk.Button(body, text="Inspect", command=self.inspect_target)
        self.inspect_btn.grid(row=1, column=3, padx=(6, 0), pady=(0, 6))

        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = ttk.Label(
            body,
            textvariable=self.status_var,
            style="Muted.TLabel",
            wraplength=scale_px(self, 580),
            justify="left",
        )
        self.status_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, scale_px(self, 6)))
        bind_adaptive_wraplength(body, self.status_label, margin_dp=24, min_dp=280)

        self.loading_bar = ttk.Progressbar(body, mode="indeterminate")
        self.loading_bar.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(0, scale_px(self, 6)))
        self.loading_bar.grid_remove()

        self.result_text = ScrolledText(body, wrap="word", height=14, state="disabled")
        self.result_text.grid(row=4, column=0, columnspan=4, sticky="nsew")

        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.copy_kids_btn = ttk.Button(
            btns,
            text="Copy KIDs",
            command=self._copy_kids,
            state="disabled",
        )
        self.copy_kids_btn.pack(side=tk.LEFT)
        self.close_btn = ttk.Button(btns, text="Close", command=self.destroy)
        self.close_btn.pack(side=tk.RIGHT)

        self._set_empty_state()
        self.finalize_modal(initial_focus=self.target_entry)

    @property
    def ui_state(self) -> Literal["empty", "loading", "error", "success"]:
        """Return the current dialog state (`empty`, `loading`, `error`, or `success`)."""
        return self._state

    @property
    def last_report(self) -> ProtectionReport | None:
        """Return the most recent `ProtectionReport`, if inspection succeeded."""
        return self._last_report

    def _set_result_text(self, content: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", content)
        self.result_text.configure(state="disabled")

    def _set_controls_busy(self, busy: bool) -> None:
        entry_state = "disabled" if busy else "normal"
        self.target_entry.configure(state=entry_state)
        self.browse_btn.configure(state=entry_state)
        self.inspect_btn.configure(state=entry_state)

    def _hide_loading_bar(self) -> None:
        try:
            self.loading_bar.stop()
            self.loading_bar.grid_remove()
        except tk.TclError:
            pass

    def _set_empty_state(self) -> None:
        self._state = "empty"
        self._last_report = None
        self._hide_loading_bar()
        self.status_label.configure(style="Muted.TLabel")
        self.status_var.set("Ready — enter a local file path or HTTP(S) URL.")
        self.copy_kids_btn.configure(state="disabled")
        self._set_result_text(_EMPTY_HINT)

    def _set_loading_state(self, target: str) -> None:
        self._state = "loading"
        self._last_report = None
        self.status_label.configure(style="Muted.TLabel")
        self.status_var.set(f"Inspecting {target}...")
        self.copy_kids_btn.configure(state="disabled")
        self._set_controls_busy(True)
        self.loading_bar.grid()
        self.loading_bar.start(12)
        self._set_result_text("Inspecting target, please wait...")

    def _set_error_state(self, message: str) -> None:
        self._state = "error"
        self._last_report = None
        self._hide_loading_bar()
        self._set_controls_busy(False)
        self.copy_kids_btn.configure(state="disabled")
        self.status_label.configure(style="Error.TLabel")
        self.status_var.set(message)
        self._set_result_text(f"Inspection failed:\n{message}")

    def _set_success_state(self, report: ProtectionReport) -> None:
        self._state = "success"
        self._last_report = report
        self._hide_loading_bar()
        self._set_controls_busy(False)
        self.status_label.configure(style="Muted.TLabel")
        kid_count = len(report.key_ids)
        if report.encrypted:
            self.status_var.set(
                f"Inspection complete — detected {len(report.pssh_boxes)} PSSH box(es), {kid_count} KID(s)."
            )
        else:
            self.status_var.set("Inspection complete — no DRM/PSSH metadata detected.")
        self.copy_kids_btn.configure(state="normal" if report.key_ids else "disabled")
        self._set_result_text(format_report(report))

    def _browse_file(self) -> None:
        current = self.target_var.get().strip()
        initial_dir = (
            os.path.dirname(current)
            if current and "://" not in current and os.path.dirname(current)
            else os.getcwd()
        )
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Select Media File, HLS Playlist, or DASH MPD",
            initialdir=initial_dir,
            filetypes=[
                ("Supported media & manifests", "*.mp4 *.m4b *.m4a *.m4s *.m4v *.mov *.m3u8 *.mpd"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            self.target_var.set(chosen)
            self.inspect_target()

    def _copy_kids(self) -> None:
        if self._last_report is None or not self._last_report.key_ids:
            return
        formatted = ",".join(kid.hex() for kid in self._last_report.key_ids)
        self.clipboard_clear()
        self.clipboard_append(formatted)
        self.status_label.configure(style="Muted.TLabel")
        self.status_var.set(f"Copied {len(self._last_report.key_ids)} KID(s) to clipboard.")

    def _run_inspection(self, target: str) -> ProtectionReport:
        lower = target.lower()
        if lower.startswith(("http://", "https://")):
            req_timeout: float | tuple[float, float] = (
                (DEFAULT_HTTP_TIMEOUT[0], float(self._timeout))
                if self._timeout and self._timeout > 0
                else DEFAULT_HTTP_TIMEOUT
            )
            session = (
                create_impersonated_session(self._impersonate, proxy=self._proxy)
                if self._impersonate
                else create_configured_http_session(proxy=self._proxy)
            )
            try:
                return inspect_url(target, session=session, timeout=req_timeout)
            finally:
                session.close()
        return inspect_file(target)

    def inspect_target(self) -> None:
        """Validate the input field and launch background inspection for the file or URL."""
        target = self.target_var.get().strip()
        if not target:
            self._set_empty_state()
            return

        self._request_seq += 1
        seq = self._request_seq
        self._set_loading_state(target)
        self._callbacks.begin_task()

        def _worker() -> None:
            report: ProtectionReport | None = None
            err_msg: str | None = None
            try:
                report = self._run_inspection(target)
            except AudiobookDLException as exc:
                err_msg = str(exc).strip() or exc.error_description
            except Exception as exc:
                err_msg = str(exc).strip() or type(exc).__name__
            finally:
                def _deliver() -> None:
                    try:
                        if not self.winfo_exists() or seq != self._request_seq:
                            return
                    except tk.TclError:
                        return
                    if err_msg is not None:
                        self._set_error_state(err_msg)
                    elif report is not None:
                        self._set_success_state(report)

                self._callbacks.post(_deliver)
                self._callbacks.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

from audiobookdl import __version__, args, logging
from audiobookdl.__main__ import DownloadSummary, run_downloads
from audiobookdl.config import (
    Config,
    SourceConfig,
    load_config,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.sources import ServiceInfo, detect_service_key, get_available_services
from audiobookdl.utils import find_executable

import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, List, Optional, Sequence


OUTPUT_FORMATS = ("Original", "m4b", "mp3", "m4a", "flac", "opus")


def default_output_dir() -> str:
    """Return the default output directory (user Downloads folder if it exists)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return str(downloads)
    return os.getcwd()


def parse_url_text(raw: str) -> List[str]:
    """Extract non-empty URLs from whitespace/newline-separated input text."""
    return [token.strip() for token in raw.split() if token.strip()]


class _GuiLogSink:
    """Forwards core log and progress events onto the Tkinter event queue."""

    def __init__(self, event_queue: "queue.Queue[tuple]") -> None:
        self._queue = event_queue

    def on_log(self, level: str, message: str) -> None:
        self._queue.put(("log", level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        self._queue.put(("progress_start", description, total))

    def on_progress_advance(self, completed: float, total: float) -> None:
        self._queue.put(("progress_advance", completed, total))

    def on_progress_finish(self) -> None:
        self._queue.put(("progress_finish",))


class _GuiInteractionHandler:
    """Bridges worker-thread prompts to Tkinter modal dialogs on the main thread."""

    def __init__(
        self,
        event_queue: "queue.Queue[tuple]",
        cancel_event: threading.Event,
    ) -> None:
        self._queue = event_queue
        self._cancel_event = cancel_event
        self._pending_events: List[threading.Event] = []
        self._lock = threading.Lock()

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        if self._cancel_event.is_set():
            raise DownloadCancelled
        done = threading.Event()
        holder: dict = {"value": None}
        with self._lock:
            self._pending_events.append(done)
        self._queue.put(("prompt_input", prompt, hidden, holder, done))
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        if self._cancel_event.is_set() or holder["value"] is None:
            raise DownloadCancelled
        return str(holder["value"])

    def confirm(self, message: str) -> bool:
        if self._cancel_event.is_set():
            return False
        done = threading.Event()
        holder: dict = {"value": False}
        with self._lock:
            self._pending_events.append(done)
        self._queue.put(("prompt_confirm", logging.strip_markup(message), holder, done))
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        return bool(holder["value"])

    def release_all(self) -> None:
        """Unblock any waiting prompt threads when the window closes or cancels."""
        with self._lock:
            for ev in self._pending_events:
                ev.set()


class AudiobookDLApp:
    """Simple Tkinter desktop UI for audiobook-dl."""

    def __init__(self, root: tk.Tk, config_location: Optional[str] = None) -> None:
        self.root = root
        self._config_location = config_location
        self.root.title(f"audiobook-dl {__version__}")
        self.root.geometry("760x680")
        self.root.minsize(660, 600)

        self._style = ttk.Style()
        if "vista" in self._style.theme_names():
            self._style.theme_use("vista")

        self._services: List[ServiceInfo] = get_available_services()
        self._services_by_label = {s.label: s for s in self._services}
        self._services_by_key = {s.key: s for s in self._services}
        self._last_detected_service: Optional[str] = None

        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._sink = _GuiLogSink(self._queue)
        self._cancel_event = threading.Event()
        self._interaction: Optional[_GuiInteractionHandler] = None
        self._worker: Optional[threading.Thread] = None
        self._is_running = False
        self._interactive_widgets: List[tk.Widget] = []

        self._config = self._load_saved_config()
        self._init_variables()
        self._build_ui()
        self._load_service_into_form()
        self._update_ffmpeg_status()
        self._set_running_state(False)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._poll_queue)

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _load_saved_config(self) -> Config:
        if self._config_location and not os.path.exists(self._config_location):
            return Config(
                sources={},
                output_template=None,
                database_directory=None,
                skip_downloaded=None,
            )
        return load_config(self._config_location)

    def _init_variables(self) -> None:
        self.output_dir_var = tk.StringVar(value=default_output_dir())
        self.template_var = tk.StringVar(value="{title}")

        first_label = self._services[0].label if self._services else ""
        self.service_label_var = tk.StringVar(value=first_label)
        self.service_hint_var = tk.StringVar(value="")
        self.saved_badge_var = tk.StringVar(value="")

        self.cookie_file_var = tk.StringVar(value="")
        self.username_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.library_var = tk.StringVar(value="")
        self.show_password_var = tk.BooleanVar(value=False)
        self.remember_login_var = tk.BooleanVar(value=True)

        saved_format = self._config.output_format or "Original"
        if saved_format not in OUTPUT_FORMATS:
            saved_format = "Original"
        self.output_format_var = tk.StringVar(value=saved_format)
        self.m4b_var = tk.BooleanVar(value=(saved_format == "m4b"))
        default_combine = True if self._config.combine is None else bool(self._config.combine)
        self.combine_var = tk.BooleanVar(value=default_combine)
        self.skip_downloaded_var = tk.BooleanVar(value=bool(self._config.skip_downloaded))
        self.no_chapters_var = tk.BooleanVar(value=False)
        self.write_json_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Ready")
        self.ffmpeg_status_var = tk.StringVar(value="")

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)

        # Header
        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text=f"audiobook-dl  v{__version__}",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._ffmpeg_label = ttk.Label(header, textvariable=self.ffmpeg_status_var)
        self._ffmpeg_label.grid(row=0, column=1, sticky="e")

        # URLs
        url_frame = ttk.LabelFrame(container, text="Audiobook URL(s)", padding=10)
        url_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        url_frame.columnconfigure(0, weight=1)

        self.urls_text = tk.Text(url_frame, height=3, wrap="word")
        self.urls_text.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.urls_text.bind("<Tab>", self._focus_next)
        self.urls_text.bind("<Shift-Tab>", self._focus_prev)
        self.urls_text.bind("<Control-Return>", lambda _e: self.start_download())
        self.urls_text.bind("<KeyRelease>", self._on_urls_changed)
        self.urls_text.bind("<<Paste>>", lambda _e: self.root.after(10, self._on_urls_changed))

        url_btn_col = ttk.Frame(url_frame)
        url_btn_col.grid(row=0, column=1, sticky="ns")
        load_btn = ttk.Button(url_btn_col, text="Load .txt...", command=self._browse_url_file)
        load_btn.pack(fill=tk.X, pady=(0, 4))
        clear_url_btn = ttk.Button(url_btn_col, text="Clear", command=self._clear_urls)
        clear_url_btn.pack(fill=tk.X)

        ttk.Label(
            url_frame,
            text="Paste one or more book or series URLs (one per line).",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Output Location
        out_frame = ttk.LabelFrame(container, text="Output", padding=10)
        out_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Folder:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        out_entry = ttk.Entry(out_frame, textvariable=self.output_dir_var)
        out_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        out_browse = ttk.Button(out_frame, text="Browse...", command=self._browse_output_dir)
        out_browse.grid(row=0, column=2, padx=(8, 0), pady=(0, 6))

        ttk.Label(out_frame, text="Template:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        tpl_entry = ttk.Entry(out_frame, textvariable=self.template_var)
        tpl_entry.grid(row=1, column=1, columnspan=2, sticky="ew")

        # Service & Authentication
        auth_frame = ttk.LabelFrame(container, text="Service & Authentication", padding=10)
        auth_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        auth_frame.columnconfigure(1, weight=1)
        auth_frame.columnconfigure(3, weight=1)

        ttk.Label(auth_frame, text="Service:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        service_row = ttk.Frame(auth_frame)
        service_row.grid(row=0, column=1, columnspan=4, sticky="ew", pady=(0, 6))
        service_row.columnconfigure(1, weight=1)

        self.service_combo = ttk.Combobox(
            service_row,
            textvariable=self.service_label_var,
            values=[s.label for s in self._services],
            state="readonly",
            width=24,
        )
        self.service_combo.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.service_combo.bind("<<ComboboxSelected>>", self._on_service_selected)

        ttk.Label(service_row, textvariable=self.service_hint_var, foreground="#555555").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(service_row, textvariable=self.saved_badge_var, foreground="#1f6f3d").grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )

        ttk.Label(auth_frame, text="Cookies File:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.cookie_entry = ttk.Entry(auth_frame, textvariable=self.cookie_file_var)
        self.cookie_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, 6))
        self.cookie_browse = ttk.Button(auth_frame, text="Browse...", command=self._browse_cookie_file)
        self.cookie_browse.grid(row=1, column=4, padx=(8, 0), pady=(0, 6))

        ttk.Label(auth_frame, text="Username:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.user_entry = ttk.Entry(auth_frame, textvariable=self.username_var)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(0, 6))

        ttk.Label(auth_frame, text="Password:").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(0, 6))
        self.password_entry = ttk.Entry(auth_frame, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=2, column=3, sticky="ew", pady=(0, 6))
        self.show_pw_btn = ttk.Checkbutton(
            auth_frame,
            text="Show",
            variable=self.show_password_var,
            command=self._toggle_password_visibility,
        )
        self.show_pw_btn.grid(row=2, column=4, padx=(8, 0), pady=(0, 6))

        ttk.Label(auth_frame, text="Library:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.lib_entry = ttk.Entry(auth_frame, textvariable=self.library_var)
        self.lib_entry.grid(row=3, column=1, sticky="ew", padx=(0, 12))

        auth_actions = ttk.Frame(auth_frame)
        auth_actions.grid(row=3, column=2, columnspan=3, sticky="ew")
        self.remember_cb = ttk.Checkbutton(
            auth_actions,
            text="Remember login & password",
            variable=self.remember_login_var,
        )
        self.remember_cb.pack(side=tk.LEFT)
        self.forget_auth_btn = ttk.Button(auth_actions, text="Forget", command=self.forget_current_credentials)
        self.forget_auth_btn.pack(side=tk.RIGHT)
        self.save_auth_btn = ttk.Button(auth_actions, text="Save", command=self.save_current_credentials)
        self.save_auth_btn.pack(side=tk.RIGHT, padx=(0, 6))

        # Options
        opt_frame = ttk.LabelFrame(container, text="Options", padding=10)
        opt_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))

        cb_m4b = ttk.Checkbutton(
            opt_frame,
            text="Download as .m4b",
            variable=self.m4b_var,
            command=self._on_m4b_toggled,
        )
        cb_m4b.grid(row=0, column=0, columnspan=2, sticky="w", padx=(0, 14))

        ttk.Label(opt_frame, text="Format:").grid(row=0, column=2, sticky="w", padx=(0, 6))
        format_combo = ttk.Combobox(
            opt_frame,
            textvariable=self.output_format_var,
            values=OUTPUT_FORMATS,
            state="readonly",
            width=10,
        )
        format_combo.grid(row=0, column=3, sticky="w", padx=(0, 14))
        format_combo.bind("<<ComboboxSelected>>", self._on_format_selected)

        cb_combine = ttk.Checkbutton(opt_frame, text="Combine parts", variable=self.combine_var)
        cb_combine.grid(row=0, column=4, sticky="w", padx=(0, 14))

        cb_skip = ttk.Checkbutton(opt_frame, text="Skip downloaded", variable=self.skip_downloaded_var)
        cb_skip.grid(row=0, column=5, sticky="w")

        cb_no_chapters = ttk.Checkbutton(opt_frame, text="No chapters", variable=self.no_chapters_var)
        cb_no_chapters.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        cb_json = ttk.Checkbutton(opt_frame, text="Write JSON metadata", variable=self.write_json_var)
        cb_json.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        cb_debug = ttk.Checkbutton(opt_frame, text="Debug logs", variable=self.debug_var)
        cb_debug.grid(row=1, column=4, sticky="w", pady=(6, 0))

        # Actions, Progress & Log
        log_frame = ttk.LabelFrame(container, text="Activity", padding=10)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(2, weight=1)

        action_bar = ttk.Frame(log_frame)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        action_bar.columnconfigure(4, weight=1)

        self.download_btn = ttk.Button(action_bar, text="Download", command=self.start_download)
        self.download_btn.grid(row=0, column=0, padx=(0, 6))

        self.cancel_btn = ttk.Button(action_bar, text="Cancel", command=self.cancel_download)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 6))

        self.open_folder_btn = ttk.Button(action_bar, text="Open Folder", command=self._open_output_folder)
        self.open_folder_btn.grid(row=0, column=2, padx=(0, 6))

        clear_log_btn = ttk.Button(action_bar, text="Clear Log", command=self._clear_log)
        clear_log_btn.grid(row=0, column=3)

        self.status_label = ttk.Label(action_bar, textvariable=self.status_var, anchor="e")
        self.status_label.grid(row=0, column=4, sticky="e")

        self.progressbar = ttk.Progressbar(log_frame, mode="determinate", maximum=100)
        self.progressbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        self.log_text = ScrolledText(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=2, column=0, sticky="nsew")

        self._interactive_widgets = [
            self.urls_text,
            load_btn,
            clear_url_btn,
            out_entry,
            out_browse,
            tpl_entry,
            self.service_combo,
            self.cookie_entry,
            self.cookie_browse,
            self.user_entry,
            self.password_entry,
            self.show_pw_btn,
            self.lib_entry,
            self.remember_cb,
            self.save_auth_btn,
            self.forget_auth_btn,
            cb_m4b,
            format_combo,
            cb_combine,
            cb_skip,
            cb_no_chapters,
            cb_json,
            cb_debug,
        ]

    def _focus_next(self, event: tk.Event) -> str:
        event.widget.tk_focusNext().focus()
        return "break"

    def _focus_prev(self, event: tk.Event) -> str:
        event.widget.tk_focusPrev().focus()
        return "break"

    def _update_ffmpeg_status(self) -> None:
        has_ffmpeg = find_executable("ffmpeg") is not None
        has_ffprobe = find_executable("ffprobe") is not None
        if has_ffmpeg and has_ffprobe:
            self.ffmpeg_status_var.set("FFmpeg: Ready")
            self._ffmpeg_label.configure(foreground="#1f6f3d")
        elif has_ffmpeg:
            self.ffmpeg_status_var.set("FFmpeg: Ready (ffprobe missing)")
            self._ffmpeg_label.configure(foreground="#9a6700")
        else:
            self.ffmpeg_status_var.set("FFmpeg: Not found (needed for combine/convert)")
            self._ffmpeg_label.configure(foreground="#b42318")

    def _selected_service(self) -> Optional[ServiceInfo]:
        return self._services_by_label.get(self.service_label_var.get())

    def _on_service_selected(self, _event: Optional[tk.Event] = None) -> None:
        self._load_service_into_form()
        self._update_service_field_states()

    def _on_urls_changed(self, _event: Optional[tk.Event] = None) -> None:
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if not urls:
            self._last_detected_service = None
            return
        detected = detect_service_key(urls[0])
        if detected and detected != self._last_detected_service:
            self._last_detected_service = detected
            service = self._services_by_key.get(detected)
            if service and self.service_label_var.get() != service.label:
                self.service_label_var.set(service.label)
                self._load_service_into_form()
                self._update_service_field_states()

    def _load_service_into_form(self) -> None:
        service = self._selected_service()
        if service is None:
            return
        saved: Optional[SourceConfig] = self._config.sources.get(service.key)
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

    def _update_service_field_states(self) -> None:
        if self._is_running:
            return
        service = self._selected_service()
        if service is None:
            return
        supports_cookies = "cookies" in service.auth_methods
        supports_login = "login" in service.auth_methods
        needs_library = supports_login and "library" in service.login_fields

        cookie_state = "normal" if supports_cookies else "disabled"
        login_state = "normal" if supports_login else "disabled"
        library_state = "normal" if needs_library else "disabled"
        save_state = "normal" if service.auth_methods else "disabled"

        self.cookie_entry.configure(state=cookie_state)
        self.cookie_browse.configure(state=cookie_state)
        self.user_entry.configure(state=login_state)
        self.password_entry.configure(state=login_state)
        self.show_pw_btn.configure(state=login_state)
        self.lib_entry.configure(state=library_state)
        self.save_auth_btn.configure(state=save_state)
        has_saved = service.key in self._config.sources
        self.forget_auth_btn.configure(state="normal" if has_saved else "disabled")

    def save_current_credentials(self, quiet: bool = False) -> None:
        """Persist the selected service's login, password, library, and cookie path."""
        service = self._selected_service()
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

        save_source_credentials(
            service.key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            overwrite=self._config_location,
        )
        self._config = self._load_saved_config()
        has_saved = service.key in self._config.sources
        self.saved_badge_var.set("Saved" if has_saved else "")
        self._update_service_field_states()
        if not quiet:
            if has_saved:
                self.status_var.set(f"Saved credentials for {service.label}")
                self._append_log(f"Saved login settings for {service.label}.")
            else:
                self.status_var.set(f"Cleared credentials for {service.label}")

    def forget_current_credentials(self) -> None:
        """Remove saved credentials for the currently selected service."""
        service = self._selected_service()
        if service is None:
            return
        save_source_credentials(service.key, overwrite=self._config_location)
        self._config = self._load_saved_config()
        self.username_var.set("")
        self.password_var.set("")
        self.library_var.set("")
        self.cookie_file_var.set("")
        self.saved_badge_var.set("")
        self._update_service_field_states()
        self.status_var.set(f"Removed saved login for {service.label}")
        self._append_log(f"Removed saved login settings for {service.label}.")

    def _on_m4b_toggled(self) -> None:
        if self.m4b_var.get():
            self.output_format_var.set("m4b")
            self.combine_var.set(True)
        elif self.output_format_var.get() == "m4b":
            self.output_format_var.set("Original")

    def _on_format_selected(self, _event: Optional[tk.Event] = None) -> None:
        is_m4b = self.output_format_var.get() == "m4b"
        self.m4b_var.set(is_m4b)
        if is_m4b:
            self.combine_var.set(True)

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def _browse_url_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as exc:
            messagebox.showerror("Error", f"Could not read file:\n{exc}", parent=self.root)
            return
        if content:
            existing = self.urls_text.get("1.0", tk.END).strip()
            combined = f"{existing}\n{content}" if existing else content
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()

    def _clear_urls(self) -> None:
        self.urls_text.delete("1.0", tk.END)
        self._on_urls_changed()

    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Select output folder",
            initialdir=self.output_dir_var.get() or default_output_dir(),
        )
        if chosen:
            self.output_dir_var.set(chosen)

    def _browse_cookie_file(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Select Netscape cookie file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self.cookie_file_var.set(chosen)

    def _open_output_folder(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        if not os.path.isdir(folder):
            messagebox.showinfo("Folder Missing", f"Folder does not exist yet:\n{folder}", parent=self.root)
            return
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("Output Folder", folder, parent=self.root)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        widget_state = "disabled" if running else "normal"
        for widget in self._interactive_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state=widget_state)
        self.download_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        if not running:
            self._update_service_field_states()

    def build_options(self) -> Any:
        """Build `audiobookdl` options namespace from current UI field values."""
        output_dir = self.output_dir_var.get().strip() or default_output_dir()
        template = self.template_var.get().strip() or "{title}"
        output_template = os.path.join(output_dir, template)
        selected_format = self.output_format_var.get().strip()
        output_format = None if selected_format in ("", "Original") else selected_format

        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        detected_keys = {k for k in (detect_service_key(u) for u in urls) if k}
        service = self._selected_service()
        apply_form_auth = (
            service is not None
            and (len(detected_keys) <= 1)
            and (not detected_keys or service.key in detected_keys)
        )

        username = None
        password = None
        library = None
        cookie_file = None
        if apply_form_auth and service is not None:
            if "cookies" in service.auth_methods:
                cookie_file = self.cookie_file_var.get().strip() or None
            if "login" in service.auth_methods:
                username = self.username_var.get().strip() or None
                password = self.password_var.get() or None
                if "library" in service.login_fields:
                    library = self.library_var.get().strip() or None

        config_location = (
            self._config_location
            if (self._config_location and os.path.exists(self._config_location))
            else None
        )

        return args.build_default_options(
            urls=urls,
            cookie_file=cookie_file,
            combine=self.combine_var.get(),
            output_template=output_template,
            debug=self.debug_var.get(),
            no_chapters=self.no_chapters_var.get(),
            output_format=output_format,
            username=username,
            password=password,
            library=library,
            skip_downloaded=self.skip_downloaded_var.get(),
            write_json_metadata=self.write_json_var.get(),
            config_location=config_location,
        )

    def start_download(self) -> None:
        if self._is_running:
            return
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if not urls:
            self.status_var.set("Enter at least one URL")
            messagebox.showwarning("Missing URL", "Please enter at least one audiobook URL.", parent=self.root)
            self.urls_text.focus_set()
            return

        service = self._selected_service()
        cookie_file = (
            self.cookie_file_var.get().strip()
            if (service is None or "cookies" in service.auth_methods)
            else ""
        )
        if cookie_file and not os.path.isfile(cookie_file):
            self.status_var.set("Cookie file not found")
            messagebox.showerror("Invalid Cookie File", f"File not found:\n{cookie_file}", parent=self.root)
            return

        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)

        options = self.build_options()
        save_output_preferences(
            output_format=options.output_format,
            combine=options.combine,
            overwrite=self._config_location,
        )
        self._config = self._load_saved_config()
        # Ensure build_options includes config_location if it was just created
        options = self.build_options()
        self._cancel_event.clear()
        self._interaction = _GuiInteractionHandler(self._queue, self._cancel_event)
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)
        self.status_var.set("Starting...")
        self._set_running_state(True)
        self._append_log(f"--- Starting download ({len(urls)} URL{'s' if len(urls) != 1 else ''}) ---")

        self._worker = threading.Thread(
            target=self._run_worker,
            args=(urls, options, self._interaction),
            daemon=True,
        )
        self._worker.start()

    def cancel_download(self) -> None:
        if not self._is_running:
            return
        self.status_var.set("Cancelling...")
        self.cancel_btn.configure(state="disabled")
        self._cancel_event.set()
        if self._interaction is not None:
            self._interaction.release_all()

    def _run_worker(
        self,
        urls: Sequence[str],
        options: Any,
        interaction: _GuiInteractionHandler,
    ) -> None:
        logging.set_ui_sink(self._sink)
        try:
            config = load_config(options.config_location)
            summary = run_downloads(
                urls,
                options,
                config,
                interaction=interaction,
                cancel_event=self._cancel_event,
            )
            self._queue.put(("done", summary, None))
        except Exception as exc:
            self._queue.put(("done", None, exc))
        finally:
            logging.set_ui_sink(None)

    _MAX_QUEUE_BATCH = 200

    def _poll_queue(self) -> None:
        pending_progress: Optional[tuple] = None
        processed = 0
        while processed < self._MAX_QUEUE_BATCH:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if item[0] == "progress_advance":
                pending_progress = item
                continue
            if pending_progress is not None:
                self._handle_event(pending_progress)
                pending_progress = None
            self._handle_event(item)
        if pending_progress is not None:
            self._handle_event(pending_progress)
        delay_ms = 10 if processed >= self._MAX_QUEUE_BATCH else 50
        self.root.after(delay_ms, self._poll_queue)

    def _handle_event(self, item: tuple) -> None:
        kind = item[0]
        if kind == "log":
            _, level, message = item
            prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
            self._append_log(f"{prefix}{message}")
            if level == "INFO" and message.strip():
                self.status_var.set(message.strip().splitlines()[0][:80])
        elif kind == "progress_start":
            _, description, _total = item
            self.progressbar.stop()
            self.progressbar.configure(mode="determinate", value=0)
            self.status_var.set(description[:80])
        elif kind == "progress_advance":
            _, completed, total = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            self.progressbar.configure(value=min(100.0, max(0.0, pct)))
        elif kind == "progress_finish":
            self.progressbar.configure(value=100.0)
        elif kind == "prompt_input":
            _, prompt, hidden, holder, done = item
            holder["value"] = simpledialog.askstring(
                "Authentication Required",
                f"{prompt}:",
                show="*" if hidden else None,
                parent=self.root,
            )
            done.set()
        elif kind == "prompt_confirm":
            _, message, holder, done = item
            holder["value"] = messagebox.askyesno("Confirm", message, parent=self.root)
            done.set()
        elif kind == "done":
            _, summary, exc = item
            self._finish_run(summary, exc)

    def _finish_run(self, summary: Optional[DownloadSummary], exc: Optional[Exception]) -> None:
        self._set_running_state(False)
        self.progressbar.stop()
        if exc is not None:
            self.progressbar.configure(value=0)
            self.status_var.set("Error")
            self._append_log(f"[ERROR] {exc}")
            messagebox.showerror("Download Failed", str(exc), parent=self.root)
            return

        if summary is None or summary.cancelled:
            self.progressbar.configure(value=0)
            self.status_var.set("Cancelled")
            return

        if summary.failed > 0 and summary.completed == 0:
            self.progressbar.configure(value=0)
            self.status_var.set("Failed")
        elif summary.failed > 0:
            self.progressbar.configure(value=100)
            self.status_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
        else:
            self.progressbar.configure(value=100)
            self.status_var.set("Completed")
            self._append_log("--- Completed ---")

    def _on_close(self) -> None:
        if self._is_running:
            self._cancel_event.set()
            if self._interaction is not None:
                self._interaction.release_all()
        self.root.destroy()


def main() -> None:
    """Launch the audiobook-dl desktop GUI."""
    root = tk.Tk()
    AudiobookDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

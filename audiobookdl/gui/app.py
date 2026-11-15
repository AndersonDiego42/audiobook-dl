from audiobookdl import __version__, args, logging
from audiobookdl.pipeline import DownloadSummary, prepare_source_result, run_downloads
from audiobookdl.config import (
    Config,
    SourceConfig,
    is_portable_mode,
    load_config,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.sources import ServiceInfo, detect_service_key, get_available_services
from audiobookdl.sources.source import Source
from audiobookdl.utils import (
    AUDIO_BITRATE_PRESETS,
    find_executable,
    read_asset_file,
    resolve_ffmpeg_dir,
    set_ffmpeg_dir,
)
from audiobookdl.utils.audiobook import Audiobook, BookId, Cover, Result, Series

from dataclasses import dataclass
import importlib.resources
import io
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union


OUTPUT_FORMATS = ("Original", "m4b", "mp3", "m4a", "flac", "opus")
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


def is_valid_geometry(geometry: str) -> bool:
    """Validate a Tkinter geometry string (`WxH` or `WxH+X+Y`) with sane minimum dimensions."""
    match = _GEOMETRY_RE.fullmatch(geometry.strip())
    if not match:
        return False
    width = int(match.group(1))
    height = int(match.group(2))
    return width >= 400 and height >= 400


def format_speed(bytes_per_sec: float) -> str:
    """Format a byte rate into human-readable B/s, KB/s, or MB/s."""
    if bytes_per_sec <= 0:
        return "--"
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    if bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"


def format_duration(seconds: Optional[float]) -> str:
    """Format a duration in seconds as `MM:SS` or `H:MM:SS`."""
    if seconds is None or seconds < 0:
        return "--"
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def describe_series_book(index: int, book: Union[BookId[Any], Audiobook]) -> str:
    """Return a readable display label for an entry inside a `Series`."""
    if isinstance(book, Audiobook):
        author = book.metadata.author
        return f"{book.title} — {author}" if author else book.title
    title = getattr(book, "title", None)
    if title:
        return str(title)
    book_id = getattr(book, "id", book)
    if isinstance(book_id, dict):
        dict_title = book_id.get("title") or book_id.get("name")
        if dict_title:
            return str(dict_title)
    return f"Book {index + 1} (ID: {book_id})"


def format_audiobook_preview(audiobook: Audiobook) -> List[Tuple[str, str]]:
    """Build ordered `(label, value)` metadata rows for previewing an `Audiobook`."""
    meta = audiobook.metadata
    rows: List[Tuple[str, str]] = [("Title", audiobook.title)]
    if meta.author:
        rows.append(("Author(s)", meta.author))
    if meta.narrator:
        rows.append(("Narrator(s)", meta.narrator))
    if meta.series:
        series_text = (
            f"{meta.series} #{meta.series_order}"
            if meta.series_order is not None
            else meta.series
        )
        rows.append(("Series", series_text))
    if meta.genre:
        rows.append(("Genre(s)", meta.genre))
    if meta.language is not None:
        lang_name = getattr(meta.language, "name", None) or str(meta.language)
        rows.append(("Language", lang_name))
    if meta.publisher:
        rows.append(("Publisher", meta.publisher))
    if meta.release_date is not None:
        rows.append(("Released", str(meta.release_date)))
    if meta.isbn:
        rows.append(("ISBN", meta.isbn))
    file_count = len(audiobook.files)
    exts = sorted({f.ext.lstrip(".").lower() for f in audiobook.files if f.ext})
    ext_suffix = f" ({', '.join(exts)})" if exts else ""
    rows.append(("Parts", f"{file_count} file{'s' if file_count != 1 else ''}{ext_suffix}"))
    chap_count = len(audiobook.chapters)
    rows.append(("Chapters", f"{chap_count} chapter{'s' if chap_count != 1 else ''}"))
    return rows


def _render_cover_thumbnail(
    cover: Optional[Cover],
    master: tk.Misc,
    max_size: int = 180,
) -> Optional[Any]:
    """Decode `cover` bytes into a resized `ImageTk.PhotoImage`, or `None` on failure."""
    if cover is None or not cover.image:
        return None
    try:
        from PIL import Image, ImageTk

        image = Image.open(io.BytesIO(cover.image))
        image.thumbnail((max_size, max_size))
        return ImageTk.PhotoImage(image, master=master)
    except Exception:
        return None


def default_output_dir() -> str:
    """Return the default output directory (user Downloads folder if it exists)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return str(downloads)
    return os.getcwd()


def parse_url_text(raw: str) -> List[str]:
    """Extract non-empty URLs from whitespace/newline-separated input text."""
    return [token.strip() for token in raw.split() if token.strip()]


@dataclass
class _QueuedJob:
    """Single queued URL download job consumed by the persistent GUI worker."""
    run_id: int
    url: str
    options: args.DownloadOptions
    interaction: "_GuiInteractionHandler"
    selected_indices: Optional[List[int]] = None
    prefetched: Optional[Tuple[Source, Result]] = None


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

    def on_progress_metrics(
        self,
        completed: float,
        total: float,
        bytes_downloaded: int,
        speed_bps: float,
        elapsed_seconds: float,
        eta_seconds: Optional[float],
    ) -> None:
        self._queue.put((
            "progress_metrics",
            completed,
            total,
            bytes_downloaded,
            speed_bps,
            elapsed_seconds,
            eta_seconds,
        ))

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        self._queue.put(("item_progress", current, total, title))

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
        self._config = self._load_saved_config()
        saved_geometry = (self._config.gui.window_geometry or "").strip()
        if saved_geometry and is_valid_geometry(saved_geometry):
            self.root.geometry(saved_geometry)
        else:
            self.root.geometry("760x680")
        self.root.minsize(660, 600)
        self._apply_window_icon()

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
        self._job_queue: "queue.Queue[Optional[_QueuedJob]]" = queue.Queue()
        self._queue_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._worker_has_active_run = False
        self._worker_run_id = 0
        self._current_ui_run_id = 0
        self._batch_total = 0
        self._run_seen_urls: Set[str] = set()
        self._ui_item_index = 0
        self._ui_item_total = 0
        self._ui_book_index = 0
        self._ui_book_total = 0
        self._run_start_time: Optional[float] = None
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds: Optional[float] = None
        self._last_metrics_render_at = 0.0
        self._is_running = False
        self._is_previewing = False
        self._preview_cancel_event = threading.Event()
        self._preview_interaction: Optional[_GuiInteractionHandler] = None
        self._preview_dialog: Optional[tk.Toplevel] = None
        self._interactive_widgets: List[
            Union[ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox]
        ] = []

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

    @property
    def is_previewing(self) -> bool:
        return self._is_previewing

    def _load_saved_config(self) -> Config:
        if self._config_location and not os.path.exists(self._config_location):
            cfg = Config(
                sources={},
                output_template=None,
                database_directory=None,
                skip_downloaded=None,
            )
        else:
            cfg = load_config(self._config_location)
        set_ffmpeg_dir(cfg.ffmpeg_dir)
        return cfg

    def _init_variables(self) -> None:
        saved_output_dir = (self._config.gui.output_dir or "").strip()
        self.output_dir_var = tk.StringVar(value=saved_output_dir or default_output_dir())
        saved_template = (self._config.gui.output_template or "").strip()
        self.template_var = tk.StringVar(value=saved_template or "{title}")

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
        saved_bitrate = (self._config.audio_bitrate or "").strip()
        if not saved_bitrate or saved_bitrate.lower() == "auto":
            saved_bitrate = "Auto"
        self.audio_bitrate_var = tk.StringVar(value=saved_bitrate)
        default_combine = True if self._config.combine is None else self._config.combine
        self.combine_var = tk.BooleanVar(value=default_combine)
        self.skip_downloaded_var = tk.BooleanVar(value=bool(self._config.skip_downloaded))
        self.resume_var = tk.BooleanVar(value=bool(self._config.resume))
        self.no_chapters_var = tk.BooleanVar(value=False)
        self.write_json_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Ready")
        self.batch_var = tk.StringVar(value="")
        self.metrics_var = tk.StringVar(value="")
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
        title_text = f"audiobook-dl  v{__version__}"
        if is_portable_mode():
            title_text += "  [Portable]"
        ttk.Label(
            header,
            text=title_text,
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ffmpeg_box = ttk.Frame(header)
        ffmpeg_box.grid(row=0, column=1, sticky="e")
        self._ffmpeg_label = ttk.Label(
            ffmpeg_box,
            textvariable=self.ffmpeg_status_var,
            takefocus=True,
        )
        self._ffmpeg_label.pack(side=tk.LEFT, padx=(0, 8))
        self._ffmpeg_label.bind("<Button-1>", lambda _e: self._show_ffmpeg_guidance())
        self._ffmpeg_label.bind("<Return>", lambda _e: self._show_ffmpeg_guidance())
        self._ffmpeg_label.bind("<space>", lambda _e: self._show_ffmpeg_guidance())

        self.ffmpeg_locate_btn = ttk.Button(
            ffmpeg_box,
            text="Locate FFmpeg...",
            command=self.browse_ffmpeg_dir,
        )
        self.ffmpeg_locate_btn.pack(side=tk.LEFT)

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
        self.urls_text.bind("<ButtonRelease-1>", lambda _e: self.root.after(10, self._on_urls_changed))
        self._build_url_context_menu()

        url_btn_col = ttk.Frame(url_frame)
        url_btn_col.grid(row=0, column=1, sticky="ns")
        paste_btn = ttk.Button(url_btn_col, text="Paste", command=self._paste_urls_from_clipboard)
        paste_btn.pack(fill=tk.X, pady=(0, 4))
        load_btn = ttk.Button(url_btn_col, text="Load .txt...", command=self._browse_url_file)
        load_btn.pack(fill=tk.X, pady=(0, 4))
        clear_url_btn = ttk.Button(url_btn_col, text="Clear", command=self._clear_urls)
        clear_url_btn.pack(fill=tk.X)

        ttk.Label(
            url_frame,
            text="Paste, drag a link, or load .txt with book/series URLs (one per line). Ctrl+Enter to download or queue.",
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

        cb_resume = ttk.Checkbutton(opt_frame, text="Resume partial", variable=self.resume_var)
        cb_resume.grid(row=1, column=5, sticky="w", pady=(6, 0))

        ttk.Label(opt_frame, text="M4B Quality:").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        bitrate_values = list(AUDIO_BITRATE_PRESETS)
        if self.audio_bitrate_var.get() not in bitrate_values:
            bitrate_values.append(self.audio_bitrate_var.get())
        bitrate_combo = ttk.Combobox(
            opt_frame,
            textvariable=self.audio_bitrate_var,
            values=bitrate_values,
            state="readonly",
            width=10,
        )
        bitrate_combo.grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))

        # Actions, Progress & Log
        log_frame = ttk.LabelFrame(container, text="Activity", padding=10)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(3, weight=1)

        action_bar = ttk.Frame(log_frame)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        action_bar.columnconfigure(5, weight=1)

        self.download_btn = ttk.Button(action_bar, text="Download", command=self.start_download)
        self.download_btn.grid(row=0, column=0, padx=(0, 6))

        self.cancel_btn = ttk.Button(action_bar, text="Cancel", command=self.cancel_download)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 6))

        self.preview_btn = ttk.Button(action_bar, text="Preview", command=self.preview_metadata)
        self.preview_btn.grid(row=0, column=2, padx=(0, 6))

        self.open_folder_btn = ttk.Button(action_bar, text="Open Folder", command=self._open_output_folder)
        self.open_folder_btn.grid(row=0, column=3, padx=(0, 6))

        clear_log_btn = ttk.Button(action_bar, text="Clear Log", command=self._clear_log)
        clear_log_btn.grid(row=0, column=4)

        self.status_label = ttk.Label(action_bar, textvariable=self.status_var, anchor="e")
        self.status_label.grid(row=0, column=5, sticky="e")

        meta_bar = ttk.Frame(log_frame)
        meta_bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        meta_bar.columnconfigure(1, weight=1)

        self.batch_label = ttk.Label(meta_bar, textvariable=self.batch_var, foreground="#333333")
        self.batch_label.grid(row=0, column=0, sticky="w")

        self.metrics_label = ttk.Label(meta_bar, textvariable=self.metrics_var, foreground="#555555", anchor="e")
        self.metrics_label.grid(row=0, column=1, sticky="e")

        self.progressbar = ttk.Progressbar(log_frame, mode="determinate", maximum=100)
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        self.log_text = ScrolledText(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=3, column=0, sticky="nsew")

        self._interactive_widgets = [
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
            bitrate_combo,
            cb_combine,
            cb_skip,
            cb_resume,
            cb_no_chapters,
            cb_json,
            cb_debug,
        ]

    def _focus_next(self, event: tk.Event) -> str:
        next_widget = event.widget.tk_focusNext()
        if next_widget is not None:
            next_widget.focus()
        return "break"

    def _focus_prev(self, event: tk.Event) -> str:
        prev_widget = event.widget.tk_focusPrev()
        if prev_widget is not None:
            prev_widget.focus()
        return "break"

    def _apply_window_icon(self) -> None:
        try:
            icon_ref = importlib.resources.files("audiobookdl").joinpath("assets/icon.ico")
            with importlib.resources.as_file(icon_ref) as icon_path:
                if icon_path.is_file():
                    self.root.iconbitmap(default=str(icon_path))
        except Exception:
            pass

    def _update_ffmpeg_status(self) -> None:
        has_ffmpeg = find_executable("ffmpeg") is not None
        has_ffprobe = find_executable("ffprobe") is not None
        if has_ffmpeg and has_ffprobe:
            self.ffmpeg_status_var.set("FFmpeg: Ready")
            self._ffmpeg_label.configure(foreground="#1f6f3d", cursor="")
            self.ffmpeg_locate_btn.configure(text="FFmpeg Folder...")
        elif has_ffmpeg:
            self.ffmpeg_status_var.set("FFmpeg: Ready (ffprobe missing - click for help)")
            self._ffmpeg_label.configure(foreground="#9a6700", cursor="hand2")
            self.ffmpeg_locate_btn.configure(text="Locate FFmpeg...")
        else:
            self.ffmpeg_status_var.set("FFmpeg: Not found (click for setup help)")
            self._ffmpeg_label.configure(foreground="#b42318", cursor="hand2")
            self.ffmpeg_locate_btn.configure(text="Locate FFmpeg...")

    def browse_ffmpeg_dir(self) -> bool:
        """Let the user choose a folder containing `ffmpeg` and persist it in config."""
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Select folder containing ffmpeg / ffprobe",
            initialdir=self._config.ffmpeg_dir or os.getcwd(),
        )
        if not chosen:
            return False
        resolved = resolve_ffmpeg_dir(chosen)
        if resolved is None:
            exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            messagebox.showwarning(
                "FFmpeg Not Found",
                f"Could not find {exe_name} inside:\n{chosen}\n\n"
                "Please choose the folder (or its bin subfolder) containing ffmpeg.",
                parent=self.root,
            )
            return False
        set_ffmpeg_dir(resolved)
        save_ffmpeg_dir(resolved, overwrite=self._config_location)
        self._config = self._load_saved_config()
        self._update_ffmpeg_status()
        self.status_var.set("FFmpeg folder updated")
        self._append_log(f"Using FFmpeg from: {resolved}")
        return True

    def _ensure_ffmpeg_available(self, options: args.DownloadOptions) -> bool:
        """Validate FFmpeg/FFprobe up front when combine or format conversion is enabled."""
        if not (options.combine or options.output_format):
            return True
        has_ffmpeg = find_executable("ffmpeg") is not None
        has_ffprobe = find_executable("ffprobe") is not None
        if has_ffmpeg and (not options.combine or has_ffprobe):
            return True

        missing_parts = []
        if not has_ffmpeg:
            missing_parts.append("ffmpeg")
        if options.combine and not has_ffprobe:
            missing_parts.append("ffprobe")
        missing_text = " and ".join(missing_parts)

        reasons = []
        if options.combine:
            reasons.append("combine parts")
        if options.output_format:
            reasons.append(f"convert to .{options.output_format}")
        reason_text = " and ".join(reasons)

        choice = messagebox.askyesnocancel(
            "FFmpeg Required",
            f"{missing_text} was not found, which is needed to {reason_text}.\n\n"
            "Yes: Locate FFmpeg folder now\n"
            "No: Continue anyway (may fail after downloading parts)\n"
            "Cancel: Stop and view setup instructions",
            parent=self.root,
        )
        if choice is True:
            if self.browse_ffmpeg_dir():
                has_ffmpeg = find_executable("ffmpeg") is not None
                has_ffprobe = find_executable("ffprobe") is not None
                return has_ffmpeg and (not options.combine or has_ffprobe)
            return False
        if choice is False:
            return True
        self._show_ffmpeg_guidance()
        return False

    def _show_ffmpeg_guidance(self) -> None:
        ffmpeg_path = find_executable("ffmpeg")
        ffprobe_path = find_executable("ffprobe")
        if ffmpeg_path and ffprobe_path:
            messagebox.showinfo(
                "FFmpeg Ready",
                f"ffmpeg: {ffmpeg_path}\nffprobe: {ffprobe_path}",
                parent=self.root,
            )
            return
        exe_dir = (
            os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.getcwd()
        )
        messagebox.showinfo(
            "FFmpeg Setup Guide",
            "FFmpeg and FFprobe are needed to combine multi-part books or convert to .m4b/.mp3.\n\n"
            "Quickest setup:\n"
            "1. Click 'Locate FFmpeg...' in the top-right to point to an existing ffmpeg folder.\n"
            f"2. Or place ffmpeg.exe and ffprobe.exe in:\n   {exe_dir}\n\n"
            "Install via package manager:\n"
            "   Windows: winget install Gyan.FFmpeg\n"
            "   macOS:   brew install ffmpeg\n"
            "   Linux:   sudo apt install ffmpeg\n\n"
            "Download builds: https://ffmpeg.org/download.html",
            parent=self.root,
        )

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

    def _build_url_context_menu(self) -> None:
        self._url_menu = tk.Menu(self.urls_text, tearoff=0)
        self._url_menu.add_command(label="Cut", command=lambda: self.urls_text.event_generate("<<Cut>>"))
        self._url_menu.add_command(label="Copy", command=lambda: self.urls_text.event_generate("<<Copy>>"))
        self._url_menu.add_command(label="Paste", command=self._paste_urls_from_clipboard)
        self._url_menu.add_command(label="Paste && Queue", command=self._paste_and_queue)
        self._url_menu.add_separator()
        self._url_menu.add_command(label="Select All", command=lambda: self.urls_text.tag_add(tk.SEL, "1.0", tk.END))
        self._url_menu.add_command(label="Clear", command=self._clear_urls)
        self.urls_text.bind("<Button-3>", self._show_url_context_menu)
        if sys.platform == "darwin":
            self.urls_text.bind("<Button-2>", self._show_url_context_menu)

    def _show_url_context_menu(self, event: tk.Event) -> str:
        try:
            self._url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._url_menu.grab_release()
        return "break"

    def _paste_urls_from_clipboard(self) -> List[str]:
        """Insert URLs from the system clipboard into the URL box."""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            self.status_var.set("Clipboard is empty")
            return []
        pasted = parse_url_text(raw)
        if not pasted:
            self.status_var.set("No URLs found in clipboard")
            return []
        existing = parse_url_text(self.urls_text.get("1.0", tk.END))
        existing_set = set(existing)
        added = [u for u in pasted if u not in existing_set]
        if added:
            combined = "\n".join([*existing, *added])
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", combined)
            self._on_urls_changed()
            self.status_var.set(f"Pasted {len(added)} URL{'s' if len(added) != 1 else ''}")
        else:
            self.status_var.set("Clipboard URL(s) already listed")
        return added

    def _paste_and_queue(self) -> None:
        pasted = self._paste_urls_from_clipboard()
        if pasted or parse_url_text(self.urls_text.get("1.0", tk.END)):
            self.start_download()

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
        if running:
            self.download_btn.configure(text="Add to Queue", state="normal")
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        else:
            self.download_btn.configure(text="Download", state="normal")
            self.cancel_btn.configure(state="disabled")
            self.preview_btn.configure(state="disabled" if self._is_previewing else "normal")
            self._update_service_field_states()

    def _refresh_batch_label(self) -> None:
        parts = []
        if self._ui_item_total > 0 and self._ui_item_index > 0:
            parts.append(f"Item {self._ui_item_index} / {self._ui_item_total}")
        if self._ui_book_total > 1 and self._ui_book_index > 0:
            parts.append(f"Book {self._ui_book_index} / {self._ui_book_total}")
        self.batch_var.set("  \u2022  ".join(parts))

    def _update_metrics_display(self, force: bool = False) -> None:
        if not self._is_running or self._run_start_time is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_metrics_render_at) < 0.5:
            return
        self._last_metrics_render_at = now
        elapsed = max(0.0, now - self._run_start_time)
        parts = []
        if self._last_pct > 0:
            parts.append(f"{self._last_pct:.0f}%")
        if self._last_speed_bps > 0:
            parts.append(format_speed(self._last_speed_bps))
        parts.append(f"Elapsed {format_duration(elapsed)}")
        if self._last_eta_seconds is not None:
            parts.append(f"ETA {format_duration(self._last_eta_seconds)}")
        self.metrics_var.set("  \u2022  ".join(parts))

    def build_options(self, urls_override: Optional[Sequence[str]] = None) -> args.DownloadOptions:
        """Build `DownloadOptions` from current UI field values."""
        output_dir = self.output_dir_var.get().strip() or default_output_dir()
        template = self.template_var.get().strip() or "{title}"
        output_template = os.path.join(output_dir, template)
        selected_format = self.output_format_var.get().strip()
        output_format = None if selected_format in ("", "Original") else selected_format
        selected_bitrate = self.audio_bitrate_var.get().strip()
        audio_bitrate = None if selected_bitrate in ("", "Auto", "auto") else selected_bitrate

        urls = (
            list(urls_override)
            if urls_override is not None
            else parse_url_text(self.urls_text.get("1.0", tk.END))
        )
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
            resume=self.resume_var.get(),
            audio_bitrate=audio_bitrate,
        )

    def _validate_cookie_file(self) -> bool:
        service = self._selected_service()
        cookie_file = (
            self.cookie_file_var.get().strip()
            if (service is None or "cookies" in service.auth_methods)
            else ""
        )
        if cookie_file and not os.path.isfile(cookie_file):
            self.status_var.set("Cookie file not found")
            messagebox.showerror("Invalid Cookie File", f"File not found:\n{cookie_file}", parent=self.root)
            return False
        return True

    def _enqueue_additional_urls(self, urls: List[str]) -> bool:
        """Append new URLs to the currently active download queue."""
        if not self._validate_cookie_file():
            return True
        unique_urls = list(dict.fromkeys(urls))
        with self._queue_lock:
            if not self._worker_has_active_run:
                return False
            new_urls = [u for u in unique_urls if u not in self._run_seen_urls]
            if not new_urls:
                self.status_var.set("URL(s) already in active queue")
                self._append_log("All entered URL(s) are already in the active queue.")
                return True
            if self._interaction is None:
                self._interaction = _GuiInteractionHandler(self._queue, self._cancel_event)
            interaction = self._interaction
            run_id = self._worker_run_id
            self._batch_total += len(new_urls)
            self._run_seen_urls.update(new_urls)
            for u in new_urls:
                job_opts = self.build_options(urls_override=[u])
                self._job_queue.put(_QueuedJob(
                    run_id=run_id,
                    url=u,
                    options=job_opts,
                    interaction=interaction,
                ))
            new_total = self._batch_total
        self._ui_item_total = new_total
        self._refresh_batch_label()
        self.status_var.set(f"Queued +{len(new_urls)} (total {new_total})")
        self._append_log(
            f"--- Added {len(new_urls)} URL{'s' if len(new_urls) != 1 else ''} to queue (total {new_total}) ---"
        )
        return True

    def _save_run_preferences(self, options: args.DownloadOptions) -> None:
        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)
        save_output_preferences(
            output_format=options.output_format,
            combine=options.combine,
            overwrite=self._config_location,
            audio_bitrate=options.audio_bitrate,
            resume=options.resume,
        )
        save_gui_preferences(
            output_dir=self.output_dir_var.get().strip() or default_output_dir(),
            output_template=self.template_var.get().strip() or "{title}",
            overwrite=self._config_location,
        )
        self._config = self._load_saved_config()

    def _start_new_run(
        self,
        job_specs: Sequence[Tuple[str, args.DownloadOptions, Optional[List[int]], Optional[Tuple[Source, Result]]]],
    ) -> None:
        """Initialize UI and queue state for a fresh download run."""
        self._cancel_event.clear()
        self._interaction = _GuiInteractionHandler(self._queue, self._cancel_event)
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)
        self.status_var.set("Starting...")
        self._run_start_time = time.monotonic()
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds = None
        self._last_metrics_render_at = 0.0
        self._set_running_state(True)

        with self._queue_lock:
            self._worker_run_id += 1
            run_id = self._worker_run_id
            self._current_ui_run_id = run_id
            self._worker_has_active_run = True
            self._batch_total = len(job_specs)
            self._run_seen_urls = {url for url, _, _, _ in job_specs}
            for url, job_opts, selected_indices, prefetched in job_specs:
                self._job_queue.put(_QueuedJob(
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=self._interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                ))

        self._ui_item_index = 1
        self._ui_item_total = len(job_specs)
        self._ui_book_index = 0
        self._ui_book_total = 0
        self._refresh_batch_label()
        self._update_metrics_display(force=True)
        self._append_log(
            f"--- Starting download ({len(job_specs)} URL{'s' if len(job_specs) != 1 else ''}) ---"
        )
        self._ensure_worker_started()

    def start_download(self) -> None:
        if self._is_previewing:
            return
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if self._is_running:
            if self._cancel_event.is_set():
                return
            if not urls:
                self.status_var.set("Enter at least one URL to queue")
                self.urls_text.focus_set()
                return
            if self._enqueue_additional_urls(urls):
                return

        if not urls:
            self.status_var.set("Enter at least one URL")
            messagebox.showwarning("Missing URL", "Please enter at least one audiobook URL.", parent=self.root)
            self.urls_text.focus_set()
            return

        if not self._validate_cookie_file():
            return

        options = self.build_options()
        if not self._ensure_ffmpeg_available(options):
            return
        self._save_run_preferences(options)

        unique_urls = list(dict.fromkeys(urls))
        job_specs = [
            (u, self.build_options(urls_override=[u]), None, None)
            for u in unique_urls
        ]
        self._start_new_run(job_specs)

    def _set_previewing_state(self, previewing: bool) -> None:
        self._is_previewing = previewing
        if previewing:
            self.preview_btn.configure(state="disabled")
            self.download_btn.configure(state="disabled")
        else:
            self.preview_btn.configure(state="disabled" if self._is_running else "normal")
            self.download_btn.configure(state="normal")

    def preview_metadata(self) -> None:
        """Fetch book or series metadata for the first entered URL without downloading audio."""
        if self._is_running or self._is_previewing:
            return
        urls = parse_url_text(self.urls_text.get("1.0", tk.END))
        if not urls:
            self.status_var.set("Enter a URL to preview")
            messagebox.showwarning(
                "Missing URL",
                "Please enter an audiobook or series URL to preview.",
                parent=self.root,
            )
            self.urls_text.focus_set()
            return
        if not self._validate_cookie_file():
            return

        if self.remember_login_var.get():
            self.save_current_credentials(quiet=True)

        target_url = urls[0]
        if len(urls) > 1:
            self._append_log(f"Previewing first URL: {target_url}")

        options = self.build_options(urls_override=[target_url])
        self._preview_cancel_event.clear()
        self._preview_interaction = _GuiInteractionHandler(self._queue, self._preview_cancel_event)
        self._set_previewing_state(True)
        self.progressbar.configure(mode="indeterminate", value=0)
        self.progressbar.start(12)
        self.status_var.set("Fetching preview...")
        self._append_log(f"--- Fetching preview: {target_url} ---")

        threading.Thread(
            target=self._run_preview_worker,
            args=(target_url, options, self._preview_interaction),
            daemon=True,
        ).start()

    def _run_preview_worker(
        self,
        url: str,
        options: args.DownloadOptions,
        interaction: _GuiInteractionHandler,
    ) -> None:
        try:
            config = load_config(options.config_location)
            ctx = logging.JobContext(
                debug=options.debug,
                quiet=options.quiet,
                ffmpeg_output=options.ffmpeg_output or options.debug,
                sink=self._sink,
                interaction=interaction,
                cancel_event=self._preview_cancel_event,
            )
            with logging.job_context(ctx):
                source, result = prepare_source_result(
                    url,
                    options,
                    config,
                    interaction=interaction,
                    cancel_event=self._preview_cancel_event,
                )
            self._queue.put(("preview_done", url, (source, result), None))
        except Exception as exc:
            self._queue.put(("preview_done", url, None, exc))

    def _handle_preview_done(
        self,
        url: str,
        prepared: Optional[Tuple[Source, Result]],
        exc: Optional[Exception],
    ) -> None:
        self._set_previewing_state(False)
        self._preview_interaction = None
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)

        if exc is not None:
            if isinstance(exc, DownloadCancelled):
                self.status_var.set("Preview cancelled")
                return
            self.status_var.set("Preview failed")
            self._append_log(f"[ERROR] {exc}")
            messagebox.showerror("Preview Failed", str(exc), parent=self.root)
            return

        if prepared is None:
            self.status_var.set("Preview failed")
            return

        source, result = prepared
        self.status_var.set(f"Preview ready: {result.title[:60]}")
        self._show_preview_dialog(url, source, result)

    def _show_preview_dialog(self, url: str, source: Source, result: Result) -> tk.Toplevel:
        """Display resolved `Audiobook` metadata/cover or a selectable `Series` checklist."""
        if self._preview_dialog is not None and self._preview_dialog.winfo_exists():
            self._preview_dialog.destroy()

        dialog = tk.Toplevel(self.root)
        self._preview_dialog = dialog
        dialog.title(f"Preview \u2014 {result.title}")
        dialog.transient(self.root)
        dialog.minsize(520, 360)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)

        if isinstance(result, Audiobook):
            top = ttk.Frame(body)
            top.grid(row=0, column=0, sticky="nsew")
            top.columnconfigure(1, weight=1)
            body.rowconfigure(0, weight=1)

            cover_frame = ttk.Frame(top, width=190, height=190)
            cover_frame.grid(row=0, column=0, sticky="nw", padx=(0, 14))
            photo = _render_cover_thumbnail(result.cover, dialog, max_size=180)
            if photo is not None:
                dialog._cover_photo = photo  # type: ignore[attr-defined]
                ttk.Label(cover_frame, image=photo).pack()
            else:
                ttk.Label(
                    cover_frame,
                    text="No Cover",
                    width=20,
                    anchor="center",
                    relief="groove",
                    padding=24,
                ).pack()

            meta_frame = ttk.Frame(top)
            meta_frame.grid(row=0, column=1, sticky="nsew")
            meta_frame.columnconfigure(1, weight=1)
            rows = format_audiobook_preview(result)
            for row_idx, (label, value) in enumerate(rows):
                ttk.Label(
                    meta_frame,
                    text=f"{label}:",
                    font=("Segoe UI", 9, "bold"),
                ).grid(row=row_idx, column=0, sticky="nw", padx=(0, 8), pady=2)
                ttk.Label(
                    meta_frame,
                    text=value,
                    wraplength=320,
                    justify="left",
                ).grid(row=row_idx, column=1, sticky="nw", pady=2)

            if result.metadata.description:
                desc_row = len(rows)
                ttk.Label(
                    meta_frame,
                    text="Description:",
                    font=("Segoe UI", 9, "bold"),
                ).grid(row=desc_row, column=0, sticky="nw", padx=(0, 8), pady=(6, 2))
                desc_box = ScrolledText(meta_frame, height=4, width=40, wrap="word")
                desc_box.grid(row=desc_row, column=1, sticky="nsew", pady=(6, 2))
                desc_box.insert("1.0", result.metadata.description.strip())
                desc_box.configure(state="disabled")
                meta_frame.rowconfigure(desc_row, weight=1)

            btn_bar = ttk.Frame(body)
            btn_bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            action_text = "Add to Queue" if self._is_running else "Download"

            def _confirm_book() -> None:
                dialog.destroy()
                self._enqueue_from_preview(url, (source, result), None)

            ttk.Button(btn_bar, text=action_text, command=_confirm_book).pack(side=tk.RIGHT)
            ttk.Button(btn_bar, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        else:
            # Series view with selectable checklist
            body.rowconfigure(2, weight=1)
            header = ttk.Frame(body)
            header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            header.columnconfigure(0, weight=1)
            ttk.Label(
                header,
                text=result.title,
                font=("Segoe UI", 11, "bold"),
            ).grid(row=0, column=0, sticky="w")
            book_count = len(result.books)
            ttk.Label(
                header,
                text=f"{source.name}  \u2022  {book_count} book{'s' if book_count != 1 else ''}",
                foreground="#555555",
            ).grid(row=1, column=0, sticky="w")

            selected_count_var = tk.StringVar()
            check_vars: List[tk.BooleanVar] = [tk.BooleanVar(value=True) for _ in result.books]
            dialog._check_vars = check_vars  # type: ignore[attr-defined]

            def _update_selected_count() -> None:
                chosen = sum(1 for v in check_vars if v.get())
                selected_count_var.set(f"Selected: {chosen} / {book_count}")

            def _set_all(val: bool) -> None:
                for v in check_vars:
                    v.set(val)
                _update_selected_count()

            toolbar = ttk.Frame(body)
            toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
            ttk.Button(toolbar, text="Select All", command=lambda: _set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(toolbar, text="Deselect All", command=lambda: _set_all(False)).pack(side=tk.LEFT)
            ttk.Label(toolbar, textvariable=selected_count_var, foreground="#333333").pack(side=tk.RIGHT)
            _update_selected_count()

            list_container = ttk.Frame(body, relief="sunken", borderwidth=1)
            list_container.grid(row=2, column=0, sticky="nsew")
            list_container.columnconfigure(0, weight=1)
            list_container.rowconfigure(0, weight=1)

            if book_count == 0:
                ttk.Label(
                    list_container,
                    text="No downloadable books found in this series.",
                    padding=20,
                ).grid(row=0, column=0)
            else:
                canvas = tk.Canvas(list_container, highlightthickness=0, height=240)
                scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
                inner = ttk.Frame(canvas, padding=6)
                inner.bind(
                    "<Configure>",
                    lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
                )
                canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
                canvas.bind(
                    "<Configure>",
                    lambda e: canvas.itemconfigure(canvas_window, width=e.width),
                )
                canvas.configure(yscrollcommand=scrollbar.set)
                canvas.grid(row=0, column=0, sticky="nsew")
                scrollbar.grid(row=0, column=1, sticky="ns")

                for idx, book in enumerate(result.books):
                    label = f"{idx + 1}. {describe_series_book(idx, book)}"
                    cb = ttk.Checkbutton(
                        inner,
                        text=label,
                        variable=check_vars[idx],
                        command=_update_selected_count,
                    )
                    cb.pack(anchor="w", fill=tk.X, pady=1)

            btn_bar = ttk.Frame(body)
            btn_bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            action_text = "Queue Selected" if self._is_running else "Download Selected"

            def _confirm_series() -> None:
                indices = [i for i, v in enumerate(check_vars) if v.get()]
                if not indices:
                    messagebox.showwarning(
                        "No Selection",
                        "Please select at least one book to download.",
                        parent=dialog,
                    )
                    return
                dialog.destroy()
                self._enqueue_from_preview(url, (source, result), indices)

            download_sel_btn = ttk.Button(
                btn_bar,
                text=action_text,
                command=_confirm_series,
                state="disabled" if book_count == 0 else "normal",
            )
            download_sel_btn.pack(side=tk.RIGHT)
            ttk.Button(btn_bar, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        return dialog

    def _enqueue_from_preview(
        self,
        url: str,
        prefetched: Tuple[Source, Result],
        selected_indices: Optional[List[int]],
    ) -> None:
        """Start or queue a download from an inspected preview result."""
        options = self.build_options(urls_override=[url])
        if not self._ensure_ffmpeg_available(options):
            return
        if self._is_running:
            with self._queue_lock:
                if self._worker_has_active_run:
                    if self._interaction is None:
                        self._interaction = _GuiInteractionHandler(self._queue, self._cancel_event)
                    self._batch_total += 1
                    self._run_seen_urls.add(url)
                    self._job_queue.put(_QueuedJob(
                        run_id=self._worker_run_id,
                        url=url,
                        options=options,
                        interaction=self._interaction,
                        selected_indices=selected_indices,
                        prefetched=prefetched,
                    ))
                    new_total = self._batch_total
                    self._ui_item_total = new_total
                    self._refresh_batch_label()
                    self.status_var.set(f"Queued preview item (total {new_total})")
                    return

        self._save_run_preferences(options)
        self._start_new_run([(url, self.build_options(urls_override=[url]), selected_indices, prefetched)])

    def _ensure_worker_started(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
            )
            self._worker.start()

    def cancel_download(self) -> None:
        if not self._is_running:
            return
        self.status_var.set("Cancelling...")
        self.cancel_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self._cancel_event.set()
        if self._interaction is not None:
            self._interaction.release_all()

    def _drain_pending_jobs(self) -> bool:
        """Remove queued jobs after cancellation/error; return True if close sentinel seen."""
        saw_sentinel = False
        with self._queue_lock:
            while True:
                try:
                    pending = self._job_queue.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    saw_sentinel = True
                    break
            self._worker_has_active_run = False
        return saw_sentinel

    def _worker_loop(self) -> None:
        while True:
            first_job = self._job_queue.get()
            if first_job is None:
                break

            run_id = first_job.run_id
            batch_summary = DownloadSummary()
            fatal_exc: Optional[Exception] = None
            item_index = 0
            saw_sentinel = False
            current_job: Optional[_QueuedJob] = first_job

            while current_job is not None:
                item_index += 1
                if self._cancel_event.is_set():
                    batch_summary.cancelled = True
                    saw_sentinel = self._drain_pending_jobs()
                    break

                with self._queue_lock:
                    item_total = self._batch_total

                self._queue.put(("batch_item", run_id, item_index, item_total, current_job.url))

                try:
                    config = load_config(current_job.options.config_location)
                    run_kwargs: Dict[str, Any] = {
                        "interaction": current_job.interaction,
                        "cancel_event": self._cancel_event,
                        "sink": self._sink,
                    }
                    if current_job.selected_indices is not None:
                        run_kwargs["selected_indices"] = current_job.selected_indices
                    if current_job.prefetched is not None:
                        run_kwargs["prefetched"] = current_job.prefetched
                    summary = run_downloads(
                        [current_job.url],
                        current_job.options,
                        config,
                        **run_kwargs,
                    )
                    batch_summary.completed += summary.completed
                    batch_summary.failed += summary.failed
                    if summary.cancelled:
                        batch_summary.cancelled = True
                        saw_sentinel = self._drain_pending_jobs()
                        break
                except Exception as exc:
                    fatal_exc = exc
                    saw_sentinel = self._drain_pending_jobs()
                    break

                with self._queue_lock:
                    try:
                        next_job = self._job_queue.get_nowait()
                    except queue.Empty:
                        self._worker_has_active_run = False
                        current_job = None
                    else:
                        if next_job is None:
                            self._worker_has_active_run = False
                            saw_sentinel = True
                            current_job = None
                        else:
                            current_job = next_job

            self._queue.put(("done", run_id, batch_summary, fatal_exc))
            if saw_sentinel:
                break

    _MAX_QUEUE_BATCH = 200

    def _poll_queue(self) -> None:
        pending_progress: Optional[tuple] = None
        pending_metrics: Optional[tuple] = None
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
            if item[0] == "progress_metrics":
                pending_metrics = item
                continue
            if pending_progress is not None:
                self._handle_event(pending_progress)
                pending_progress = None
            if pending_metrics is not None:
                self._handle_event(pending_metrics)
                pending_metrics = None
            self._handle_event(item)
        if pending_progress is not None:
            self._handle_event(pending_progress)
        if pending_metrics is not None:
            self._handle_event(pending_metrics)
        if self._is_running:
            self._update_metrics_display()
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
        elif kind == "batch_item":
            _, run_id, item_index, item_total, _url = item
            if run_id == self._current_ui_run_id:
                self._ui_item_index = item_index
                self._ui_item_total = max(self._ui_item_total, item_total)
                self._ui_book_index = 0
                self._ui_book_total = 0
                self._refresh_batch_label()
        elif kind == "item_progress":
            _, book_index, book_total, _title = item
            self._ui_book_index = book_index
            self._ui_book_total = book_total
            self._refresh_batch_label()
        elif kind == "progress_start":
            _, description, _total = item
            self.progressbar.stop()
            self.progressbar.configure(mode="determinate", value=0)
            self._last_pct = 0.0
            self._last_speed_bps = 0.0
            self._last_eta_seconds = None
            self._update_metrics_display(force=True)
            self.status_var.set(description[:80])
        elif kind == "progress_advance":
            _, completed, total = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            clamped = min(100.0, max(0.0, pct))
            self._last_pct = clamped
            self.progressbar.configure(value=clamped)
        elif kind == "progress_metrics":
            _, completed, total, _bytes_downloaded, speed_bps, _elapsed, eta_seconds = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            self._last_pct = min(100.0, max(0.0, pct))
            self._last_speed_bps = speed_bps
            self._last_eta_seconds = eta_seconds
            self._update_metrics_display(force=True)
        elif kind == "progress_finish":
            self._last_pct = 100.0
            self._last_eta_seconds = 0.0
            self.progressbar.configure(value=100.0)
            self._update_metrics_display(force=True)
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
        elif kind == "preview_done":
            _, preview_url, prepared, preview_exc = item
            self._handle_preview_done(preview_url, prepared, preview_exc)
        elif kind == "done":
            _, run_id, summary, exc = item
            if run_id == self._current_ui_run_id:
                self._finish_run(summary, exc)

    def _finish_run(self, summary: Optional[DownloadSummary], exc: Optional[Exception]) -> None:
        self._set_running_state(False)
        self.progressbar.stop()
        with self._queue_lock:
            self._run_seen_urls.clear()
        final_elapsed = (
            format_duration(max(0.0, time.monotonic() - self._run_start_time))
            if self._run_start_time is not None
            else None
        )
        self._run_start_time = None
        self.batch_var.set("")
        if exc is not None:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Error")
            self._append_log(f"[ERROR] {exc}")
            messagebox.showerror("Download Failed", str(exc), parent=self.root)
            return

        if summary is None or summary.cancelled:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Cancelled")
            return

        if summary.failed > 0 and summary.completed == 0:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Failed")
        elif summary.failed > 0:
            self.progressbar.configure(value=100)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
        else:
            self.progressbar.configure(value=100)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set("Completed")
            self._append_log("--- Completed ---")

    def _on_close(self) -> None:
        try:
            geom = self.root.geometry()
            save_gui_preferences(
                window_geometry=geom if is_valid_geometry(geom) else None,
                output_dir=self.output_dir_var.get().strip() or None,
                output_template=self.template_var.get().strip() or None,
                overwrite=self._config_location,
            )
        except OSError:
            pass
        if self._is_running:
            self._cancel_event.set()
            if self._interaction is not None:
                self._interaction.release_all()
        if self._is_previewing:
            self._preview_cancel_event.set()
            if self._preview_interaction is not None:
                self._preview_interaction.release_all()
        self._job_queue.put(None)
        self.root.destroy()


def run_self_test(check_gui: bool = True) -> None:
    """Validate bundled assets, dynamic packages, and GUI startup for frozen builds."""
    from Crypto.Cipher import AES
    import pycountry
    from audiobookdl.config import tomllib

    help_text = read_asset_file("assets/simple_help.txt")
    if not help_text.strip():
        raise RuntimeError("Missing assets/simple_help.txt")
    chapter_tpl = read_asset_file("assets/ffmpeg_chapter_template.txt")
    if "{title}" not in chapter_tpl:
        raise RuntimeError("Invalid assets/ffmpeg_chapter_template.txt")
    if pycountry.languages.get(alpha_2="en") is None:
        raise RuntimeError("pycountry language database unavailable")
    parsed_toml = tomllib.loads('output_template = "{title}"\n')
    if parsed_toml.get("output_template") != "{title}":
        raise RuntimeError("TOML parser check failed")
    cipher = AES.new(b"0123456789abcdef", AES.MODE_CBC, b"fedcba9876543210")
    if len(cipher.encrypt(b"0123456789abcdef")) != 16:
        raise RuntimeError("Cryptodome AES check failed")

    if check_gui:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = tk.Tk()
            root.withdraw()
            try:
                app = AudiobookDLApp(
                    root,
                    config_location=os.path.join(tmpdir, "audiobook-dl.toml"),
                )
                root.update_idletasks()
                if not root.title().startswith("audiobook-dl"):
                    raise RuntimeError(f"Unexpected window title: {root.title()!r}")
                _ = app.build_options()
            finally:
                root.destroy()


def main() -> None:
    """Launch the audiobook-dl desktop GUI."""
    if "--self-test" in sys.argv[1:]:
        run_self_test(check_gui=True)
        return
    root = tk.Tk()
    AudiobookDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

from audiobookdl import __version__, args, logging, output
from audiobookdl.pipeline import (
    DownloadSummary,
    list_service_library,
    prepare_source_result,
    run_downloads,
    search_service,
    verify_service_auth,
)
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
from audiobookdl.sources.source import CatalogBook, Source
from audiobookdl.utils import (
    AUDIO_BITRATE_PRESETS,
    find_executable,
    read_asset_file,
    resolve_ffmpeg_dir,
    set_ffmpeg_dir,
)
from audiobookdl.utils.archive import DownloadArchive, default_archive_path
from audiobookdl.utils.updater import (
    UpdateInfo,
    check_for_updates,
    install_ffmpeg_via_winget,
    winget_available,
)
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookMetadata,
    BookId,
    Cover,
    Result,
    Series,
)

import argparse
from collections import deque
import ctypes
from dataclasses import dataclass
from datetime import date
import importlib.resources
import io
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple, Union
import uuid


OUTPUT_FORMATS = ("Original", "m4b", "mp3", "m4a", "flac", "opus")
TEMPLATE_PRESET_LABELS: Tuple[Tuple[str, str], ...] = (
    ("Custom", ""),
    ("Default", output.OUTPUT_TEMPLATE_PRESETS["default"]),
    ("Audiobookshelf", output.OUTPUT_TEMPLATE_PRESETS["audiobookshelf"]),
    ("Plex", output.OUTPUT_TEMPLATE_PRESETS["plex"]),
    ("Kavita", output.OUTPUT_TEMPLATE_PRESETS["kavita"]),
)
_PRESET_LABEL_TO_TEMPLATE: Dict[str, str] = {label: tpl for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
_TEMPLATE_TO_PRESET_LABEL: Dict[str, str] = {tpl: label for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
_SAMPLE_PREVIEW_METADATA = AudiobookMetadata(
    title="Dune",
    authors=["Frank Herbert"],
    series="Dune Chronicles",
    series_order=1,
    release_date=date(1965, 8, 1),
    source="storytel",
)
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
    job_id: str
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
        self._active_run_id = 0

    def set_active_run_id(self, run_id: int) -> None:
        self._active_run_id = run_id

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
        self._queue.put(("item_progress", self._active_run_id, current, total, title))

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


class PreviewDialog(tk.Toplevel):
    """Typed `Toplevel` window hosting book metadata or a series selection checklist."""

    _cover_photo: Optional[Any] = None
    _check_vars: List[tk.BooleanVar]
    _filter_var: tk.StringVar
    _checkbuttons: List[Tuple[int, str, ttk.Checkbutton]]
    _toggle_series_item: Callable[..., None]


class ArchiveLedgerDialog(tk.Toplevel):
    """Typed `Toplevel` window hosting the SQLite download history ledger."""

    _tree: ttk.Treeview
    _search_var: tk.StringVar
    _refresh: Callable[..., None]


class CatalogBrowserDialog(tk.Toplevel):
    """Typed `Toplevel` window hosting service catalog search and library browsing."""

    _tree: ttk.Treeview
    _service_var: tk.StringVar
    _query_var: tk.StringVar
    _status_var: tk.StringVar
    _sync_buttons: Callable[..., None]
    _populate_items: Callable[[Sequence[CatalogBook]], None]
    _run_fetch: Callable[[str], None]
    _add_selected: Callable[[], None]


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
            screen_w = max(660, self.root.winfo_screenwidth())
            screen_h = max(480, self.root.winfo_screenheight())
            default_w = min(780, max(660, screen_w - 40))
            default_h = min(780, max(480, screen_h - 80))
            self.root.geometry(f"{default_w}x{default_h}")
        self.root.minsize(660, 480)
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
        self._pending_jobs: Deque[Optional[_QueuedJob]] = deque()
        self._queue_lock = threading.Lock()
        self._queue_cond = threading.Condition(self._queue_lock)
        self._worker: Optional[threading.Thread] = None
        self._worker_has_active_run = False
        self._worker_run_id = 0
        self._current_ui_run_id = 0
        self._batch_total = 0
        self._run_seen_urls: Set[str] = set()
        self._queue_records: Dict[str, Dict[str, Any]] = {}
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
        self._is_verifying_auth = False
        self._catalog_request_id = 0
        self._preview_cancel_event = threading.Event()
        self._preview_interaction: Optional[_GuiInteractionHandler] = None
        self._preview_dialog: Optional[PreviewDialog] = None
        self._vscroll_visible = False
        self._last_canvas_size: Optional[Tuple[int, int]] = None
        self._last_content_req_size: Optional[Tuple[int, int]] = None
        self._interactive_widgets: List[
            Union[ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox]
        ] = []

        self._init_variables()
        self._build_menu_bar()
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
        saved_template = (self._config.gui.output_template or "").strip() or "{title}"
        self.template_var = tk.StringVar(value=saved_template)
        self.template_preset_var = tk.StringVar(
            value=_TEMPLATE_TO_PRESET_LABEL.get(saved_template, "Custom")
        )
        self.template_preview_var = tk.StringVar(value="")

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
        self.enrich_metadata_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Ready")
        self.batch_var = tk.StringVar(value="")
        self.metrics_var = tk.StringVar(value="")
        self.ffmpeg_status_var = tk.StringVar(value="")

    def _sync_scroll_layout(self, event: Optional[tk.Event] = None) -> None:
        if event is not None:
            canvas_w = event.width
            canvas_h = event.height
            self._last_canvas_size = (canvas_w, canvas_h)
        elif self._last_canvas_size is not None:
            canvas_w, canvas_h = self._last_canvas_size
        else:
            canvas_w = self._scroll_canvas.winfo_width()
            canvas_h = self._scroll_canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return
        req_w = self._content_frame.winfo_reqwidth()
        req_h = self._content_frame.winfo_reqheight()
        self._last_content_req_size = (req_w, req_h)
        target_w = max(canvas_w, req_w)
        target_h = max(canvas_h, req_h)
        self._scroll_canvas.itemconfigure(
            self._scroll_window_id,
            width=target_w,
            height=target_h,
        )
        self._scroll_canvas.configure(scrollregion=(0, 0, target_w, target_h))
        needs_vscroll = req_h > canvas_h + 1
        if needs_vscroll and not self._vscroll_visible:
            self._vscroll.grid(row=0, column=1, sticky="ns")
            self._vscroll_visible = True
        elif not needs_vscroll and self._vscroll_visible:
            self._vscroll.grid_remove()
            self._vscroll_visible = False
            self._scroll_canvas.yview_moveto(0.0)

    def _on_content_configure(self, _event: Optional[tk.Event] = None) -> None:
        req_size = (
            self._content_frame.winfo_reqwidth(),
            self._content_frame.winfo_reqheight(),
        )
        if req_size != self._last_content_req_size:
            self._sync_scroll_layout()

    def _ensure_focus_visible(self, event: tk.Event) -> None:
        if not self._vscroll_visible:
            return
        widget = getattr(event, "widget", None)
        if widget is None or widget in (self.root, self._scroll_canvas, self._content_frame):
            return
        try:
            if widget.winfo_toplevel() is not self.root:
                return
            content_y = self._content_frame.winfo_rooty()
            widget_top = widget.winfo_rooty() - content_y
            widget_height = widget.winfo_height()
            widget_bottom = widget_top + widget_height
            content_h = self._content_frame.winfo_height()
            canvas_h = self._scroll_canvas.winfo_height()
        except tk.TclError:
            return
        if content_h <= canvas_h or canvas_h <= 1 or widget_height <= 1 or widget_height > canvas_h:
            return
        first, last = self._scroll_canvas.yview()
        view_top = first * content_h
        view_bottom = last * content_h
        if widget_top < view_top:
            self._scroll_canvas.yview_moveto(max(0.0, (widget_top - 8) / content_h))
        elif widget_bottom > view_bottom:
            self._scroll_canvas.yview_moveto(min(1.0, (widget_bottom + 8 - canvas_h) / content_h))

    def _on_main_mousewheel(self, event: tk.Event) -> None:
        widget = getattr(event, "widget", None)
        if widget is not None:
            if "popdown" in str(widget):
                return
            for scrollable in (self.urls_text, self.log_text, self.queue_tree):
                if widget is scrollable:
                    try:
                        if scrollable.yview() != (0.0, 1.0):
                            return
                    except Exception:
                        return
        first, last = self._scroll_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        delta = getattr(event, "delta", 0)
        if delta:
            step = -1 if delta > 0 else 1
            if sys.platform != "darwin" and abs(delta) >= 120:
                step = int(-delta / 120)
            self._scroll_canvas.yview_scroll(step, "units")
        elif getattr(event, "num", None) == 4:
            self._scroll_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self._scroll_canvas.yview_scroll(1, "units")

    def _build_ui(self) -> None:
        scroll_host = ttk.Frame(self.root)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        scroll_host.columnconfigure(0, weight=1)
        scroll_host.rowconfigure(0, weight=1)

        canvas_bg = self._style.lookup("TFrame", "background") or ""
        canvas_kwargs: Dict[str, Any] = {"highlightthickness": 0, "borderwidth": 0}
        if canvas_bg:
            canvas_kwargs["background"] = canvas_bg
        self._scroll_canvas = tk.Canvas(scroll_host, **canvas_kwargs)
        self._scroll_canvas.grid(row=0, column=0, sticky="nsew")

        self._vscroll = ttk.Scrollbar(
            scroll_host,
            orient="vertical",
            command=self._scroll_canvas.yview,
        )
        self._scroll_canvas.configure(yscrollcommand=self._vscroll.set)

        container = ttk.Frame(self._scroll_canvas, padding=(12, 8))
        self._content_frame = container
        self._scroll_window_id = self._scroll_canvas.create_window(
            (0, 0),
            window=container,
            anchor="nw",
        )
        container.bind("<Configure>", self._on_content_configure)
        self._scroll_canvas.bind("<Configure>", self._sync_scroll_layout)
        self.root.bind("<FocusIn>", self._ensure_focus_visible)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind(seq, self._on_main_mousewheel)

        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)

        # Header
        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
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
        url_frame = ttk.LabelFrame(container, text="Audiobook URL(s)", padding=(10, 6))
        url_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        url_frame.columnconfigure(0, weight=1)

        self.urls_text = tk.Text(url_frame, height=2, width=40, wrap="word")
        self.urls_text.grid(row=0, column=0, sticky="ew")
        self.urls_text.bind("<Tab>", self._focus_next)
        self.urls_text.bind("<Shift-Tab>", self._focus_prev)
        self.urls_text.bind("<Control-Return>", lambda _e: self.start_download())
        self.urls_text.bind("<KeyRelease>", self._on_urls_changed)
        self.urls_text.bind("<<Paste>>", lambda _e: self.root.after(10, self._on_urls_changed))
        self.urls_text.bind("<ButtonRelease-1>", lambda _e: self.root.after(10, self._on_urls_changed))
        self._build_url_context_menu()

        url_footer = ttk.Frame(url_frame)
        url_footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        url_footer.columnconfigure(0, weight=1)
        ttk.Label(
            url_footer,
            text="One URL per line \u2022 Right-click: Paste & Queue \u2022 Ctrl+Enter to start",
            foreground="#555555",
        ).grid(row=0, column=0, sticky="w")
        url_btn_row = ttk.Frame(url_footer)
        url_btn_row.grid(row=0, column=1, sticky="e")
        paste_btn = ttk.Button(url_btn_row, text="Paste", command=self._paste_urls_from_clipboard)
        paste_btn.pack(side=tk.LEFT, padx=(0, 4))
        load_btn = ttk.Button(url_btn_row, text="Load .txt...", command=self._browse_url_file)
        load_btn.pack(side=tk.LEFT, padx=(0, 4))
        clear_url_btn = ttk.Button(url_btn_row, text="Clear", command=self._clear_urls)
        clear_url_btn.pack(side=tk.LEFT)

        # Output Location
        out_frame = ttk.LabelFrame(container, text="Output", padding=(10, 6))
        out_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        out_frame.columnconfigure(1, weight=1)

        ttk.Label(out_frame, text="Folder:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        out_entry = ttk.Entry(out_frame, textvariable=self.output_dir_var)
        out_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        out_browse = ttk.Button(out_frame, text="Browse...", command=self._browse_output_dir)
        out_browse.grid(row=0, column=2, padx=(8, 0), pady=(0, 4))

        ttk.Label(out_frame, text="Template:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        tpl_row = ttk.Frame(out_frame)
        tpl_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 4))
        tpl_row.columnconfigure(0, weight=1)
        tpl_entry = ttk.Entry(tpl_row, textvariable=self.template_var)
        tpl_entry.grid(row=0, column=0, sticky="ew")
        self.preset_combo = ttk.Combobox(
            tpl_row,
            textvariable=self.template_preset_var,
            values=[label for label, _tpl in TEMPLATE_PRESET_LABELS],
            state="readonly",
            width=16,
        )
        self.preset_combo.grid(row=0, column=1, padx=(8, 0))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        ttk.Label(out_frame, text="Preview:").grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Label(out_frame, textvariable=self.template_preview_var, foreground="#555555").grid(
            row=2, column=1, columnspan=2, sticky="w"
        )
        for var in (self.output_dir_var, self.template_var, self.output_format_var, self.combine_var):
            var.trace_add("write", lambda *_args: self._update_template_preview())
        self.template_var.trace_add("write", lambda *_args: self._sync_preset_from_template())
        self._update_template_preview()

        # Service & Authentication
        auth_frame = ttk.LabelFrame(container, text="Service & Authentication", padding=(10, 6))
        auth_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        auth_frame.columnconfigure(1, weight=1)
        auth_frame.columnconfigure(3, weight=1)

        ttk.Label(auth_frame, text="Service:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        service_row = ttk.Frame(auth_frame)
        service_row.grid(row=0, column=1, columnspan=4, sticky="ew", pady=(0, 4))
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

        ttk.Label(auth_frame, text="Cookies File:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.cookie_entry = ttk.Entry(auth_frame, textvariable=self.cookie_file_var)
        self.cookie_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, 4))
        self.cookie_browse = ttk.Button(auth_frame, text="Browse...", command=self._browse_cookie_file)
        self.cookie_browse.grid(row=1, column=4, padx=(8, 0), pady=(0, 4))

        ttk.Label(auth_frame, text="Username:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        self.user_entry = ttk.Entry(auth_frame, textvariable=self.username_var)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 4))

        ttk.Label(auth_frame, text="Password:").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(0, 4))
        self.password_entry = ttk.Entry(auth_frame, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=2, column=3, sticky="ew", pady=(0, 4))
        self.show_pw_btn = ttk.Checkbutton(
            auth_frame,
            text="Show",
            variable=self.show_password_var,
            command=self._toggle_password_visibility,
        )
        self.show_pw_btn.grid(row=2, column=4, padx=(8, 0), pady=(0, 4))

        ttk.Label(auth_frame, text="Library:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.lib_entry = ttk.Entry(auth_frame, textvariable=self.library_var, width=14)
        self.lib_entry.grid(row=3, column=1, sticky="ew", padx=(0, 10))

        auth_actions = ttk.Frame(auth_frame)
        auth_actions.grid(row=3, column=2, columnspan=3, sticky="ew")
        self.remember_cb = ttk.Checkbutton(
            auth_actions,
            text="Remember login",
            variable=self.remember_login_var,
        )
        self.remember_cb.pack(side=tk.LEFT)
        self.forget_auth_btn = ttk.Button(auth_actions, text="Forget", command=self.forget_current_credentials)
        self.forget_auth_btn.pack(side=tk.RIGHT)
        self.save_auth_btn = ttk.Button(auth_actions, text="Save", command=self.save_current_credentials)
        self.save_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.test_auth_btn = ttk.Button(auth_actions, text="Test Login", command=self.verify_current_auth)
        self.test_auth_btn.pack(side=tk.RIGHT, padx=(0, 4))

        # Options
        opt_frame = ttk.LabelFrame(container, text="Options", padding=(10, 6))
        opt_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))

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
        cb_no_chapters.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        cb_json = ttk.Checkbutton(opt_frame, text="Write JSON metadata", variable=self.write_json_var)
        cb_json.grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))

        cb_debug = ttk.Checkbutton(opt_frame, text="Debug logs", variable=self.debug_var)
        cb_debug.grid(row=1, column=4, sticky="w", pady=(4, 0))

        cb_resume = ttk.Checkbutton(opt_frame, text="Resume partial", variable=self.resume_var)
        cb_resume.grid(row=1, column=5, sticky="w", pady=(4, 0))

        ttk.Label(opt_frame, text="M4B Quality:").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
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
        bitrate_combo.grid(row=2, column=2, columnspan=2, sticky="w", pady=(4, 0))

        cb_enrich = ttk.Checkbutton(
            opt_frame,
            text="Enrich metadata",
            variable=self.enrich_metadata_var,
        )
        cb_enrich.grid(row=2, column=4, columnspan=2, sticky="w", pady=(4, 0))

        # Actions, Progress & Log
        log_frame = ttk.LabelFrame(container, text="Activity", padding=(10, 6))
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(3, weight=1)

        action_bar = ttk.Frame(log_frame)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
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
        meta_bar.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        meta_bar.columnconfigure(1, weight=1)

        self.batch_label = ttk.Label(meta_bar, textvariable=self.batch_var, foreground="#333333")
        self.batch_label.grid(row=0, column=0, sticky="w")

        self.metrics_label = ttk.Label(meta_bar, textvariable=self.metrics_var, foreground="#555555", anchor="e")
        self.metrics_label.grid(row=0, column=1, sticky="e")

        self.progressbar = ttk.Progressbar(log_frame, mode="determinate", maximum=100)
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self.activity_notebook = ttk.Notebook(log_frame)
        self.activity_notebook.grid(row=3, column=0, sticky="nsew")

        log_tab = ttk.Frame(self.activity_notebook)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(log_tab, height=4, width=40, state="disabled", wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.activity_notebook.add(log_tab, text="Activity Log")

        queue_tab = ttk.Frame(self.activity_notebook, padding=4)
        queue_tab.columnconfigure(0, weight=1)
        queue_tab.rowconfigure(1, weight=1)

        queue_toolbar = ttk.Frame(queue_tab)
        queue_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.queue_remove_btn = ttk.Button(queue_toolbar, text="Remove", command=self._remove_selected_queue_item)
        self.queue_remove_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_up_btn = ttk.Button(queue_toolbar, text="Move Up", command=lambda: self._move_selected_queue_item(-1))
        self.queue_up_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_down_btn = ttk.Button(queue_toolbar, text="Move Down", command=lambda: self._move_selected_queue_item(1))
        self.queue_down_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.queue_retry_btn = ttk.Button(queue_toolbar, text="Retry Failed", command=self._retry_failed_queue_items)
        self.queue_retry_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_clear_btn = ttk.Button(queue_toolbar, text="Clear Completed", command=self._clear_completed_history)
        self.queue_clear_btn.pack(side=tk.RIGHT)

        self.queue_tree = ttk.Treeview(
            queue_tab,
            columns=("service", "url", "status", "detail"),
            show="headings",
            selectmode="browse",
            height=3,
        )
        self.queue_tree.heading("service", text="Service")
        self.queue_tree.heading("url", text="URL / Title")
        self.queue_tree.heading("status", text="Status")
        self.queue_tree.heading("detail", text="Detail")
        self.queue_tree.column("service", width=90, stretch=False)
        self.queue_tree.column("url", width=220, stretch=True)
        self.queue_tree.column("status", width=90, stretch=False)
        self.queue_tree.column("detail", width=140, stretch=True)
        queue_scroll = ttk.Scrollbar(queue_tab, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=queue_scroll.set)
        self.queue_tree.grid(row=1, column=0, sticky="nsew")
        queue_scroll.grid(row=1, column=1, sticky="ns")
        self.activity_notebook.add(queue_tab, text="Queue & History")

        # Bind optional drag-and-drop URL support when tkinterdnd2 is available on host Tk.
        if hasattr(self.urls_text, "drop_target_register") and hasattr(self.urls_text, "dnd_bind"):
            try:
                self.urls_text.drop_target_register("DND_Text")  # type: ignore[attr-defined]
                self.urls_text.dnd_bind("<<Drop>>", self._on_url_drop)  # type: ignore[attr-defined]
            except Exception:
                pass

        self._interactive_widgets = [
            out_entry,
            out_browse,
            tpl_entry,
            self.preset_combo,
            self.service_combo,
            self.cookie_entry,
            self.cookie_browse,
            self.user_entry,
            self.password_entry,
            self.show_pw_btn,
            self.lib_entry,
            self.remember_cb,
            self.test_auth_btn,
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
            cb_enrich,
            cb_debug,
        ]

    def _build_menu_bar(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load URLs from .txt...", command=self._browse_url_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Open Output Folder", command=self._open_output_folder)
        file_menu.add_command(label="Reload Config", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Save Credentials", command=self.save_current_credentials)
        tools_menu.add_command(label="Test Login", command=self.verify_current_auth)
        tools_menu.add_command(label="Browse Library / Search...", command=self._show_catalog_browser_dialog)
        tools_menu.add_command(label="Locate FFmpeg Folder...", command=self.browse_ffmpeg_dir)
        tools_menu.add_command(label="Download History Ledger...", command=self._show_archive_ledger_dialog)
        tools_menu.add_separator()
        tools_menu.add_command(label="Retry Failed Jobs", command=self._retry_failed_queue_items)
        tools_menu.add_command(label="Clear Completed History", command=self._clear_completed_history)
        tools_menu.add_command(label="Clear Log", command=self._clear_log)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="FFmpeg Setup Guide", command=self._show_ffmpeg_guidance)
        if sys.platform.startswith("win"):
            help_menu.add_command(label="Install FFmpeg via winget...", command=self._install_ffmpeg_winget)
        help_menu.add_command(label="Check for Updates...", command=self.check_updates_interactive)
        help_menu.add_separator()
        help_menu.add_command(label="About audiobook-dl", command=self._show_about_dialog)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menubar)
        self.root.bind("<Control-o>", lambda _e: self._browse_url_file())
        self.root.bind("<Control-q>", lambda _e: self._on_close())

    def _on_url_drop(self, event: Any) -> None:
        dropped = getattr(event, "data", "")
        if not dropped:
            return
        urls = parse_url_text(str(dropped).strip("{}"))
        if urls:
            existing = parse_url_text(self.urls_text.get("1.0", tk.END))
            merged = list(dict.fromkeys([*existing, *urls]))
            self.urls_text.delete("1.0", tk.END)
            self.urls_text.insert("1.0", "\n".join(merged))
            self._on_urls_changed()

    def _reload_config(self) -> None:
        try:
            loaded = self._load_saved_config()
            with self._queue_lock:
                self._config = loaded
            self._load_service_into_form()
            self._update_ffmpeg_status()
            self.status_var.set("Config reloaded")
            self._append_log("Reloaded configuration.")
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc), parent=self.root)

    def _show_about_dialog(self) -> None:
        messagebox.showinfo(
            "About audiobook-dl",
            f"audiobook-dl v{__version__}\n"
            f"Supported services: {len(self._services)}\n"
            "https://github.com/jo1gi/audiobook-dl",
            parent=self.root,
        )

    def _archive_ledger_path(self) -> str:
        cfg = self._config_snapshot()
        if cfg.download_archive:
            return cfg.download_archive
        db_dir = cfg.database_directory or args.default_database_directory()
        return default_archive_path(db_dir)

    def _show_archive_ledger_dialog(self) -> ArchiveLedgerDialog:
        """Open a modal dialog displaying recorded download history from SQLite."""
        dialog = ArchiveLedgerDialog(self.root)
        dialog.title("Download History Ledger")
        dialog.transient(self.root)
        dialog.geometry("760x440")
        dialog.minsize(560, 320)

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        search_var = tk.StringVar()
        top_bar = ttk.Frame(body)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(top_bar, textvariable=search_var, width=32)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        tree = ttk.Treeview(
            body,
            columns=("downloaded_at", "source", "title", "author", "output_path"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("downloaded_at", text="Date (UTC)")
        tree.heading("source", text="Service")
        tree.heading("title", text="Title")
        tree.heading("author", text="Author")
        tree.heading("output_path", text="Output Path")
        tree.column("downloaded_at", width=140, stretch=False)
        tree.column("source", width=90, stretch=False)
        tree.column("title", width=180, stretch=True)
        tree.column("author", width=130, stretch=True)
        tree.column("output_path", width=180, stretch=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        archive_path = self._archive_ledger_path()

        def _populate(*_args: Any) -> None:
            for iid in tree.get_children():
                tree.delete(iid)
            if not os.path.exists(archive_path):
                return
            ledger = DownloadArchive(archive_path)
            for entry in ledger.list_entries(query=search_var.get(), limit=500):
                tree.insert(
                    "",
                    tk.END,
                    iid=str(entry.id),
                    values=(
                        entry.downloaded_at,
                        entry.source,
                        entry.title,
                        entry.author,
                        entry.output_path or "",
                    ),
                )

        def _remove_selected() -> None:
            sel = tree.selection()
            if not sel:
                return
            entry_id = int(sel[0])
            DownloadArchive(archive_path).delete_entry(entry_id)
            tree.delete(sel[0])

        def _clear_all() -> None:
            if not os.path.exists(archive_path):
                return
            if not messagebox.askyesno(
                "Clear Ledger",
                "Delete all entries from the download history ledger?",
                parent=dialog,
            ):
                return
            DownloadArchive(archive_path).clear()
            _populate()

        ttk.Button(top_bar, text="Refresh", command=_populate).pack(side=tk.LEFT)

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(btn_bar, text="Remove Selected", command=_remove_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_bar, text="Clear All", command=_clear_all).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)

        search_var.trace_add("write", _populate)
        _populate()
        dialog._tree = tree
        dialog._search_var = search_var
        dialog._refresh = _populate
        return dialog

    def _show_catalog_browser_dialog(self) -> CatalogBrowserDialog:
        """Open a modal dialog to search service catalogs or list saved libraries."""
        browseable = [
            svc for svc in self._services if svc.supports_search or svc.supports_library
        ]
        dialog = CatalogBrowserDialog(self.root)
        dialog.title("Browse Library / Search")
        dialog.transient(self.root)
        dialog.geometry("760x460")
        dialog.minsize(560, 340)

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        selected_service = self._selected_service()
        default_label = (
            selected_service.label
            if selected_service is not None and (selected_service.supports_search or selected_service.supports_library)
            else (browseable[0].label if browseable else "")
        )
        service_var = tk.StringVar(value=default_label)
        query_var = tk.StringVar()
        status_var = tk.StringVar(value="Choose a service and click Search or List Library.")

        top_bar = ttk.Frame(body)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(top_bar, text="Service:").pack(side=tk.LEFT, padx=(0, 6))
        svc_combo = ttk.Combobox(
            top_bar,
            textvariable=service_var,
            values=[svc.label for svc in browseable],
            state="readonly",
            width=22,
        )
        svc_combo.pack(side=tk.LEFT, padx=(0, 8))

        query_entry = ttk.Entry(top_bar, textvariable=query_var, width=28)
        query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        search_btn = ttk.Button(top_bar, text="Search")
        search_btn.pack(side=tk.LEFT, padx=(0, 6))
        library_btn = ttk.Button(top_bar, text="List Library")
        library_btn.pack(side=tk.LEFT)

        tree = ttk.Treeview(
            body,
            columns=("title", "author", "series", "url"),
            show="headings",
            selectmode="extended",
        )
        tree.heading("title", text="Title")
        tree.heading("author", text="Author")
        tree.heading("series", text="Series")
        tree.heading("url", text="URL")
        tree.column("title", width=220, stretch=True)
        tree.column("author", width=150, stretch=True)
        tree.column("series", width=130, stretch=True)
        tree.column("url", width=220, stretch=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        status_label = ttk.Label(btn_bar, textvariable=status_var, foreground="#555555")
        status_label.pack(side=tk.LEFT)

        def _sync_buttons(*_args: Any) -> None:
            svc = self._services_by_label.get(service_var.get())
            if svc is None:
                query_entry.configure(state="disabled")
                search_btn.configure(state="disabled")
                library_btn.configure(state="disabled")
                return
            query_entry.configure(state="normal" if svc.supports_search else "disabled")
            search_btn.configure(state="normal" if svc.supports_search else "disabled")
            library_btn.configure(state="normal" if svc.supports_library else "disabled")

        def _run_fetch(mode: str) -> None:
            svc = self._services_by_label.get(service_var.get())
            if svc is None:
                return
            query_text = query_var.get().strip()
            if mode == "search" and not query_text:
                status_var.set("Enter a search term first.")
                return
            self._catalog_request_id += 1
            request_id = self._catalog_request_id
            options = self.build_options(urls_override=[])
            cfg = self._config_snapshot()
            interaction = _GuiInteractionHandler(self._queue, threading.Event())
            search_btn.configure(state="disabled")
            library_btn.configure(state="disabled")
            status_var.set(f"{'Searching' if mode == 'search' else 'Loading library for'} {svc.label}...")

            def _worker() -> None:
                try:
                    if mode == "search":
                        books = search_service(
                            svc.key,
                            query_text,
                            options,
                            cfg,
                            interaction=interaction,
                        )
                    else:
                        books = list_service_library(
                            svc.key,
                            options,
                            cfg,
                            interaction=interaction,
                        )
                    self._queue.put(("catalog_loaded", request_id, dialog, books, None))
                except Exception as exc:
                    self._queue.put(("catalog_loaded", request_id, dialog, [], exc))

            threading.Thread(target=_worker, daemon=True).start()

        def _populate_items(books: Sequence[CatalogBook]) -> None:
            for iid in tree.get_children():
                tree.delete(iid)
            for idx, book in enumerate(books):
                tree.insert(
                    "",
                    tk.END,
                    iid=f"book-{idx}",
                    values=(book.title, book.author, book.series, book.url),
                )
            _sync_buttons()
            count = len(books)
            status_var.set(f"Found {count} book{'s' if count != 1 else ''}.")

        def _add_selected() -> None:
            selected_iids = tree.selection()
            if not selected_iids:
                selected_iids = tree.get_children()
            urls_to_add = []
            for iid in selected_iids:
                values = tree.item(iid, "values")
                if values and len(values) >= 4 and str(values[3]).strip():
                    urls_to_add.append(str(values[3]).strip())
            if not urls_to_add:
                return
            existing = parse_url_text(self.urls_text.get("1.0", tk.END))
            existing_set = set(existing)
            added = [u for u in urls_to_add if u not in existing_set]
            if added:
                combined = "\n".join([*existing, *added])
                self.urls_text.delete("1.0", tk.END)
                self.urls_text.insert("1.0", combined)
                self._on_urls_changed()
            self.status_var.set(f"Added {len(added)} URL{'s' if len(added) != 1 else ''} from catalog")
            dialog.destroy()

        search_btn.configure(command=lambda: _run_fetch("search"))
        library_btn.configure(command=lambda: _run_fetch("library"))
        query_entry.bind("<Return>", lambda _e: _run_fetch("search"))
        svc_combo.bind("<<ComboboxSelected>>", _sync_buttons)

        ttk.Button(btn_bar, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Add Selected to URLs", command=_add_selected).pack(side=tk.RIGHT, padx=(0, 6))

        _sync_buttons()
        dialog._tree = tree
        dialog._service_var = service_var
        dialog._query_var = query_var
        dialog._status_var = status_var
        dialog._sync_buttons = _sync_buttons
        dialog._populate_items = _populate_items
        dialog._run_fetch = _run_fetch
        dialog._add_selected = _add_selected
        return dialog

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

    def _install_ffmpeg_winget(self) -> None:
        """Install FFmpeg via Windows winget (`Gyan.FFmpeg`) on a background thread."""
        if not sys.platform.startswith("win") or not winget_available():
            messagebox.showinfo(
                "winget Unavailable",
                "Windows Package Manager (winget) was not found on PATH.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Install FFmpeg via winget",
            "Run `winget install -e --id Gyan.FFmpeg` to install FFmpeg?",
            parent=self.root,
        ):
            return
        self.status_var.set("Installing FFmpeg via winget...")
        self._append_log("Installing FFmpeg via winget (Gyan.FFmpeg)...")

        def _worker() -> None:
            ok, detail = install_ffmpeg_via_winget()
            self._queue.put(("ffmpeg_install_done", ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

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
        self.test_auth_btn.configure(
            state="disabled" if self._is_verifying_auth else save_state
        )
        self.save_auth_btn.configure(state=save_state)
        has_saved = service.key in self._config.sources
        self.forget_auth_btn.configure(state="normal" if has_saved else "disabled")

    def verify_current_auth(self) -> None:
        """Test authentication for the currently selected service in the background."""
        if self._is_verifying_auth or self._is_running:
            return
        service = self._selected_service()
        if service is None or not service.auth_methods:
            return
        options = self.build_options(urls_override=[])
        cfg = self._config_snapshot()
        interaction = _GuiInteractionHandler(self._queue, threading.Event())
        self._is_verifying_auth = True
        self.test_auth_btn.configure(state="disabled")
        self.status_var.set(f"Testing login for {service.label}...")

        def _worker() -> None:
            try:
                ok = verify_service_auth(service.key, options, cfg, interaction=interaction)
                self._queue.put(("auth_verified", service.label, ok, None))
            except Exception as exc:
                self._queue.put(("auth_verified", service.label, False, exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _schedule_update_check(self) -> None:
        """Run a quiet background update check at launch unless opted out."""
        if self._config.check_updates is False:
            return
        proxy = self._config.proxy

        def _worker() -> None:
            info = check_for_updates(proxy=proxy, respect_opt_out=True)
            if info is not None and info.has_update:
                self._queue.put(("update_checked", info, False))

        threading.Thread(target=_worker, daemon=True).start()

    def check_updates_interactive(self) -> None:
        """Check for newer releases from the Help menu."""
        self.status_var.set("Checking for updates...")
        proxy = self._config.proxy

        def _worker() -> None:
            info = check_for_updates(proxy=proxy, respect_opt_out=False)
            self._queue.put(("update_checked", info, True))

        threading.Thread(target=_worker, daemon=True).start()

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

    def _on_preset_selected(self, _event: Optional[tk.Event] = None) -> None:
        chosen = self.template_preset_var.get()
        tpl = _PRESET_LABEL_TO_TEMPLATE.get(chosen)
        if tpl:
            self.template_var.set(tpl)

    def _sync_preset_from_template(self) -> None:
        tpl = self.template_var.get().strip()
        target_label = _TEMPLATE_TO_PRESET_LABEL.get(tpl, "Custom")
        if self.template_preset_var.get() != target_label:
            self.template_preset_var.set(target_label)

    def _update_template_preview(self) -> None:
        folder = self.output_dir_var.get().strip() or default_output_dir()
        tpl = self.template_var.get().strip() or "{title}"
        full_tpl = os.path.join(folder, tpl)
        fmt = self.output_format_var.get().strip()
        ext = "m4b" if fmt in ("", "Original") else fmt
        try:
            rendered = output.gen_output_location(full_tpl, _SAMPLE_PREVIEW_METADATA, "")
            suffix = f".{ext}" if self.combine_var.get() else f"/01.{ext}"
            self.template_preview_var.set(f"{rendered}{suffix}")
        except Exception as exc:
            self.template_preview_var.set(f"(invalid template: {exc})")

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
        try:
            if sys.platform.startswith("win") and hasattr(os, "startfile"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as exc:
            messagebox.showerror(
                "Open Folder Failed",
                f"Could not open folder:\n{folder}\n\n{exc}",
                parent=self.root,
            )

    def _config_snapshot(self) -> Config:
        """Return a thread-safe shallow copy of the current cached configuration."""
        with self._queue_lock:
            cfg = self._config
            return Config(
                sources={
                    k: SourceConfig(
                        username=sc.username,
                        password=sc.password,
                        library=sc.library,
                        cookie_file=sc.cookie_file,
                    )
                    for k, sc in cfg.sources.items()
                },
                output_template=cfg.output_template,
                database_directory=cfg.database_directory,
                skip_downloaded=cfg.skip_downloaded,
                ffmpeg_dir=cfg.ffmpeg_dir,
                output_format=cfg.output_format,
                combine=cfg.combine,
                audio_bitrate=cfg.audio_bitrate,
                resume=cfg.resume,
                overwrite=cfg.overwrite,
                max_workers=cfg.max_workers,
                book_delay=cfg.book_delay,
                proxy=cfg.proxy,
                timeout=cfg.timeout,
                download_archive=cfg.download_archive,
                gui=cfg.gui,
            )

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    MAX_LOG_LINES = 5000

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            excess = line_count - self.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
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
            enrich_metadata=self.enrich_metadata_var.get(),
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

    @staticmethod
    def _make_job_id() -> str:
        return uuid.uuid4().hex[:12]

    def _service_label_for_url(self, url: str) -> str:
        detected = detect_service_key(url)
        if detected and detected in self._services_by_key:
            return self._services_by_key[detected].label
        return detected or "Unknown"

    def _register_queue_job(self, job: _QueuedJob) -> None:
        """Insert a queued job row into the Queue & History table and internal index."""
        service_label = self._service_label_for_url(job.url)
        self._queue_records[job.job_id] = {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "url": job.url,
            "status": "Queued",
            "detail": "",
            "service": service_label,
        }
        self.queue_tree.insert(
            "",
            tk.END,
            iid=job.job_id,
            values=(service_label, job.url, "Queued", ""),
        )

    def _update_queue_row(self, job_id: str, status: str, detail: str = "") -> None:
        rec = self._queue_records.get(job_id)
        if rec is None:
            return
        rec["status"] = status
        rec["detail"] = detail
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=(rec["service"], rec["url"], status, detail),
            )

    def _remove_selected_queue_item(self) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        job_id = selected[0]
        rec = self._queue_records.get(job_id)
        if rec is None:
            return
        with self._queue_cond:
            pending_idx: Optional[int] = None
            for idx, job in enumerate(self._pending_jobs):
                if job is not None and job.job_id == job_id:
                    pending_idx = idx
                    break
            if pending_idx is not None:
                del self._pending_jobs[pending_idx]
                self._batch_total = max(0, self._batch_total - 1)
                self._run_seen_urls.discard(rec["url"])
                new_total = self._batch_total
            elif rec["status"] == "Running":
                messagebox.showinfo(
                    "Job Active",
                    "The currently running download cannot be removed; use Cancel instead.",
                    parent=self.root,
                )
                return
            else:
                new_total = None
        if new_total is not None:
            self._ui_item_total = max(self._ui_item_index, new_total)
            self._refresh_batch_label()
        self._queue_records.pop(job_id, None)
        if self.queue_tree.exists(job_id):
            self.queue_tree.delete(job_id)

    def _move_selected_queue_item(self, direction: int) -> None:
        selected = self.queue_tree.selection()
        if not selected or direction not in (-1, 1):
            return
        job_id = selected[0]
        with self._queue_cond:
            pending_list = list(self._pending_jobs)
            pos = next(
                (i for i, job in enumerate(pending_list) if job is not None and job.job_id == job_id),
                None,
            )
            if pos is None:
                return
            target = pos + direction
            if target < 0 or target >= len(pending_list) or pending_list[target] is None:
                return
            neighbor_id = pending_list[target].job_id  # type: ignore[union-attr]
            pending_list[pos], pending_list[target] = pending_list[target], pending_list[pos]
            self._pending_jobs = deque(pending_list)

        if self.queue_tree.exists(job_id) and self.queue_tree.exists(neighbor_id):
            idx_a = self.queue_tree.index(job_id)
            idx_b = self.queue_tree.index(neighbor_id)
            self.queue_tree.move(job_id, "", idx_b)
            self.queue_tree.move(neighbor_id, "", idx_a)

    def _retry_failed_queue_items(self) -> None:
        failed_urls = [
            rec["url"]
            for iid in self.queue_tree.get_children()
            for rec in [self._queue_records.get(iid)]
            if rec is not None and rec["status"] in ("Failed", "Cancelled")
        ]
        if not failed_urls:
            self.status_var.set("No failed or cancelled jobs to retry")
            return
        if self._is_running:
            if not self._cancel_event.is_set():
                self._enqueue_additional_urls(failed_urls)
            return
        options = self.build_options()
        if not self._ensure_ffmpeg_available(options):
            return
        self._save_run_preferences(options)
        unique_urls = list(dict.fromkeys(failed_urls))
        job_specs = [
            (u, self.build_options(urls_override=[u]), None, None)
            for u in unique_urls
        ]
        self._start_new_run(job_specs)

    def _clear_completed_history(self) -> None:
        removable = [
            iid
            for iid in self.queue_tree.get_children()
            if self._queue_records.get(iid, {}).get("status") in ("Completed", "Failed", "Cancelled")
        ]
        for iid in removable:
            self._queue_records.pop(iid, None)
            if self.queue_tree.exists(iid):
                self.queue_tree.delete(iid)

    def _enqueue_additional_urls(self, urls: List[str]) -> bool:
        """Append new URLs to the currently active download queue."""
        if not self._validate_cookie_file():
            return True
        unique_urls = list(dict.fromkeys(urls))
        added_jobs: List[_QueuedJob] = []
        with self._queue_cond:
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
                job = _QueuedJob(
                    job_id=self._make_job_id(),
                    run_id=run_id,
                    url=u,
                    options=job_opts,
                    interaction=interaction,
                )
                self._pending_jobs.append(job)
                added_jobs.append(job)
            new_total = self._batch_total
            self._queue_cond.notify_all()
        for job in added_jobs:
            self._register_queue_job(job)
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
        loaded = self._load_saved_config()
        with self._queue_lock:
            self._config = loaded

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

        queued_jobs: List[_QueuedJob] = []
        with self._queue_cond:
            self._worker_run_id += 1
            run_id = self._worker_run_id
            self._current_ui_run_id = run_id
            self._sink.set_active_run_id(run_id)
            self._worker_has_active_run = True
            self._batch_total = len(job_specs)
            self._run_seen_urls = {url for url, _, _, _ in job_specs}
            for url, job_opts, selected_indices, prefetched in job_specs:
                job = _QueuedJob(
                    job_id=self._make_job_id(),
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=self._interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                )
                self._pending_jobs.append(job)
                queued_jobs.append(job)
            self._queue_cond.notify_all()

        for job in queued_jobs:
            self._register_queue_job(job)

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

    def _warn_mixed_service_credentials(self, urls: Sequence[str]) -> bool:
        """Warn before starting if batch URLs target an unconfigured auth service alongside another."""
        selected = self._selected_service()
        selected_key = selected.key if selected else None
        detected_keys: List[str] = []
        for url in urls:
            key = detect_service_key(url)
            if key and key not in detected_keys:
                detected_keys.append(key)
        if len(detected_keys) <= 1 and (not detected_keys or detected_keys[0] == selected_key):
            return True
        cfg = self._config_snapshot()
        missing: List[str] = []
        for key in detected_keys:
            if key == selected_key:
                continue
            info = self._services_by_key.get(key)
            if info is None or not info.auth_methods:
                continue
            src_cfg = cfg.sources.get(key)
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
            parent=self.root,
        )

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
        if not self._warn_mixed_service_credentials(urls):
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
            config = self._config_snapshot()
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

    def _show_preview_dialog(self, url: str, source: Source, result: Result) -> PreviewDialog:
        """Display resolved `Audiobook` metadata/cover or a selectable `Series` checklist."""
        if self._preview_dialog is not None and self._preview_dialog.winfo_exists():
            self._preview_dialog.destroy()

        dialog = PreviewDialog(self.root)
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
                dialog._cover_photo = photo
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
            filter_var = tk.StringVar()
            check_vars: List[tk.BooleanVar] = [tk.BooleanVar(value=True) for _ in result.books]
            dialog._check_vars = check_vars
            dialog._filter_var = filter_var
            checkbuttons: List[Tuple[int, str, ttk.Checkbutton]] = []
            dialog._checkbuttons = checkbuttons
            last_clicked: Dict[str, Optional[int]] = {"index": None}

            def _update_selected_count() -> None:
                chosen = sum(1 for v in check_vars if v.get())
                selected_count_var.set(f"Selected: {chosen} / {book_count}")

            def _set_all(val: bool) -> None:
                query = filter_var.get().strip().lower()
                if query:
                    for idx_item, label_item, _cb in checkbuttons:
                        if query in label_item.lower():
                            check_vars[idx_item].set(val)
                else:
                    for v in check_vars:
                        v.set(val)
                _update_selected_count()

            def _toggle_series_item(idx: int, shift_held: bool = False) -> None:
                if shift_held and last_clicked["index"] is not None:
                    start, end = sorted((last_clicked["index"], idx))
                    target_val = check_vars[idx].get()
                    query = filter_var.get().strip().lower()
                    for pos, label_item, _cb in checkbuttons:
                        if start <= pos <= end and (not query or query in label_item.lower()):
                            check_vars[pos].set(target_val)
                last_clicked["index"] = idx
                _update_selected_count()

            dialog._toggle_series_item = _toggle_series_item

            toolbar = ttk.Frame(body)
            toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
            ttk.Button(toolbar, text="Select All", command=lambda: _set_all(True)).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Button(toolbar, text="Deselect All", command=lambda: _set_all(False)).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
            filter_entry = ttk.Entry(toolbar, textvariable=filter_var, width=22)
            filter_entry.pack(side=tk.LEFT, padx=(0, 8))
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

                def _on_mousewheel(event: tk.Event) -> None:
                    delta = getattr(event, "delta", 0)
                    if delta:
                        step = -1 if delta > 0 else 1
                        if sys.platform != "darwin" and abs(delta) >= 120:
                            step = int(-delta / 120)
                        canvas.yview_scroll(step, "units")
                    elif getattr(event, "num", None) == 4:
                        canvas.yview_scroll(-1, "units")
                    elif getattr(event, "num", None) == 5:
                        canvas.yview_scroll(1, "units")

                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    canvas.bind(seq, _on_mousewheel)
                    inner.bind(seq, _on_mousewheel)

                check_skip = self.skip_downloaded_var.get()
                for idx, book in enumerate(result.books):
                    label = f"{idx + 1}. {describe_series_book(idx, book)}"
                    if check_skip and self._series_book_exists(book):
                        label = f"{label} [Exists]"
                    cb = ttk.Checkbutton(
                        inner,
                        text=label,
                        variable=check_vars[idx],
                    )
                    cb.configure(
                        command=lambda i=idx: _toggle_series_item(
                            i,
                            shift_state.get("held", False),
                        )
                    )
                    cb.pack(anchor="w", fill=tk.X, pady=1)
                    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                        cb.bind(seq, _on_mousewheel)
                    checkbuttons.append((idx, label, cb))

                shift_state: Dict[str, bool] = {"held": False}
                for _idx, _label, cb_widget in checkbuttons:
                    cb_widget.bind(
                        "<ButtonPress-1>",
                        lambda e, state=shift_state: state.__setitem__("held", bool(e.state & 0x0001)),
                    )

                def _apply_filter(*_args: Any) -> None:
                    query = filter_var.get().strip().lower()
                    for _pos, item_label, cb_widget in checkbuttons:
                        if not query or query in item_label.lower():
                            cb_widget.pack(anchor="w", fill=tk.X, pady=1)
                        else:
                            cb_widget.pack_forget()
                    canvas.configure(scrollregion=canvas.bbox("all"))

                filter_var.trace_add("write", _apply_filter)

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

    def _series_book_exists(self, book: Union[BookId[Any], Audiobook]) -> bool:
        """Return True when `book` already appears on disk at the configured output path."""
        if not isinstance(book, Audiobook):
            return False
        try:
            opts = self.build_options()
            template = opts.output_template or "{title}"
            target = output.gen_output_location(template, book.metadata, opts.remove_chars)
            if os.path.exists(target):
                return True
            if opts.output_format and os.path.exists(f"{target}.{opts.output_format}"):
                return True
            parent = os.path.dirname(target) or "."
            base = os.path.basename(target)
            if os.path.isdir(parent):
                for entry in os.listdir(parent):
                    stem, _ext = os.path.splitext(entry)
                    if stem == base:
                        return True
        except (OSError, ValueError, KeyError):
            return False
        return False

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
            added_job: Optional[_QueuedJob] = None
            new_total = 0
            with self._queue_cond:
                if self._worker_has_active_run:
                    if self._interaction is None:
                        self._interaction = _GuiInteractionHandler(self._queue, self._cancel_event)
                    self._batch_total += 1
                    self._run_seen_urls.add(url)
                    added_job = _QueuedJob(
                        job_id=self._make_job_id(),
                        run_id=self._worker_run_id,
                        url=url,
                        options=options,
                        interaction=self._interaction,
                        selected_indices=selected_indices,
                        prefetched=prefetched,
                    )
                    self._pending_jobs.append(added_job)
                    new_total = self._batch_total
                    self._queue_cond.notify_all()
            if added_job is not None:
                self._register_queue_job(added_job)
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

    def _drain_pending_jobs(self, cancel_status: str = "Cancelled") -> bool:
        """Remove queued jobs after cancellation/error; return True if close sentinel seen."""
        saw_sentinel = False
        cancelled_ids: List[str] = []
        with self._queue_cond:
            while self._pending_jobs:
                pending = self._pending_jobs.popleft()
                if pending is None:
                    saw_sentinel = True
                    break
                cancelled_ids.append(pending.job_id)
            self._worker_has_active_run = False
        for job_id in cancelled_ids:
            self._queue.put(("job_status", job_id, cancel_status, ""))
        return saw_sentinel

    def _worker_loop(self) -> None:
        while True:
            with self._queue_cond:
                while not self._pending_jobs:
                    self._queue_cond.wait()
                first_job = self._pending_jobs.popleft()
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
                    self._queue.put(("job_status", current_job.job_id, "Cancelled", ""))
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    break

                with self._queue_lock:
                    item_total = self._batch_total

                self._queue.put(("batch_item", run_id, item_index, item_total, current_job.url))
                self._queue.put(("job_status", current_job.job_id, "Running", ""))

                try:
                    config = self._config_snapshot()
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
                    batch_summary.books_completed += summary.books_completed
                    batch_summary.books_skipped += summary.books_skipped
                    batch_summary.books_failed += summary.books_failed
                    batch_summary.book_errors.extend(summary.book_errors)
                    if summary.cancelled:
                        batch_summary.cancelled = True
                        self._queue.put(("job_status", current_job.job_id, "Cancelled", ""))
                        saw_sentinel = self._drain_pending_jobs("Cancelled")
                        break
                    if summary.failed > 0 or summary.books_failed > 0:
                        detail = summary.book_errors[0] if summary.book_errors else "Download failed"
                        self._queue.put(("job_status", current_job.job_id, "Failed", detail[:120]))
                    else:
                        detail = (
                            f"{summary.books_completed} ok"
                            if summary.books_completed
                            else ("Skipped" if summary.books_skipped else "Done")
                        )
                        self._queue.put(("job_status", current_job.job_id, "Completed", detail))
                except Exception as exc:
                    fatal_exc = exc
                    self._queue.put(("job_status", current_job.job_id, "Failed", str(exc)[:120]))
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    break

                with self._queue_cond:
                    if not self._pending_jobs:
                        self._worker_has_active_run = False
                        current_job = None
                    else:
                        next_job = self._pending_jobs.popleft()
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
        elif kind == "job_status":
            _, job_id, status_text, detail = item
            self._update_queue_row(job_id, status_text, detail)
        elif kind == "item_progress":
            _, run_id, book_index, book_total, _title = item
            if run_id == self._current_ui_run_id:
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
        elif kind == "auth_verified":
            _, service_label, ok, auth_exc = item
            self._is_verifying_auth = False
            self._update_service_field_states()
            if auth_exc is not None:
                self.status_var.set(f"Login failed for {service_label}")
                self._append_log(f"[ERROR] Login check failed for {service_label}: {auth_exc}")
                messagebox.showerror("Test Login Failed", str(auth_exc), parent=self.root)
            elif ok:
                self.status_var.set(f"Login OK: {service_label}")
                self._append_log(f"Verified login for {service_label}.")
                messagebox.showinfo("Test Login", f"Authentication succeeded for {service_label}.", parent=self.root)
            else:
                self.status_var.set(f"Login invalid: {service_label}")
                messagebox.showwarning("Test Login", f"Authentication failed for {service_label}.", parent=self.root)
        elif kind == "catalog_loaded":
            _, req_id, dialog, books, cat_exc = item
            if req_id != self._catalog_request_id:
                return
            catalog_dialog: CatalogBrowserDialog = dialog
            try:
                if not catalog_dialog.winfo_exists():
                    return
            except tk.TclError:
                return
            if cat_exc is not None:
                catalog_dialog._sync_buttons()
                catalog_dialog._status_var.set("Error loading items")
                messagebox.showerror("Catalog Error", str(cat_exc), parent=catalog_dialog)
            else:
                catalog_dialog._populate_items(books)
        elif kind == "ffmpeg_install_done":
            _, ok, detail = item
            self._update_ffmpeg_status()
            if ok:
                self.status_var.set("FFmpeg installed")
                self._append_log(detail)
                messagebox.showinfo("FFmpeg Installed", detail, parent=self.root)
            else:
                self.status_var.set("FFmpeg install failed")
                self._append_log(f"[ERROR] {detail}")
                messagebox.showerror("FFmpeg Install Failed", detail, parent=self.root)
        elif kind == "update_checked":
            _, info, interactive = item
            self._handle_update_checked(info, interactive)
        elif kind == "done":
            _, run_id, summary, exc = item
            if run_id == self._current_ui_run_id:
                self._finish_run(summary, exc)

    def _handle_update_checked(self, info: Optional[UpdateInfo], interactive: bool) -> None:
        if info is None:
            if interactive:
                self.status_var.set("Update check failed")
                messagebox.showwarning(
                    "Update Check",
                    "Could not reach update server to check for new releases.",
                    parent=self.root,
                )
            return
        if info.has_update:
            msg = (
                f"A new version of audiobook-dl is available: v{info.latest_version} "
                f"(current: v{info.current_version}).\n\n"
                f"Download: {info.release_url}"
            )
            self.status_var.set(f"Update available: v{info.latest_version}")
            self._append_log(f"Update available: v{info.latest_version} ({info.release_url})")
            if interactive:
                messagebox.showinfo("Update Available", msg, parent=self.root)
        elif interactive:
            self.status_var.set(f"Up to date (v{info.current_version})")
            messagebox.showinfo(
                "Up to Date",
                f"You are running the latest version (v{info.current_version}).",
                parent=self.root,
            )

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
        with self._queue_cond:
            self._pending_jobs.append(None)
            self._queue_cond.notify_all()
        self.root.destroy()


def _enable_high_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows when supported by the OS."""
    if not sys.platform.startswith("win"):
        return
    try:
        shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass


def parse_gui_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments supported by the desktop GUI launcher."""
    parser = argparse.ArgumentParser(prog="audiobook-dl-gui")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate bundled assets and GUI initialization without opening a window",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_location",
        default=None,
        help="Path to an audiobook-dl TOML configuration file",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


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


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Launch the audiobook-dl desktop GUI."""
    parsed = parse_gui_args(argv)
    if parsed.self_test:
        run_self_test(check_gui=True)
        return
    _enable_high_dpi_awareness()
    root = tk.Tk()
    app = AudiobookDLApp(root, config_location=parsed.config_location)
    app._schedule_update_check()
    root.mainloop()


if __name__ == "__main__":
    main()

"""Action bar, progress metrics, scrollable activity log, and job queue panel."""

from collections import deque
import json
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from audiobookdl.gui.controller import GuiController, QueuedJob
from audiobookdl.gui.formatting import format_duration, format_speed, parse_url_text
from audiobookdl.gui.notifications import format_completion_notification, send_os_notification_async
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import ModalDialog, ScrolledTreeview, TreeColumnSpec
from audiobookdl.pipeline import DownloadSummary


def export_queue_to_path(records: Sequence[Mapping[str, Any]], path: str) -> int:
    """Export `records` as `.json` (full specs) or `.txt` (URLs); return exported count."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        urls = [
            str(r.get("url") or "").strip()
            for r in records
            if r.get("job_kind", "download") == "download" and str(r.get("url") or "").strip()
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(urls) + ("\n" if urls else ""))
        return len(urls)

    exported: list[dict[str, Any]] = []
    for r in records:
        item: dict[str, Any] = {
            "url": str(r.get("url") or ""),
            "job_kind": str(r.get("job_kind") or "download"),
            "service": str(r.get("service") or ""),
        }
        if r.get("profile"):
            item["profile"] = r["profile"]
        if r.get("selected_indices") is not None:
            item["selected_indices"] = list(r["selected_indices"])
        if isinstance(r.get("job_overrides"), Mapping):
            item["job_overrides"] = dict(r["job_overrides"])
        if isinstance(r.get("pack_request"), Mapping):
            item["pack_request"] = dict(r["pack_request"])
        exported.append(item)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "jobs": exported}, fh, indent=2, ensure_ascii=False)
    return len(exported)


def load_queue_import_file(path: str) -> list[dict[str, Any]]:
    """Load queued job descriptors from a `.json` or `.txt` file."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig") as fh:
        raw_text = fh.read()
    if ext == ".json":
        parsed = json.loads(raw_text)
        items = parsed.get("jobs") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            raise ValueError("JSON queue file must contain a list or {'jobs': [...]}.")
        out: list[dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, str) and entry.strip():
                out.append({"job_kind": "download", "url": entry.strip()})
            elif isinstance(entry, Mapping):
                out.append(dict(entry))
        return out

    return [{"job_kind": "download", "url": u} for u in parse_url_text(raw_text)]


class JobInspectorDialog(ModalDialog):
    """Themed modal dialog inspecting full job metadata, overrides, and error text."""

    def __init__(
        self,
        parent: tk.Misc,
        record: Mapping[str, Any],
        *,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "Job Inspector",
            palette=palette,
            default_size=(620, 420),
            min_size=(560, 360),
        )
        self._record = dict(record)

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(7, weight=1)

        fields = [
            ("Service:", str(record.get("service") or "")),
            ("Kind:", str(record.get("job_kind") or "download")),
            ("Status:", str(record.get("status") or "")),
            ("Title:", str(record.get("title") or "--")),
            ("Source / URL:", str(record.get("url") or "")),
            ("Output Path:", str(record.get("output_path") or "--")),
            (
                "Profile / Options:",
                self._format_overrides(record),
            ),
        ]
        for row_idx, (label, val) in enumerate(fields):
            ttk.Label(body, text=label, font=make_bold_font(self, 9)).grid(
                row=row_idx, column=0, sticky="nw", padx=(0, 8), pady=2
            )
            ttk.Label(body, text=val, wraplength=scale_px(self, 420), justify="left").grid(
                row=row_idx, column=1, sticky="w", pady=2
            )

        ttk.Label(body, text="Detail / Error:", font=make_bold_font(self, 9)).grid(
            row=7, column=0, sticky="nw", padx=(0, 8), pady=(6, 2)
        )
        detail_box = ScrolledText(body, height=6, width=52, wrap="word")
        detail_box.grid(row=7, column=1, sticky="nsew", pady=(6, 2))
        detail_box.insert("1.0", str(record.get("detail") or "(none)"))
        detail_box.configure(state="disabled")

        btns = ttk.Frame(body)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Copy Detail / Error", command=self._copy_detail).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        if record.get("output_path"):
            ttk.Button(btns, text="Copy Path", command=self._copy_path).pack(side=tk.LEFT)
        close_btn = ttk.Button(btns, text="Close", command=self.destroy)
        close_btn.pack(side=tk.RIGHT)

        self.finalize_modal(initial_focus=close_btn)

    @staticmethod
    def _format_overrides(record: Mapping[str, Any]) -> str:
        parts: list[str] = []
        if record.get("profile"):
            parts.append(f"profile={record['profile']}")
        if record.get("selected_indices") is not None:
            parts.append(f"books={len(record['selected_indices'])} selected")
        ov = record.get("job_overrides")
        if isinstance(ov, Mapping):
            for k, v in ov.items():
                if k != "output_template":
                    parts.append(f"{k}={v}")
        return ", ".join(parts) if parts else "Default"

    def _copy_detail(self) -> None:
        detail = str(self._record.get("detail") or "").strip()
        if detail:
            self.clipboard_clear()
            self.clipboard_append(detail)

    def _copy_path(self) -> None:
        path = str(self._record.get("output_path") or "").strip()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)


class ActivityQueuePanel(ttk.LabelFrame):
    """LabelFrame managing action buttons, progress/metrics display, activity log, and queue table."""

    MAX_LOG_LINES = 5000

    def __init__(
        self,
        master: tk.Misc,
        root: tk.Tk,
        controller: GuiController,
        *,
        status_var: tk.StringVar,
        batch_var: tk.StringVar,
        metrics_var: tk.StringVar,
        on_start_download: Callable[[], None],
        on_cancel_download: Callable[[], None],
        on_preview_metadata: Callable[[], None],
        on_pack_local: Callable[[], Any],
        on_open_folder: Callable[[], None],
        on_retry_failed: Callable[[], None],
        service_label_for_url: Callable[[str], str],
        on_retry_selected: Callable[[Sequence[str]], None] | None = None,
        on_preview_url: Callable[[str], None] | None = None,
        on_send_to_urls: Callable[[Sequence[str]], None] | None = None,
        on_open_path: Callable[[str, bool], None] | None = None,
        on_import_queue: Callable[[list[dict[str, Any]]], None] | None = None,
        on_queue_summary_changed: Callable[[int, int], None] | None = None,
        on_progress_mode_changed: Callable[[str], None] | None = None,
        progress_var: tk.DoubleVar | None = None,
        active_job_var: tk.StringVar | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(master, text="Activity", padding=(10, 6))
        self._window = root
        self._controller = controller
        self._palette = palette or LIGHT_PALETTE
        self.status_var = status_var
        self.batch_var = batch_var
        self.stage_var = tk.StringVar(value="")
        self.metrics_var = metrics_var
        self.progress_var = progress_var or tk.DoubleVar(value=0.0)
        self.active_job_var = active_job_var or tk.StringVar(value="Idle")
        self._on_start_download = on_start_download
        self._on_cancel_download = on_cancel_download
        self._on_preview_metadata = on_preview_metadata
        self._on_pack_local = on_pack_local
        self._on_open_folder = on_open_folder
        self._on_retry_failed = on_retry_failed
        self._service_label_for_url = service_label_for_url
        self._on_retry_selected = on_retry_selected
        self._on_preview_url = on_preview_url
        self._on_send_to_urls = on_send_to_urls
        self._on_open_path = on_open_path
        self._on_import_queue = on_import_queue
        self._on_queue_summary_changed = on_queue_summary_changed
        self._on_progress_mode_changed = on_progress_mode_changed

        self.current_ui_run_id = 0
        self.ui_item_index = 0
        self.ui_item_total = 0
        self.ui_book_index = 0
        self.ui_book_total = 0
        self._run_start_time: float | None = None
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds: float | None = None
        self._last_metrics_render_at = 0.0
        self._log_line_count = 0
        self._log_entries: deque[tuple[str, str]] = deque(maxlen=self.MAX_LOG_LINES)
        self.log_level_var = tk.StringVar(value="All")
        self.log_search_var = tk.StringVar(value="")
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.queue_filter_var = tk.StringVar(value="All")
        self.queue_search_var = tk.StringVar(value="")
        self._queue_order: list[str] = []
        self._drag_job_id: str | None = None

        self._build_widgets()
        self.apply_theme_palette(self._palette)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        action_bar = ttk.Frame(self)
        action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        action_bar.columnconfigure(7, weight=1)

        self.download_btn = ttk.Button(action_bar, text="Download", command=self._on_start_download)
        self.download_btn.grid(row=0, column=0, padx=(0, 6))

        self.pause_btn = ttk.Button(action_bar, text="Pause", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=1, padx=(0, 6))

        self.cancel_btn = ttk.Button(action_bar, text="Cancel", command=self._on_cancel_download)
        self.cancel_btn.grid(row=0, column=2, padx=(0, 6))

        self.preview_btn = ttk.Button(action_bar, text="Preview", command=self._on_preview_metadata)
        self.preview_btn.grid(row=0, column=3, padx=(0, 6))

        self.pack_btn = ttk.Button(action_bar, text="Pack Local...", command=self._on_pack_local)
        self.pack_btn.grid(row=0, column=4, padx=(0, 6))

        self.open_folder_btn = ttk.Button(
            action_bar, text="Open Folder", command=self._on_open_folder
        )
        self.open_folder_btn.grid(row=0, column=5, padx=(0, 6))

        ttk.Button(action_bar, text="Clear Log", command=self.clear_log).grid(row=0, column=6)

        self.status_label = ttk.Label(action_bar, textvariable=self.status_var, anchor="e")
        self.status_label.grid(row=0, column=7, sticky="e")

        meta_bar = ttk.Frame(self)
        meta_bar.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        meta_bar.columnconfigure(1, weight=1)

        self.batch_label = ttk.Label(meta_bar, textvariable=self.batch_var, style="Secondary.TLabel")
        self.batch_label.grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.stage_label = ttk.Label(meta_bar, textvariable=self.stage_var, style="Muted.TLabel")
        self.stage_label.grid(row=0, column=1, sticky="w")

        self.metrics_label = ttk.Label(
            meta_bar, textvariable=self.metrics_var, style="Muted.TLabel", anchor="e"
        )
        self.metrics_label.grid(row=0, column=2, sticky="e")

        self.progressbar = ttk.Progressbar(
            self,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        )
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self.activity_notebook = ttk.Notebook(self)
        self.activity_notebook.grid(row=3, column=0, sticky="nsew")

        log_tab = ttk.Frame(self.activity_notebook)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(1, weight=1)

        log_toolbar = ttk.Frame(log_tab, padding=(4, 4))
        log_toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(log_toolbar, text="Level:").pack(side=tk.LEFT, padx=(0, 4))
        level_combo = ttk.Combobox(
            log_toolbar,
            textvariable=self.log_level_var,
            values=("All", "Info+", "Errors Only"),
            state="readonly",
            width=11,
        )
        level_combo.pack(side=tk.LEFT, padx=(0, 8))
        level_combo.bind("<<ComboboxSelected>>", lambda _e: self._rerender_logs())
        ttk.Label(log_toolbar, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        log_search_entry = ttk.Entry(log_toolbar, textvariable=self.log_search_var, width=20)
        log_search_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.log_search_var.trace_add("write", lambda *_args: self._rerender_logs())
        ttk.Checkbutton(log_toolbar, text="Auto-scroll", variable=self.autoscroll_var).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(log_toolbar, text="Save Log...", command=self._save_log_to_file).pack(
            side=tk.RIGHT
        )

        self.log_text = ScrolledText(log_tab, height=4, width=40, state="disabled", wrap="word")
        try:
            fixed_font = tkfont.nametofont("TkFixedFont", root=self._window)
            self.log_text.configure(font=fixed_font)
        except tk.TclError:
            pass
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.activity_notebook.add(log_tab, text="Activity Log")

        queue_tab = ttk.Frame(self.activity_notebook, padding=4)
        queue_tab.columnconfigure(0, weight=1)
        queue_tab.rowconfigure(2, weight=1)
        self._queue_tab = queue_tab

        queue_toolbar = ttk.Frame(queue_tab)
        queue_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        self.queue_remove_btn = ttk.Button(
            queue_toolbar, text="Remove", command=self.remove_selected_item
        )
        self.queue_remove_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_up_btn = ttk.Button(
            queue_toolbar, text="Move Up", command=lambda: self.move_selected_item(-1)
        )
        self.queue_up_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_down_btn = ttk.Button(
            queue_toolbar, text="Move Down", command=lambda: self.move_selected_item(1)
        )
        self.queue_down_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.queue_retry_btn = ttk.Button(
            queue_toolbar, text="Retry Failed", command=self._on_retry_failed
        )
        self.queue_retry_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_open_btn = ttk.Button(
            queue_toolbar,
            text="Open Output",
            command=lambda: self._open_selected_output(reveal_in_folder=False),
        )
        self.queue_open_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_clear_btn = ttk.Button(
            queue_toolbar, text="Clear Completed", command=self.clear_completed_history
        )
        self.queue_clear_btn.pack(side=tk.RIGHT)

        queue_filter_bar = ttk.Frame(queue_tab)
        queue_filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(queue_filter_bar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        q_filter_combo = ttk.Combobox(
            queue_filter_bar,
            textvariable=self.queue_filter_var,
            values=("All", "Queued", "Running", "Completed", "Failed"),
            state="readonly",
            width=11,
        )
        q_filter_combo.pack(side=tk.LEFT, padx=(0, 8))
        q_filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_queue_filter())
        ttk.Label(queue_filter_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        q_search_entry = ttk.Entry(queue_filter_bar, textvariable=self.queue_search_var, width=24)
        q_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.queue_search_var.trace_add("write", lambda *_args: self._apply_queue_filter())

        queue_columns = (
            TreeColumnSpec("service", "Service", width=90, stretch=False),
            TreeColumnSpec("url", "URL / Title", width=220, stretch=True),
            TreeColumnSpec("status", "Status", width=90, stretch=False),
            TreeColumnSpec("detail", "Detail", width=140, stretch=True),
        )
        self._scrolled_queue = ScrolledTreeview(
            queue_tab, queue_columns, selectmode="extended", height=3
        )
        self._scrolled_queue.grid(row=2, column=0, sticky="nsew")
        self.queue_tree = self._scrolled_queue.tree
        self._build_queue_context_menu()
        self.queue_tree.bind("<Double-1>", self._on_queue_double_click)
        self.queue_tree.bind("<Delete>", lambda _e: self.remove_selected_item())
        self.queue_tree.bind("<ButtonPress-1>", self._on_queue_drag_start, add="+")
        self.queue_tree.bind("<ButtonRelease-1>", self._on_queue_drag_drop, add="+")
        self.queue_tree.bind("<Button-3>", self._show_queue_context_menu)
        if sys.platform == "darwin":
            self.queue_tree.bind("<Button-2>", self._show_queue_context_menu)
        self.activity_notebook.add(queue_tab, text="Queue & History")
        self._refresh_queue_empty_and_badge()

    def _build_queue_context_menu(self) -> None:
        menu = tk.Menu(self.queue_tree, tearoff=0)
        menu.add_command(
            label="Open Output",
            command=lambda: self._open_selected_output(reveal_in_folder=False),
        )
        menu.add_command(
            label="Open Containing Folder",
            command=lambda: self._open_selected_output(reveal_in_folder=True),
        )
        menu.add_command(label="View Job Details...", command=self._show_selected_details)
        menu.add_separator()
        menu.add_command(label="Retry Selected", command=self._retry_selected_items)
        menu.add_command(label="Preview Metadata...", command=self._preview_selected_item)
        menu.add_command(label="Send URL to Input", command=self._send_selected_to_urls)
        menu.add_separator()
        menu.add_command(label="Copy URL / Source", command=self._copy_selected_urls)
        menu.add_command(label="Copy Detail / Error", command=self._copy_selected_detail)
        menu.add_separator()
        menu.add_command(label="Move to Top", command=lambda: self.move_selected_to_edge(to_top=True))
        menu.add_command(label="Move Up", command=lambda: self.move_selected_item(-1))
        menu.add_command(label="Move Down", command=lambda: self.move_selected_item(1))
        menu.add_command(label="Move to Bottom", command=lambda: self.move_selected_to_edge(to_top=False))
        menu.add_command(label="Remove Selected", command=self.remove_selected_item)
        menu.add_command(label="Clear Completed & Failed", command=self.clear_completed_history)
        menu.add_separator()
        menu.add_command(label="Export Queue...", command=self.export_queue_dialog)
        if self._on_import_queue is not None:
            menu.add_command(label="Import Queue...", command=self.import_queue_dialog)
        self._queue_menu = menu

    def export_queue_dialog(self) -> None:
        """Prompt for a file path and export current queue records as JSON or text URLs."""
        with self._controller.queue_lock:
            records = [
                dict(self._controller.queue_records[jid])
                for jid in self._queue_order
                if jid in self._controller.queue_records
            ]
        if not records:
            messagebox.showinfo("Queue Empty", "There are no queue items to export.", parent=self._window)
            return
        path = filedialog.asksaveasfilename(
            parent=self._window,
            title="Export Queue",
            defaultextension=".json",
            filetypes=[("JSON Queue File", "*.json"), ("Text URL List", "*.txt")],
        )
        if not path:
            return
        try:
            count = export_queue_to_path(records, path)
            self.status_var.set(f"Exported {count} queue item(s)")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self._window)

    def import_queue_dialog(self) -> None:
        """Prompt for a `.json` or `.txt` queue file and enqueue imported jobs."""
        if self._on_import_queue is None:
            return
        path = filedialog.askopenfilename(
            parent=self._window,
            title="Import Queue",
            filetypes=[("Queue files", "*.json *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            items = load_queue_import_file(path)
        except Exception as exc:
            messagebox.showerror("Import Failed", str(exc), parent=self._window)
            return
        if not items:
            messagebox.showinfo("Empty File", "No queue items found in the selected file.", parent=self._window)
            return
        self._on_import_queue(items)

    def apply_theme_palette(self, palette: ThemePalette) -> None:
        """Apply semantic log and queue status tag colors from `palette`."""
        self._palette = palette
        self.log_text.tag_configure("error", foreground=palette.error)
        self.log_text.tag_configure("warn", foreground=palette.warning)
        self.log_text.tag_configure("debug", foreground=palette.fg_muted)
        self.log_text.tag_configure("section", foreground=palette.accent)
        self.log_text.tag_configure("info", foreground=palette.text_fg)
        self.queue_tree.tag_configure("running", foreground=palette.accent)
        self.queue_tree.tag_configure("completed", foreground=palette.success)
        self.queue_tree.tag_configure("failed", foreground=palette.error)
        self.queue_tree.tag_configure("paused", foreground=palette.warning)
        self.queue_tree.tag_configure("interrupted", foreground=palette.warning)
        self.queue_tree.tag_configure("cancelled", foreground=palette.fg_muted)
        self.queue_tree.tag_configure("queued", foreground=palette.fg)

    @staticmethod
    def _status_tag(status: str) -> tuple[str, ...]:
        lower = status.strip().lower()
        if lower in ("running", "completed", "failed", "paused", "interrupted", "cancelled", "queued"):
            return (lower,)
        return ()

    @staticmethod
    def _classify_log_tag(level: str, text: str) -> str:
        stripped = text.strip()
        if level == "ERROR" or stripped.startswith("[ERROR]"):
            return "error"
        if level == "WARNING" or stripped.startswith("[WARN]") or stripped.startswith("[WARNING]"):
            return "warn"
        if level == "DEBUG" or stripped.startswith("[DEBUG]"):
            return "debug"
        if stripped.startswith("---") and stripped.endswith("---"):
            return "section"
        return "info"

    def _log_entry_matches(self, level: str, text: str) -> bool:
        level_filter = self.log_level_var.get()
        tag = self._classify_log_tag(level, text)
        if level_filter == "Info+" and tag == "debug":
            return False
        if level_filter == "Errors Only" and tag not in ("error", "warn"):
            return False
        query = self.log_search_var.get().strip().lower()
        if query and query not in text.lower():
            return False
        return True

    def _save_log_to_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self._window,
            title="Save Activity Log",
            defaultextension=".log",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for _level, line in self._log_entries:
                    fh.write(line + "\n")
            self.status_var.set("Saved activity log")
        except OSError as exc:
            messagebox.showerror("Save Log Failed", str(exc), parent=self._window)

    def _rerender_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self._log_line_count = 0
        for level, line in self._log_entries:
            if not self._log_entry_matches(level, line):
                continue
            tag = self._classify_log_tag(level, line)
            self.log_text.insert(tk.END, line + "\n", (tag,))
            self._log_line_count += line.count("\n") + 1
        if self.autoscroll_var.get():
            self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self._log_entries.clear()
        self._log_line_count = 0
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log_batch(self, entries: Sequence[tuple[str, str]]) -> None:
        """Append a batch of `(level, line)` entries efficiently with filtering and smart auto-scroll."""
        if not entries:
            return
        try:
            was_at_bottom = self.log_text.yview()[1] >= 0.99
        except tk.TclError:
            was_at_bottom = True

        matched: list[tuple[str, str]] = []
        for level, text in entries:
            self._log_entries.append((level, text))
            if self._log_entry_matches(level, text):
                matched.append((text, self._classify_log_tag(level, text)))

        if not matched:
            return

        self.log_text.configure(state="normal")
        for text, tag in matched:
            self.log_text.insert(tk.END, text + "\n", (tag,))
            self._log_line_count += text.count("\n") + 1
        if self._log_line_count > self.MAX_LOG_LINES:
            excess = self._log_line_count - self.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
            self._log_line_count -= excess
        if self.autoscroll_var.get() and was_at_bottom:
            self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, text: str) -> None:
        """Append a line to the scrollable activity log, capping total line count."""
        tag = self._classify_log_tag("INFO", text)
        level = {"error": "ERROR", "warn": "WARNING", "debug": "DEBUG"}.get(tag, "INFO")
        self.append_log_batch([(level, text)])

    def set_running_state(
        self,
        running: bool,
        *,
        is_previewing: bool,
        is_syncing: bool = False,
    ) -> None:
        if running:
            self.download_btn.configure(text="Add to Queue", state="normal")
            self.pause_btn.configure(
                text="Resume" if self._controller.is_paused() else "Pause",
                state="normal",
            )
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        elif is_syncing:
            self.download_btn.configure(text="Download", state="disabled")
            self.pause_btn.configure(text="Pause", state="disabled")
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        else:
            self.download_btn.configure(text="Download", state="normal")
            self.pause_btn.configure(text="Pause", state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.preview_btn.configure(state="disabled" if is_previewing else "normal")

    def set_previewing_state(self, previewing: bool, *, is_running: bool) -> None:
        if previewing:
            self.preview_btn.configure(state="disabled")
            self.download_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
        else:
            self.preview_btn.configure(state="disabled" if is_running else "normal")
            self.download_btn.configure(state="disabled" if is_running else "normal")
            self.cancel_btn.configure(state="normal" if is_running else "disabled")

    def toggle_pause(self, *, is_running: bool | None = None) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        active = self._controller.worker_has_active_run if is_running is None else is_running
        if not active:
            return
        paused = self._controller.toggle_pause()
        self.pause_btn.configure(text="Resume" if paused else "Pause")
        if paused:
            self.status_var.set("Queue paused (pausing before next job)")
            self.append_log("--- Queue paused ---")
        else:
            self.status_var.set("Queue resumed")
            self.append_log("--- Queue resumed ---")

    def refresh_batch_label(self) -> None:
        parts = []
        if self.ui_item_total > 0 and self.ui_item_index > 0:
            parts.append(f"Item {self.ui_item_index} / {self.ui_item_total}")
        if self.ui_book_total > 1 and self.ui_book_index > 0:
            parts.append(f"Book {self.ui_book_index} / {self.ui_book_total}")
        self.batch_var.set("  \u2022  ".join(parts))

    def update_metrics_display(self, *, is_running: bool, force: bool = False) -> None:
        if not is_running or self._run_start_time is None:
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

    def _update_stage_from_message(self, message: str) -> None:
        lower = message.strip().lower()
        if not lower:
            return
        if "combining" in lower or "packing" in lower:
            self.stage_var.set("Stage: Combining & muxing")
        elif "converting" in lower:
            self.stage_var.set("Stage: Converting audio")
        elif "splitting" in lower:
            self.stage_var.set("Stage: Splitting chapters")
        elif "enrich" in lower:
            self.stage_var.set("Stage: Enriching metadata")
        elif "adding chapters" in lower:
            self.stage_var.set("Stage: Writing chapters")
        elif "adding metadata" in lower:
            self.stage_var.set("Stage: Writing tags")
        elif "embedding cover" in lower or "adding cover" in lower:
            self.stage_var.set("Stage: Embedding cover")
        elif "uploading" in lower or "audiobookshelf" in lower:
            self.stage_var.set("Stage: Syncing library")
        elif "downloading" in lower:
            self.stage_var.set("Stage: Downloading")

    def _set_progress(self, value: float, *, indeterminate: bool = False) -> None:
        if indeterminate:
            self.progressbar.configure(mode="indeterminate")
            self.progress_var.set(0.0)
            self.progressbar.start(12)
            if self._on_progress_mode_changed is not None:
                self._on_progress_mode_changed("indeterminate")
            return
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate")
        if self._on_progress_mode_changed is not None:
            self._on_progress_mode_changed("determinate")
        self.progress_var.set(float(value))

    def prepare_run_ui_state(self, run_id: int, item_total: int, log_header: str) -> None:
        """Initialize progress, timer, and batch counters for a fresh queue run."""
        self._set_progress(0.0)
        self.status_var.set("Starting...")
        self.active_job_var.set("Starting...")
        self.stage_var.set("Stage: Preparing")
        self._run_start_time = time.monotonic()
        self._last_pct = 0.0
        self._last_speed_bps = 0.0
        self._last_eta_seconds = None
        self._last_metrics_render_at = 0.0
        self.current_ui_run_id = run_id
        self.ui_item_index = 1
        self.ui_item_total = item_total
        self.ui_book_index = 0
        self.ui_book_total = 0
        self.refresh_batch_label()
        self.update_metrics_display(is_running=True, force=True)
        self.append_log(log_header)

    @staticmethod
    def format_queue_row_values(rec: Mapping[str, Any]) -> tuple[str, str, str, str]:
        """Format `(service, display_target, status, detail)` columns for `queue_tree`."""
        service = str(rec.get("service") or "")
        url = str(rec.get("url") or "")
        title = str(rec.get("title") or "").strip()
        job_kind = str(rec.get("job_kind") or "download")
        if title and title != url:
            display_target = title if job_kind == "pack" else f"{title} \u2014 {url}"
        else:
            display_target = url
        return (service, display_target, str(rec.get("status") or ""), str(rec.get("detail") or ""))

    def _queue_record_matches_filter(self, rec: Mapping[str, Any] | None) -> bool:
        if rec is None:
            return False
        filt = self.queue_filter_var.get()
        st = str(rec.get("status") or "")
        if filt == "Queued" and st not in ("Queued", "Paused"):
            return False
        if filt == "Running" and st != "Running":
            return False
        if filt == "Completed" and st != "Completed":
            return False
        if filt == "Failed" and st not in ("Failed", "Cancelled", "Interrupted"):
            return False
        q = self.queue_search_var.get().strip().lower()
        if q:
            hay = " ".join(
                str(rec.get(k) or "") for k in ("service", "title", "url", "status", "detail")
            ).lower()
            if q not in hay:
                return False
        return True

    def _apply_queue_filter(self) -> None:
        pos = 0
        for iid in list(self._queue_order):
            if not self.queue_tree.exists(iid):
                continue
            rec = self._controller.get_queue_record(iid)
            if self._queue_record_matches_filter(rec):
                self.queue_tree.reattach(iid, "", pos)
                pos += 1
            else:
                self.queue_tree.detach(iid)
        self._refresh_queue_empty_and_badge()

    def _refresh_queue_empty_and_badge(self) -> None:
        visible = self.queue_tree.get_children()
        self._scrolled_queue.set_empty_state(
            not visible,
            "No matching jobs in queue." if self._queue_order else "Queue is empty. Enter URLs above or browse a library to get started.",
        )
        active = 0
        failed = 0
        for iid in self._queue_order:
            rec = self._controller.get_queue_record(iid)
            st = str((rec or {}).get("status") or "")
            if st in ("Running", "Queued", "Paused"):
                active += 1
            elif st in ("Failed", "Interrupted"):
                failed += 1
        parts: list[str] = []
        if active:
            parts.append(f"{active} active")
        if failed:
            parts.append(f"{failed} failed")
        label = f"Queue & History ({', '.join(parts)})" if parts else "Queue & History"
        try:
            self.activity_notebook.tab(self._queue_tab, text=label)  # type: ignore[no-untyped-call]
        except tk.TclError:
            pass
        if self._on_queue_summary_changed is not None:
            self._on_queue_summary_changed(active, failed)

    def restore_saved_queue_history(self) -> None:
        """Restore unresolved queue items persisted across GUI restarts."""
        for rec in self._controller.load_saved_queue_records():
            self._queue_order.append(rec["job_id"])
            self.queue_tree.insert(
                "",
                tk.END,
                iid=rec["job_id"],
                values=self.format_queue_row_values(rec),
                tags=self._status_tag(str(rec.get("status") or "")),
            )
        self._apply_queue_filter()

    def register_queue_job(self, job: QueuedJob, *, persist: bool = True) -> None:
        """Insert a queued job row into the Queue & History table and internal index."""
        service_label = (
            "Local Pack" if job.pack_request is not None else self._service_label_for_url(job.url)
        )
        rec = self._controller.register_queue_record(job, service_label, persist=persist)
        self._queue_order.append(job.job_id)
        self.queue_tree.insert(
            "",
            tk.END,
            iid=job.job_id,
            values=self.format_queue_row_values(rec),
            tags=self._status_tag(str(rec.get("status") or "")),
        )
        self._apply_queue_filter()

    def update_queue_row(self, job_id: str, status: str, detail: str = "") -> None:
        rec = self._controller.update_queue_record(job_id, status, detail)
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=self.format_queue_row_values(rec),
                tags=self._status_tag(status),
            )
        self._apply_queue_filter()

    def update_queue_row_metadata(
        self,
        job_id: str,
        *,
        title: str | None = None,
        output_path: str | None = None,
    ) -> None:
        """Update resolved title / output path on a queue row and refresh its display."""
        rec = self._controller.update_queue_metadata(
            job_id,
            title=title,
            output_path=output_path,
        )
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=self.format_queue_row_values(rec),
            )

    def _selected_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for iid in self.queue_tree.selection():
            rec = self._controller.get_queue_record(iid)
            if rec is not None:
                out.append(rec)
        return out

    @staticmethod
    def _resolve_record_path(rec: Mapping[str, Any], *, reveal_in_folder: bool) -> str | None:
        out_path = str(rec.get("output_path") or "").strip()
        if out_path:
            if os.path.exists(out_path):
                return out_path
            if reveal_in_folder:
                parent = os.path.dirname(out_path)
                if parent and os.path.isdir(parent):
                    return parent
        if reveal_in_folder and rec.get("job_kind") == "pack":
            pack_req = rec.get("pack_request")
            if isinstance(pack_req, Mapping):
                for raw_path in pack_req.get("paths") or ():
                    cand = str(raw_path).strip()
                    if cand and os.path.exists(cand):
                        return cand
        return None

    def _open_selected_output(self, *, reveal_in_folder: bool = False) -> None:
        records = self._selected_records()
        if not records or self._on_open_path is None:
            return
        target = self._resolve_record_path(records[0], reveal_in_folder=reveal_in_folder)
        if not target:
            self.status_var.set("No output file or folder recorded for selected job")
            return
        self._on_open_path(target, reveal_in_folder)

    def _retry_selected_items(self) -> None:
        selected = list(self.queue_tree.selection())
        if not selected or self._on_retry_selected is None:
            return
        self._on_retry_selected(selected)

    def _preview_selected_item(self) -> None:
        records = self._selected_records()
        if len(records) != 1 or self._on_preview_url is None:
            return
        rec = records[0]
        if rec.get("job_kind", "download") == "download" and rec.get("url"):
            self._on_preview_url(str(rec["url"]))

    def _send_selected_to_urls(self) -> None:
        if self._on_send_to_urls is None:
            return
        urls = [
            str(rec["url"])
            for rec in self._selected_records()
            if rec.get("job_kind", "download") == "download" and rec.get("url")
        ]
        if urls:
            self._on_send_to_urls(urls)

    def _copy_selected_urls(self) -> None:
        values = [str(rec.get("url") or "") for rec in self._selected_records() if rec.get("url")]
        if not values:
            return
        self._window.clipboard_clear()
        self._window.clipboard_append("\n".join(values))
        self.status_var.set(f"Copied {len(values)} item{'s' if len(values) != 1 else ''}")

    def _copy_selected_detail(self) -> None:
        records = self._selected_records()
        if len(records) != 1:
            return
        detail = str(records[0].get("detail") or "").strip()
        if not detail:
            return
        self._window.clipboard_clear()
        self._window.clipboard_append(detail)
        self.status_var.set("Copied job detail to clipboard")

    def _show_selected_details(self) -> None:
        records = self._selected_records()
        if len(records) != 1:
            return
        JobInspectorDialog(self._window, records[0], palette=self._palette)

    def _on_queue_double_click(self, event: tk.Event) -> None:
        row_iid = self.queue_tree.identify_row(event.y)
        if not row_iid:
            return
        self.queue_tree.focus(row_iid)
        self.queue_tree.selection_set(row_iid)
        rec = self._controller.get_queue_record(row_iid)
        if rec is None:
            return
        if rec.get("status") == "Completed" and self._on_open_path is not None:
            target = self._resolve_record_path(rec, reveal_in_folder=False)
            if target:
                self._on_open_path(target, False)
                return
        self._show_selected_details()

    def _show_queue_context_menu(self, event: tk.Event) -> str:
        row_iid = self.queue_tree.identify_row(event.y)
        if row_iid:
            if row_iid not in self.queue_tree.selection():
                self.queue_tree.selection_set(row_iid)
            self.queue_tree.focus(row_iid)

        records = self._selected_records()
        single = records[0] if len(records) == 1 else None
        can_open = bool(
            single is not None
            and self._on_open_path is not None
            and self._resolve_record_path(single, reveal_in_folder=False)
        )
        can_reveal = bool(
            single is not None
            and self._on_open_path is not None
            and self._resolve_record_path(single, reveal_in_folder=True)
        )
        can_retry = any(
            rec.get("status") in ("Failed", "Cancelled", "Interrupted")
            and (
                (rec.get("job_kind", "download") == "download" and rec.get("url"))
                or (rec.get("job_kind") == "pack" and isinstance(rec.get("pack_request"), dict))
            )
            for rec in records
        )
        can_preview = bool(
            single is not None
            and self._on_preview_url is not None
            and single.get("job_kind", "download") == "download"
            and single.get("url")
        )
        can_send = bool(
            self._on_send_to_urls is not None
            and any(rec.get("job_kind", "download") == "download" and rec.get("url") for rec in records)
        )
        can_copy_url = any(bool(rec.get("url")) for rec in records)
        can_copy_detail = bool(single is not None and str(single.get("detail") or "").strip())
        can_move = bool(single is not None and single.get("status") == "Queued")
        can_remove = any(rec.get("status") != "Running" for rec in records)
        has_clearable = any(
            (rec := self._controller.get_queue_record(iid)) is not None
            and rec.get("status") in ("Completed", "Failed", "Cancelled", "Interrupted")
            for iid in self.queue_tree.get_children()
        )

        menu = self._queue_menu
        menu.entryconfigure("Open Output", state="normal" if can_open else "disabled")
        menu.entryconfigure("Open Containing Folder", state="normal" if can_reveal else "disabled")
        menu.entryconfigure("View Job Details...", state="normal" if single is not None else "disabled")
        menu.entryconfigure("Retry Selected", state="normal" if (can_retry and self._on_retry_selected) else "disabled")
        menu.entryconfigure("Preview Metadata...", state="normal" if can_preview else "disabled")
        menu.entryconfigure("Send URL to Input", state="normal" if can_send else "disabled")
        menu.entryconfigure("Copy URL / Source", state="normal" if can_copy_url else "disabled")
        menu.entryconfigure("Copy Detail / Error", state="normal" if can_copy_detail else "disabled")
        menu.entryconfigure("Move to Top", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move Up", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move Down", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move to Bottom", state="normal" if can_move else "disabled")
        menu.entryconfigure("Remove Selected", state="normal" if can_remove else "disabled")
        menu.entryconfigure("Clear Completed & Failed", state="normal" if has_clearable else "disabled")

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def remove_selected_item(self) -> None:
        selected = list(self.queue_tree.selection())
        if not selected:
            return
        saw_active_running = False
        removed_any = False
        latest_total: int | None = None
        for job_id in selected:
            removed, new_total, is_active_running = self._controller.remove_pending_job(job_id)
            if is_active_running:
                saw_active_running = True
                continue
            if not removed:
                continue
            removed_any = True
            if new_total is not None:
                latest_total = new_total
            if self.queue_tree.exists(job_id):
                self.queue_tree.delete(job_id)
            if job_id in self._queue_order:
                self._queue_order.remove(job_id)
        if latest_total is not None:
            self.ui_item_total = max(self.ui_item_index, latest_total)
            self.refresh_batch_label()
        if removed_any:
            self._refresh_queue_empty_and_badge()
        if saw_active_running and not removed_any:
            messagebox.showinfo(
                "Job Active",
                "The currently running download cannot be removed; use Cancel instead.",
                parent=self._window,
            )

    def move_selected_item(self, direction: int) -> None:
        selected = self.queue_tree.selection()
        if len(selected) != 1:
            return
        job_id = selected[0]
        neighbor_id = self._controller.move_pending_job(job_id, direction)
        if neighbor_id is None:
            return
        if job_id in self._queue_order and neighbor_id in self._queue_order:
            ia = self._queue_order.index(job_id)
            ib = self._queue_order.index(neighbor_id)
            self._queue_order[ia], self._queue_order[ib] = self._queue_order[ib], self._queue_order[ia]
        if self.queue_tree.exists(job_id) and self.queue_tree.exists(neighbor_id):
            idx_a = self.queue_tree.index(job_id)
            idx_b = self.queue_tree.index(neighbor_id)
            self.queue_tree.move(job_id, "", idx_b)
            self.queue_tree.move(neighbor_id, "", idx_a)

    def _sync_pending_order_in_tree(self, pending_order: Sequence[str]) -> None:
        pending_set = set(pending_order)
        # Keep non-pending (running/completed) in place; place reordered pending jobs in order
        it = iter(pending_order)
        self._queue_order = [
            next(it) if jid in pending_set else jid for jid in self._queue_order
        ]
        self._apply_queue_filter()

    def move_selected_to_edge(self, *, to_top: bool) -> None:
        selected = self.queue_tree.selection()
        if len(selected) != 1:
            return
        new_order = self._controller.move_pending_job_to_edge(selected[0], to_top=to_top)
        if new_order is not None:
            self._sync_pending_order_in_tree(new_order)

    def _on_queue_drag_start(self, event: tk.Event) -> None:
        if self.queue_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            self._drag_job_id = None
            return
        row_iid = self.queue_tree.identify_row(event.y)
        self._drag_job_id = row_iid or None

    def _on_queue_drag_drop(self, event: tk.Event) -> None:
        src = self._drag_job_id
        self._drag_job_id = None
        if not src:
            return
        target = self.queue_tree.identify_row(event.y)
        if not target or target == src:
            return
        new_order = self._controller.move_pending_job_to_target(src, target)
        if new_order is not None:
            self._sync_pending_order_in_tree(new_order)

    def clear_completed_history(self) -> None:
        for iid in self._controller.clear_completed_records(list(self._queue_order)):
            if self.queue_tree.exists(iid):
                self.queue_tree.delete(iid)
            if iid in self._queue_order:
                self._queue_order.remove(iid)
        self._refresh_queue_empty_and_badge()

    def handle_activity_event(self, item: tuple) -> bool:
        """Handle log, batch/book progress, and queue status events; return True if handled."""
        kind = item[0]
        if kind == "log":
            _, level, message = item
            prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
            self.append_log_batch([(str(level), f"{prefix}{message}")])
            if level == "INFO" and message.strip():
                self.status_var.set(message.strip().splitlines()[0][:80])
                self._update_stage_from_message(message)
            return True
        if kind == "log_batch":
            _, entries = item
            formatted: list[tuple[str, str]] = []
            last_info = ""
            for level, message in entries:
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                formatted.append((str(level), f"{prefix}{message}"))
                if level == "INFO" and str(message).strip():
                    last_info = str(message)
            self.append_log_batch(formatted)
            if last_info:
                self.status_var.set(last_info.strip().splitlines()[0][:80])
                self._update_stage_from_message(last_info)
            return True
        if kind == "batch_item":
            _, run_id, item_index, item_total, target_url = item
            if run_id == self.current_ui_run_id:
                self.ui_item_index = item_index
                self.ui_item_total = max(self.ui_item_total, item_total)
                self.ui_book_index = 0
                self.ui_book_total = 0
                self.stage_var.set("Stage: Fetching metadata")
                if target_url:
                    self.active_job_var.set(str(target_url).strip()[:60])
                self.refresh_batch_label()
            return True
        if kind == "job_status":
            _, job_id, status_text, detail = item
            self.update_queue_row(job_id, status_text, detail)
            if status_text == "Running":
                rec = self._controller.get_queue_record(job_id)
                if rec is not None:
                    label = str(rec.get("title") or rec.get("url") or "").strip()
                    if label:
                        self.active_job_var.set(label[:60])
            return True
        if kind == "job_meta":
            _, job_id, title_text, output_path = item
            self.update_queue_row_metadata(
                job_id,
                title=title_text or None,
                output_path=output_path or None,
            )
            if title_text:
                self.active_job_var.set(str(title_text).strip()[:60])
            return True
        if kind == "item_progress":
            _, run_id, book_index, book_total, book_title = item
            if run_id == self.current_ui_run_id:
                self.ui_book_index = book_index
                self.ui_book_total = book_total
                if book_title:
                    self.active_job_var.set(str(book_title).strip()[:60])
                self.refresh_batch_label()
            return True
        if kind == "progress_start":
            _, description, _total = item
            self._set_progress(0.0)
            self._last_pct = 0.0
            self._last_speed_bps = 0.0
            self._last_eta_seconds = None
            self.stage_var.set("Stage: Downloading")
            self.update_metrics_display(is_running=True, force=True)
            self.status_var.set(description[:80])
            if description.strip():
                self.active_job_var.set(description.strip()[:60])
            return True
        if kind == "progress_advance":
            _, completed, total = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            clamped = min(100.0, max(0.0, pct))
            self._last_pct = clamped
            self.progress_var.set(clamped)
            return True
        if kind == "progress_metrics":
            _, completed, total, _bytes_downloaded, speed_bps, _elapsed, eta_seconds = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            self._last_pct = min(100.0, max(0.0, pct))
            self.progress_var.set(self._last_pct)
            self._last_speed_bps = speed_bps
            self._last_eta_seconds = eta_seconds
            self.update_metrics_display(is_running=True, force=True)
            return True
        if kind == "progress_finish":
            self._last_pct = 100.0
            self._last_eta_seconds = 0.0
            self.progress_var.set(100.0)
            self.update_metrics_display(is_running=True, force=True)
            return True
        return False

    def finish_run(
        self,
        summary: DownloadSummary | None,
        exc: Exception | None,
        *,
        notify_enabled: bool,
        notifier: Callable[[str, str], None] | None = None,
    ) -> None:
        """Reset progress/metrics UI and report run completion/failure/cancellation."""
        notify_fn = notifier or send_os_notification_async
        self._set_progress(0.0)
        final_elapsed = (
            format_duration(max(0.0, time.monotonic() - self._run_start_time))
            if self._run_start_time is not None
            else None
        )
        self._run_start_time = None
        self.batch_var.set("")
        self.stage_var.set("")
        if exc is not None:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Error")
            self.active_job_var.set("Idle (last run failed)")
            self.append_log(f"[ERROR] {exc}")
            if notify_enabled:
                payload = format_completion_notification(summary, exc)
                if payload is not None:
                    notify_fn(payload[0], payload[1])
            messagebox.showerror("Download Failed", str(exc), parent=self._window)
            return

        if summary is None or summary.cancelled:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Cancelled")
            self.active_job_var.set("Idle (cancelled)")
            return

        if summary.failed > 0 and summary.completed == 0:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Failed")
            self.active_job_var.set("Idle (failed)")
        elif summary.failed > 0:
            self._set_progress(100.0)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
            self.active_job_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
        else:
            self._set_progress(100.0)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set("Completed")
            self.active_job_var.set("Completed")
            self.append_log("--- Completed ---")
        if notify_enabled:
            payload = format_completion_notification(summary, None)
            if payload is not None:
                notify_fn(payload[0], payload[1])

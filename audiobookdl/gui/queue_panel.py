"""Action bar, progress metrics, scrollable activity log, and job queue panel."""

import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from collections.abc import Callable

from audiobookdl.gui.controller import GuiController, QueuedJob
from audiobookdl.gui.formatting import format_duration, format_speed
from audiobookdl.gui.notifications import format_completion_notification, send_os_notification_async
from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
from audiobookdl.pipeline import DownloadSummary


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
        on_pack_local: Callable[[], None],
        on_open_folder: Callable[[], None],
        on_retry_failed: Callable[[], None],
        service_label_for_url: Callable[[str], str],
    ) -> None:
        super().__init__(master, text="Activity", padding=(10, 6))
        self._window = root
        self._controller = controller
        self.status_var = status_var
        self.batch_var = batch_var
        self.stage_var = tk.StringVar(value="")
        self.metrics_var = metrics_var
        self._on_start_download = on_start_download
        self._on_cancel_download = on_cancel_download
        self._on_preview_metadata = on_preview_metadata
        self._on_pack_local = on_pack_local
        self._on_open_folder = on_open_folder
        self._on_retry_failed = on_retry_failed
        self._service_label_for_url = service_label_for_url

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

        self._build_widgets()

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

        self.progressbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self.activity_notebook = ttk.Notebook(self)
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
        queue_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
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
        self.queue_down_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.queue_retry_btn = ttk.Button(
            queue_toolbar, text="Retry Failed", command=self._on_retry_failed
        )
        self.queue_retry_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.queue_clear_btn = ttk.Button(
            queue_toolbar, text="Clear Completed", command=self.clear_completed_history
        )
        self.queue_clear_btn.pack(side=tk.RIGHT)

        queue_columns = (
            TreeColumnSpec("service", "Service", width=90, stretch=False),
            TreeColumnSpec("url", "URL / Title", width=220, stretch=True),
            TreeColumnSpec("status", "Status", width=90, stretch=False),
            TreeColumnSpec("detail", "Detail", width=140, stretch=True),
        )
        scrolled_queue = ScrolledTreeview(queue_tab, queue_columns, selectmode="browse", height=3)
        scrolled_queue.grid(row=1, column=0, sticky="nsew")
        self.queue_tree = scrolled_queue.tree
        self.activity_notebook.add(queue_tab, text="Queue & History")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, text: str) -> None:
        """Append a line to the scrollable activity log, capping total line count."""
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            excess = line_count - self.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def set_running_state(self, running: bool, *, is_previewing: bool) -> None:
        if running:
            self.download_btn.configure(text="Add to Queue", state="normal")
            self.pause_btn.configure(
                text="Resume" if self._controller.is_paused() else "Pause",
                state="normal",
            )
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

    def prepare_run_ui_state(self, run_id: int, item_total: int, log_header: str) -> None:
        """Initialize progress, timer, and batch counters for a fresh queue run."""
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate", value=0)
        self.status_var.set("Starting...")
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

    def restore_saved_queue_history(self) -> None:
        """Restore unresolved queue items persisted across GUI restarts."""
        for rec in self._controller.load_saved_queue_records():
            self.queue_tree.insert(
                "",
                tk.END,
                iid=rec["job_id"],
                values=(rec["service"], rec["url"], rec["status"], rec.get("detail", "")),
            )

    def register_queue_job(self, job: QueuedJob, *, persist: bool = True) -> None:
        """Insert a queued job row into the Queue & History table and internal index."""
        service_label = (
            "Local Pack" if job.pack_request is not None else self._service_label_for_url(job.url)
        )
        self._controller.register_queue_record(job, service_label, persist=persist)
        self.queue_tree.insert(
            "",
            tk.END,
            iid=job.job_id,
            values=(service_label, job.url, "Queued", ""),
        )

    def update_queue_row(self, job_id: str, status: str, detail: str = "") -> None:
        rec = self._controller.update_queue_record(job_id, status, detail)
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=(rec["service"], rec["url"], status, detail),
            )

    def remove_selected_item(self) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        job_id = selected[0]
        removed, new_total, is_active_running = self._controller.remove_pending_job(job_id)
        if is_active_running:
            messagebox.showinfo(
                "Job Active",
                "The currently running download cannot be removed; use Cancel instead.",
                parent=self._window,
            )
            return
        if not removed:
            return
        if new_total is not None:
            self.ui_item_total = max(self.ui_item_index, new_total)
            self.refresh_batch_label()
        if self.queue_tree.exists(job_id):
            self.queue_tree.delete(job_id)

    def move_selected_item(self, direction: int) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        job_id = selected[0]
        neighbor_id = self._controller.move_pending_job(job_id, direction)
        if neighbor_id is None:
            return
        if self.queue_tree.exists(job_id) and self.queue_tree.exists(neighbor_id):
            idx_a = self.queue_tree.index(job_id)
            idx_b = self.queue_tree.index(neighbor_id)
            self.queue_tree.move(job_id, "", idx_b)
            self.queue_tree.move(neighbor_id, "", idx_a)

    def clear_completed_history(self) -> None:
        for iid in self._controller.clear_completed_records(self.queue_tree.get_children()):
            if self.queue_tree.exists(iid):
                self.queue_tree.delete(iid)

    def handle_activity_event(self, item: tuple) -> bool:
        """Handle log, batch/book progress, and queue status events; return True if handled."""
        kind = item[0]
        if kind == "log":
            _, level, message = item
            prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
            self.append_log(f"{prefix}{message}")
            if level == "INFO" and message.strip():
                self.status_var.set(message.strip().splitlines()[0][:80])
                self._update_stage_from_message(message)
            return True
        if kind == "batch_item":
            _, run_id, item_index, item_total, _url = item
            if run_id == self.current_ui_run_id:
                self.ui_item_index = item_index
                self.ui_item_total = max(self.ui_item_total, item_total)
                self.ui_book_index = 0
                self.ui_book_total = 0
                self.stage_var.set("Stage: Fetching metadata")
                self.refresh_batch_label()
            return True
        if kind == "job_status":
            _, job_id, status_text, detail = item
            self.update_queue_row(job_id, status_text, detail)
            return True
        if kind == "item_progress":
            _, run_id, book_index, book_total, _title = item
            if run_id == self.current_ui_run_id:
                self.ui_book_index = book_index
                self.ui_book_total = book_total
                self.refresh_batch_label()
            return True
        if kind == "progress_start":
            _, description, _total = item
            self.progressbar.stop()
            self.progressbar.configure(mode="determinate", value=0)
            self._last_pct = 0.0
            self._last_speed_bps = 0.0
            self._last_eta_seconds = None
            self.stage_var.set("Stage: Downloading")
            self.update_metrics_display(is_running=True, force=True)
            self.status_var.set(description[:80])
            return True
        if kind == "progress_advance":
            _, completed, total = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            clamped = min(100.0, max(0.0, pct))
            self._last_pct = clamped
            self.progressbar.configure(value=clamped)
            return True
        if kind == "progress_metrics":
            _, completed, total, _bytes_downloaded, speed_bps, _elapsed, eta_seconds = item
            pct = (completed / total * 100.0) if total > 0 else 0.0
            self._last_pct = min(100.0, max(0.0, pct))
            self._last_speed_bps = speed_bps
            self._last_eta_seconds = eta_seconds
            self.update_metrics_display(is_running=True, force=True)
            return True
        if kind == "progress_finish":
            self._last_pct = 100.0
            self._last_eta_seconds = 0.0
            self.progressbar.configure(value=100.0)
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
        self.progressbar.stop()
        final_elapsed = (
            format_duration(max(0.0, time.monotonic() - self._run_start_time))
            if self._run_start_time is not None
            else None
        )
        self._run_start_time = None
        self.batch_var.set("")
        self.stage_var.set("")
        if exc is not None:
            self.progressbar.configure(value=0)
            self.metrics_var.set("")
            self.status_var.set("Error")
            self.append_log(f"[ERROR] {exc}")
            if notify_enabled:
                payload = format_completion_notification(summary, exc)
                if payload is not None:
                    notify_fn(payload[0], payload[1])
            messagebox.showerror("Download Failed", str(exc), parent=self._window)
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
            self.append_log("--- Completed ---")
        if notify_enabled:
            payload = format_completion_notification(summary, None)
            if payload is not None:
                notify_fn(payload[0], payload[1])

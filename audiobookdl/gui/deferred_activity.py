"""Deferred activity-event handling for when `DownloadPage` does not exist yet.

`EventDispatcher` routes controller activity events to `ActivityQueuePanel`
once the Download page has been lazily built. Until then this handler keeps
the same observable behavior — buffered log lines, status/active-job vars,
progress vars, queue-record updates, and the download badge — without forcing
page creation.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
import tkinter as tk

from audiobookdl.gui.events import (
    BatchItemEvent,
    ControllerEvent,
    DownloadEstimateEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogBatchEvent,
    LogEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
)
from audiobookdl.gui.queue_panel import ActivityQueuePanel

if TYPE_CHECKING:
    from audiobookdl.gui.controller import GuiController


class DeferredActivityHandler:
    """Applies controller activity events to app-level state before the queue panel exists."""

    def __init__(
        self,
        controller: GuiController,
        *,
        status_var: tk.StringVar,
        active_job_var: tk.StringVar,
        progress_var: tk.DoubleVar,
        append_log: Callable[..., None],
        update_download_badge: Callable[[Sequence[Mapping[str, Any]]], None],
    ) -> None:
        self._controller = controller
        self._status_var = status_var
        self._active_job_var = active_job_var
        self._progress_var = progress_var
        self._append_log = append_log
        self._update_download_badge = update_download_badge
        self.pending_log_entries: deque[tuple[str | None, str]] = deque(
            maxlen=ActivityQueuePanel.MAX_LOG_LINES
        )

    def buffer_log(self, level: str | None, text: str) -> None:
        """Store a log line until the queue panel exists to render it."""
        self.pending_log_entries.append((level, text))

    def flush_pending_logs(self, panel: ActivityQueuePanel) -> None:
        """Replay buffered log lines into a freshly created queue panel."""
        if not self.pending_log_entries:
            return
        entries: list[tuple[str, str]] = []
        for level, text in self.pending_log_entries:
            if level is None:
                tag = ActivityQueuePanel.classify_log_tag("INFO", text)
                resolved = {"error": "ERROR", "warn": "WARNING", "debug": "DEBUG"}.get(tag, "INFO")
                entries.append((resolved, text))
            else:
                entries.append((level, text))
        self.pending_log_entries.clear()
        panel.append_log_batch(entries)

    def handle(self, item: ControllerEvent) -> bool:
        """Handle activity events without forcing lazy `DownloadPage` creation."""
        if isinstance(item, LogEvent):
            prefix = f"[{item.level}] " if item.level in ("DEBUG", "ERROR") else ""
            self._append_log(f"{prefix}{item.message}", level=item.level)
            if item.level == "INFO" and item.message.strip():
                self._status_var.set(item.message.strip().splitlines()[0][:80])
            return True
        if isinstance(item, LogBatchEvent):
            last_info = ""
            for level, message in item.entries:
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                self._append_log(f"{prefix}{message}", level=level)
                if level == "INFO" and message.strip():
                    last_info = message
            if last_info:
                self._status_var.set(last_info.strip().splitlines()[0][:80])
            return True
        if isinstance(item, JobStatusEvent):
            self._controller.update_queue_record(item.job_id, item.status, item.detail)
            with self._controller.queue_lock:
                records = list(self._controller.queue_records.values())
            self._update_download_badge(records)
            return True
        if isinstance(item, JobMetaEvent):
            self._controller.update_queue_metadata(
                item.job_id,
                title=item.title or None,
                output_path=item.output_path or None,
            )
            if item.title:
                self._active_job_var.set(item.title.strip()[:60])
            return True
        if isinstance(item, ProgressStartEvent):
            self._progress_var.set(0.0)
            self._status_var.set(item.description[:80])
            if item.description.strip():
                self._active_job_var.set(item.description.strip()[:60])
            return True
        if isinstance(item, ProgressAdvanceEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self._progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressMetricsEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self._progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressFinishEvent):
            self._progress_var.set(100.0)
            return True
        return isinstance(item, (BatchItemEvent, ItemProgressEvent, DownloadEstimateEvent))

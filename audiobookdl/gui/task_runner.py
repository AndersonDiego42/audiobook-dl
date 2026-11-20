"""Bounded background/database executors and activity tracking for GUI tasks."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audiobookdl.gui.event_bus import GuiInteractionHandler


class GuiTaskRunner:
    """Owns the shared GUI background executor, serialized DB executor, and activity wakeups."""

    def __init__(self) -> None:
        self._aux_interactions_lock = threading.Lock()
        self._aux_interactions: set[GuiInteractionHandler] = set()
        self._aux_cancel_events: set[threading.Event] = set()
        self._bg_lock = threading.Lock()
        self._active_bg_tasks = 0
        self._bg_done_cond = threading.Condition(self._bg_lock)
        self._bg_executor: ThreadPoolExecutor | None = None
        self._db_lock = threading.Lock()
        self._db_executor: ThreadPoolExecutor | None = None
        self._activity_callback: Callable[[], None] | None = None

    @property
    def db_executor(self) -> ThreadPoolExecutor:
        """Return the shared single-worker serialized SQLite executor."""
        with self._db_lock:
            if self._db_executor is None:
                self._db_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="abdl-db",
                )
            return self._db_executor

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Register a main-thread wakeup callback invoked when background tasks start."""
        self._activity_callback = callback

    def notify_activity(self) -> None:
        """Invoke the registered activity callback, swallowing callback errors."""
        cb = self._activity_callback
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def has_active_bg_tasks(self) -> bool:
        """Return True while bounded-pool auxiliary tasks are still running."""
        with self._bg_lock:
            return self._active_bg_tasks > 0

    def submit_bg_task(self, fn: Callable[[], None]) -> None:
        """Execute `fn` on the shared bounded GUI background executor."""
        with self._bg_lock:
            if self._bg_executor is None:
                self._bg_executor = ThreadPoolExecutor(
                    max_workers=6,
                    thread_name_prefix="abdl-gui-bg",
                )
            self._active_bg_tasks += 1
            executor = self._bg_executor

        def _wrapped() -> None:
            try:
                fn()
            finally:
                with self._bg_lock:
                    self._active_bg_tasks = max(0, self._active_bg_tasks - 1)
                    if self._active_bg_tasks == 0:
                        self._bg_done_cond.notify_all()

        try:
            executor.submit(_wrapped)
        except RuntimeError:
            with self._bg_lock:
                self._active_bg_tasks = max(0, self._active_bg_tasks - 1)
                if self._active_bg_tasks == 0:
                    self._bg_done_cond.notify_all()
            raise
        self.notify_activity()

    def track_interaction(
        self,
        interaction: GuiInteractionHandler,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Register an auxiliary interaction so shutdown can release its prompts."""
        with self._aux_interactions_lock:
            self._aux_interactions.add(interaction)
            if cancel_event is not None:
                self._aux_cancel_events.add(cancel_event)

    def untrack_interaction(
        self,
        interaction: GuiInteractionHandler,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Remove a previously tracked auxiliary interaction/cancel pair."""
        with self._aux_interactions_lock:
            self._aux_interactions.discard(interaction)
            if cancel_event is not None:
                self._aux_cancel_events.discard(cancel_event)

    def release_tracked_interactions(self) -> None:
        """Signal and unblock any pending auxiliary interaction prompts and cancel events."""
        with self._aux_interactions_lock:
            handlers = list(self._aux_interactions)
            cancel_events = list(self._aux_cancel_events)
        for ev in cancel_events:
            ev.set()
        for handler in handlers:
            handler.release_all()

    def shutdown(self) -> None:
        """Stop accepting background work, wait briefly for in-flight tasks, and shut executors down."""
        with self._bg_lock:
            bg_exec = self._bg_executor
            self._bg_executor = None
            if self._active_bg_tasks > 0:
                self._bg_done_cond.wait(timeout=0.35)
        self._activity_callback = None
        if bg_exec is not None:
            bg_exec.shutdown(wait=False, cancel_futures=True)
        with self._db_lock:
            db_exec = self._db_executor
            self._db_executor = None
        if db_exec is not None:
            db_exec.shutdown(wait=False, cancel_futures=True)

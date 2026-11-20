"""Headless event queue, log sink, and worker-thread prompt bridge for the GUI."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import queue
import threading
from typing import Any

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.events import (
    ControllerEvent,
    DownloadEstimateEvent,
    ItemProgressEvent,
    JobMetaEvent,
    LogBatchEvent,
    LogEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
    PromptChoiceEvent,
    PromptConfirmEvent,
    PromptInputEvent,
)


class ControllerEventQueue:
    """Thread-safe non-blocking event queue with bounded backpressure coalescing."""

    DEFAULT_MAX_SIZE = 4096

    def __init__(self, maxsize: int = DEFAULT_MAX_SIZE) -> None:
        self._maxsize = max(64, int(maxsize))
        self._lock = threading.Lock()
        self._items: deque[ControllerEvent] = deque()
        self._overflow_progress: dict[tuple[type[Any], str | None], ControllerEvent] = {}

    def put(self, item: ControllerEvent) -> None:
        """Enqueue `item` without blocking, coalescing excess progress/log floods when saturated."""
        with self._lock:
            if len(self._items) < self._maxsize:
                self._items.append(item)
                return
            if isinstance(item, (ProgressAdvanceEvent, ProgressMetricsEvent)):
                self._overflow_progress[(type(item), item.job_id)] = item
                return
            if isinstance(item, LogEvent):
                if item.level == "DEBUG":
                    return
                if not self._evict_oldest_matching(
                    lambda ev: isinstance(ev, LogEvent) and ev.level == "DEBUG"
                ):
                    if not self._evict_oldest_matching(lambda ev: isinstance(ev, LogEvent)):
                        return
                self._items.append(item)
                return
            # Critical lifecycle, status, metadata, and modal prompt events are never dropped.
            if not self._evict_oldest_matching(
                lambda ev: isinstance(ev, (ProgressAdvanceEvent, ProgressMetricsEvent))
            ):
                self._evict_oldest_matching(lambda ev: isinstance(ev, LogEvent))
            self._items.append(item)

    def put_nowait(self, item: ControllerEvent) -> None:
        self.put(item)

    def _evict_oldest_matching(self, predicate: Callable[[ControllerEvent], bool]) -> bool:
        for idx, existing in enumerate(self._items):
            if predicate(existing):
                del self._items[idx]
                return True
        return False

    def get_nowait(self) -> ControllerEvent:
        with self._lock:
            if self._items:
                return self._items.popleft()
            if self._overflow_progress:
                first_key = next(iter(self._overflow_progress))
                return self._overflow_progress.pop(first_key)
            raise queue.Empty

    def empty(self) -> bool:
        with self._lock:
            return not self._items and not self._overflow_progress

    def qsize(self) -> int:
        with self._lock:
            return len(self._items) + len(self._overflow_progress)


class GuiLogSink:
    """Forwards core log and progress events onto the Tkinter event queue."""

    def __init__(self, event_queue: ControllerEventQueue) -> None:
        self._queue = event_queue
        self._run_lock = threading.Lock()
        self._active_run_id = 0
        self._job_local = threading.local()

    def _job_state(self) -> Any:
        state = getattr(self._job_local, "state", None)
        if state is None:
            state = {"job_id": None, "series_title": "", "last_title": ""}
            self._job_local.state = state
        return state

    @property
    def _active_job_id(self) -> str | None:
        return self._job_state()["job_id"]

    def set_active_run_id(self, run_id: int) -> None:
        with self._run_lock:
            self._active_run_id = run_id

    def get_active_run_id(self) -> int:
        with self._run_lock:
            return self._active_run_id

    def set_active_job_id(self, job_id: str | None) -> None:
        """Bind `job_id` for the current worker thread so `item_progress` resolves queue titles."""
        state = self._job_state()
        state["job_id"] = job_id
        state["series_title"] = ""
        state["last_title"] = ""

    def on_log(self, level: str, message: str) -> None:
        self._queue.put(LogEvent(level=level, message=message, job_id=self._active_job_id))

    def on_progress_start(self, description: str, total: float) -> None:
        self._queue.put(
            ProgressStartEvent(
                description=description,
                total=total,
                job_id=self._active_job_id,
            )
        )

    def on_progress_advance(self, completed: float, total: float) -> None:
        self._queue.put(
            ProgressAdvanceEvent(
                completed=completed,
                total=total,
                job_id=self._active_job_id,
            )
        )

    def on_progress_metrics(
        self,
        completed: float,
        total: float,
        bytes_downloaded: int,
        speed_bps: float,
        elapsed_seconds: float,
        eta_seconds: float | None,
    ) -> None:
        self._queue.put(
            ProgressMetricsEvent(
                completed=completed,
                total=total,
                bytes_downloaded=bytes_downloaded,
                speed_bps=speed_bps,
                elapsed_seconds=elapsed_seconds,
                eta_seconds=eta_seconds,
                job_id=self._active_job_id,
            )
        )

    def on_download_estimate(
        self,
        estimated_bytes: int | None,
        is_exact: bool,
        known_files: int,
        total_files: int,
    ) -> None:
        self._queue.put(
            DownloadEstimateEvent(
                estimated_bytes=estimated_bytes,
                is_exact=is_exact,
                known_files=known_files,
                total_files=total_files,
                job_id=self._active_job_id,
            )
        )

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        self._queue.put(
            ItemProgressEvent(
                run_id=self.get_active_run_id(),
                current=current,
                total=total,
                title=title,
            )
        )
        cleaned = (title or "").strip()
        state = self._job_state()
        active_job_id = state["job_id"]
        if not active_job_id or not cleaned:
            return
        series_title = state["series_title"]
        if total > 1 and current == 0:
            state["series_title"] = cleaned
            display_title = cleaned
        elif total > 1 and current > 0:
            if series_title and series_title != cleaned:
                display_title = f"{series_title} ({current}/{total})"
            else:
                display_title = f"{cleaned} ({current}/{total})"
        else:
            display_title = cleaned
        if display_title != state["last_title"]:
            state["last_title"] = display_title
            self._queue.put(JobMetaEvent(job_id=active_job_id, title=display_title))

    def on_progress_finish(self) -> None:
        self._queue.put(ProgressFinishEvent(job_id=self._active_job_id))


class GuiInteractionHandler:
    """Bridges worker-thread prompts to Tkinter modal dialogs on the main thread."""

    def __init__(
        self,
        event_queue: ControllerEventQueue,
        cancel_event: threading.Event,
    ) -> None:
        self._queue = event_queue
        self._cancel_event = cancel_event
        self._pending_events: list[threading.Event] = []
        self._released = False
        self._lock = threading.Lock()

    def _await_prompt(self, build_event: Callable[[threading.Event], ControllerEvent]) -> bool:
        done = threading.Event()
        with self._lock:
            if self._released or self._cancel_event.is_set():
                return False
            self._pending_events.append(done)
            ev = build_event(done)
        self._queue.put(ev)
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        return not self._cancel_event.is_set() and not self._released

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        holder: dict[str, Any] = {"value": None}
        if not self._await_prompt(
            lambda done: PromptInputEvent(prompt=prompt, hidden=hidden, holder=holder, done=done)
        ):
            raise DownloadCancelled
        if holder["value"] is None:
            raise DownloadCancelled
        return str(holder["value"])

    def confirm(self, message: str) -> bool:
        holder: dict[str, Any] = {"value": False}
        if not self._await_prompt(
            lambda done: PromptConfirmEvent(
                message=message, holder=holder, done=done
            )
        ):
            return False
        return bool(holder["value"])

    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: int | None = None,
    ) -> int | None:
        if not choices:
            return None
        holder: dict[str, Any] = {"value": None}
        if not self._await_prompt(
            lambda done: PromptChoiceEvent(
                title=title,
                choices=tuple(choices),
                default_index=default,
                holder=holder,
                done=done,
            )
        ):
            return None
        val = holder["value"]
        return val if isinstance(val, int) and not isinstance(val, bool) and 0 <= val < len(choices) else None

    def release_all(self) -> None:
        """Unblock any waiting prompt threads when the window closes or cancels."""
        with self._lock:
            self._released = True
            for ev in self._pending_events:
                ev.set()


class _PrefixedInteractionHandler:
    """Wrap a `GuiInteractionHandler` so interactive prompts identify `(service_label, profile)`."""

    def __init__(self, inner: GuiInteractionHandler, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix

    def _prefixed(self, text: str) -> str:
        return f"[{self._prefix}] {text}" if self._prefix else text

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        return self._inner.ask_input(self._prefixed(prompt), hidden=hidden)

    def confirm(self, message: str) -> bool:
        return self._inner.confirm(self._prefixed(message))

    def choose_index(
        self, title: str, choices: Sequence[str], default: int | None = None
    ) -> int | None:
        return self._inner.choose_index(self._prefixed(title), choices, default=default)

    def release_all(self) -> None:
        self._inner.release_all()


class GuiEventBus:
    """Owns the controller event queue/sink and drains/coalesces events for the UI thread."""

    def __init__(self) -> None:
        self.queue = ControllerEventQueue()
        self.sink = GuiLogSink(self.queue)

    def drain_events(
        self,
        handler: Callable[[ControllerEvent], None],
        max_batch: int = ControllerEventQueue.DEFAULT_MAX_SIZE,
    ) -> int:
        """Drain up to `max_batch` events, coalescing consecutive logs and progress updates."""
        pending_progress: dict[str | None, ProgressAdvanceEvent] = {}
        pending_metrics: dict[str | None, ProgressMetricsEvent] = {}
        pending_logs: list[tuple[str, str, str | None]] = []
        processed = 0

        def _flush_logs() -> None:
            if not pending_logs:
                return
            if len(pending_logs) == 1:
                lvl, msg, jid = pending_logs[0]
                pending_logs.clear()
                handler(LogEvent(level=lvl, message=msg, job_id=jid))
                return
            batch = tuple((lvl, msg) for lvl, msg, _ in pending_logs)
            pending_logs.clear()
            handler(LogBatchEvent(entries=batch))

        def _flush_progress() -> None:
            if pending_progress:
                for adv_ev in list(pending_progress.values()):
                    handler(adv_ev)
                pending_progress.clear()
            if pending_metrics:
                for met_ev in list(pending_metrics.values()):
                    handler(met_ev)
                pending_metrics.clear()

        while processed < max_batch:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if isinstance(item, LogEvent):
                _flush_progress()
                pending_logs.append((item.level, item.message, item.job_id))
                continue
            if isinstance(item, ProgressAdvanceEvent):
                _flush_logs()
                pending_progress[item.job_id] = item
                continue
            if isinstance(item, ProgressMetricsEvent):
                _flush_logs()
                pending_metrics[item.job_id] = item
                continue
            _flush_logs()
            _flush_progress()
            handler(item)
        _flush_logs()
        _flush_progress()
        return processed

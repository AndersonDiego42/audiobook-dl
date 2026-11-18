from collections import deque
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import re
from typing import Any, Protocol
from collections.abc import Generator, Sequence
from audiobookdl.utils import read_asset_file
import sys
import threading
import time
import traceback


class LogSink(Protocol):
    """Receives log and progress events for the desktop GUI or headless consumers."""

    def on_log(self, level: str, message: str) -> None: ...
    def on_progress_start(self, description: str, total: float) -> None: ...
    def on_progress_advance(self, completed: float, total: float) -> None: ...
    def on_progress_finish(self) -> None: ...


class InteractionHandler(Protocol):
    """Handles user prompts (credentials, overwrite confirmation, choices) for the GUI."""

    def ask_input(self, prompt: str, hidden: bool = False) -> str: ...
    def confirm(self, message: str) -> bool: ...
    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: int | None = None,
    ) -> int | None: ...


class NonInteractiveInteractionHandler:
    """Fallback interaction handler when no GUI prompt bridge is attached."""

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        raise RuntimeError(f"Interactive prompt required ({prompt}), but no UI interaction handler is active")

    def confirm(self, message: str) -> bool:
        return False

    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: int | None = None,
    ) -> int | None:
        return default if default is not None and 0 <= default < len(choices) else None


@dataclass
class JobContext:
    """Per-job logging, UI sink, interaction, cancellation, and pause context."""

    debug: bool = False
    quiet: bool = False
    ffmpeg_output: bool = False
    ffmpeg_dir: str | None = None
    sink: LogSink | None = None
    interaction: InteractionHandler | None = None
    cancel_event: threading.Event | None = None
    pause_event: threading.Event | None = None


_job_context: contextvars.ContextVar[JobContext | None] = contextvars.ContextVar(
    "audiobookdl_job_context",
    default=None,
)


def get_job_context() -> JobContext | None:
    """Return the active `JobContext` for the current task/thread, if any."""
    return _job_context.get()


@contextmanager
def job_context(ctx: JobContext) -> Generator[JobContext]:
    """Context manager binding `ctx` for the duration of a download job."""
    token = _job_context.set(ctx)
    try:
        yield ctx
    finally:
        _job_context.reset(token)


def is_debug_mode() -> bool:
    """Return whether debug logging is enabled in the active `JobContext`."""
    ctx = _job_context.get()
    return ctx.debug if ctx is not None else False


def wait_if_paused(
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> None:
    """Block cooperatively while `pause_event` is cleared, raising `DownloadCancelled` on cancel."""
    ctx = _job_context.get()
    ce = cancel_event if cancel_event is not None else (ctx.cancel_event if ctx is not None else None)
    if ce is not None and ce.is_set():
        from audiobookdl.exceptions import DownloadCancelled

        raise DownloadCancelled
    pe = pause_event if pause_event is not None else (ctx.pause_event if ctx is not None else None)
    if pe is None or pe.is_set():
        return
    while not pe.wait(0.2):
        if ce is not None and ce.is_set():
            from audiobookdl.exceptions import DownloadCancelled

            raise DownloadCancelled
    if ce is not None and ce.is_set():
        from audiobookdl.exceptions import DownloadCancelled

        raise DownloadCancelled


def is_quiet_mode() -> bool:
    """Return whether quiet mode is enabled in the active `JobContext`."""
    ctx = _job_context.get()
    return ctx.quiet if ctx is not None else False


def is_ffmpeg_output() -> bool:
    """Return whether verbose FFmpeg output is enabled in the active `JobContext`."""
    ctx = _job_context.get()
    return ctx.ffmpeg_output if ctx is not None else False


def get_ui_sink() -> LogSink | None:
    """Return the active `LogSink` for the current `JobContext`, if any."""
    ctx = _job_context.get()
    return ctx.sink if ctx is not None else None


_MARKUP_STRIP_RE = re.compile(r"\\(\[)|\[(?:/[^\n\[\]]*|[a-z#@][^\n\[\]]*)\]")
_MARKUP_ESCAPE_RE = re.compile(r"(\\*)(\[)")


def escape_markup(msg: str) -> str:
    """Escape square-bracket markup tokens in `msg` before logging."""
    if "[" not in msg:
        return msg
    return _MARKUP_ESCAPE_RE.sub(lambda m: f"{m.group(1) * 2}\\[", msg)


def strip_markup(msg: str) -> str:
    """Strip inline style markup tags into plain text for GUI and plain logs."""
    if "[" not in msg:
        return msg
    return _MARKUP_STRIP_RE.sub(lambda m: m.group(1) or "", msg)


_PROGRESS_EMIT_INTERVAL = 0.05  # 20 Hz max UI progress emission rate
_PROGRESS_EMIT_MIN_DELTA = 0.005  # 0.5% progress delta threshold
_SPEED_WINDOW_SECONDS = 3.0
_METRICS_EMIT_INTERVAL = 0.25


def emit_item_progress(current: int, total: int, title: str = "") -> None:
    """Notify active UI sink of item/book position within a URL or series."""
    sink = get_ui_sink()
    if sink is None:
        return
    handler = getattr(sink, "on_item_progress", None)
    if callable(handler):
        handler(current, total, strip_markup(title))


class _SinkTask:
    """Task progress state tracking completed and remaining units."""

    def __init__(self, total: float) -> None:
        now = time.monotonic()
        self.total = total
        self.completed = 0.0
        self.last_emitted_completed = 0.0
        self.last_emitted_time = now
        self.start_time = now
        self.bytes_downloaded = 0
        self.byte_samples: deque[tuple[float, int]] = deque()
        self.last_metrics_time = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.completed)


class _SinkProgressAdapter:
    """Thread-safe Progress context manager forwarding updates to a `LogSink`."""

    def __init__(self, sink: LogSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self.tasks: list[_SinkTask] = []

    def __enter__(self) -> "_SinkProgressAdapter":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._sink.on_progress_finish()

    def add_task(self, description: str, total: float = 100.0) -> int:
        with self._lock:
            task = _SinkTask(float(total))
            self.tasks.append(task)
            task_id = len(self.tasks) - 1
        self._sink.on_progress_start(strip_markup(description), float(total))
        return task_id

    def _build_metrics_locked(
        self,
        task: _SinkTask,
        now: float,
    ) -> tuple[float, float, int, float, float, float | None]:
        cutoff = now - _SPEED_WINDOW_SECONDS
        while task.byte_samples and task.byte_samples[0][0] < cutoff:
            task.byte_samples.popleft()
        elapsed = max(0.0, now - task.start_time)
        window_bytes = sum(sample_bytes for _, sample_bytes in task.byte_samples)
        if task.byte_samples:
            span = max(0.25, now - task.byte_samples[0][0])
            effective_span = max(span, min(elapsed, 1.0)) if elapsed > 0 else span
            speed_bps = window_bytes / effective_span
        else:
            speed_bps = 0.0
        eta_seconds: float | None = None
        if task.total > 0 and task.completed > 0:
            if task.completed >= task.total:
                eta_seconds = 0.0
            elif elapsed >= 0.5 and (task.completed / task.total) >= 0.002:
                eta_seconds = elapsed * ((task.total - task.completed) / task.completed)
        return (
            task.completed,
            task.total,
            task.bytes_downloaded,
            speed_bps,
            elapsed,
            eta_seconds,
        )

    def _emit_metrics(
        self,
        metrics: tuple[float, float, int, float, float, float | None] | None,
    ) -> None:
        if metrics is None:
            return
        handler = getattr(self._sink, "on_progress_metrics", None)
        if callable(handler):
            handler(*metrics)

    def record_bytes(self, task_id: int, num_bytes: int) -> None:
        """Record downloaded bytes for rolling speed and ETA calculation."""
        if num_bytes <= 0:
            return
        metrics = None
        with self._lock:
            if not (0 <= task_id < len(self.tasks)):
                return
            task = self.tasks[task_id]
            now = time.monotonic()
            task.bytes_downloaded += num_bytes
            task.byte_samples.append((now, num_bytes))
            cutoff = now - _SPEED_WINDOW_SECONDS
            while task.byte_samples and task.byte_samples[0][0] < cutoff:
                task.byte_samples.popleft()
            if (now - task.last_metrics_time) >= _METRICS_EMIT_INTERVAL:
                task.last_metrics_time = now
                metrics = self._build_metrics_locked(task, now)
        self._emit_metrics(metrics)

    def advance(self, task_id: int, advance: float = 1.0) -> None:
        emit_values: tuple[float, float] | None = None
        metrics = None
        with self._lock:
            task = self.tasks[task_id]
            task.completed = max(0.0, min(task.total, task.completed + float(advance)))
            completed = task.completed
            total = task.total
            if completed != task.last_emitted_completed:
                delta_ratio = (
                    abs(completed - task.last_emitted_completed) / total
                    if total > 0
                    else 0.0
                )
                reached_completion = total > 0 and completed >= total
                now = time.monotonic()
                if (
                    reached_completion
                    or delta_ratio >= _PROGRESS_EMIT_MIN_DELTA
                    or (now - task.last_emitted_time) >= _PROGRESS_EMIT_INTERVAL
                ):
                    task.last_emitted_completed = completed
                    task.last_emitted_time = now
                    task.last_metrics_time = now
                    emit_values = (completed, total)
                    metrics = self._build_metrics_locked(task, now)
        if emit_values is not None:
            self._sink.on_progress_advance(*emit_values)
            self._emit_metrics(metrics)


def _emit_to_sink(level: str, msg: str) -> bool:
    sink = get_ui_sink()
    if sink is None:
        return False
    sink.on_log(level, strip_markup(msg))
    return True


def mask_secret(value: str | None) -> str:
    """Return a redacted representation of a secret/token for debug logging."""
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return f"<redacted len={len(value)}>"
    return f"<redacted len={len(value)} tail=...{value[-4:]}>"


def debug(msg: str) -> None:
    """Emit debug message to the active UI sink or stderr."""
    if is_debug_mode():
        if _emit_to_sink("DEBUG", msg):
            return
        if sys.stderr is not None:
            print(f"DEBUG {strip_markup(msg)}", file=sys.stderr)


def log(msg: str):
    """Emit informational message to the active UI sink or stderr."""
    if not is_quiet_mode():
        if _emit_to_sink("INFO", msg):
            return
        if sys.stderr is not None:
            prefix = "INFO " if is_debug_mode() else ""
            print(f"{prefix}{strip_markup(msg)}", file=sys.stderr)


def book_update(msg: str):
    """Display indented msg in log"""
    if is_debug_mode():
        log(msg)
    else:
        log(f"  {msg}")


def error(msg: str):
    if _emit_to_sink("ERROR", msg):
        return
    if sys.stderr is not None:
        print(strip_markup(msg), file=sys.stderr)


def print_error_file(name: str, **kwargs):
    """Print predefined error message"""
    msg = read_asset_file(f"assets/errors/{name}.txt").format(**kwargs)
    msg = msg.strip()
    error(msg)


@dataclass
class _HeadlessTask:
    total: float
    completed: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.completed)


class _HeadlessProgressAdapter:
    """In-memory progress adapter used when no UI `LogSink` is bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tasks: list[_HeadlessTask] = []
        self._bytes_total: dict[int, int] = {}
        self._samples: dict[int, deque[tuple[float, int]]] = {}

    def __enter__(self) -> "_HeadlessProgressAdapter":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def add_task(self, _description: str, total: float | None = 100.0, **_kwargs: Any) -> int:
        with self._lock:
            task_id = len(self.tasks)
            self.tasks.append(_HeadlessTask(total=float(total or 0.0)))
            self._bytes_total[task_id] = 0
            self._samples[task_id] = deque()
            return task_id

    def advance(self, task_id: int, advance: float = 1.0) -> None:
        with self._lock:
            if 0 <= task_id < len(self.tasks):
                task = self.tasks[task_id]
                task.completed = max(0.0, min(task.total, task.completed + float(advance)))

    def record_bytes(self, task_id: int, num_bytes: int) -> None:
        if num_bytes <= 0:
            return
        now = time.monotonic()
        cutoff = now - _SPEED_WINDOW_SECONDS
        with self._lock:
            self._bytes_total[task_id] = self._bytes_total.get(task_id, 0) + num_bytes
            bucket = self._samples.setdefault(task_id, deque())
            bucket.append((now, num_bytes))
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()

    def get_byte_metrics(self, task_id: int) -> tuple[int, float] | None:
        now = time.monotonic()
        cutoff = now - _SPEED_WINDOW_SECONDS
        with self._lock:
            total = self._bytes_total.get(task_id, 0)
            if total <= 0:
                return None
            bucket = self._samples.get(task_id)
            if not bucket:
                return total, 0.0
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()
            window_bytes = sum(chunk for _, chunk in bucket)
            span = max(0.25, now - bucket[0][0]) if bucket else 0.0
            speed_bps = (window_bytes / span) if span > 0 else 0.0
            return total, speed_bps


def progress(_progress_format: Sequence[Any] = ()) -> Any:
    sink = get_ui_sink()
    if sink is not None:
        return _SinkProgressAdapter(sink)
    return _HeadlessProgressAdapter()


def print_traceback() -> None:
    """Print traceback"""
    sink = get_ui_sink()
    if sink is not None:
        sink.on_log("ERROR", traceback.format_exc().strip())
        return
    traceback.print_exc()

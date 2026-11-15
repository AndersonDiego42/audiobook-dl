from collections import deque
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from rich.text import Text
from rich.style import Style
from rich.markup import render
from rich.console import Console
from rich.progress import Progress, ProgressColumn
from rich.prompt import Confirm, Prompt
from typing import Any, Deque, Iterator, List, Optional, Protocol, Sequence, Tuple, Union
from audiobookdl.utils import read_asset_file
import io
import sys
import threading
import time
import traceback

debug_mode = False
quiet_mode = False
ffmpeg_output = False
console = Console(stderr=True) if sys.stderr is not None else Console(file=io.StringIO())
DEBUG_PREFIX = render("[yellow bold]DEBUG[/]")
INFO_PREFIX = render("[cyan bold] INFO[/]")


class LogSink(Protocol):
    """Receives log and progress events when running outside a terminal (e.g. GUI)."""

    def on_log(self, level: str, message: str) -> None: ...
    def on_progress_start(self, description: str, total: float) -> None: ...
    def on_progress_advance(self, completed: float, total: float) -> None: ...
    def on_progress_finish(self) -> None: ...


class InteractionHandler(Protocol):
    """Handles user prompts (credentials, overwrite confirmation) for CLI or GUI."""

    def ask_input(self, prompt: str, hidden: bool = False) -> str: ...
    def confirm(self, message: str) -> bool: ...
    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: Optional[int] = None,
    ) -> Optional[int]: ...


class CliInteractionHandler:
    """Default terminal prompt handler using Rich."""

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        return Prompt.ask(prompt, password=hidden)

    def confirm(self, message: str) -> bool:
        return Confirm.ask(message)

    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: Optional[int] = None,
    ) -> Optional[int]:
        if not choices:
            return None
        console.print(f"\n[bold]{title}[/]")
        console.print("  [0] Skip")
        for idx, label in enumerate(choices, start=1):
            console.print(f"  [{idx}] {label}")
        default_str = str(default + 1) if default is not None and 0 <= default < len(choices) else "0"
        while True:
            raw = Prompt.ask(f"Select [0-{len(choices)}]", default=default_str).strip()
            if raw == "0" or raw == "":
                return None
            if raw.isdigit():
                picked = int(raw) - 1
                if 0 <= picked < len(choices):
                    return picked
            console.print(f"[red]Please enter a number between 0 and {len(choices)}.[/]")


_ui_sink: Optional[LogSink] = None


@dataclass
class JobContext:
    """Per-job logging, UI sink, interaction, and cancellation context."""

    debug: bool = False
    quiet: bool = False
    ffmpeg_output: bool = False
    ffmpeg_dir: Optional[str] = None
    sink: Optional[LogSink] = None
    interaction: Optional[InteractionHandler] = None
    cancel_event: Optional[threading.Event] = None


_job_context: contextvars.ContextVar[Optional[JobContext]] = contextvars.ContextVar(
    "audiobookdl_job_context",
    default=None,
)


def get_job_context() -> Optional[JobContext]:
    """Return the active `JobContext` for the current task/thread, if any."""
    return _job_context.get()


def set_job_context(
    ctx: Optional[JobContext],
) -> contextvars.Token[Optional[JobContext]]:
    """Bind `ctx` to the current task/thread and return the reset token."""
    return _job_context.set(ctx)


def reset_job_context(token: contextvars.Token[Optional[JobContext]]) -> None:
    """Restore the previous `JobContext` using `token`."""
    _job_context.reset(token)


@contextmanager
def job_context(ctx: JobContext) -> Iterator[JobContext]:
    """Context manager binding `ctx` for the duration of a download job."""
    token = _job_context.set(ctx)
    try:
        yield ctx
    finally:
        _job_context.reset(token)


def is_debug_mode() -> bool:
    """Return whether debug logging is enabled in the active job or globally."""
    ctx = _job_context.get()
    return ctx.debug if ctx is not None else debug_mode


def is_quiet_mode() -> bool:
    """Return whether quiet mode is enabled in the active job or globally."""
    ctx = _job_context.get()
    return ctx.quiet if ctx is not None else quiet_mode


def is_ffmpeg_output() -> bool:
    """Return whether verbose FFmpeg output is enabled in the active job or globally."""
    ctx = _job_context.get()
    return ctx.ffmpeg_output if ctx is not None else ffmpeg_output


def get_ui_sink() -> Optional[LogSink]:
    """Return the active `LogSink` for the current job, or the global sink."""
    ctx = _job_context.get()
    if ctx is not None and ctx.sink is not None:
        return ctx.sink
    return _ui_sink


def set_ui_sink(sink: Optional[LogSink]) -> None:
    """Register or clear the global fallback UI sink for logs and progress."""
    global _ui_sink
    _ui_sink = sink


def strip_markup(msg: str) -> str:
    """Convert Rich markup text into plain text for non-terminal UIs."""
    try:
        return Text.from_markup(msg).plain
    except Exception:
        return msg


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
    """Minimal task representation mirroring Rich Task's `remaining` attribute."""

    def __init__(self, total: float) -> None:
        now = time.monotonic()
        self.total = total
        self.completed = 0.0
        self.last_emitted_completed = 0.0
        self.last_emitted_time = now
        self.start_time = now
        self.bytes_downloaded = 0
        self.byte_samples: Deque[Tuple[float, int]] = deque()
        self.last_metrics_time = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.completed)


class _SinkProgressAdapter:
    """Thread-safe Progress context manager forwarding updates to a `LogSink`."""

    def __init__(self, sink: LogSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self.tasks: List[_SinkTask] = []

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
    ) -> Tuple[float, float, int, float, float, Optional[float]]:
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
        eta_seconds: Optional[float] = None
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
        metrics: Optional[Tuple[float, float, int, float, float, Optional[float]]],
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
            task.bytes_downloaded += int(num_bytes)
            task.byte_samples.append((now, int(num_bytes)))
            cutoff = now - _SPEED_WINDOW_SECONDS
            while task.byte_samples and task.byte_samples[0][0] < cutoff:
                task.byte_samples.popleft()
            if (now - task.last_metrics_time) >= _METRICS_EMIT_INTERVAL:
                task.last_metrics_time = now
                metrics = self._build_metrics_locked(task, now)
        self._emit_metrics(metrics)

    def advance(self, task_id: int, advance: float = 1.0) -> None:
        emit_values: Optional[Tuple[float, float]] = None
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


def mask_secret(value: Optional[str]) -> str:
    """Return a redacted representation of a secret/token for debug logging."""
    if not value:
        return "<empty>"
    text = str(value)
    if len(text) <= 8:
        return f"<redacted len={len(text)}>"
    return f"<redacted len={len(text)} tail=...{text[-4:]}>"


def debug(msg: str, remove_styling=False):
    """Print debug msg"""
    if is_debug_mode():
        if _emit_to_sink("DEBUG", msg):
            return
        if remove_styling:
            rendered_msg = render(msg, style=Style(bold=False, color="white"))
            console.print(DEBUG_PREFIX, rendered_msg)
        else:
            console.print(DEBUG_PREFIX, msg)


def log(msg: str):
    """Display msg in log"""
    if not is_quiet_mode():
        if _emit_to_sink("INFO", msg):
            return
        if is_debug_mode():
            console.print(INFO_PREFIX, msg)
        else:
            console.print(msg)


def book_update(msg: str):
    """Display indented msg in log"""
    if is_debug_mode():
        log(msg)
    else:
        log(f"  {msg}")


def error(msg: str):
    if _emit_to_sink("ERROR", msg):
        return
    console.print(msg)


def print_error_file(name: str, **kwargs):
    """Print predefined error message"""
    msg = read_asset_file(f"assets/errors/{name}.txt").format(**kwargs)
    msg = msg.strip()
    error(msg)


def print_asset_file(path: str):
    """Read asset file and print it"""
    content = read_asset_file(path)
    if _emit_to_sink("INFO", content):
        return
    console.print(content)


def simple_help() -> None:
    """Print basic help information"""
    print_asset_file("assets/simple_help.txt")


def progress(progress_format: List[Union[str, ProgressColumn]]) -> Any:
    sink = get_ui_sink()
    if sink is not None:
        return _SinkProgressAdapter(sink)
    return Progress(*progress_format, console=console)


def print_traceback() -> None:
    """Print traceback"""
    sink = get_ui_sink()
    if sink is not None:
        sink.on_log("ERROR", traceback.format_exc().strip())
        return
    console.print()
    console.print("[underline white bold]Traceback[/]")
    traceback.print_exc()

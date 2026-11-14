from rich.text import Text
from rich.style import Style
from rich.markup import render
from rich.console import Console
from rich.progress import Progress, ProgressColumn
from rich.prompt import Confirm, Prompt
from typing import Any, List, Optional, Protocol, Tuple, Union
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


class CliInteractionHandler:
    """Default terminal prompt handler using Rich."""

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        return Prompt.ask(prompt, password=hidden)

    def confirm(self, message: str) -> bool:
        return Confirm.ask(message)


_ui_sink: Optional[LogSink] = None


def set_ui_sink(sink: Optional[LogSink]) -> None:
    """Register or clear the active UI sink for logs and progress."""
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


class _SinkTask:
    """Minimal task representation mirroring Rich Task's `remaining` attribute."""

    def __init__(self, total: float) -> None:
        self.total = total
        self.completed = 0.0
        self.last_emitted_completed = 0.0
        self.last_emitted_time = time.monotonic()

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

    def advance(self, task_id: int, advance: float = 1.0) -> None:
        emit_values: Optional[Tuple[float, float]] = None
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
                    emit_values = (completed, total)
        if emit_values is not None:
            self._sink.on_progress_advance(*emit_values)


def _emit_to_sink(level: str, msg: str) -> bool:
    if _ui_sink is None:
        return False
    _ui_sink.on_log(level, strip_markup(msg))
    return True


def debug(msg: str, remove_styling=False):
    """Print debug msg"""
    if debug_mode:
        if _emit_to_sink("DEBUG", msg):
            return
        if remove_styling:
            rendered_msg = render(msg, style=Style(bold=False, color="white"))
            console.print(DEBUG_PREFIX, rendered_msg)
        else:
            console.print(DEBUG_PREFIX, msg)


def log(msg: str):
    """Display msg in log"""
    if not quiet_mode:
        if _emit_to_sink("INFO", msg):
            return
        if debug_mode:
            console.print(INFO_PREFIX, msg)
        else:
            console.print(msg)


def book_update(msg: str):
    """Display indented msg in log"""
    if debug_mode:
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
    if _ui_sink is not None:
        return _SinkProgressAdapter(_ui_sink)
    return Progress(*progress_format, console=console)


def print_traceback() -> None:
    """Print traceback"""
    if _ui_sink is not None:
        _ui_sink.on_log("ERROR", traceback.format_exc().strip())
        return
    console.print()
    console.print("[underline white bold]Traceback[/]")
    traceback.print_exc()

from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextvars
import subprocess
import tempfile
import threading
from typing import TypeVar, cast
from collections.abc import Callable, Iterator, Sequence

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.utils import subprocess_kwargs


T_in = TypeVar("T_in")
T_out = TypeVar("T_out")

_ffmpeg_progress_var: contextvars.ContextVar[Callable[[float], None] | None] = contextvars.ContextVar(
    "ffmpeg_progress_callback",
    default=None,
)


@contextmanager
def ffmpeg_progress_callback(callback: Callable[[float], None] | None) -> Iterator[None]:
    """Attach a per-context progress callback (receiving elapsed seconds) to FFmpeg runs."""
    token = _ffmpeg_progress_var.set(callback)
    try:
        yield
    finally:
        _ffmpeg_progress_var.reset(token)


def _terminate_process(proc: subprocess.Popen[bytes], timeout: float = 3.0) -> None:
    """Terminate a running child process, escalating to kill if it does not exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_cancellable_subprocess(
    command: Sequence[str],
    *,
    capture_output: bool = True,
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.1,
) -> subprocess.CompletedProcess[bytes]:
    """
    Run `command` via `Popen` while polling `cancel_event`.

    Captures stderr to an unbuffered temporary file when `capture_output` is
    True so large ffmpeg logs cannot deadlock on OS pipe buffers. Terminates
    the child process cleanly and raises `DownloadCancelled` when cancelled.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    stderr_file = tempfile.TemporaryFile() if capture_output else None
    progress_cb = _ffmpeg_progress_var.get()
    wants_progress = progress_cb is not None and "-progress" in command
    proc: subprocess.Popen[bytes] | None = None
    reader_thread: threading.Thread | None = None
    try:
        proc = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE if wants_progress else (subprocess.DEVNULL if capture_output else None),
            stderr=stderr_file,
            **subprocess_kwargs(capture_output=capture_output),
        )
        if wants_progress and proc.stdout is not None and progress_cb is not None:
            cb = progress_cb
            pipe = proc.stdout

            def _drain_progress() -> None:
                try:
                    for raw_line in pipe:
                        line = raw_line.decode("utf-8", "ignore").strip()
                        if line.startswith("out_time_us="):
                            raw_val = line.split("=", 1)[1].strip()
                            if raw_val.lstrip("-").isdigit():
                                micros = max(0, int(raw_val))
                                cb(micros / 1_000_000.0)
                except OSError:
                    pass

            reader_thread = threading.Thread(target=_drain_progress, daemon=True)
            reader_thread.start()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process(proc)
                raise DownloadCancelled
            try:
                returncode = proc.wait(
                    timeout=poll_interval if cancel_event is not None else None
                )
                break
            except subprocess.TimeoutExpired:
                continue
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
        stderr_bytes = b""
        if stderr_file is not None:
            stderr_file.seek(0)
            stderr_bytes = stderr_file.read()
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout=b"",
            stderr=stderr_bytes,
        )
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if reader_thread is not None and reader_thread.is_alive():
            reader_thread.join(timeout=1.0)
        if stderr_file is not None:
            stderr_file.close()


def run_cancellable_pool(
    fn: Callable[[T_in], T_out],
    items: Sequence[T_in],
    max_workers: int,
    cancel_event: threading.Event | None = None,
) -> list[T_out]:
    """
    Execute `fn` over `items` in order using up to `max_workers` threads.

    Skips spawning a thread pool when `len(items) <= 1`. Polls `cancel_event`
    and cancels queued futures immediately on cancellation or failure.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    count = len(items)
    if count == 0:
        return []
    if count == 1 or max_workers <= 1:
        results: list[T_out] = []
        for item in items:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            results.append(fn(item))
        return results

    workers = min(max_workers, count)
    executor = ThreadPoolExecutor(max_workers=workers)

    def _run_item(item: T_in) -> T_out:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        return fn(item)

    futures = [
        executor.submit(contextvars.copy_context().run, _run_item, item)
        for item in items
    ]
    future_to_idx = {fut: idx for idx, fut in enumerate(futures)}
    ordered: list[T_out | None] = [None] * count
    pending = set(futures)
    try:
        while pending:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            done, pending = wait(
                pending,
                timeout=0.1 if cancel_event is not None else None,
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                ordered[future_to_idx[fut]] = fut.result()
        executor.shutdown(wait=True)
        return cast(list[T_out], ordered)
    except BaseException:
        for fut in futures:
            fut.cancel()
        if cancel_event is not None and cancel_event.is_set() and pending:
            wait(pending, timeout=2.0)
        executor.shutdown(wait=False, cancel_futures=True)
        raise

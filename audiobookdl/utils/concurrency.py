from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import subprocess
import tempfile
import threading
from typing import Callable, List, Optional, Sequence, TypeVar, cast

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.utils import subprocess_kwargs


T_in = TypeVar("T_in")
T_out = TypeVar("T_out")


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
    cancel_event: Optional[threading.Event] = None,
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
    proc: Optional[subprocess.Popen[bytes]] = None
    try:
        proc = subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL if capture_output else None,
            stderr=stderr_file,
            **subprocess_kwargs(capture_output=capture_output),
        )
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
        if stderr_file is not None:
            stderr_file.close()


def run_cancellable_pool(
    fn: Callable[[T_in], T_out],
    items: Sequence[T_in],
    max_workers: int,
    cancel_event: Optional[threading.Event] = None,
) -> List[T_out]:
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
        results: List[T_out] = []
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

    futures = [executor.submit(_run_item, item) for item in items]
    future_to_idx = {fut: idx for idx, fut in enumerate(futures)}
    ordered: List[Optional[T_out]] = [None] * count
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
        return cast(List[T_out], ordered)
    except BaseException:
        for fut in futures:
            fut.cancel()
        if cancel_event is not None and cancel_event.is_set() and pending:
            wait(pending, timeout=2.0)
        executor.shutdown(wait=False, cancel_futures=True)
        raise

from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextvars
import os
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, TypeVar, cast
from collections.abc import Callable, Iterator, Mapping, Sequence

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


def _ffmpeg_spawn_limit() -> int:
    """Resolve the global cap on concurrent ffmpeg/ffprobe child processes."""
    raw = os.environ.get("AUDIOBOOK_DL_MAX_FFMPEG_PROCS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return min(max(2, os.cpu_count() or 4), 8)


_FFMPEG_SPAWN_SEMAPHORE = threading.BoundedSemaphore(_ffmpeg_spawn_limit())


def _is_ffmpeg_family(executable: str) -> bool:
    """Return True when `executable` names an ffmpeg/ffprobe binary (any extension)."""
    name = os.path.basename(executable).lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name == "ffmpeg" or name.startswith("ffmpeg-") or name == "ffprobe" or name.startswith("ffprobe-")


def _acquire_ffmpeg_slot(cancel_event: threading.Event | None) -> None:
    """Acquire a global ffmpeg/ffprobe spawn slot, honoring `cancel_event` while queued."""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        if _FFMPEG_SPAWN_SEMAPHORE.acquire(timeout=0.1):
            return


# POSIX-only process-group helpers: `os.killpg`/`os.getpgid`/`SIGKILL` do not
# exist on Windows, so resolve them dynamically for cross-platform type checks.
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _signal_process_group(proc: subprocess.Popen[bytes], sig: int) -> bool:
    """Send `sig` to the child's whole POSIX process group.

    Returns True when the signal was delivered; False off POSIX or on failure.
    """
    if _KILLPG is None or _GETPGID is None:
        return False
    try:
        _KILLPG(_GETPGID(proc.pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Force-kill the child's whole process tree (best effort)."""
    if os.name == "nt":
        # taskkill /T kills shim/cmd.exe wrappers and their ffmpeg grandchildren,
        # which otherwise leak and hold output file locks (WinError 32). It must
        # run while the parent PID is still alive, so it replaces proc.kill().
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                **subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        _signal_process_group(proc, _SIGKILL)
    try:
        proc.kill()
    except OSError:
        pass


def _terminate_process(proc: subprocess.Popen[bytes], timeout: float = 3.0) -> None:
    """Terminate a running child process tree, escalating to kill if it does not exit."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Terminate only the wrapper would orphan grandchildren; kill the tree.
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return
    if not _signal_process_group(proc, signal.SIGTERM):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_process_tree(proc)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _configured_ffmpeg_threads() -> str | None:
    raw = os.environ.get("AUDIOBOOK_DL_FFMPEG_THREADS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    return None


def _configured_worker_cap() -> int | None:
    raw = os.environ.get("AUDIOBOOK_DL_MAX_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _apply_ffmpeg_thread_limit(command: Sequence[str]) -> list[str]:
    """Inject `-filter_threads` and `-threads` into `ffmpeg` media invocations when configured."""
    cmd = list(command)
    threads = _configured_ffmpeg_threads()
    if threads is None or len(cmd) < 3 or "-i" not in cmd:
        return cmd
    exe_name = os.path.basename(cmd[0]).lower()
    if not (exe_name == "ffmpeg" or exe_name.startswith("ffmpeg.")):
        return cmd
    if "-threads" in cmd or "-filter_threads" in cmd:
        return cmd
    return [cmd[0], "-filter_threads", threads, *cmd[1:-1], "-threads", threads, cmd[-1]]


def run_cancellable_subprocess(
    command: Sequence[str],
    *,
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.1,
    timeout: float | None = None,
    capture_stdout: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """
    Run `command` via `Popen` while polling `cancel_event`.

    Always captures stderr to an unbuffered temporary file so large ffmpeg logs
    cannot deadlock on OS pipe buffers. When `capture_stdout` is set, stdout is
    piped and drained on a thread into `CompletedProcess.stdout`. Terminates the
    child process tree cleanly and raises `DownloadCancelled` when cancelled or
    `subprocess.TimeoutExpired` when `timeout` seconds elapse.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    effective_command = _apply_ffmpeg_thread_limit(command)
    stderr_file = tempfile.TemporaryFile()
    progress_cb = _ffmpeg_progress_var.get()
    wants_progress = progress_cb is not None and "-progress" in effective_command
    wants_stdout = wants_progress or capture_stdout
    proc: subprocess.Popen[bytes] | None = None
    reader_thread: threading.Thread | None = None
    stdout_chunks: list[bytes] = []
    slot_held = False
    try:
        if effective_command and _is_ffmpeg_family(str(effective_command[0])):
            _acquire_ffmpeg_slot(cancel_event)
            slot_held = True
        popen_kwargs = subprocess_kwargs()
        if os.name != "nt":
            # Own process group/session so _terminate_process can killpg the tree.
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            effective_command,
            stdout=subprocess.PIPE if wants_stdout else subprocess.DEVNULL,
            stderr=stderr_file,
            env=dict(env) if env is not None else None,
            **popen_kwargs,
        )
        if wants_stdout and proc.stdout is not None:
            cb = progress_cb if wants_progress else None
            pipe = proc.stdout

            def _drain_stdout() -> None:
                try:
                    for raw_line in pipe:
                        if cb is not None:
                            line = raw_line.decode("utf-8", "ignore").strip()
                            if line.startswith("out_time_us="):
                                raw_val = line.split("=", 1)[1].strip()
                                if raw_val.lstrip("-").isdigit():
                                    micros = max(0, int(raw_val))
                                    cb(micros / 1_000_000.0)
                        else:
                            stdout_chunks.append(raw_line)
                except OSError:
                    pass

            # copy_context propagates ffmpeg_progress_callback into the reader.
            reader_ctx = contextvars.copy_context()
            reader_thread = threading.Thread(
                target=reader_ctx.run, args=(_drain_stdout,), daemon=True
            )
            reader_thread.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process(proc)
                raise DownloadCancelled
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                _terminate_process(proc)
                raise subprocess.TimeoutExpired(
                    list(effective_command),
                    timeout if timeout is not None else 0.0,
                )
            wait_timeout = poll_interval if cancel_event is not None else None
            if deadline is not None:
                remaining = deadline - now
                wait_timeout = remaining if wait_timeout is None else min(wait_timeout, remaining)
            try:
                returncode = proc.wait(timeout=wait_timeout)
                break
            except subprocess.TimeoutExpired:
                continue
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
        stderr_bytes = b""
        if stderr_file is not None:
            stderr_file.seek(0)
            stderr_bytes = stderr_file.read()
        stdout_bytes = b"".join(stdout_chunks) if capture_stdout else b""
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
        )
    finally:
        if slot_held:
            _FFMPEG_SPAWN_SEMAPHORE.release()
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
    *,
    grace_seconds: float = 5.0,
) -> list[T_out]:
    """
    Execute `fn` over `items` in order using up to `max_workers` threads.

    Skips spawning a thread pool when `len(items) <= 1`. Polls `cancel_event`
    and cancels queued futures immediately on cancellation or failure. On
    error/cancel, running workers get a bounded `grace_seconds` window to
    observe cancellation and unwind before the executor is shut down, so
    callers do not race in-flight work when cleaning up shared resources.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    count = len(items)
    if count == 0:
        return []
    worker_cap = _configured_worker_cap()
    effective_workers = min(max_workers, worker_cap) if worker_cap is not None else max_workers
    if count == 1 or effective_workers <= 1:
        results: list[T_out] = []
        for item in items:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            results.append(fn(item))
        return results

    workers = min(effective_workers, count)
    executor = ThreadPoolExecutor(max_workers=workers)

    def _run_item(item: T_in) -> T_out:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        return fn(item)

    ordered: list[T_out | None] = [None] * count
    future_to_idx: dict[Any, int] = {}
    pending: set[Any] = set()
    next_idx = 0

    def _submit_up_to_window() -> None:
        nonlocal next_idx
        while next_idx < count and len(pending) < workers:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            fut = executor.submit(contextvars.copy_context().run, _run_item, items[next_idx])
            future_to_idx[fut] = next_idx
            pending.add(fut)
            next_idx += 1

    try:
        _submit_up_to_window()
        while pending:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            done, pending = wait(
                pending,
                timeout=0.1 if cancel_event is not None else None,
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                idx = future_to_idx.pop(fut)
                ordered[idx] = fut.result()
            _submit_up_to_window()
        executor.shutdown(wait=True)
        return cast(list[T_out], ordered)
    except BaseException:
        for fut in pending:
            fut.cancel()
        if pending and grace_seconds > 0:
            wait(pending, timeout=grace_seconds)
        executor.shutdown(wait=False, cancel_futures=True)
        raise

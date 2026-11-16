from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
import weakref

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()


def _get_path_lock(abs_path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(abs_path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[abs_path] = lock
        return lock


@contextmanager
def file_lock(target_path: str, timeout: float = 5.0) -> Iterator[None]:
    """
    Serialize in-process threads and cross-process access around `target_path`.

    Acquires an in-process per-path `RLock` and an advisory OS lock on
    `<target_path>.lock`.
    """
    abs_path = os.path.abspath(target_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock_path = f"{abs_path}.lock"
    thread_lock = _get_path_lock(abs_path)
    with thread_lock:
        fh = open(lock_path, "a+b")
        try:
            _acquire_os_lock(fh, timeout=timeout)
            try:
                yield
            finally:
                _release_os_lock(fh)
        finally:
            fh.close()


def _acquire_os_lock(fh, timeout: float = 5.0) -> None:
    if sys.platform == "win32":
        import msvcrt

        # Ensure at least 1 byte exists for msvcrt.locking byte-range lock.
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        deadline = time.monotonic() + timeout
        while True:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    else:
        import fcntl

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)


def _release_os_lock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _restrict_windows_private_file(path: str) -> None:
    """Restrict NTFS permissions on `path` to the current user (best effort)."""
    if os.name != "nt":
        return
    username = (os.environ.get("USERNAME") or "").strip()
    if not username or not shutil.which("icacls"):
        return
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    principal = f"{domain}\\{username}" if domain else username
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def atomic_write_text(
    target_path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    private: bool = False,
) -> str:
    """Atomically write `content` to `target_path` via a unique temp file."""
    abs_path = os.path.abspath(target_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(abs_path)}.",
        suffix=".tmp",
        dir=parent or ".",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            if os.name == "nt":
                _restrict_windows_private_file(tmp_path)
            else:
                os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, abs_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target_path

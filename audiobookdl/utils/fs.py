from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
_THREAD_LOCK_STATE = threading.local()


def _get_path_lock(abs_path: str) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(abs_path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[abs_path] = lock
        return lock


def _thread_lock_depths() -> dict[str, int]:
    depths = getattr(_THREAD_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_STATE.depths = depths
    return depths


@contextmanager
def file_lock(target_path: str, timeout: float = 5.0) -> Iterator[None]:
    """
    Serialize in-process threads and cross-process access around `target_path`.

    Acquires an in-process per-path `RLock` and an advisory OS lock on
    `<target_path>.lock`. Reentrant calls on the same thread reuse the outer
    OS file lock without deadlocking.
    """
    abs_path = os.path.abspath(target_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock_path = f"{abs_path}.lock"
    thread_lock = _get_path_lock(abs_path)
    with thread_lock:
        depths = _thread_lock_depths()
        depth = depths.get(abs_path, 0)
        if depth > 0:
            depths[abs_path] = depth + 1
            try:
                yield
            finally:
                remaining = depths.get(abs_path, 1) - 1
                if remaining <= 0:
                    depths.pop(abs_path, None)
                else:
                    depths[abs_path] = remaining
            return
        fh = open(lock_path, "a+b")
        try:
            _acquire_os_lock(fh, timeout=timeout)
            depths[abs_path] = 1
            try:
                yield
            finally:
                depths.pop(abs_path, None)
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


_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000


@functools.lru_cache(maxsize=1)
def _current_user_private_sddl() -> str | None:
    """Return a protected SDDL string granting File All Access solely to the current Windows user."""
    if os.name != "nt":
        return None
    from audiobookdl.utils._win32 import advapi32, kernel32

    kernel32_dll = kernel32()
    advapi32_dll = advapi32()

    h_token = wintypes.HANDLE()
    if not advapi32_dll.OpenProcessToken(
        kernel32_dll.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(h_token)
    ):
        return None
    try:
        needed = wintypes.DWORD(0)
        advapi32_dll.GetTokenInformation(h_token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buf = (ctypes.c_ubyte * needed.value)()
        if not advapi32_dll.GetTokenInformation(
            h_token, _TOKEN_USER, buf, needed.value, ctypes.byref(needed)
        ):
            return None
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_str_ptr = wintypes.LPWSTR()
        if not advapi32_dll.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str_ptr)):
            return None
        try:
            sid_str = sid_str_ptr.value
            return f"D:P(A;;FA;;;{sid_str})" if sid_str else None
        finally:
            if sid_str_ptr:
                kernel32_dll.LocalFree(ctypes.cast(sid_str_ptr, wintypes.HLOCAL))
    finally:
        kernel32_dll.CloseHandle(h_token)


def _apply_windows_private_dacl(path: str) -> bool:
    """Restrict NTFS permissions on `path` in-process via Win32 `advapi32`; return True on success."""
    sddl = _current_user_private_sddl()
    if not sddl:
        return False
    from audiobookdl.utils._win32 import advapi32, kernel32

    kernel32_dll = kernel32()
    advapi32_dll = advapi32()

    sd_ptr = ctypes.c_void_p()
    if not advapi32_dll.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(sd_ptr), None
    ):
        return False
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl_ptr = ctypes.c_void_p()
        if not advapi32_dll.GetSecurityDescriptorDacl(
            sd_ptr,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_ptr),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value:
            return False
        status = advapi32_dll.SetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl_ptr,
            None,
        )
        return status == 0
    finally:
        if sd_ptr:
            kernel32_dll.LocalFree(sd_ptr)


def _restrict_windows_private_file(path: str) -> None:
    """Restrict NTFS permissions on `path` to the current user (best effort)."""
    if os.name != "nt":
        return
    try:
        if _apply_windows_private_dacl(path):
            return
    except (OSError, AttributeError, ctypes.ArgumentError):
        pass
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


def replace_with_retry(
    src: str,
    dst: str,
    *,
    attempts: int = 5,
    base_delay: float = 0.05,
) -> None:
    """`os.replace` with bounded exponential backoff on `PermissionError`.

    Retries transient Windows `WinError 5`/`WinError 32` failures caused by
    antivirus scanners or the search indexer briefly holding the target open.
    Other `OSError`s propagate immediately.
    """
    last_exc: PermissionError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


def _apply_private_permissions(tmp_path: str, private: bool) -> None:
    if not private:
        return
    if os.name == "nt":
        _restrict_windows_private_file(tmp_path)
    else:
        os.chmod(tmp_path, 0o600)


def atomic_write_text(
    target_path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    private: bool = False,
    newline: str | None = None,
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
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _apply_private_permissions(tmp_path, private)
        replace_with_retry(tmp_path, abs_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target_path


def atomic_write_bytes(
    target_path: str,
    data: bytes,
    *,
    private: bool = False,
) -> str:
    """Atomically write `data` to `target_path` via a unique temp file."""
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
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _apply_private_permissions(tmp_path, private)
        replace_with_retry(tmp_path, abs_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target_path

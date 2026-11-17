"""Platform drag-and-drop helpers (native Windows `WM_DROPFILES` and optional `tkinterdnd2`)."""

from __future__ import annotations

from collections.abc import Callable
import sys
import tkinter as tk
from typing import Any


def create_tk_root() -> tk.Tk:
    """Create a `TkinterDnD.Tk` root when `tkinterdnd2` is installed, else standard `tk.Tk`."""
    try:
        from tkinterdnd2 import TkinterDnD

        return TkinterDnD.Tk()  # type: ignore[no-any-return]
    except Exception:
        return tk.Tk()


def install_windows_file_drop(
    root: tk.Tk,
    on_paths: Callable[[list[str]], None],
) -> Callable[[], None]:
    """Enable OS file/folder drops on `root` via Win32 `WM_DROPFILES` and return a cleanup callback."""
    if not sys.platform.startswith("win"):
        return lambda: None

    import ctypes
    from ctypes import wintypes

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4
    GA_ROOT = 2
    LRESULT = ctypes.c_ssize_t
    WNDPROC_TYPE = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallWindowProcW.restype = LRESULT

    set_wndproc: Any
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        set_wndproc = user32.SetWindowLongPtrW
    else:
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongW.restype = ctypes.c_void_p
        set_wndproc = user32.SetWindowLongW

    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragAcceptFiles.restype = None
    shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [wintypes.HANDLE]
    shell32.DragFinish.restype = None

    try:
        root.update_idletasks()
        raw_hwnd = int(root.winfo_id())
    except tk.TclError:
        return lambda: None

    top_hwnd = user32.GetAncestor(wintypes.HWND(raw_hwnd), GA_ROOT) or raw_hwnd
    target_hwnd = wintypes.HWND(top_hwnd)
    if not user32.IsWindow(target_hwnd):
        return lambda: None

    prev_wndproc_holder: list[int] = [0]
    cleaned_up = False

    def _wndproc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_DROPFILES:
            hdrop = wintypes.HANDLE(wparam)
            paths: list[str] = []
            try:
                count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                for idx in range(count):
                    length = shell32.DragQueryFileW(hdrop, idx, None, 0)
                    if length <= 0:
                        continue
                    buf = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(hdrop, idx, buf, length + 1)
                    if buf.value:
                        paths.append(buf.value)
            finally:
                shell32.DragFinish(hdrop)
            if paths:
                try:
                    if root.winfo_exists():
                        dropped = list(paths)

                        def _deliver_paths() -> None:
                            on_paths(dropped)

                        root.after(0, _deliver_paths)
                except tk.TclError:
                    pass
            return 0
        prev_proc = prev_wndproc_holder[0]
        if prev_proc:
            return int(user32.CallWindowProcW(ctypes.c_void_p(prev_proc), hwnd, msg, wparam, lparam))
        return 0

    callback = WNDPROC_TYPE(_wndproc)
    callback_ptr = ctypes.cast(callback, ctypes.c_void_p)
    shell32.DragAcceptFiles(target_hwnd, True)
    prev = set_wndproc(target_hwnd, GWLP_WNDPROC, callback_ptr)
    if not prev:
        shell32.DragAcceptFiles(target_hwnd, False)
        return lambda: None
    prev_wndproc_holder[0] = int(prev)

    def _cleanup(_event: tk.Event | None = None) -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        prev_proc = prev_wndproc_holder[0]
        prev_wndproc_holder[0] = 0
        try:
            if user32.IsWindow(target_hwnd):
                if prev_proc:
                    set_wndproc(target_hwnd, GWLP_WNDPROC, ctypes.c_void_p(prev_proc))
                shell32.DragAcceptFiles(target_hwnd, False)
        except Exception:
            pass

    # Keep callback alive on `root` until window destruction.
    root._win_drop_callback = callback  # type: ignore[attr-defined]
    root._win_drop_cleanup = _cleanup  # type: ignore[attr-defined]
    root.bind("<Destroy>", lambda ev: _cleanup() if getattr(ev, "widget", None) is root else None, add="+")
    return _cleanup

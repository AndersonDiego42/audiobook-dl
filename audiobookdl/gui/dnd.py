"""Platform drag-and-drop helpers (native Windows `WM_DROPFILES` and optional `tkinterdnd2`)."""

from __future__ import annotations

from collections.abc import Callable
import importlib
import sys
import tkinter as tk
from typing import Any, cast


def create_tk_root() -> tk.Tk:
    """Create a `TkinterDnD.Tk` root when `tkinterdnd2` is installed, else standard `tk.Tk`."""
    try:
        mod = importlib.import_module("tkinterdnd2")
    except ImportError:
        return tk.Tk()
    prev_default = getattr(tk, "_default_root", None)
    try:
        return cast(tk.Tk, mod.TkinterDnD.Tk())
    except (AttributeError, tk.TclError):
        partial = getattr(tk, "_default_root", None)
        if partial is not None and partial is not prev_default:
            try:
                partial.destroy()
            except tk.TclError:
                pass
            tk._default_root = prev_default  # type: ignore[attr-defined]
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
    WM_COPYDATA = 0x004A
    WM_COPYGLOBALDATA = 0x0049
    WM_NCDESTROY = 0x0082
    MSGFLT_ALLOW = 1
    GA_ROOT = 2
    SUBCLASS_ID = 0x4142444C
    LRESULT = ctypes.c_ssize_t
    SUBCLASSPROC = ctypes.WINFUNCTYPE(
        LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    comctl32 = ctypes.windll.comctl32

    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL

    comctl32.SetWindowSubclass.argtypes = [
        wintypes.HWND,
        SUBCLASSPROC,
        ctypes.c_size_t,
        ctypes.c_size_t,
    ]
    comctl32.SetWindowSubclass.restype = wintypes.BOOL
    comctl32.DefSubclassProc.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    comctl32.DefSubclassProc.restype = LRESULT
    comctl32.RemoveWindowSubclass.argtypes = [
        wintypes.HWND,
        SUBCLASSPROC,
        ctypes.c_size_t,
    ]
    comctl32.RemoveWindowSubclass.restype = wintypes.BOOL

    change_filter = getattr(user32, "ChangeWindowMessageFilterEx", None)
    if change_filter is not None:
        change_filter.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        change_filter.restype = wintypes.BOOL

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

    cleaned_up = False
    callback_holder: list[Any] = [None]

    def _cleanup(_event: tk.Event | None = None) -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        cb = callback_holder[0]
        callback_holder[0] = None
        try:
            if user32.IsWindow(target_hwnd):
                if cb is not None:
                    comctl32.RemoveWindowSubclass(target_hwnd, cb, SUBCLASS_ID)
                shell32.DragAcceptFiles(target_hwnd, False)
        except Exception:
            pass
        try:
            root_ref: Any = root
            root_ref._win_drop_installed_ok = False
        except Exception:
            pass

    def _subclass_proc(
        hwnd: Any,
        msg: int,
        wparam: int,
        lparam: int,
        _subclass_id: int,
        _ref_data: int,
    ) -> int:
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
        if msg == WM_NCDESTROY:
            _cleanup()
        return int(comctl32.DefSubclassProc(hwnd, msg, wparam, lparam))

    callback = SUBCLASSPROC(_subclass_proc)
    callback_holder[0] = callback
    shell32.DragAcceptFiles(target_hwnd, True)
    if change_filter is not None:
        for allowed_msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
            try:
                change_filter(target_hwnd, allowed_msg, MSGFLT_ALLOW, None)
            except Exception:
                pass
    installed = comctl32.SetWindowSubclass(target_hwnd, callback, SUBCLASS_ID, 0)
    if not installed:
        shell32.DragAcceptFiles(target_hwnd, False)
        callback_holder[0] = None
        return lambda: None

    # Keep callback alive on `root` until window destruction.
    root_holder: Any = root
    root_holder._win_drop_callback = callback
    root_holder._win_drop_cleanup = _cleanup
    root_holder._win_drop_installed_ok = True
    root.bind("<Destroy>", lambda ev: _cleanup() if getattr(ev, "widget", None) is root else None, add="+")
    return _cleanup

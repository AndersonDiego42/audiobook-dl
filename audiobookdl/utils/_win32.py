"""Shared Windows Win32/ctypes helpers (DPAPI, kernel32, advapi32).

All DLL handles are private ``ctypes.WinDLL`` instances loaded lazily and
configured exactly once, so callers never mutate the shared ``ctypes.windll``
global signatures (which caused cross-module ``ArgumentError`` crashes and
64-bit pointer truncation — see AUD-036).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
from typing import Any


_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_DLL_LOCK = threading.Lock()
_DLLS: dict[str, Any] = {}


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    if not data:
        return _DataBlob(0, None), None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def _require_windows(dll_name: str) -> Any:
    if os.name != "nt":
        raise OSError(f"Win32 API '{dll_name}' is unavailable on this platform")
    return _load_dll(dll_name)


def _load_dll(name: str) -> Any:
    dll = _DLLS.get(name)
    if dll is not None:
        return dll
    with _DLL_LOCK:
        dll = _DLLS.get(name)
        if dll is None:
            dll = ctypes.WinDLL(name, use_last_error=True)
            _configure_dll(name, dll)
            _DLLS[name] = dll
    return dll


def _configure_dll(name: str, dll: Any) -> None:
    if name == "kernel32":
        dll.GetCurrentProcess.argtypes = []
        dll.GetCurrentProcess.restype = wintypes.HANDLE
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        dll.CloseHandle.restype = wintypes.BOOL
        dll.LocalFree.argtypes = [wintypes.HLOCAL]
        dll.LocalFree.restype = wintypes.HLOCAL
    elif name == "crypt32":
        dll.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        dll.CryptProtectData.restype = wintypes.BOOL
        dll.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        dll.CryptUnprotectData.restype = wintypes.BOOL
    elif name == "advapi32":
        dll.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        dll.OpenProcessToken.restype = wintypes.BOOL
        dll.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        dll.GetTokenInformation.restype = wintypes.BOOL
        dll.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        dll.ConvertSidToStringSidW.restype = wintypes.BOOL
        dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG),
        ]
        dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        dll.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        dll.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        dll.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        dll.SetNamedSecurityInfoW.restype = wintypes.DWORD


def kernel32() -> Any:
    """Return the private, preconfigured `kernel32` DLL handle."""
    return _require_windows("kernel32")


def crypt32() -> Any:
    """Return the private, preconfigured `crypt32` DLL handle."""
    return _require_windows("crypt32")


def advapi32() -> Any:
    """Return the private, preconfigured `advapi32` DLL handle."""
    return _require_windows("advapi32")


def _entropy_blob(entropy: bytes | None) -> tuple[_DataBlob | None, Any]:
    if entropy is None:
        return None, None
    return _make_blob(entropy)


def dpapi_protect(data: bytes, entropy: bytes | None = None) -> bytes:
    """Encrypt `data` with Windows DPAPI (`CryptProtectData`)."""
    crypt32_dll = crypt32()
    kernel32_dll = kernel32()

    in_blob, _in_buf = _make_blob(data)
    entropy_blob, _ent_buf = _entropy_blob(entropy)
    entropy_arg = ctypes.byref(entropy_blob) if entropy_blob is not None else None
    out_blob = _DataBlob()
    ok = crypt32_dll.CryptProtectData(
        ctypes.byref(in_blob),
        "audiobook-dl",
        entropy_arg,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32_dll.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def dpapi_unprotect(data: bytes, entropy: bytes | None = None) -> bytes:
    """Decrypt DPAPI-protected `data` (`CryptUnprotectData`)."""
    crypt32_dll = crypt32()
    kernel32_dll = kernel32()

    in_blob, _in_buf = _make_blob(data)
    entropy_blob, _ent_buf = _entropy_blob(entropy)
    entropy_arg = ctypes.byref(entropy_blob) if entropy_blob is not None else None
    out_blob = _DataBlob()
    ok = crypt32_dll.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        entropy_arg,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32_dll.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

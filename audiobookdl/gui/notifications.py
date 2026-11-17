"""Cross-platform OS desktop notifications when queued downloads finish."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading

from audiobookdl.pipeline import DownloadSummary

WINDOWS_APP_USER_MODEL_ID = "jo1gi.audiobook-dl"
_POWERSHELL_FALLBACK_AUMID = (
    r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
)


def format_completion_notification(
    summary: DownloadSummary | None,
    exc: Exception | None,
) -> tuple[str, str] | None:
    """
    Build a `(title, message)` pair for desktop notification when a run ends.

    Returns `None` when the run was user-cancelled or no completion event applies.
    """
    if exc is not None:
        detail = str(exc).strip() or type(exc).__name__
        if len(detail) > 160:
            detail = detail[:157] + "..."
        return ("audiobook-dl: Download Failed", detail)

    if summary is None or summary.cancelled:
        return None

    if summary.failed > 0 and summary.completed == 0:
        noun = "audiobook" if summary.failed == 1 else "audiobooks"
        return (
            "audiobook-dl: Download Failed",
            f"{summary.failed} {noun} failed to download.",
        )

    if summary.failed > 0:
        return (
            "audiobook-dl: Batch Finished",
            f"Completed {summary.completed}, failed {summary.failed}.",
        )

    count = summary.completed
    if count <= 0:
        return None
    noun = "audiobook" if count == 1 else "audiobooks"
    return (
        "audiobook-dl: Download Complete",
        f"Finished downloading {count} {noun}.",
    )


def _resolve_windows_powershell() -> tuple[str | None, bool]:
    """Return `(executable_path, supports_winrt)` preferring Windows PowerShell 5.1."""
    ps5 = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps5:
        return ps5, True
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    winps_path = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(winps_path):
        return winps_path, True
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if pwsh:
        return pwsh, False
    return None, False


def set_windows_process_app_id(aumid: str = WINDOWS_APP_USER_MODEL_ID) -> bool:
    """Bind the current process to a custom Windows AppUserModelID for taskbar & toasts."""
    if sys.platform != "win32":
        return False
    try:
        shell32 = getattr(ctypes, "windll", None)
        if shell32 is None:
            return False
        rc = int(shell32.shell32.SetCurrentProcessExplicitAppUserModelID(str(aumid)))
        return rc >= 0
    except (AttributeError, OSError, ValueError):
        return False


def ensure_windows_aumid_registered(
    *,
    app_name: str = "audiobook-dl",
    aumid: str = WINDOWS_APP_USER_MODEL_ID,
    icon_path: str | None = None,
) -> bool:
    """Register per-user HKCU AppUserModelId metadata so Windows toasts show `app_name`."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    key_path = rf"Software\Classes\AppUserModelId\{aumid}"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, str(app_name))
            resolved_icon = icon_path
            if resolved_icon is None:
                candidate = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "icon.ico")
                if os.path.isfile(candidate):
                    resolved_icon = candidate
            if resolved_icon and os.path.isfile(resolved_icon):
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, os.path.abspath(resolved_icon))
        return True
    except OSError:
        return False


def _send_windows_toast(title: str, message: str, *, app_name: str, timeout: float) -> bool:
    """Display a Windows 10/11 toast notification via WinRT PowerShell 5.1 or `pwsh` NotifyIcon fallback."""
    exe, supports_winrt = _resolve_windows_powershell()
    if not exe:
        return False

    aumid = (
        WINDOWS_APP_USER_MODEL_ID
        if ensure_windows_aumid_registered(app_name=app_name, aumid=WINDOWS_APP_USER_MODEL_ID)
        else _POWERSHELL_FALLBACK_AUMID
    )
    if supports_winrt:
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
            "$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]\n"
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
            "$texts = $template.GetElementsByTagName('text')\n"
            "$texts.Item(0).AppendChild($template.CreateTextNode($env:ABDL_NOTIFY_TITLE)) | Out-Null\n"
            "$texts.Item(1).AppendChild($template.CreateTextNode($env:ABDL_NOTIFY_MSG)) | Out-Null\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($template)\n"
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:ABDL_NOTIFY_APP)\n"
            "$notifier.Show($toast)\n"
        )
    else:
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "Add-Type -AssemblyName System.Drawing\n"
            "$notify = New-Object System.Windows.Forms.NotifyIcon\n"
            "$notify.Icon = [System.Drawing.SystemIcons]::Information\n"
            "$notify.Visible = $true\n"
            "$notify.ShowBalloonTip(5000, $env:ABDL_NOTIFY_TITLE, $env:ABDL_NOTIFY_MSG, "
            "[System.Windows.Forms.ToolTipIcon]::Info)\n"
            "Start-Sleep -Milliseconds 200\n"
            "$notify.Dispose()\n"
        )
    env = os.environ.copy()
    env["ABDL_NOTIFY_TITLE"] = title
    env["ABDL_NOTIFY_MSG"] = message
    env["ABDL_NOTIFY_APP"] = aumid
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creation_flags,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _send_macos_notification(title: str, message: str, *, timeout: float) -> bool:
    """Display a macOS notification banner using `osascript`."""
    exe = shutil.which("osascript")
    if not exe:
        return False
    script = "on run argv\n  display notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        completed = subprocess.run(
            [exe, "-e", script, title, message],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _send_linux_notification(title: str, message: str, *, app_name: str, timeout: float) -> bool:
    """Display a freedesktop desktop notification using `notify-send`."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        completed = subprocess.run(
            [exe, "--app-name", app_name, "--", title, message],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def send_os_notification(
    title: str,
    message: str,
    *,
    app_name: str = "audiobook-dl",
    platform_name: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Send a best-effort native OS desktop notification without raising."""
    plat = (platform_name or sys.platform).lower()
    if plat.startswith("win"):
        return _send_windows_toast(title, message, app_name=app_name, timeout=timeout)
    if plat == "darwin":
        return _send_macos_notification(title, message, timeout=timeout)
    return _send_linux_notification(title, message, app_name=app_name, timeout=timeout)


def send_os_notification_async(
    title: str,
    message: str,
    *,
    app_name: str = "audiobook-dl",
    platform_name: str | None = None,
    timeout: float = 5.0,
) -> threading.Thread:
    """Dispatch `send_os_notification` on a daemon thread so GUI responsiveness stays snappy."""
    thread = threading.Thread(
        target=send_os_notification,
        args=(title, message),
        kwargs={"app_name": app_name, "platform_name": platform_name, "timeout": timeout},
        daemon=True,
        name="audiobook-dl-notify",
    )
    thread.start()
    return thread

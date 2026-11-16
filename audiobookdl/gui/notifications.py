"""Cross-platform OS desktop notifications when queued downloads finish."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

from audiobookdl.pipeline import DownloadSummary


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


def _send_windows_toast(title: str, message: str, *, app_name: str, timeout: float) -> bool:
    """Display a Windows 10/11 toast notification via WinRT PowerShell without flashing a window."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False

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
    env = os.environ.copy()
    env["ABDL_NOTIFY_TITLE"] = title
    env["ABDL_NOTIFY_MSG"] = message
    env["ABDL_NOTIFY_APP"] = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
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

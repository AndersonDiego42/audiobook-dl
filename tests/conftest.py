"""Global pytest fixtures shared across unit, GUI, and E2E tests."""

import ctypes
import gc
import os
import sys
import threading
import time

import pytest


_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def _is_truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV


def _lower_process_priority() -> None:
    """Lower test process priority so child Python/FFmpeg work never freezes the desktop."""
    if _is_truthy_env("AUDIOBOOK_DL_TEST_NORMAL_PRIORITY"):
        return
    if sys.platform.startswith("win"):
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetCurrentProcess()
            kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    elif hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass


def _configured_cooldown_seconds() -> float:
    raw = os.environ.get("AUDIOBOOK_DL_TEST_COOLDOWN_MS")
    if raw is not None and raw.strip():
        try:
            return max(0.0, float(raw.strip()) / 1000.0)
        except ValueError:
            return 0.0
    if _is_truthy_env("CI"):
        return 0.0
    return 0.025


def pytest_configure(config: pytest.Config) -> None:
    """Enable low-power test defaults (lower priority, 1-thread FFmpeg, 1-worker pools)."""
    _ = config
    _lower_process_priority()
    os.environ.setdefault("AUDIOBOOK_DL_FFMPEG_THREADS", "1")
    os.environ.setdefault("AUDIOBOOK_DL_MAX_WORKERS", "1")


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Reclaim GUI wrappers and pause briefly after heavy tests to keep local CPU cool."""
    if item.get_closest_marker("gui") is not None:
        gc.collect()
    if nextitem is None:
        return
    if any(item.get_closest_marker(m) is not None for m in ("gui", "ffmpeg", "e2e")):
        delay = _configured_cooldown_seconds()
        if delay > 0:
            time.sleep(delay)


@pytest.fixture(autouse=True)
def _respect_no_worker_cap(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow pool-sizing unit tests marked `no_worker_cap` to observe production worker counts."""
    if request.node.get_closest_marker("no_worker_cap") is not None:
        monkeypatch.delenv("AUDIOBOOK_DL_MAX_WORKERS", raising=False)


@pytest.fixture(autouse=True)
def _suppress_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent GUI tests from spawning real OS desktop toast notifications."""

    def _noop_async(*_args, **_kwargs) -> threading.Thread:
        return threading.Thread(target=lambda: None, daemon=True)

    monkeypatch.setattr(
        "audiobookdl.gui.notifications.send_os_notification_async",
        _noop_async,
    )
    monkeypatch.setattr(
        "audiobookdl.gui.queue_panel.send_os_notification_async",
        _noop_async,
    )

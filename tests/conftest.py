"""Global pytest fixtures shared across unit, GUI, and E2E tests."""

import threading

import pytest


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

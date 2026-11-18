"""Optional system-tray integration (`pystray`) for background scheduled sync."""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
import os
import threading
from typing import Any


def is_tray_supported() -> bool:
    """Return `True` when optional `pystray` dependency can be imported."""
    return importlib.util.find_spec("pystray") is not None


def _load_tray_image() -> Any:
    """Load the bundled application icon as a Pillow `Image`, or synthesize a compact fallback."""
    from PIL import Image, ImageDraw

    assets_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "assets",
    )
    for name in ("icon.ico", "audiobook-dl.ico", "icon.png"):
        candidate = os.path.join(assets_dir, name)
        if os.path.isfile(candidate):
            try:
                return Image.open(candidate)
            except Exception:
                pass
    img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((14, 12, 50, 52), outline=(255, 255, 255, 255), width=4)
    draw.rectangle((22, 20, 42, 44), fill=(255, 255, 255, 255))
    return img


class TrayIconController:
    """Manages an optional background `pystray` icon and thread-safe GUI callbacks."""

    def __init__(
        self,
        *,
        dispatch: Callable[[Callable[[], None]], object],
        on_restore: Callable[[], None],
        on_sync_now: Callable[[], None],
        on_exit: Callable[[], None],
        tooltip: str = "audiobook-dl",
    ) -> None:
        self._dispatch = dispatch
        self._on_restore = on_restore
        self._on_sync_now = on_sync_now
        self._on_exit = on_exit
        self._tooltip = tooltip
        self._lock = threading.Lock()
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def is_available() -> bool:
        """Return `True` when `pystray` is installed in the runtime environment."""
        return is_tray_supported()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._icon is not None

    def start(self) -> bool:
        """Start the tray icon thread if `pystray` is installed; returns `True` if running."""
        with self._lock:
            if self._icon is not None:
                return True
        if not self.is_available():
            return False
        import pystray

        def _invoke(cb: Callable[[], None]) -> Callable[..., None]:
            def _handler(*_args: Any) -> None:
                try:
                    self._dispatch(cb)
                except Exception:
                    pass

            return _handler

        menu = pystray.Menu(
            pystray.MenuItem("Open audiobook-dl", _invoke(self._on_restore), default=True),
            pystray.MenuItem("Sync Now", _invoke(self._on_sync_now)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", _invoke(self._on_exit)),
        )
        icon = pystray.Icon("audiobook-dl", _load_tray_image(), self._tooltip, menu)
        with self._lock:
            self._icon = icon
            self._thread = threading.Thread(target=icon.run, name="TrayIconThread", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        """Stop and remove the active tray icon if running."""
        with self._lock:
            icon = self._icon
            self._icon = None
            self._thread = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

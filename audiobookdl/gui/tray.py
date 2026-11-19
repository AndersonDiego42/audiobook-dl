"""Optional system-tray integration (`pystray`) for background scheduled sync."""

from __future__ import annotations

from collections.abc import Callable
import importlib.resources
import importlib.util
import inspect
import os
import queue
import sys
import threading
from typing import Any, Literal


TrayAction = Literal["restore", "sync", "exit"]


def is_tray_supported() -> bool:
    """Return `True` when optional `pystray` tray integration is supported on this platform."""
    if sys.platform == "darwin":
        return False
    if sys.platform != "win32" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    return importlib.util.find_spec("pystray") is not None


def _load_tray_image() -> Any:
    """Load the bundled application icon as a Pillow `Image`, or synthesize a compact fallback."""
    from PIL import Image, ImageDraw

    try:
        assets_dir = importlib.resources.files("audiobookdl").joinpath("assets")
        for name in ("icon.ico", "audiobook-dl.ico", "icon.png"):
            candidate = assets_dir.joinpath(name)
            try:
                if candidate.is_file():
                    with candidate.open("rb") as fh, Image.open(fh) as opened:
                        opened.load()
                        return opened.copy()
            except Exception:
                pass
    except Exception:
        pass
    img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((14, 12, 50, 52), outline=(255, 255, 255, 255), width=4)
    draw.rectangle((22, 20, 42, 44), fill=(255, 255, 255, 255))
    return img


class TrayIconController:
    """Manages an optional background `pystray` icon and thread-safe GUI action queuing."""

    def __init__(
        self,
        *,
        on_restore: Callable[[], None],
        on_sync_now: Callable[[], None],
        on_exit: Callable[[], None],
        tooltip: str = "audiobook-dl",
    ) -> None:
        self._on_restore = on_restore
        self._on_sync_now = on_sync_now
        self._on_exit = on_exit
        self._tooltip = tooltip
        self._lock = threading.Lock()
        self._action_queue: queue.SimpleQueue[TrayAction] = queue.SimpleQueue()
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None
        self._ready_event: threading.Event | None = None

    @staticmethod
    def is_available() -> bool:
        """Return `True` when `pystray` is supported and installed in the runtime environment."""
        return is_tray_supported()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._icon is not None

    def _enqueue_action(self, action: TrayAction) -> Callable[..., None]:
        def _handler(*_args: Any) -> None:
            self._action_queue.put(action)

        return _handler

    def _run_icon(self, icon: Any, ready_event: threading.Event) -> None:
        def _on_setup(ic: Any) -> None:
            try:
                ic.visible = True
            except Exception:
                pass
            ready_event.set()

        try:
            supports_setup = False
            try:
                sig = inspect.signature(icon.run)
                supports_setup = "setup" in sig.parameters
            except (TypeError, ValueError):
                supports_setup = False
            if supports_setup:
                icon.run(setup=_on_setup)
            else:
                ready_event.set()
                icon.run()
        except Exception:
            pass
        finally:
            ready_event.set()
            with self._lock:
                if self._icon is icon:
                    self._icon = None
                    self._thread = None
                    self._ready_event = None

    def start(self) -> bool:
        """Start the tray icon thread if supported; returns `True` if running."""
        with self._lock:
            if self._icon is not None:
                return True
            if not self.is_available():
                return False
            try:
                import pystray

                menu = pystray.Menu(
                    pystray.MenuItem("Open audiobook-dl", self._enqueue_action("restore"), default=True),
                    pystray.MenuItem("Sync Now", self._enqueue_action("sync")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Exit", self._enqueue_action("exit")),
                )
                icon = pystray.Icon("audiobook-dl", _load_tray_image(), self._tooltip, menu)
            except Exception:
                return False
            ready_event = threading.Event()
            thread = threading.Thread(
                target=self._run_icon,
                args=(icon, ready_event),
                name="TrayIconThread",
                daemon=True,
            )
            self._icon = icon
            self._thread = thread
            self._ready_event = ready_event
            thread.start()
        return True

    def drain_pending_actions(self) -> int:
        """Dispatch queued tray actions on the calling (Tk main) thread."""
        handlers: dict[TrayAction, Callable[[], None]] = {
            "restore": self._on_restore,
            "sync": self._on_sync_now,
            "exit": self._on_exit,
        }
        processed = 0
        while True:
            try:
                action = self._action_queue.get_nowait()
            except queue.Empty:
                break
            handler = handlers.get(action)
            if handler is not None:
                processed += 1
                handler()
                if action == "exit":
                    break
        return processed

    def stop(self) -> None:
        """Stop and remove the active tray icon if running."""
        with self._lock:
            icon = self._icon
            thread = self._thread
            ready_event = self._ready_event
            self._icon = None
            self._thread = None
            self._ready_event = None
        while True:
            try:
                self._action_queue.get_nowait()
            except queue.Empty:
                break
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.2)
                if thread.is_alive() and ready_event is not None:
                    ready_event.wait(timeout=0.8)
                    try:
                        icon.stop()
                    except Exception:
                        pass
                    thread.join(timeout=1.0)

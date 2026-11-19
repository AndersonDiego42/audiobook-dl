"""Environment-specific configuration for end-to-end tests."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float for {name}: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _detect_headless() -> bool:
    if "AUDIOBOOK_DL_E2E_HEADLESS" in os.environ:
        return _env_bool("AUDIOBOOK_DL_E2E_HEADLESS")
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    return _env_bool("CI")


@dataclass(frozen=True)
class E2EConfig:
    """Runtime settings governing E2E execution across local and CI environments."""

    target_mode: str
    gui_executable: Path | None
    timeout_seconds: float
    server_host: str
    ffmpeg_dir: Path | None
    artifacts_dir: Path | None
    require_ffmpeg: bool
    require_gui: bool
    headless: bool

    @classmethod
    def from_env(cls) -> E2EConfig:
        """Load `E2EConfig` from `AUDIOBOOK_DL_E2E_*` environment variables."""
        target_mode = os.environ.get("AUDIOBOOK_DL_E2E_TARGET", "module").strip().lower()
        if target_mode not in ("module", "executable"):
            raise ValueError(
                f"Unsupported AUDIOBOOK_DL_E2E_TARGET={target_mode!r}; expected 'module' or 'executable'"
            )

        gui_bin_raw = os.environ.get("AUDIOBOOK_DL_E2E_GUI_BIN", "").strip()
        ffmpeg_raw = os.environ.get("AUDIOBOOK_DL_E2E_FFMPEG_DIR", "").strip()
        artifacts_raw = os.environ.get("AUDIOBOOK_DL_E2E_ARTIFACTS_DIR", "").strip()

        return cls(
            target_mode=target_mode,
            gui_executable=Path(gui_bin_raw) if gui_bin_raw else None,
            timeout_seconds=_env_float("AUDIOBOOK_DL_E2E_TIMEOUT", 30.0),
            server_host=os.environ.get("AUDIOBOOK_DL_E2E_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1",
            ffmpeg_dir=Path(ffmpeg_raw) if ffmpeg_raw else None,
            artifacts_dir=Path(artifacts_raw) if artifacts_raw else None,
            require_ffmpeg=_env_bool("AUDIOBOOK_DL_E2E_REQUIRE_FFMPEG", default=False),
            require_gui=_env_bool("AUDIOBOOK_DL_E2E_REQUIRE_GUI", default=False),
            headless=_detect_headless(),
        )

    def gui_self_test_command(self) -> list[str]:
        """Return argv used to invoke the GUI `--self-test` readiness check."""
        if self.target_mode == "executable":
            if self.gui_executable is not None:
                return [str(self.gui_executable), "--self-test"]
            return ["audiobook-dl", "--self-test"]
        return [sys.executable, "-m", "audiobookdl", "--self-test"]

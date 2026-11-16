"""Reusable E2E execution harnesses for CLI subprocesses, FFmpeg, and Desktop GUI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import site
import subprocess
import time
import tkinter as tk
from typing import Any
from collections.abc import Callable, Sequence

import pytest

from audiobookdl import logging
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.utils import find_executable
from tests.e2e.config import E2EConfig
from tests.e2e.diagnostics import E2EDiagnosticRecorder


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def require_ffmpeg(config: E2EConfig) -> tuple[str, str]:
    """Verify `ffmpeg` and `ffprobe` readiness, skipping or failing per `config`."""
    ffmpeg_dir = str(config.ffmpeg_dir) if config.ffmpeg_dir is not None else None
    with logging.job_context(logging.JobContext(ffmpeg_dir=ffmpeg_dir)):
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        msg = f"ffmpeg/ffprobe unavailable (ffmpeg={ffmpeg!r}, ffprobe={ffprobe!r})"
        if config.require_ffmpeg:
            pytest.fail(msg)
        pytest.skip(msg)
    assert ffmpeg is not None and ffprobe is not None
    for exe in (ffmpeg, ffprobe):
        try:
            proc = subprocess.run(
                [exe, "-version"],
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        except OSError as exc:
            msg = f"Failed executing {exe}: {exc}"
            if config.require_ffmpeg:
                pytest.fail(msg)
            pytest.skip(msg)
        if proc.returncode != 0:
            msg = f"{exe} -version exited with {proc.returncode}: {proc.stderr}"
            if config.require_ffmpeg:
                pytest.fail(msg)
            pytest.skip(msg)
    return ffmpeg, ffprobe


class E2EWorkspace:
    """Isolated per-test filesystem and environment layout."""

    def __init__(self, root: Path, config: E2EConfig) -> None:
        self.root = root
        self.config = config
        self.work_dir = root / "work"
        self.output_dir = root / "output"
        self.config_dir = root / "config"
        self.db_dir = root / "db"
        self.home_dir = root / "home"
        self.appdata_dir = self.home_dir / "AppData" / "Roaming"
        self.localappdata_dir = self.home_dir / "AppData" / "Local"
        self.xdg_config_dir = self.home_dir / "xdg_config"
        self.xdg_data_dir = self.home_dir / "xdg_data"
        for directory in (
            self.work_dir,
            self.output_dir,
            self.config_dir,
            self.db_dir,
            self.home_dir,
            self.appdata_dir,
            self.localappdata_dir,
            self.xdg_config_dir,
            self.xdg_data_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "audiobook-dl.toml"

    def write_toml(self, content: str) -> Path:
        """Write `audiobook-dl.toml` inside the isolated config directory."""
        self.config_path.write_text(content, encoding="utf-8")
        return self.config_path

    def write_input_file(self, filename: str, urls: Sequence[str], *, bom: bool = False) -> Path:
        """Create a URL batch file inside `work_dir`."""
        path = self.work_dir / filename
        payload = ("\n".join(urls) + "\n").encode("utf-8")
        if bom:
            payload = b"\xef\xbb\xbf" + payload
        path.write_bytes(payload)
        return path

    def build_env(
        self,
        *,
        proxy_url: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return `(full_env, overrides_only)` isolated from host user state."""
        env = dict(os.environ)
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)

        overrides: dict[str, str] = {
            "HOME": str(self.home_dir),
            "USERPROFILE": str(self.home_dir),
            "APPDATA": str(self.appdata_dir),
            "LOCALAPPDATA": str(self.localappdata_dir),
            "XDG_CONFIG_HOME": str(self.xdg_config_dir),
            "XDG_DATA_HOME": str(self.xdg_data_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        if self.config.target_mode == "module":
            existing_pp = env.get("PYTHONPATH", "")
            overrides["PYTHONPATH"] = (
                f"{REPO_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(REPO_ROOT)
            )
            # Preserve host user site-packages even when HOME/APPDATA are sandboxed
            if site.ENABLE_USER_SITE and site.USER_BASE and "PYTHONUSERBASE" not in env:
                overrides["PYTHONUSERBASE"] = site.USER_BASE
        if self.config.ffmpeg_dir is not None:
            overrides["PATH"] = f"{self.config.ffmpeg_dir}{os.pathsep}{env.get('PATH', '')}"
        if proxy_url:
            overrides["HTTP_PROXY"] = proxy_url
            overrides["http_proxy"] = proxy_url
            overrides["NO_PROXY"] = "127.0.0.1,localhost"
            overrides["no_proxy"] = "127.0.0.1,localhost"
        if extra_env:
            overrides.update(extra_env)

        env.update(overrides)
        return env, overrides


@dataclass(frozen=True)
class CliResult:
    """Result returned by `CliRunner` subprocess execution."""

    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class CliRunner:
    """Subprocess runner for `audiobook-dl` CLI and `--self-test` entry points."""

    def __init__(
        self,
        config: E2EConfig,
        workspace: E2EWorkspace,
        diagnostics: E2EDiagnosticRecorder,
        proxy_url: str | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.diagnostics = diagnostics
        self.proxy_url = proxy_url

    def _exec(
        self,
        command: list[str],
        *,
        use_proxy: bool = True,
        timeout: float | None = None,
        extra_env: dict[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> CliResult:
        effective_timeout = timeout or self.config.timeout_seconds
        env, overrides = self.workspace.build_env(
            proxy_url=self.proxy_url if use_proxy else None,
            extra_env=extra_env,
        )
        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.workspace.work_dir),
                env=env,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                check=False,
            )
            returncode: int | None = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = None
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
        duration = time.monotonic() - start

        self.diagnostics.record_subprocess(
            command=command,
            cwd=str(self.workspace.work_dir),
            returncode=returncode,
            duration_seconds=duration,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            env_overrides=overrides,
        )
        return CliResult(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
        )

    def check_ready(self) -> CliResult:
        """Verify CLI entry point responds to `--version` cleanly."""
        result = self._exec(self.config.cli_command() + ["--version"], use_proxy=False, timeout=10.0)
        if result.timed_out or result.returncode != 0:
            raise AssertionError(
                f"CLI readiness check failed (rc={result.returncode}, timed_out={result.timed_out}): "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
        return result

    def run(
        self,
        args: Sequence[str],
        *,
        use_proxy: bool = True,
        timeout: float | None = None,
        extra_env: dict[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> CliResult:
        """Invoke `audiobook-dl` with `args` inside the isolated E2E workspace."""
        return self._exec(
            self.config.cli_command() + list(args),
            use_proxy=use_proxy,
            timeout=timeout,
            extra_env=extra_env,
            stdin_text=stdin_text,
        )

    def run_gui_self_test(self, *, timeout: float | None = None) -> CliResult:
        """Invoke `audiobookdl.gui --self-test` in the isolated E2E workspace."""
        return self._exec(
            self.config.gui_self_test_command(),
            use_proxy=False,
            timeout=timeout,
        )


class GuiHarness:
    """Lifecycle, readiness, event-loop pumping, and teardown harness for `AudiobookDLApp`."""

    def __init__(
        self,
        config: E2EConfig,
        workspace: E2EWorkspace,
        diagnostics: E2EDiagnosticRecorder,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.diagnostics = diagnostics
        self.root: tk.Tk | None = None
        self.app: AudiobookDLApp | None = None
        self._closed = False

    def start(self) -> AudiobookDLApp:
        """Create Tk root, launch `AudiobookDLApp`, and verify UI readiness."""
        if self.config.target_mode == "executable":
            pytest.skip(
                "GuiHarness requires AUDIOBOOK_DL_E2E_TARGET=module (in-process Tkinter); "
                "frozen GUI executable readiness is verified via gui_self_test_command()"
            )
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            msg = f"Tk display unavailable: {exc}"
            if self.config.require_gui:
                pytest.fail(msg)
            pytest.skip(msg)
        root.withdraw()
        self.root = root
        app = AudiobookDLApp(root, config_location=str(self.workspace.config_path))
        app.output_dir_var.set(str(self.workspace.output_dir))
        root.update_idletasks()
        if not root.title().startswith("audiobook-dl"):
            raise AssertionError(f"GUI readiness check failed; unexpected title {root.title()!r}")
        self.app = app
        self._closed = False
        self.diagnostics.record_gui_snapshot(self.snapshot())
        return app

    def snapshot(self) -> dict[str, Any]:
        """Capture current UI state for diagnostics."""
        if self.app is None:
            return {"state": "not_started"}
        try:
            log_content = self.app.log_text.get("1.0", tk.END).strip()
        except tk.TclError:
            log_content = "<destroyed>"
        try:
            progress_val = self.app.progressbar.cget("value")
        except tk.TclError:
            progress_val = None
        return {
            "status": self.app.status_var.get(),
            "batch": self.app.batch_var.get(),
            "metrics": self.app.metrics_var.get(),
            "progress": progress_val,
            "is_running": self.app.is_running,
            "is_previewing": self.app.is_previewing,
            "service": self.app.service_label_var.get(),
            "log_tail": log_content.splitlines()[-15:] if log_content else [],
        }

    def pump_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float | None = None,
        description: str = "condition",
    ) -> None:
        """Pump the Tk event loop until `predicate()` returns True or `timeout` expires."""
        if self.root is None:
            raise RuntimeError("GuiHarness has not been started")
        effective_timeout = timeout or self.config.timeout_seconds
        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                self.diagnostics.record_gui_snapshot(self.snapshot())
                return
            time.sleep(0.02)
        self.diagnostics.record_gui_snapshot(self.snapshot())
        raise TimeoutError(
            f"GUI timed out after {effective_timeout:.1f}s waiting for {description}: "
            f"{self.snapshot()}"
        )

    def close(self) -> None:
        """Close the GUI cleanly, persisting config and stopping background workers."""
        if self._closed:
            return
        self._closed = True
        if self.app is not None:
            self.diagnostics.record_gui_snapshot(self.snapshot())
            try:
                if self.app._preview_dialog is not None and self.app._preview_dialog.winfo_exists():
                    self.app._preview_dialog.destroy()
            except tk.TclError:
                pass
            try:
                self.app._on_close(force=True)
            except tk.TclError:
                pass
            if self.app.controller.worker is not None and self.app.controller.worker.is_alive():
                self.app.controller.worker.join(timeout=3.0)
            self.app = None
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            self.root = None

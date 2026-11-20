import os
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.utils import concurrency
from audiobookdl.utils.concurrency import (
    _terminate_process,
    run_cancellable_subprocess,
)


def test_run_cancellable_subprocess_timeout_terminates_child():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_cancellable_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.3,
            poll_interval=0.05,
        )
    assert time.monotonic() - start < 5.0


def test_run_cancellable_subprocess_capture_stdout():
    result = run_cancellable_subprocess(
        [sys.executable, "-c", "print('hello-stdout')"],
        capture_stdout=True,
    )
    assert result.returncode == 0
    assert b"hello-stdout" in result.stdout


def test_ffmpeg_spawn_semaphore_bounds_concurrency(tmp_path, monkeypatch):
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    shutil.copy(sys.executable, ffmpeg_exe)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(concurrency, "_FFMPEG_SPAWN_SEMAPHORE", sem)

    # Hold the only slot: the ffmpeg spawn must queue until it is released.
    assert sem.acquire(timeout=1)
    finished = threading.Event()
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            run_cancellable_subprocess(
                [str(ffmpeg_exe), "-c", "import time; time.sleep(0.4)"],
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert not finished.is_set()
    sem.release()
    assert finished.wait(timeout=5.0)
    thread.join(timeout=5.0)
    assert not errors


def test_ffmpeg_spawn_queue_honors_cancel_event(tmp_path, monkeypatch):
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    shutil.copy(sys.executable, ffmpeg_exe)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(concurrency, "_FFMPEG_SPAWN_SEMAPHORE", sem)
    assert sem.acquire(timeout=1)

    cancel_event = threading.Event()
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            run_cancellable_subprocess(
                [str(ffmpeg_exe), "-c", "import time; time.sleep(5)"],
                cancel_event=cancel_event,
                poll_interval=0.05,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    time.sleep(0.2)
    cancel_event.set()
    thread.join(timeout=5.0)
    sem.release()
    assert len(errors) == 1
    assert isinstance(errors[0], DownloadCancelled)


def test_non_ffmpeg_command_bypasses_spawn_semaphore(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(concurrency, "_acquire_ffmpeg_slot", spy)
    result = run_cancellable_subprocess(
        [sys.executable, "-c", "pass"],
    )
    assert result.returncode == 0
    spy.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="taskkill path is Windows-only")
def test_terminate_process_uses_taskkill_tree_kill(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(concurrency.subprocess, "run", fake_run)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _terminate_process(proc, timeout=5.0)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.poll() is not None
    assert any(cmd[:2] == ["taskkill", "/F"] and "/T" in cmd for cmd in calls)


def test_probe_audio_info_timeout_returns_probe_error(monkeypatch):
    from audiobookdl.utils import probe_audio_info

    def _hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 30.0)

    monkeypatch.setattr("audiobookdl.utils.find_executable", lambda _name: "ffprobe")
    monkeypatch.setattr(
        "audiobookdl.utils.concurrency.run_cancellable_subprocess", _hang
    )
    info = probe_audio_info("missing.mp3", timeout=0.01)
    assert info.probe_error is not None
    assert "timed out" in info.probe_error

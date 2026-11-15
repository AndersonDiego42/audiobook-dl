"""End-to-end tests for the desktop Tkinter GUI and `--self-test` entry point."""

from __future__ import annotations

import json
import tkinter as tk

from mutagen.mp4 import MP4
import pytest

from audiobookdl.config import load_config
from audiobookdl.utils import ffprobe_duration
from tests.e2e.fixtures import MediaFixtureFactory, build_librivox_html
from tests.e2e.harness import CliRunner, E2EWorkspace, GuiHarness
from tests.e2e.server import LocalAudiobookServer


pytestmark = [pytest.mark.e2e, pytest.mark.gui]


def test_gui_self_test_subprocess(cli_runner: CliRunner) -> None:
    res = cli_runner.run_gui_self_test()
    assert res.returncode == 0, res.stderr
    assert not res.timed_out


@pytest.mark.ffmpeg
def test_gui_preview_and_download_m4b_end_to_end(
    gui_harness: GuiHarness,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = ffmpeg_tools
    part1 = media_factory.make_mp3("gui_p1.mp3", duration_sec=1.0, freq_hz=440)
    part2 = media_factory.make_mp3("gui_p2.mp3", duration_sec=1.0, freq_hz=550)
    cover = media_factory.make_jpeg()

    book_url = "http://librivox.org/books/gui-e2e-book"
    e2e_server.add_route(
        "/books/gui-e2e-book",
        build_librivox_html(
            title="GUI E2E Audiobook",
            cover_url="http://librivox.org/assets/gui-cover.jpg",
            parts=[
                ("Chapter 1", "http://librivox.org/audio/gui1.mp3"),
                ("Chapter 2", "http://librivox.org/audio/gui2.mp3"),
            ],
        ),
        content_type="text/html; charset=utf-8",
        host="librivox.org",
    )
    e2e_server.add_route("/assets/gui-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")
    e2e_server.add_route("/audio/gui1.mp3", part1, content_type="audio/mpeg", host="librivox.org")
    e2e_server.add_route("/audio/gui2.mp3", part2, content_type="audio/mpeg", host="librivox.org")

    monkeypatch.setenv("HTTP_PROXY", e2e_server.url)
    monkeypatch.setenv("http_proxy", e2e_server.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    app = gui_harness.start()
    app.urls_text.insert("1.0", f"{book_url}\n")
    app._on_urls_changed()
    assert app.service_label_var.get() == "Librivox"

    # 1. Preview metadata through real background fetch + Tkinter dialog
    app.preview_metadata()
    gui_harness.pump_until(
        lambda: not app.is_previewing and app._preview_dialog is not None,
        description="preview dialog to open",
    )
    assert app._preview_dialog is not None
    assert "GUI E2E Audiobook" in app._preview_dialog.title()
    assert hasattr(app._preview_dialog, "_cover_photo")
    app._preview_dialog.destroy()

    # 2. Configure .m4b + JSON sidecar and run full download via real worker thread
    app.m4b_var.set(True)
    app._on_m4b_toggled()
    app.write_json_var.set(True)
    app.start_download()
    assert app.is_running

    gui_harness.pump_until(
        lambda: not app.is_running,
        description="GUI download completion",
    )

    assert app.status_var.get() == "Completed"
    assert float(app.progressbar.cget("value")) == pytest.approx(100.0)
    log_text = app.log_text.get("1.0", tk.END)
    assert "GUI E2E Audiobook" in log_text
    assert "--- Completed ---" in log_text

    out_m4b = e2e_workspace.output_dir / "GUI E2E Audiobook.m4b"
    out_json = e2e_workspace.output_dir / "GUI E2E Audiobook.m4b.json"
    assert out_m4b.is_file()
    assert out_json.is_file()
    assert json.loads(out_json.read_text(encoding="utf-8"))["title"] == "GUI E2E Audiobook"
    assert 1.8 <= ffprobe_duration(str(out_m4b)) <= 2.3
    mp4 = MP4(str(out_m4b))
    assert mp4.tags is not None
    assert mp4.tags["\xa9nam"] == ["GUI E2E Audiobook"]

    # 3. Verify preferences persist on close
    gui_harness.close()
    reloaded = load_config(str(e2e_workspace.config_path))
    assert reloaded.gui.output_dir == str(e2e_workspace.output_dir)
    assert reloaded.gui.output_template == "{title}"

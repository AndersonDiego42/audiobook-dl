import io
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from audiobookdl import args, logging, utils
from audiobookdl.pipeline import DownloadSummary, fetch_metadata, run_downloads
from audiobookdl.config import Config, load_config, save_source_credentials
from audiobookdl.exceptions import BookNotFound, DownloadCancelled
from audiobookdl.gui.app import (
    AudiobookDLApp,
    parse_gui_args,
    run_self_test,
)
from audiobookdl.gui.controller import GuiInteractionHandler, QueuedJob
from audiobookdl.gui.formatting import (
    describe_series_book,
    format_audiobook_preview,
    format_duration,
    format_failed_job_detail,
    format_speed,
    is_valid_geometry,
    parse_url_text,
)
from audiobookdl.output.download import setup_download_dir
from audiobookdl.sources import detect_service_key, get_available_services
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookFile,
    AudiobookMetadata,
    BookId,
    Chapter,
    Cover,
    Series,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.starts: list[tuple[str, float]] = []
        self.advances: list[tuple[float, float]] = []
        self.finished = False

    def on_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        self.starts.append((description, total))

    def on_progress_advance(self, completed: float, total: float) -> None:
        self.advances.append((completed, total))

    def on_progress_finish(self) -> None:
        self.finished = True


class _FakeInteraction:
    def __init__(self, input_value: str = "user", confirm_value: bool = True) -> None:
        self.input_value = input_value
        self.confirm_value = confirm_value
        self.prompts: list[tuple[str, bool]] = []
        self.confirms: list[str] = []

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        self.prompts.append((prompt, hidden))
        return self.input_value

    def confirm(self, message: str) -> bool:
        self.confirms.append(message)
        return self.confirm_value

    def choose_index(
        self,
        title: str,
        choices: Any,
        default: int | None = None,
    ) -> int | None:
        return default


def test_build_default_options_matches_cli_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audiobook-dl"])
    cli_opts = vars(args.parse_arguments())
    default_opts = vars(args.build_default_options())
    assert default_opts == cli_opts


def test_logging_ui_sink_and_progress():
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(debug=True, quiet=False, sink=sink)):
        logging.log("Downloading [blue]Example Book[/]")
        logging.debug("Debug [bold]line[/]")
        with logging.progress([]) as progress:
            task_id = progress.add_task("Downloading [blue]Example[/]", total=4)
            progress.advance(task_id, 1.5)
            assert progress.tasks[task_id].remaining == pytest.approx(2.5)
            progress.advance(task_id, 2.5)
            assert progress.tasks[task_id].remaining == pytest.approx(0.0)

    assert ("INFO", "Downloading Example Book") in sink.logs
    assert ("DEBUG", "Debug line") in sink.logs
    assert sink.starts == [("Downloading Example", 4.0)]
    assert sink.advances == [(1.5, 4.0), (4.0, 4.0)]
    assert sink.finished is True


def test_find_executable_prefers_meipass_then_exe_dir(tmp_path, monkeypatch):
    meipass_dir = tmp_path / "meipass"
    exe_dir = tmp_path / "exedir"
    meipass_dir.mkdir()
    exe_dir.mkdir()

    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    in_exe_dir = exe_dir / exe_name
    in_exe_dir.write_text("exe")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "audiobook-dl.exe"))

    # Falls back to executable dir when not in _MEIPASS
    found_in_exe = utils.find_executable("ffmpeg")
    assert found_in_exe is not None
    assert Path(found_in_exe) == in_exe_dir

    # Prefers _MEIPASS when present
    in_meipass = meipass_dir / exe_name
    in_meipass.write_text("meipass")
    found_in_meipass = utils.find_executable("ffmpeg")
    assert found_in_meipass is not None
    assert Path(found_in_meipass) == in_meipass


def test_setup_download_dir_decline_does_not_delete_existing(tmp_path):
    existing = tmp_path / "Existing Book"
    existing.mkdir()
    marker = existing / "keep.mp3"
    marker.write_text("audio")

    interaction = _FakeInteraction(confirm_value=False)
    with pytest.raises(DownloadCancelled):
        setup_download_dir(str(existing), interaction=interaction)

    assert marker.exists()
    assert len(interaction.confirms) == 1


def test_run_downloads_stops_on_cancel_event():
    cancel_event = threading.Event()
    cancel_event.set()
    options = args.build_default_options()
    config = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)

    summary = run_downloads(
        ["https://example.com/book"],
        options,
        config,
        cancel_event=cancel_event,
    )
    assert summary.cancelled is True
    assert summary.completed == 0


def test_run_downloads_records_error_summary():
    options = args.build_default_options()
    config = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    with patch("audiobookdl.pipeline.process_url", side_effect=BookNotFound()):
        summary = run_downloads(["https://example.com/missing"], options, config)
    assert summary == DownloadSummary(completed=0, failed=1, cancelled=False)


def test_parse_url_text():
    raw = "  https://example.com/a \n\n https://example.com/b\thttps://example.com/c  "
    assert parse_url_text(raw) == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]


def test_gui_app_smoke_and_state_transitions(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://example.com/book1\n")
        app.output_format_var.set("m4b")
        app.combine_var.set(True)

        opts = app.build_options()
        assert opts.urls == ["https://example.com/book1"]
        assert opts.output_format == "m4b"
        assert opts.combine is True
        assert opts.output_template == os.path.join(str(tmp_path), "{title}")

        # Verify state transitions
        app._set_running_state(True)
        assert app.is_running
        app._finish_run(DownloadSummary(completed=1, failed=0, cancelled=False), None)
        assert not app.is_running
        assert app.status_var.get() == "Completed"

        app._finish_run(DownloadSummary(completed=0, failed=0, cancelled=True), None)
        assert app.status_var.get() == "Cancelled"
        root.update_idletasks()
    finally:
        root.destroy()


def test_get_available_services_and_detect_service_key():
    services = get_available_services()
    keys = {s.key for s in services}
    assert {"storytel", "nextory", "yourcloudlibrary", "librivox", "audiobooks.com"} <= keys
    assert detect_service_key("https://www.storytel.com/no/nn/books/something-12345") == "storytel"
    assert detect_service_key("https://unknown.example.org/book") is None


def test_save_and_load_source_credentials_round_trip(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('output_template = "{author}/{title}"\nskip_downloaded = true\n', encoding="utf-8")

    tricky_pw = 'p@ss"w\\ord\'\t$123'
    cookie_abs = str(tmp_path / "cookies.txt")
    save_source_credentials(
        "storytel",
        username="reader@example.com",
        password=tricky_pw,
        overwrite=str(cfg_path),
    )
    save_source_credentials(
        "audiobooks.com",
        cookie_file=cookie_abs,
        overwrite=str(cfg_path),
    )

    loaded = load_config(str(cfg_path))
    assert loaded.output_template == "{author}/{title}"
    assert loaded.skip_downloaded is True
    assert loaded.sources["storytel"].username == "reader@example.com"
    assert loaded.sources["storytel"].password == tricky_pw
    assert loaded.sources["audiobooks.com"].cookie_file == cookie_abs

    # Clearing one source leaves the other intact
    save_source_credentials("storytel", overwrite=str(cfg_path))
    reloaded = load_config(str(cfg_path))
    assert "storytel" not in reloaded.sources
    assert reloaded.sources["audiobooks.com"].cookie_file == cookie_abs


def test_gui_service_dropdown_saves_and_restores_credentials(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        dropdown_values = list(app.service_combo["values"])
        assert "Storytel / Mofibo" in dropdown_values
        assert "YourCloudLibrary" in dropdown_values

        # Select Storytel, enter login & password, save
        app.service_label_var.set("Storytel / Mofibo")
        app._auth_panel.on_service_selected()
        app.username_var.set("alice@example.com")
        app.password_var.set("hunter2!")
        app.save_current_credentials()
        assert app.saved_badge_var.get() == "Saved"

        # Switch to Librivox (no auth), then back via URL auto-detection
        app.service_label_var.set("Librivox")
        app._auth_panel.on_service_selected()
        assert app.username_var.get() == ""
        assert str(app.user_entry.cget("state")) == "disabled"

        app.urls_text.insert("1.0", "https://www.storytel.com/no/nn/books/something-12345")
        app._on_urls_changed()
        assert app.service_label_var.get() == "Storytel / Mofibo"
        assert app.username_var.get() == "alice@example.com"
        assert app.password_var.get() == "hunter2!"

        # Re-opening app restores saved credentials from disk
        root.destroy()
        root = tk.Tk()
        root.withdraw()
        app2 = AudiobookDLApp(root, config_location=str(cfg_path))
        app2.service_label_var.set("Storytel / Mofibo")
        app2._auth_panel.on_service_selected()
        assert app2.username_var.get() == "alice@example.com"
        assert app2.password_var.get() == "hunter2!"

        app2.forget_current_credentials()
        assert app2.username_var.get() == ""
        assert app2.password_var.get() == ""
        assert app2.saved_badge_var.get() == ""
    finally:
        root.destroy()


def test_gui_m4b_option_sync_and_persistence(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.m4b_var.get() is False
        assert app.output_format_var.get() == "Original"

        app.combine_var.set(False)
        app.m4b_var.set(True)
        app._on_m4b_toggled()
        assert app.output_format_var.get() == "m4b"
        assert app.combine_var.get() is True

        opts = app.build_options()
        assert opts.output_format == "m4b"
        assert opts.combine is True

        # Selecting another format clears the m4b checkbox
        app.output_format_var.set("mp3")
        app._on_format_selected()
        assert app.m4b_var.get() is False

        # Selecting m4b via dropdown checks the m4b checkbox and enables combine
        app.combine_var.set(False)
        app.output_format_var.set("m4b")
        app._on_format_selected()
        assert app.m4b_var.get() is True
        assert app.combine_var.get() is True

        # Persist output preferences and verify reload
        from audiobookdl.config import save_output_preferences
        save_output_preferences("m4b", True, config_location=str(cfg_path))

        root.destroy()
        root = tk.Tk()
        root.withdraw()
        app2 = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app2.m4b_var.get() is True
        assert app2.output_format_var.get() == "m4b"
        assert app2.combine_var.get() is True
    finally:
        root.destroy()


def test_gui_app_end_to_end_worker_and_prompts(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://example.com/book1\n")

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.app.simpledialog.askstring", lambda *a, **kw: "secret-user")
        monkeypatch.setattr("audiobookdl.gui.app.messagebox.askyesno", lambda *a, **kw: True)

        observed = {}

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None):
            with logging.job_context(logging.JobContext(sink=sink)):
                logging.log("Downloading [blue]Test Book[/]")
                with logging.progress([]) as progress:
                    task_id = progress.add_task("Downloading [blue]Test Book[/]", total=2)
                    progress.advance(task_id, 1)
                    progress.advance(task_id, 1)
                assert interaction is not None
                observed["asked"] = interaction.ask_input("Username")
                observed["confirmed"] = interaction.confirm("Override?")
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        app.start_download()
        assert app.is_running

        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert not app.is_running
        assert app.status_var.get() == "Completed"
        assert observed == {"asked": "secret-user", "confirmed": True}
        log_content = app.log_text.get("1.0", tk.END)
        assert "Downloading Test Book" in log_content
        assert "--- Completed ---" in log_content
    finally:
        root.destroy()


def test_sink_progress_adapter_throttles_by_delta_time_and_completion(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("audiobookdl.logging.time.monotonic", lambda: clock[0])

    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        with logging.progress([]) as progress:
            task_id = progress.add_task("Downloading", total=1000.0)
            # Four tiny advances (0.1% each = 0.4% total, 0s elapsed) -> throttled
            for _ in range(4):
                progress.advance(task_id, 1.0)
            assert sink.advances == []

            # Fifth 0.1% advance crosses 0.5% delta -> emits
            progress.advance(task_id, 1.0)
            assert sink.advances == [(5.0, 1000.0)]

            # Tiny advance < 0.5%, but 60ms elapsed -> emits
            clock[0] += 0.06
            progress.advance(task_id, 1.0)
            assert sink.advances == [(5.0, 1000.0), (6.0, 1000.0)]

            # Jump near end, then tiny 1.0 advance to reach completion -> emits 1000.0
            progress.advance(task_id, 993.0)
            progress.advance(task_id, 1.0)
            assert sink.advances[-1] == (1000.0, 1000.0)

            # Redundant zero advance at completion does not re-emit
            count_at_completion = len(sink.advances)
            progress.advance(task_id, 0.0)
            assert len(sink.advances) == count_at_completion


def test_gui_poll_queue_bounds_batch_and_coalesces_progress(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        handled: list[tuple] = []
        original_handle = app._handle_event

        def recording_handle(item: tuple) -> None:
            handled.append(item)
            original_handle(item)

        app._handle_event = recording_handle  # type: ignore[method-assign]

        # Queue 250 items: burst of progress_advance, a log in between, and more progress_advance
        for i in range(1, 151):
            app.controller.event_queue.put(("progress_advance", float(i), 300.0))
        app.controller.event_queue.put(("log", "INFO", "Midway"))
        for i in range(151, 250):
            app.controller.event_queue.put(("progress_advance", float(i), 300.0))

        # First tick drains up to MAX_QUEUE_BATCH (200) items and coalesces consecutive advances
        app._poll_queue()
        assert handled == [
            ("progress_advance", 150.0, 300.0),
            ("log", "INFO", "Midway"),
            ("progress_advance", 199.0, 300.0),
        ]
        assert app.progressbar.cget("value") == pytest.approx(199.0 / 300.0 * 100.0)

        # Second tick drains remaining 50 items, coalesced into one progress_advance
        handled.clear()
        app._poll_queue()
        assert handled == [("progress_advance", 249.0, 300.0)]
        assert app.progressbar.cget("value") == pytest.approx(249.0 / 300.0 * 100.0)
    finally:
        root.destroy()


def test_run_self_test_and_ffmpeg_guidance(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        run_self_test(check_gui=False)
        pytest.skip(f"Tk display unavailable: {exc}")

    run_self_test(check_gui=True)

    root.withdraw()
    try:
        shown = []
        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda _name: None)
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showinfo",
            lambda title, message, parent=None: shown.append((title, message)),
        )
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        assert "Not found" in app.ffmpeg_status_var.get()
        app._ffmpeg_assistant.show_guidance()
        assert len(shown) == 1
        assert "Setup Guide" in shown[0][0]
        assert "ffmpeg.exe" in shown[0][1]
    finally:
        root.destroy()


def test_format_speed_duration_and_geometry_validation():
    assert format_speed(0) == "--"
    assert format_speed(512) == "512 B/s"
    assert format_speed(2048) == "2.0 KB/s"
    assert format_speed(2.5 * 1024 * 1024) == "2.5 MB/s"

    assert format_duration(None) == "--"
    assert format_duration(-1) == "--"
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "1:01:01"

    assert is_valid_geometry("760x680") is True
    assert is_valid_geometry("920x740+100-50") is True
    assert is_valid_geometry("1x1+0+0") is False
    assert is_valid_geometry("not-a-geometry") is False


def test_gui_persists_and_restores_geometry_folder_and_template(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    custom_dir = str(tmp_path / "MyAudiobooks")
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.output_dir_var.set(custom_dir)
        app.template_var.set("{author}/{title}")
        root.geometry("880x710")
        root.update_idletasks()
        app._on_close()
    except tk.TclError:
        root.destroy()
        raise

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        root2.update_idletasks()
        assert app2.output_dir_var.get() == custom_dir
        assert app2.template_var.get() == "{author}/{title}"
        assert app2.controller.config.gui.window_geometry is not None
        assert app2.controller.config.gui.window_geometry.startswith("880x710")
    finally:
        root2.destroy()


def test_gui_add_to_queue_while_running_and_batch_progress(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://example.com/book1\n")

        first_started = threading.Event()
        release_first = threading.Event()
        processed_urls: list[str] = []

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None):
            url = urls[0]
            processed_urls.append(url)
            with logging.job_context(logging.JobContext(sink=sink)):
                if url.endswith("book1"):
                    first_started.set()
                    assert release_first.wait(timeout=3.0)
                    logging.emit_item_progress(1, 1, "Book One")
                else:
                    logging.emit_item_progress(2, 5, "Series Book Two")
                with logging.progress([]) as progress:
                    task_id = progress.add_task(f"Downloading {url}", total=2.0)
                    progress.advance(task_id, 2.0)
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        app.start_download()
        assert app.is_running
        assert str(app.download_btn.cget("text")) == "Add to Queue"
        assert first_started.wait(timeout=2.0)

        # Pump queue so batch_item for Item 1 / 1 is rendered
        for _ in range(10):
            root.update()
            if app.batch_var.get():
                break
            threading.Event().wait(0.02)
        assert "Item 1 / 1" in app.batch_var.get()

        # Queue a second URL while the first is still running (and keep book1 in text to verify dedup)
        app.urls_text.insert(tk.END, "https://example.com/book2\n")
        app.start_download()
        assert "Item 1 / 2" in app.batch_var.get()

        release_first.set()
        for _ in range(120):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert not app.is_running
        assert str(app.download_btn.cget("text")) == "Download"
        assert app.status_var.get() == "Completed"
        assert processed_urls == [
            "https://example.com/book1",
            "https://example.com/book2",
        ]
        log_content = app.log_text.get("1.0", tk.END)
        assert "Added 1 URL to queue (total 2)" in log_content
    finally:
        root.destroy()


def test_gui_paste_urls_from_clipboard(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        root.clipboard_clear()
        root.clipboard_append(
            "https://www.storytel.com/no/nn/books/something-12345\n"
            "https://example.com/book2\n"
        )
        root.update()

        added = app._paste_urls_from_clipboard()
        assert added == [
            "https://www.storytel.com/no/nn/books/something-12345",
            "https://example.com/book2",
        ]
        assert app.service_label_var.get() == "Storytel / Mofibo"

        # Pasting same clipboard again deduplicates
        added_again = app._paste_urls_from_clipboard()
        assert added_again == []
    finally:
        root.destroy()


def test_sink_progress_adapter_emits_speed_and_eta_metrics(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("audiobookdl.logging.time.monotonic", lambda: clock[0])

    class _MetricsSink(_RecordingSink):
        def __init__(self) -> None:
            super().__init__()
            self.metrics: list[tuple] = []

        def on_progress_metrics(
            self,
            completed: float,
            total: float,
            bytes_downloaded: int,
            speed_bps: float,
            elapsed_seconds: float,
            eta_seconds,
        ) -> None:
            self.metrics.append((
                completed,
                total,
                bytes_downloaded,
                speed_bps,
                elapsed_seconds,
                eta_seconds,
            ))

    sink = _MetricsSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        with logging.progress([]) as progress:
            task_id = progress.add_task("Downloading", total=4.0)
            clock[0] += 1.0
            progress.record_bytes(task_id, 1024 * 1024)
            progress.advance(task_id, 1.0)

            assert len(sink.metrics) >= 1
            completed, total, total_bytes, speed_bps, elapsed, eta = sink.metrics[-1]
            assert completed == pytest.approx(1.0)
            assert total == pytest.approx(4.0)
            assert total_bytes == 1024 * 1024
            assert speed_bps > 0
            assert elapsed == pytest.approx(1.0)
            assert eta == pytest.approx(3.0)


def _make_sample_png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=(30, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def test_format_audiobook_preview_and_describe_series_book():
    meta = AudiobookMetadata(
        title="Dune",
        series="Dune Saga",
        series_order=1,
        authors=["Frank Herbert"],
        narrators=["Scott Brick"],
        genres=["Science Fiction"],
        publisher="Macmillan Audio",
    )
    book = Audiobook(
        session=requests.Session(),
        metadata=meta,
        files=[
            AudiobookFile(url="https://example.com/1.m4a", ext="m4a"),
            AudiobookFile(url="https://example.com/2.m4a", ext="m4a"),
        ],
        chapters=[Chapter(start=0, title="Ch 1"), Chapter(start=1000, title="Ch 2")],
    )
    rows = dict(format_audiobook_preview(book))
    assert rows["Title"] == "Dune"
    assert rows["Author(s)"] == "Frank Herbert"
    assert rows["Narrator(s)"] == "Scott Brick"
    assert rows["Series"] == "Dune Saga #1"
    assert rows["Parts"] == "2 files (m4a)"
    assert rows["Chapters"] == "2 chapters"

    assert describe_series_book(0, book) == "Dune — Frank Herbert"
    assert describe_series_book(1, BookId(id="id-2", title="Dune Messiah")) == "Dune Messiah"
    assert describe_series_book(2, BookId(id={"title": "Episode 3"})) == "Episode 3"
    assert describe_series_book(3, BookId(id="abc-4")) == "Book 4 (ID: abc-4)"


def test_fetch_metadata_and_selective_series_download(monkeypatch):
    meta_a = AudiobookMetadata(title="Book A")
    meta_b = AudiobookMetadata(title="Book B")
    meta_c = AudiobookMetadata(title="Book C")
    book_a = Audiobook(session=requests.Session(), metadata=meta_a, files=[])
    book_b = Audiobook(session=requests.Session(), metadata=meta_b, files=[])
    book_c = Audiobook(session=requests.Session(), metadata=meta_c, files=[])
    series = Series(title="Trilogy", books=[BookId("a"), BookId("b"), BookId("c")])

    downloaded_titles: list[str] = []
    source_download_calls: list[str] = []

    class _FakeSource:
        name = "FakeSource"
        requires_authentication = False
        authenticated = True

        def __init__(self, _options) -> None:
            pass

        def download(self, url: str):
            source_download_calls.append(url)
            return series

        def download_from_id(self, book_id: str) -> Audiobook:
            return {"a": book_a, "b": book_b, "c": book_c}[book_id]

        def on_download_complete(self, _audiobook: Audiobook) -> None:
            pass

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _FakeSource)
    monkeypatch.setattr(
        "audiobookdl.pipeline.process_audiobook",
        lambda _src, ab, _opts, **_kw: downloaded_titles.append(ab.title),
    )

    options = args.build_default_options()
    config = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)

    fetched = fetch_metadata("https://example.com/series", options, config)
    assert isinstance(fetched, Series)
    assert downloaded_titles == []
    assert source_download_calls == ["https://example.com/series"]

    # Selective download reusing prefetched source + result does not re-call source.download
    summary = run_downloads(
        ["https://example.com/series"],
        options,
        config,
        selected_indices=[0, 2],
        prefetched=(_FakeSource(options), series),  # type: ignore[arg-type]
    )
    assert summary == DownloadSummary(completed=1, failed=0, cancelled=False)
    assert downloaded_titles == ["Book A", "Book C"]
    assert len(source_download_calls) == 1

    # Empty selection finishes cleanly with zero downloads
    downloaded_titles.clear()
    summary_empty = run_downloads(
        ["https://example.com/series"],
        options,
        config,
        selected_indices=[],
        prefetched=(_FakeSource(options), series),  # type: ignore[arg-type]
    )
    assert summary_empty == DownloadSummary(completed=1, failed=0, cancelled=False)
    assert downloaded_titles == []


def test_gui_preview_audiobook_with_cover_and_series_checklist(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))

        audiobook = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(
                title="Sample Audio",
                authors=["Jane Author"],
                description="A great listen.",
            ),
            files=[AudiobookFile(url="https://example.com/part1.mp3", ext="mp3")],
            chapters=[Chapter(start=0, title="Intro")],
            cover=Cover(image=_make_sample_png_bytes(), extension="png"),
        )
        fake_source = SimpleNamespace(name="FakeSource")

        app.urls_text.insert("1.0", "https://example.com/book1\n")
        monkeypatch.setattr(
            "audiobookdl.gui.controller.prepare_source_result",
            lambda _u, _o, _c, **_kw: (fake_source, audiobook),
        )

        app.preview_metadata()
        for _ in range(100):
            root.update()
            if not app.is_previewing and app._preview_dialog is not None:
                break
            threading.Event().wait(0.02)

        assert not app.is_previewing
        dialog = app._preview_dialog
        assert dialog is not None
        assert dialog.winfo_exists()
        assert "Sample Audio" in dialog.title()
        assert hasattr(dialog, "_cover_photo")
        dialog.destroy()

        # Now preview a Series, uncheck book #2, and confirm selective download
        series = Series(
            title="My Trilogy",
            books=[
                BookId(id="1", title="First Book"),
                BookId(id="2", title="Second Book"),
                BookId(id="3", title="Third Book"),
            ],
        )
        monkeypatch.setattr(
            "audiobookdl.gui.controller.prepare_source_result",
            lambda _u, _o, _c, **_kw: (fake_source, series),
        )
        captured: dict = {}

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None, selected_indices=None, prefetched=None):
            captured["urls"] = list(urls)
            captured["selected_indices"] = selected_indices
            captured["prefetched"] = prefetched
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        app.preview_metadata()
        for _ in range(100):
            root.update()
            if not app.is_previewing and app._preview_dialog is not None:
                break
            threading.Event().wait(0.02)

        series_dialog = app._preview_dialog
        assert series_dialog is not None
        check_vars = series_dialog._check_vars
        assert len(check_vars) == 3
        check_vars[1].set(False)
        selected = [i for i, v in enumerate(check_vars) if v.get()]
        series_dialog.destroy()

        app._enqueue_from_preview("https://example.com/series", (fake_source, series), selected)  # type: ignore[arg-type]
        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert not app.is_running
        assert captured["urls"] == ["https://example.com/series"]
        assert captured["selected_indices"] == [0, 2]
        assert captured["prefetched"][1] is series
    finally:
        root.destroy()


def test_gui_preview_error_displays_error_and_restores_buttons(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.urls_text.insert("1.0", "https://example.com/missing\n")

        errors: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showerror",
            lambda title, msg, parent=None: errors.append((title, msg)),
        )
        monkeypatch.setattr(
            "audiobookdl.gui.controller.prepare_source_result",
            lambda *_a, **_kw: (_ for _ in ()).throw(BookNotFound()),
        )

        app.preview_metadata()
        for _ in range(100):
            root.update()
            if not app.is_previewing:
                break
            threading.Event().wait(0.02)

        assert not app.is_previewing
        assert app.status_var.get() == "Preview failed"
        assert len(errors) == 1
        assert str(app.preview_btn.cget("state")) == "normal"
        assert str(app.download_btn.cget("state")) == "normal"
    finally:
        root.destroy()


def test_gui_ffmpeg_locate_persists_and_updates_status(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    ffmpeg_root = tmp_path / "ffmpeg-bundle"
    bin_dir = ffmpeg_root / "bin"
    bin_dir.mkdir(parents=True)
    exe_suffix = ".exe" if os.name == "nt" else ""
    (bin_dir / f"ffmpeg{exe_suffix}").write_text("stub")
    (bin_dir / f"ffprobe{exe_suffix}").write_text("stub")

    monkeypatch.setattr("audiobookdl.utils.shutil.which", lambda _name: None)
    monkeypatch.setattr("audiobookdl.utils.validate_executable", lambda _path: None)
    monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.validate_executable", lambda _path: None)
    monkeypatch.setattr("audiobookdl.gui.app.filedialog.askdirectory", lambda **_kw: str(ffmpeg_root))

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert "Not found" in app.ffmpeg_status_var.get()

        assert app.browse_ffmpeg_dir()
        assert app.ffmpeg_status_var.get() == "FFmpeg: Ready"
        assert load_config(str(cfg_path)).ffmpeg_dir == os.path.abspath(str(bin_dir))
    finally:
        root.destroy()

    # Re-opening app restores ffmpeg_dir from audiobook-dl.toml
    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.ffmpeg_status_var.get() == "FFmpeg: Ready"
    finally:
        root2.destroy()


def test_gui_start_download_validates_ffmpeg_upfront(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    monkeypatch.setattr("audiobookdl.utils.shutil.which", lambda _name: None)

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://example.com/book1\n")
        app.combine_var.set(True)

        guidance_calls: list[str] = []
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showinfo",
            lambda title, _msg, parent=None: guidance_calls.append(title),
        )
        # Cancel in prompt aborts before download starts and opens guidance
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.askyesnocancel",
            lambda *_a, **_kw: None,
        )
        app.start_download()
        assert not app.is_running
        assert guidance_calls == ["FFmpeg Setup Guide"]

        # Yes in prompt lets user pick FFmpeg dir and proceeds
        ffmpeg_dir = tmp_path / "ff"
        ffmpeg_dir.mkdir()
        exe_suffix = ".exe" if os.name == "nt" else ""
        (ffmpeg_dir / f"ffmpeg{exe_suffix}").write_text("stub")
        (ffmpeg_dir / f"ffprobe{exe_suffix}").write_text("stub")

        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.askyesnocancel",
            lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "audiobookdl.gui.app.filedialog.askdirectory",
            lambda **_kw: str(ffmpeg_dir),
        )
        monkeypatch.setattr("audiobookdl.utils.validate_executable", lambda _path: None)
        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.validate_executable", lambda _path: None)
        monkeypatch.setattr(
            "audiobookdl.gui.controller.run_downloads",
            lambda *_a, **_kw: DownloadSummary(completed=1, failed=0, cancelled=False),
        )

        app.start_download()
        assert app.is_running
        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)
        assert not app.is_running
        assert app.status_var.get() == "Completed"
    finally:
        root.destroy()


def test_gui_rejects_unrunnable_ffmpeg_and_resets_to_path(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    bad_dir = tmp_path / "bad-ffmpeg" / "bin"
    bad_dir.mkdir(parents=True)
    exe_suffix = ".exe" if os.name == "nt" else ""
    (bad_dir / f"ffmpeg{exe_suffix}").write_text("not-an-executable")
    (bad_dir / f"ffprobe{exe_suffix}").write_text("not-an-executable")

    # Pre-seed config with an unrunnable ffmpeg_dir (e.g., wrong architecture build)
    cfg_path.write_text(
        f'ffmpeg_dir = "{str(bad_dir).replace(os.sep, "/")}"\n',
        encoding="utf-8",
    )

    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr("audiobookdl.utils.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "audiobookdl.gui.app.messagebox.showwarning",
        lambda title, msg, parent=None: warnings.append((title, msg)),
    )
    monkeypatch.setattr(
        "audiobookdl.gui.app.filedialog.askdirectory",
        lambda **_kw: str(bad_dir.parent),
    )

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert "Unusable" in app.ffmpeg_status_var.get()

        # Selecting an unrunnable binary is rejected and keeps config unchanged
        assert app.browse_ffmpeg_dir() is False
        assert warnings and warnings[-1][0] == "Invalid FFmpeg Binary"

        # Resetting clears ffmpeg_dir and updates status
        app.reset_ffmpeg_dir()
        assert load_config(str(cfg_path)).ffmpeg_dir is None
        assert "Not found" in app.ffmpeg_status_var.get()
    finally:
        root.destroy()


def test_gui_resume_and_m4b_bitrate_sync_and_persistence(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.resume_var.get() is False
        assert app.audio_bitrate_var.get() == "Auto"
        default_opts = app.build_options()
        assert default_opts.resume is False
        assert default_opts.audio_bitrate is None

        app.resume_var.set(True)
        app.audio_bitrate_var.set("192k")
        app.m4b_var.set(True)
        app._on_m4b_toggled()

        opts = app.build_options()
        assert opts.resume is True
        assert opts.audio_bitrate == "192k"
        app._save_run_preferences(opts)
    finally:
        root.destroy()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.resume_var.get() is True
        assert app2.audio_bitrate_var.get() == "192k"
        opts2 = app2.build_options()
        assert opts2.resume is True
        assert opts2.audio_bitrate == "192k"
    finally:
        root2.destroy()


def test_gui_log_caps_line_count(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        for i in range(AudiobookDLApp.MAX_LOG_LINES + 50):
            app._queue_panel.append_log(f"line {i}")
        total_lines = int(app.log_text.index("end-1c").split(".")[0])
        assert total_lines <= AudiobookDLApp.MAX_LOG_LINES + 1
        assert "line 0" not in app.log_text.get("1.0", "2.0")
    finally:
        root.destroy()


def test_gui_queue_move_remove_retry_and_clear(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        ctrl = app.controller
        interaction = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
        opts_a = app.build_options(urls_override=["https://librivox.org/book-a"])
        opts_b = app.build_options(urls_override=["https://librivox.org/book-b"])
        job_a = QueuedJob(job_id="job_a", run_id=1, url="https://librivox.org/book-a", options=opts_a, interaction=interaction)
        job_b = QueuedJob(job_id="job_b", run_id=1, url="https://librivox.org/book-b", options=opts_b, interaction=interaction)
        with ctrl.queue_cond:
            ctrl.worker_has_active_run = True
            ctrl.batch_total = 2
            ctrl.run_seen_urls = {job_a.url, job_b.url}
            ctrl.pending_jobs.extend([job_a, job_b])
        app._queue_panel.register_queue_job(job_a)
        app._queue_panel.register_queue_job(job_b)

        # Move job_b up before job_a
        app.queue_tree.selection_set("job_b")
        app._queue_panel.move_selected_item(-1)
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["job_b", "job_a"]
        assert list(app.queue_tree.get_children()) == ["job_b", "job_a"]

        # Remove job_a
        app.queue_tree.selection_set("job_a")
        app._queue_panel.remove_selected_item()
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["job_b"]
        assert list(app.queue_tree.get_children()) == ["job_b"]
        assert ctrl.batch_total == 1

        # Retry failed items and clear history
        ctrl.pending_jobs.clear()
        ctrl.worker_has_active_run = False
        app._queue_panel.update_queue_row("job_b", "Failed", "timeout")

        retried = {}
        monkeypatch.setattr(app._ffmpeg_assistant, "ensure_available", lambda _opts: True)
        monkeypatch.setattr(app, "_save_run_preferences", lambda _opts: None)
        monkeypatch.setattr(app, "_start_new_run", lambda specs: retried.setdefault("urls", [s[0] for s in specs]))
        app._retry_failed_queue_items()
        assert retried["urls"] == ["https://librivox.org/book-b"]

        app._queue_panel.clear_completed_history()
        assert list(app.queue_tree.get_children()) == []
    finally:
        root.destroy()


def test_gui_series_preview_filter_and_shift_range(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))
        app.template_var.set("{title}")
        app.skip_downloaded_var.set(True)
        (tmp_path / "Alpha Book.m4b").write_bytes(b"existing")

        sess = requests.Session()
        books: list[Audiobook | BookId[Any]] = [
            Audiobook(
                session=sess,
                metadata=AudiobookMetadata(title="Alpha Book", authors=["Author"]),
                files=[],
                chapters=[],
            ),
            Audiobook(
                session=sess,
                metadata=AudiobookMetadata(title="Beta Book", authors=["Author"]),
                files=[],
                chapters=[],
            ),
            Audiobook(
                session=sess,
                metadata=AudiobookMetadata(title="Gamma Book", authors=["Author"]),
                files=[],
                chapters=[],
            ),
        ]
        series = Series(title="My Series", books=books)
        fake_source = MagicMock()
        fake_source.name = "storytel"
        dialog = app._show_preview_dialog("https://www.storytel.com/series/1", fake_source, series)
        root.update_idletasks()
        try:
            assert "[Exists]" in dialog._item_labels[0]
            assert "[Exists]" not in dialog._item_labels[1]
            assert dialog._tree is not None
            assert list(dialog._tree.get_children()) == ["0", "1", "2"]

            # Shift+Click range selection from 0 to 2 after unchecking index 0
            dialog._check_vars[0].set(False)
            dialog._toggle_series_item(0, shift_held=False)
            dialog._check_vars[2].set(False)
            dialog._toggle_series_item(2, shift_held=True)
            assert [v.get() for v in dialog._check_vars] == [False, False, False]
            row_values = dialog._tree.item("1", "values")
            assert isinstance(row_values, (tuple, list))
            assert row_values[0] == "[ ]"

            # Filter entry detaches non-matching rows in Treeview cleanly
            dialog._filter_var.set("beta")
            root.update_idletasks()
            assert list(dialog._tree.get_children()) == ["1"]
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_gui_ignores_stale_item_progress_and_warns_mixed_services(tmp_path, monkeypatch):
    parsed = parse_gui_args(["--config", str(tmp_path / "custom.toml")])
    assert parsed.config_location == str(tmp_path / "custom.toml")

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app._queue_panel.current_ui_run_id = 2
        app._handle_event(("item_progress", 1, 3, 5, "Old Run Book"))
        assert app._queue_panel.ui_book_index == 0
        app._handle_event(("item_progress", 2, 1, 4, "Current Run Book"))
        assert app._queue_panel.ui_book_index == 1
        assert app._queue_panel.ui_book_total == 4

        app.service_label_var.set("Storytel")
        app._auth_panel.on_service_selected()
        asked = {}
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.askyesno",
            lambda title, msg, parent=None: asked.setdefault("msg", msg) and False,
        )
        proceed = app._auth_panel.warn_mixed_service_credentials([
            "https://www.storytel.com/books/1",
            "https://www.nextory.com/book/2",
        ])
        assert proceed is False
        assert "Nextory" in asked["msg"]
    finally:
        root.destroy()


def test_gui_template_preset_and_live_preview(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))
        app.template_preset_var.set("Audiobookshelf")
        app._on_preset_selected()
        assert "series_order_padded" in app.template_var.get()
        preview = app.template_preview_var.get().replace("\\", "/")
        assert "Frank Herbert/Dune Chronicles/Book 01 - Dune" in preview

        app.template_var.set("{author} - {title}")
        assert app.template_preset_var.get() == "Custom"
    finally:
        root.destroy()


def test_gui_archive_ledger_dialog_filters_and_populates(tmp_path):
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        archive_path = tmp_path / "downloads.db"
        app.controller.config.download_archive = str(archive_path)
        ledger = DownloadArchive(str(archive_path))
        ledger.record("storytel", "k1", title="First Audiobook", author="Alice")
        ledger.record("librivox", "k2", title="Second Audiobook", author="Bob")

        dialog = app._show_archive_ledger_dialog()
        root.update_idletasks()
        try:
            assert len(dialog._tree.get_children()) == 2
            assert dialog._ledger is not None
            initial_ledger = dialog._ledger
            dialog._search_var.set("alice")
            # Immediate tick has not refreshed yet due to debounce
            root.update_idletasks()
            assert len(dialog._tree.get_children()) == 2
            # Wait past 180ms debounce window and pump Tk events
            import time

            deadline = time.monotonic() + 1.0
            while len(dialog._tree.get_children()) != 1 and time.monotonic() < deadline:
                root.update()
                time.sleep(0.02)
            assert len(dialog._tree.get_children()) == 1
            # Same persistent DownloadArchive connection was reused across refreshes
            assert dialog._ledger is initial_ledger
        finally:
            dialog.destroy()
        assert dialog._ledger is None
    finally:
        root.destroy()


def test_gui_verify_auth_catalog_browser_and_update_handlers(tmp_path, monkeypatch):
    from audiobookdl.sources.source import CatalogBook
    from audiobookdl.utils.updater import UpdateInfo

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.enrich_metadata_var.set(True)
        opts = app.build_options(urls_override=[])
        assert opts.enrich_metadata is True

        shown_info = []
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showinfo",
            lambda title, msg, parent=None: shown_info.append((title, msg)),
        )
        app._auth_panel.is_verifying_auth = True
        app._handle_event(("auth_verified", "Storytel", True, None))
        assert not app._auth_panel.is_verifying_auth
        assert "Login OK: Storytel" in app.status_var.get()
        assert shown_info[-1][0] == "Test Login"

        app._handle_event((
            "update_checked",
            UpdateInfo(current_version="0.7.7", latest_version="1.2.0", has_update=True),
            True,
        ))
        assert "Update available: v1.2.0" in app.status_var.get()
        assert shown_info[-1][0] == "Update Available"

        dialog = app._show_catalog_browser_dialog()
        root.update_idletasks()
        try:
            dialog.populate_items(
                [
                    CatalogBook(
                        title="Found Book 1",
                        url="https://librivox.org/book-1",
                        author="Author A",
                        series="Series X",
                        source="librivox",
                    ),
                    CatalogBook(
                        title="Found Book 2",
                        url="https://librivox.org/book-2",
                        author="Author B",
                        source="librivox",
                    ),
                ]
            )
            dialog._tree.selection_set(("book-0",))
            dialog.add_selected()
            assert "https://librivox.org/book-1" in app.urls_text.get("1.0", tk.END)
            assert "https://librivox.org/book-2" not in app.urls_text.get("1.0", tk.END)
        finally:
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except tk.TclError:
                pass
    finally:
        root.destroy()


def test_gui_scroll_layout_prevents_lower_clipping(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        root.update_idletasks()
        scroll_frame = app._scroll_frame
        req_w = scroll_frame.content_frame.winfo_reqwidth()
        req_h = scroll_frame.content_frame.winfo_reqheight()
        assert req_w <= 650

        # Short viewport: vertical scrollbar activates and lower Activity controls are not crushed
        short_h = max(320, req_h - 220)
        # pyrefly: ignore[bad-argument-type]
        scroll_frame.sync_layout(SimpleNamespace(width=700, height=short_h))
        root.update_idletasks()
        assert scroll_frame.vscroll_visible is True
        configured_h = int(float(scroll_frame.canvas.itemcget(scroll_frame.window_id, "height")))
        assert configured_h == req_h
        scroll_frame.canvas.yview_moveto(1.0)
        assert scroll_frame.canvas.yview()[1] == pytest.approx(1.0)

        # Tall viewport: vertical scrollbar hides and content expands to fill canvas height
        tall_h = req_h + 150
        # pyrefly: ignore[bad-argument-type]
        scroll_frame.sync_layout(SimpleNamespace(width=820, height=tall_h))
        # pyrefly: ignore[unnecessary-comparison]
        assert scroll_frame.vscroll_visible is False
        assert int(float(scroll_frame.canvas.itemcget(scroll_frame.window_id, "height"))) == tall_h
        assert scroll_frame.canvas.cget("scrollregion") == f"0 0 820 {tall_h}"
        assert scroll_frame.canvas.yview()[0] == pytest.approx(0.0)
    finally:
        root.destroy()


def test_format_audiobook_preview_omits_chapters_for_ebook():
    ebook = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Sample Ebook"),
        files=[AudiobookFile(url="https://example.com/book.epub", ext="epub")],
        chapters=[],
    )
    rows = dict(format_audiobook_preview(ebook))
    assert rows["Title"] == "Sample Ebook"
    assert rows["Parts"] == "1 file (epub)"
    assert "Chapters" not in rows


def test_gui_media_type_option_persistence_and_ffmpeg_bypass(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.media_type_var.get() == "Auto"
        assert app.build_options().media_type == "auto"

        app.media_type_var.set("Ebook")
        opts = app.build_options()
        assert opts.media_type == "ebook"

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda _cmd: None)
        assert app._ffmpeg_assistant.ensure_available(opts) is True

        app._save_run_preferences(opts)
    finally:
        root.destroy()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.media_type_var.get() == "Ebook"
        assert app2.build_options().media_type == "ebook"
    finally:
        root2.destroy()


def test_format_failed_job_detail_handles_tuples_and_truncation():
    assert format_failed_job_detail([]) == "Download failed"
    assert format_failed_job_detail([("Book A", "HTTP 403")]) == "Book A: HTTP 403"
    assert format_failed_job_detail([("", "Timed out\nretrying")]) == "Timed out retrying"
    assert format_failed_job_detail([("Book B", "")]) == "Book B"
    assert format_failed_job_detail([("   ", "   ")]) == "Download failed"

    long_detail = format_failed_job_detail([("https://example.com/book", "x" * 200)])
    assert len(long_detail) == 120
    assert long_detail.startswith("https://example.com/book: ")


def test_gui_failed_job_formats_book_error_tuple_as_string(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://example.com/broken-book\n")

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None):
            return DownloadSummary(
                completed=0,
                failed=1,
                cancelled=False,
                books_failed=1,
                book_errors=[("https://example.com/broken-book", "Authentication failed")],
            )

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        app.start_download()
        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert not app.is_running
        assert app.status_var.get() == "Failed"
        children = app.queue_tree.get_children()
        assert len(children) == 1
        job_id = children[0]
        rec = app.controller.queue_records[job_id]
        expected_detail = "https://example.com/broken-book: Authentication failed"
        assert rec["status"] == "Failed"
        assert isinstance(rec["detail"], str)
        assert rec["detail"] == expected_detail
        values = app.queue_tree.item(job_id, "values")
        assert isinstance(values, (tuple, list))
        assert values[2] == "Failed"
        assert values[3] == expected_detail
    finally:
        root.destroy()


def test_gui_cli_parity_controls_build_options_and_config_loading(tmp_path, monkeypatch):
    from audiobookdl.utils.archive import default_archive_path

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    db_dir = tmp_path / "db"
    custom_archive = tmp_path / "custom-archive.sqlite"
    cfg_path.write_text(
        f'database_directory = "{str(db_dir).replace(os.sep, "/")}"\n'
        'overwrite = false\n'
        'max_workers = 4\n'
        'book_delay = 2.5\n'
        'proxy = "http://127.0.0.1:8888"\n'
        'timeout = 45\n',
        encoding="utf-8",
    )

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.overwrite_var.get() == "Keep existing"
        assert app.max_workers_var.get() == "4"
        assert app.book_delay_var.get() == "2.5"
        assert app.proxy_var.get() == "http://127.0.0.1:8888"
        assert app.timeout_var.get() == "45"
        assert app.download_archive_var.get() is False

        # Set all 12 parity controls and verify DownloadOptions output
        app.split_chapters_var.set(True)
        app.write_opf_var.set(True)
        app.write_cue_var.set(True)
        app.write_cover_var.set(True)
        app.download_archive_var.set(True)
        app.max_workers_var.set("7")
        app.book_delay_var.set("1.25")
        app.proxy_var.set("http://127.0.0.1:9090")
        app.timeout_var.set("60")
        app.overwrite_var.set("Overwrite")
        app.remove_chars_var.set(":?")
        app.cover_only_var.set(True)

        opts = app.build_options(urls_override=["https://example.com/book"])
        assert opts.split_chapters is True
        assert opts.write_opf is True
        assert opts.write_cue is True
        assert opts.write_cover is True
        assert opts.download_archive is not None
        assert Path(opts.download_archive) == Path(default_archive_path(str(db_dir)))
        assert opts.max_workers == 7
        assert opts.book_delay == pytest.approx(1.25)
        assert opts.proxy == "http://127.0.0.1:9090"
        assert opts.timeout == pytest.approx(60.0)
        assert opts.overwrite is True
        assert opts.remove_chars == ":?"
        assert opts.cover is True

        # Cover-only preview & FFmpeg bypass even when combine/split_chapters are set
        assert app.template_preview_var.get().endswith("Dune.jpg")
        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda _cmd: None)
        assert app._ffmpeg_assistant.ensure_available(opts) is True

        # Splitting chapters without cover-only requires FFmpeg
        app.cover_only_var.set(False)
        app.combine_var.set(False)
        app.output_format_var.set("Original")
        split_opts = app.build_options(urls_override=["https://example.com/book"])
        assert split_opts.split_chapters is True
        assert app.template_preview_var.get().endswith("/01.m4b")
        monkeypatch.setattr("audiobookdl.gui.app.messagebox.askyesnocancel", lambda *_a, **_kw: False)
        assert app._ffmpeg_assistant.ensure_available(split_opts) is True

        # Invalid numeric option shows error dialog and prevents run
        errors: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showerror",
            lambda title, msg, parent=None: errors.append((title, msg)),
        )
        app.urls_text.insert("1.0", "https://example.com/book\n")
        app.max_workers_var.set("not-a-number")
        app.start_download()
        assert not app.is_running
        assert errors and errors[-1][0] == "Invalid Option"

        # Reloading config updates config-backed controls including download_archive toggle
        cfg_path.write_text(
            f'download_archive = "{str(custom_archive).replace(os.sep, "/")}"\n'
            'overwrite = true\n'
            'max_workers = 2\n',
            encoding="utf-8",
        )
        app._reload_config()
        assert app.download_archive_var.get() is True
        assert app.overwrite_var.get() == "Overwrite"
        assert app.max_workers_var.get() == "2"
        reloaded_opts = app.build_options(urls_override=[])
        assert reloaded_opts.download_archive is not None
        assert Path(reloaded_opts.download_archive) == custom_archive

        # Unchecking archive toggle explicitly disables download_archive
        app.download_archive_var.set(False)
        assert app.build_options(urls_override=[]).download_archive is None
    finally:
        root.destroy()


def test_gui_persists_all_toggles_on_close_and_run_start(tmp_path):
    from audiobookdl.config import load_config

    cfg_path = tmp_path / "audiobook-dl.toml"
    custom_archive = tmp_path / "my-archive.sqlite"
    cfg_path.write_text(
        f'download_archive = "{str(custom_archive).replace(os.sep, "/")}"\n',
        encoding="utf-8",
    )

    root = tk.Tk()
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    assert app.download_archive_var.get() is True

    app.output_format_var.set("mp3")
    app.combine_var.set(True)
    app.media_type_var.set("Ebook")
    app.audio_bitrate_var.set("128k")
    app.resume_var.set(False)
    app.skip_downloaded_var.set(True)
    app.write_json_var.set(True)
    app.enrich_metadata_var.set(True)
    app.enrich_providers_var.set("itunes,google")
    app.enrich_interactive_var.set(True)
    app.no_chapters_var.set(True)
    app.debug_var.set(True)
    app.split_chapters_var.set(True)
    app.write_opf_var.set(True)
    app.write_cue_var.set(True)
    app.write_cover_var.set(True)
    app.cover_only_var.set(True)
    app.remove_chars_var.set(":?")
    app.overwrite_var.set("Keep existing")
    app.max_workers_var.set("5")
    app.book_delay_var.set("2.5")
    app.proxy_var.set("http://127.0.0.1:8123")
    app.timeout_var.set("30")
    app.download_archive_var.set(False)

    # Closing the window without starting a download persists all toggles
    app._on_close()

    cfg_after_close = load_config(str(cfg_path))
    assert cfg_after_close.download_archive is not None
    assert Path(cfg_after_close.download_archive) == custom_archive
    assert cfg_after_close.gui.use_download_archive is False

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.output_format_var.get() == "mp3"
        assert app2.combine_var.get() is True
        assert app2.media_type_var.get() == "Ebook"
        assert app2.audio_bitrate_var.get() == "128k"
        assert app2.resume_var.get() is False
        assert app2.skip_downloaded_var.get() is True
        assert app2.write_json_var.get() is True
        assert app2.enrich_metadata_var.get() is True
        assert app2.enrich_providers_var.get() == "itunes,google"
        assert app2.enrich_interactive_var.get() is True
        assert app2.no_chapters_var.get() is True
        assert app2.debug_var.get() is True
        assert app2.split_chapters_var.get() is True
        assert app2.write_opf_var.get() is True
        assert app2.write_cue_var.get() is True
        assert app2.write_cover_var.get() is True
        assert app2.cover_only_var.get() is True
        assert app2.remove_chars_var.get() == ":?"
        assert app2.overwrite_var.get() == "Keep existing"
        assert app2.max_workers_var.get() == "5"
        assert app2.book_delay_var.get() == "2.5"
        assert app2.proxy_var.get() == "http://127.0.0.1:8123"
        assert app2.timeout_var.get() == "30"
        assert app2.download_archive_var.get() is False

        # Flip toggles and persist via _save_run_preferences (run-start path)
        app2.skip_downloaded_var.set(False)
        app2.debug_var.set(False)
        app2.enrich_metadata_var.set(False)
        app2.download_archive_var.set(True)
        opts2 = app2.build_options(urls_override=["https://example.com/book"])
        app2._save_run_preferences(opts2)
    finally:
        root2.destroy()

    root3 = tk.Tk()
    root3.withdraw()
    try:
        app3 = AudiobookDLApp(root3, config_location=str(cfg_path))
        assert app3.skip_downloaded_var.get() is False
        assert app3.debug_var.get() is False
        assert app3.enrich_metadata_var.get() is False
        assert app3.download_archive_var.get() is True
        rebuilt = app3.build_options(urls_override=[])
        assert rebuilt.download_archive is not None
        assert Path(rebuilt.download_archive) == custom_archive
        # Toggling write_json_metadata & no_chapters persisted across both sessions
        assert app3.write_json_var.get() is True
        assert app3.no_chapters_var.get() is True
    finally:
        root3.destroy()


def test_gui_close_persists_valid_toggles_despite_invalid_numeric_input(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    root = tk.Tk()
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    app.debug_var.set(True)
    app.write_opf_var.set(True)
    app.max_workers_var.set("not-a-number")
    app._on_close()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.debug_var.get() is True
        assert app2.write_opf_var.get() is True
        assert app2.max_workers_var.get() == ""
    finally:
        root2.destroy()


def test_preview_dialog_inline_metadata_and_cover_editing(tmp_path, monkeypatch):
    from PIL import Image

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        original_cover = Cover(image=_make_sample_png_bytes(), extension="png")
        audiobook = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(
                title="Original Title",
                authors=["First Author"],
                narrators=["First Narrator"],
                series="Original Series",
                series_order=1,
                description="Book summary.",
            ),
            files=[AudiobookFile(url="https://example.com/1.m4b", ext="m4b")],
            chapters=[Chapter(start=0, title="Ch 1")],
            cover=original_cover,
        )
        fake_source = SimpleNamespace(name="FakeSource")

        # 1. Cancelling via Close/destroy leaves audiobook untouched
        confirmed: list[tuple] = []
        dialog_cancel = app._show_preview_dialog("https://example.com/1", fake_source, audiobook)  # type: ignore[arg-type]
        dialog_cancel._title_var.set("Unsaved Change")
        dialog_cancel._series_var.set("Unsaved Series")
        dialog_cancel.destroy()
        assert audiobook.metadata.title == "Original Title"
        assert audiobook.metadata.series == "Original Series"
        assert confirmed == []

        # 2. Inline validation, cover replace/clear, and confirm apply edits
        from audiobookdl.gui.preview_dialog import PreviewDialog

        dialog = PreviewDialog(
            root,
            "https://example.com/1",
            fake_source,  # type: ignore[arg-type]
            audiobook,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda url, prefetched, indices: confirmed.append((url, prefetched, indices)),
            palette=app._palette,
        )
        assert dialog._title_var.get() == "Original Title"
        assert dialog._authors_var.get() == "First Author"
        assert dialog._narrators_var.get() == "First Narrator"
        assert dialog._series_var.get() == "Original Series"
        assert dialog._series_order_var.get() == "1"
        assert dialog._cover_photo is not None

        warnings: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "audiobookdl.gui.preview_dialog.messagebox.showwarning",
            lambda title, msg, parent=None: warnings.append((title, msg)),
        )

        # Empty title fails validation without closing
        dialog._title_var.set("   ")
        assert dialog._apply_audiobook_edits(audiobook) is False
        assert warnings and "Title" in warnings[-1][1]

        # Non-integer series order fails validation
        dialog._title_var.set("Edited Title")
        dialog._series_order_var.set("two")
        assert dialog._apply_audiobook_edits(audiobook) is False
        assert "whole number" in warnings[-1][1]

        # Clear cover, then replace cover from disk
        dialog._clear_cover()
        assert dialog._pending_cover is None
        assert dialog._cover_photo is None
        assert str(dialog._clear_cover_btn.cget("state")) == "disabled"  # type: ignore[union-attr]

        new_cover_path = tmp_path / "new_cover.jpg"
        Image.new("RGB", (96, 96), color=(200, 50, 50)).save(new_cover_path, format="JPEG")
        monkeypatch.setattr(
            "audiobookdl.gui.preview_dialog.filedialog.askopenfilename",
            lambda **_kw: str(new_cover_path),
        )
        dialog._choose_cover_file()
        assert dialog._pending_cover is not None
        assert dialog._pending_cover.extension == "jpg"
        assert dialog._cover_photo is not None
        assert str(dialog._clear_cover_btn.cget("state")) == "normal"  # type: ignore[union-attr]

        # Apply valid edits and confirm via the Download button
        dialog._authors_var.set("Author A; Author B")
        dialog._narrators_var.set("Narrator X; Narrator Y")
        dialog._series_var.set("Edited Series")
        dialog._series_order_var.set("4")
        from tkinter import ttk as _ttk

        action_btns = [
            w for w in dialog.winfo_children()[0].winfo_children()[-1].winfo_children()
            if isinstance(w, _ttk.Button) and str(w.cget("text")) in ("Download", "Add to Queue")
        ]
        assert len(action_btns) == 1
        action_btns[0].invoke()

        assert len(confirmed) == 1
        confirmed_url, (confirmed_src, confirmed_book), confirmed_indices = confirmed[0]
        assert confirmed_url == "https://example.com/1"
        assert confirmed_src is fake_source
        assert confirmed_indices is None
        assert confirmed_book is audiobook
        assert audiobook.metadata.title == "Edited Title"
        assert audiobook.metadata.authors == ["Author A", "Author B"]
        assert audiobook.metadata.narrators == ["Narrator X", "Narrator Y"]
        assert audiobook.metadata.series == "Edited Series"
        assert audiobook.metadata.series_order == 4
        assert audiobook.cover is not None
        assert audiobook.cover.extension == "jpg"
        assert audiobook.cover.image == new_cover_path.read_bytes()
    finally:
        root.destroy()


def test_gui_theme_switching_live_and_persistence(tmp_path, monkeypatch):
    from audiobookdl.gui.theme import DARK_PALETTE, LIGHT_PALETTE, resolve_palette

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))

        # Switch to dark theme live
        dark = app.set_theme("dark")
        assert dark == DARK_PALETTE
        assert app.theme_mode == "dark"
        assert app._style.theme_use() == "clam"
        assert app._style.lookup("Muted.TLabel", "foreground") == DARK_PALETTE.fg_muted
        assert app._style.lookup("Success.TLabel", "foreground") == DARK_PALETTE.success
        assert app.log_text.cget("background") == DARK_PALETTE.text_bg
        assert app.urls_text.cget("foreground") == DARK_PALETTE.text_fg
        assert app._scroll_frame.canvas.cget("background") == DARK_PALETTE.bg

        root.update_idletasks()
        assert app._scroll_frame.content_frame.winfo_reqwidth() <= 650

        # Open a child dialog in dark mode, then switch live to light mode
        book = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Themed Book"),
            files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
        )
        dialog = app._show_preview_dialog(
            "https://example.com/1",
            SimpleNamespace(name="FakeSource"),  # type: ignore[arg-type]
            book,
        )
        assert dialog.cget("background") == DARK_PALETTE.bg

        light = app.set_theme("light")
        assert light == LIGHT_PALETTE
        assert dialog.cget("background") == LIGHT_PALETTE.bg
        assert app._style.lookup("Muted.TLabel", "foreground") == LIGHT_PALETTE.fg_muted
        dialog.destroy()

        # Persist dark theme on close and restore on next launch
        app.set_theme("dark")
        app._on_close()
    except tk.TclError:
        root.destroy()
        raise

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.theme_mode == "dark"
        assert app2._palette == DARK_PALETTE
    finally:
        root2.destroy()

    # System mode delegates to detect_system_theme
    monkeypatch.setattr("audiobookdl.gui.theme.detect_system_theme", lambda: "dark")
    assert resolve_palette("system") == DARK_PALETTE
    monkeypatch.setattr("audiobookdl.gui.theme.detect_system_theme", lambda: "light")
    assert resolve_palette("system") == LIGHT_PALETTE


def test_gui_hidpi_scaling_helpers():
    from tkinter import ttk
    from audiobookdl.gui.theme import configure_hidpi_scaling, get_dpi_scale, scale_px

    class _FakeWidget:
        def __init__(self, dpi: float) -> None:
            self._dpi = dpi

        def winfo_fpixels(self, _spec: str) -> float:
            return self._dpi

    assert get_dpi_scale(None) == pytest.approx(1.0)
    assert get_dpi_scale(_FakeWidget(96.5)) == pytest.approx(1.0)  # type: ignore[arg-type]
    assert get_dpi_scale(_FakeWidget(144.0)) == pytest.approx(1.5)  # type: ignore[arg-type]
    assert scale_px(_FakeWidget(144.0), 180) == 270  # type: ignore[arg-type]
    assert scale_px(_FakeWidget(96.0), 180) == 180  # type: ignore[arg-type]

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        style = ttk.Style()
        scale = configure_hidpi_scaling(root, style)
        assert scale >= 1.0
        row_h = int(float(style.lookup("Treeview", "rowheight")))
        assert row_h >= 22
    finally:
        root.destroy()


def test_gui_cookie_import_dialog_states_and_apply(tmp_path):
    import http.cookiejar
    from audiobookdl.gui.cookie_dialog import CookieImportDialog
    from audiobookdl.utils.browser_cookies import (
        BrowserCookieError,
        CookieImportResult,
        _build_cookie,
    )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.remember_login_var.set(True)

        applied_paths = []
        cookie = _build_cookie(
            domain=".everand.com",
            name="scribd_session",
            value="abc_token",
            path="/",
            expires=1999999999,
            secure=True,
            http_only=True,
        )

        def _on_apply(key: str, path: str, count: int) -> None:
            applied_paths.append((key, path, count))
            app._auth_panel.on_cookies_imported(key, path, count)

        dialog = CookieImportDialog(
            root,
            app._services,
            initial_service_key="overdrive",
            on_apply=_on_apply,
            palette=app._palette,
            browser_detector=lambda: {"firefox": True, "edge": False, "chrome": False, "brave": False, "chromium": False, "opera": False},
        )
        root.update_idletasks()
        try:
            assert "overdrive.com" in dialog._domains_var.get()
            assert "(Detected)" in dialog._browser_var.get()

            # Switch service updates default domains and target path
            dialog._service_var.set("Everand / Scribd")
            dialog._on_service_changed()
            assert "everand.com" in dialog._domains_var.get()
            custom_out = tmp_path / "imported_everand.txt"
            dialog._output_var.set(str(custom_out))

            # Empty scan result shows inline guidance and keeps Save & Apply disabled
            dialog._on_scan_finished(
                CookieImportResult(
                    browser="firefox",
                    domains=("everand.com",),
                    cookies=[],
                    total_scanned=20,
                    profile_used="default",
                ),
                None,
            )
            assert "No cookies matched" in dialog._status_var.get()
            assert str(dialog._save_btn.cget("state")) == "disabled"

            # Error state surfaces message cleanly
            dialog._on_scan_finished(None, BrowserCookieError("Browser DB locked"))
            assert "Browser DB locked" in dialog._status_var.get()
            assert str(dialog._save_btn.cget("state")) == "disabled"

            # Success result populates Treeview and enables Save & Apply
            dialog._on_scan_finished(
                CookieImportResult(
                    browser="firefox",
                    domains=("everand.com",),
                    cookies=[cookie],
                    total_scanned=20,
                    profile_used="default-release",
                ),
                None,
            )
            assert len(dialog._tree.get_children()) == 1
            assert str(dialog._save_btn.cget("state")) == "normal"

            dialog.apply_and_close()
            assert applied_paths == [("everand", str(custom_out), 1)]
            assert app.cookie_file_var.get() == str(custom_out)
            assert custom_out.is_file()
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(str(custom_out), ignore_discard=True, ignore_expires=True)
            assert [c.value for c in jar] == ["abc_token"]
            assert app.controller.config.sources["everand"].cookie_file == str(custom_out)
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

        # Launching through app._show_cookie_import_dialog reuses singleton & wires palette
        app._auth_panel.show_cookie_import_dialog()
        assert app._auth_panel.cookie_dialog is not None
        assert app._auth_panel.cookie_dialog.winfo_exists()
        first_dlg = app._auth_panel.cookie_dialog
        app._auth_panel.show_cookie_import_dialog()
        assert app._auth_panel.cookie_dialog is first_dlg
        first_dlg.destroy()
    finally:
        root.destroy()


def test_gui_completion_notifications_and_toggle_persistence(tmp_path, monkeypatch):
    import subprocess
    from audiobookdl.gui.notifications import format_completion_notification, send_os_notification
    from audiobookdl.pipeline import DownloadSummary

    # 1. Payload formatting
    assert format_completion_notification(DownloadSummary(completed=1, failed=0), None) == (
        "audiobook-dl: Download Complete",
        "Finished downloading 1 audiobook.",
    )
    assert format_completion_notification(DownloadSummary(completed=2, failed=1), None) == (
        "audiobook-dl: Batch Finished",
        "Completed 2, failed 1.",
    )
    assert format_completion_notification(DownloadSummary(completed=0, failed=2), None) == (
        "audiobook-dl: Download Failed",
        "2 audiobooks failed to download.",
    )
    assert format_completion_notification(DownloadSummary(completed=1, cancelled=True), None) is None
    assert format_completion_notification(None, RuntimeError("Network timeout")) == (
        "audiobook-dl: Download Failed",
        "Network timeout",
    )

    # 2. Cross-platform dispatch helpers
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("audiobookdl.gui.notifications.subprocess.run", fake_run)
    monkeypatch.setattr("audiobookdl.gui.notifications.shutil.which", lambda name: f"/bin/{name}")

    assert send_os_notification("Done", "1 book", platform_name="win32") is True
    assert send_os_notification("Done", "1 book", platform_name="darwin") is True
    assert send_os_notification("Done", "1 book", platform_name="linux") is True
    assert any("osascript" in c[0] for c in commands)
    assert any("notify-send" in c[0] for c in commands)

    monkeypatch.setattr("audiobookdl.gui.notifications.shutil.which", lambda _name: None)
    assert send_os_notification("Done", "1 book", platform_name="linux") is False

    # 3. GUI trigger + config persistence across close/reopen
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    sent_notifs = []
    monkeypatch.setattr(
        "audiobookdl.gui.queue_panel.send_os_notification_async",
        lambda title, msg, **_kw: sent_notifs.append((title, msg)),
    )
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.notify_on_complete_var.get() is True

        app._finish_run(DownloadSummary(completed=2, failed=0), None)
        assert sent_notifs == [
            ("audiobook-dl: Download Complete", "Finished downloading 2 audiobooks.")
        ]

        # Cancelled runs do not notify
        sent_notifs.clear()
        app._finish_run(DownloadSummary(completed=0, cancelled=True), None)
        assert sent_notifs == []

        # Disabled toggle suppresses notifications and persists on close
        app.notify_on_complete_var.set(False)
        app._finish_run(DownloadSummary(completed=1, failed=0), None)
        assert sent_notifs == []

        app._on_close()
    except tk.TclError:
        root.destroy()
        raise

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.notify_on_complete_var.get() is False
    finally:
        root2.destroy()


def test_gui_preview_dialog_enrichment_updates_fields_and_cover(tmp_path):
    from io import BytesIO
    from PIL import Image
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.utils.audiobook import Chapter, Cover
    from audiobookdl.utils.enrichment import EnrichmentCandidate

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        buf = BytesIO()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="JPEG")
        cover = Cover(image=buf.getvalue(), extension="jpg")

        book = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
            files=[AudiobookFile(url="https://example.com/1.m4b", ext="m4b")],
        )
        candidate = EnrichmentCandidate(
            provider="audnexus",
            score=0.98,
            title="Dune",
            authors=["Frank Herbert"],
            narrators=["Scott Brick"],
            asin="B002V5B16O",
        )

        def fake_apply(target: Audiobook, _cand: EnrichmentCandidate):
            target.metadata.narrators = ["Scott Brick"]
            target.metadata.asin = "B002V5B16O"
            target.cover = cover
            target.chapters = [Chapter(start=0, title="Book 1")]
            return ["narrators", "asin"], True, True

        dialog = PreviewDialog(
            root,
            "https://example.com/dune",
            MagicMock(name="storytel"),
            book,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_args: None,
            on_search_enrichment=lambda _b: [candidate],
            on_apply_enrichment=fake_apply,
        )
        fields, cover_updated, chapters_updated = fake_apply(book, candidate)
        dialog._on_enrichment_applied(book, fields, cover_updated, chapters_updated, None)
        assert dialog._narrators_var.get() == "Scott Brick"
        assert dialog._pending_cover is cover
        assert dialog._cover_modified is True
        assert "narrators" in dialog._enrich_status_var.get()
        dialog.destroy()
    finally:
        root.destroy()


def test_gui_preview_cancel_enables_cancel_button_and_cancels_cleanly(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.urls_text.insert("1.0", "https://example.com/slow-book")
        app._set_previewing_state(True)
        assert str(app.cancel_btn.cget("state")) == "normal"
        assert str(app.preview_btn.cget("state")) == "disabled"
        assert str(app.download_btn.cget("state")) == "disabled"

        app.cancel_download()
        assert app.controller.preview_cancel_event.is_set()
        assert app.status_var.get() == "Cancelling preview..."
        assert str(app.cancel_btn.cget("state")) == "disabled"

        app._handle_preview_done("https://example.com/slow-book", None, DownloadCancelled())
        assert not app._is_previewing
        assert app.status_var.get() == "Preview cancelled"
        assert str(app.preview_btn.cget("state")) == "normal"
        assert str(app.download_btn.cget("state")) == "normal"
        assert str(app.cancel_btn.cget("state")) == "disabled"
    finally:
        root.destroy()


def test_gui_persistent_queue_and_pause_resume_toggle(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text("", encoding="utf-8")
    try:
        app1 = AudiobookDLApp(root, config_location=str(cfg_path))
        app1._set_running_state(True)
        assert str(app1.pause_btn.cget("state")) == "normal"
        assert app1.pause_btn.cget("text") == "Pause"
        app1.toggle_pause_queue()
        assert app1.controller.is_paused() is True
        assert app1.pause_btn.cget("text") == "Resume"
        app1.toggle_pause_queue()
        assert app1.controller.is_paused() is False
        assert app1.pause_btn.cget("text") == "Pause"
        app1._set_running_state(False)

        # Simulate unresolved & completed queue records before restart
        j1 = QueuedJob(job_id="j_pend", run_id=1, url="https://librivox.org/pending", options=args.build_default_options(), interaction=MagicMock())
        j2 = QueuedJob(job_id="j_done", run_id=1, url="https://librivox.org/done", options=args.build_default_options(), interaction=MagicMock())
        app1._queue_panel.register_queue_job(j1)
        app1._queue_panel.register_queue_job(j2)
        app1._queue_panel.update_queue_row("j_done", "Completed", "1 ok")
        app1.controller.shutdown(is_running=False, is_previewing=False)
    finally:
        root.destroy()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        children = app2.queue_tree.get_children()
        assert len(children) == 1
        values = app2.queue_tree.item(children[0], "values")
        assert isinstance(values, tuple)
        assert values[1] == "https://librivox.org/pending"
        assert values[2] == "Interrupted"
        assert app2.controller.retryable_urls(children) == ["https://librivox.org/pending"]
    finally:
        root2.destroy()


def test_gui_queue_panel_tracks_stage_subprogress(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        panel = app._queue_panel
        panel.prepare_run_ui_state(1, 1, "--- Starting ---")
        assert panel.stage_var.get() == "Stage: Preparing"

        panel.handle_activity_event(("batch_item", 1, 1, 1, "https://librivox.org/book"))
        assert panel.stage_var.get() == "Stage: Fetching metadata"

        panel.handle_activity_event(("progress_start", "Downloading Book", 10.0))
        assert panel.stage_var.get() == "Stage: Downloading"

        panel.handle_activity_event(("log", "INFO", "Combining files"))
        assert panel.stage_var.get() == "Stage: Combining & muxing"

        panel.handle_activity_event(("log", "INFO", "Enriching metadata from providers"))
        assert panel.stage_var.get() == "Stage: Enriching metadata"

        app._finish_run(DownloadSummary(completed=1, failed=0, cancelled=False), None)
        assert panel.stage_var.get() == ""
    finally:
        root.destroy()


def test_gui_controller_thread_local_sessions_and_dialog_main_thread_bridge(tmp_path):
    from audiobookdl.gui.controller import GuiController
    from audiobookdl.gui.widgets import MainThreadCallbackQueue

    ctrl = GuiController(config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        main_sess = ctrl.get_http_session()
        worker_sessions: list[Any] = []

        def _worker():
            worker_sessions.append(ctrl.get_http_session())
            worker_sessions.append(ctrl.get_http_session())

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=2.0)

        assert len(worker_sessions) == 2
        assert worker_sessions[0] is worker_sessions[1]
        assert worker_sessions[0] is not main_sess
    finally:
        ctrl.shutdown(is_running=False, is_previewing=False)
    assert ctrl._thread_sessions == []

    # Verify none of the 5 dialog modules invoke Tcl (`after` / `winfo_exists`) inside worker defs
    import ast

    gui_dir = Path(__file__).resolve().parent.parent / "audiobookdl" / "gui"
    for fname in (
        "abs_dialog.py",
        "archive_dialog.py",
        "cookie_dialog.py",
        "pack_dialog.py",
        "preview_dialog.py",
    ):
        tree = ast.parse((gui_dir / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_worker", "_apply_worker"):
                worker_src = ast.unparse(node)
                assert ".after(" not in worker_src, f"{fname}:{node.name} must not call .after()"
                assert ".winfo_exists(" not in worker_src, f"{fname}:{node.name} must not call .winfo_exists()"

    # Exercise MainThreadCallbackQueue delivering background thread callbacks to main thread
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        cbq = MainThreadCallbackQueue(root, interval_ms=10)
        cbq.begin_task()
        observed_threads: list[int] = []

        def _bg():
            try:
                cbq.post(lambda: observed_threads.append(threading.get_ident()))
            finally:
                cbq.finish_worker()

        threading.Thread(target=_bg, daemon=True).start()
        import time

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not observed_threads:
            root.update()
            time.sleep(0.01)
        assert observed_threads == [threading.get_ident()]
    finally:
        root.destroy()


def test_gui_preview_closes_source_on_dismiss_and_on_job_completion(tmp_path, monkeypatch):
    from audiobookdl.gui.preview_dialog import PreviewDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        src_dismiss = MagicMock()
        book = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Dismiss Book"),
            files=[],
        )
        dlg = PreviewDialog(
            root,
            "https://example.com/dismiss",
            src_dismiss,
            book,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_args: None,
        )
        dlg.destroy()
        src_dismiss.close.assert_called_once()

        # Confirmed preview transfers ownership to GuiController, which closes source after run
        src_confirm = MagicMock()
        confirmed_payloads: list[Any] = []
        dlg2 = PreviewDialog(
            root,
            "https://example.com/confirm",
            src_confirm,
            book,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda u, pref, idx: confirmed_payloads.append((u, pref, idx)),
        )
        dlg2._source_transferred = True
        dlg2.destroy()
        src_confirm.close.assert_not_called()

        app.output_dir_var.set(str(tmp_path))
        monkeypatch.setattr(app._ffmpeg_assistant, "ensure_available", lambda _opts: True)
        monkeypatch.setattr(app, "_save_run_preferences", lambda _opts: None)
        monkeypatch.setattr(
            "audiobookdl.gui.controller.run_downloads",
            lambda *_a, **_kw: DownloadSummary(completed=1, books_completed=1),
        )
        app._enqueue_from_preview("https://example.com/confirm", (src_confirm, book), None)
        import time

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and app._is_running:
            root.update()
            app._poll_queue()
            time.sleep(0.01)
        src_confirm.close.assert_called_once()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_remove_paused_job_does_not_execute_and_unexpected_exception_continues_queue(
    tmp_path, monkeypatch
):
    import time
    from audiobookdl.gui.controller import GuiController

    ctrl = GuiController(config_location=str(tmp_path / "audiobook-dl.toml"))
    executed_urls: list[str] = []
    entered_pause = threading.Event()
    gate_started = threading.Event()
    gate_release = threading.Event()
    original_wait = ctrl.pause_event.wait

    def tracking_wait(timeout=None):
        entered_pause.set()
        return original_wait(timeout)

    ctrl.pause_event.wait = tracking_wait  # type: ignore[method-assign]

    def fake_run_downloads(urls, _opts, _cfg, **_kw):
        url = urls[0]
        if url.endswith("/gate"):
            gate_started.set()
            gate_release.wait(timeout=2.0)
            return DownloadSummary(completed=1, books_completed=1)
        executed_urls.append(url)
        if url.endswith("/boom"):
            raise RuntimeError("unexpected non-audiobookdl error")
        return DownloadSummary(completed=1, books_completed=1)

    monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

    try:
        opts = args.build_default_options(database_directory=str(tmp_path))
        _run_id, jobs = ctrl.start_new_run(
            [
                ("https://example.com/gate", opts, None, None),
                ("https://example.com/paused-removed", opts, None, None),
                ("https://example.com/boom", opts, None, None),
                ("https://example.com/after-boom", opts, None, None),
            ]
        )
        for j in jobs:
            ctrl.register_queue_record(j, "Example")

        assert gate_started.wait(timeout=2.0)
        ctrl.pause_queue()
        gate_release.set()
        assert entered_pause.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and ctrl.active_job_id != jobs[1].job_id:
            time.sleep(0.01)
        assert ctrl.active_job_id == jobs[1].job_id

        removed, new_total, is_running = ctrl.remove_pending_job(jobs[1].job_id)
        assert removed is True
        assert is_running is False
        assert new_total == 3

        ctrl.resume_queue()
        events: list[tuple] = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ctrl.drain_events(events.append)
            if any(ev[0] == "done" for ev in events):
                break
            time.sleep(0.01)

        assert "https://example.com/paused-removed" not in executed_urls
        assert executed_urls == [
            "https://example.com/boom",
            "https://example.com/after-boom",
        ]
        done_ev = next(ev for ev in events if ev[0] == "done")
        _kind, _rid, summary, fatal_exc = done_ev
        assert fatal_exc is None
        assert summary.failed == 1
        assert summary.completed == 2
        statuses = {ev[1]: ev[2] for ev in events if ev[0] == "job_status"}
        assert statuses[jobs[2].job_id] == "Failed"
        assert statuses[jobs[3].job_id] == "Completed"
    finally:
        gate_release.set()
        ctrl.shutdown(is_running=False, is_previewing=False)


def test_gui_retry_failed_allowed_during_active_run_and_poll_continues_in_modal(
    tmp_path, monkeypatch
):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        ctrl = app.controller
        interaction = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
        opts_a = app.build_options(urls_override=["https://librivox.org/fail-a"])
        opts_b = app.build_options(urls_override=["https://librivox.org/run-b"])
        job_a = QueuedJob(job_id="ja", run_id=1, url="https://librivox.org/fail-a", options=opts_a, interaction=interaction)
        job_b = QueuedJob(job_id="jb", run_id=1, url="https://librivox.org/run-b", options=opts_b, interaction=interaction)
        with ctrl.queue_cond:
            ctrl.worker_has_active_run = True
            ctrl.worker_run_id = 1
            ctrl.batch_total = 2
            ctrl.run_seen_urls = {job_a.url, job_b.url}
            ctrl.pending_jobs.append(job_b)
        app._is_running = True
        app._queue_panel.register_queue_job(job_a)
        app._queue_panel.register_queue_job(job_b)
        app._queue_panel.update_queue_row("ja", "Failed", "network timeout")

        app._retry_failed_queue_items()
        pending_urls = [j.url for j in ctrl.pending_jobs if j is not None]
        assert "https://librivox.org/fail-a" in pending_urls
        assert ctrl.batch_total == 3

        # Simulate nested modal wait_window during prompt_choice: logs queued afterwards still drain!
        app._cancel_poll_timer()
        holder: dict[str, Any] = {"value": None}
        done = threading.Event()
        ctrl.event_queue.put(("prompt_choice", "Pick One", ["A", "B"], 0, holder, done))

        def _fake_wait_window(dlg):
            ctrl.event_queue.put(("log", "INFO", "Background progress during modal"))
            # Pump Tk timer while modal is open
            for _ in range(5):
                root.update()
                import time
                time.sleep(0.02)
            dlg.selected_index = 1
            dlg.destroy()

        monkeypatch.setattr(root, "wait_window", _fake_wait_window)
        app._poll_queue()
        assert done.is_set()
        assert holder["value"] == 1
        assert "Background progress during modal" in app.log_text.get("1.0", tk.END)
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_debounces_save_queue_state_and_hoists_series_build_options(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        save_calls = []
        monkeypatch.setattr(app.controller, "save_queue_state", lambda: save_calls.append(1))
        monkeypatch.setattr(app.controller, "ensure_worker_started", lambda: None)

        specs = [
            (f"https://librivox.org/book-{i}", app.build_options(urls_override=[f"https://librivox.org/book-{i}"]), None, None)
            for i in range(5)
        ]
        app._start_new_run(specs)
        assert len(save_calls) == 1

        # Series preview hoists build_options() once instead of per book in _series_book_exists
        build_calls = []
        orig_build = app.build_options

        def spy_build(*a, **kw):
            build_calls.append(1)
            return orig_build(*a, **kw)

        monkeypatch.setattr(app, "build_options", spy_build)
        app.skip_downloaded_var.set(True)
        sess = requests.Session()
        series = Series(
            title="Hoisted Series",
            books=[
                Audiobook(session=sess, metadata=AudiobookMetadata(title=f"Book {i}"), files=[], chapters=[])
                for i in range(6)
            ],
        )
        dlg = app._show_preview_dialog("https://example.com/series", SimpleNamespace(name="Fake"), series)  # type: ignore[arg-type]
        try:
            assert len(build_calls) == 1
        finally:
            if dlg.winfo_exists():
                dlg.destroy()

        # Series preview caches os.listdir stems per directory across books
        listdir_calls: list[str] = []
        orig_listdir = os.listdir

        def spy_listdir(path="."):
            listdir_calls.append(str(path))
            return orig_listdir(path)

        monkeypatch.setattr(os, "listdir", spy_listdir)
        app.output_dir_var.set(str(tmp_path))
        app.template_var.set("{title}")
        dlg2 = app._show_preview_dialog("https://example.com/series", SimpleNamespace(name="Fake"), series)  # type: ignore[arg-type]
        try:
            assert len(listdir_calls) == 1
        finally:
            if dlg2.winfo_exists():
                dlg2.destroy()

        # Coalescing save_queue_state inside drain_events prevents N writes on N job_status updates
        save_calls.clear()
        for i in range(5):
            job_id = app.controller.pending_jobs[i].job_id  # type: ignore[union-attr]
            app.controller.event_queue.put(("job_status", job_id, "Cancelled", "Cancelled"))
        app.controller.drain_events(app._handle_event)
        assert len(save_calls) == 1
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_set_theme_reuses_cached_ffmpeg_validation(tmp_path, monkeypatch):
    from audiobookdl.utils import clear_executable_validation_cache

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    clear_executable_validation_cache()
    bin_dir = tmp_path / "ffmpeg_bin"
    bin_dir.mkdir()
    exe_suffix = ".exe" if os.name == "nt" else ""
    ff_path = bin_dir / f"ffmpeg{exe_suffix}"
    fp_path = bin_dir / f"ffprobe{exe_suffix}"
    ff_path.write_text("stub", encoding="utf-8")
    fp_path.write_text("stub", encoding="utf-8")

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        f'ffmpeg_dir = "{str(bin_dir).replace(os.sep, "/")}"\n',
        encoding="utf-8",
    )

    spawn_calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        spawn_calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("audiobookdl.utils.shutil.which", lambda _name: None)
    monkeypatch.setattr("audiobookdl.utils.subprocess.run", fake_run)

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.ffmpeg_status_var.get() == "FFmpeg: Ready"
        assert len(spawn_calls) == 2  # initial ffmpeg + ffprobe probe

        app.set_theme("dark")
        app.set_theme("light")
        assert len(spawn_calls) == 2  # cached by (abspath, mtime_ns, size)
    finally:
        clear_executable_validation_cache()
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_options_panel_exposes_new_pipeline_options_and_roundtrips(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.loudnorm_var.set(True)
        app.mono_var.set(True)
        app.chapters_from_silence_var.set(True)
        app.chapter_offset_ms_var.set("-250")
        app.square_cover_var.set(True)
        app.max_cover_size_var.set("1200")
        app.post_exec_var.set("echo done")
        app.upload_to_abs_var.set(True)

        opts = app.build_options(urls_override=["https://librivox.org/book-1"])
        assert opts.loudnorm is True
        assert opts.mono is True
        assert opts.chapters_from_silence is True
        assert opts.chapter_offset_ms == -250
        assert opts.square_cover is True
        assert opts.max_cover_size == 1200
        assert opts.post_exec == "echo done"
        assert opts.upload_to_abs is True
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_dark_mode_listbox_accent_button_fonts_modality_and_scrollable_frame(tmp_path, monkeypatch):
    from audiobookdl.gui.theme import DARK_PALETTE, LIGHT_PALETTE
    from audiobookdl.sources.source import CatalogBook

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        pre_bound: list[int] = []
        root.bind("<MouseWheel>", lambda _e: pre_bound.append(1))
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))

        # ScrollableFrame preserves existing bindings via add="+" and ignores child Toplevel wheel events
        root.event_generate("<MouseWheel>", delta=-120)
        assert len(pre_bound) == 1

        app.set_theme("dark")
        assert app._style.lookup("Accent.TButton", "background") == DARK_PALETTE.accent
        app.set_theme("light")
        assert app._style.lookup("Accent.TButton", "background") == LIGHT_PALETTE.accent

        # PackDialog is single-instance, has grab_set / Escape, and styles tk.Listbox in dark mode
        app.set_theme("dark")
        monkeypatch.setattr(app._ffmpeg_assistant, "ensure_available", lambda _opts: True)
        dlg1 = app._show_pack_dialog()
        dlg2 = app._show_pack_dialog()
        assert dlg1 is not None and dlg1 is dlg2
        assert dlg1.grab_current() is dlg1
        assert dlg1._paths_listbox.cget("background") == DARK_PALETTE.surface
        assert dlg1._paths_listbox.cget("foreground") == DARK_PALETTE.fg
        # Wheel event originating from child Toplevel does not scroll main canvas
        app._scroll_frame.on_mousewheel(SimpleNamespace(widget=dlg1._paths_listbox, delta=-120))
        dlg1.destroy()

        # Archive, ABS, and Catalog dialogs guard against duplicate instances and support empty selection
        arch1 = app._show_archive_ledger_dialog()
        arch2 = app._show_archive_ledger_dialog()
        assert arch1 is arch2
        assert arch1.grab_current() is arch1
        arch1.destroy()

        abs1 = app._show_audiobookshelf_dialog()
        abs2 = app._show_audiobookshelf_dialog()
        assert abs1 is abs2
        assert abs1.grab_current() is abs1
        abs1.destroy()

        cat1 = app._show_catalog_browser_dialog()
        cat2 = app._show_catalog_browser_dialog()
        assert cat1 is cat2
        assert cat1.grab_current() is cat1
        cat1.populate_items(
            [
                CatalogBook(title="One", url="https://librivox.org/1", author="A", source="librivox"),
            ]
        )
        cat1._tree.selection_set(())
        cat1.add_selected()
        assert "https://librivox.org/1" not in app.urls_text.get("1.0", tk.END)
        assert "Select one or more" in cat1._status_var.get()
        cat1.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_sync_libraries_and_windows_toast_powershell_preference(tmp_path, monkeypatch):
    from audiobookdl.gui import notifications
    from audiobookdl.sync import SyncCycleResult

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        started: list[Any] = []
        shown: list[str] = []
        monkeypatch.setattr(app.controller, "start_library_sync", lambda opts: started.append(opts))
        monkeypatch.setattr("audiobookdl.gui.app.messagebox.showinfo", lambda t, _m, parent=None: shown.append(t))

        app._sync_libraries(services=["librivox"])
        assert len(started) == 1
        assert started[0].services == ["librivox"]
        assert app._is_syncing is True

        app._handle_event(("sync_done", SyncCycleResult(books_discovered=3, newly_added=1, downloaded=1), None))
        assert app._is_syncing is False
        assert "Sync complete" in app.status_var.get()
        assert shown == ["Library Sync Complete"]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()

    # Windows toast prefers powershell.exe (WinRT) and falls back to NotifyIcon on pwsh-only systems
    cmds_run: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name in ("pwsh.exe", "pwsh"):
            return "C:/Program Files/PowerShell/7/pwsh.exe"
        return None

    monkeypatch.setattr(notifications.shutil, "which", fake_which)
    monkeypatch.setattr(notifications.os.path, "isfile", lambda _p: False)
    monkeypatch.setattr(
        notifications.subprocess,
        "run",
        lambda cmd, **_kw: cmds_run.append(list(cmd)) or SimpleNamespace(returncode=0),
    )
    assert notifications._send_windows_toast("Title", "Body", app_name="audiobook-dl", timeout=2.0) is True
    assert "NotifyIcon" in cmds_run[0][-1]
    assert "WindowsRuntime" not in cmds_run[0][-1]

    cmds_run.clear()
    monkeypatch.setattr(
        notifications.shutil,
        "which",
        lambda name: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" if "powershell" in name else None,
    )
    assert notifications._send_windows_toast("Title", "Body", app_name="audiobook-dl", timeout=2.0) is True
    assert "WindowsRuntime" in cmds_run[0][-1]







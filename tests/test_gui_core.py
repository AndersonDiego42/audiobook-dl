import io
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from audiobookdl import args, logging, utils
from audiobookdl.pipeline import DownloadSummary, fetch_metadata, run_downloads
from audiobookdl.config import Config, load_config, save_source_credentials
from audiobookdl.exceptions import BookNotFound, DownloadCancelled
from audiobookdl.gui.app import (
    AudiobookDLApp,
    describe_series_book,
    format_audiobook_preview,
    format_duration,
    format_speed,
    is_valid_geometry,
    parse_url_text,
    run_self_test,
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


def test_build_default_options_matches_cli_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audiobook-dl"])
    cli_opts = vars(args.parse_arguments())
    default_opts = vars(args.build_default_options())
    assert default_opts == cli_opts


def test_logging_ui_sink_and_progress():
    sink = _RecordingSink()
    logging.set_ui_sink(sink)
    prev_quiet = logging.quiet_mode
    prev_debug = logging.debug_mode
    logging.quiet_mode = False
    logging.debug_mode = True
    try:
        logging.log("Downloading [blue]Example Book[/]")
        logging.debug("Debug [bold]line[/]")
        with logging.progress([]) as progress:
            task_id = progress.add_task("Downloading [blue]Example[/]", total=4)
            progress.advance(task_id, 1.5)
            assert progress.tasks[task_id].remaining == pytest.approx(2.5)
            progress.advance(task_id, 2.5)
            assert progress.tasks[task_id].remaining == pytest.approx(0.0)
    finally:
        logging.set_ui_sink(None)
        logging.quiet_mode = prev_quiet
        logging.debug_mode = prev_debug

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
        app._on_service_selected()
        app.username_var.set("alice@example.com")
        app.password_var.set("hunter2!")
        app.save_current_credentials()
        assert app.saved_badge_var.get() == "Saved"

        # Switch to Librivox (no auth), then back via URL auto-detection
        app.service_label_var.set("Librivox")
        app._on_service_selected()
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
        app2._on_service_selected()
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
        save_output_preferences("m4b", True, overwrite=str(cfg_path))

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

        monkeypatch.setattr("audiobookdl.gui.app.find_executable", lambda name: name)
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

        monkeypatch.setattr("audiobookdl.gui.app.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.app.run_downloads", fake_run_downloads)

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
    logging.set_ui_sink(sink)
    try:
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
    finally:
        logging.set_ui_sink(None)


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
            app._queue.put(("progress_advance", float(i), 300.0))
        app._queue.put(("log", "INFO", "Midway"))
        for i in range(151, 250):
            app._queue.put(("progress_advance", float(i), 300.0))

        # First tick drains up to _MAX_QUEUE_BATCH (200) items and coalesces consecutive advances
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
        monkeypatch.setattr("audiobookdl.gui.app.find_executable", lambda _name: None)
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showinfo",
            lambda title, message, parent=None: shown.append((title, message)),
        )
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        assert "Not found" in app.ffmpeg_status_var.get()
        app._show_ffmpeg_guidance()
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
        assert app2._config.gui.window_geometry is not None
        assert app2._config.gui.window_geometry.startswith("880x710")
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

        monkeypatch.setattr("audiobookdl.gui.app.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.app.run_downloads", fake_run_downloads)

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
    logging.set_ui_sink(sink)
    try:
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
    finally:
        logging.set_ui_sink(None)


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
            "audiobookdl.gui.app.prepare_source_result",
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
            "audiobookdl.gui.app.prepare_source_result",
            lambda _u, _o, _c, **_kw: (fake_source, series),
        )
        captured: dict = {}

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None, selected_indices=None, prefetched=None):
            captured["urls"] = list(urls)
            captured["selected_indices"] = selected_indices
            captured["prefetched"] = prefetched
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.app.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.app.run_downloads", fake_run_downloads)

        app.preview_metadata()
        for _ in range(100):
            root.update()
            if not app.is_previewing and app._preview_dialog is not None:
                break
            threading.Event().wait(0.02)

        series_dialog = app._preview_dialog
        assert series_dialog is not None
        check_vars = getattr(series_dialog, "_check_vars")
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
            "audiobookdl.gui.app.prepare_source_result",
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
    monkeypatch.setattr("audiobookdl.gui.app.filedialog.askdirectory", lambda **_kw: str(ffmpeg_root))

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert "Not found" in app.ffmpeg_status_var.get()

        assert app.browse_ffmpeg_dir()
        assert app.ffmpeg_status_var.get() == "FFmpeg: Ready"
        assert load_config(str(cfg_path)).ffmpeg_dir == os.path.abspath(str(bin_dir))
    finally:
        root.destroy()
        utils.set_ffmpeg_dir(None)

    # Re-opening app restores ffmpeg_dir from audiobook-dl.toml
    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        assert app2.ffmpeg_status_var.get() == "FFmpeg: Ready"
    finally:
        root2.destroy()
        utils.set_ffmpeg_dir(None)


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
        monkeypatch.setattr(
            "audiobookdl.gui.app.run_downloads",
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
        utils.set_ffmpeg_dir(None)


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

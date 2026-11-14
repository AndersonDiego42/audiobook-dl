import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

import pytest

from audiobookdl import args, logging, utils
from audiobookdl.__main__ import DownloadSummary, run_downloads
from audiobookdl.config import Config, load_config, save_source_credentials
from audiobookdl.exceptions import BookNotFound, DownloadCancelled
from audiobookdl.gui.app import AudiobookDLApp, parse_url_text
from audiobookdl.output.download import setup_download_dir
from audiobookdl.sources import detect_service_key, get_available_services


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
    assert Path(utils.find_executable("ffmpeg")) == in_exe_dir

    # Prefers _MEIPASS when present
    in_meipass = meipass_dir / exe_name
    in_meipass.write_text("meipass")
    assert Path(utils.find_executable("ffmpeg")) == in_meipass


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
    with patch("audiobookdl.__main__.process_url", side_effect=BookNotFound()):
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
        assert app.is_running is True
        app._finish_run(DownloadSummary(completed=1, failed=0, cancelled=False), None)
        assert app.is_running is False
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

        monkeypatch.setattr("audiobookdl.gui.app.simpledialog.askstring", lambda *a, **kw: "secret-user")
        monkeypatch.setattr("audiobookdl.gui.app.messagebox.askyesno", lambda *a, **kw: True)

        observed = {}

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None):
            logging.log("Downloading [blue]Test Book[/]")
            with logging.progress([]) as progress:
                task_id = progress.add_task("Downloading [blue]Test Book[/]", total=2)
                progress.advance(task_id, 1)
                progress.advance(task_id, 1)
            observed["asked"] = interaction.ask_input("Username")
            observed["confirmed"] = interaction.confirm("Override?")
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.app.run_downloads", fake_run_downloads)

        app.start_download()
        assert app.is_running is True

        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert app.is_running is False
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

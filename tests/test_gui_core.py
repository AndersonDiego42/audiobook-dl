import io
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import requests

from audiobookdl import logging, utils
from audiobookdl.options_schema import DownloadOptions, build_default_options
from audiobookdl.pipeline import DownloadSummary, fetch_metadata, run_downloads
from audiobookdl.config import Config, load_config, save_source_credentials
from audiobookdl.exceptions import BookNotFound, DownloadCancelled
from audiobookdl.gui.app import (
    AudiobookDLApp,
    parse_gui_args,
    run_self_test,
)
from audiobookdl.gui.controller import GuiInteractionHandler, QueuedJob
from audiobookdl.gui.widgets import tree_item_values
from audiobookdl.gui.events import (
    AuthVerifiedEvent,
    BatchItemEvent,
    CatalogLoadedEvent,
    ControllerEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogEvent,
    ProgressAdvanceEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
    PromptChoiceEvent,
    RunDoneEvent,
    SyncDoneEvent,
    UpdateCheckedEvent,
)
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


def test_build_default_options_matches_schema_defaults():
    default_opts = vars(build_default_options())
    assert default_opts == vars(DownloadOptions())


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
    options = build_default_options()
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
    options = build_default_options()
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


@pytest.mark.gui
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
        app._runs.set_running_state(True)
        assert app.is_running
        app._runs.finish_run(DownloadSummary(completed=1, failed=0, cancelled=False), None)
        assert not app.is_running
        assert app.status_var.get() == "Completed"

        app._runs.finish_run(DownloadSummary(completed=0, failed=0, cancelled=True), None)
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


@pytest.mark.gui
def test_gui_service_dropdown_saves_and_restores_credentials(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        acct_page = app.navigate("accounts")
        auth_panel = acct_page.auth_panel
        dropdown_values = list(auth_panel.service_combo["values"])
        assert "Storytel / Mofibo" in dropdown_values
        assert "YourCloudLibrary" in dropdown_values

        # Select Storytel, enter login & password, save
        app.service_label_var.set("Storytel / Mofibo")
        auth_panel.on_service_selected()
        app.username_var.set("alice@example.com")
        app.password_var.set("hunter2!")
        app._auth.save_current_credentials()
        assert app.saved_badge_var.get() == "Saved"

        # Switch to Librivox (no auth), then back via URL auto-detection
        app.service_label_var.set("Librivox")
        auth_panel.on_service_selected()
        assert app.username_var.get() == ""
        assert str(auth_panel.user_entry.cget("state")) == "disabled"

        app.navigate("download")
        app.urls_text.insert("1.0", "https://www.storytel.com/no/nn/books/something-12345")
        app._urls.on_urls_changed()
        assert app.service_label_var.get() == "Storytel / Mofibo"
        assert app.username_var.get() == "alice@example.com"
        assert app.password_var.get() == "hunter2!"

        # Re-opening app restores saved credentials from disk
        root.destroy()
        root = tk.Tk()
        root.withdraw()
        app2 = AudiobookDLApp(root, config_location=str(cfg_path))
        acct2 = app2.navigate("accounts")
        app2.service_label_var.set("Storytel / Mofibo")
        acct2.auth_panel.on_service_selected()
        assert app2.username_var.get() == "alice@example.com"
        assert app2.password_var.get() == "hunter2!"

        app2._auth.forget_current_credentials()
        assert app2.username_var.get() == ""
        assert app2.password_var.get() == ""
        assert app2.saved_badge_var.get() == ""
    finally:
        root.destroy()


@pytest.mark.gui
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
        app._options.on_m4b_toggled()
        assert app.output_format_var.get() == "m4b"
        assert app.combine_var.get() is True

        opts = app.build_options()
        assert opts.output_format == "m4b"
        assert opts.combine is True

        # Selecting another format clears the m4b checkbox
        app.output_format_var.set("mp3")
        app._options.on_format_selected()
        assert app.m4b_var.get() is False

        # Selecting m4b via dropdown checks the m4b checkbox and enables combine
        app.combine_var.set(False)
        app.output_format_var.set("m4b")
        app._options.on_format_selected()
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


@pytest.mark.gui
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
        monkeypatch.setattr("audiobookdl.gui.event_dispatcher.simpledialog.askstring", lambda *a, **kw: "secret-user")
        monkeypatch.setattr("audiobookdl.gui.app.messagebox.askyesno", lambda *a, **kw: True)

        observed = {}

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None):
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


@pytest.mark.gui
def test_gui_poll_queue_bounds_batch_and_coalesces_progress(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        handled: list[ControllerEvent] = []
        original_handle = app._events.handle_event

        def recording_handle(item: ControllerEvent) -> None:
            handled.append(item)
            original_handle(item)

        app._events.handle_event = recording_handle  # type: ignore[method-assign]

        # Queue 250 items: burst of progress_advance, a log in between, and more progress_advance
        for i in range(1, 151):
            app.controller.event_queue.put(ProgressAdvanceEvent(completed=float(i), total=300.0))
        app.controller.event_queue.put(LogEvent(level="INFO", message="Midway"))
        for i in range(151, 250):
            app.controller.event_queue.put(ProgressAdvanceEvent(completed=float(i), total=300.0))

        # First tick drains up to MAX_QUEUE_BATCH (200) items and coalesces consecutive advances
        app._events.poll_queue()
        assert handled == [
            ProgressAdvanceEvent(completed=150.0, total=300.0),
            LogEvent(level="INFO", message="Midway"),
            ProgressAdvanceEvent(completed=199.0, total=300.0),
        ]
        assert app.progressbar.cget("value") == pytest.approx(199.0 / 300.0 * 100.0)

        # Second tick drains remaining 50 items, coalesced into one progress_advance
        handled.clear()
        app._events.poll_queue()
        assert handled == [ProgressAdvanceEvent(completed=249.0, total=300.0)]
        assert app.progressbar.cget("value") == pytest.approx(249.0 / 300.0 * 100.0)
    finally:
        root.destroy()


@pytest.mark.gui
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


@pytest.mark.gui
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


@pytest.mark.gui
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

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None):
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


@pytest.mark.gui
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

        added = app._urls.paste_urls_from_clipboard()
        assert added == [
            "https://www.storytel.com/no/nn/books/something-12345",
            "https://example.com/book2",
        ]
        assert app.service_label_var.get() == "Storytel / Mofibo"

        # Pasting same clipboard again deduplicates
        added_again = app._urls.paste_urls_from_clipboard()
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

    options = build_default_options()
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


@pytest.mark.gui
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
            if not app.is_previewing and app._preview.dialog is not None:
                break
            threading.Event().wait(0.02)

        assert not app.is_previewing
        dialog = app._preview.dialog
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

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None, selected_indices=None, prefetched=None):
            captured["urls"] = list(urls)
            captured["selected_indices"] = selected_indices
            captured["prefetched"] = prefetched
            return DownloadSummary(completed=1, failed=0, cancelled=False)

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        app.preview_metadata()
        for _ in range(100):
            root.update()
            if not app.is_previewing and app._preview.dialog is not None:
                break
            threading.Event().wait(0.02)

        series_dialog = app._preview.dialog
        assert series_dialog is not None
        check_vars = series_dialog._check_vars
        assert len(check_vars) == 3
        check_vars[1].set(False)
        selected = [i for i, v in enumerate(check_vars) if v.get()]
        series_dialog.destroy()

        app._preview.enqueue_from_preview("https://example.com/series", (fake_source, series), selected)  # type: ignore[arg-type]
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


@pytest.mark.gui
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


@pytest.mark.gui
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
    monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.filedialog.askdirectory", lambda **_kw: str(ffmpeg_root))

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert "Not found" in app.ffmpeg_status_var.get()

        assert app._ffmpeg_assistant.browse_dir()
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


@pytest.mark.gui
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
        # Cancel/close-X in prompt aborts before download starts without opening a second guidance dialog
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.askyesnocancel",
            lambda *_a, **_kw: None,
        )
        app.start_download()
        assert not app.is_running
        assert guidance_calls == []

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
            "audiobookdl.gui.ffmpeg_assistant.filedialog.askdirectory",
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


@pytest.mark.gui
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
        "audiobookdl.gui.ffmpeg_assistant.filedialog.askdirectory",
        lambda **_kw: str(bad_dir.parent),
    )

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert "Unusable" in app.ffmpeg_status_var.get()

        # Selecting an unrunnable binary is rejected and keeps config unchanged
        assert app._ffmpeg_assistant.browse_dir() is False
        assert warnings and warnings[-1][0] == "Invalid FFmpeg Binary"

        # Resetting clears ffmpeg_dir and updates status
        app._ffmpeg_assistant.reset_dir()
        assert load_config(str(cfg_path)).ffmpeg_dir is None
        assert "Not found" in app.ffmpeg_status_var.get()
    finally:
        root.destroy()


@pytest.mark.gui
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
        app._options.on_m4b_toggled()

        opts = app.build_options()
        assert opts.resume is True
        assert opts.audio_bitrate == "192k"
        app._options.save_run_preferences(opts)
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


@pytest.mark.gui
def test_gui_log_caps_line_count(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        for i in range(AudiobookDLApp.MAX_LOG_LINES + 50):
            app.download_page.queue_panel.append_log(f"line {i}")
        total_lines = int(app.log_text.index("end-1c").split(".")[0])
        assert total_lines <= AudiobookDLApp.MAX_LOG_LINES + 1
        assert "line 0" not in app.log_text.get("1.0", "2.0")
    finally:
        root.destroy()


@pytest.mark.gui
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
        app.download_page.queue_panel.register_queue_job(job_a)
        app.download_page.queue_panel.register_queue_job(job_b)

        # Move job_b up before job_a
        app.queue_tree.selection_set("job_b")
        app.download_page.queue_panel.move_selected_item(-1)
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["job_b", "job_a"]
        assert list(app.queue_tree.get_children()) == ["job_b", "job_a"]

        # Remove job_a
        app.queue_tree.selection_set("job_a")
        app.download_page.queue_panel.remove_selected_item()
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["job_b"]
        assert list(app.queue_tree.get_children()) == ["job_b"]
        assert ctrl.batch_total == 1

        # Retry failed items and clear history
        ctrl.pending_jobs.clear()
        ctrl.worker_has_active_run = False
        app.download_page.queue_panel.update_queue_row("job_b", "Failed", "timeout")

        retried = {}
        monkeypatch.setattr(app._ffmpeg_assistant, "ensure_available", lambda _opts: True)
        monkeypatch.setattr(app._options, "save_run_preferences", lambda _opts: None)
        monkeypatch.setattr(app._runs, "start_new_run", lambda specs: retried.setdefault("urls", [s[0] for s in specs]))
        app._runs.retry_failed_queue_items()
        assert retried["urls"] == ["https://librivox.org/book-b"]

        app.download_page.queue_panel.clear_completed_history()
        assert list(app.queue_tree.get_children()) == []
    finally:
        root.destroy()


@pytest.mark.gui
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
        dialog = app._preview.show_preview_dialog("https://www.storytel.com/series/1", fake_source, series)
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
            dialog._apply_filter()
            root.update_idletasks()
            assert list(dialog._tree.get_children()) == ["1"]
        finally:
            dialog.destroy()
    finally:
        root.destroy()


@pytest.mark.gui
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
        app.download_page.queue_panel.current_ui_run_id = 2
        app._events.handle_event(ItemProgressEvent(run_id=1, current=3, total=5, title="Old Run Book"))
        assert app.download_page.queue_panel.ui_book_index == 0
        app._events.handle_event(ItemProgressEvent(run_id=2, current=1, total=4, title="Current Run Book"))
        assert app.download_page.queue_panel.ui_book_index == 1
        assert app.download_page.queue_panel.ui_book_total == 4

        app.service_label_var.set("Storytel")
        acct = app.navigate("accounts")
        acct.auth_panel.on_service_selected()
        asked = {}
        monkeypatch.setattr(
            "audiobookdl.gui.auth_panel.messagebox.askyesno",
            lambda title, msg, parent=None: asked.setdefault("msg", msg) and False,
        )
        proceed = acct.auth_panel.warn_mixed_service_credentials([
            "https://www.storytel.com/books/1",
            "https://www.nextory.com/book/2",
        ])
        assert proceed is False
        assert "Nextory" in asked["msg"]
    finally:
        root.destroy()


@pytest.mark.gui
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
        app._options.on_preset_selected()
        assert "series_order_padded" in app.template_var.get()
        preview = app.template_preview_var.get().replace("\\", "/")
        assert "Frank Herbert/Dune Chronicles/Book 01 - Dune" in preview

        app.template_var.set("{author} - {title}")
        assert app.template_preset_var.get() == "Custom"
    finally:
        root.destroy()


@pytest.mark.gui
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

        lib_page = app.open_library_tab("archive")
        tab = lib_page.archive_tab
        root.update_idletasks()
        assert len(tab._tree.get_children()) == 2
        assert tab._ledger is not None
        initial_ledger = tab._ledger
        tab._search_var.set("alice")
        # Immediate tick has not refreshed yet due to debounce
        root.update_idletasks()
        assert len(tab._tree.get_children()) == 2
        # Wait past 180ms debounce window and pump Tk events
        import time

        deadline = time.monotonic() + 1.0
        while len(tab._tree.get_children()) != 1 and time.monotonic() < deadline:
            root.update()
            time.sleep(0.04)
        assert len(tab._tree.get_children()) == 1
        # Same persistent DownloadArchive connection was reused across refreshes
        assert tab._ledger is initial_ledger
    finally:
        root.destroy()


@pytest.mark.gui
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
        monkeypatch.setattr(
            "audiobookdl.gui.auth_panel.messagebox.showinfo",
            lambda title, msg, parent=None: shown_info.append((title, msg)),
        )
        app._auth.is_verifying_auth = True
        app._events.handle_event(AuthVerifiedEvent(service_label="Storytel", ok=True, error=None))
        assert not app._auth.is_verifying_auth
        assert "Login OK: Storytel" in app.status_var.get()
        assert shown_info[-1][0] == "Test Login"

        app._events.handle_event(
            UpdateCheckedEvent(
                info=UpdateInfo(current_version="0.7.7", latest_version="1.2.0", has_update=True),
                interactive=True,
            )
        )
        assert "Update available: v1.2.0" in app.status_var.get()
        assert shown_info[-1][0] == "Update Available"

        lib_page = app.open_library_tab("catalog")
        tab = lib_page.catalog_tab
        root.update_idletasks()
        tab.populate_items(
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
        tab._tree.selection_set(("book-0",))
        tab.add_selected()
        assert "https://librivox.org/book-1" in app.urls_text.get("1.0", tk.END)
        assert "https://librivox.org/book-2" not in app.urls_text.get("1.0", tk.END)
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui_scroll_layout_prevents_lower_clipping(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        root.update_idletasks()
        scroll_frame = app.download_page.top_scroll
        req_w = scroll_frame.content_frame.winfo_reqwidth()
        req_h = scroll_frame.content_frame.winfo_reqheight()
        assert req_w <= 750

        # Short viewport: vertical scrollbar activates inside top pane without crushing queue
        short_h = max(80, req_h - 140)
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


@pytest.mark.gui
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

        app._options.save_run_preferences(opts)
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


@pytest.mark.gui
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

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None):
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
        assert app.queue_tree.set(job_id, "status") == "Failed"
        assert app.queue_tree.set(job_id, "detail") == expected_detail
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui_controls_build_options_and_config_loading(tmp_path, monkeypatch):
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
        'timeout = 45\n'
        'max_retries = 6\n',
        encoding="utf-8",
    )

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        assert app.overwrite_var.get() == "Keep existing"
        assert app.max_workers_var.get() == "4"
        assert app.book_delay_var.get() == "2.5"
        assert app.proxy_var.get() == "http://127.0.0.1:8888"
        assert app.timeout_var.get() == "45"
        assert app.max_retries_var.get() == "6"
        assert app.download_archive_var.get() is False

        # Set all option controls and verify DownloadOptions output
        app.split_chapters_var.set(True)
        app.write_opf_var.set(True)
        app.write_cue_var.set(True)
        app.write_cover_var.set(True)
        app.download_archive_var.set(True)
        app.max_workers_var.set("7")
        app.book_delay_var.set("1.25")
        app.proxy_var.set("http://127.0.0.1:9090")
        app.timeout_var.set("60")
        app.max_retries_var.set("2")
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
        assert opts.max_retries == 2
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
        assert app.template_preview_var.get().endswith(f"{os.sep}01.m4b")
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
        app._options.reload_config()
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


@pytest.mark.gui
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
    app.intro_chapter_mode_var.set("Always (first is 00)")
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
    app.max_retries_var.set("2")
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
        assert app2.intro_chapter_mode_var.get() == "Always (first is 00)"
        assert app2.build_options().intro_chapter_mode == "always"
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
        assert app2.max_retries_var.get() == "2"
        assert app2.download_archive_var.get() is False

        # Flip toggles and persist via _save_run_preferences (run-start path)
        app2.skip_downloaded_var.set(False)
        app2.debug_var.set(False)
        app2.enrich_metadata_var.set(False)
        app2.download_archive_var.set(True)
        opts2 = app2.build_options(urls_override=["https://example.com/book"])
        app2._options.save_run_preferences(opts2)
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


@pytest.mark.gui
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


@pytest.mark.gui
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
        dialog_cancel = app._preview.show_preview_dialog("https://example.com/1", fake_source, audiobook)  # type: ignore[arg-type]
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
            palette=app._window.palette,
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


@pytest.mark.gui
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
        assert app.download_page.top_scroll.canvas.cget("background") == DARK_PALETTE.bg

        root.update_idletasks()
        assert app.download_page.top_scroll.content_frame.winfo_reqwidth() <= 750

        # Open a child dialog in dark mode, then switch live to light mode
        book = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Themed Book"),
            files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
        )
        dialog = app._preview.show_preview_dialog(
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
        assert app2._window.palette == DARK_PALETTE
    finally:
        root2.destroy()

    # System mode delegates to detect_system_theme
    monkeypatch.setattr("audiobookdl.gui.theme.detect_system_theme", lambda: "dark")
    assert resolve_palette("system") == DARK_PALETTE
    monkeypatch.setattr("audiobookdl.gui.theme.detect_system_theme", lambda: "light")
    assert resolve_palette("system") == LIGHT_PALETTE


@pytest.mark.gui
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


@pytest.mark.gui
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
            app._auth.on_cookies_imported(key, path, count)

        dialog = CookieImportDialog(
            root,
            app._services,
            initial_service_key="overdrive",
            on_apply=_on_apply,
            palette=app._window.palette,
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

        # Launching through app._auth.show_cookie_import_dialog reuses singleton & wires palette
        app._auth.show_cookie_import_dialog()
        assert app._auth.cookie_dialog is not None
        assert app._auth.cookie_dialog.winfo_exists()
        first_dlg = app._auth.cookie_dialog
        app._auth.show_cookie_import_dialog()
        assert app._auth.cookie_dialog is first_dlg
        first_dlg.destroy()
    finally:
        root.destroy()


@pytest.mark.gui
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

        app._runs.finish_run(DownloadSummary(completed=2, failed=0), None)
        assert sent_notifs == [
            ("audiobook-dl: Download Complete", "Finished downloading 2 audiobooks.")
        ]

        # Cancelled runs do not notify
        sent_notifs.clear()
        app._runs.finish_run(DownloadSummary(completed=0, cancelled=True), None)
        assert sent_notifs == []

        # Disabled toggle suppresses notifications and persists on close
        app.notify_on_complete_var.set(False)
        app._runs.finish_run(DownloadSummary(completed=1, failed=0), None)
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


@pytest.mark.gui
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

        def fake_apply(target: Audiobook, _cand: EnrichmentCandidate, _opts: Any = None):
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
            on_search_enrichment=lambda _b, _opts: [candidate],
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


@pytest.mark.gui
def test_gui_preview_cancel_enables_cancel_button_and_cancels_cleanly(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        app.urls_text.insert("1.0", "https://example.com/slow-book")
        app._preview.set_previewing_state(True)
        assert str(app.cancel_btn.cget("state")) == "normal"
        assert str(app.preview_btn.cget("state")) == "disabled"
        assert str(app.download_btn.cget("state")) == "disabled"

        app.cancel_download()
        assert app.controller.preview_cancel_event.is_set()
        assert app.status_var.get() == "Cancelling preview..."
        assert str(app.cancel_btn.cget("state")) == "disabled"

        app._preview.handle_preview_done("https://example.com/slow-book", None, DownloadCancelled())
        assert not app.is_previewing
        assert app.status_var.get() == "Preview cancelled"
        assert str(app.preview_btn.cget("state")) == "normal"
        assert str(app.download_btn.cget("state")) == "normal"
        assert str(app.cancel_btn.cget("state")) == "disabled"
    finally:
        root.destroy()


@pytest.mark.gui
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
        app1._runs.set_running_state(True)
        assert str(app1.pause_btn.cget("state")) == "normal"
        assert app1.pause_btn.cget("text") == "Pause"
        app1.toggle_pause_queue()
        assert app1.controller.is_paused() is True
        assert app1.pause_btn.cget("text") == "Resume"
        app1.toggle_pause_queue()
        assert app1.controller.is_paused() is False
        assert app1.pause_btn.cget("text") == "Pause"
        app1._runs.set_running_state(False)

        # Simulate unresolved & completed queue records before restart
        j1 = QueuedJob(job_id="j_pend", run_id=1, url="https://librivox.org/pending", options=build_default_options(), interaction=MagicMock())
        j2 = QueuedJob(job_id="j_done", run_id=1, url="https://librivox.org/done", options=build_default_options(), interaction=MagicMock())
        app1.download_page.queue_panel.register_queue_job(j1)
        app1.download_page.queue_panel.register_queue_job(j2)
        app1.download_page.queue_panel.update_queue_row("j_done", "Completed", "1 ok")
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
        assert app2.queue_tree.set(children[0], "url") == "https://librivox.org/pending"
        assert app2.queue_tree.set(children[0], "status") == "Interrupted"
        assert app2.controller.retryable_urls(children) == ["https://librivox.org/pending"]
    finally:
        root2.destroy()


@pytest.mark.gui
def test_gui_queue_panel_tracks_stage_subprogress(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        panel = app.download_page.queue_panel
        panel.prepare_run_ui_state(1, 1, "--- Starting ---")
        assert panel.stage_var.get() == "Stage: Preparing"

        panel.handle_activity_event(
            BatchItemEvent(run_id=1, item_index=1, item_total=1, target_url="https://librivox.org/book")
        )
        assert panel.stage_var.get() == "Stage: Fetching metadata"

        panel.handle_activity_event(ProgressStartEvent(description="Downloading Book", total=10.0))
        assert panel.stage_var.get() == "Stage: Downloading"

        panel.handle_activity_event(LogEvent(level="INFO", message="Combining files"))
        assert panel.stage_var.get() == "Stage: Combining & muxing"

        panel.handle_activity_event(LogEvent(level="INFO", message="Enriching metadata from providers"))
        assert panel.stage_var.get() == "Stage: Enriching metadata"

        app._runs.finish_run(DownloadSummary(completed=1, failed=0, cancelled=False), None)
        assert panel.stage_var.get() == ""
    finally:
        root.destroy()


@pytest.mark.gui
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

    # Verify none of the page/dialog modules invoke Tcl (`after` / `winfo_exists`) inside worker defs
    import ast

    gui_dir = Path(__file__).resolve().parent.parent / "audiobookdl" / "gui"
    for fname in (
        "pages/settings_page.py",
        "pages/library_page.py",
        "pages/archive_tab.py",
        "pages/catalog_tab.py",
        "pages/catalog_covers.py",
        "pages/sync_tab.py",
        "pages/pack_page.py",
        "pages/accounts_page.py",
        "cookie_dialog.py",
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
            time.sleep(0.04)
        assert observed_threads == [threading.get_ident()]
    finally:
        root.destroy()


@pytest.mark.gui
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
        monkeypatch.setattr(app._options, "save_run_preferences", lambda _opts: None)
        monkeypatch.setattr(
            "audiobookdl.gui.controller.run_downloads",
            lambda *_a, **_kw: DownloadSummary(completed=1, books_completed=1),
        )
        app._preview.enqueue_from_preview("https://example.com/confirm", (src_confirm, book), None)
        import time

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and app.is_running:
            root.update()
            app._events.poll_queue()
            time.sleep(0.04)
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
        opts = build_default_options(database_directory=str(tmp_path))
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
            time.sleep(0.04)
        assert ctrl.active_job_id == jobs[1].job_id

        removed, new_total, is_running = ctrl.remove_pending_job(jobs[1].job_id)
        assert removed is True
        assert is_running is False
        assert new_total == 3

        ctrl.resume_queue()
        events: list[ControllerEvent] = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ctrl.drain_events(events.append)
            if any(isinstance(ev, RunDoneEvent) for ev in events):
                break
            time.sleep(0.04)

        assert "https://example.com/paused-removed" not in executed_urls
        assert executed_urls == [
            "https://example.com/boom",
            "https://example.com/after-boom",
        ]
        done_ev = next(ev for ev in events if isinstance(ev, RunDoneEvent))
        assert done_ev.error is None
        assert done_ev.summary.failed == 1
        assert done_ev.summary.completed == 2
        statuses = {ev.job_id: ev.status for ev in events if isinstance(ev, JobStatusEvent)}
        assert statuses[jobs[2].job_id] == "Failed"
        assert statuses[jobs[3].job_id] == "Completed"
    finally:
        gate_release.set()
        ctrl.shutdown(is_running=False, is_previewing=False)


@pytest.mark.gui
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
        app._runs.is_running = True
        app.download_page.queue_panel.register_queue_job(job_a)
        app.download_page.queue_panel.register_queue_job(job_b)
        app.download_page.queue_panel.update_queue_row("ja", "Failed", "network timeout")

        app._runs.retry_failed_queue_items()
        pending_urls = [j.url for j in ctrl.pending_jobs if j is not None]
        assert "https://librivox.org/fail-a" in pending_urls
        assert ctrl.batch_total == 3

        # Simulate nested modal wait_window during prompt_choice: logs queued afterwards still drain!
        app._events.cancel_poll_timer()
        holder: dict[str, Any] = {"value": None}
        done = threading.Event()
        ctrl.event_queue.put(
            PromptChoiceEvent(
                title="Pick One",
                choices=("A", "B"),
                default_index=0,
                holder=holder,
                done=done,
            )
        )

        def _fake_wait_window(dlg):
            ctrl.event_queue.put(LogEvent(level="INFO", message="Background progress during modal"))
            # Pump Tk timer while modal is open
            for _ in range(5):
                root.update()
                import time
                time.sleep(0.04)
            dlg.selected_index = 1
            dlg.destroy()

        monkeypatch.setattr(root, "wait_window", _fake_wait_window)
        app._events.poll_queue()
        assert done.is_set()
        assert holder["value"] == 1
        assert "Background progress during modal" in app.log_text.get("1.0", tk.END)
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
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
        app._runs.start_new_run(specs)
        assert len(save_calls) == 1

        # Series preview hoists build_options() once instead of per book in _series_book_exists
        build_calls = []
        orig_build = app._options.build_options

        def spy_build(*a, **kw):
            build_calls.append(1)
            return orig_build(*a, **kw)

        monkeypatch.setattr(app._options, "build_options", spy_build)
        app.skip_downloaded_var.set(True)
        sess = requests.Session()
        series = Series(
            title="Hoisted Series",
            books=[
                Audiobook(session=sess, metadata=AudiobookMetadata(title=f"Book {i}"), files=[], chapters=[])
                for i in range(6)
            ],
        )
        dlg = app._preview.show_preview_dialog("https://example.com/series", SimpleNamespace(name="Fake"), series)  # type: ignore[arg-type]
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
        dlg2 = app._preview.show_preview_dialog("https://example.com/series", SimpleNamespace(name="Fake"), series)  # type: ignore[arg-type]
        try:
            assert len(listdir_calls) == 1
        finally:
            if dlg2.winfo_exists():
                dlg2.destroy()

        # Coalescing save_queue_state inside drain_events prevents N writes on N job_status updates
        save_calls.clear()
        for i in range(5):
            job_id = app.controller.pending_jobs[i].job_id  # type: ignore[union-attr]
            app.controller.event_queue.put(
                JobStatusEvent(job_id=job_id, status="Cancelled", detail="Cancelled")
            )
        app.controller.drain_events(app._events.handle_event)
        assert len(save_calls) == 1
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
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


@pytest.mark.gui
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
        app.override_chapters_var.set(True)
        app.chapter_offset_ms_var.set("-250")
        app.square_cover_var.set(True)
        app.max_cover_size_var.set("1200")
        app.post_exec_var.set("echo done")
        app.upload_to_abs_var.set(True)

        opts = app.build_options(urls_override=["https://librivox.org/book-1"])
        assert opts.loudnorm is True
        assert opts.mono is True
        assert opts.chapters_from_silence is True
        assert opts.override_chapters is True
        app.no_chapters_var.set(True)
        assert str(app.download_page.options_panel._cb_override_chapters.cget("state")) == "disabled"
        app.no_chapters_var.set(False)
        assert str(app.download_page.options_panel._cb_override_chapters.cget("state")) == "normal"
        assert opts.chapter_offset_ms == -250
        assert opts.square_cover is True
        assert opts.max_cover_size == 1200
        assert opts.post_exec == "echo done"
        assert opts.upload_to_abs is True
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_dark_mode_listbox_accent_button_fonts_modality_and_pages(tmp_path):
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

        # ScrollableFrame inside top_scroll preserves existing bindings via add="+"
        root.event_generate("<MouseWheel>", delta=-120)
        assert len(pre_bound) == 1

        app.set_theme("dark")
        assert app._style.lookup("Accent.TButton", "background") == DARK_PALETTE.accent
        app.set_theme("light")
        assert app._style.lookup("Accent.TButton", "background") == LIGHT_PALETTE.accent

        # PackPage styles tk.Listbox in dark mode and keeps instance across navigation
        app.set_theme("dark")
        pack1 = app.navigate("pack")
        pack2 = app.navigate("pack")
        assert pack1 is pack2
        assert pack1._paths_listbox.cget("background") == DARK_PALETTE.surface
        assert pack1._paths_listbox.cget("foreground") == DARK_PALETTE.fg
        # Wheel event originating from another page does not scroll DownloadPage canvas
        app.download_page.top_scroll.on_mousewheel(
            SimpleNamespace(widget=pack1._paths_listbox, delta=-120)  # type: ignore[arg-type]
        )

        # LibraryPage hosts Archive, Catalog, Sync tabs without modal grabs
        lib = app.open_library_tab("archive")
        assert root.grab_current() is None
        assert lib.archive_tab.winfo_exists()

        settings_page = app.navigate("settings")
        assert settings_page.winfo_exists()
        assert root.grab_current() is None

        cat_lib = app.open_library_tab("catalog")
        cat1 = cat_lib.catalog_tab
        cat1.populate_items(
            [
                CatalogBook(title="One", url="https://librivox.org/1", author="A", source="librivox"),
            ]
        )
        cat1._tree.selection_set(())
        cat1.add_selected()
        assert "https://librivox.org/1" not in app.urls_text.get("1.0", tk.END)
        assert "Select one or more" in cat1._status_var.get()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
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

        app._sync.sync_libraries(services=["librivox"])
        assert len(started) == 1
        assert started[0].services == ["librivox"]
        assert app.is_syncing is True

        app._events.handle_event(
            SyncDoneEvent(
                cycle=SyncCycleResult(books_discovered=3, newly_added=1, downloaded=1),
                error=None,
            )
        )
        assert not app.is_syncing
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


@pytest.mark.gui
def test_gui_queue_title_resolution_and_output_path(tmp_path, monkeypatch):
    from audiobookdl.gui.controller import ControllerEventQueue, GuiLogSink

    q = ControllerEventQueue()
    sink = GuiLogSink(q)
    sink.set_active_run_id(4)
    sink.set_active_job_id("job_series")
    sink.on_item_progress(0, 3, "Chronicles")
    sink.on_item_progress(2, 3, "Book Two")
    drained: list[ControllerEvent] = []
    while not q.empty():
        drained.append(q.get_nowait())
    meta_events = [ev for ev in drained if isinstance(ev, JobMetaEvent)]
    assert meta_events == [
        JobMetaEvent(job_id="job_series", title="Chronicles", output_path=""),
        JobMetaEvent(job_id="job_series", title="Chronicles (2/3)", output_path=""),
    ]

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        out_file = tmp_path / "Resolved Title.m4b"
        out_file.write_bytes(b"audio")
        app.output_dir_var.set(str(tmp_path))
        app.urls_text.insert("1.0", "https://librivox.org/book\n")

        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None):
            logging.emit_item_progress(1, 1, "Resolved Title")
            return DownloadSummary(
                completed=1,
                failed=0,
                cancelled=False,
                books_completed=1,
                outputs=[{"title": "Resolved Title", "output_path": str(out_file)}],
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
        children = app.queue_tree.get_children()
        assert len(children) == 1
        job_id = children[0]
        rec = app.controller.queue_records[job_id]
        assert rec["title"] == "Resolved Title"
        assert rec["output_path"] == str(out_file)
        values = app.queue_tree.item(job_id, "values")
        assert isinstance(values, (tuple, list))
        assert app.queue_tree.set(job_id, "url") == "Resolved Title \u2014 https://librivox.org/book"
        assert app.queue_tree.set(job_id, "status") == "Completed"
        assert out_file.name in str(app.queue_tree.set(job_id, "detail"))
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_queue_row_actions_context_menu_double_click_and_multiselect(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        ctrl = app.controller
        panel = app.download_page.queue_panel
        interaction = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)

        out_file = tmp_path / "Finished Book.m4b"
        out_file.write_bytes(b"m4b")
        j_done = QueuedJob(
            job_id="j_done",
            run_id=1,
            url="https://librivox.org/done",
            options=app.build_options(urls_override=["https://librivox.org/done"]),
            interaction=interaction,
        )
        j_fail = QueuedJob(
            job_id="j_fail",
            run_id=1,
            url="https://librivox.org/failed",
            options=app.build_options(urls_override=["https://librivox.org/failed"]),
            interaction=interaction,
        )
        j_pend = QueuedJob(
            job_id="j_pend",
            run_id=1,
            url="https://librivox.org/pending",
            options=app.build_options(urls_override=["https://librivox.org/pending"]),
            interaction=interaction,
        )
        panel.register_queue_job(j_done, persist=False)
        panel.register_queue_job(j_fail, persist=False)
        panel.register_queue_job(j_pend, persist=False)
        panel.update_queue_row_metadata("j_done", title="Finished Book", output_path=str(out_file))
        panel.update_queue_row("j_done", "Completed", f"1 ok \u2014 {out_file.name}")
        panel.update_queue_row("j_fail", "Failed", "Network timeout")

        # Verify context menu item enablement for completed vs failed row
        app.queue_tree.selection_set("j_done")
        monkeypatch.setattr(panel._queue_menu, "tk_popup", lambda _x, _y: None)
        monkeypatch.setattr(panel._queue_menu, "grab_release", lambda: None)
        monkeypatch.setattr(app.queue_tree, "identify_row", lambda _y: "j_done")
        panel._show_queue_context_menu(SimpleNamespace(y=0, x_root=10, y_root=10))  # type: ignore[arg-type]
        assert str(panel._queue_menu.entrycget("Open Output", "state")) == "normal"
        assert str(panel._queue_menu.entrycget("Retry Selected", "state")) == "disabled"

        app.queue_tree.selection_set("j_fail")
        monkeypatch.setattr(app.queue_tree, "identify_row", lambda _y: "j_fail")
        panel._show_queue_context_menu(SimpleNamespace(y=0, x_root=10, y_root=10))  # type: ignore[arg-type]
        assert str(panel._queue_menu.entrycget("Open Output", "state")) == "disabled"
        assert str(panel._queue_menu.entrycget("Retry Selected", "state")) == "normal"
        assert str(panel._queue_menu.entrycget("Preview Metadata...", "state")) == "normal"
        assert str(panel._queue_menu.entrycget("Copy Detail / Error", "state")) == "normal"

        # Double-click completed row opens output path; double-click failed row opens JobInspectorDialog
        from audiobookdl.gui.queue_panel import JobInspectorDialog
        from audiobookdl.gui.window_manager import open_path_in_os

        opened: list[tuple[str, bool]] = []
        monkeypatch.setattr(panel, "_on_open_path", lambda p, rev=False: opened.append((p, rev)))
        monkeypatch.setattr(app.queue_tree, "identify_row", lambda _y: "j_done")
        panel._on_queue_double_click(SimpleNamespace(y=0))  # type: ignore[arg-type]
        assert opened == [(str(out_file), False)]

        # Verify app._open_path_in_os handles direct open and reveal-in-folder
        popen_calls: list[list[str]] = []
        monkeypatch.setattr("audiobookdl.gui.window_manager.subprocess.Popen", lambda cmd: popen_calls.append(list(cmd)))
        if hasattr(os, "startfile"):
            startfile_calls: list[str] = []
            monkeypatch.setattr(os, "startfile", lambda p: startfile_calls.append(p))
            open_path_in_os(app.root, str(out_file), reveal_in_folder=False)
            assert startfile_calls == [str(out_file)]
        else:
            open_path_in_os(app.root, str(out_file), reveal_in_folder=False)
            assert len(popen_calls) == 1
        open_path_in_os(app.root, str(out_file), reveal_in_folder=True)
        assert len(popen_calls) >= 1

        monkeypatch.setattr(app.queue_tree, "identify_row", lambda _y: "j_fail")
        panel._on_queue_double_click(SimpleNamespace(y=0))  # type: ignore[arg-type]
        inspectors = [w for w in root.winfo_children() if isinstance(w, JobInspectorDialog)]
        assert len(inspectors) == 1
        assert inspectors[0]._record["detail"] == "Network timeout"
        assert inspectors[0]._record["url"] == "https://librivox.org/failed"
        inspectors[0].destroy()

        # Copy URL, Copy Detail, Send URL to Input, Preview Metadata
        app.queue_tree.selection_set("j_fail")
        panel._copy_selected_urls()
        assert root.clipboard_get() == "https://librivox.org/failed"
        panel._copy_selected_detail()
        assert root.clipboard_get() == "Network timeout"
        panel._send_selected_to_urls()
        assert "https://librivox.org/failed" in app.urls_text.get("1.0", tk.END)

        previewed: list[str | None] = []
        monkeypatch.setattr(app, "preview_metadata", lambda url_override=None: previewed.append(url_override))
        panel._preview_selected_item()
        assert previewed == ["https://librivox.org/failed"]

        # Multi-select removal removes multiple selected rows at once
        app.queue_tree.selection_set(("j_done", "j_fail"))
        panel.remove_selected_item()
        assert list(app.queue_tree.get_children()) == ["j_pend"]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_catalog_browser_status_badges_hide_downloaded_and_one_click_download(tmp_path, monkeypatch):
    from audiobookdl.sources.source import CatalogBook
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        cfg_path = tmp_path / "audiobook-dl.toml"
        archive_path = tmp_path / "downloads.db"
        cfg_path.write_text(
            f'download_archive = "{str(archive_path).replace(os.sep, "/")}"\n',
            encoding="utf-8",
        )
        with DownloadArchive(str(archive_path), persistent=True) as archive:
            archive.record(
                "librivox",
                "https://librivox.org/already-done",
                title="Already Done",
                author="Author A",
                url="https://librivox.org/already-done",
                status="downloaded",
            )
            archive.record(
                "librivox",
                "https://librivox.org/pending-book",
                title="Pending Book",
                author="Author B",
                url="https://librivox.org/pending-book",
                status="pending",
            )

        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.output_dir_var.set(str(tmp_path))
        lib_page = app.open_library_tab("catalog")
        tab = lib_page.catalog_tab
        root.update_idletasks()

        books = [
            CatalogBook(
                title="Already Done",
                url="https://librivox.org/already-done",
                author="Author A",
                source="librivox",
            ),
            CatalogBook(
                title="Pending Book",
                url="https://librivox.org/pending-book",
                author="Author B",
                source="librivox",
            ),
            CatalogBook(
                title="New Discovery",
                url="https://librivox.org/new-discovery",
                author="Author C",
                source="librivox",
            ),
        ]
        tab.populate_items(books)

        v0 = tab._tree.item("book-0", "values")
        v1 = tab._tree.item("book-1", "values")
        v2 = tab._tree.item("book-2", "values")
        assert isinstance(v0, (tuple, list))
        assert isinstance(v1, (tuple, list))
        assert isinstance(v2, (tuple, list))
        assert v0[3] == "Downloaded"
        assert v1[3] == "Pending"
        assert v2[3] == ""
        assert "downloaded" in tab._tree.item("book-0", "tags")
        assert "pending" in tab._tree.item("book-1", "tags")

        # Hide Downloaded filters out book-0 while preserving book-1/book-2 iids
        tab._hide_downloaded_var.set(True)
        tab._on_filter_toggled()
        assert list(tab._tree.get_children()) == ["book-1", "book-2"]
        assert "downloaded hidden" in tab._status_var.get()

        # Save to Library DB immediately refreshes book-2 status badge to Pending
        tab._tree.selection_set(("book-2",))
        tab.save_to_library()
        v2_saved = tab._tree.item("book-2", "values")
        assert isinstance(v2_saved, (tuple, list))
        assert v2_saved[3] == "Pending"

        # 1-click Download Selected queues and downloads without needing urls_text
        def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, pause_event=None, sink=None):
            return DownloadSummary(completed=1, failed=0, cancelled=False, books_completed=1)

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        tab._tree.selection_set(("book-2",))
        tab.download_selected()
        assert tab.winfo_exists()
        assert "https://librivox.org/new-discovery" not in app.urls_text.get("1.0", tk.END)

        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert not app.is_running
        q_children = app.queue_tree.get_children()
        assert len(q_children) == 1
        q_rec = app.controller.queue_records[q_children[0]]
        assert q_rec["url"] == "https://librivox.org/new-discovery"
        assert q_rec["status"] == "Completed"
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_retry_preserves_profile_and_selected_indices(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    seen_calls: list[tuple[str | None, list[int] | None]] = []

    def fake_run_downloads(urls, options, config, interaction=None, cancel_event=None, sink=None, selected_indices=None, **_kw):
        seen_calls.append((options.profile, list(selected_indices) if selected_indices is not None else None))
        return DownloadSummary(completed=0, failed=1, cancelled=False, book_errors=[("Book", "boom")])

    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)

        opts = app.build_options(urls_override=["https://storytel.com/series/1"])
        opts.profile = "work"
        app._runs.start_new_run([("https://storytel.com/series/1", opts, [1, 3], None)])
        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)

        assert seen_calls == [("work", [1, 3])]
        # Change current UI profile to default, then retry failed job; original profile and indices must remain.
        app.profile_var.set("default")
        app._runs.retry_failed_queue_items()
        for _ in range(100):
            root.update()
            if not app.is_running:
                break
            threading.Event().wait(0.02)
        assert seen_calls == [("work", [1, 3]), ("work", [1, 3])]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_preview_filter_focus_and_series_book_exists_for_book_id(tmp_path, monkeypatch):
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.output_dir_var.set(str(tmp_path))
        app.download_archive_var.set(True)
        with DownloadArchive(app._sync.archive_ledger_path(), persistent=True) as archive:
            archive.record("librivox", "k1", title="Book One", author="A", status="downloaded")

        b1 = BookId(id="1", title="Book One")
        b2 = BookId(id="2", title="Book Two")
        series = Series(title="My Series", books=[b1, b2])
        source = SimpleNamespace(name="LibriVox", close=lambda: None)
        dlg = app._preview.show_preview_dialog("https://librivox.org/s", source, series)  # type: ignore[arg-type]
        assert isinstance(dlg, PreviewDialog)
        assert "[Exists]" in dlg._item_labels[0]
        assert "[Exists]" not in dlg._item_labels[1]
        assert dlg._tree is not None
        dlg._tree.focus("0")
        dlg._filter_var.set("Two")
        dlg._apply_filter()
        assert dlg._tree.focus() == "1"
        dlg.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_sync_cancel_and_auth_panel_preserves_profile(tmp_path, monkeypatch):
    from audiobookdl.sync import SyncCycleResult

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        # Preserve profile and unsaved password when typing URL for already-selected service
        storytel = next(s for s in app._services if s.key == "storytel")
        app.service_label_var.set(storytel.label)
        app.profile_var.set("work")
        app.password_var.set("unsaved-pass")
        app._auth.on_urls_changed(["https://www.storytel.com/books/123"])
        assert app.profile_var.get() == "work"
        assert app.password_var.get() == "unsaved-pass"

        # Cancellable sync
        cancelled_seen = threading.Event()

        def fake_run_sync_once(sync_opts, *, config=None, cancel_event=None, interaction=None, sink=None):
            assert interaction is not None
            assert cancel_event is not None
            cancel_event.wait(timeout=2.0)
            cancelled_seen.set()
            return SyncCycleResult(cancelled=True)

        monkeypatch.setattr("audiobookdl.sync.run_sync_once", fake_run_sync_once)
        app._sync.sync_libraries()
        assert app.is_syncing is True
        assert str(app.download_page.queue_panel.cancel_btn.cget("state")) == "normal"
        app.cancel_download()
        assert cancelled_seen.wait(timeout=2.0)
        for _ in range(100):
            root.update()
            if not app.is_syncing:
                break
            threading.Event().wait(0.02)
        assert not app.is_syncing
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_m2_sorting_collapsible_dependencies_log_batch_and_tokens(tmp_path):
    from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. ScrolledTreeview sorting preserves groups and handles numeric & duration columns
        st = ScrolledTreeview(
            root,
            (
                TreeColumnSpec("title", "Title", 120),
                TreeColumnSpec("tracks", "Tracks", 60, sort_kind="number"),
                TreeColumnSpec("dur", "Duration", 60, sort_kind="duration"),
            ),
            show_tree=True,
        )
        st.tree.insert("", "end", iid="g1", values=("Group A", "", ""), open=True)
        st.tree.insert("g1", "end", iid="b1", values=("Book 10", "10", "1:00:00"))
        st.tree.insert("g1", "end", iid="b2", values=("Book 2", "2", "05:30"))
        st.sort_by_column("tracks", descending=False)
        assert list(st.tree.get_children("g1")) == ["b2", "b1"]
        st.sort_by_column("dur", descending=True)
        assert list(st.tree.get_children("g1")) == ["b1", "b2"]
        st.sort_by_column("title", descending=False)
        assert list(st.tree.get_children("g1")) == ["b2", "b1"]  # natural sort: Book 2 < Book 10

        # 2. App layout, collapsible sections (collapsed by default), template tokens & dual preview
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        assert app.download_page.options_section.is_collapsed is True
        assert app.download_page.naming_section.is_collapsed is True
        assert app.download_page.options_section.toggle(False) is False
        assert not app.download_page.options_section.is_collapsed
        assert app.download_page.options_section.toggle(True) is True

        app.template_var.set("{series?{series}/}{title}")
        assert "Dune Chronicles" in app.template_preview_var.get()
        assert "Project Hail Mary" in app.template_standalone_preview_var.get()
        assert "Dune Chronicles" not in app.template_standalone_preview_var.get()

        # 3. OptionsPanel dynamic dependency enabling & interactive mutual exclusion
        app.enrich_metadata_var.set(False)
        assert str(app.download_page.options_panel._cb_enrich_interactive.cget("state")) == "disabled"
        app.enrich_metadata_var.set(True)
        assert str(app.download_page.options_panel._cb_enrich_interactive.cget("state")) == "normal"

        app.cover_only_var.set(True)
        assert str(app.download_page.options_panel._format_combo.cget("state")) == "disabled"
        app.cover_only_var.set(False)
        assert str(app.download_page.options_panel._format_combo.cget("state")) == "readonly"

        app.combine_var.set(True)
        app.split_chapters_var.set(True)
        app.download_page.options_panel._on_split_chapters_clicked()
        assert app.combine_var.get() is False

        # 4. Log batch coalescing & level/search filtering
        for i in range(5):
            app.controller.event_queue.put(LogEvent(level="INFO", message=f"Info line {i}"))
        app.controller.event_queue.put(LogEvent(level="ERROR", message="Failure boom"))
        app.controller.event_queue.put(LogEvent(level="DEBUG", message="Verbose trace"))
        app._events.poll_queue()
        text_all = app.log_text.get("1.0", tk.END)
        assert "Info line 4" in text_all
        assert "[ERROR] Failure boom" in text_all
        assert "[DEBUG] Verbose trace" in text_all

        app.download_page.queue_panel.log_level_var.set("Errors Only")
        app.download_page.queue_panel._rerender_logs()
        text_err = app.log_text.get("1.0", tk.END)
        assert "[ERROR] Failure boom" in text_err
        assert "Info line 4" not in text_err
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_m3_chapter_editor_preview_diff_and_pack_extended_overrides(tmp_path):
    from datetime import date
    from tkinter import ttk
    from audiobookdl.gui.chapter_editor import ChapterEditorFrame, parse_chapters_from_file
    from audiobookdl.gui.pages.pack_page import PackPage
    from audiobookdl.gui.preview_dialog import PreviewDialog, _EnrichmentPickerDialog
    from audiobookdl.utils.audiobook import Audiobook, AudiobookFile, AudiobookMetadata, Chapter
    from audiobookdl.utils.enrichment import EnrichmentCandidate

    cue_path = tmp_path / "book.cue"
    cue_path.write_text(
        'TITLE "Demo"\nFILE "demo.m4b" MP3\n  TRACK 01 AUDIO\n    TITLE "Intro"\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    TITLE "Part One"\n    INDEX 01 01:30:00\n',
        encoding="utf-8",
    )
    cue_chapters = parse_chapters_from_file(str(cue_path))
    assert [(c.start, c.title) for c in cue_chapters] == [(0, "Intro"), (90_000, "Part One")]

    json_path = tmp_path / "chapters.json"
    json_path.write_text(
        '{"chapters":[{"title":"Start","start_seconds":0},{"title":"Next","start_ms":12500}]}',
        encoding="utf-8",
    )
    json_chapters = parse_chapters_from_file(str(json_path))
    assert [(c.start, c.title) for c in json_chapters] == [(0, "Start"), (12_500, "Next")]

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. ChapterEditorFrame merge, source column & mutation
        edited_batches: list[list[Chapter]] = []
        editor = ChapterEditorFrame(root, on_change=lambda chs: edited_batches.append(list(chs)))
        editor.set_chapters(
            [Chapter(0, "Ch 1"), Chapter(10_000, "Ch 2"), Chapter(20_000, "Ch 3")],
            source_label=".cue",
        )
        assert tree_item_values(editor.tree, "0")[4] == ".cue"
        editor.tree.selection_set("0")
        editor.merge_with_next()
        assert [(c.start, c.title) for c in editor.get_chapters()] == [(0, "Ch 1"), (20_000, "Ch 3")]
        assert len(edited_batches) == 1

        # 2. _EnrichmentPickerDialog diff rendering & PreviewDialog extended fields + chapters
        ab = Audiobook(
            session=SimpleNamespace(),  # type: ignore[arg-type]
            metadata=AudiobookMetadata("Original Title", authors=["Orig Author"]),
            files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
            chapters=[Chapter(0, "Opening")],
        )
        cand = EnrichmentCandidate(
            provider="audible",
            score=0.95,
            title="Enriched Title",
            authors=["Orig Author"],
            publisher="Dream Press",
            release_date=date(2022, 7, 15),
            genres=["Fantasy"],
            asin="B0TESTASIN",
        )
        picker = _EnrichmentPickerDialog(root, [cand], current_metadata=ab.metadata)
        diff_body = picker._diff_text.get("1.0", tk.END)
        assert "Dream Press" in diff_body
        assert "Original Title" in diff_body
        picker.destroy()

        confirmed: list[tuple] = []
        preview = PreviewDialog(
            root,
            "https://librivox.org/demo",
            SimpleNamespace(name="librivox"),  # type: ignore[arg-type]
            ab,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _x: False,
            on_confirm=lambda url, pair, sel: confirmed.append((url, pair, sel)),
        )
        preview._genres_var.set("Mystery; Thriller")
        preview._publisher_var.set("Penguin Audio")
        preview._release_date_var.set("2020-03-04")
        preview._language_var.set("en")
        assert preview._desc_text is not None
        assert preview._chapter_editor is not None
        preview._desc_text.delete("1.0", tk.END)
        preview._desc_text.insert("1.0", "Custom synopsis")
        preview._chapter_editor.set_chapters([Chapter(0, "Prelude"), Chapter(30_000, "Chapter 1")])
        download_btns = [
            w
            for frame in preview.winfo_children()
            for child in frame.winfo_children()
            for w in child.winfo_children()
            if isinstance(w, ttk.Button) and w.cget("text") == "Download"
        ]
        assert len(download_btns) == 1
        download_btns[0].invoke()
        assert len(confirmed) == 1
        assert ab.metadata.genres == ["Mystery", "Thriller"]
        assert ab.metadata.publisher == "Penguin Audio"
        assert ab.metadata.release_date == date(2020, 3, 4)
        assert ab.metadata.description == "Custom synopsis"
        assert [(c.start, c.title) for c in ab.chapters] == [(0, "Prelude"), (30_000, "Chapter 1")]

        # 3. PackPage accepts initial_paths and preserves extended overrides & chapters
        from audiobookdl.gui.controller import GuiController
        from audiobookdl.options_schema import build_default_options as _build_default_options

        folder = tmp_path / "Local Book"
        folder.mkdir()
        (folder / "01.mp3").write_bytes(b"mp3")
        ctrl_pack = GuiController(config_location=str(tmp_path / "pack_cfg.toml"))
        try:
            submitted_reqs: list[Any] = []
            pack_page = PackPage(
                root,
                ctrl_pack,
                options_provider=lambda: _build_default_options(output_format="m4b"),
                output_dir_provider=lambda: str(tmp_path),
                ensure_ffmpeg=lambda _opts: True,
                on_submit=lambda reqs, fmt: submitted_reqs.append((reqs, fmt)),
                initial_paths=[str(folder)],
            )
            assert str(folder.resolve()) in pack_page._paths
            pack_page.genres_var.set("History")
            pack_page.publisher_var.set("MIT Press")
            pack_page.release_date_var.set("2019")
            pack_page.language_var.set("en")
            pack_page.description_var.set("Notes")
            book_key = pack_page._book_key_for_index(0)
            assert book_key is not None
            pack_page._edited_chapters_by_key[book_key] = [Chapter(0, "Part A")]
            ov = pack_page._build_overrides(book_idx=0)
            assert ov is not None
            assert ov.genres == ["History"]
            assert ov.publisher == "MIT Press"
            assert ov.chapters == [Chapter(0, "Part A")]
            pack_page.destroy()
        finally:
            ctrl_pack.shutdown(is_running=False, is_previewing=False)
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui_m3_queue_inspector_reorder_filter_sync_accounts_and_archive(tmp_path, monkeypatch):
    from audiobookdl.gui.pages.accounts_page import AccountsPage
    from audiobookdl.gui.pages.archive_tab import ArchiveTab
    from audiobookdl.gui.pages.sync_tab import SyncTab
    from audiobookdl.gui.queue_panel import JobInspectorDialog
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    archive_path = tmp_path / "downloads.db"
    cfg_path.write_text(
        f'download_archive = "{str(archive_path).replace(os.sep, "/")}"\n[sources.storytel.profiles.personal]\nusername = "alice"\n',
        encoding="utf-8",
    )
    with DownloadArchive(str(archive_path), persistent=True) as arch:
        missing_file = tmp_path / "gone.m4b"
        arch.record(
            "librivox",
            "https://librivox.org/missing-book",
            title="Missing Book",
            author="Author M",
            output_path=str(missing_file),
            status="downloaded",
        )

    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        ctrl = app.controller
        panel = app.download_page.queue_panel
        inter = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)

        # 1. Queue reorder to edge/target + filter & search + JobInspectorDialog
        j1 = QueuedJob("qa", 1, "https://librivox.org/a", app.build_options(["https://librivox.org/a"]), inter)
        j2 = QueuedJob("qb", 1, "https://librivox.org/b", app.build_options(["https://librivox.org/b"]), inter)
        j3 = QueuedJob("qc", 1, "https://librivox.org/c", app.build_options(["https://librivox.org/c"]), inter)
        with ctrl.queue_cond:
            ctrl.pending_jobs.extend([j1, j2, j3])
        for j in (j1, j2, j3):
            panel.register_queue_job(j, persist=False)
        panel.update_queue_row("qc", "Failed", "HTTP 500")

        assert ctrl.move_pending_job_to_edge("qa", to_top=False) == ["qb", "qc", "qa"]
        assert ctrl.move_pending_job_to_target("qc", "qb") == ["qc", "qb", "qa"]

        panel.queue_filter_var.set("Failed")
        panel._apply_queue_filter()
        assert list(app.queue_tree.get_children()) == ["qc"]
        panel.queue_filter_var.set("All")
        panel._apply_queue_filter()
        assert set(app.queue_tree.get_children()) == {"qa", "qb", "qc"}

        insp = JobInspectorDialog(root, ctrl.get_queue_record("qc") or {})
        assert "HTTP 500" in insp._record["detail"]
        insp.destroy()

        # 2. SyncTab & AccountsPage
        sync_runs: list[tuple] = []
        stab = SyncTab(
            root,
            ctrl.config_snapshot,
            app._services,
            on_run_sync=lambda targets, mode: sync_runs.append((targets, mode)),
        )
        stab.mode_var.set("dry_run")
        stab._start_sync()
        assert len(sync_runs) == 1 and sync_runs[0][1] == "dry_run"
        stab.destroy()

        apage = AccountsPage(root, ctrl, app._services, app._auth)
        assert "storytel:personal" in apage._tree.get_children()
        apage.destroy()

        # 3. ArchiveTab modeless refresh & missing file verification + callbacks
        uploaded_entries: list[Any] = []
        ltab = ArchiveTab(
            root,
            str(archive_path),
            on_upload_abs=lambda entry: uploaded_entries.append(entry),
        )
        assert root.grab_current() is None
        first_row = ltab._tree.get_children()[0]
        ltab._tree.selection_set(first_row)
        ltab._on_entry_selected()
        deadline = time.monotonic() + 2.0
        while "Calculating..." in ltab._detail_meta_var.get() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert "Missing on disk" in ltab._detail_meta_var.get()
        assert "Profile: default" in ltab._detail_meta_var.get()
        ltab._upload_selected_to_abs()
        assert len(uploaded_entries) == 1 and uploaded_entries[0].title == "Missing Book"
        res = ltab.scan_folder(str(tmp_path), background=False)
        assert res is not None
        ltab.refresh_if_open()
        ltab.destroy()

        # 3b. Queue export & import roundtrip (JSON & txt)
        from audiobookdl.gui.queue_panel import export_queue_to_path, load_queue_import_file

        with ctrl.queue_lock:
            recs = [dict(ctrl.queue_records[jid]) for jid in ("qa", "qb", "qc")]
        q_json = tmp_path / "queue.json"
        q_txt = tmp_path / "queue.txt"
        assert export_queue_to_path(recs, str(q_json)) == 3
        assert export_queue_to_path(recs, str(q_txt)) == 3
        imported_json = load_queue_import_file(str(q_json))
        imported_txt = load_queue_import_file(str(q_txt))
        assert [it["url"] for it in imported_json] == [
            "https://librivox.org/a",
            "https://librivox.org/b",
            "https://librivox.org/c",
        ]
        assert len(imported_txt) == 3

        # 4. Live URL validation highlights invalid lines, drop & paste open PackPage
        app.urls_text.delete("1.0", tk.END)
        app.urls_text.insert("1.0", "https://librivox.org/valid\nnot-a-valid-url")
        app._urls.on_urls_changed()
        assert len(app.urls_text.tag_ranges("warning_line")) == 2
        assert "1 URL" in str(app.download_page.url_frame.cget("text"))

        drop_dir = tmp_path / "DroppedBook"
        drop_dir.mkdir()
        opened_packs: list[list[str]] = []
        monkeypatch.setattr(app, "open_pack_with_paths", lambda paths: opened_packs.append(list(paths)))
        app._urls.on_url_drop(SimpleNamespace(data=f"{{{drop_dir}}}"))
        assert opened_packs == [[str(drop_dir)]]

        root.clipboard_clear()
        root.clipboard_append(str(drop_dir))
        app._urls.paste_urls_from_clipboard()
        assert opened_packs == [[str(drop_dir)], [str(drop_dir)]]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_sidebar_lazy_pages_refresh_on_visit_and_keyboard_nav(tmp_path, monkeypatch):
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    archive_path = tmp_path / "downloads.db"
    cfg_path.write_text(
        f'download_archive = "{str(archive_path).replace(os.sep, "/")}"\n',
        encoding="utf-8",
    )
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        # Only Download page is instantiated on startup
        assert app.shell.active_page_id == "download"
        assert app.shell.built_page_ids() == ("download",)

        # Menu bar keeps File, View, Help and removes Tools
        menubar_name = root.cget("menu")
        menubar = root.nametowidget(menubar_name)
        top_labels = [
            menubar.entrycget(i, "label")
            for i in range((menubar.index("end") or 0) + 1)
            if menubar.type(i) == "cascade"
        ]
        assert top_labels == ["File", "View", "Help"]

        # Lazy instantiation & keep-alive
        lib1 = app.navigate("library")
        assert set(app.shell.built_page_ids()) == {"download", "library"}
        assert len(lib1.archive_tab._tree.get_children()) == 0

        # New ledger entry written externally is picked up on next visit (on_show refresh)
        with DownloadArchive(str(archive_path), persistent=True) as arch:
            arch.record("librivox", "b1", title="Fresh Book", author="Author")
        lib2 = app.navigate("library")
        assert lib2 is lib1
        assert len(lib2.archive_tab._tree.get_children()) == 1

        acct1 = app.navigate("accounts")
        app.controller.save_source_credentials("storytel", username="bob", profile="work")
        acct2 = app.navigate("accounts")
        assert acct2 is acct1
        assert "storytel:work" in acct2._tree.get_children()

        pack1 = app.navigate("pack")
        settings1 = app.navigate("settings")
        assert pack1.winfo_exists() and settings1.winfo_exists()
        assert set(app.shell.built_page_ids()) == {"download", "library", "accounts", "pack", "settings"}

        # Sidebar keyboard navigation (Down / Up wrap-around -> Return) and Ctrl+1..5
        nav_btns = app.shell._nav_buttons
        focused_targets: list[str] = []
        for pid, btn in nav_btns.items():
            monkeypatch.setattr(btn, "focus_set", lambda p=pid: focused_targets.append(p))
        app.shell._move_nav_focus(0, 1)
        app.shell._move_nav_focus(0, -1)
        assert focused_targets == ["library", "settings"]
        app.shell._activate_from_key("pack")
        assert app.shell.active_page_id == "pack"
        for idx, pid in enumerate(("download", "library", "pack", "accounts", "settings"), start=1):
            assert root.bind(f"<Control-Key-{idx}>")
            app.navigate(pid)
            assert app.shell.active_page_id == pid
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_download_splitter_fit_to_content_and_minimum_queue_height(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        root.geometry("960x560+40+40")
        root.update_idletasks()
        dl = app.download_page
        initial_geom = root.geometry()

        dl.options_section.toggle(True)
        dl.naming_section.toggle(True)
        paned_h = 540
        collapsed_sash = dl.fit_splitter_to_top_content(total_height=paned_h)
        assert collapsed_sash is not None
        assert root.geometry() == initial_geom

        # Expanding Options grows the top pane to fit without crushing the queue below minimum
        dl.options_section.toggle(False)
        expanded_sash = dl.fit_splitter_to_top_content(total_height=paned_h)
        assert expanded_sash is not None
        assert expanded_sash > collapsed_sash
        assert root.geometry() == initial_geom
        min_queue = min(dl.MIN_QUEUE_DP, max(80, paned_h // 2))
        assert (paned_h - expanded_sash) >= min_queue

        # On a short viewport, expanding both keeps queue >= min_queue and activates internal scroll
        short_paned_h = 400
        dl.naming_section.toggle(False)
        short_sash = dl.fit_splitter_to_top_content(total_height=short_paned_h)
        assert short_sash is not None
        assert (short_paned_h - short_sash) >= min(dl.MIN_QUEUE_DP, max(80, short_paned_h // 2))
        dl.top_scroll.sync_layout(SimpleNamespace(width=700, height=short_sash))
        assert dl.top_scroll.vscroll_visible is True

        # Collapsing back on normal height shrinks top pane and hides internal scrollbar
        dl.options_section.toggle(True)
        dl.naming_section.toggle(True)
        recollapsed_sash = dl.fit_splitter_to_top_content(total_height=paned_h)
        assert recollapsed_sash is not None
        assert recollapsed_sash == collapsed_sash
        dl.top_scroll.sync_layout(SimpleNamespace(width=700, height=recollapsed_sash))
        assert not dl.top_scroll.vscroll_visible
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_window_state_persistence_roundtrip_and_monitor_clamping(tmp_path):
    from audiobookdl.gui.formatting import clamp_geometry_to_monitors, parse_geometry

    # Multi-monitor geometry parsing & clamping
    assert is_valid_geometry("800x600+-100+50") is True
    assert parse_geometry("800x600+-100+50") == (800, 600, -100, 50)
    monitors = [(0, 0, 1920, 1040), (-1920, 0, 0, 1080)]
    # Visible on secondary monitor -> stays on secondary monitor
    assert clamp_geometry_to_monitors("900x700+-1500+100", monitors) == "900x700+-1500+100"
    # Completely offscreen (unplugged monitor) -> centered on primary monitor
    assert clamp_geometry_to_monitors("900x700+2500+200", monitors) == "900x700+510+170"

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        app.download_page.options_section.toggle(False)
        app.download_page.naming_section.toggle(False)
        root.geometry("920x660+60+40")
        root.update()
        app._window.normal_geometry = "920x660+60+40"
        app._window.is_maximized = True
        app.download_page._split_ratio = 0.42
        app.download_page._user_dragged_split = True
        app.navigate("library")
        app._on_close()
    except tk.TclError:
        root.destroy()
        raise

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        gui_cfg = app2.controller.config.gui
        assert gui_cfg.window_maximized is True
        assert gui_cfg.window_geometry is not None and gui_cfg.window_geometry.startswith("920x660")
        assert gui_cfg.download_split_ratio == pytest.approx(0.42, abs=0.02)
        assert gui_cfg.active_page == "library"
        assert gui_cfg.options_collapsed is False
        assert gui_cfg.output_naming_collapsed is False
        assert app2.shell.active_page_id == "library"
        assert app2.download_page.options_section.is_collapsed is False
        assert app2.download_page.naming_section.is_collapsed is False
    finally:
        root2.destroy()


@pytest.mark.gui
def test_gui_modal_dialog_centering_clamping_grab_and_focus_restore(tmp_path, monkeypatch):
    from tkinter import ttk
    from audiobookdl.gui.formatting import parse_geometry
    from audiobookdl.gui.widgets import ModalDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        opener = app.urls_text
        monkeypatch.setattr(root, "focus_get", lambda: opener)

        dlg = ModalDialog(
            root,
            "Test Modal",
            palette=app._window.palette,
            default_size=(600, 420),
            min_size=(300, 200),
        )
        first_entry = ttk.Entry(dlg)
        first_entry.pack(padx=12, pady=12)
        dlg.finalize_modal(initial_focus=first_entry, monitors=[(0, 0, 800, 600)])
        root.update_idletasks()

        assert str(dlg.transient()) in (str(root), ".")
        assert dlg.grab_current() is dlg
        parsed = parse_geometry(dlg.geometry())
        assert parsed is not None
        w, h, x, y = parsed
        assert x is not None and y is not None
        assert 0 <= x and x + w <= 800
        assert 0 <= y and y + h <= 600

        restored_focus: list[bool] = []
        monkeypatch.setattr(opener, "focus_set", lambda: restored_focus.append(True))
        assert dlg.bind("<Escape>")
        dlg._on_escape()
        assert not dlg.winfo_exists()
        assert restored_focus == [True]

        # AboutDialog uses ModalDialog
        about = app._window.show_about_dialog()
        assert about.grab_current() is about
        assert "audiobook-dl" in about._info_text
        about.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_sidebar_badge_footer_and_ffmpeg_banner(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        dl = app.download_page
        # FFmpeg missing banner shows and can be dismissed, and hides when ready
        app._on_ffmpeg_status_changed(False, "FFmpeg: Not found")
        assert dl._ffmpeg_banner.winfo_manager() == "grid"
        dl.dismiss_ffmpeg_banner()
        assert dl._ffmpeg_banner.winfo_manager() == ""
        app._on_ffmpeg_status_changed(True, "FFmpeg: Ready")
        assert dl._ffmpeg_banner.winfo_manager() == ""

        # Download nav badge reflects active + failed counts
        badge = app.shell._badge_labels["download"]
        app.shell.set_download_badge(2, 1)
        assert badge.cget("text") == "2 queued, 1 failed"
        assert badge.winfo_manager() == "grid"
        app.shell.set_download_badge(0, 0)
        assert badge.winfo_manager() == ""
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_catalog_fetch_scopes_auth_to_catalog_service_and_shows_non_empty_error(tmp_path, monkeypatch):
    from audiobookdl.exceptions import UserNotAuthorized

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        captured: list[tuple[str, str, Any]] = []
        shown_errors: list[tuple[str, str]] = []
        monkeypatch.setattr(
            app.controller,
            "start_catalog_fetch",
            lambda mode, svc_key, query, opts, _tab, archive_path=None: captured.append(
                (mode, svc_key, opts)
            ) or 1,
        )
        monkeypatch.setattr(
            "audiobookdl.gui.app.messagebox.showerror",
            lambda title, msg, parent=None: shown_errors.append((title, msg)),
        )

        # Configure Accounts with Nextory credentials while Catalog fetches Storytel
        app._auth.select_service_profile("nextory")
        app._auth.username_var.set("nextory-user")
        app._auth.password_var.set("nextory-pass")

        lib_page = app.open_library_tab("catalog")
        cat = lib_page.catalog_tab
        cat._service_var.set("Storytel / Mofibo")
        cat.sync_buttons()
        cat.run_fetch("library")

        assert len(captured) == 1
        mode, svc_key, opts = captured[0]
        assert mode == "library"
        assert svc_key == "storytel"
        assert opts.username is None
        assert opts.password is None

        # UserNotAuthorized without a custom message still displays readable dialog text
        app.controller.catalog_request_id = 1
        app._events.handle_event(
            CatalogLoadedEvent(
                request_id=1,
                target_tab=cat,
                payload=([], None),
                error=UserNotAuthorized(
                    detail="HTTP 401 from https://api.storytel.net/libraries/bookshelf"
                ),
            )
        )
        assert shown_errors
        assert shown_errors[0][0] == "Catalog Error"
        assert "User not authorized" in shown_errors[0][1]
        assert "HTTP 401 from https://api.storytel.net/libraries/bookshelf" in shown_errors[0][1]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_catalog_displays_covers_and_ebook_formats(tmp_path):
    import io
    import time
    from PIL import Image
    from audiobookdl.gui.pages.catalog_tab import CatalogTab
    from audiobookdl.sources import ServiceInfo
    from audiobookdl.sources.source import CatalogBook

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        buf = io.BytesIO()
        Image.new("RGB", (60, 80), color=(22, 120, 200)).save(buf, format="PNG")
        sample_png = buf.getvalue()

        fetched_urls: list[str] = []

        def fake_cover_fetch(url: str) -> bytes | None:
            fetched_urls.append(url)
            return sample_png if "cover-1" in url else None

        svc = ServiceInfo(
            key="storytel",
            label="Storytel / Mofibo",
            auth_methods=("login",),
            login_fields=("username", "password"),
            supports_search=True,
            supports_library=True,
        )
        tab = CatalogTab(
            root,
            [svc],
            svc,
            on_fetch=lambda *_a: None,
            on_add_urls=lambda *_a: None,
            cover_fetcher=fake_cover_fetch,
        )
        try:
            assert tab._tree.heading("#0", "text") == "Cover"
            books = [
                CatalogBook(
                    title="Dual Format",
                    url="https://www.storytel.com/en/books/1",
                    author="Alice",
                    source="storytel",
                    cover_url="https://covers.example.com/cover-1.jpg",
                    formats=("audiobook", "ebook"),
                ),
                CatalogBook(
                    title="Audio Only",
                    url="https://www.storytel.com/en/books/2",
                    author="Bob",
                    source="storytel",
                    formats=("audiobook",),
                ),
            ]
            tab.populate_items(books)
            assert tree_item_values(tab._tree, "book-0")[4] == "Audiobook + Ebook"
            assert tree_item_values(tab._tree, "book-1")[4] == "Audiobook"
            assert "1 with ebook" in tab._status_var.get()

            # Wait briefly for background cover fetch + PhotoImage update
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                root.update()
                if "https://covers.example.com/cover-1.jpg" in tab._covers.url_photos:
                    break
                time.sleep(0.04)

            assert "https://covers.example.com/cover-1.jpg" in fetched_urls
            assert "https://covers.example.com/cover-1.jpg" in tab._covers.url_photos
            expected_photo_name = str(tab._covers.url_photos["https://covers.example.com/cover-1.jpg"])
            row_images = tab._tree.item("book-0", "image")
            if isinstance(row_images, (tuple, list)):
                assert expected_photo_name in row_images
            else:
                assert str(row_images) == expected_photo_name

            # Has Ebook filter shows only books with an ebook
            tab._has_ebook_var.set(True)
            tab._on_filter_toggled()
            assert list(tab._tree.get_children()) == ["book-0"]
            assert "Showing 1 of 2" in tab._status_var.get()
        finally:
            if tab.winfo_exists():
                tab.destroy()
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui01_apply_theme_hook_preserves_palette_and_chapter_modal(tmp_path):
    from audiobookdl.gui.chapter_editor import _ChapterEditDialog
    from audiobookdl.gui.theme import DARK_PALETTE, LIGHT_PALETTE, ThemePalette, apply_theme_to_widget_tree

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        pack_page = app.navigate("pack")
        accounts_page = app.navigate("accounts")
        settings_page = app.navigate("settings")
        dl_page = app.navigate("download")
        app.navigate("library")

        for mode, expected in (("dark", DARK_PALETTE), ("light", LIGHT_PALETTE)):
            palette = app.set_theme(mode)
            assert isinstance(palette, ThemePalette)
            assert isinstance(pack_page._palette, ThemePalette)
            assert pack_page._palette == expected
            assert isinstance(pack_page._chapter_editor._palette, ThemePalette)
            assert pack_page._chapter_editor._palette == expected
            assert isinstance(settings_page._palette, ThemePalette)
            assert settings_page._palette == expected
            assert isinstance(accounts_page._palette, ThemePalette)
            assert accounts_page._palette == expected
            assert dl_page.top_scroll.canvas.cget("background") == expected.bg
            assert settings_page._scroll.canvas.cget("background") == expected.bg

        # Opening chapter edit modal after theme switching must not fail with str.mode error
        dlg = _ChapterEditDialog(
            pack_page._chapter_editor,
            Chapter(0, "Chapter 1"),
            palette=pack_page._chapter_editor._palette,
        )
        assert isinstance(dlg._palette, ThemePalette)
        dlg.destroy()

        # Hook type errors should fail loudly rather than being silently swallowed
        bad_frame = tk.Frame(root)
        bad_frame.apply_theme = lambda _palette: (_ for _ in ()).throw(TypeError("bad hook"))  # type: ignore[attr-defined]
        with pytest.raises(TypeError, match="bad hook"):
            apply_theme_to_widget_tree(bad_frame, LIGHT_PALETTE)
        bad_frame.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui02_non_download_startup_no_duplicate_queue_and_lazy_download_page(tmp_path):
    import json

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('[gui]\nactive_page = "library"\n', encoding="utf-8")
    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    queue_file = db_dir / "gui_queue.json"
    queue_file.write_text(
        json.dumps([
            {"job_kind": "download", "url": "https://librivox.org/b1", "service": "Librivox", "status": "Interrupted", "detail": "Interrupted"},
            {"job_kind": "download", "url": "https://librivox.org/b2", "service": "Librivox", "status": "Failed", "detail": "HTTP 500"},
        ]),
        encoding="utf-8",
    )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        assert app.shell.active_page_id == "library"
        assert app.shell.built_page_ids() == ("library",)
        assert len(app.controller.queue_records) == 2

        # Logging while on a non-download page buffers without instantiating DownloadPage
        app.controller.sink.on_log("INFO", "Background status check")
        app._events.append_log("Direct buffered log")
        app._events.poll_queue()
        assert app.shell.built_page_ids() == ("library",)

        # Repeated load_saved_queue_records calls remain idempotent
        again = app.controller.load_saved_queue_records()
        assert len(again) == 2
        assert len(app.controller.queue_records) == 2

        # Navigating to Download builds it once with exact queue rows and buffered logs
        dl_page = app.navigate("download")
        assert len(dl_page.queue_panel.queue_tree.get_children()) == 2
        log_contents = dl_page.queue_panel.log_text.get("1.0", tk.END)
        assert "Background status check" in log_contents
        assert "Direct buffered log" in log_contents

        # Saving on close writes 2 records, not 4
        app._on_close(force=True)
        persisted = json.loads(queue_file.read_text(encoding="utf-8"))
        assert len(persisted) == 2
    finally:
        try:
            if root.winfo_exists():
                app.controller.shutdown(is_running=False, is_previewing=False)
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_gui03_accounts_test_all_logins_uses_gui_interaction(tmp_path, monkeypatch):
    import time
    from tkinter import simpledialog

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.storytel]\nusername = "reader@example.com"\npassword = "secret"\n',
        encoding="utf-8",
    )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        seen_handlers: list[Any] = []
        monkeypatch.setattr(simpledialog, "askstring", lambda *_a, **_kw: "123456")

        def fake_verify(svc_key, _opts, _cfg, *, interaction=None, cancel_event=None):
            seen_handlers.append(interaction)
            assert interaction is not None
            assert isinstance(getattr(interaction, "_inner", interaction), GuiInteractionHandler)
            code = interaction.ask_input("2FA Code")
            return svc_key == "storytel" and code == "123456"

        monkeypatch.setattr("audiobookdl.gui.controller.verify_service_auth", fake_verify)
        acct_page = app.navigate("accounts")
        acct_page._test_all_logins()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            root.update()
            if not acct_page._is_testing:
                break
            time.sleep(0.04)

        assert acct_page._is_testing is False
        assert len(seen_handlers) == 1
        row_vals = acct_page._tree.item("storytel:default", "values")
        assert row_vals[5] == "Valid"
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui04_pack_page_per_book_chapters_and_overrides_in_batch(tmp_path):
    from audiobookdl.gui.pages.pack_page import PackPage
    from audiobookdl.pack import PackBookSpec

    b1 = tmp_path / "Book1"
    b2 = tmp_path / "Book2"
    b1.mkdir()
    b2.mkdir()
    t1 = b1 / "01.mp3"
    t2 = b2 / "01.mp3"
    t1.write_bytes(b"mp3")
    t2.write_bytes(b"mp3")

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        submitted: list[tuple[list[Any], str]] = []
        pack_page = PackPage(
            root,
            app.controller,
            options_provider=lambda: build_default_options(output_format="m4b"),
            output_dir_provider=lambda: str(tmp_path),
            ensure_ffmpeg=lambda _opts: True,
            on_submit=lambda reqs, fmt: submitted.append((reqs, fmt)),
        )
        pack_page._append_path(str(b1))
        pack_page._append_path(str(b2))

        sess = requests.Session()
        spec0 = PackBookSpec(
            source_root=str(b1),
            track_paths=[str(t1)],
            audiobook=Audiobook(
                session=sess,
                metadata=AudiobookMetadata(title="Book One", authors=["Author One"]),
                files=[],
                chapters=[Chapter(0, "Orig 1")],
            ),
            output_path=str(tmp_path / "Book One.m4b"),
            output_format="m4b",
            total_duration_seconds=10.0,
            chapters_source="tracks",
        )
        spec1 = PackBookSpec(
            source_root=str(b2),
            track_paths=[str(t2)],
            audiobook=Audiobook(
                session=sess,
                metadata=AudiobookMetadata(title="Book Two", authors=["Author Two"]),
                files=[],
                chapters=[Chapter(0, "Orig 2")],
            ),
            output_path=str(tmp_path / "Book Two.m4b"),
            output_format="m4b",
            total_duration_seconds=12.0,
            chapters_source="tracks",
        )
        pack_page._preview_specs = [spec0, spec1]
        for idx, spec in enumerate(pack_page._preview_specs):
            pack_page._tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(spec.audiobook.title, spec.audiobook.metadata.author, 1, 1, "0:10", spec.output_path),
            )

        # Edit Book 0 chapters
        pack_page._tree.selection_set("0")
        pack_page._on_book_selected()
        edited_b0 = [Chapter(0, "Custom B0 Ch1"), Chapter(4000, "Custom B0 Ch2")]
        pack_page._on_chapters_edited(edited_b0)

        # Selecting Book 1 must display Book 1's chapters, not Book 0's
        pack_page._tree.selection_set("1")
        pack_page._on_book_selected()
        assert pack_page._chapter_editor.get_chapters() == [Chapter(0, "Orig 2")]

        # Apply per-book metadata override to Book 1
        pack_page._apply_book_override(
            1,
            {"title": "Book Two Revised", "authors": ["Jane Doe"], "series_order": 2},
        )
        pack_page.series_var.set("Shared Series")

        # Scan-scoped overrides never leak Book 0's chapters globally
        scan_ov = pack_page._build_overrides(book_idx=None)
        assert scan_ov is not None and scan_ov.chapters is None

        pack_page._submit()
        assert len(submitted) == 1
        reqs, fmt = submitted[0]
        assert fmt == "m4b"
        assert len(reqs) == 2
        assert reqs[0].overrides is not None
        assert reqs[0].overrides.series == "Shared Series"
        assert reqs[0].overrides.chapters == edited_b0
        assert reqs[1].label == "Book Two Revised"
        assert reqs[1].overrides is not None
        assert reqs[1].overrides.series == "Shared Series"
        assert reqs[1].overrides.chapters is None
        assert reqs[1].overrides.title == "Book Two Revised"
        assert reqs[1].overrides.authors == ["Jane Doe"]
        assert reqs[1].overrides.series_order == 2
        pack_page.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui05_queue_sorting_disabled_filter_safe_move_and_drag_threshold(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        ctrl = app.controller
        panel = app.download_page.queue_panel
        assert panel._scrolled_queue._enable_sorting is False

        inter = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
        ja = QueuedJob("qa", 1, "https://librivox.org/a", app.build_options(["https://librivox.org/a"]), inter)
        jb = QueuedJob("qb", 1, "https://librivox.org/b", app.build_options(["https://librivox.org/b"]), inter)
        jc = QueuedJob("qc", 1, "https://librivox.org/c", app.build_options(["https://librivox.org/c"]), inter)
        with ctrl.queue_cond:
            ctrl.pending_jobs.extend([ja, jb, jc])
        for j in (ja, jb, jc):
            panel.register_queue_job(j, persist=False)
        panel.update_queue_row_metadata("qa", title="Visible A")
        panel.update_queue_row_metadata("qb", title="Hidden B")
        panel.update_queue_row_metadata("qc", title="Visible C")

        # Calling sort_by_column on queue_tree is a no-op
        panel._scrolled_queue.sort_by_column("url")
        assert list(app.queue_tree.get_children()) == ["qa", "qb", "qc"]

        # Move Up while middle neighbor qb is detached by search filter must not raise TclError
        panel.queue_search_var.set("Visible")
        panel._apply_queue_filter()
        assert list(app.queue_tree.get_children()) == ["qa", "qc"]
        app.queue_tree.selection_set("qc")
        panel.move_selected_item(-1)
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["qa", "qc", "qb"]
        assert list(app.queue_tree.get_children()) == ["qa", "qc"]
        panel.move_selected_item(-1)
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["qc", "qa", "qb"]
        assert list(app.queue_tree.get_children()) == ["qc", "qa"]

        # Clear filter and test drag threshold + hover highlight
        panel.queue_search_var.set("")
        panel._apply_queue_filter()
        assert list(app.queue_tree.get_children()) == ["qc", "qa", "qb"]

        monkeypatch.setattr(app.queue_tree, "identify_region", lambda _x, _y: "cell")
        # Micro-jitter click (< 6px) must NOT reorder
        monkeypatch.setattr(app.queue_tree, "identify_row", lambda y: "qb" if y < 15 else "qc")
        panel._on_queue_drag_start(SimpleNamespace(x=10, y=10))  # type: ignore[arg-type]
        panel._on_queue_drag_drop(SimpleNamespace(x=10, y=13))  # type: ignore[arg-type]
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["qc", "qa", "qb"]

        # Drag >= 6px highlights hover target and reorders on drop
        monkeypatch.setattr(app.queue_tree, "identify_row", lambda y: "qb" if y < 20 else "qc")
        panel._on_queue_drag_start(SimpleNamespace(x=10, y=10))  # type: ignore[arg-type]
        panel._on_queue_drag_motion(SimpleNamespace(x=10, y=40))  # type: ignore[arg-type]
        assert "drag_target" in app.queue_tree.item("qc", "tags")
        panel._on_queue_drag_drop(SimpleNamespace(x=10, y=40))  # type: ignore[arg-type]
        assert "drag_target" not in app.queue_tree.item("qc", "tags")
        assert [j.job_id for j in ctrl.pending_jobs if j is not None] == ["qb", "qc", "qa"]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_06_persists_sync_schedule_clipboard_monitor_and_check_updates(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.storytel]\nusername = "alice"\n[sources.everand.profiles.work]\nusername = "bob"\n',
        encoding="utf-8",
    )
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        lib_page = app.navigate("library")
        sync_tab = lib_page.sync_tab
        sync_tab.auto_sync_var.set(True)
        sync_tab.interval_var.set("15")
        sync_tab.mode_var.set("scan_only")
        for svc, _prof, var in sync_tab._target_vars:
            var.set(svc == "everand")
        sync_tab._save_schedule()
        assert "Saved auto-sync schedule (every 15 min)" in app.status_var.get()

        app.navigate("settings")
        app.clipboard_monitor_var.set(True)
        app._toggle_clipboard_monitor()
        app.check_updates_var.set(False)
        app._toggle_check_updates()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()

    # Reopen app from disk and confirm everything persisted & restored
    root2 = tk.Tk()
    root2.withdraw()
    app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
    try:
        assert app2.clipboard_monitor_var.get() is True
        assert app2._automation.clipboard_after_id is not None
        assert app2.check_updates_var.get() is False
        assert app2._automation.auto_sync_enabled is True
        assert app2._automation.auto_sync_minutes == 15
        assert app2._automation.auto_sync_mode == "scan_only"
        assert app2._automation.auto_sync_targets == [("everand", "work")]
        assert app2._automation.auto_sync_after_id is not None

        lib_page2 = app2.navigate("library")
        sync_tab2 = lib_page2.sync_tab
        assert sync_tab2.auto_sync_var.get() is True
        assert sync_tab2.interval_var.get() == "15"
        assert sync_tab2.mode_var.get() == "scan_only"
        states2 = {(svc, prof): var.get() for svc, prof, var in sync_tab2._target_vars}
        assert states2.get(("everand", "work")) is True
        assert states2.get(("storytel", "default")) is False
    finally:
        app2.controller.shutdown(is_running=False, is_previewing=False)
        root2.destroy()


@pytest.mark.gui
def test_gui_07_archive_tab_async_selection_and_verify_missing(tmp_path):
    from audiobookdl.gui.pages.archive_tab import ArchiveTab
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    archive_path = tmp_path / "downloads.db"
    existing_dir = tmp_path / "ExistingBook"
    existing_dir.mkdir()
    (existing_dir / "track.mp3").write_bytes(b"x" * 2048)
    missing_file = tmp_path / "missing_book.m4b"

    with DownloadArchive(str(archive_path), persistent=True) as arch:
        id_existing = arch.record(
            "librivox",
            "https://librivox.org/existing",
            title="Existing Book",
            output_path=str(existing_dir),
            status="downloaded",
        )
        id_missing = arch.record(
            "librivox",
            "https://librivox.org/missing",
            title="Missing Book",
            output_path=str(missing_file),
            status="downloaded",
        )

    tab = ArchiveTab(root, str(archive_path))
    try:
        # Rapidly switch selection from missing -> existing; stale token ensures existing wins
        tab._tree.selection_set(str(id_missing))
        tab._on_entry_selected()
        assert "Calculating..." in tab._detail_meta_var.get()

        tab._tree.selection_set(str(id_existing))
        tab._on_entry_selected()
        assert "Calculating..." in tab._detail_meta_var.get()

        deadline = time.monotonic() + 2.0
        while "Calculating..." in tab._detail_meta_var.get() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        meta = tab._detail_meta_var.get()
        assert "2.0 KB" in meta
        assert str(existing_dir) in meta

        # Background Verify Missing marks missing_book as pending without freezing main thread
        tab._verify_missing_files()
        deadline = time.monotonic() + 2.0
        while "marked" not in tab._status_var.get() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert "marked 1 missing file(s) as pending" in tab._status_var.get()
        assert tab._entries_by_id[id_missing].status == "pending"
        assert tab._entries_by_id[id_existing].status == "downloaded"
    finally:
        tab.destroy()
        root.destroy()


@pytest.mark.gui
def test_gui_08_debounced_filters_and_coalesced_log_inserts(tmp_path, monkeypatch):
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.utils.audiobook import BookId, Series

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        panel = app.download_page.queue_panel
        insert_calls: list[tuple] = []
        orig_insert = panel.log_text.insert

        def _spy_insert(index, *args):
            insert_calls.append((index, args))
            return orig_insert(index, *args)

        monkeypatch.setattr(panel.log_text, "insert", _spy_insert)

        # Batch of 100 entries with alternating tags coalesces cleanly into 1 Tcl insert call
        batch = [
            ("INFO", f"info line {i}") if i < 50 else ("ERROR", f"err line {i}")
            for i in range(100)
        ]
        panel.append_log_batch(batch)
        assert len(insert_calls) == 1
        assert len(insert_calls[0][1]) == 4  # (info_text, ('info',), err_text, ('error',))

        # Typing in log_search_var schedules debounced rerender instead of synchronous per-keystroke rebuild
        insert_calls.clear()
        panel.log_search_var.set("err")
        assert len(insert_calls) == 0
        assert panel._log_search_after_id is not None
        deadline = time.monotonic() + 1.0
        while panel._log_search_after_id is not None and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert len(insert_calls) == 1
        assert "err line 50" in panel.log_text.get("1.0", tk.END)
        assert "info line 0" not in panel.log_text.get("1.0", tk.END)

        # Typing in queue_search_var schedules debounced filter
        panel.queue_search_var.set("needle")
        assert panel._queue_filter_after_id is not None
        panel._cancel_after("_queue_filter_after_id")

        # PreviewDialog series filter debounces _filter_var
        series = Series(
            title="Test Series",
            books=[BookId(id="1", title="Alpha"), BookId(id="2", title="Beta")],
        )
        source = SimpleNamespace(name="LibriVox", close=lambda: None)
        dlg = PreviewDialog(
            root,
            "https://librivox.org/s",
            source,  # type: ignore[arg-type]
            series,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_args: None,
        )
        try:
            dlg._filter_var.set("Beta")
            assert dlg._filter_after_id is not None
            dlg_tree = dlg._tree
            assert dlg_tree is not None
            assert list(dlg_tree.get_children()) == ["0", "1"]  # not yet filtered synchronously
            deadline = time.monotonic() + 1.0
            while dlg._filter_after_id is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            assert list(dlg_tree.get_children()) == ["1"]
        finally:
            dlg.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_09_preview_dialog_metadata_tab_uses_scrollable_frame(tmp_path):
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.gui.theme import DARK_PALETTE
    from audiobookdl.gui.widgets import ScrollableFrame
    from audiobookdl.utils.audiobook import Audiobook, AudiobookFile, AudiobookMetadata, Chapter

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        meta = AudiobookMetadata(
            title="Long Metadata Book",
            authors=["Author One"],
            description="Paragraph\n" * 20,
        )
        ab = Audiobook(
            session=SimpleNamespace(),  # type: ignore[arg-type]
            metadata=meta,
            files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
            chapters=[Chapter(0, "Opening"), Chapter(60000, "Chapter 2")],
        )
        source = SimpleNamespace(name="LibriVox", close=lambda: None)
        dlg = PreviewDialog(
            root,
            "https://librivox.org/book",
            source,  # type: ignore[arg-type]
            ab,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_args: None,
            palette=DARK_PALETTE,
        )
        try:
            assert isinstance(dlg._meta_scroll, ScrollableFrame)
            assert dlg._desc_text is not None
            assert dlg._readonly_frame is not None
            assert dlg._readonly_frame.master is dlg._meta_scroll.content_frame
            assert str(dlg._meta_scroll.canvas.cget("background")) == DARK_PALETTE.bg

            # Under constrained height, ScrollableFrame activates vertical scrollbar
            dlg.update_idletasks()
            req_h = dlg._meta_scroll.content_frame.winfo_reqheight()
            assert req_h > 200
            dlg._meta_scroll.sync_layout(SimpleNamespace(width=520, height=200))
            assert dlg._meta_scroll.vscroll_visible is True
            dlg._meta_scroll.sync_layout(SimpleNamespace(width=520, height=req_h + 40))
            assert not dlg._meta_scroll.vscroll_visible
        finally:
            dlg.destroy()
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui_10_dropped_txt_url_file_and_local_folder(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        url_list_file = tmp_path / "urls.txt"
        url_list_file.write_text(
            "# comment\nhttps://librivox.org/book-1\nhttps://librivox.org/book-2\n",
            encoding="utf-8",
        )
        pack_dir = tmp_path / "LocalAlbum"
        pack_dir.mkdir()
        opened_packs: list[list[str]] = []
        monkeypatch.setattr(app, "open_pack_with_paths", lambda p: opened_packs.append(list(p)))

        app._urls.on_url_drop(SimpleNamespace(data=f"{{{url_list_file}}} {{{pack_dir}}}"))
        urls_box_text = app.urls_text.get("1.0", tk.END)
        assert "https://librivox.org/book-1" in urls_box_text
        assert "https://librivox.org/book-2" in urls_box_text
        assert opened_packs == [[str(pack_dir)]]
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_gui_11_controller_fetch_cover_bytes_uses_pooled_session_proxy_and_cap(tmp_path):
    from audiobookdl.gui.controller import GuiController

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('proxy = "http://127.0.0.1:8888"\ntimeout = 12.0\n', encoding="utf-8")
    ctrl = GuiController(str(cfg_path))
    seen_calls: list[dict[str, Any]] = []

    class _FakeResp:
        def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
            self.status_code = status_code
            self.headers = headers
            self._chunks = chunks

        def iter_content(self, chunk_size: int = 65536):
            return iter(self._chunks)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    fake_session = SimpleNamespace(
        get=lambda url, **kw: (
            seen_calls.append({"url": url, **kw}),
            _FakeResp(200, {"Content-Length": "4"}, [b"PNG!"]),
        )[1],
        close=lambda: None,
    )
    ctrl._thread_local.http_session = fake_session
    ctrl._thread_sessions.append(fake_session)

    try:
        data = ctrl.fetch_cover_bytes("https://cdn.example.com/cover.png")
        assert data == b"PNG!"
        assert len(seen_calls) == 1
        assert seen_calls[0]["proxies"] == {
            "http": "http://127.0.0.1:8888",
            "https": "http://127.0.0.1:8888",
        }
        assert seen_calls[0]["timeout"] == 12.0
        assert seen_calls[0]["stream"] is True

        # Rejects oversized Content-Length without reading body
        fake_session.get = lambda url, **kw: _FakeResp(
            200, {"Content-Length": str(10 * 1024 * 1024)}, [b"x"]
        )
        assert ctrl.fetch_cover_bytes("https://cdn.example.com/huge.png") is None
    finally:
        ctrl.shutdown(is_running=False, is_previewing=False)


@pytest.mark.gui
def test_gui_13_catalog_and_accounts_cancel_and_scrollable_router_cleanup(tmp_path):
    from audiobookdl.gui.widgets import ScrollableFrame, _SCROLLABLE_TOPLEVEL_ROUTES

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('[sources.storytel]\nusername = "alice"\n', encoding="utf-8")
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        # 1. ScrollableFrame registers in router and unregisters cleanly on destroy
        sf = ScrollableFrame(root)
        assert any(r() is sf for r in _SCROLLABLE_TOPLEVEL_ROUTES.get(root, []))
        sf.destroy()
        assert not any(r() is sf for r in _SCROLLABLE_TOPLEVEL_ROUTES.get(root, []))

        # 2. CatalogTab Cancel button cancels controller request and restores buttons
        cancelled: list[bool] = []
        lib = app.open_library_tab("catalog")
        cat = lib.catalog_tab
        orig_cancel = app.controller.cancel_catalog_fetch
        app.controller.cancel_catalog_fetch = lambda: (cancelled.append(True), orig_cancel())[1]
        cat.run_fetch("library")
        assert str(cat._cancel_btn.cget("state")) == "normal"
        cat._cancel_btn.invoke()
        assert cancelled == [True]
        assert str(cat._cancel_btn.cget("state")) == "disabled"
        assert "cancelled" in cat._status_var.get().lower()

        # 3. AccountsPage Cancel Test stops remaining verifications
        apage = app.open_accounts()
        checked_services: list[str] = []
        entered = threading.Event()
        release = threading.Event()

        def _slow_verify(svc_key, *_a, **_kw):
            checked_services.append(svc_key)
            entered.set()
            release.wait(timeout=2.0)
            return True

        with patch("audiobookdl.gui.controller.verify_service_auth", side_effect=_slow_verify):
            apage._test_all_logins()
            assert entered.wait(timeout=2.0)
            apage._cancel_test_logins()
            release.set()
            deadline = time.monotonic() + 2.0
            while apage._is_testing and time.monotonic() < deadline:
                app._events.poll_queue()
                root.update()
                time.sleep(0.01)
        assert "cancelled" in apage._status_var.get().lower()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_14_scrollable_frame_wheel_guard_does_not_mutate_combobox_or_spinbox(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        dl = app.download_page
        dl.options_section.toggle(False)
        dl.top_scroll.guard_scroll_inputs()
        combo = dl.options_panel._format_combo
        before_fmt = app.output_format_var.get()
        assert combo in dl.top_scroll._guarded_inputs

        # Simulate wheel event on unfocused Combobox -> returns 'break' without mutating value
        res = dl.top_scroll._on_guarded_input_wheel(
            SimpleNamespace(widget=combo, delta=-120, num=None)  # type: ignore[arg-type]
        )
        assert res == "break"
        assert app.output_format_var.get() == before_fmt
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_15_multi_service_auth_auto_selects_single_named_profile(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.storytel]\nusername = "main"\n[sources.everand.profiles.work]\nusername = "work-user"\n',
        encoding="utf-8",
    )
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        # Select storytel in GUI form, then build options for an everand URL
        storytel = next(s for s in app._services if s.key == "storytel")
        app.service_label_var.set(storytel.label)
        auth_kwargs = app._auth.extract_options_auth(["https://www.everand.com/audiobook/12345/demo"])
        assert auth_kwargs["profile"] == "work"
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_16_queue_profile_column_live_row_progress_and_job_options_edit(tmp_path):
    from audiobookdl.gui.queue_panel import JobInspectorDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('[sources.storytel.profiles.work]\nusername = "bob"\n', encoding="utf-8")
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        ctrl = app.controller
        panel = app.download_page.queue_panel
        inter = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
        opts = app.build_options(["https://www.storytel.com/books/1"])
        opts.profile = "work"
        job = QueuedJob("job1", 1, "https://www.storytel.com/books/1", opts, inter)
        with ctrl.queue_cond:
            ctrl.pending_jobs.append(job)
        panel.register_queue_job(job, persist=False)

        assert app.queue_tree.set("job1", "profile") == "work"

        # Simulate live Running + progress_metrics -> updates Detail cell without modifying persisted record
        panel.prepare_run_ui_state(1, 1, "--- Start ---")
        panel.handle_activity_event(JobStatusEvent(job_id="job1", status="Running", detail=""))
        panel.handle_activity_event(ProgressStartEvent(description="Downloading Book", total=100.0))
        panel.handle_activity_event(
            ProgressMetricsEvent(
                completed=50.0,
                total=100.0,
                bytes_downloaded=1024 * 1024,
                speed_bps=2048.0,
                elapsed_seconds=1.0,
                eta_seconds=1.0,
            )
        )
        live_detail = str(app.queue_tree.set("job1", "detail"))
        assert "50%" in live_detail
        assert ctrl.queue_records["job1"]["detail"] == ""

        # Edit profile & options on queued job
        panel.handle_activity_event(JobStatusEvent(job_id="job1", status="Queued", detail=""))
        updated = panel._apply_job_config_update(
            "job1",
            "default",
            {"output_format": "mp3", "combine": True, "media_type": "audiobook"},
        )
        assert updated is not None
        assert app.queue_tree.set("job1", "profile") == "default"
        assert job.options.profile is None
        assert job.options.output_format == "mp3"
        assert job.options.combine is True

        insp = JobInspectorDialog(
            root,
            updated,
            profiles=["default", "work"],
            on_save_options=panel._apply_job_config_update,
        )
        assert str(insp._edit_btn.cget("state")) == "normal"
        insp.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_17_archive_tab_pagination_and_edit_metadata(tmp_path, monkeypatch):
    from audiobookdl.gui.pages.archive_metadata_dialog import _ArchiveMetadataDialog
    from audiobookdl.gui.pages.archive_tab import ArchiveTab
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    archive_path = tmp_path / "downloads.db"
    with DownloadArchive(str(archive_path), persistent=True) as arch:
        for i in range(5):
            arch.record(
                "librivox",
                f"k{i}",
                title=f"Entry {i}",
                author="Writer",
                status="downloaded",
            )

    monkeypatch.setattr(ArchiveTab, "_PAGE_SIZE", 2)
    tab = ArchiveTab(root, str(archive_path))
    try:
        tab._group_var.set(False)
        tab._refresh()
        assert "Showing 1-2 of 5 items" in tab._status_var.get()
        assert len(tab._tree.get_children()) == 2
        assert str(tab._prev_page_btn.cget("state")) == "disabled"
        assert str(tab._next_page_btn.cget("state")) == "normal"

        tab._next_page()
        assert "Showing 3-4 of 5 items" in tab._status_var.get()
        assert str(tab._prev_page_btn.cget("state")) == "normal"

        tab._load_all_entries()
        assert "Showing 5 items" in tab._status_var.get()
        assert len(tab._tree.get_children()) == 5

        first_iid = tab._tree.get_children()[0]
        tab._tree.selection_set(first_iid)

        class _FakeMetaDlg:
            def __init__(self, *_a, **_kw) -> None:
                self.result_updates = {
                    "title": "Renamed Entry",
                    "author": "Updated Author",
                    "narrator": "Updated Narrator",
                    "series": "New Series",
                    "series_order": 2,
                    "asin": "B0EDITED",
                    "isbn": None,
                    "output_path": None,
                    "status": "pending",
                    "tags": ["reviewed"],
                }

        monkeypatch.setattr(
            "audiobookdl.gui.pages.archive_tab._ArchiveMetadataDialog",
            _FakeMetaDlg,
        )
        monkeypatch.setattr(tab, "wait_window", lambda _dlg: None)
        tab._edit_selected_metadata()
        assert tab._entries_by_id[int(first_iid)].title == "Renamed Entry"
        assert tab._entries_by_id[int(first_iid)].status == "pending"
        assert tab._entries_by_id[int(first_iid)].tags == ("reviewed",)
        _ = _ArchiveMetadataDialog
    finally:
        tab.destroy()
        root.destroy()


@pytest.mark.gui
def test_gui_18_preview_series_book_inspect_and_chapter_editor_ops(tmp_path):
    from audiobookdl.gui.chapter_editor import (
        ChapterEditorFrame,
        export_chapters_to_file,
        parse_chapters_from_file,
    )
    from audiobookdl.gui.preview_dialog import PreviewDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. ChapterEditorFrame shift & .json/.cue export round-trip
        editor = ChapterEditorFrame(root)
        editor.set_chapters([Chapter(1000, "Ch 1"), Chapter(5000, "Ch 2")])
        editor.apply_chapter_shift(mode="offset", value=2.5)
        assert [c.start for c in editor.get_chapters()] == [3500, 7500]
        editor.apply_chapter_shift(mode="scale", value=2.0, indices=[1])
        assert [c.start for c in editor.get_chapters()] == [3500, 15000]

        json_path = tmp_path / "chapters.json"
        cue_path = tmp_path / "chapters.cue"
        export_chapters_to_file(str(json_path), editor.get_chapters())
        export_chapters_to_file(str(cue_path), editor.get_chapters())
        assert parse_chapters_from_file(str(json_path)) == editor.get_chapters()
        parsed_cue = parse_chapters_from_file(str(cue_path))
        assert [c.title for c in parsed_cue] == ["Ch 1", "Ch 2"]
        assert abs(parsed_cue[0].start - 3500) <= 20
        editor.destroy()

        # 2. PreviewDialog series title editing & inspecting individual book
        resolved_book = Audiobook(
            session=SimpleNamespace(),  # type: ignore[arg-type]
            metadata=AudiobookMetadata(title="Book 1 Full", authors=["Author A"]),
            files=[AudiobookFile(url="https://librivox.org/b1.mp3", ext="mp3")],
            chapters=[Chapter(0, "Opening")],
        )
        closed_flag = {"closed": False}

        class _FakeSeriesSource:
            name = "LibriVox"

            def download_from_id(self, book_id: str) -> Audiobook:
                assert book_id == "b1"
                return resolved_book

            def close(self) -> None:
                closed_flag["closed"] = True

        series = Series(
            title="Original Series",
            books=[BookId(id="b1", title="Book 1 Stub"), BookId(id="b2", title="Book 2 Stub")],
        )
        confirmed: list[tuple[str, Any, list[int] | None]] = []
        dlg = PreviewDialog(
            root,
            "https://librivox.org/series",
            _FakeSeriesSource(),  # type: ignore[arg-type]
            series,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda u, pair, sel: confirmed.append((u, pair[1], sel)),
        )
        try:
            dlg._series_title_var.set("Renamed Series")
            sub = dlg._open_series_book_subdialog(0, resolved_book)
            try:
                assert not closed_flag["closed"]
                sub._title_var.set("Book 1 Customized")
                assert sub._apply_audiobook_edits(resolved_book) is True
                sub._source_transferred = True
                sub.destroy()
                sub._on_confirm(
                    dlg._url,
                    (dlg._source, resolved_book),
                    None,
                )
            finally:
                if sub.winfo_exists():
                    sub.destroy()
            assert not closed_flag["closed"]
            assert isinstance(series.books[0], Audiobook)
            assert series.books[0].title == "Book 1 Customized"

            # Verify Series confirm saves edited series title
            dlg._source_transferred = True
            dlg._series_ref.title = dlg._series_title_var.get().strip()  # type: ignore[union-attr]
            assert series.title == "Renamed Series"
        finally:
            if dlg.winfo_exists():
                dlg.destroy()
    finally:
        root.destroy()


def test_gui_20_typed_events_and_automation_coordinator(monkeypatch):
    from audiobookdl.gui.coordinators import AutomationCoordinator
    from audiobookdl.gui.events import LogBatchEvent, LogEvent, ProgressMetricsEvent

    log_ev = LogEvent(level="INFO", message="hi")
    assert log_ev.level == "INFO"
    batch_ev = LogBatchEvent(entries=(("INFO", "a"), ("ERROR", "b")))
    assert len(batch_ev.entries) == 2
    pm_ev = ProgressMetricsEvent(1.0, 2.0, 100, 50.0, 2.0, 2.0)
    assert pm_ev.speed_bps == 50.0

    # AutomationCoordinator captures supported URLs without duplication
    scheduled: list[Any] = []

    class _FakeRoot:
        def winfo_exists(self) -> bool:
            return True

        def clipboard_get(self) -> str:
            return "https://librivox.org/book-abc\nhttps://example.com/ignored"

        def after(self, ms: int, cb: Any) -> str:
            scheduled.append((ms, cb))
            return f"after-{len(scheduled)}"

        def after_cancel(self, _aid: str) -> None:
            return None

    appended: list[str] = []
    statuses: list[str] = []
    coord = AutomationCoordinator(
        _FakeRoot(),  # type: ignore[arg-type]
        is_clipboard_enabled=lambda: True,
        append_urls=lambda urls: (appended.extend(urls), list(urls))[1],
        save_clipboard_pref=lambda _b: None,
        set_status=statuses.append,
        can_run_auto_sync=lambda: True,
        execute_sync_targets=lambda *_a: None,
        sync_libraries=lambda: None,
    )
    coord.poll_clipboard()
    assert appended == ["https://librivox.org/book-abc"]
    assert any("Captured 1 URL(s)" in s for s in statuses)


def test_gui_21_windows_aumid_registration(monkeypatch):
    from audiobookdl.gui import notifications

    cmds: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(notifications.sys, "platform", "win32")
    monkeypatch.setattr(notifications, "ensure_windows_aumid_registered", lambda **_kw: True)
    monkeypatch.setattr(
        notifications,
        "_resolve_windows_powershell",
        lambda: ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", True),
    )
    monkeypatch.setattr(
        notifications.subprocess,
        "run",
        lambda cmd, **kw: cmds.append((list(cmd), dict(kw["env"]))) or SimpleNamespace(returncode=0),
    )
    assert notifications._send_windows_toast("Done", "Book finished", app_name="audiobook-dl", timeout=2.0) is True
    assert cmds[0][1]["ABDL_NOTIFY_APP"] == notifications.WINDOWS_APP_USER_MODEL_ID


@pytest.mark.gui
def test_gui_22_catalog_cover_width_series_order_filter_and_details():
    import io
    import time
    from tkinter import ttk
    from PIL import Image
    from audiobookdl.gui.pages.catalog_tab import CatalogTab
    from audiobookdl.gui.theme import apply_theme, scale_px
    from audiobookdl.sources import ServiceInfo
    from audiobookdl.sources.source import CatalogBook

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        style = ttk.Style(root)
        palette = apply_theme(root, style, "dark")
        assert int(style.lookup("Catalog.Treeview", "rowheight")) >= scale_px(root, 60)
        assert int(style.lookup("Catalog.Treeview", "indent")) == scale_px(root, 14)

        buf = io.BytesIO()
        Image.new("RGB", (200, 300), color=(40, 110, 190)).save(buf, format="PNG")
        sample_cover = buf.getvalue()

        svc = ServiceInfo(
            key="storytel",
            label="Storytel / Mofibo",
            auth_methods=("login",),
            login_fields=("username", "password"),
            supports_search=True,
            supports_library=True,
        )
        added_urls: list[str] = []
        tab = CatalogTab(
            root,
            [svc],
            svc,
            on_fetch=lambda *_a: None,
            on_add_urls=added_urls.extend,
            cover_fetcher=lambda url: sample_cover if "cover-1" in url else None,
            palette=palette,
        )
        tab.pack(fill=tk.BOTH, expand=True)
        root.update_idletasks()
        try:
            assert tab._covers.MAX_COVER_CACHE >= 2000
            assert tab._tree.column("#0", "width") >= scale_px(tab, 102)
            assert tab._tree.column("#0", "minwidth") >= scale_px(tab, 84)

            books = [
                CatalogBook(
                    title="O conto de Zoe",
                    url="https://www.storytel.com/br/books/o-conto-de-zoe",
                    author="John Scalzi",
                    series="Guerra do Velho",
                    series_order=4,
                    source="storytel",
                    formats=("audiobook", "ebook"),
                    cover_url="https://covers.example.com/cover-1.jpg",
                ),
                CatalogBook(
                    title="Guerra do velho",
                    url="https://www.storytel.com/br/books/guerra-do-velho",
                    author="John Scalzi",
                    series="Guerra do Velho",
                    series_order=1,
                    source="storytel",
                    formats=("audiobook",),
                    cover_url="https://covers.example.com/cover-1.jpg",
                ),
                CatalogBook(
                    title="A Very Long Standalone Title That Would Truncate In Grid",
                    url="https://www.storytel.com/br/books/standalone-long",
                    author="Ursula K. Le Guin",
                    source="storytel",
                    formats=("audiobook",),
                ),
            ]
            tab.populate_items(books, statuses=["", "Downloaded", ""])

            roots = tab._tree.get_children("")
            assert len(roots) == 2
            group_iid = roots[0]
            group_vals = tree_item_values(tab._tree, group_iid)
            assert "Guerra do Velho (2 items)" == group_vals[0]
            assert group_vals[1] == "John Scalzi"
            assert group_vals[2] == ""
            assert group_vals[3] == "1/2 downloaded"
            assert group_vals[4] == "1 w/ Ebook"

            # Series items are ordered by series_order (#1 before #4) and display #order
            children = tab._tree.get_children(group_iid)
            assert list(children) == ["book-1", "book-0"]
            assert tree_item_values(tab._tree, "book-1")[2] == "Guerra do Velho #1"
            assert tree_item_values(tab._tree, "book-0")[2] == "Guerra do Velho #4"

            # Collapse All / Expand All controls
            tab.collapse_all_groups()
            assert not tab._tree.item(group_iid, "open")
            tab.expand_all_groups()
            assert tab._tree.item(group_iid, "open")

            # Book Details pane updates on single leaf selection and fetches detail cover
            tab._tree.selection_set(("book-0",))
            tab._on_tree_selected()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                root.update()
                if "https://covers.example.com/cover-1.jpg" in tab._covers.detail_cover_png_cache:
                    break
                time.sleep(0.03)
            assert tab._detail_title_var.get() == "O conto de Zoe"
            assert "John Scalzi" in tab._detail_meta_var.get()
            assert "Guerra do Velho #4" in tab._detail_meta_var.get()
            assert "Audiobook + Ebook" in tab._detail_meta_var.get()
            assert "https://covers.example.com/cover-1.jpg" in tab._covers.detail_cover_png_cache
            assert "1 selected" in tab._status_var.get()

            # Group selection summarizes the series in Book Details
            tab._tree.selection_set((group_iid,))
            tab._on_tree_selected()
            assert "Guerra do Velho (2 items)" == tab._detail_title_var.get()
            assert "Items: 2" in tab._detail_meta_var.get()

            # Select All selects every visible leaf book
            tab.select_all_visible()
            assert len(tab._selected_books()) == 3
            assert "3 selected" in tab._status_var.get()

            # Local filter filters loaded books without wiping _loaded_books
            tab._local_filter_var.set("ursula standalone")
            tab._on_filter_toggled()
            assert list(tab._tree.get_children("")) == ["book-2"]
            assert "Showing 1 of 3" in tab._status_var.get()
            assert len(tab._loaded_books) == 3

            tab._clear_local_filter()
            assert len(tab._tree.get_children("")) == 2

            # Flat mode tightens #0 cover width while keeping minwidth >= 84
            tab._group_var.set(False)
            tab._on_group_toggled()
            assert tab._tree.column("#0", "width") >= scale_px(tab, 84)
            assert len(tab._tree.get_children("")) == 3

            # Details pane toggle
            assert tab._details_visible
            tab._toggle_details_pane()
            assert not tab._details_visible
            assert tab._details_toggle_btn.cget("text") == "Show Details"
            tab._toggle_details_pane()
            assert tab._details_visible
            assert tab._details_toggle_btn.cget("text") == "Hide Details"
        finally:
            if tab.winfo_exists():
                tab.destroy()
    finally:
        root.destroy()


@pytest.mark.gui
def test_settings_page_custom_providers_ui_and_app_integration(tmp_path, monkeypatch):
    import audiobookdl.gui.pages.settings_page as settings_mod
    from audiobookdl import Audiobook, AudiobookMetadata
    from audiobookdl.config import load_config
    from audiobookdl.credentials import PLAINTEXT_OPT_IN_ENV

    monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV, "1")
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('output_template = "{title}"\n', encoding="utf-8")

    root = tk.Tk()
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            settings_page = app.navigate("settings")
            # Initially empty
            assert list(settings_page._custom_tree.get_children()) == []
            assert "No custom" in settings_page._custom_status_var.get()

            # Save a custom provider through the Settings callback
            assert settings_page._persist_custom_provider(
                "hardcover",
                url="https://abs-hc.example.com/",
                api_key="hc-key",
                enabled=True,
            )
            settings_page._refresh_custom_providers()
            assert list(settings_page._custom_tree.get_children()) == ["hardcover"]
            assert settings_page._custom_tree.item("hardcover", "values")[2] == "Configured"

            # DownloadPage OptionsPanel hint & build_options reflect the new custom provider
            dl_page = app.navigate("download")
            assert "custom: hardcover" in dl_page.options_panel._enrich_providers_hint_var.get()
            app.enrich_providers_var.set("hardcover,itunes")
            opts = app.build_options(urls_override=[])
            assert opts.enrich_providers == "custom:hardcover,itunes"
            assert app._options.read_persisted_gui_preferences()["enrich_providers"] == "custom:hardcover,itunes"

            # Toggle Enabled persists to TOML
            app.navigate("settings")
            settings_page._custom_tree.selection_set("hardcover")
            settings_page._toggle_custom_provider()
            assert load_config(str(cfg_path)).custom_metadata_providers["hardcover"].enabled is False
            assert settings_page._custom_tree.item("hardcover", "values")[3] == "No"

            # Test Connection updates treeview status on success and error
            monkeypatch.setattr(
                settings_mod,
                "test_custom_metadata_provider",
                lambda *_a, **_kw: (3, "Connected (3 sample matches)"),
            )
            settings_page._custom_tree.selection_set("hardcover")
            settings_page._test_custom_provider()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and settings_page._testing_custom_provider:
                root.update()
                time.sleep(0.02)
            root.update()
            assert settings_page._custom_tree.item("hardcover", "values")[4] == "Connected (3 sample matches)"

            def _fail_probe(*_a, **_kw):
                raise RuntimeError("HTTP 401 Unauthorized")

            monkeypatch.setattr(settings_mod, "test_custom_metadata_provider", _fail_probe)
            settings_page._test_custom_provider()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and settings_page._testing_custom_provider:
                root.update()
                time.sleep(0.02)
            root.update()
            assert "401" in settings_page._custom_tree.item("hardcover", "values")[4]

            # Controller search_enrichment_for_audiobook routes custom providers cleanly
            captured_calls: list[Any] = []

            def _fake_search(meta, **kwargs):
                captured_calls.append((meta.title, kwargs.get("providers"), kwargs.get("custom_providers")))
                return []

            monkeypatch.setattr("audiobookdl.gui.controller.search_enrichment_candidates", _fake_search)
            dummy_book = Audiobook(session=MagicMock(), metadata=AudiobookMetadata(title="Dune"), files=[])
            app.controller.search_enrichment_for_audiobook(dummy_book, opts)
            assert len(captured_calls) == 1
            assert captured_calls[0][1] == ("custom:hardcover", "itunes")
            assert "hardcover" in captured_calls[0][2]
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_audit_regressions_queue_reset_pack_keys_sync_mode_filter_and_abs_chapters(tmp_path: Path):
    import json
    import tkinter as tk
    from audiobookdl import Chapter
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.chapter_editor import parse_chapters_from_file
    from audiobookdl.gui.controller import GuiInteractionHandler, PackJobRequest, QueuedJob
    from audiobookdl.gui.coordinators import AutomationCoordinator
    from audiobookdl.options_schema import build_default_options
    from audiobookdl.pack import PackBookSpec
    from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata

    # 1. AutomationCoordinator auto_sync_mode defaults to "enqueue" & validates modes
    coord = AutomationCoordinator.__new__(AutomationCoordinator)
    coord._auto_sync_mode = "enqueue"
    assert coord.auto_sync_mode == "enqueue"
    coord.auto_sync_mode = "scan_only"
    assert coord.auto_sync_mode == "scan_only"
    with pytest.raises(ValueError, match="Invalid auto_sync_mode"):
        coord.auto_sync_mode = "download_missing"

    # 2. parse_chapters_from_file handles ABS metadata.json float seconds accurately
    abs_json = tmp_path / "metadata.json"
    abs_json.write_text(
        json.dumps({
            "tags": [],
            "chapters": [
                {"id": 0, "start": 0.0, "end": 12.5, "title": "Intro"},
                {"id": 1, "start": 12.5, "end": 45.25, "title": "Chapter 1"},
            ],
        }),
        encoding="utf-8",
    )
    parsed_chapters = parse_chapters_from_file(str(abs_json))
    assert [(c.start, c.title) for c in parsed_chapters] == [
        (0, "Intro"),
        (12_500, "Chapter 1"),
    ]

    # 3. queue_state_path honors database_directory & clearing override restores baseline
    custom_db = tmp_path / "custom_db"
    cfg_path = tmp_path / "audiobook-dl.toml"
    posix_db = str(custom_db).replace("\\", "/")
    cfg_path.write_text(f'database_directory = "{posix_db}"\n', encoding="utf-8")

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            ctrl = app.controller
            assert Path(ctrl.queue_state_path()).parent == custom_db

            interaction = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
            base_opts = build_default_options(output_format="mp3")
            job = QueuedJob(
                job_id="dl-ov-1",
                run_id=1,
                url="https://librivox.org/book-1",
                options=base_opts,
                interaction=interaction,
            )
            ctrl.pending_jobs.append(job)
            app.download_page.queue_panel.register_queue_job(job, persist=False)

            ctrl.update_queue_job_config("dl-ov-1", job_overrides={"output_format": "m4b", "audio_bitrate": "128k"})
            assert job.options.output_format == "m4b"
            assert job.options.audio_bitrate == "128k"
            assert "audio_bitrate" in job.options._explicit_fields

            ctrl.update_queue_job_config("dl-ov-1", job_overrides={"output_format": None, "audio_bitrate": None})
            opts_after_reset: Any = job.options
            assert opts_after_reset.audio_bitrate is None
            assert "audio_bitrate" not in job.options._explicit_fields

            # Pack job output_format edit syncs pack_request
            pack_job = QueuedJob(
                job_id="pack-ov-1",
                run_id=1,
                url="Local Book",
                options=build_default_options(output_format="m4b"),
                interaction=interaction,
                pack_request=PackJobRequest(paths=[str(tmp_path)], output_format="m4b"),
            )
            ctrl.pending_jobs.append(pack_job)
            app.download_page.queue_panel.register_queue_job(pack_job, persist=False)
            updated_pack = ctrl.update_queue_job_config("pack-ov-1", job_overrides={"output_format": "mp3"})
            assert updated_pack is not None
            assert updated_pack["pack_request"]["output_format"] == "mp3"
            assert pack_job.pack_request is not None and pack_job.pack_request.output_format == "mp3"

            # PackPage re-applying scanned specs preserves edits keyed by source_root
            pack_page = app.shell.ensure_page("pack")
            spec_a = PackBookSpec(
                source_root=str(tmp_path / "book_a"),
                track_paths=[str(tmp_path / "book_a" / "01.mp3")],
                audiobook=Audiobook(
                    session=MagicMock(),
                    metadata=AudiobookMetadata(title="Book A"),
                    chapters=[Chapter(start=0, title="Old Ch")],
                    files=[],
                ),
                output_format="m4b",
                output_path=str(tmp_path / "Book A.m4b"),
                total_duration_seconds=60.0,
            )
            key_a = pack_page._spec_key(spec_a)
            pack_page._edited_chapters_by_key[key_a] = [Chapter(start=0, title="Edited Ch")]
            pack_page._book_overrides_by_key[key_a] = {"title": "Custom Title"}
            pack_page._invalidate_preview("Rescanning...", clear_edits=False)
            rescanned = PackBookSpec(
                source_root=str(tmp_path / "book_a"),
                track_paths=[str(tmp_path / "book_a" / "01.mp3")],
                audiobook=Audiobook(
                    session=MagicMock(),
                    metadata=AudiobookMetadata(title="Book A"),
                    chapters=[Chapter(start=0, title="Old Ch")],
                    files=[],
                ),
                output_format="mp3",
                output_path=str(tmp_path / "Book A.mp3"),
                total_duration_seconds=60.0,
            )
            pack_page._apply_saved_edits_to_spec(rescanned)
            assert rescanned.audiobook.chapters[0].title == "Edited Ch"
            assert rescanned.audiobook.metadata.title == "Custom Title"

            # ActivityQueuePanel single-row filter detach/reattach on status update
            panel = app.download_page.queue_panel
            panel.queue_filter_var.set("Failed")
            panel._apply_queue_filter()
            assert "dl-ov-1" in panel._detached_iids
            panel.update_queue_row("dl-ov-1", "Failed", "boom")
            assert "dl-ov-1" not in panel._detached_iids
            assert panel.queue_tree.parent("dl-ov-1") == ""
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_gui_event_driven_idle_poll_wakes_on_bg_task_and_debounces_archive_selection(tmp_path):
    from audiobookdl.gui.pages.archive_tab import ArchiveTab
    from audiobookdl.utils.archive import DownloadArchive

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        try:
            # 1. Idle poll stops scheduling 250ms timers once queues and tasks are empty.
            app._events.poll_queue()
            assert app._events._poll_after_id is None

            # 2. Submitting a bounded pool task wakes polling cleanly and sleeps again once complete.
            gate = threading.Event()
            done_flag = threading.Event()

            def _bg_job() -> None:
                gate.wait(timeout=2.0)
                app.controller.event_queue.put(LogEvent(level="INFO", message="bg-woke"))
                done_flag.set()

            app.controller._submit_bg_task(_bg_job)
            assert app.controller.has_active_background_tasks() is True
            assert app._events._poll_after_id is not None

            gate.set()
            assert done_flag.wait(timeout=2.0)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and (
                app._events._poll_after_id is not None or not app.controller.event_queue.empty()
            ):
                root.update()
                time.sleep(0.01)
            assert app.controller.has_active_background_tasks() is False
            assert app._events._poll_after_id is None
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass



    # 3. ArchiveTab._on_entry_selected debounces rapid selection switches and skips intermediate walk.
    root2 = tk.Tk()
    root2.withdraw()
    try:
        archive_path = tmp_path / "downloads.db"
        dir_a = tmp_path / "BookA"
        dir_b = tmp_path / "BookB"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "a.mp3").write_bytes(b"a" * 1024)
        (dir_b / "b.mp3").write_bytes(b"b" * 3072)

        with DownloadArchive(str(archive_path), persistent=True) as arch:
            id_a = arch.record("librivox", "https://example.com/a", title="A", output_path=str(dir_a))
            id_b = arch.record("librivox", "https://example.com/b", title="B", output_path=str(dir_b))

        tab = ArchiveTab(root2, str(archive_path))
        seen_paths: list[str] = []
        orig_format_size = tab._format_size

        def spy_format_size(path: str, *, is_cancelled=None) -> str:
            seen_paths.append(path)
            return orig_format_size(path, is_cancelled=is_cancelled)

        tab._format_size = spy_format_size  # type: ignore[method-assign]
        try:
            tab._tree.selection_set(str(id_a))
            tab._on_entry_selected()
            tab._tree.selection_set(str(id_b))
            tab._on_entry_selected()

            deadline = time.monotonic() + 2.0
            while "Calculating..." in tab._detail_meta_var.get() and time.monotonic() < deadline:
                root2.update()
                time.sleep(0.01)
            assert seen_paths == [str(dir_b)]
            assert "3.0 KB" in tab._detail_meta_var.get()
        finally:
            tab.destroy()
    finally:
        try:
            if root2.winfo_exists():
                root2.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_gui_p2_3_horizontal_scroll_live_system_theme_and_queue_concurrency(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from audiobookdl.config import load_config
    from audiobookdl.gui.controller import GuiController, QueuedJob, GuiInteractionHandler
    from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
    from audiobookdl.options_schema import build_default_options
    from audiobookdl.pipeline import DownloadSummary

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    system_theme = ["light"]
    monkeypatch.setattr("audiobookdl.gui.window_manager.detect_system_theme", lambda: system_theme[0])
    monkeypatch.setattr("audiobookdl.gui.theme.detect_system_theme", lambda: system_theme[0])

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            # 1. ScrolledTreeview horizontal scrollbar auto-hides & Shift-wheel scrolls
            st = ScrolledTreeview(
                root,
                (
                    TreeColumnSpec("col_a", "Column A", width=300, stretch=False),
                    TreeColumnSpec("col_b", "Column B", width=300, stretch=False),
                ),
            )
            st._on_xscroll("0.0", "0.5")
            assert bool(st.hscrollbar.winfo_manager()) is True
            st._on_xscroll("0.0", "1.0")
            assert st.hscrollbar.winfo_manager() == ""
            assert st._on_shift_mousewheel(cast(tk.Event, SimpleNamespace(delta=-120))) == "break"

            # 2. Live OS theme change tracking when mode == "system"
            app.set_theme("system")
            assert app._window.palette.mode == "light"
            assert app._window.theme_watch_after_id is not None
            system_theme[0] = "dark"
            app._window.check_system_theme()
            assert app._window.palette.mode == "dark"
            app.set_theme("light")
            assert app._window.theme_watch_after_id is None

            # 3. Queue concurrency UI spinbox persists to config
            dl_page = app.download_page
            app.queue_concurrency_var.set(2)
            dl_page.queue_panel._handle_concurrency_change()
            assert app.controller.queue_concurrency == 2
            assert load_config(str(cfg_path)).gui.queue_concurrency == 2
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass



    # 4. GuiController runs queued jobs concurrently when queue_concurrency == 2
    ctrl = GuiController(config_location=str(cfg_path))
    assert ctrl.queue_concurrency == 2
    barrier = threading.Barrier(2, timeout=2.0)
    seen_urls: list[str] = []
    seen_lock = threading.Lock()

    def _concurrent_run(urls, _opts, _cfg, **_kwargs):
        barrier.wait()
        with seen_lock:
            seen_urls.extend(urls)
        return DownloadSummary(completed=1, books_completed=1)

    monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", _concurrent_run)
    try:
        ev = threading.Event()
        inter = GuiInteractionHandler(ctrl.event_queue, ev)
        opts = build_default_options(output_template=str(tmp_path / "{title}"))
        with ctrl.queue_cond:
            ctrl.worker_has_active_run = True
            ctrl.batch_total = 2
            ctrl.pending_jobs.append(
                QueuedJob(
                    job_id="j1",
                    run_id=1,
                    url="https://example.com/1",
                    options=opts,
                    interaction=inter,
                )
            )
            ctrl.pending_jobs.append(
                QueuedJob(
                    job_id="j2",
                    run_id=1,
                    url="https://example.com/2",
                    options=opts,
                    interaction=inter,
                )
            )
            ctrl.ensure_worker_started()
            ctrl.queue_cond.notify_all()

        done_events = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not done_events:
            ctrl.drain_events(
                lambda e: done_events.append(e) if isinstance(e, RunDoneEvent) else None
            )
            time.sleep(0.02)
        assert len(done_events) == 1
        assert done_events[0].summary.books_completed == 2
        assert set(seen_urls) == {"https://example.com/1", "https://example.com/2"}
    finally:
        ctrl.shutdown(is_running=False, is_previewing=False)


@pytest.mark.gui
def test_p3_2_audition_tray_daemon_and_duplicate_preview(tmp_path, monkeypatch):
    from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter
    from audiobookdl.config import load_config
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.chapter_editor import ChapterEditorFrame, resolve_audition_target
    from audiobookdl.gui.tray import TrayIconController
    from audiobookdl.utils.archive import DownloadArchive, default_archive_path

    # 1. resolve_audition_target pinpoints single-file and multi-track chapter offsets
    t1 = tmp_path / "01.mp3"
    t2 = tmp_path / "02.mp3"
    t1.write_bytes(b"a")
    t2.write_bytes(b"b")
    assert resolve_audition_target(12500, [(str(t1), 60000)]) == (str(t1), 12.5)
    assert resolve_audition_target(75000, [(str(t1), 60000), (str(t2), 60000)]) == (str(t2), 15.0)

    root = tk.Tk()
    root.withdraw()
    try:
        # 2. ChapterEditorFrame Audition 5s starts and cleanly terminates ffplay subprocess
        terminated = []

        class _FakeProc:
            def __init__(self):
                self._rc = None

            def poll(self):
                return self._rc

            def terminate(self):
                self._rc = 0
                terminated.append("term")

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self._rc = -9

        spawned_cmds = []
        with monkeypatch.context() as mp:
            mp.setattr(
                "audiobookdl.gui.chapter_editor.find_executable",
                lambda name: "C:/bin/ffplay.exe" if name == "ffplay" else None,
            )
            mp.setattr(
                "audiobookdl.gui.chapter_editor.subprocess.Popen",
                lambda cmd, **_kw: (spawned_cmds.append(list(cmd)), _FakeProc())[1],
            )

            editor = ChapterEditorFrame(root)
            editor.set_chapters(
                [Chapter(start=0, title="Intro"), Chapter(start=75000, title="Part 2")],
                audition_tracks=[(str(t1), 60000), (str(t2), 60000)],
            )
            assert str(editor._audition_btn.cget("state")) == "normal"
            editor.tree.selection_set("1")
            assert editor.toggle_audition() is True
            assert spawned_cmds and "-ss" in spawned_cmds[0] and "15.000" in spawned_cmds[0]
            assert str(t2) in spawned_cmds[0]
            assert editor._audition_btn.cget("text") == "Stop Audition"
            assert editor.toggle_audition() is False
            assert terminated == ["term"]
            editor.destroy()

        # 3. Minimize-to-tray config + App._on_close() + cross-source duplicate preview warning
        cfg_path = tmp_path / "tray_cfg.toml"
        db_dir = tmp_path / "db"
        cfg_path.write_text(
            f'database_directory = "{str(db_dir).replace(chr(92), "/")}"\n\n'
            "[gui]\nuse_download_archive = true\n",
            encoding="utf-8",
        )
        with DownloadArchive(default_archive_path(str(db_dir)), persistent=True) as archive:
            archive.record(
                "storytel",
                "st-dune",
                title="Dune",
                author="Frank Herbert",
                asin="B000R3434M",
                output_path=str(tmp_path / "Dune.m4b"),
                status="downloaded",
            )

        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            tray_starts = []
            tray_stops = []
            monkeypatch.setattr(TrayIconController, "is_available", staticmethod(lambda: True))
            monkeypatch.setattr(app._window.tray_controller, "start", lambda: (tray_starts.append(True), True)[1])
            monkeypatch.setattr(app._window.tray_controller, "stop", lambda: tray_stops.append(True))

            app.minimize_to_tray_var.set(True)
            app._window.toggle_minimize_to_tray()
            assert load_config(str(cfg_path)).gui.minimize_to_tray is True

            # Normal close minimizes instead of destroying root
            app._on_close()
            assert len(tray_starts) == 1
            assert root.winfo_exists()

            # Cross-source duplicate banner shown in PreviewDialog
            src = type("_DummySrc", (), {"name": "libro.fm", "close": lambda self: None})()
            ab = Audiobook(
                session=MagicMock(),
                metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"], asin="B000R3434M"),
                files=[AudiobookFile(url="https://libro.fm/1.mp3", ext="mp3")],
            )
            dlg = app._preview.show_preview_dialog("https://libro.fm/book/dune", src, ab)  # type: ignore[arg-type]
            assert dlg._duplicate_warning is not None
            assert "storytel" in dlg._duplicate_warning
            dlg.destroy()

            # Forced close stops tray cleanly
            app._on_close(force=True)
            assert len(tray_stops) >= 1
        except Exception:
            if root.winfo_exists():
                app._on_close(force=True)
            raise
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_gui_download_page_responsive_two_column_layout(tmp_path):
    from audiobookdl.gui.theme import scale_px

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        root.geometry("960x680+40+40")
        root.update_idletasks()
        dl = app.download_page
        assert dl._layout_mode == "vertical"
        assert dl.paned is dl._vpaned
        assert str(dl.top_scroll) in dl._vpaned.panes()
        assert str(dl.queue_panel) in dl._vpaned.panes()

        dl.urls_text.insert("1.0", "https://example.com/book-1")
        dl.queue_panel.append_log("sample log entry")
        ctrl = app.controller
        interaction = GuiInteractionHandler(ctrl.event_queue, ctrl.cancel_event)
        opts = app.build_options(urls_override=["https://librivox.org/book-1"])
        dl.queue_panel.register_queue_job(
            QueuedJob(
                job_id="j_resp",
                run_id=1,
                url="https://librivox.org/book-1",
                options=opts,
                interaction=interaction,
            ),
            persist=False,
        )
        root.update()
        dl._split_ratio = 0.37

        wide_w = scale_px(dl, dl.TWO_COL_ENTER_DP + 180)
        wide_root_w = wide_w + scale_px(dl, app.shell.SIDEBAR_WIDTH_DP)
        root.geometry(f"{wide_root_w}x720+40+40")
        root.update()
        dl._on_page_configure(SimpleNamespace(widget=dl, width=wide_w))  # type: ignore[arg-type]
        assert dl._layout_mode == "horizontal"
        assert dl.paned is dl._hpaned
        assert str(dl.top_scroll) in dl._hpaned.panes()
        assert str(dl.queue_panel) in dl._hpaned.panes()
        assert tuple(dl._vpaned.panes()) == ()

        h_pos = dl.fit_horizontal_splitter(total_width=wide_w)
        assert h_pos is not None
        assert dl._h_split_ratio == pytest.approx(h_pos / wide_w, abs=0.01)
        assert dl.get_split_ratio() == pytest.approx(0.37)

        assert "https://example.com/book-1" in dl.urls_text.get("1.0", tk.END)
        assert "sample log entry" in dl.queue_panel.log_text.get("1.0", tk.END)
        assert dl.queue_panel.queue_tree.exists("j_resp")

        # Hysteresis band keeps horizontal layout until width drops below TWO_COL_EXIT_DP
        hyst_w = scale_px(dl, (dl.TWO_COL_ENTER_DP + dl.TWO_COL_EXIT_DP) // 2)
        dl._on_page_configure(SimpleNamespace(widget=dl, width=hyst_w))  # type: ignore[arg-type]
        assert dl._layout_mode == "horizontal"

        root.geometry("960x680+40+40")
        root.update()
        narrow_w = scale_px(dl, dl.TWO_COL_EXIT_DP - 180)
        dl._on_page_configure(SimpleNamespace(widget=dl, width=narrow_w))  # type: ignore[arg-type]
        assert dl._layout_mode == "vertical"
        assert dl.paned is dl._vpaned
        assert str(dl.top_scroll) in dl._vpaned.panes()
        assert str(dl.queue_panel) in dl._vpaned.panes()
        assert tuple(dl._hpaned.panes()) == ()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_activity_panel_toolbar_reflow_and_download_bars(tmp_path):
    from audiobookdl.gui.theme import scale_px

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        dl = app.download_page
        qp = dl.queue_panel

        qp.sync_responsive_toolbars(scale_px(qp, 420))
        assert qp._action_bar_mode == "compact"
        assert qp._log_toolbar_mode == "compact"
        assert qp._queue_toolbar_mode == "compact"
        assert qp.download_btn.grid_info()["row"] == 0
        assert qp.pack_btn.grid_info()["row"] == 1
        assert qp._log_save_btn.grid_info()["row"] == 1
        assert qp.queue_clear_btn.grid_info()["row"] == 1

        qp.sync_responsive_toolbars(scale_px(qp, 760))
        assert qp._action_bar_mode == "wide"
        assert qp._log_toolbar_mode == "wide"
        assert qp._queue_toolbar_mode == "wide"
        assert qp.pack_btn.grid_info()["row"] == 0
        assert qp._log_save_btn.grid_info()["row"] == 0
        assert qp.queue_clear_btn.grid_info()["row"] == 0

        dl._on_folder_bar_configure(SimpleNamespace(width=scale_px(dl, 500)))  # type: ignore[arg-type]
        dl._on_url_footer_configure(SimpleNamespace(width=scale_px(dl, 500)))  # type: ignore[arg-type]
        assert dl._folder_bar_mode == "compact"
        assert dl._url_footer_mode == "compact"
        assert dl.account_chip_btn.grid_info()["row"] == 1
        assert dl._url_btn_row.grid_info()["row"] == 1

        dl._on_folder_bar_configure(SimpleNamespace(width=scale_px(dl, 700)))  # type: ignore[arg-type]
        dl._on_url_footer_configure(SimpleNamespace(width=scale_px(dl, 700)))  # type: ignore[arg-type]
        assert dl._folder_bar_mode == "wide"
        assert dl._url_footer_mode == "wide"
        assert dl.account_chip_btn.grid_info()["row"] == 0
        assert dl._url_btn_row.grid_info()["row"] == 0
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_navigation_history_breadcrumbs_and_deep_links(tmp_path):
    from audiobookdl.gui.shell import NavRoute

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('[gui]\nactive_page = "library:catalog"\n', encoding="utf-8")

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        # Direct deep-link startup restores Library -> Catalog & Search lazily
        assert app.shell.active_page_id == "library"
        assert app.shell.active_route == NavRoute("library", sub_tab="catalog")
        assert app.shell.built_page_ids() == ("library",)
        lib = app.shell.get_page("library")
        assert lib is not None
        assert lib.current_sub_tab() == "catalog"
        assert "Library" in app.shell.breadcrumb_var.get()
        assert "Catalog & Search" in app.shell.breadcrumb_var.get()
        assert not app.shell.can_go_back
        assert not app.shell.can_go_forward

        # Manual sub-tab switch records history and updates breadcrumb
        lib.notebook.select(lib.sync_tab)
        lib._handle_notebook_tab_changed()
        assert app.shell.active_route == NavRoute("library", sub_tab="sync")
        assert "Library Sync" in app.shell.breadcrumb_var.get()
        assert app.shell.can_go_back

        # Context-preserving jump to Accounts and Pack
        app.controller.save_source_credentials("storytel", username="alice", profile="work")
        app.open_accounts("storytel", "work")
        assert app.shell.active_page_id == "accounts"
        sel_svc = app._auth.selected_service()
        assert sel_svc is not None and sel_svc.key == "storytel"
        assert app._auth.profile_var.get() == "work"
        assert f"{sel_svc.label} (work)" in app.shell.breadcrumb_var.get()

        app.navigate("pack")
        assert app.shell.active_page_id == "pack"
        assert app.shell.breadcrumb_var.get() == "Pack"

        # Back restores Accounts (with Storytel/work context), then Library Sync, then Catalog
        app.shell.go_back()
        assert app.shell.active_page_id == "accounts"
        sel_back = app._auth.selected_service()
        assert sel_back is not None and sel_back.key == "storytel"
        assert app._auth.profile_var.get() == "work"

        app.shell.go_back()
        assert app.shell.active_page_id == "library"
        assert lib.current_sub_tab() == "sync"

        app.shell.go_back()
        assert app.shell.active_page_id == "library"
        assert lib.current_sub_tab() == "catalog"
        assert not app.shell.can_go_back
        assert app.shell.can_go_forward

        # Forward walks back forward through Sync -> Accounts
        app.shell.go_forward()
        assert app.shell.active_page_id == "library"
        assert lib.current_sub_tab() == "sync"
        app.shell.go_forward()
        assert app.shell.active_page_id == "accounts"
        assert app._auth.profile_var.get() == "work"

        # View menu exposes Back, Forward, and Library Tabs while preserving top-level menus
        menubar = root.nametowidget(root.cget("menu"))
        top_labels = [
            menubar.entrycget(i, "label")
            for i in range((menubar.index("end") or 0) + 1)
            if menubar.type(i) == "cascade"
        ]
        assert top_labels == ["File", "View", "Help"]
        view_menu = menubar.nametowidget(menubar.entrycget("View", "menu"))
        view_labels = [
            view_menu.entrycget(i, "label")
            for i in range((view_menu.index("end") or 0) + 1)
            if view_menu.type(i) != "separator"
        ]
        assert "Back" in view_labels
        assert "Forward" in view_labels
        assert "Library Tabs" in view_labels
        assert root.bind("<Alt-Left>")
        assert root.bind("<Alt-Right>")

        # Persistence round-trips sub-tab deep link ("library:catalog")
        app.open_library_tab("catalog")
        app._on_close(force=True)
        persisted = load_config(str(cfg_path))
        assert persisted.gui.active_page == "library:catalog"
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_gui_contextual_account_jump_and_view_in_download_action(tmp_path, monkeypatch):
    from tkinter import ttk
    from audiobookdl.gui.controller import PackJobRequest

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        dl = app.download_page
        dl.urls_text.insert("1.0", "https://librivox.org/test-book")
        app._urls.on_urls_changed()

        # Clicking account chip jumps to Accounts for the detected service, Back restores Download + URLs
        dl.account_chip_btn.invoke()
        assert app.shell.active_page_id == "accounts"
        sel = app._auth.selected_service()
        assert sel is not None and sel.key == "librivox"
        app.shell.back_btn.invoke()
        assert app.shell.active_page_id == "download"
        assert "https://librivox.org/test-book" in dl.urls_text.get("1.0", tk.END)

        # Catalog -> Account... jumps to Accounts pre-selected for Catalog's service
        lib = app.open_library_tab("catalog")
        storytel_label = next(s.label for s in app._services if s.key == "storytel")
        lib.catalog_tab._service_var.set(storytel_label)
        lib.catalog_tab.sync_buttons()
        assert lib.catalog_tab._account_btn is not None
        lib.catalog_tab._account_btn.invoke()
        assert app.shell.active_page_id == "accounts"
        acct_sel = app._auth.selected_service()
        assert acct_sel is not None and acct_sel.key == "storytel"
        app.shell.go_back()
        assert app.shell.active_page_id == "library"
        assert lib.current_sub_tab() == "catalog"

        # ArchiveTab button uses unified "Add to URLs" label and triggers "View in Download ->"
        archive_btn_texts = [
            str(w.cget("text"))
            for frame in lib.archive_tab.winfo_children()
            for w in frame.winfo_children()
            if isinstance(w, ttk.Button)
        ]
        assert "Add to URLs" in archive_btn_texts
        assert "Queue Selected" not in archive_btn_texts
        assert lib.archive_tab._on_queue_urls is not None
        lib.archive_tab._on_queue_urls(["https://librivox.org/from-archive"])
        assert app.shell.context_action_btn.winfo_manager() == "grid"
        assert "View in Download" in str(app.shell.context_action_btn.cget("text"))

        app.shell.context_action_btn.invoke()
        assert app.shell.active_page_id == "download"
        assert app.shell.context_action_btn.winfo_manager() == ""
        assert "https://librivox.org/from-archive" in dl.urls_text.get("1.0", tk.END)

        # Pack submission surfaces "View in Download ->" contextual quick-jump
        app.navigate("pack")
        monkeypatch.setattr(
            app.controller,
            "start_or_enqueue_pack_jobs",
            lambda _pairs: (True, 1, [], 1),
        )
        req = PackJobRequest(
            paths=[str(tmp_path)],
            recursive=False,
            delete_source=False,
            output_dir=str(tmp_path),
            label="Test Pack",
            output_format="m4b",
        )
        app._runs.on_pack_submitted([req], "m4b")
        assert app.shell.active_page_id == "pack"
        assert app.shell.context_action_btn.winfo_manager() == "grid"
        assert "View in Download" in str(app.shell.context_action_btn.cget("text"))
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_skip_unavailable_and_download_size_estimate_displays(tmp_path):
    from tkinter import ttk
    from audiobookdl.gui.events import DownloadEstimateEvent, ProgressMetricsEvent, ProgressStartEvent
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.utils.audiobook import AudiobookFile

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        assert app.skip_unavailable_var.get() is True
        app.skip_unavailable_var.set(False)
        opts = app.build_options(urls_override=["https://example.com/book"])
        assert opts.skip_unavailable is False

        # Live DownloadEstimateEvent + ProgressMetricsEvent surface expected size in metrics_var
        dl_page = app.shell.ensure_page("download")
        qp = dl_page.queue_panel
        qp.prepare_run_ui_state(1, 1, "--- Start ---")
        qp.handle_activity_event(ProgressStartEvent(description="Downloading Test", total=2.0))
        qp.handle_activity_event(
            DownloadEstimateEvent(
                estimated_bytes=10 * 1024 * 1024,
                is_exact=False,
                known_files=1,
                total_files=2,
            )
        )
        qp.handle_activity_event(
            ProgressMetricsEvent(
                completed=1.0,
                total=2.0,
                bytes_downloaded=5 * 1024 * 1024,
                speed_bps=1024 * 1024,
                elapsed_seconds=5.0,
                eta_seconds=5.0,
            )
        )
        metrics_text = app.metrics_var.get()
        assert "5.0 MB / ~10.0 MB" in metrics_text

        # PreviewDialog renders Est. Size row for books with known file sizes
        src = MagicMock()
        src.name = "rss"
        ab = Audiobook(
            session=MagicMock(),
            metadata=AudiobookMetadata(title="Sized Book", authors=["Author"]),
            files=[AudiobookFile(url="https://cdn.example.com/1.mp3", ext="mp3", size=20 * 1024 * 1024)],
        )
        dlg = PreviewDialog(
            root,
            "https://example.com/rss",
            src,
            ab,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_a: None,
        )
        assert dlg._readonly_frame is not None
        labels = [
            str(w.cget("text"))
            for w in dlg._readonly_frame.winfo_children()
            if isinstance(w, ttk.Label)
        ]
        assert "Est. Size:" in labels
        assert "20.0 MB" in labels
        dlg.destroy()
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_profile_management_crud_and_smart_service_select(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        accounts = app.open_accounts("storytel")

        # Create named profile via coordinator & save credentials with locale
        assert app._auth.create_new_profile("work") is True
        app.username_var.set("alice@example.com")
        app.password_var.set("pw-work")
        app._auth.locale_var.set("sv")
        app._auth.save_current_credentials()

        # Smart service selection auto-picks sole named profile ("work")
        app._auth.select_service_smart("storytel")
        assert app.profile_var.get() == "work"
        assert app._auth.locale_var.get() == "sv"

        # Rename profile cleanly migrates config and updates form + table
        monkeypatch.setattr("audiobookdl.gui.pages.accounts_page.messagebox.askyesno", lambda *_a, **_kw: True)
        assert app._auth.rename_current_profile("family") is True
        assert app.profile_var.get() == "family"
        assert "storytel:family" in app.controller.config_snapshot().sources
        assert "storytel:work" not in app.controller.config_snapshot().sources
        assert app.controller.config_snapshot().sources["storytel:family"].locale == "sv"

        # Delete profile via AccountsPage removes profile and refreshes table
        accounts.refresh()
        accounts._tree.selection_set("storytel:family")
        accounts._delete_selected_profile()
        assert "storytel:family" not in app.controller.config_snapshot().sources
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_oauth_panel_connect_disconnect_and_accounts_status(tmp_path, monkeypatch):
    from audiobookdl.config import load_source_oauth_token, save_source_oauth_token
    from audiobookdl.sources import ServiceInfo
    from audiobookdl.utils.oauth import OAuthTokenBundle

    oauth_svc = ServiceInfo(
        key="customoauth",
        label="CustomOAuth",
        auth_methods=("oauth", "login"),
        login_fields=("username", "password"),
        supports_search=True,
        supports_library=True,
    )
    monkeypatch.setattr(
        "audiobookdl.gui.app.get_available_services",
        lambda: [oauth_svc],
    )
    monkeypatch.setattr(
        "audiobookdl.gui.auth_panel.messagebox.showinfo",
        lambda *_a, **_kw: None,
    )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        accounts = app.open_accounts("customoauth")
        panel = accounts.auth_panel
        assert panel._oauth_row.winfo_manager() == "grid"
        assert app._auth.oauth_status_var.get() == "Not connected"

        # Trigger OAuth connect -> verify background invocation and completion event handling
        called: list[tuple[str, str]] = []

        def _fake_start_oauth(svc_key, svc_label, prof, opts):
            called.append((svc_key, prof))
            save_source_oauth_token(
                svc_key,
                OAuthTokenBundle(access_token="gui-oauth-tok", refresh_token="gui-ref"),
                profile=prof,
                overwrite=str(cfg_path),
            )
            import threading
            return threading.Event()

        monkeypatch.setattr(app.controller, "start_oauth_connect", _fake_start_oauth)
        app._auth.oauth_client_id_var.set("my-gui-client")
        panel.oauth_connect_btn.invoke()
        assert called == [("customoauth", "default")]

        app._auth.handle_oauth_connected("customoauth", "CustomOAuth", "default", True, None)
        assert app._auth.oauth_status_var.get() == "Connected"
        assert app.saved_badge_var.get() == "Saved"
        accounts.refresh()
        assert tree_item_values(accounts._tree, "customoauth:default")[5] == "OAuth Connected"

        # Disconnect OAuth prompts confirmation, runs asynchronously via start_oauth_disconnect, and removes token
        monkeypatch.setattr(
            "audiobookdl.gui.auth_panel.messagebox.askyesno",
            lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            app.controller,
            "disconnect_oauth",
            lambda svc_key, _opts, profile=None: (
                True,
                app.controller.delete_source_profile(svc_key, profile=profile),
            ),
        )
        panel.oauth_disconnect_btn.invoke()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and app._auth._is_disconnecting_oauth:
            app._events.poll_queue()
            root.update()
            time.sleep(0.01)
        assert app._auth.oauth_status_var.get() == "Not connected"
        assert load_source_oauth_token("customoauth", profile="default", overwrite=str(cfg_path)) is None
    finally:
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


@pytest.mark.gui
def test_gui_custom_bitrate_and_sample_rate_validation_persistence_and_ffmpeg_check(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            assert app.sample_rate_var.get() == "Auto"
            assert app.build_options().sample_rate is None

            panel = app.download_page.options_panel
            assert str(panel._bitrate_combo.cget("state")) == "normal"
            assert str(panel._sample_rate_combo.cget("state")) == "normal"

            # Inline validation catches malformed bitrate & sample rate
            app.audio_bitrate_var.set("bad-bitrate")
            app.sample_rate_var.set("123")
            panel._validate_inline_fields()
            validation_msg = panel._validation_var.get()
            assert "Bitrate" in validation_msg
            assert "Sample rate" in validation_msg

            # Custom valid values clear validation and build DownloadOptions cleanly
            app.audio_bitrate_var.set("160k")
            app.sample_rate_var.set("22050")
            panel._validate_inline_fields()
            assert panel._validation_var.get() == ""

            opts = app.build_options()
            assert opts.audio_bitrate == "160k"
            assert opts.sample_rate == 22050

            # FFmpegAssistant treats explicit bitrate or sample_rate as needing ffmpeg even on Original format
            with (
                patch.object(app._ffmpeg_assistant, "check_tool", return_value=(None, None)),
                patch("audiobookdl.gui.ffmpeg_assistant.messagebox.askyesnocancel", return_value=False) as mock_prompt,
            ):
                assert app._ffmpeg_assistant.ensure_available(build_default_options(sample_rate=22050)) is True
                mock_prompt.assert_called_once()
                assert "process audio" in mock_prompt.call_args[0][1]

            # Per-job queue override supports sample_rate
            interaction = GuiInteractionHandler(app.controller.event_queue, app.controller.cancel_event)
            job = QueuedJob(
                job_id="dl-sr-1",
                run_id=1,
                url="https://librivox.org/book-sr",
                options=build_default_options(),
                interaction=interaction,
            )
            app.controller.pending_jobs.append(job)
            app.download_page.queue_panel.register_queue_job(job, persist=False)
            app.controller.update_queue_job_config(
                "dl-sr-1",
                job_overrides={"audio_bitrate": "160k", "sample_rate": 22050},
            )
            assert job.options.audio_bitrate == "160k"
            assert job.options.sample_rate == 22050

            app._options.save_run_preferences(opts)
        finally:
            app.controller.shutdown(is_running=False, is_previewing=False)
    finally:
        root.destroy()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        try:
            assert app2.audio_bitrate_var.get() == "160k"
            assert app2.sample_rate_var.get() == "22050"
            opts2 = app2.build_options()
            assert opts2.audio_bitrate == "160k"
            assert opts2.sample_rate == 22050
        finally:
            app2.controller.shutdown(is_running=False, is_previewing=False)
    finally:
        root2.destroy()


@pytest.mark.gui
def test_aud001_enrichment_builds_options_on_main_thread(tmp_path, monkeypatch):
    from audiobookdl import Audiobook, AudiobookMetadata
    from audiobookdl.utils.enrichment import EnrichmentCandidate

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        try:
            # Calling build_options off the main thread must fail immediately
            worker_exc: list[Exception] = []

            def _off_thread_build() -> None:
                try:
                    app._options.build_options(urls_override=["https://example.com/book"])
                except Exception as exc:
                    worker_exc.append(exc)

            t = threading.Thread(target=_off_thread_build)
            t.start()
            t.join(timeout=2.0)
            assert len(worker_exc) == 1
            assert isinstance(worker_exc[0], RuntimeError)

            # PreviewCoordinator enrichment snapshots DownloadOptions on main thread before worker
            observed_threads: dict[str, int] = {}
            main_ident = threading.get_ident()
            orig_build = app._options.build_options

            def spy_build(*a, **kw):
                observed_threads["build_options"] = threading.get_ident()
                return orig_build(*a, **kw)

            monkeypatch.setattr(app._options, "build_options", spy_build)

            cand = EnrichmentCandidate(provider="audnexus", score=0.95, title="Dune")

            def fake_search(_book, opts):
                observed_threads["search_worker"] = threading.get_ident()
                assert opts.urls == ["https://example.com/dune"]
                return [cand]

            def fake_apply(target, chosen, opts):
                observed_threads["apply_worker"] = threading.get_ident()
                assert opts.urls == ["https://example.com/dune"]
                target.metadata.title = chosen.title
                return ["title"], False, False

            monkeypatch.setattr(app.controller, "search_enrichment_for_audiobook", fake_search)
            monkeypatch.setattr(app.controller, "apply_enrichment_candidate", fake_apply)

            class _PickerStub:
                def __init__(self, *_a, **_kw):
                    self.selected_candidate = cand

                def destroy(self):
                    pass

            monkeypatch.setattr("audiobookdl.gui.preview_dialog._EnrichmentPickerDialog", _PickerStub)
            monkeypatch.setattr(
                "audiobookdl.gui.preview_dialog.PreviewDialog.wait_window",
                lambda _self, _win: None,
            )

            book = Audiobook(
                session=requests.Session(),
                metadata=AudiobookMetadata(title="Dune"),
                files=[],
            )
            src = SimpleNamespace(name="Fake", close=lambda: None)
            dlg = app._preview.show_preview_dialog("https://example.com/dune", src, book)  # type: ignore[arg-type]
            try:
                dlg._start_enrichment(book)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and "apply_worker" not in observed_threads:
                    root.update()
                    time.sleep(0.02)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and "Updated: title" not in dlg._enrich_status_var.get():
                    root.update()
                    time.sleep(0.02)
                assert observed_threads["build_options"] == main_ident
                assert observed_threads["search_worker"] != main_ident
                assert observed_threads["apply_worker"] != main_ident
                assert dlg._enrich_status_var.get() == "Updated: title"
            finally:
                if dlg.winfo_exists():
                    dlg.destroy()
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_aud002_tray_queue_dispatch_platform_guards_and_stop_join(tmp_path, monkeypatch):
    import sys as _sys
    import types
    from audiobookdl.gui.tray import TrayIconController, is_tray_supported

    # Platform checks: darwin disabled, headless linux disabled
    with monkeypatch.context() as mp:
        mp.setattr("audiobookdl.gui.tray.importlib.util.find_spec", lambda _name: object())
        mp.setattr(_sys, "platform", "darwin")
        assert is_tray_supported() is False

        mp.setattr(_sys, "platform", "linux")
        mp.delenv("DISPLAY", raising=False)
        mp.delenv("WAYLAND_DISPLAY", raising=False)
        assert is_tray_supported() is False

        mp.setenv("DISPLAY", ":0")
        assert is_tray_supported() is True

    # TrayIconController queues actions from background thread without calling root.after()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        try:
            restored: list[int] = []
            synced: list[int] = []
            stopped_flag = threading.Event()
            menu_callbacks: dict[str, Any] = {}

            class _FakeMenu:
                SEPARATOR = object()

                def __init__(self, *items):
                    self.items = items

            class _FakeMenuItem:
                def __init__(self, text, action, default=False):
                    menu_callbacks[text] = action

            class _FakeIcon:
                def __init__(self, _name, _image, _tooltip, _menu):
                    self.visible = False

                def run(self, setup=None):
                    if setup is not None:
                        setup(self)
                    stopped_flag.wait(timeout=2.0)

                def stop(self):
                    stopped_flag.set()

            fake_pystray = types.SimpleNamespace(
                Menu=_FakeMenu,
                MenuItem=_FakeMenuItem,
                Icon=_FakeIcon,
            )
            monkeypatch.setitem(_sys.modules, "pystray", fake_pystray)
            monkeypatch.setattr(TrayIconController, "is_available", staticmethod(lambda: True))

            tray = TrayIconController(
                on_restore=lambda: restored.append(threading.get_ident()),
                on_sync_now=lambda: synced.append(threading.get_ident()),
                on_exit=lambda: None,
            )
            app._window.tray_controller = tray
            assert tray.start() is True
            assert tray.is_running is True

            # Trigger tray callbacks from a non-main thread
            bg = threading.Thread(
                target=lambda: (
                    menu_callbacks["Sync Now"](None),
                    menu_callbacks["Open audiobook-dl"](None),
                )
            )
            bg.start()
            bg.join(timeout=1.0)
            assert restored == []
            assert synced == []

            # EventDispatcher.poll_queue() drains tray actions on the main thread
            app._events.poll_queue()
            assert synced == [threading.get_ident()]
            assert restored == [threading.get_ident()]
            assert app._events._poll_after_id is not None

            thread_ref = tray._thread
            tray.stop()
            assert not tray.is_running
            assert stopped_flag.is_set()
            assert thread_ref is not None and not thread_ref.is_alive()

            # Backend error during Icon construction returns False cleanly instead of raising
            class _BrokenIcon:
                def __init__(self, *_a):
                    raise RuntimeError("no dbus")

            fake_pystray.Icon = _BrokenIcon
            assert tray.start() is False
            assert tray.is_running is False
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_aud003_parallel_workers_state_pause_remove_and_per_job_progress(tmp_path, monkeypatch):
    from audiobookdl.gui.controller import GuiController
    from audiobookdl.gui.events import ProgressAdvanceEvent, ProgressMetricsEvent, ProgressStartEvent
    from audiobookdl.pipeline import DownloadSummary

    ctrl = GuiController(config_location=str(tmp_path / "audiobook-dl.toml"))
    ctrl.set_queue_concurrency(2)

    j1_started = threading.Event()
    j2_started = threading.Event()
    j1_release = threading.Event()
    j2_release = threading.Event()
    executed: list[str] = []

    def fake_run_downloads(urls, _opts, _cfg, **_kw):
        url = urls[0]
        executed.append(url)
        if url.endswith("/1"):
            j1_started.set()
            j1_release.wait(timeout=3.0)
        elif url.endswith("/2"):
            j2_started.set()
            j2_release.wait(timeout=3.0)
        return DownloadSummary(completed=1, books_completed=1)

    monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)
    try:
        opts = build_default_options(database_directory=str(tmp_path))
        _run_id, jobs = ctrl.start_new_run(
            [
                ("https://example.com/1", opts, None, None),
                ("https://example.com/2", opts, None, None),
            ]
        )
        for j in jobs:
            ctrl.register_queue_record(j, "Example")

        assert j1_started.wait(timeout=2.0)
        assert j2_started.wait(timeout=2.0)
        # Both jobs are running concurrently; pause_queue must emit Paused for BOTH
        ctrl.pause_queue()
        paused_events: list[ControllerEvent] = []
        ctrl.drain_events(paused_events.append)
        paused_ids = {
            ev.job_id for ev in paused_events if isinstance(ev, JobStatusEvent) and ev.status == "Paused"
        }
        assert paused_ids == {jobs[0].job_id, jobs[1].job_id}

        ctrl.resume_queue()
        resumed_events: list[ControllerEvent] = []
        ctrl.drain_events(resumed_events.append)
        resumed_ids = {
            ev.job_id for ev in resumed_events if isinstance(ev, JobStatusEvent) and ev.status == "Running"
        }
        assert resumed_ids == {jobs[0].job_id, jobs[1].job_id}

        # Release job 2 first while job 1 is still active -> worker_has_active_run must stay True!
        j2_release.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and jobs[1].job_id in ctrl.active_job_ids:
            time.sleep(0.02)
        assert ctrl.worker_has_active_run is True
        assert ctrl.has_active_background_tasks() is True

        j1_release.set()
        events: list[ControllerEvent] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ctrl.drain_events(events.append)
            if any(isinstance(ev, RunDoneEvent) for ev in events):
                break
            time.sleep(0.02)
        assert not ctrl.worker_has_active_run
        assert not ctrl.has_active_background_tasks()
    finally:
        j1_release.set()
        j2_release.set()
        ctrl.shutdown(is_running=False, is_previewing=False)

    # Paused job popped by a sibling worker must be cleanly cancelled on remove_pending_job
    ctrl2 = GuiController(config_location=str(tmp_path / "audiobook-dl-2.toml"))
    ctrl2.set_queue_concurrency(2)
    gate_a_started = threading.Event()
    gate_b_started = threading.Event()
    gate_a_release = threading.Event()
    gate_b_release = threading.Event()
    ran_urls2: list[str] = []

    def fake_run2(urls, _opts, _cfg, **_kw):
        url = urls[0]
        if url.endswith("/gate-a"):
            gate_a_started.set()
            gate_a_release.wait(timeout=3.0)
        elif url.endswith("/gate-b"):
            gate_b_started.set()
            gate_b_release.wait(timeout=3.0)
        else:
            ran_urls2.append(url)
        return DownloadSummary(completed=1, books_completed=1)

    monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run2)
    try:
        opts = build_default_options(database_directory=str(tmp_path))
        _rid, jobs2 = ctrl2.start_new_run(
            [
                ("https://example.com/gate-a", opts, None, None),
                ("https://example.com/gate-b", opts, None, None),
                ("https://example.com/paused-sibling", opts, None, None),
                ("https://example.com/keep", opts, None, None),
            ]
        )
        for j in jobs2:
            ctrl2.register_queue_record(j, "Example")
        assert gate_a_started.wait(timeout=2.0)
        assert gate_b_started.wait(timeout=2.0)
        ctrl2.pause_queue()
        gate_b_release.set()
        # Wait until worker 2 pops jobs2[2] ("paused-sibling") and pauses on it
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and jobs2[2].job_id not in ctrl2.active_job_ids:
            time.sleep(0.02)
        assert jobs2[2].job_id in ctrl2.active_job_ids
        assert jobs2[2].job_id not in ctrl2.running_job_ids
        removed, new_total, is_running = ctrl2.remove_pending_job(jobs2[2].job_id)
        assert removed is True
        assert is_running is False
        assert new_total == 3
        assert jobs2[2].job_id in ctrl2.cancelled_job_ids

        gate_a_release.set()
        ctrl2.resume_queue()
        events2: list[ControllerEvent] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ctrl2.drain_events(events2.append)
            if any(isinstance(ev, RunDoneEvent) for ev in events2):
                break
            time.sleep(0.02)
        assert "https://example.com/paused-sibling" not in ran_urls2
        assert "https://example.com/keep" in ran_urls2
    finally:
        gate_a_release.set()
        gate_b_release.set()
        ctrl2.shutdown(is_running=False, is_previewing=False)

    # ActivityQueuePanel tracks independent per-job row progress & aggregate header progress
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "ui.toml"))
        try:
            panel = app.download_page.queue_panel
            inter = GuiInteractionHandler(app.controller.event_queue, app.controller.cancel_event)
            opts_u = app.build_options(["https://librivox.org/a"])
            ja = QueuedJob("ja", 1, "https://librivox.org/a", opts_u, inter)
            jb = QueuedJob("jb", 1, "https://librivox.org/b", opts_u, inter)
            panel.register_queue_job(ja, persist=False)
            panel.register_queue_job(jb, persist=False)
            panel.prepare_run_ui_state(1, 2, "--- Parallel ---")

            panel.handle_activity_event(JobStatusEvent(job_id="ja", status="Running"))
            panel.handle_activity_event(JobStatusEvent(job_id="jb", status="Running"))
            panel.handle_activity_event(ProgressStartEvent(description="A", total=100.0, job_id="ja"))
            panel.handle_activity_event(ProgressStartEvent(description="B", total=100.0, job_id="jb"))
            panel.handle_activity_event(
                ProgressMetricsEvent(20.0, 100.0, 200, 100.0, 2.0, 8.0, job_id="ja")
            )
            panel.handle_activity_event(
                ProgressMetricsEvent(80.0, 100.0, 800, 300.0, 2.0, 1.0, job_id="jb")
            )

            assert "20%" in str(app.queue_tree.set("ja", "detail"))
            assert "80%" in str(app.queue_tree.set("jb", "detail"))
            assert app.progress_var.get() == pytest.approx(50.0)
            assert "50%" in app.metrics_var.get()

            # Finishing ja must NOT freeze jb updates
            panel.handle_activity_event(JobStatusEvent(job_id="ja", status="Completed", detail="1 ok"))
            panel._job_progress["jb"].last_row_render_at = 0.0
            panel.handle_activity_event(ProgressAdvanceEvent(90.0, 100.0, job_id="jb"))
            assert "90%" in str(app.queue_tree.set("jb", "detail"))
            assert app.progress_var.get() == pytest.approx(90.0)
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_aud004_child_widget_destroy_preserves_automation_timers(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        try:
            app.clipboard_monitor_var.set(True)
            app._automation.apply_clipboard_monitor(quiet=True, persist=False)
            app._automation.configure_auto_sync_timer(True, 5)
            assert app._automation.clipboard_after_id is not None
            assert app._automation.auto_sync_after_id is not None

            child_top = tk.Toplevel(root)
            child_top.withdraw()
            child_menu = tk.Menu(root)
            root.update()
            child_top.destroy()
            child_menu.destroy()
            root.update()

            assert app._automation.clipboard_after_id is not None
            assert app._automation.auto_sync_after_id is not None
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass




@pytest.mark.gui
def test_aud005_polling_exception_recovery_bounded_queues_and_modal_drain(tmp_path, monkeypatch):
    from audiobookdl.gui.events import ProgressAdvanceEvent
    from audiobookdl.gui.widgets import MainThreadCallbackQueue

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        try:
            # 1. Unhandled exception in one event does not kill EventDispatcher polling
            app._runs.is_running = True
            seen_messages: list[str] = []
            orig_handle = app._events.handle_event

            def flaky_handle(item: ControllerEvent) -> None:
                if isinstance(item, LogEvent) and item.message == "explode":
                    raise RuntimeError("boom")
                if isinstance(item, LogEvent):
                    seen_messages.append(item.message)
                orig_handle(item)

            monkeypatch.setattr(app._events, "handle_event", flaky_handle)
            app.controller.event_queue.put(LogEvent(level="INFO", message="explode"))
            app.controller.event_queue.put(JobMetaEvent(job_id="missing", title="sep"))
            app.controller.event_queue.put(LogEvent(level="INFO", message="recovered"))
            app._events.poll_queue()
            assert "recovered" in seen_messages
            assert app._events._poll_after_id is not None
            app._runs.is_running = False

            # 2. MainThreadCallbackQueue bounds batch to max_batch, survives callback error,
            #    and schedules polling on main-thread post() without begin_task()
            cbq = MainThreadCallbackQueue(root, interval_ms=10, max_batch=3)
            ran: list[int] = []

            def _bad_cb() -> None:
                raise RuntimeError("cb boom")

            cbq.post(_bad_cb)
            for idx in range(5):
                cbq.post(lambda i=idx: ran.append(i))
            assert cbq._after_id is not None
            cbq._poll()
            # First tick ran at most max_batch=3 items (_bad_cb + 0 + 1) and rescheduled
            assert ran == [0, 1]
            assert cbq._after_id is not None
            cbq._poll()
            assert ran == [0, 1, 2, 3, 4]

            # 3. ControllerEventQueue stays bounded under flood without dropping critical events
            q = app.controller.event_queue
            while not q.empty():
                q.get_nowait()
            for i in range(6000):
                q.put(LogEvent(level="DEBUG", message=f"dbg-{i}"))
                q.put(ProgressAdvanceEvent(completed=float(i), total=6000.0, job_id="j1"))
            q.put(JobStatusEvent(job_id="j1", status="Completed", detail="ok"))
            assert q.qsize() <= 4096 + 4

            drained: list[ControllerEvent] = []
            while not q.empty():
                drained.append(q.get_nowait())
            assert any(
                isinstance(ev, JobStatusEvent) and ev.job_id == "j1" and ev.status == "Completed"
                for ev in drained
            )

            # 4. EventDispatcher continues draining logs WHILE a modal wait_window is open
            monkeypatch.setattr(app._events, "handle_event", orig_handle)
            app._events.cancel_poll_timer()
            holder: dict[str, Any] = {"value": None}
            done = threading.Event()
            q.put(
                PromptChoiceEvent(
                    title="Modal Drain Test",
                    choices=("X", "Y"),
                    default_index=0,
                    holder=holder,
                    done=done,
                )
            )
            observed_during_modal: list[bool] = []

            def _wait_and_check(dlg):
                q.put(LogEvent(level="INFO", message="Drained live inside modal"))
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    root.update()
                    if "Drained live inside modal" in app.log_text.get("1.0", tk.END):
                        observed_during_modal.append(True)
                        break
                    time.sleep(0.02)
                dlg.selected_index = 0
                dlg.destroy()

            monkeypatch.setattr(root, "wait_window", _wait_and_check)
            app._events.poll_queue()
            assert observed_during_modal == [True]
            assert done.is_set()
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud006_preview_dialog_worker_refcount_snapshot_and_subdialog_guards(tmp_path, monkeypatch):
    from audiobookdl import Audiobook, AudiobookMetadata, Series
    from audiobookdl.gui.preview_dialog import PreviewDialog
    from audiobookdl.utils.enrichment import EnrichmentCandidate

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. Confirm button disabled during enrichment, snapshot mutated off-thread, atomic commit
        book = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Original Title"),
            files=[],
        )
        closed_count = 0

        class _FakeSrc:
            name = "Fake"
            _session = book.session

            def close(self):
                nonlocal closed_count
                closed_count += 1

        cand = EnrichmentCandidate(provider="audnexus", score=0.95, title="Enriched Title")
        apply_started = threading.Event()
        apply_release = threading.Event()
        passed_snapshots: list[Audiobook] = []

        def fake_apply(target: Audiobook, _cand: EnrichmentCandidate, _opts: Any):
            passed_snapshots.append(target)
            apply_started.set()
            apply_release.wait(timeout=2.0)
            target.metadata.title = "Enriched Title"
            return ["title"], False, False

        class _PickerStub:
            def __init__(self, *_a, **_kw):
                self.selected_candidate = cand

            def destroy(self):
                pass

        monkeypatch.setattr("audiobookdl.gui.preview_dialog._EnrichmentPickerDialog", _PickerStub)
        monkeypatch.setattr(
            "audiobookdl.gui.preview_dialog.PreviewDialog.wait_window",
            lambda _self, _win: None,
        )

        dlg = PreviewDialog(
            root,
            "https://example.com/book",
            _FakeSrc(),  # type: ignore[arg-type]
            book,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_a: None,
            on_search_enrichment=lambda _b, _opts: [cand],
            on_apply_enrichment=fake_apply,
        )
        try:
            dlg._start_enrichment(book)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not apply_started.is_set():
                root.update()
                time.sleep(0.02)
            assert apply_started.is_set()
            assert dlg._confirm_btn is not None
            assert str(dlg._confirm_btn.cget("state")) == "disabled"
            # Live Audiobook is NOT mutated while _apply_worker is mid-flight
            assert book.metadata.title == "Original Title"
            assert passed_snapshots[0] is not book

            # Destroy dialog while worker is active -> source.close() must defer until worker exits!
            dlg.destroy()
            assert closed_count == 0
            apply_release.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and closed_count == 0:
                time.sleep(0.02)
            assert closed_count == 1
        finally:
            apply_release.set()
            if dlg.winfo_exists():
                dlg.destroy()

        # 2. Series sub-dialog deduplication, confirm block while open, and cleanup on parent destroy
        b1 = Audiobook(
            session=requests.Session(),
            metadata=AudiobookMetadata(title="Book 1"),
            files=[],
        )
        series = Series(title="My Series", books=[b1])
        series_closed = 0

        class _FakeSeriesSrc:
            name = "FakeSeries"

            def close(self):
                nonlocal series_closed
                series_closed += 1

        confirmed: list[Any] = []
        sdlg = PreviewDialog(
            root,
            "https://example.com/series",
            _FakeSeriesSrc(),  # type: ignore[arg-type]
            series,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *a: confirmed.append(a),
        )
        try:
            sub1 = sdlg._open_series_book_subdialog(0, b1)
            sub2 = sdlg._open_series_book_subdialog(0, b1)
            assert sub1 is sub2
            assert sub1.winfo_exists()

            info_shown: list[str] = []
            monkeypatch.setattr(
                "audiobookdl.gui.preview_dialog.messagebox.showinfo",
                lambda title, _msg, parent=None: info_shown.append(title),
            )
            assert sdlg._confirm_btn is not None
            sdlg._confirm_btn.invoke()
            assert info_shown == ["Open Book Preview"]
            assert confirmed == []

            sdlg.destroy()
            assert not sub1.winfo_exists()
            assert series_closed == 1
        finally:
            if sdlg.winfo_exists():
                sdlg.destroy()
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud007_job_options_dialog_preserves_tristate_overwrite_and_pack_fields(tmp_path):
    from audiobookdl.gui.queue_panel import _JobOptionsDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. Download job with unset overwrite ("Ask" / None) and unset booleans
        dl_rec = {
            "job_id": "j1",
            "job_kind": "download",
            "profile": "default",
            "status": "Queued",
            "job_overrides": {"output_format": "m4b"},
        }
        dlg = _JobOptionsDialog(root, dl_rec, profiles=["default", "work"])
        try:
            assert dlg._overwrite_var.get() == "Default (inherit)"
            assert dlg._combine_var.get() == "Default"
            assert dlg._split_var.get() == "Default"
            assert dlg._write_abs_var.get() == "Default"
            assert dlg._upload_abs_var.get() == "Default"
            dlg._profile_var.set("work")
            dlg._save()
            assert dlg.result is not None
            prof, ov = dlg.result
            assert prof == "work"
            assert ov["overwrite"] is None
            assert ov["combine"] is None
            assert ov["split_chapters"] is None
            assert ov["write_abs_metadata"] is None
            assert ov["upload_to_abs"] is None
        finally:
            if dlg.winfo_exists():
                dlg.destroy()

        # 2. Pack job omits disabled download-only fields
        pack_rec = {
            "job_id": "p1",
            "job_kind": "pack",
            "status": "Queued",
            "pack_request": {"paths": [str(tmp_path)], "output_format": "m4b"},
            "job_overrides": {"output_format": "m4b", "overwrite": True},
        }
        pdlg = _JobOptionsDialog(root, pack_rec)
        try:
            assert pdlg._overwrite_var.get() == "Overwrite"
            pdlg._overwrite_var.set("Ask")
            pdlg._save()
            assert pdlg.result is not None
            _pprof, pov = pdlg.result
            assert pov["overwrite"] is None
            assert "combine" not in pov
            assert "split_chapters" not in pov
            assert "skip_downloaded" not in pov
            assert "media_type" not in pov
        finally:
            if pdlg.winfo_exists():
                pdlg.destroy()
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud008_nested_modal_grab_restore_sizing_and_combobox_escape():
    from tkinter import ttk
    from audiobookdl.gui.formatting import parse_geometry
    from audiobookdl.gui.widgets import ModalDialog

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.geometry("400x300+40+40")
    root.update_idletasks()
    try:
        # Parent modal with intentionally small default_size -> finalize_modal expands to reqheight!
        parent_dlg = ModalDialog(
            root,
            "Parent Modal",
            default_size=(200, 80),
            min_size=(180, 70),
        )
        tall_frame = ttk.Frame(parent_dlg, width=320, height=220)
        tall_frame.pack_propagate(False)
        tall_frame.pack(padx=10, pady=10)
        cb = ttk.Combobox(tall_frame, values=("One", "Two"), state="readonly")
        cb.pack(pady=10)
        parent_dlg.finalize_modal(initial_focus=cb, monitors=[(0, 0, 1280, 800)])
        root.update_idletasks()
        parsed = parse_geometry(parent_dlg.geometry())
        assert parsed is not None
        assert parsed[0] >= 320
        assert parsed[1] >= 220
        assert parent_dlg.grab_current() is parent_dlg

        # Open nested child ModalDialog -> closing child restores grab_set() to parent_dlg
        child_dlg = ModalDialog(parent_dlg, "Child Modal", default_size=(260, 140))
        child_entry = ttk.Entry(child_dlg)
        child_entry.pack(padx=12, pady=12)
        child_dlg.finalize_modal(initial_focus=child_entry, monitors=[(0, 0, 1280, 800)])
        assert child_dlg.grab_current() is child_dlg

        child_dlg.destroy()
        assert not child_dlg.winfo_exists()
        assert parent_dlg.winfo_exists()
        assert parent_dlg.grab_current() is parent_dlg

        # Escape while Combobox popdown is posted unposts popdown without closing parent_dlg
        cb.focus_set()
        root.tk.call("ttk::combobox::Post", str(cb))
        root.update()
        pd = str(root.tk.call("ttk::combobox::PopdownWindow", str(cb)))
        assert bool(int(root.tk.call("winfo", "ismapped", pd)))
        assert parent_dlg._is_combobox_popdown_active() is True
        cb.event_generate("<Escape>")
        root.update()
        assert parent_dlg.winfo_exists()
        assert not bool(int(root.tk.call("winfo", "ismapped", pd)))
        parent_dlg.destroy()
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud009_interaction_release_preregistration_and_shutdown(tmp_path, monkeypatch):
    from audiobookdl.gui.controller import GuiController, GuiInteractionHandler
    from audiobookdl.pipeline import DownloadSummary

    ctrl = GuiController(config_location=str(tmp_path / "audiobook-dl.toml"))
    try:
        # 1. GuiInteractionHandler never blocks if released concurrently
        cancel_ev = threading.Event()
        inter = GuiInteractionHandler(ctrl.event_queue, cancel_ev)
        inter.release_all()
        assert inter.confirm("Proceed?") is False
        assert inter.choose_index("Pick", ["A", "B"]) is None
        with pytest.raises(DownloadCancelled):
            inter.ask_input("Code")

        # 2. start_new_run pre-registers queue_records before workers run; update_queue_record returns copy
        seen_record_in_worker: list[dict[str, Any] | None] = []

        def fake_run_downloads(urls, _opts, _cfg, **_kw):
            with ctrl.queue_lock:
                seen_record_in_worker.append(next(iter(ctrl.queue_records.values()), None))
            return DownloadSummary(completed=1, books_completed=1)

        monkeypatch.setattr("audiobookdl.gui.controller.run_downloads", fake_run_downloads)
        opts = build_default_options(database_directory=str(tmp_path))
        _rid, jobs = ctrl.start_new_run(
            [("https://librivox.org/book-1", opts, None, None)],
            service_label_resolver=lambda _u: "LibriVox",
        )
        assert jobs[0].job_id in ctrl.queue_records
        assert ctrl.queue_records[jobs[0].job_id]["service"] == "LibriVox"

        updated_copy = ctrl.update_queue_record(jobs[0].job_id, "Running", "ok")
        assert updated_copy is not None
        updated_copy["status"] = "MUTATED_OUTSIDE"
        assert ctrl.queue_records[jobs[0].job_id]["status"] != "MUTATED_OUTSIDE"

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not seen_record_in_worker:
            time.sleep(0.02)
        assert seen_record_in_worker and seen_record_in_worker[0] is not None

        # 3. shutdown() cancels OAuth/sync/preview unconditionally even if is_running=False
        oauth_cancel = threading.Event()
        oauth_inter = GuiInteractionHandler(ctrl.event_queue, oauth_cancel)
        ctrl._register_aux_interaction(oauth_inter, oauth_cancel)
        ctrl.shutdown(is_running=False, is_previewing=False)
        assert ctrl.cancel_event.is_set()
        assert ctrl.preview_cancel_event.is_set()
        assert oauth_cancel.is_set()
        assert oauth_inter._released is True
    finally:
        ctrl.shutdown(is_running=False, is_previewing=False)


@pytest.mark.gui
def test_aud010_post_exec_startup_trust_and_argv_list_preservation(tmp_path, monkeypatch):
    from audiobookdl.config import load_config

    trust_file = tmp_path / "trust" / "trusted_post_exec.json"
    monkeypatch.setattr(
        "audiobookdl.gui.options_state._post_exec_trust_store_path",
        lambda: str(trust_file),
    )
    cfg_path = tmp_path / "untrusted.toml"
    cfg_path.write_text(
        'post_exec = ["my_script.exe", "--flag", "arg with spaces"]\n',
        encoding="utf-8",
    )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        # 1. Untrusted post_exec is NOT loaded silently at startup
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        try:
            assert app.post_exec_var.get() == ""
            assert app.build_options(urls_override=[]).post_exec is None

            # Declining prompt keeps post_exec disabled
            monkeypatch.setattr(
                "audiobookdl.gui.options_state.messagebox.askyesno",
                lambda *_a, **_kw: False,
            )
            app._options.prompt_pending_post_exec()
            assert app.post_exec_var.get() == ""
            assert app.build_options(urls_override=[]).post_exec is None
        finally:
            app._on_close(force=True)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass

    # 2. Approving argv list preserves list[str] across build_options, save, and restart without shell=True
    cfg_path.write_text(
        'post_exec = ["my_script.exe", "--flag", "arg with spaces"]\n',
        encoding="utf-8",
    )
    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        try:
            monkeypatch.setattr(
                "audiobookdl.gui.options_state.messagebox.askyesno",
                lambda *_a, **_kw: True,
            )
            app2._options.prompt_pending_post_exec()
            assert app2.post_exec_var.get().startswith("[argv, shell=False] ")
            opts2 = app2.build_options(urls_override=["https://example.com/b"])
            assert opts2.post_exec == ["my_script.exe", "--flag", "arg with spaces"]
            assert trust_file.is_file()
        finally:
            app2._on_close(force=True)
    finally:
        try:
            if root2.winfo_exists():
                root2.destroy()
        except tk.TclError:
            pass

    # Saved TOML keeps list[str] intact (never flattened to str), and trusted launch loads automatically
    reloaded = load_config(str(cfg_path))
    assert reloaded.post_exec == ["my_script.exe", "--flag", "arg with spaces"]

    root3 = tk.Tk()
    root3.withdraw()
    try:
        app3 = AudiobookDLApp(root3, config_location=str(cfg_path))
        try:
            assert app3._options._pending_untrusted_post_exec is None
            assert app3.build_options(urls_override=[]).post_exec == [
                "my_script.exe",
                "--flag",
                "arg with spaces",
            ]
        finally:
            app3._on_close(force=True)
    finally:
        try:
            if root3.winfo_exists():
                root3.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud011_accounts_verification_cancel_prefix_cache_and_cookie_refresh(tmp_path, monkeypatch):
    from audiobookdl.gui.app import AudiobookDLApp

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.bookbeat]\nusername = "alice"\n[sources.storytel]\nusername = "bob"\n',
        encoding="utf-8",
    )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        gate = threading.Event()
        prompts_seen: list[str] = []

        def fake_verify(svc_key, opts, cfg, *, interaction=None, persist_cookies=True, cancel_event=None):
            if svc_key == "storytel":
                assert interaction is not None
                prompts_seen.append(interaction._prefixed("OTP code"))
                gate.wait(timeout=15.0)
                return False
            return True

        monkeypatch.setattr("audiobookdl.gui.controller.verify_service_auth", fake_verify)

        acct_page = app.open_accounts()
        t0 = time.monotonic()
        acct_page._test_all_logins()
        assert acct_page._is_testing is True

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            app._events.poll_queue()
            if ("bookbeat", "default") in acct_page._verification_cache:
                break
            root.update()
            time.sleep(0.01)
        assert acct_page._verification_cache[("bookbeat", "default")] == ("Valid", "ok")
        assert any("[Storytel" in p for p in prompts_seen)

        acct_page._cancel_test_logins()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and acct_page._is_testing:
            app._events.poll_queue()
            root.update()
            time.sleep(0.01)
        elapsed = time.monotonic() - t0
        assert not acct_page._is_testing
        assert elapsed < 3.0
        gate.set()

        acct_page.refresh()
        vals = tree_item_values(acct_page._tree, "bookbeat:default")
        assert vals[5] == "Valid"
        acct_page.invalidate_verification("storytel", "default")
        assert ("bookbeat", "default") in acct_page._verification_cache

        app.remember_login_var.set(False)
        refreshes: list[bool] = []
        orig_refresh = acct_page.refresh
        acct_page.refresh = lambda: (refreshes.append(True), orig_refresh())[1]  # type: ignore[method-assign]
        app._auth.on_cookies_imported("libro.fm", str(tmp_path / "cookies.txt"), 3)
        assert len(refreshes) == 1
        assert ("bookbeat", "default") in acct_page._verification_cache
    finally:
        app._on_close(force=True)
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud012_settings_abs_lock_dirty_scroll_and_notify_persist(tmp_path):
    from audiobookdl.gui.app import AudiobookDLApp

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[audiobookshelf]\nurl = "http://abs.local:13378"\n',
        encoding="utf-8",
    )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        settings_page = app.navigate("settings")
        assert settings_page._scroll._inner_scrollables is not None
        assert settings_page._scroll._inner_scrollables() == (settings_page._custom_tree,)

        settings_page._url_var.set("http://edited.local:8080")
        assert settings_page._abs_dirty is True
        app.navigate("download")
        app.navigate("settings")
        assert settings_page._url_var.get() == "http://edited.local:8080"

        settings_page._token_var.set("secret-tok")
        settings_page._library_var.set("lib-1")
        settings_page._save()
        assert not settings_page._abs_dirty
        assert app.controller.config_snapshot().audiobookshelf.url == "http://edited.local:8080"
        assert app.controller.config_snapshot().audiobookshelf.library_id == "lib-1"

        app.notify_on_complete_var.set(False)
        app._toggle_notify_on_complete()
        assert app.controller.config_snapshot().gui.notify_on_complete is False
    finally:
        app._on_close(force=True)
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud013_oauth_cancel_async_disconnect_profile_focusout_and_cookie_dialog(tmp_path, monkeypatch):
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.cookie_dialog import CookieImportDialog, default_imported_cookie_path
    from audiobookdl.sources import ServiceInfo

    svc_oauth = ServiceInfo(
        key="customoauth",
        label="CustomOAuth",
        auth_methods=("oauth", "login"),
        login_fields=("username", "password"),
    )
    svc_cookie_a = ServiceInfo(
        key="storytel",
        label="Storytel",
        auth_methods=("cookies", "login"),
        login_fields=("username", "password"),
    )
    svc_cookie_b = ServiceInfo(
        key="audiobooks.com",
        label="audiobooks.com",
        auth_methods=("cookies",),
        login_fields=(),
    )
    svc_free = ServiceInfo(key="librivox", label="Librivox", auth_methods=(), login_fields=())
    monkeypatch.setattr(
        "audiobookdl.gui.app.get_available_services",
        lambda: [svc_free, svc_oauth, svc_cookie_a, svc_cookie_b],
    )

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        app.show_password_var.set(True)
        acct_page = app.open_accounts()
        panel = acct_page.auth_panel
        assert str(panel.password_entry.cget("show")) == ""

        app._auth.select_service_profile("customoauth", "default")
        app.username_var.set("unsaved_user")
        panel.on_profile_changed()
        assert app.username_var.get() == "unsaved_user"

        oauth_ev = threading.Event()
        monkeypatch.setattr(
            app.controller,
            "start_oauth_connect",
            lambda *_a, **_kw: oauth_ev,
        )
        app._auth.connect_oauth()
        assert panel.oauth_connect_btn.cget("text") == "Cancel OAuth"
        assert str(panel.oauth_connect_btn.cget("state")) == "normal"
        panel.oauth_connect_btn.invoke()
        assert oauth_ev.is_set()

        app._auth.select_service_profile("librivox", "default")
        app.username_var.set("keep_me")
        monkeypatch.setattr(
            "audiobookdl.gui.auth_panel.messagebox.showinfo",
            lambda *_a, **_kw: None,
        )
        app._auth.handle_oauth_connected("customoauth", "CustomOAuth", "default", True, None)
        assert app.username_var.get() == "keep_me"

        dlg = CookieImportDialog(
            root,
            app._services,
            initial_service_key="storytel",
            on_apply=lambda *_a: None,
            browser_detector=lambda: {"firefox": True},
        )
        dlg._on_output_edited()
        assert dlg._custom_output_path is False
        dlg.select_service("audiobooks.com")
        assert dlg._output_var.get() == default_imported_cookie_path("audiobooks.com")
        dlg.destroy()
    finally:
        app._on_close(force=True)
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud014_close_cleanup_tray_exit_zoomed_and_unattended_sync(tmp_path, monkeypatch):
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.sync import SyncCycleResult

    cfg_path = tmp_path / "audiobook-dl.toml"
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        app._window.normal_geometry = "800x600+20+20"
        monkeypatch.setattr(app._window, "is_window_maximized", lambda: True)
        app._window.on_root_configure(type("Ev", (), {"widget": app.root})())
        assert app._window.is_maximized is True
        assert app._window.normal_geometry == "800x600+20+20"

        modal_calls: list[str] = []
        toast_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "audiobookdl.gui.sync_coordinator.messagebox.showinfo",
            lambda *a, **kw: modal_calls.append("info"),
        )
        monkeypatch.setattr(
            "audiobookdl.gui.sync_coordinator.send_os_notification_async",
            lambda title, msg: toast_calls.append((title, msg)),
        )
        app._sync._last_sync_unattended = True
        cycle = SyncCycleResult(books_discovered=2, newly_added=1, downloaded=1, skipped=1, failed=0)
        app._sync.handle_sync_done(cycle, None)
        assert modal_calls == []
        assert len(toast_calls) == 1

        monkeypatch.setattr(type(app.controller), "is_uploading_abs", property(lambda _self: True))
        assert app._has_active_background_operations() is True
        monkeypatch.setattr(app._window, "collect_geometry_snapshot", lambda: (_ for _ in ()).throw(tk.TclError("bad window")))
        app._on_close(force=True)
        try:
            destroyed = not root.winfo_exists()
        except tk.TclError:
            destroyed = True
        assert destroyed
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_aud015_cross_page_validation_state_and_helpers(tmp_path):
    from audiobookdl.gui.formatting import clamp_geometry_to_monitors
    from audiobookdl.gui.options_state import _join_output_template
    from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.pack import PackBookSpec, PackOverrides
    from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata, BookId, Series

    assert clamp_geometry_to_monitors("300x220+10+10", [(0, 0, 1280, 800)], min_width=240, min_height=180) == "300x220+10+10"

    joined = _join_output_template(str(tmp_path), "/{series}/{title}")
    assert joined.startswith(str(tmp_path))
    assert ("/" not in joined[len(str(tmp_path)) :]) if os.sep == "\\" else True

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    cfg_path = tmp_path / "audiobook-dl.toml"
    app = AudiobookDLApp(root, config_location=str(cfg_path))
    try:
        dl_page = app.download_page
        qp = dl_page.queue_panel
        qp.set_previewing_state(False, is_running=True)
        assert qp.download_btn.cget("text") == "Add to Queue"
        assert str(qp.download_btn.cget("state")) == "normal"

        st = ScrolledTreeview(
            root,
            (TreeColumnSpec("title", "Title", width=120), TreeColumnSpec("num", "Num", width=60, sort_kind="number")),
        )
        st.tree.insert("", tk.END, iid="r1", values=("Alpha", "10"))
        st.tree.insert("", tk.END, iid="r2", values=("Beta", "-"))
        st.tree.insert("", tk.END, iid="r3", values=("Gamma", "2"))
        st.sort_by_column("num", descending=True)
        assert list(st.tree.get_children()) == ["r1", "r3", "r2"]
        st.destroy()

        app.split_chapters_var.set(True)
        app.m4b_var.set(True)
        app._options.on_m4b_toggled()
        assert app.split_chapters_var.get() is False
        assert app.combine_var.get() is True
        cfg_path.write_text("[gui]\nqueue_concurrency = 3\n", encoding="utf-8")
        app._options.reload_config()
        assert app.controller.queue_concurrency == 3

        pack_page = app.navigate("pack")
        spec = PackBookSpec(
            source_root=str(tmp_path / "book1"),
            track_paths=[str(tmp_path / "book1" / "01.mp3")],
            audiobook=Audiobook(
                session=None,  # type: ignore[arg-type]
                files=[],
                metadata=AudiobookMetadata(title="Book 1"),
            ),
            output_format="m4b",
            output_path=str(tmp_path / "Book 1.m4b"),
        )
        pack_page._preview_specs = [spec]
        bkey = pack_page._spec_key(spec)
        pack_page._book_overrides_by_key[bkey] = {"authors": [], "series_order": None}
        merged = pack_page._merge_book_overrides(PackOverrides(authors=["Base Author"], series_order=5), 0)
        assert merged is not None
        assert merged.authors == []
        assert merged.series_order is None

        from audiobookdl.gui.preview_dialog import PreviewDialog

        class _DummySrc:
            name = "Dummy"

            def close(self) -> None:
                pass

        series_res = Series(
            title="S",
            books=[
                BookId(id="a", title="Book Z"),
                BookId(id="b", title="Book A"),
                BookId(id="c", title="Book M"),
            ],
        )
        pdlg = PreviewDialog(
            root,
            "https://example.com/s",
            _DummySrc(),  # type: ignore[arg-type]
            series_res,
            is_running=False,
            skip_downloaded=False,
            series_book_exists=lambda _b: False,
            on_confirm=lambda *_a: None,
        )
        assert pdlg._tree is not None
        pdlg._tree.move("1", "", 0)
        pdlg._tree.move("2", "", 1)
        pdlg._tree.move("0", "", 2)
        for v in pdlg._check_vars:
            v.set(False)
        pdlg._check_vars[1].set(True)
        pdlg._toggle_series_item(1, False)
        pdlg._check_vars[2].set(True)
        pdlg._toggle_series_item(2, True)
        assert [v.get() for v in pdlg._check_vars] == [False, True, True]
        pdlg.destroy()
    finally:
        app._on_close(force=True)
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass


















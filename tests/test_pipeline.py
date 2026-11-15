import threading

import pytest
import requests

import audiobookdl
from audiobookdl import (
    Audiobook,
    AudiobookMetadata,
    Config,
    DownloadEvent,
    DownloadOptions,
    DownloadSummary,
    Downloader,
    args,
    logging,
)
from audiobookdl.utils.audiobook import BookId, Series


class _RecordingInteraction:
    def __init__(self, answers=None, confirm_value: bool = True) -> None:
        self.answers = dict(answers or {})
        self.confirm_value = confirm_value
        self.prompts: list[str] = []

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        self.prompts.append(prompt)
        return self.answers.get(prompt, "")

    def confirm(self, message: str) -> bool:
        self.prompts.append(message)
        return self.confirm_value


def test_public_api_exports():
    assert audiobookdl.Downloader is Downloader
    assert audiobookdl.DownloadOptions is DownloadOptions
    assert audiobookdl.DownloadSummary is DownloadSummary
    assert audiobookdl.DownloadEvent is DownloadEvent
    assert audiobookdl.Config is Config


def test_downloader_rejects_both_sink_and_on_event():
    class _DummySink:
        def on_log(self, level: str, message: str) -> None:
            pass

        def on_progress_start(self, description: str, total: float) -> None:
            pass

        def on_progress_advance(self, completed: float, total: float) -> None:
            pass

        def on_progress_finish(self) -> None:
            pass

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    with pytest.raises(ValueError, match="Specify either 'sink' or 'on_event'"):
        Downloader(cfg, args.build_default_options(), sink=_DummySink(), on_event=lambda _ev: None)


def test_downloader_events_isolation_and_selective_workflow(monkeypatch):
    meta_1 = AudiobookMetadata(title="First Title")
    meta_2 = AudiobookMetadata(title="Second Title")
    book_1 = Audiobook(session=requests.Session(), metadata=meta_1, files=[])
    book_2 = Audiobook(session=requests.Session(), metadata=meta_2, files=[])
    series = Series(title="Series One", books=[BookId("1"), BookId("2")])

    events: list[DownloadEvent] = []
    downloaded_titles: list[str] = []
    auth_calls: list[dict[str, str]] = []

    class _FakeAuthSource:
        name = "fakesource"
        requires_authentication = True
        supports_login = True
        login_data = ["username", "password"]

        def __init__(self, _options) -> None:
            self.authenticated = False

        def login(self, _url: str, **login_data: str) -> None:
            auth_calls.append(login_data)
            self.authenticated = True

        def download(self, _url: str) -> Series:
            return series

        def download_from_id(self, book_id: str) -> Audiobook:
            return {"1": book_1, "2": book_2}[book_id]

        def on_download_complete(self, _audiobook: Audiobook) -> None:
            pass

    def fake_process_audiobook(_src, ab: Audiobook, _opts, **_kw) -> None:
        downloaded_titles.append(ab.title)
        with logging.progress([]) as prog:
            task = prog.add_task(f"Downloading {ab.title}", total=100.0)
            prog.record_bytes(task, 2048)
            prog.advance(task, 50.0)
            prog.advance(task, 50.0)

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _FakeAuthSource)
    monkeypatch.setattr("audiobookdl.pipeline.process_audiobook", fake_process_audiobook)

    logging.debug_mode = False
    logging.quiet_mode = False
    logging.ffmpeg_output = False
    assert logging.get_ui_sink() is None

    config = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    options = args.build_default_options(debug=True, quiet=False)
    interaction = _RecordingInteraction(answers={"Username": "alice", "Password": "secret"})

    downloader = Downloader(
        config=config,
        options=options,
        on_event=events.append,
        interaction=interaction,
    )

    source, fetched = downloader.prepare("https://example.com/series")
    assert fetched is series
    assert auth_calls == [{"username": "alice", "password": "secret"}]
    assert downloaded_titles == []
    assert logging.get_job_context() is None

    # Reuse resolved source + series with selective download
    summary = downloader.download(
        "https://example.com/series",
        selected_indices=[1],
        prefetched=(source, fetched),  # type: ignore[arg-type]
    )
    assert summary == DownloadSummary(completed=1, failed=0, cancelled=False)
    assert downloaded_titles == ["Second Title"]

    # Global logging flags and sink remained isolated/untouched
    assert not logging.debug_mode
    assert not logging.quiet_mode
    assert not logging.ffmpeg_output
    assert logging.get_ui_sink() is None
    assert logging.get_job_context() is None

    kinds = {ev.kind for ev in events}
    assert {"log", "item_progress", "progress_start", "progress_advance", "progress_finish"} <= kinds
    log_messages = [ev.data["message"] for ev in events if ev.kind == "log"]
    assert any("Downloading 1 books in Series One" in msg for msg in log_messages)
    progress_starts = [ev.data["description"] for ev in events if ev.kind == "progress_start"]
    assert progress_starts == ["Downloading Second Title"]
    item_titles = [ev.data["title"] for ev in events if ev.kind == "item_progress"]
    assert "Second Title" in item_titles


def test_downloader_fetch_metadata_and_cancel_event(monkeypatch):
    single_book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Solo Book"),
        files=[],
    )

    class _SimpleSource:
        name = "simple"
        requires_authentication = False
        authenticated = True

        def __init__(self, _options) -> None:
            pass

        def download(self, _url: str) -> Audiobook:
            return single_book

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _SimpleSource)

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    cancel_event = threading.Event()
    downloader = Downloader(config=cfg, options=args.build_default_options(), cancel_event=cancel_event)

    assert downloader.fetch_metadata("https://example.com/solo") is single_book

    cancel_event.set()
    summary = downloader.download(["https://example.com/solo"])
    assert summary == DownloadSummary(completed=0, failed=0, cancelled=True)


def test_cli_main_delegates_to_downloader(monkeypatch):
    captured: dict = {}

    class _FakeDownloader:
        def __init__(self, config, options) -> None:
            captured["config"] = config
            captured["options"] = options

        def download(self, urls):
            captured["urls"] = list(urls)
            return DownloadSummary(completed=len(urls), failed=0, cancelled=False)

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    monkeypatch.setattr("sys.argv", ["audiobook-dl", "https://example.com/one"])
    monkeypatch.setattr("audiobookdl.__main__.load_config", lambda _loc: cfg)
    monkeypatch.setattr("audiobookdl.__main__.Downloader", _FakeDownloader)

    from audiobookdl.__main__ import main

    main()
    assert captured["config"] is cfg
    assert captured["urls"] == ["https://example.com/one"]
    assert captured["options"].database_directory is not None
    assert logging.get_job_context() is None
    # Restore module defaults after CLI test
    logging.debug_mode = False
    logging.quiet_mode = False
    logging.ffmpeg_output = False


def test_apply_runtime_options_merges_resume_and_audio_bitrate():
    from audiobookdl.pipeline import apply_runtime_options

    cfg = Config(
        sources={},
        output_template=None,
        database_directory=None,
        skip_downloaded=None,
        resume=True,
        audio_bitrate="192k",
    )
    opts = args.build_default_options()
    apply_runtime_options(opts, cfg)
    assert opts.resume is True
    assert opts.audio_bitrate == "192k"

    # Explicit "auto" CLI override beats config audio_bitrate
    opts_auto = args.build_default_options(audio_bitrate="auto")
    apply_runtime_options(opts_auto, cfg)
    assert opts_auto.audio_bitrate == "auto"

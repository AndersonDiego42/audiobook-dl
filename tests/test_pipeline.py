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


def test_downloader_keyboard_interrupt_marks_cancelled_and_stops_batch(monkeypatch):
    processed: list[str] = []

    def fake_process_url(url, options, config, **_kwargs):
        processed.append(url)
        raise KeyboardInterrupt

    monkeypatch.setattr("audiobookdl.pipeline.process_url", fake_process_url)
    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    downloader = Downloader(cfg, args.build_default_options())

    summary = downloader.download(["https://example.com/one", "https://example.com/two"])

    assert processed == ["https://example.com/one"]
    assert summary == DownloadSummary(completed=0, failed=0, cancelled=True)


@pytest.mark.parametrize(
    ("summary", "expected_code"),
    [
        (DownloadSummary(completed=0, failed=0, cancelled=True), 130),
        (DownloadSummary(completed=1, failed=1, cancelled=False), 1),
    ],
)
def test_cli_main_exit_codes(monkeypatch, summary, expected_code):
    class _FakeDownloader:
        def __init__(self, config, options) -> None:
            pass

        def download(self, urls):
            return summary

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    monkeypatch.setattr("sys.argv", ["audiobook-dl", "https://example.com/one"])
    monkeypatch.setattr("audiobookdl.__main__.load_config", lambda _loc: cfg)
    monkeypatch.setattr("audiobookdl.__main__.Downloader", _FakeDownloader)

    from audiobookdl.__main__ import main

    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == expected_code
    finally:
        logging.debug_mode = False
        logging.quiet_mode = False
        logging.ffmpeg_output = False


def test_download_cover_respects_output_template_and_creates_parents(tmp_path, monkeypatch):
    from audiobookdl import Cover
    from audiobookdl.pipeline import download_cover, process_audiobook

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="My Book", authors=["Jane Doe"]),
        files=[],
        cover=Cover(image=b"img-bytes", extension="jpg"),
    )

    # 1. Nested output template creates parent directories and avoids writing to CWD
    opts_nested = args.build_default_options(
        cover=True,
        output_template=str(tmp_path / "library" / "{author}" / "{title}"),
    )
    process_audiobook(None, book, opts_nested)  # type: ignore[arg-type]
    expected_nested = tmp_path / "library" / "Jane Doe" / "My Book.jpg"
    assert expected_nested.read_bytes() == b"img-bytes"
    assert not (cwd / "cover.jpg").exists()

    # 2. Trailing slash or existing book directory places cover.<ext> inside that directory
    opts_dir_slash = args.build_default_options(
        cover=True,
        output_template=str(tmp_path / "covers") + "/",
    )
    download_cover(book, opts_dir_slash)
    assert (tmp_path / "covers" / "cover.jpg").read_bytes() == b"img-bytes"

    existing_book_dir = tmp_path / "existing_dir" / "My Book"
    existing_book_dir.mkdir(parents=True)
    opts_existing_dir = args.build_default_options(
        cover=True,
        output_template=str(tmp_path / "existing_dir" / "{title}"),
    )
    download_cover(book, opts_existing_dir)
    assert (existing_book_dir / "cover.jpg").read_bytes() == b"img-bytes"

    # 3. Missing cover is a clean no-op
    book_no_cover = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="No Cover"),
        files=[],
        cover=None,
    )
    opts_none = args.build_default_options(
        cover=True,
        output_template=str(tmp_path / "missing" / "{title}"),
    )
    download_cover(book_no_cover, opts_none)
    assert not (tmp_path / "missing").exists()


def test_apply_runtime_options_toml_fallback_and_cli_override(tmp_path):
    from audiobookdl.pipeline import apply_runtime_options

    cfg = Config(
        sources={},
        output_template="{author}/{title}",
        database_directory=str(tmp_path / "custom_db"),
        skip_downloaded=True,
        output_format="m4b",
        combine=True,
        overwrite=True,
        max_workers=6,
        book_delay=0.5,
        proxy="http://127.0.0.1:9000",
        timeout=30.0,
    )
    cli_omitted = args.parse_arguments(["https://example.com/book"])
    apply_runtime_options(cli_omitted, cfg)
    assert cli_omitted.output_template == "{author}/{title}"
    assert cli_omitted.database_directory == str(tmp_path / "custom_db")
    assert cli_omitted.output_format == "m4b"
    assert cli_omitted.combine is True
    assert cli_omitted.skip_downloaded is True
    assert cli_omitted.overwrite is True
    assert cli_omitted.max_workers == 6
    assert cli_omitted.book_delay == 0.5
    assert cli_omitted.proxy == "http://127.0.0.1:9000"
    assert cli_omitted.timeout == 30.0

    # Explicit CLI flags (even back to "{title}" or --no-combine) must win over config
    cli_override = args.parse_arguments([
        "-o", "{title}",
        "--no-combine",
        "--no-skip-downloaded",
        "--no-overwrite",
        "https://example.com/book",
    ])
    apply_runtime_options(cli_override, cfg)
    assert cli_override.output_template == "{title}"
    assert cli_override.combine is False
    assert cli_override.skip_downloaded is False
    assert cli_override.overwrite is False


def test_series_summary_tracks_partial_failures_and_info_only_skips_downloads(monkeypatch):
    from audiobookdl.exceptions import BookNotReleased, GenericAudiobookDLException

    b1 = Audiobook(session=requests.Session(), metadata=AudiobookMetadata(title="Book One"), files=[])
    b2 = Audiobook(session=requests.Session(), metadata=AudiobookMetadata(title="Book Two"), files=[])
    series = Series(title="Saga", books=[BookId("1"), BookId("2"), BookId("3")])

    class _MixedSeriesSource:
        name = "fakesource"
        names = ["FakeSource"]
        requires_authentication = False
        authenticated = True

        def __init__(self, _options) -> None:
            pass

        def download(self, _url: str) -> Series:
            return series

        def download_from_id(self, book_id: str) -> Audiobook:
            if book_id == "1":
                return b1
            if book_id == "2":
                raise BookNotReleased
            raise GenericAudiobookDLException("broken third book")

        def on_download_complete(self, _ab: Audiobook) -> None:
            pass

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _MixedSeriesSource)
    downloaded: list[str] = []
    monkeypatch.setattr(
        "audiobookdl.pipeline.process_audiobook",
        lambda _s, ab, _o, **_kw: downloaded.append(ab.title),
    )

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    # 1. --info mode prints info without downloading
    info_events: list[DownloadEvent] = []
    info_summary = Downloader(
        cfg,
        args.build_default_options(info_only=True, series_items="1,3"),
        on_event=info_events.append,
    ).download(["https://example.com/saga"])
    assert downloaded == []
    assert info_summary == DownloadSummary(completed=1, failed=0, cancelled=False)
    info_logs = "\n".join(ev.data["message"] for ev in info_events if ev.kind == "log")
    assert "3 books, 2 selected" in info_logs

    # 2. Full series run records completed, skipped, and per-book errors cleanly
    full_summary = Downloader(cfg, args.build_default_options()).download(["https://example.com/saga"])
    assert downloaded == ["Book One"]
    assert full_summary.completed == 1
    assert full_summary.failed == 0
    assert full_summary.books_completed == 1
    assert full_summary.books_skipped == 1
    assert full_summary.books_failed == 1
    assert len(full_summary.book_errors) == 1
    assert "broken third book" in full_summary.book_errors[0][1]


def test_cli_list_sources_prints_services_and_exits(monkeypatch, capsys):
    from audiobookdl.__main__ import main

    monkeypatch.setattr("sys.argv", ["audiobook-dl", "--list-sources"])
    main()
    stdout = capsys.readouterr().out
    assert "storytel" in stdout
    assert "overdrive" in stdout


def test_apply_runtime_options_resolves_output_preset_unless_explicit_output():
    from audiobookdl.output.output import OUTPUT_TEMPLATE_PRESETS
    from audiobookdl.pipeline import apply_runtime_options

    cfg = Config(sources={}, output_template="{author} - {title}", database_directory=None, skip_downloaded=None)
    preset_opts = args.parse_arguments(["--output-preset", "audiobookshelf", "https://example.com"])
    apply_runtime_options(preset_opts, cfg)
    assert preset_opts.output_template == OUTPUT_TEMPLATE_PRESETS["audiobookshelf"]

    override_opts = args.parse_arguments(["--output-preset", "audiobookshelf", "-o", "{title}", "https://example.com"])
    apply_runtime_options(override_opts, cfg)
    assert override_opts.output_template == "{title}"


def test_download_archive_records_and_skips_repeat_downloads(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from audiobookdl.pipeline import run_downloads
    from audiobookdl.utils.archive import DownloadArchive
    from audiobookdl.utils.audiobook import AudiobookFile

    archive_file = tmp_path / "ledger.db"
    download_calls = []

    def fake_download(audiobook, opts, interaction=None, cancel_event=None):
        download_calls.append(audiobook.title)

    monkeypatch.setattr("audiobookdl.pipeline.download", fake_download)

    fake_source = MagicMock()
    fake_source.name = "librivox"
    fake_book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(
            title="Archived Book",
            authors=["Author A"],
            scrape_url="https://librivox.org/book/999",
        ),
        files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
    )
    opts = args.build_default_options(
        download_archive=str(archive_file),
        database_directory=str(tmp_path / "db"),
        output_template=str(tmp_path / "{title}"),
    )
    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)

    summary1 = run_downloads(
        ["https://librivox.org/book/999"],
        opts,
        cfg,
        prefetched=(fake_source, fake_book),
    )
    assert summary1.completed == 1
    assert summary1.books_completed == 1
    assert download_calls == ["Archived Book"]

    archive = DownloadArchive(str(archive_file))
    entries = archive.list_entries()
    assert len(entries) == 1
    assert entries[0].title == "Archived Book"

    # Second run skips via archive without calling download again
    summary2 = run_downloads(
        ["https://librivox.org/book/999"],
        opts,
        cfg,
        prefetched=(fake_source, fake_book),
    )
    assert summary2.books_skipped == 1
    assert download_calls == ["Archived Book"]


def test_resolve_redirect_url_fallback_and_prepare_source_result(tmp_path, monkeypatch):
    from audiobookdl.pipeline import prepare_source_result
    from audiobookdl.utils.enrichment import resolve_redirect_url

    class _FakeSession:
        def head(self, url, **_kwargs):
            resp = requests.Response()
            resp.status_code = 200
            resp.url = "https://librivox.org/the-time-machine"
            return resp

    resolved = resolve_redirect_url("https://short.example/tm", session=_FakeSession())
    assert resolved == "https://librivox.org/the-time-machine"

    class _MockLibrivox:
        name = "librivox"
        requires_authentication = False
        _session = requests.Session()

        def __init__(self, _options) -> None:
            pass

        def download(self, url: str) -> Audiobook:
            return Audiobook(
                session=self._session,
                metadata=AudiobookMetadata(title=f"Fetched:{url}"),
                files=[],
            )

    from audiobookdl.exceptions import NoSourceFound

    def _find_compat(url: str):
        if url.startswith("https://librivox.org/"):
            return _MockLibrivox
        raise NoSourceFound()

    monkeypatch.setattr(
        "audiobookdl.pipeline.resolve_redirect_url",
        lambda url, **_kw: "https://librivox.org/the-time-machine",
    )
    monkeypatch.setattr(
        "audiobookdl.pipeline.find_compatible_source",
        _find_compat,
    )

    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    opts = args.build_default_options()
    _src, result = prepare_source_result("https://short.example/tm", opts, cfg)
    assert isinstance(result, Audiobook)
    assert result.title == "Fetched:https://librivox.org/the-time-machine"


def test_enrich_metadata_from_openlibrary_fills_missing_without_overwriting():
    from datetime import date
    from audiobookdl.utils.enrichment import enrich_metadata_from_openlibrary

    payload = {
        "docs": [
            {
                "isbn": ["9780441172719"],
                "publisher": ["New Publisher"],
                "first_publish_year": 1965,
                "subject": ["Science Fiction", "Classics"],
                "first_sentence": ["A beginning is the time..."],
            }
        ]
    }

    class _OkSession:
        def get(self, *_args, **_kwargs):
            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return payload

            return _Resp()

    meta = AudiobookMetadata(title="Dune", authors=["Frank Herbert"], publisher="Existing Pub")
    updated = enrich_metadata_from_openlibrary(meta, session=_OkSession())
    assert updated is True
    assert meta.publisher == "Existing Pub"
    assert meta.isbn == "9780441172719"
    assert meta.release_date == date(1965, 1, 1)
    assert "Science Fiction" in meta.genres
    assert meta.description == "A beginning is the time..."

    class _OfflineSession:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    assert enrich_metadata_from_openlibrary(AudiobookMetadata(title="Dune"), session=_OfflineSession()) is False


def test_verify_service_auth_caches_and_reloads_cookies(tmp_path, monkeypatch):
    from audiobookdl.pipeline import authenticate, verify_service_auth
    from audiobookdl.sources.source import Source

    loaded_paths = []

    class _CookieTestSource(Source):
        name = "cookietest"
        names = ["CookieTest"]
        _authentication_methods = ["cookies"]

        def cached_cookie_path(self) -> str:
            return str(tmp_path / "cookietest.cookies.txt")

        def verify_auth(self) -> bool:
            self._session.cookies.set("session_tok", "abc123", domain="example.com")
            return True

        def load_cookie_file(self, cookie_file: str) -> None:
            loaded_paths.append(cookie_file)
            super().load_cookie_file(cookie_file)

    monkeypatch.setattr("audiobookdl.pipeline.find_source_by_name", lambda _name: _CookieTestSource)

    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    opts = args.build_default_options()
    assert verify_service_auth("cookietest", opts, cfg) is True
    cookie_cache = tmp_path / "cookietest.cookies.txt"
    assert cookie_cache.is_file()
    assert "session_tok" in cookie_cache.read_text(encoding="utf-8")

    # Separate Source instance auto-loads the cached cookie file without -c flag
    fresh = _CookieTestSource(opts)
    authenticate("https://example.com/book", fresh, opts, cfg)
    assert loaded_paths == [str(cookie_cache)]
    assert fresh._session.cookies.get("session_tok") == "abc123"


def test_search_service_librivox_and_list_service_library_storytel(tmp_path, monkeypatch):
    from audiobookdl.pipeline import list_service_library, search_service

    class _FakeResp:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self._payload = payload
            import json
            self.content = json.dumps(payload).encode("utf-8")

        def json(self) -> dict:
            return self._payload

    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    opts = args.build_default_options()

    def _mock_librivox_get(_self, url, **_kwargs):
        assert "Dune" in url
        return _FakeResp(
            {
                "books": [
                    {
                        "title": "LibriVox Odyssey",
                        "url_librivox": "https://librivox.org/odyssey",
                        "authors": [{"first_name": "Homer", "last_name": ""}],
                    }
                ]
            }
        )

    monkeypatch.setattr("requests.Session.get", _mock_librivox_get)
    lv_results = search_service("librivox", "Dune", opts, cfg)
    assert len(lv_results) == 1
    assert lv_results[0].title == "LibriVox Odyssey"
    assert lv_results[0].author == "Homer"
    assert lv_results[0].url == "https://librivox.org/odyssey"

    monkeypatch.setattr(
        "audiobookdl.sources.storytel.StorytelSource._login",
        lambda self, _url, **_kw: None,
    )
    monkeypatch.setattr(
        "audiobookdl.sources.storytel.StorytelSource.download_bookshelf",
        lambda _self: {
            "items": {
                "1": {"model": {"id": "99", "name": "Shelf Book", "authors": [{"name": "A. Writer"}]}}
            }
        },
    )
    cfg_st = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    st_results = list_service_library(
        "storytel",
        args.build_default_options(username="u", password="p"),
        cfg_st,
    )
    assert len(st_results) == 1
    assert st_results[0].title == "Shelf Book"
    assert st_results[0].author == "A. Writer"
    assert st_results[0].url == "https://www.storytel.com/books/99"


def test_apply_runtime_options_media_type_precedence():
    from audiobookdl.pipeline import apply_runtime_options

    cfg = Config(
        sources={},
        output_template=None,
        database_directory="",
        skip_downloaded=False,
        media_type="ebook",
    )
    opts_default = args.build_default_options()
    apply_runtime_options(opts_default, cfg)
    assert opts_default.media_type == "ebook"

    opts_cli = args.parse_arguments(["--media-type", "both", "https://example.com/book"])
    apply_runtime_options(opts_cli, cfg)
    assert opts_cli.media_type == "both"


def test_archive_distinguishes_ebook_and_audiobook_and_records_epub_format(tmp_path, monkeypatch):
    from audiobookdl import AudiobookFile
    from audiobookdl.pipeline import process_audiobook
    from audiobookdl.utils.archive import DownloadArchive, make_book_key

    audio = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Same Title", scrape_url="https://www.storytel.com/books/1"),
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
    )
    ebook = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Same Title", scrape_url="https://www.storytel.com/books/1"),
        files=[AudiobookFile(url="https://example.com/b.epub", ext="epub")],
    )
    key_audio = make_book_key("storytel", audio)
    key_ebook = make_book_key("storytel", ebook)
    assert key_audio != key_ebook
    assert key_ebook.endswith("::ebook")

    class _FakeSource:
        name = "storytel"
        skip_downloaded = False

        def on_download_complete(self, _book):
            pass

    monkeypatch.setattr("audiobookdl.output.download.download_audiobook", lambda *a, **kw: None)
    db_path = tmp_path / "downloads.db"
    opts = args.build_default_options(
        output_format="m4b",
        output_template=str(tmp_path / "{title}"),
        download_archive=str(db_path),
    )
    assert process_audiobook(_FakeSource(), ebook, opts) is True

    archive = DownloadArchive(str(db_path))
    assert archive.contains("storytel", key_ebook)
    assert not archive.contains("storytel", key_audio)
    entries = archive.list_entries()
    assert len(entries) == 1
    assert entries[0].book_key == key_ebook
    assert entries[0].audio_format == "epub"


def test_download_result_series_handles_book_has_no_ebook_and_dual_format_series(tmp_path, monkeypatch):
    from audiobookdl import AudiobookFile
    from audiobookdl.exceptions import BookHasNoEbook
    from audiobookdl.pipeline import download_result

    class _FakeDualSource:
        name = "storytel"
        skip_downloaded = False

        def on_download_complete(self, _book):
            pass

        def download_from_id(self, book_id):
            if book_id == "missing-ebook":
                raise BookHasNoEbook
            if book_id == "dual":
                abook = Audiobook(
                    session=requests.Session(),
                    metadata=AudiobookMetadata(title="Dual"),
                    files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
                )
                ebook = Audiobook(
                    session=requests.Session(),
                    metadata=AudiobookMetadata(title="Dual"),
                    files=[AudiobookFile(url="https://example.com/b.epub", ext="epub")],
                )
                return Series(title="Dual", books=[abook, ebook])
            raise AssertionError(f"Unexpected id: {book_id}")

    downloaded_exts: list[str] = []

    def _fake_download_audiobook(book, *_args, **_kwargs):
        downloaded_exts.append(book.files[0].ext)

    monkeypatch.setattr("audiobookdl.output.download.download_audiobook", _fake_download_audiobook)
    series = Series(title="Test Series", books=[BookId("missing-ebook"), BookId("dual")])
    summary = DownloadSummary()
    opts = args.build_default_options(output_template=str(tmp_path / "{title}"))

    download_result(_FakeDualSource(), series, opts, summary=summary)
    assert downloaded_exts == ["mp3", "epub"]
    assert summary.books_completed == 2
    assert summary.books_failed == 0


def test_enrichment_and_updater_close_auto_sessions_and_process_reuses_source_session(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from audiobookdl import AudiobookFile
    from audiobookdl.utils.enrichment import enrich_metadata_from_openlibrary
    from audiobookdl.utils.updater import check_for_updates

    closed_count = 0

    class _FakeAutoSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            nonlocal closed_count
            closed_count += 1

        def get(self, *_args, **_kwargs):
            return MagicMock(status_code=200, json=lambda: {"tag_name": "v9.9.9", "html_url": "https://x"})

    monkeypatch.setattr("audiobookdl.utils.updater.requests.Session", _FakeAutoSession)
    info = check_for_updates(current_version="0.1.0", respect_opt_out=False)
    assert info is not None and info.has_update is True
    assert closed_count == 1

    enriched_sessions = []

    def fake_enrich(metadata, **kwargs):
        enriched_sessions.append(kwargs.get("session"))
        return True

    fake_session = MagicMock()
    src = MagicMock()
    src.name = "storytel"
    src._session = fake_session
    book = Audiobook(
        session=fake_session,
        metadata=AudiobookMetadata(title="Enrich Book"),
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
    )
    opts = args.build_default_options(
        output_template=str(tmp_path / "{title}"),
        enrich_metadata=True,
    )
    with (
        patch("audiobookdl.pipeline.enrich_metadata_from_openlibrary", side_effect=fake_enrich),
        patch("audiobookdl.pipeline.download"),
    ):
        from audiobookdl.pipeline import process_audiobook

        process_audiobook(src, book, opts)
    assert enriched_sessions == [fake_session]

    monkeypatch.setattr("audiobookdl.utils.enrichment.requests.Session", _FakeAutoSession)
    enrich_metadata_from_openlibrary(AudiobookMetadata(title="Fallback Close"))
    assert closed_count == 2


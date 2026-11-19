from collections.abc import Sequence
import threading
from typing import Any, cast

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
    logging,
)
from audiobookdl.options_schema import build_default_options
from audiobookdl.sources.source import Source
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

    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: int | None = None,
    ) -> int | None:
        self.prompts.append(title)
        return default


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
        Downloader(cfg, build_default_options(), sink=_DummySink(), on_event=lambda _ev: None)


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
        with logging.progress() as prog:
            task = prog.add_task(f"Downloading {ab.title}", total=100.0)
            prog.record_bytes(task, 2048)
            prog.advance(task, 50.0)
            prog.advance(task, 50.0)

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _FakeAuthSource)
    monkeypatch.setattr("audiobookdl.pipeline.process_audiobook", fake_process_audiobook)

    assert not logging.is_debug_mode()
    assert not logging.is_quiet_mode()
    assert logging.get_ui_sink() is None

    config = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    options = build_default_options(debug=True)
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

    # JobContext flags and sink remained isolated/untouched
    assert not logging.is_debug_mode()
    assert not logging.is_quiet_mode()
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
    downloader = Downloader(config=cfg, options=build_default_options(), cancel_event=cancel_event)

    assert downloader.fetch_metadata("https://example.com/solo") is single_book

    cancel_event.set()
    summary = downloader.download(["https://example.com/solo"])
    assert summary == DownloadSummary(completed=0, failed=0, cancelled=True)


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
    opts = build_default_options()
    apply_runtime_options(opts, cfg)
    assert opts.resume is True
    assert opts.audio_bitrate == "192k"

    # Explicit "auto" option override beats config audio_bitrate
    opts_auto = build_default_options(audio_bitrate="auto")
    apply_runtime_options(opts_auto, cfg)
    assert opts_auto.audio_bitrate == "auto"


def test_downloader_keyboard_interrupt_marks_cancelled_and_stops_batch(monkeypatch):
    processed: list[str] = []

    def fake_process_url(url, options, config, **_kwargs):
        processed.append(url)
        raise KeyboardInterrupt

    monkeypatch.setattr("audiobookdl.pipeline.process_url", fake_process_url)
    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    downloader = Downloader(cfg, build_default_options())

    summary = downloader.download(["https://example.com/one", "https://example.com/two"])

    assert processed == ["https://example.com/one"]
    assert summary == DownloadSummary(completed=0, failed=0, cancelled=True)


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
    opts_nested = build_default_options(
        cover=True,
        output_template=str(tmp_path / "library" / "{author}" / "{title}"),
    )
    process_audiobook(None, book, opts_nested)  # type: ignore[arg-type]
    expected_nested = tmp_path / "library" / "Jane Doe" / "My Book.jpg"
    assert expected_nested.read_bytes() == b"img-bytes"
    assert not (cwd / "cover.jpg").exists()

    # 2. Trailing slash or existing book directory places cover.<ext> inside that directory
    opts_dir_slash = build_default_options(
        cover=True,
        output_template=str(tmp_path / "covers") + "/",
    )
    download_cover(book, opts_dir_slash)
    assert (tmp_path / "covers" / "cover.jpg").read_bytes() == b"img-bytes"

    existing_book_dir = tmp_path / "existing_dir" / "My Book"
    existing_book_dir.mkdir(parents=True)
    opts_existing_dir = build_default_options(
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
    opts_none = build_default_options(
        cover=True,
        output_template=str(tmp_path / "missing" / "{title}"),
    )
    download_cover(book_no_cover, opts_none)
    assert not (tmp_path / "missing").exists()


def test_apply_runtime_options_toml_fallback_and_explicit_override(tmp_path):
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
    omitted_opts = build_default_options()
    apply_runtime_options(omitted_opts, cfg)
    assert omitted_opts.output_template == "{author}/{title}"
    assert omitted_opts.database_directory == str(tmp_path / "custom_db")
    assert omitted_opts.output_format == "m4b"
    assert omitted_opts.combine is True
    assert omitted_opts.skip_downloaded is True
    assert omitted_opts.overwrite is True
    assert omitted_opts.max_workers == 6
    assert omitted_opts.book_delay == 0.5
    assert omitted_opts.proxy == "http://127.0.0.1:9000"
    assert omitted_opts.timeout == 30.0

    # Explicit overrides (even back to "{title}" or False) must win over config
    explicit_opts = build_default_options(
        output_template="{title}",
        combine=False,
        skip_downloaded=False,
        overwrite=False,
    )
    apply_runtime_options(explicit_opts, cfg)
    assert explicit_opts.output_template == "{title}"
    assert explicit_opts.combine is False
    assert explicit_opts.skip_downloaded is False
    assert explicit_opts.overwrite is False


def test_series_summary_tracks_partial_failures(monkeypatch):
    from audiobookdl.exceptions import BookNotReleased, GenericAudiobookDLException

    b1 = Audiobook(session=requests.Session(), metadata=AudiobookMetadata(title="Book One"), files=[])
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
    # Full series run records completed, skipped, and per-book errors cleanly
    full_summary = Downloader(cfg, build_default_options()).download(["https://example.com/saga"])
    assert downloaded == ["Book One"]
    assert full_summary.completed == 1
    assert full_summary.failed == 0
    assert full_summary.books_completed == 1
    assert full_summary.books_skipped == 1
    assert full_summary.books_failed == 1
    assert len(full_summary.book_errors) == 1
    assert "broken third book" in full_summary.book_errors[0][1]


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
    opts = build_default_options(
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

    resolved = resolve_redirect_url("https://short.example/tm", session=cast(requests.Session, _FakeSession()))
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
    opts = build_default_options()
    _src, result = prepare_source_result("https://short.example/tm", opts, cfg)
    assert isinstance(result, Audiobook)
    assert result.title == "Fetched:https://librivox.org/the-time-machine"


def test_enrich_metadata_from_openlibrary_fills_missing_without_overwriting():
    from datetime import date
    from audiobookdl import AudiobookFile
    from audiobookdl.utils.enrichment import enrich_audiobook_metadata

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
    book = Audiobook(
        session=cast(requests.Session, _OkSession()),
        metadata=meta,
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
    )
    res = enrich_audiobook_metadata(
        book,
        providers=("openlibrary",),
        session=cast(requests.Session, _OkSession()),
        include_chapters=False,
    )
    assert res.updated is True
    assert meta.publisher == "Existing Pub"
    assert meta.isbn == "9780441172719"
    assert meta.release_date == date(1965, 1, 1)
    assert "Science Fiction" in meta.genres
    assert meta.description == "A beginning is the time..."

    class _OfflineSession:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    offline_book = Audiobook(
        session=cast(requests.Session, _OfflineSession()),
        metadata=AudiobookMetadata(title="Dune"),
        files=[],
    )
    offline_res = enrich_audiobook_metadata(
        offline_book,
        providers=("openlibrary",),
        session=cast(requests.Session, _OfflineSession()),
        include_chapters=False,
    )
    assert offline_res.updated is False


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

        def verify_auth(self, url: str = "") -> bool:
            self._session.cookies.set("session_tok", "abc123", domain="example.com")
            return True

        def load_cookie_file(self, cookie_file: str) -> None:
            loaded_paths.append(cookie_file)
            super().load_cookie_file(cookie_file)

    monkeypatch.setattr("audiobookdl.pipeline.find_source_by_name", lambda _name: _CookieTestSource)

    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    opts = build_default_options()
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
    opts = build_default_options()

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
        build_default_options(username="u", password="p"),
        cfg_st,
    )
    assert len(st_results) == 1
    assert st_results[0].title == "Shelf Book"
    assert st_results[0].author == "A. Writer"
    assert st_results[0].url == "https://www.storytel.com/en/books/99"


def test_apply_runtime_options_media_type_precedence():
    from audiobookdl.pipeline import apply_runtime_options

    cfg = Config(
        sources={},
        output_template=None,
        database_directory="",
        skip_downloaded=False,
        media_type="ebook",
    )
    opts_default = build_default_options()
    apply_runtime_options(opts_default, cfg)
    assert opts_default.media_type == "ebook"

    opts_explicit = build_default_options(media_type="both")
    apply_runtime_options(opts_explicit, cfg)
    assert opts_explicit.media_type == "both"


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
    opts = build_default_options(
        output_format="m4b",
        output_template=str(tmp_path / "{title}"),
        download_archive=str(db_path),
    )
    assert process_audiobook(cast(Source[Any], _FakeSource()), ebook, opts) is True

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
    opts = build_default_options(output_template=str(tmp_path / "{title}"))

    download_result(cast(Source[Any], _FakeDualSource()), series, opts, summary=summary)
    assert downloaded_exts == ["mp3", "epub"]
    assert summary.books_completed == 2
    assert summary.books_failed == 0


def test_enrichment_and_updater_close_auto_sessions_and_process_isolates_source_session(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from audiobookdl import AudiobookFile
    from audiobookdl.utils.enrichment import EnrichmentResult, enrich_audiobook_metadata
    from audiobookdl.utils.updater import check_for_updates

    closed_count = 0

    class _FakeAutoSession:
        def __init__(self):
            self.headers = {}
            self.proxies = {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            nonlocal closed_count
            closed_count += 1

        def mount(self, *_args, **_kwargs):
            pass

        def close(self):
            nonlocal closed_count
            closed_count += 1

        def get(self, *_args, **_kwargs):
            return MagicMock(status_code=200, json=lambda: {"tag_name": "v9.9.9", "html_url": "https://x"})

    with monkeypatch.context() as mp:
        mp.setattr("audiobookdl.utils.updater.requests.Session", _FakeAutoSession)
        info = check_for_updates(current_version="0.1.0", respect_opt_out=False)
    assert info is not None and info.has_update is True
    assert closed_count == 1

    enriched_snapshots: list[tuple[Any, dict[str, str], dict[str, str]]] = []

    def fake_enrich(audiobook, **kwargs):
        sess = kwargs.get("session")
        enriched_snapshots.append(
            (
                sess,
                dict(getattr(sess, "headers", {})),
                dict(getattr(sess, "cookies", {})),
            )
        )
        return EnrichmentResult(updated=True, providers_used=["openlibrary"])

    source_session = requests.Session()
    source_session.headers.update(
        {
            "Authorization": "Bearer secret-source-jwt",
            "X-Login-Token": "secret-login",
            "X-Profile-Token": "secret-profile",
            "Session-Key": "secret-session-key",
        }
    )
    source_session.cookies.set("sid", "secret-cookie", domain="storytel.com")
    src = MagicMock()
    src.name = "storytel"
    src._session = source_session
    book = Audiobook(
        session=source_session,
        metadata=AudiobookMetadata(title="Enrich Book"),
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
    )
    opts = build_default_options(
        output_template=str(tmp_path / "{title}"),
        enrich_metadata=True,
    )
    with (
        patch("audiobookdl.pipeline.enrich_audiobook_metadata", side_effect=fake_enrich),
        patch("audiobookdl.pipeline.download"),
    ):
        from audiobookdl.pipeline import process_audiobook

        process_audiobook(src, book, opts)

    assert len(enriched_snapshots) == 1
    used_sess, used_headers, used_cookies = enriched_snapshots[0]
    assert used_sess is not source_session
    assert isinstance(used_sess, requests.Session)
    lowered_headers = {k.lower(): v for k, v in used_headers.items()}
    assert "authorization" not in lowered_headers
    assert "x-login-token" not in lowered_headers
    assert "x-profile-token" not in lowered_headers
    assert "session-key" not in lowered_headers
    assert used_cookies == {}

    monkeypatch.setattr("audiobookdl.utils.http.requests.Session", _FakeAutoSession)
    enrich_audiobook_metadata(
        Audiobook(session=None, metadata=AudiobookMetadata(title="Fallback Close"), files=[]),
        providers=("openlibrary",),
        include_chapters=False,
    )
    assert closed_count == 2


def test_authenticate_isolates_profiles_and_persists_login_cookies(tmp_path):
    import os

    from audiobookdl.config import SourceConfig
    from audiobookdl.pipeline import authenticate
    from audiobookdl.sources.source import Source

    login_calls = []

    class _ProfileSource(Source):
        name = "profilesvc"
        names = ["ProfileSvc"]
        _authentication_methods = ["login", "cookies"]

        def _login(self, url: str, username: str, password: str):
            login_calls.append((url, username, password))
            self._session.cookies.set("auth_user", username, domain="example.com")

    cfg = Config(
        sources={
            "profilesvc": SourceConfig("main@example.com", "pw1", None, None),
            "profilesvc:work": SourceConfig("work@example.com", "pw2", None, None),
        },
        output_template=None,
        database_directory=str(tmp_path),
        skip_downloaded=False,
    )

    work_opts = build_default_options(database_directory=str(tmp_path), profile="work")
    work_src = _ProfileSource(work_opts)
    authenticate("https://example.com/book", work_src, work_opts, cfg)
    assert login_calls == [("https://example.com/book", "work@example.com", "pw2")]
    assert work_src.cached_cookie_path().endswith("session_cookies_work.txt")
    assert os.path.isfile(work_src.cached_cookie_path())

    # Fresh source for same profile reloads cached cookies without re-logging in
    work_src2 = _ProfileSource(work_opts)
    authenticate("https://example.com/book", work_src2, work_opts, cfg)
    assert len(login_calls) == 1
    assert work_src2._session.cookies.get("auth_user") == "work@example.com"

    # Default profile uses its own credentials and separate cookie file
    default_opts = build_default_options(database_directory=str(tmp_path))
    default_src = _ProfileSource(default_opts)
    authenticate("https://example.com/book", default_src, default_opts, cfg)
    assert login_calls[-1] == ("https://example.com/book", "main@example.com", "pw1")
    assert default_src.cached_cookie_path().endswith("session_cookies.txt")


def test_explicit_none_download_archive_bypasses_reading_and_writing_downloads_db(tmp_path):
    from unittest.mock import MagicMock, patch
    from audiobookdl import AudiobookFile
    from audiobookdl.pipeline import process_audiobook
    from audiobookdl.utils.archive import DownloadArchive, make_book_key

    db_dir = tmp_path / "db"
    archive_path = db_dir / "downloads.db"
    book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Opt Out Book", authors=["A"], scrape_url="https://ex.com/opt"),
        files=[AudiobookFile(url="https://ex.com/opt.mp3", ext="mp3")],
    )
    key = make_book_key("storytel", book)
    DownloadArchive(str(archive_path)).record("storytel", key, title="Opt Out Book", output_path="old.mp3")

    opts = build_default_options(
        output_template=str(tmp_path / "{title}"),
        database_directory=str(db_dir),
        skip_downloaded=True,
        download_archive=None,
    )
    src = MagicMock()
    src.name = "storytel"

    new_book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Fresh Opt Out", authors=["B"], scrape_url="https://ex.com/fresh"),
        files=[AudiobookFile(url="https://ex.com/fresh.mp3", ext="mp3")],
    )

    with patch("audiobookdl.pipeline.download") as mock_dl:
        # Must NOT skip pre-recorded entry when download_archive=None is explicitly passed
        assert process_audiobook(src, book, opts) is True
        # Must NOT insert new entry into downloads.db when download_archive=None is explicitly passed
        assert process_audiobook(src, new_book, opts) is True
    assert mock_dl.call_count == 2
    assert len(DownloadArchive(str(archive_path)).list_entries()) == 1


def test_apply_runtime_options_isolates_ffmpeg_dir_per_job_context(tmp_path):
    import sys
    from audiobookdl import logging, utils
    from audiobookdl.pipeline import apply_runtime_options

    bin_a = tmp_path / "ff_a"
    bin_b = tmp_path / "ff_b"
    bin_a.mkdir()
    bin_b.mkdir()
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (bin_a / exe_name).write_bytes(b"a")
    (bin_b / exe_name).write_bytes(b"b")

    cfg_a = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None, ffmpeg_dir=str(bin_a))
    cfg_b = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None, ffmpeg_dir=str(bin_b))
    ctx_a = apply_runtime_options(build_default_options(), cfg_a)
    ctx_b = apply_runtime_options(build_default_options(), cfg_b)

    assert utils.get_ffmpeg_dir() is None

    with logging.job_context(ctx_a):
        assert utils.get_ffmpeg_dir() == str(bin_a)
        assert utils.find_executable("ffmpeg") == str(bin_a / exe_name)
    with logging.job_context(ctx_b):
        assert utils.get_ffmpeg_dir() == str(bin_b)
        assert utils.find_executable("ffmpeg") == str(bin_b / exe_name)
    assert utils.get_ffmpeg_dir() is None


def test_fetch_metadata_and_process_url_close_owned_source_sessions(tmp_path, monkeypatch):
    from audiobookdl import AudiobookFile
    from audiobookdl.pipeline import fetch_metadata, process_url

    closed_flags = []

    class _MockSource:
        name = "mock"
        skip_downloaded = False
        requires_authentication = False
        authenticated = True

        def close(self):
            closed_flags.append("closed")

        def on_download_complete(self, _book):
            pass

        def download(self, url):
            return Audiobook(
                session=requests.Session(),
                metadata=AudiobookMetadata(title="Cleanup Book", scrape_url=url),
                files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
            )

    monkeypatch.setattr(
        "audiobookdl.pipeline.prepare_source_result",
        lambda url, *_a, **_kw: (_MockSource(), _MockSource().download(url)),
    )
    opts = build_default_options(output_template=str(tmp_path / "{title}"), skip_downloaded=False)
    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)

    fetch_metadata("https://example.com/book", opts, cfg)
    assert len(closed_flags) == 1

    monkeypatch.setattr("audiobookdl.pipeline.download_result", lambda *_a, **_kw: None)
    process_url("https://example.com/book", opts, cfg)
    assert len(closed_flags) == 2


def test_post_exec_hook_injects_metadata_env_vars(tmp_path):
    import subprocess
    from unittest.mock import patch
    from audiobookdl.pipeline import DEFAULT_POST_EXEC_TIMEOUT, run_post_exec_hook

    # run_post_exec_hook injects AUDIOBOOK_DL_* environment variables and passes timeout/capture_output
    captured_env = {}

    def fake_subproc_run(cmd, shell=False, env=None, check=False, **kwargs):
        captured_env["cmd"] = cmd
        captured_env["shell"] = shell
        captured_env["env"] = dict(env or {})
        captured_env["kwargs"] = kwargs
        return type("_R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    with patch("audiobookdl.pipeline.subprocess.run", side_effect=fake_subproc_run):
        rc = run_post_exec_hook(
            "notify-hook",
            output_path=str(tmp_path / "Book.m4b"),
            metadata=AudiobookMetadata(title="Hook Book", authors=["Hook Author"], source="librivox"),
            audio_format="m4b",
        )
    assert rc == 0
    assert captured_env["cmd"] == "notify-hook"
    assert captured_env["kwargs"]["timeout"] == DEFAULT_POST_EXEC_TIMEOUT
    assert captured_env["kwargs"]["capture_output"] is True
    assert captured_env["kwargs"]["text"] is True
    assert captured_env["env"]["AUDIOBOOK_DL_TITLE"] == "Hook Book"
    assert captured_env["env"]["AUDIOBOOK_DL_AUTHOR"] == "Hook Author"
    assert captured_env["env"]["AUDIOBOOK_DL_FORMAT"] == "m4b"
    assert captured_env["env"]["AUDIOBOOK_DL_SOURCE"] == "librivox"

    # Timeout and nonzero stderr are logged cleanly and return None / nonzero without crashing
    logged: list[str] = []
    with (
        patch(
            "audiobookdl.pipeline.subprocess.run",
            side_effect=subprocess.TimeoutExpired("notify-hook", 1.5, stderr=b"hung"),
        ),
        patch("audiobookdl.pipeline.logging.error", side_effect=logged.append),
    ):
        assert (
            run_post_exec_hook(
                "notify-hook",
                output_path=str(tmp_path / "Book.m4b"),
                metadata=AudiobookMetadata(title="Hook Book"),
                audio_format="m4b",
                timeout=1.5,
            )
            is None
        )
    assert any("timed out" in msg for msg in logged)

    logged.clear()
    with (
        patch(
            "audiobookdl.pipeline.subprocess.run",
            return_value=type("_R", (), {"returncode": 2, "stdout": "", "stderr": "boom"})(),
        ),
        patch("audiobookdl.pipeline.logging.error", side_effect=logged.append),
    ):
        assert (
            run_post_exec_hook(
                "notify-hook",
                output_path=str(tmp_path / "Book.m4b"),
                metadata=AudiobookMetadata(title="Hook Book"),
                audio_format="m4b",
            )
            == 2
        )
    assert any("boom" in msg for msg in logged)


def test_source_impersonate_session_and_cookie_bridge(tmp_path, monkeypatch):
    from http.cookiejar import CookieJar
    from types import SimpleNamespace
    from audiobookdl.exceptions import GenericAudiobookDLException
    from audiobookdl.sources.source import Source

    class _DummySource(Source):
        names = ["dummy_imp"]
        match = [r"https://example\.com/.*"]

        def download(self, url):  # pragma: no cover - not invoked
            raise NotImplementedError

    # 1. Helpful error when curl_cffi is not installed
    def missing_import(name):
        if name == "curl_cffi.requests":
            raise ImportError("No module named 'curl_cffi'")
        return __import__(name)

    monkeypatch.setattr("audiobookdl.utils.http.importlib.import_module", missing_import)
    opts = build_default_options(impersonate="chrome", database_directory=str(tmp_path))
    with pytest.raises(GenericAudiobookDLException, match=r"audiobook-dl\[impersonate\]"):
        _DummySource(opts)

    # 2. Functional stub simulating curl_cffi.requests.Session and Cookies(jar=CookieJar)
    class _StubCurlCookies:
        def __init__(self) -> None:
            self.jar = CookieJar()

        def __iter__(self):
            return iter(["token"])  # Deliberately yields names, not Cookie objects

    class _StubTimeoutError(Exception):
        __module__ = "curl_cffi.requests.exceptions"

    class _StubCurlSession:
        def __init__(self, *, impersonate: str) -> None:
            self.impersonate = impersonate
            self.proxies: dict[str, str] = {}
            self.cookies = _StubCurlCookies()
            self.closed = False
            self.calls = 0

        def get(self, url, timeout=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise _StubTimeoutError("timed out")
            return SimpleNamespace(status_code=200, content=b'{"ok": true}', headers={})

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "audiobookdl.utils.http.importlib.import_module",
        lambda name: SimpleNamespace(Session=_StubCurlSession) if name == "curl_cffi.requests" else __import__(name),
    )

    opts_imp = build_default_options(
        impersonate="chrome124",
        proxy="http://127.0.0.1:8888",
        database_directory=str(tmp_path),
    )
    src = _DummySource(opts_imp)
    try:
        assert isinstance(src._session, _StubCurlSession)
        assert src._session.impersonate == "chrome124"
        assert src._session.proxies == {"http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888"}

        # Retry handles curl_cffi timeout cleanly and returns JSON
        payload = src.get_json("https://example.com/api")
        assert payload == {"ok": True}
        assert src._session.calls == 2

        # Cookie bridge supports load_cookie_file, _get_all_cookies, and save_cookie_file
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tTRUE\t1999999999\tsession_id\tabc123\n",
            encoding="utf-8",
        )
        src.load_cookie_file(str(cookie_file))
        from audiobookdl.sources.source.networking import _get_all_cookies

        assert _get_all_cookies(src._session) == {"session_id": "abc123"}
        saved_path = src.save_cookie_file(str(tmp_path / "saved.txt"))
        assert "session_id\tabc123" in (tmp_path / "saved.txt").read_text(encoding="utf-8")
        assert saved_path == str(tmp_path / "saved.txt")

        child = src.create_unauthenticated_session()
        assert isinstance(child, _StubCurlSession)
    finally:
        src.close()
    assert src._session.closed is True
    assert child.closed is True


def test_process_audiobook_uses_custom_metadata_provider_from_config(tmp_path):
    from unittest.mock import MagicMock, patch

    from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata
    from audiobookdl.config import CustomMetadataProviderConfig, structure_config
    from audiobookdl.pipeline import process_audiobook
    from audiobookdl.utils.enrichment import EnrichmentResult

    captured: dict[str, Any] = {}

    def fake_enrich(audiobook, **kwargs):
        captured["providers"] = kwargs.get("providers")
        captured["custom_providers"] = kwargs.get("custom_providers")
        audiobook.metadata.narrators = ["Custom Narrator"]
        return EnrichmentResult(
            updated=True,
            providers_used=["custom:hardcover"],
            fields_updated=["narrators"],
        )

    cfg = structure_config(
        str(tmp_path / "audiobook-dl.toml"),
        {
            "enrich_metadata": True,
            "enrich_providers": "hardcover",
            "enrichment": {
                "custom": {
                    "hardcover": {
                        "url": "https://hc.example.com",
                        "api_key": "tok",
                        "enabled": True,
                    }
                }
            },
        },
        credential_store=None,
    )
    src = MagicMock()
    src.name = "librivox"
    src._session = MagicMock()
    book = Audiobook(
        session=src._session,
        metadata=AudiobookMetadata(title="Custom Enrich Book"),
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
    )
    opts = build_default_options(
        output_template=str(tmp_path / "{title}"),
        enrich_metadata=True,
        enrich_providers="hardcover",
    )
    with (
        patch("audiobookdl.pipeline.enrich_audiobook_metadata", side_effect=fake_enrich),
        patch("audiobookdl.pipeline.download"),
    ):
        process_audiobook(src, book, opts, config=cfg)

    assert captured["providers"] == ("custom:hardcover",)
    assert isinstance(captured["custom_providers"], dict)
    assert isinstance(captured["custom_providers"]["hardcover"], CustomMetadataProviderConfig)
    assert book.metadata.narrators == ["Custom Narrator"]


def test_downloader_session_pool_isolates_profiles_and_closes(tmp_path):
    from audiobookdl.config import structure_config
    from audiobookdl.pipeline import Downloader, _resolve_pool_key
    from audiobookdl.sources.librivox import LibrivoxSource

    opts1 = build_default_options(output_template=str(tmp_path / "{title}"), profile="work")
    opts2 = build_default_options(output_template=str(tmp_path / "{title}"), profile="personal")
    cfg = structure_config(str(tmp_path / "audiobook-dl.toml"), {}, credential_store=None)

    k1 = _resolve_pool_key(LibrivoxSource, opts1, cfg)
    k2 = _resolve_pool_key(LibrivoxSource, opts2, cfg)
    assert k1 != k2

    closed_flags: list[bool] = []
    with Downloader(options=opts1, config=cfg) as dl:
        pool = dl._effective_session_pool()
        sess1 = requests.Session()
        orig_close = sess1.close
        sess1.close = lambda: (closed_flags.append(True), orig_close())[1]  # type: ignore[method-assign]
        pool[k1] = sess1
        pool[k2] = requests.Session()
        assert pool[k1] is not pool[k2]

    assert closed_flags == [True]
    assert dl._owned_session_pool == {}


def test_post_exec_hook_supports_argv_list_without_shell_and_blocks_disallowed_shell(tmp_path):
    from unittest.mock import patch
    from audiobookdl.pipeline import run_post_exec_hook

    seen: list[tuple[object, bool]] = []

    def fake_run(cmd, shell=False, **_kw):
        seen.append((cmd, shell))
        return type("_R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("audiobookdl.pipeline.subprocess.run", side_effect=fake_run):
        rc = run_post_exec_hook(
            ["notify-send", "Done"],
            output_path=str(tmp_path / "Book.m4b"),
            metadata=AudiobookMetadata(title="Hook Book"),
            audio_format="m4b",
            allow_shell=False,
        )
    assert rc == 0
    assert seen == [(["notify-send", "Done"], False)]

    seen.clear()
    warnings: list[str] = []
    with (
        patch("audiobookdl.pipeline.subprocess.run", side_effect=fake_run),
        patch("audiobookdl.pipeline.logging.warning", side_effect=warnings.append),
    ):
        rc_blocked = run_post_exec_hook(
            "echo unsafe",
            output_path=str(tmp_path / "Book.m4b"),
            metadata=AudiobookMetadata(title="Hook Book"),
            audio_format="m4b",
            allow_shell=False,
        )
    assert rc_blocked is None
    assert seen == []
    assert any("Skipping shell post_exec" in msg for msg in warnings)


def test_prepare_source_result_reauthenticates_once_on_expired_cached_cookies(tmp_path, monkeypatch):
    import os
    from audiobookdl import AudiobookFile
    from audiobookdl.config import SourceConfig
    from audiobookdl.exceptions import UserNotAuthorized
    from audiobookdl.pipeline import prepare_source_result
    from audiobookdl.sources.source import Source
    from audiobookdl.utils.http import extract_session_cookies

    class _RetryAuthSource(Source):
        names = ["retrytest"]
        match = [r"https?://retry\.example\.com/book/.+"]
        _authentication_methods = ["cookies", "login"]
        login_data = ["username", "password"]
        calls: list[str] = []

        def _login(self, url: str, username: str, password: str) -> None:
            self.calls.append(f"login:{username}:{password}")
            self._session.cookies.set("session", "fresh-token", domain="retry.example.com")

        def download(self, url: str) -> Audiobook:
            cookie_val = self._session.cookies.get("session")
            self.calls.append(f"download:{cookie_val}")
            if cookie_val != "fresh-token":
                raise UserNotAuthorized(detail="stale cookie")
            return Audiobook(
                session=self._session,
                metadata=AudiobookMetadata(title="Refreshed Book", scrape_url=url),
                files=[AudiobookFile(url="https://retry.example.com/a.mp3", ext="mp3")],
            )

    _RetryAuthSource.calls = []
    opts = build_default_options(database_directory=str(tmp_path), skip_downloaded=False)
    seed_source = _RetryAuthSource(opts)
    seed_source._session.cookies.set("session", "expired-token", domain="retry.example.com")
    cookie_path = seed_source.save_cookie_file()
    seed_source.close()
    assert os.path.isfile(cookie_path)

    cfg = Config(
        sources={
            "retrytest": SourceConfig(
                username="alice",
                password="secret",
                cookie_file=None,
                library=None,
                locale=None,
            )
        },
        output_template=None,
        database_directory=str(tmp_path),
        skip_downloaded=False,
    )
    monkeypatch.setattr(
        "audiobookdl.pipeline.find_compatible_source",
        lambda _url: _RetryAuthSource,
    )

    source, result = prepare_source_result(
        "https://retry.example.com/book/1",
        opts,
        cfg,
    )
    try:
        assert isinstance(result, Audiobook)
        assert result.metadata.title == "Refreshed Book"
        assert _RetryAuthSource.calls == [
            "download:expired-token",
            "login:alice:secret",
            "download:fresh-token",
        ]
        # Refreshed cookies are persisted to the cache jar
        saved = extract_session_cookies(source._session)
        assert any(c.name == "session" and c.value == "fresh-token" for c in saved)
        assert os.path.isfile(cookie_path)
    finally:
        source.close()

    # If re-login download still raises UserNotAuthorized, propagate cleanly after 1 retry
    _RetryAuthSource.calls = []

    def _always_unauthorized(_self, _url: str) -> Audiobook:
        _RetryAuthSource.calls.append("download:fail")
        raise UserNotAuthorized(detail="still invalid")

    monkeypatch.setattr(_RetryAuthSource, "download", _always_unauthorized)
    with pytest.raises(UserNotAuthorized):
        prepare_source_result("https://retry.example.com/book/1", opts, cfg)
    assert _RetryAuthSource.calls == ["download:fail", "login:alice:secret", "download:fail"]


def test_process_audiobook_skips_cross_source_duplicate_when_skip_downloaded(tmp_path, monkeypatch):
    from audiobookdl import AudiobookFile
    from audiobookdl.pipeline import process_audiobook
    from audiobookdl.utils.archive import DownloadArchive, default_archive_path

    db_dir = tmp_path / "db"
    archive_path = default_archive_path(str(db_dir))
    with DownloadArchive(archive_path, persistent=True) as archive:
        archive.record(
            "storytel",
            "https://storytel.example.com/1",
            title="Foundation",
            author="Isaac Asimov",
            isbn="9780553293357",
            output_path=str(tmp_path / "Foundation.m4b"),
            status="downloaded",
        )

    download_called = []
    monkeypatch.setattr("audiobookdl.pipeline.download", lambda *a, **kw: download_called.append(True))

    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(
            title="Foundation",
            authors=["Isaac Asimov"],
            isbn="978-0-553-29335-7",
            scrape_url="https://bookbeat.example.com/book/42",
        ),
        files=[AudiobookFile(url="https://bookbeat.example.com/track.mp3", ext="mp3")],
    )
    opts = build_default_options(
        database_directory=str(db_dir),
        output_template=str(tmp_path / "{title}"),
        skip_downloaded=True,
    )
    processed = process_audiobook(
        type("_Src", (), {"name": "bookbeat", "on_download_complete": lambda *_a: None})(),
        book,
        opts,
    )
    assert processed is False
    assert download_called == []


def test_process_audiobook_override_chapters_passes_flags_to_enrichment_and_download(tmp_path):
    from unittest.mock import MagicMock, patch
    from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter
    from audiobookdl.pipeline import process_audiobook
    from audiobookdl.utils.enrichment import EnrichmentResult

    captured: dict[str, Any] = {}

    def fake_enrich(audiobook, **kwargs):
        captured["override_chapters"] = kwargs.get("override_chapters")
        audiobook.chapters = [Chapter(start=0, title="Audible Ch 1")]
        return EnrichmentResult(
            updated=True,
            providers_used=["audnexus"],
            chapters_updated=True,
        )

    def fake_download(audiobook, opts, **kwargs):
        captured["download_chapters_already_overridden"] = kwargs.get("chapters_already_overridden")

    src = MagicMock()
    src.name = "storytel"
    src._session = MagicMock()
    book = Audiobook(
        session=src._session,
        metadata=AudiobookMetadata(title="Storytel Book"),
        files=[AudiobookFile(url="https://example.com/a.mp3", ext="mp3")],
        chapters=[Chapter(start=0, title="Bad 1"), Chapter(start=5000, title="Bad 2")],
    )
    opts = build_default_options(
        output_template=str(tmp_path / "{title}"),
        enrich_metadata=True,
        override_chapters=True,
    )
    with (
        patch("audiobookdl.pipeline.enrich_audiobook_metadata", side_effect=fake_enrich),
        patch("audiobookdl.pipeline.download", side_effect=fake_download),
    ):
        process_audiobook(src, book, opts)

    assert captured["override_chapters"] is True
    assert captured["download_chapters_already_overridden"] is True
    assert [(c.start, c.title) for c in book.chapters] == [(0, "Audible Ch 1")]


def test_bulk_downloads_skip_unavailable_books_and_fail_on_auth_errors(monkeypatch):
    from audiobookdl.exceptions import (
        BookNotFound,
        MissingBookAccess,
        NoFilesFound,
        UserNotAuthorized,
    )

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    processed: list[str] = []

    def fake_process_url(url, _opts, _cfg, **kwargs):
        if url.endswith("/locked"):
            raise MissingBookAccess
        if url.endswith("/missing"):
            raise BookNotFound
        if url.endswith("/empty"):
            raise NoFilesFound
        if url.endswith("/auth"):
            raise UserNotAuthorized
        processed.append(url)
        kwargs["summary"].books_completed += 1

    monkeypatch.setattr("audiobookdl.pipeline.process_url", fake_process_url)

    # Multi-URL bulk run skips unavailable books and continues to later valid URLs
    summary = Downloader(cfg, build_default_options()).download(
        [
            "https://example.com/locked",
            "https://example.com/ok1",
            "https://example.com/missing",
            "https://example.com/empty",
            "https://example.com/ok2",
        ]
    )
    assert processed == ["https://example.com/ok1", "https://example.com/ok2"]
    assert summary.books_completed == 2
    assert summary.books_skipped == 3
    assert summary.failed == 0
    assert summary.books_failed == 0

    # Account auth error still fails rather than being silently skipped
    processed.clear()
    auth_summary = Downloader(cfg, build_default_options(skip_unavailable=True)).download(
        ["https://example.com/auth", "https://example.com/ok3"]
    )
    assert processed == ["https://example.com/ok3"]
    assert auth_summary.books_completed == 1
    assert auth_summary.failed == 1
    assert auth_summary.books_failed == 1

    # Single URL with skip_unavailable=True skips cleanly
    single_skip = Downloader(cfg, build_default_options(skip_unavailable=True)).download(
        ["https://example.com/locked"]
    )
    assert single_skip.books_skipped == 1
    assert single_skip.failed == 0


def test_series_skips_inaccessible_and_missing_books(monkeypatch):
    from audiobookdl.exceptions import BookNotFound, MissingBookAccess, NoFilesFound

    b1 = Audiobook(session=requests.Session(), metadata=AudiobookMetadata(title="Available Book"), files=[])
    series = Series(
        title="Series",
        books=[BookId("locked"), BookId("missing"), BookId("empty"), BookId("ok")],
    )

    class _FakeSeriesSource:
        name = "fakesource"
        names = ["FakeSource"]
        requires_authentication = False
        authenticated = True

        def __init__(self, _options) -> None:
            pass

        def download(self, _url: str) -> Series:
            return series

        def download_from_id(self, book_id: str) -> Audiobook:
            if book_id == "locked":
                raise MissingBookAccess
            if book_id == "missing":
                raise BookNotFound
            if book_id == "empty":
                raise NoFilesFound
            return b1

        def on_download_complete(self, _ab: Audiobook) -> None:
            pass

    monkeypatch.setattr("audiobookdl.pipeline.find_compatible_source", lambda _url: _FakeSeriesSource)
    downloaded: list[str] = []
    monkeypatch.setattr(
        "audiobookdl.pipeline.process_audiobook",
        lambda _s, ab, _o, **_kw: downloaded.append(ab.title) or True,
    )

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    summary = Downloader(cfg, build_default_options()).download(["https://example.com/series"])
    assert downloaded == ["Available Book"]
    assert summary.books_completed == 1
    assert summary.books_skipped == 3
    assert summary.books_failed == 0
    assert summary.failed == 0


def test_get_cookie_path_ignores_cwd_cookies_txt(tmp_path, monkeypatch):
    from audiobookdl.config import SourceConfig
    from audiobookdl.pipeline import get_cookie_path

    cwd_cookie = tmp_path / "cookies.txt"
    cwd_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    opts = build_default_options()
    assert get_cookie_path(opts, None) is None

    cfg_cookie = tmp_path / "explicit.txt"
    cfg_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    src_cfg = SourceConfig(
        username=None,
        password=None,
        cookie_file=str(cfg_cookie),
        library=None,
        locale=None,
    )
    assert get_cookie_path(opts, src_cfg) == str(cfg_cookie)

    opt_cookie = tmp_path / "opt.txt"
    opt_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    opts_with_file = build_default_options(cookie_file=str(opt_cookie))
    assert get_cookie_path(opts_with_file, src_cfg) == str(opt_cookie)


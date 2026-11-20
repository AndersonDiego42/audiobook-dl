import json
import tkinter as tk
from datetime import date
from typing import Any, cast
from unittest.mock import patch

import pytest
import requests

from audiobookdl.options_schema import build_default_options
from audiobookdl.config import (
    AudiobookshelfConfig,
    Config,
    load_config,
    save_audiobookshelf_config,
)
from audiobookdl.credentials import PlaintextFallbackCredentialStore
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.integrations.audiobookshelf import (
    AudiobookshelfClient,
    AudiobookshelfError,
    sync_book_to_audiobookshelf,
)
from audiobookdl.output.download import add_metadata_to_file
from audiobookdl.output.metadata.reader import read_sidecar_metadata
from audiobookdl.output.metadata.sidecars import build_abs_metadata_dict
from audiobookdl.pipeline import process_audiobook
from audiobookdl.sources.source import Source
from audiobookdl.utils.audiobook import Audiobook, AudiobookFile, AudiobookMetadata, Chapter


class _MockResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON")
        return self._json_data


class _FakeAbsSession:
    def __init__(self):
        self.calls = []
        self.streamed_bodies = []

    def request(self, method, url, headers=None, json=None, data=None, files=None, timeout=None):
        import re as _re

        uploaded_names = [pair[1][0] for pair in (files or [])]
        body_bytes = b""
        if data is not None and hasattr(data, "read"):
            chunks = []
            while True:
                chunk = data.read(16384)
                if not chunk:
                    break
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
            self.streamed_bodies.append((headers or {}, len(data), body_bytes))
            uploaded_names.extend(
                m.decode("utf-8")
                for m in _re.findall(rb'filename="([^"]+)"', body_bytes)
            )
        self.calls.append((method, url, headers, data, uploaded_names))
        if url.endswith("/api/authorize"):
            return _MockResponse(200, {"user": {"username": "absadmin"}})
        if url.endswith("/api/libraries"):
            return _MockResponse(200, {"libraries": [{"id": "lib-1", "name": "Audiobooks"}]})
        if url.endswith("/api/libraries/lib-1"):
            return _MockResponse(200, {"library": {"id": "lib-1", "folders": [{"id": "fol-1"}]}})
        if url.endswith("/api/upload"):
            return _MockResponse(200, {"ok": True})
        if "/api/libraries/lib-1/scan" in url:
            return _MockResponse(200, {"scanning": True})
        return _MockResponse(404, text="not found")

    def close(self):
        pass


def test_abs_metadata_sidecar_roundtrips_with_reader_and_write_option(tmp_path):
    meta = AudiobookMetadata(
        title="Children of Time",
        authors=["Adrian Tchaikovsky"],
        narrators=["Mel Hudson"],
        series="Children of Time",
        series_order=1,
        genres=["Sci-Fi"],
        publisher="Tor",
        description="Space epic",
        isbn="9781447273301",
        asin="B071Y9TBLK",
        release_date=date(2015, 6, 4),
    )
    chapters = [
        Chapter(start=0, title="Prologue"),
        Chapter(start=12500, title="Chapter 1"),
    ]
    payload = build_abs_metadata_dict(meta, chapters)
    assert payload["publishedYear"] == "2015"
    assert payload["series"] == [{"name": "Children of Time", "sequence": "1"}]
    assert payload["chapters"] == [
        {"id": 0, "start": 0.0, "end": 12.5, "title": "Prologue"},
        {"id": 1, "start": 12.5, "end": 12.5, "title": "Chapter 1"},
    ]

    out_dir = tmp_path / "Children of Time"
    out_dir.mkdir()
    audio_path = out_dir / "Children of Time.m4b"
    audio_path.write_bytes(b"fake-audio")
    opts = build_default_options(write_abs_metadata=True)
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        add_metadata_to_file(
            Audiobook(session=requests.Session(), metadata=meta, files=[], chapters=chapters),
            str(audio_path),
            opts,
            chapters_embedded=True,
            cover_embedded=True,
        )

    json_file = out_dir / "metadata.json"
    assert json_file.is_file()
    parsed_meta, _cover, parsed_chapters = read_sidecar_metadata(str(out_dir))
    assert parsed_meta is not None
    assert parsed_meta.title == "Children of Time"
    assert parsed_meta.series == "Children of Time"
    assert parsed_meta.series_order == 1
    assert len(parsed_chapters) == 2


def test_abs_config_persistence_and_client_upload_scan(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    store = PlaintextFallbackCredentialStore(allow_plaintext=True)
    save_audiobookshelf_config(
        url="https://abs.local/",
        library_id="lib-1",
        api_key="secret-token",
        auto_upload=True,
        auto_scan=True,
        overwrite=str(cfg_path),
        credential_store=store,
    )
    loaded = load_config(str(cfg_path), credential_store=store)
    assert loaded.audiobookshelf.url == "https://abs.local"
    assert loaded.audiobookshelf.library_id == "lib-1"
    assert loaded.audiobookshelf.api_key == "secret-token"
    assert loaded.audiobookshelf.auto_upload is True
    assert loaded.audiobookshelf.auto_scan is True

    book_dir = tmp_path / "Book"
    book_dir.mkdir()
    m4b = book_dir / "Book.m4b"
    m4b.write_bytes(b"m4b")
    (book_dir / "metadata.json").write_text("{}", encoding="utf-8")

    fake_session = _FakeAbsSession()
    typed_session = cast(requests.Session, fake_session)
    client = AudiobookshelfClient("https://abs.local/", "secret-token", session=typed_session)
    assert client.ping() == "absadmin"
    assert client.list_libraries() == [("lib-1", "Audiobooks")]

    meta = AudiobookMetadata(title="Dune", authors=["Frank Herbert"], series="Dune")
    did_sync = sync_book_to_audiobookshelf(
        str(m4b),
        meta,
        loaded.audiobookshelf,
        session=typed_session,
    )
    assert did_sync is True
    upload_calls = [c for c in fake_session.calls if c[1].endswith("/api/upload")]
    scan_calls = [c for c in fake_session.calls if "/scan" in c[1]]
    assert len(upload_calls) == 1
    assert "Book.m4b" in upload_calls[0][4]
    assert "metadata.json" in upload_calls[0][4]
    assert len(scan_calls) == 1


def test_pipeline_process_audiobook_triggers_abs_sync(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.pipeline.jobs.download", lambda *a, **kw: None)
    fake_abs_session = _FakeAbsSession()

    class _Src:
        name = "librivox"
        skip_downloaded = False

        def on_download_complete(self, _b):
            pass

    source_session = requests.Session()
    source_session.headers.update(
        {
            "Authorization": "Bearer source-secret",
            "X-Login-Token": "src-login",
            "X-Profile-Token": "src-profile",
            "Session-Key": "src-session-key",
        }
    )
    source_session.cookies.set("sid", "source-cookie", domain="storytel.com")

    target_m4b = tmp_path / "MyBook.m4b"
    target_m4b.write_bytes(b"audio")
    book = Audiobook(
        session=source_session,
        metadata=AudiobookMetadata(title="MyBook", authors=["Alice"], scrape_url="https://librivox.org/mybook"),
        files=[AudiobookFile(url="https://example.com/1.mp3", ext="mp3")],
    )
    cfg = Config(
        sources={},
        output_template=str(tmp_path / "{title}"),
        database_directory=str(tmp_path),
        skip_downloaded=False,
        audiobookshelf=AudiobookshelfConfig(
            url="https://abs.local",
            api_key="tok",
            library_id="lib-1",
            auto_upload=True,
            auto_scan=True,
        ),
    )
    opts = build_default_options(
        output_template=str(tmp_path / "{title}"),
        output_format="m4b",
        database_directory=str(tmp_path),
    )
    with patch(
        "audiobookdl.integrations.audiobookshelf.create_configured_http_session",
        return_value=fake_abs_session,
    ):
        assert process_audiobook(cast(Source[Any], _Src()), book, opts, config=cfg) is True

    assert any(c[1].endswith("/api/upload") for c in fake_abs_session.calls)
    assert any("/scan" in c[1] for c in fake_abs_session.calls)
    for _method, _url, hdrs, _data, _names in fake_abs_session.calls:
        lowered = {str(k).lower(): v for k, v in (hdrs or {}).items()}
        assert lowered.get("authorization") == "Bearer tok"
        assert "x-login-token" not in lowered
        assert "x-profile-token" not in lowered
        assert "session-key" not in lowered
        assert "cookie" not in lowered

    # Missing config + force upload raises explicit error inside sync helper
    with pytest.raises(AudiobookshelfError):
        sync_book_to_audiobookshelf(str(target_m4b), book.metadata, AudiobookshelfConfig(), force_upload=True)


def test_gui_abs_dialog_saves_settings(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        cfg_path = tmp_path / "audiobook-dl.toml"
        cfg_path.write_text("", encoding="utf-8")
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        with patch("audiobookdl.gui.config_store.save_audiobookshelf_config") as mock_save:
            page = app.navigate("settings")
            page._url_var.set("https://abs.myhost.tld/")
            page._token_var.set("tok-123")
            page._library_var.set("lib-99")
            page._auto_upload_var.set(True)
            page._save()
            mock_save.assert_called_once()
        app.controller.shutdown(is_running=False, is_previewing=False)
    finally:
        root.destroy()


def test_abs_sidecar_sets_final_chapter_duration_and_avoids_clobbering(tmp_path):
    import json

    meta = AudiobookMetadata(title="Dune", authors=["Frank Herbert"])
    chapters = [
        Chapter(start=0, title="Part 1"),
        Chapter(start=10000, title="Part 2"),
    ]
    payload = build_abs_metadata_dict(meta, chapters, duration_seconds=42.5)
    assert payload["chapters"] == [
        {"id": 0, "start": 0.0, "end": 10.0, "title": "Part 1"},
        {"id": 1, "start": 10.0, "end": 42.5, "title": "Part 2"},
    ]

    # Co-existing write_json_metadata and write_abs_metadata in dir output should not clobber
    book_dir = tmp_path / "Dune"
    book_dir.mkdir()
    part1 = book_dir / "01.mp3"
    part1.write_bytes(b"mp3")
    from audiobookdl.output.download import add_metadata_to_dir
    from audiobookdl.utils import AudioProbeInfo

    opts_both = build_default_options(write_json_metadata=True, write_abs_metadata=True)
    probe_cache = {str(part1): AudioProbeInfo(duration=42.5)}
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        add_metadata_to_dir(
            Audiobook(session=requests.Session(), metadata=meta, files=[], chapters=chapters),
            [str(part1)],
            str(book_dir),
            opts_both,
            probe_cache=probe_cache,
        )
    assert (book_dir / "audiobook.json").is_file()
    abs_json = json.loads((book_dir / "metadata.json").read_text(encoding="utf-8"))
    assert abs_json["chapters"][-1]["end"] == 42.5

    # Shared flat directory with another audio stem avoids overwriting existing metadata.json
    flat_dir = tmp_path / "Author"
    flat_dir.mkdir()
    first = flat_dir / "First Book.m4b"
    second = flat_dir / "Second Book.m4b"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    (flat_dir / "metadata.json").write_text('{"title": "First Book"}', encoding="utf-8")

    opts_abs = build_default_options(write_abs_metadata=True)
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        add_metadata_to_file(
            Audiobook(
                session=requests.Session(),
                metadata=AudiobookMetadata(title="Second Book"),
                files=[],
                chapters=chapters,
            ),
            str(second),
            opts_abs,
            chapters_embedded=True,
            cover_embedded=True,
            probe_cache={str(second): AudioProbeInfo(duration=35.0)},
        )
    assert json.loads((flat_dir / "metadata.json").read_text(encoding="utf-8"))["title"] == "First Book"
    sec_abs = json.loads((flat_dir / "Second Book.metadata.json").read_text(encoding="utf-8"))
    assert sec_abs["title"] == "Second Book"
    assert sec_abs["chapters"][-1]["end"] == 35.0


def test_multipart_upload_stream_computes_exact_length_and_streams_chunks(tmp_path):
    from audiobookdl.integrations.audiobookshelf import _MultipartUploadStream

    f1 = tmp_path / "book.m4b"
    f2 = tmp_path / "metadata.json"
    payload1 = b"A" * 200_000
    payload2 = b'{"title": "Streamed"}'
    f1.write_bytes(payload1)
    f2.write_bytes(payload2)

    stream = _MultipartUploadStream(
        {"library": "lib-1", "title": "Streamed"},
        [("0", str(f1)), ("1", str(f2))],
        boundary="test-boundary-123",
    )
    expected_len = len(stream)
    chunks = []
    try:
        while True:
            chunk = stream.read(16_384)
            if not chunk:
                break
            assert len(chunk) <= 16_384
            chunks.append(chunk)
    finally:
        stream.close()

    assembled = b"".join(chunks)
    assert len(assembled) == expected_len
    assert payload1 in assembled
    assert payload2 in assembled
    assert b"--test-boundary-123--\r\n" in assembled


def test_multipart_upload_stream_reset_invokes_on_reset(tmp_path):
    from audiobookdl.integrations.audiobookshelf import _MultipartUploadStream

    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"x" * 1000)
    resets = []
    stream = _MultipartUploadStream(
        {"library": "lib-1"},
        [("0", str(audio))],
        on_reset=lambda: resets.append(1),
    )
    try:
        stream.read(64)
        stream.reset()
        assert resets == [1]
    finally:
        stream.close()


def test_upload_book_resets_progress_counters_on_retry(tmp_path):
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"audio-bytes" * 40000)  # ~440 KB
    sent_values: list[int] = []
    attempts = {"count": 0}

    class FlakyAbsSession(_FakeAbsSession):
        def request(self, method, url, headers=None, json=None, data=None, files=None, timeout=None):
            if url.endswith("/api/upload"):
                attempts["count"] += 1
                if attempts["count"] == 1 and hasattr(data, "read"):
                    data.read(100_000)  # partial send before the failure
                    return _MockResponse(503, text="busy")
            return super().request(
                method, url, headers=headers, json=json, data=data, files=files, timeout=timeout
            )

    session = FlakyAbsSession()
    client = AudiobookshelfClient("https://abs.example.com", "tok", session=session)
    meta = AudiobookMetadata(title="Retry Book")
    ok = client.upload_book(
        str(audio),
        library_id="lib-1",
        metadata=meta,
        on_progress=lambda sent, total: sent_values.append(sent),
    )
    assert ok is True
    assert attempts["count"] == 2
    # The retried attempt restarts `sent` near zero, so progress values decrease.
    assert any(cur < prev for prev, cur in zip(sent_values, sent_values[1:], strict=False))


def test_collect_upload_files_shared_directory_uses_stem_sidecars_only(tmp_path):
    client = AudiobookshelfClient("https://abs.example.com", "tok")

    shared = tmp_path / "shared_dir"
    shared.mkdir()
    book_a = shared / "Book A.m4b"
    book_a_meta = shared / "Book A.metadata.json"
    book_a_webp = shared / "Book A.webp"
    book_b = shared / "Book B.m4b"
    foreign_meta = shared / "metadata.json"
    foreign_cover = shared / "cover.jpg"
    for p in (book_a, book_a_meta, book_a_webp, book_b, foreign_meta, foreign_cover):
        p.write_bytes(b"data")

    collected = client._collect_upload_files(str(book_a))
    collected_names = {p.split("\\")[-1].split("/")[-1] for p in collected}
    assert collected_names == {"Book A.m4b", "Book A.metadata.json", "Book A.webp"}

    isolated = tmp_path / "single_book_dir"
    isolated.mkdir()
    solo = isolated / "Solo Book.m4b"
    solo_meta = isolated / "metadata.json"
    solo_cover = isolated / "cover.jpg"
    for p in (solo, solo_meta, solo_cover):
        p.write_bytes(b"data")

    solo_collected = client._collect_upload_files(str(solo))
    solo_names = {p.split("\\")[-1].split("/")[-1] for p in solo_collected}
    assert solo_names == {"Solo Book.m4b", "metadata.json", "cover.jpg"}


def test_audiobookshelf_warns_only_on_non_loopback_http(monkeypatch):
    from audiobookdl import logging
    from audiobookdl.integrations.audiobookshelf import AudiobookshelfClient

    logged: list[str] = []
    monkeypatch.setattr(logging, "warning", logged.append)

    AudiobookshelfClient("http://localhost:13378", "tok")
    AudiobookshelfClient("http://127.0.0.1:13378", "tok")
    AudiobookshelfClient("https://abs.example.com", "tok")
    assert logged == []

    AudiobookshelfClient("http://abs.lan.example:13378", "tok")
    assert len(logged) == 1
    assert "unencrypted http://" in logged[0]


def test_abs_upload_retries_transient_503_tracks_progress_skips_duplicate_and_webp_opf(
    tmp_path, monkeypatch
):
    from audiobookdl.integrations import audiobookshelf as abs_mod
    from audiobookdl.output.metadata.sidecars import build_opf_xml

    monkeypatch.setattr(abs_mod, "sleep_or_cancel", lambda _d, _e=None: None)
    book_file = tmp_path / "RetryBook.m4b"
    book_file.write_bytes(b"M4B-DATA-" * 100)

    class _FlakySession:
        def __init__(self) -> None:
            self.upload_attempts = 0
            self.search_called = False

        def request(self, method, url, headers=None, json=None, data=None, files=None, timeout=None):
            if url.endswith("/api/libraries/lib-1"):
                return _MockResponse(200, {"library": {"id": "lib-1", "folders": [{"id": "fol-1"}]}})
            if "/search?" in url:
                self.search_called = True
                return _MockResponse(
                    200,
                    {"book": [{"libraryItem": {"media": {"metadata": {"asin": "B0DUP999"}}}}]},
                )
            if url.endswith("/api/upload"):
                body = data.read() if data is not None else b""
                assert len(body) > len(b"M4B-DATA-") * 90
                self.upload_attempts += 1
                if self.upload_attempts == 1:
                    return _MockResponse(503, text="busy")
                return _MockResponse(200, {"ok": True})
            return _MockResponse(404, text="missing")

    sess = _FlakySession()
    client = AudiobookshelfClient("https://abs.example.com", "tok", session=cast(requests.Session, sess))
    progress_events: list[tuple[int, int]] = []
    meta = AudiobookMetadata(title="Retry Book", authors=["Author"], asin="B0NEW123")
    ok = client.upload_book(
        str(book_file),
        library_id="lib-1",
        metadata=meta,
        on_progress=lambda sent, total: progress_events.append((sent, total)),
    )
    assert ok is True
    assert sess.upload_attempts == 2
    assert progress_events[-1][0] == progress_events[-1][1] > 0

    # Duplicate check skips POST /api/upload when ASIN already exists in library
    dup_meta = AudiobookMetadata(title="Existing", asin="B0DUP999")
    assert client.upload_book(str(book_file), library_id="lib-1", metadata=dup_meta, skip_if_exists=True) is False
    assert sess.search_called is True
    assert sess.upload_attempts == 2

    opf = build_opf_xml(meta, "cover.webp")
    assert 'media-type="image/webp"' in opf


def test_audiobookshelf_client_replaces_curl_cffi_session():
    class _FakeCurlSession:
        __module__ = "curl_cffi.requests.session"

        def __init__(self) -> None:
            self.proxies = {"https": "http://127.0.0.1:8888", "http": "http://127.0.0.1:8888"}
            self.headers = {"X-Login-Token": "leaked"}

    fake_curl = _FakeCurlSession()
    client = AudiobookshelfClient(
        "https://abs.example.com",
        "abs-tok",
        session=cast(requests.Session, fake_curl),
    )
    try:
        assert isinstance(client.session, requests.Session)
        assert client.session is not fake_curl
        assert client._owns_session is True
        assert client.session.proxies.get("https") == "http://127.0.0.1:8888"
        assert "X-Login-Token" not in client.session.headers
    finally:
        client.close()



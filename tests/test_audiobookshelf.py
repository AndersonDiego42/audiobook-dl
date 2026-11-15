import json
import tkinter as tk
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from audiobookdl import args
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
from audiobookdl.output.metadata.sidecars import build_abs_metadata_dict, write_sidecar_abs_metadata
from audiobookdl.pipeline import process_audiobook
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

    def request(self, method, url, headers=None, json=None, data=None, files=None, timeout=None):
        uploaded_names = [pair[1][0] for pair in (files or [])]
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
    opts = args.build_default_options(write_abs_metadata=True)
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
    client = AudiobookshelfClient("https://abs.local/", "secret-token", session=fake_session)
    assert client.ping() == "absadmin"
    assert client.list_libraries() == [("lib-1", "Audiobooks")]

    meta = AudiobookMetadata(title="Dune", authors=["Frank Herbert"], series="Dune")
    did_sync = sync_book_to_audiobookshelf(
        str(m4b),
        meta,
        loaded.audiobookshelf,
        session=fake_session,
    )
    assert did_sync is True
    upload_calls = [c for c in fake_session.calls if c[1].endswith("/api/upload")]
    scan_calls = [c for c in fake_session.calls if "/scan" in c[1]]
    assert len(upload_calls) == 1
    assert "Book.m4b" in upload_calls[0][4]
    assert "metadata.json" in upload_calls[0][4]
    assert len(scan_calls) == 1


def test_pipeline_process_audiobook_triggers_abs_sync(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.pipeline.download", lambda *a, **kw: None)
    fake_session = _FakeAbsSession()

    class _Src:
        name = "librivox"
        skip_downloaded = False

        def on_download_complete(self, _b):
            pass

    target_m4b = tmp_path / "MyBook.m4b"
    target_m4b.write_bytes(b"audio")
    book = Audiobook(
        session=fake_session,
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
    opts = args.build_default_options(
        output_template=str(tmp_path / "{title}"),
        output_format="m4b",
        database_directory=str(tmp_path),
    )
    assert process_audiobook(_Src(), book, opts, config=cfg) is True
    assert any(c[1].endswith("/api/upload") for c in fake_session.calls)
    assert any("/scan" in c[1] for c in fake_session.calls)

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
        with patch("audiobookdl.gui.abs_dialog.save_audiobookshelf_config") as mock_save:
            dialog = app._show_audiobookshelf_dialog()
            try:
                dialog._url_var.set("https://abs.myhost.tld/")
                dialog._token_var.set("tok-123")
                dialog._library_var.set("lib-99")
                dialog._auto_upload_var.set(True)
                dialog._save()
                mock_save.assert_called_once()
            finally:
                if dialog.winfo_exists():
                    dialog.destroy()
    finally:
        root.destroy()

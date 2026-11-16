import json
import tkinter as tk
from unittest.mock import patch

import pytest

from audiobookdl.__main__ import main as cli_main
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.output.metadata.reader import TrackTagInfo
from audiobookdl.utils.archive import CatalogBook, DownloadArchive
from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.library_scanner import scan_local_library


def _make_tag(title: str, *, album: str = "", author: str = "", asin: str = "") -> TrackTagInfo:
    meta = AudiobookMetadata(title=title)
    if author:
        meta.add_author(author)
    if asin:
        meta.asin = asin
    return TrackTagInfo(track_number=1, disc_number=1, title=title, album=album, metadata=meta)


def test_scan_local_library_matches_pending_registers_new_and_checks_missing(tmp_path):
    lib_root = tmp_path / "Library"
    pending_dir = lib_root / "Frank Herbert" / "Dune"
    pending_dir.mkdir(parents=True)
    track1 = pending_dir / "01.mp3"
    track2 = pending_dir / "02.mp3"
    track1.write_bytes(b"mp3-1")
    track2.write_bytes(b"mp3-2")
    (pending_dir / "metadata.json").write_text(
        json.dumps(
            {
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "narrators": ["Scott Brick"],
                "series": [{"name": "Dune", "sequence": "1"}],
                "asin": "B002V1OF70",
            }
        ),
        encoding="utf-8",
    )

    standalone_dir = lib_root / "Standalone"
    standalone_dir.mkdir(parents=True)
    book_a = standalone_dir / "BookA.m4b"
    book_b = standalone_dir / "BookB.m4b"
    book_a.write_bytes(b"m4b-a")
    book_b.write_bytes(b"m4b-b")

    db_path = tmp_path / "downloads.db"
    archive = DownloadArchive(str(db_path))
    archive.upsert_catalog_items(
        "audible",
        [CatalogBook(title="Dune", url="https://example.com/dune", author="Frank Herbert")],
    )
    gone_entry_id = archive.record(
        "librivox",
        "https://librivox.org/gone",
        title="Deleted Book",
        author="Old Author",
        output_path=str(tmp_path / "MissingBook.m4b"),
    )

    def _fake_read_tags(path: str) -> TrackTagInfo:
        name = path.replace("\\", "/")
        if name.endswith("BookA.m4b"):
            return _make_tag("Book A", album="Book A", author="Author A")
        if name.endswith("BookB.m4b"):
            return _make_tag("Book B", album="Book B", author="Author B")
        return TrackTagInfo()

    with patch("audiobookdl.pack.read_track_tags", side_effect=_fake_read_tags), patch(
        "audiobookdl.utils.library_scanner.read_track_tags", side_effect=_fake_read_tags
    ):
        result = scan_local_library([str(lib_root)], archive, verify_missing=True)

    assert result.scanned_books == 3
    assert result.matched == 1
    assert result.registered == 2
    assert result.missing == 1

    entries = archive.list_entries(include_hidden=True)
    by_title = {e.title: e for e in entries}
    assert by_title["Dune"].status == "downloaded"
    assert by_title["Dune"].output_path == str(pending_dir)
    assert by_title["Dune"].asin == "B002V1OF70"
    assert by_title["Book A"].status == "downloaded"
    assert by_title["Book B"].status == "downloaded"
    assert archive.get_entry(gone_entry_id).status == "pending"


def test_cli_scan_subcommand_json_and_gui_scan_folder(tmp_path, capsys):
    lib_root = tmp_path / "Audiobooks"
    book_dir = lib_root / "Alice - Odyssey"
    book_dir.mkdir(parents=True)
    audio_file = book_dir / "Odyssey.mp3"
    audio_file.write_bytes(b"mp3")

    db_path = tmp_path / "downloads.db"
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text("", encoding="utf-8")

    with patch("audiobookdl.pack.read_track_tags", return_value=TrackTagInfo()):
        with pytest.raises(SystemExit) as exc:
            cli_main(
                [
                    "scan",
                    "--config",
                    str(cfg_path),
                    "--download-archive",
                    str(db_path),
                    "--json",
                    str(lib_root),
                ]
            )
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned_books"] == 1
    assert payload["registered"] == 1
    assert payload["items"][0]["title"] == "Odyssey"

    # GUI ArchiveLedgerDialog scan_folder integration
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.controller.config.download_archive = str(db_path)
        dialog = app._show_archive_ledger_dialog()
        try:
            with patch("audiobookdl.pack.read_track_tags", return_value=TrackTagInfo()):
                res = dialog.scan_folder(str(lib_root), background=False)
            assert res is not None
            assert res.matched == 1
            assert len(dialog._tree.get_children()) == 1
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_scan_local_library_closes_batch_connection_and_dedupes_via_index(tmp_path):
    from audiobookdl.utils.archive import DownloadArchive
    from audiobookdl.utils.library_scanner import scan_local_library

    lib_root = tmp_path / "lib"
    book_dir = lib_root / "Author" / "Book One"
    book_dir.mkdir(parents=True)
    (book_dir / "01.mp3").write_bytes(b"mp3-1")
    (book_dir / "metadata.json").write_text(
        json.dumps({"title": "Book One", "authors": ["Author"]}),
        encoding="utf-8",
    )

    archive = DownloadArchive(str(tmp_path / "downloads.db"))
    assert archive._conn is None
    with patch("audiobookdl.pack.read_track_tags", return_value=TrackTagInfo()):
        res1 = scan_local_library([str(lib_root)], archive)
        assert res1.registered == 1
        assert archive._conn is None

        res2 = scan_local_library([str(lib_root)], archive)
        assert res2.matched == 1
        assert archive._conn is None

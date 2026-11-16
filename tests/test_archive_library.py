import sqlite3
import tkinter as tk

import pytest
import requests

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, args
from audiobookdl.__main__ import main as cli_main
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.pipeline import process_audiobook
from audiobookdl.sources.source import CatalogBook
from audiobookdl.utils.archive import (
    DownloadArchive,
    make_book_key,
    parse_library_query,
    parse_tags_input,
)


def test_archive_migrates_v1_schema_and_preserves_rows(tmp_path):
    db_path = tmp_path / "downloads.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            book_key TEXT NOT NULL,
            url TEXT,
            title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            output_path TEXT,
            format TEXT,
            downloaded_at TEXT NOT NULL,
            UNIQUE(source, book_key)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO downloads (source, book_key, url, title, author, output_path, format, downloaded_at)
        VALUES ('storytel', 'https://storytel.com/1', 'https://storytel.com/1', 'Legacy Book', 'Old Author', '/books/Legacy', 'm4b', '2025-01-01 00:00:00 UTC')
        """
    )
    conn.commit()
    conn.close()

    archive = DownloadArchive(str(db_path))
    entries = archive.list_entries()
    assert len(entries) == 1
    assert entries[0].title == "Legacy Book"
    assert entries[0].status == "downloaded"
    assert entries[0].profile == "default"
    assert entries[0].tags == ()
    assert archive.contains("storytel", "https://storytel.com/1") is True


def test_catalog_upsert_tags_rich_search_and_soft_delete(tmp_path):
    db_path = tmp_path / "downloads.db"
    archive = DownloadArchive(str(db_path))

    inserted, updated = archive.upsert_catalog_items(
        "storytel",
        [
            CatalogBook(
                title="Dune",
                url="https://storytel.com/dune",
                author="Frank Herbert",
                series="Dune Saga",
                series_order=1,
            ),
            CatalogBook(
                title="Foundation",
                url="https://storytel.com/foundation",
                author="Isaac Asimov",
                series="Foundation",
                series_order=1,
            ),
        ],
        profile="family",
    )
    assert (inserted, updated) == (2, 0)

    # Mark Dune as downloaded, then re-scan catalog; status='downloaded' must stay intact
    dune = archive.list_entries(query='title:dune author:"Frank Herbert"')[0]
    assert dune.status == "pending"
    assert dune.profile == "family"
    assert archive.contains("storytel", dune.book_key) is False

    archive.record(
        "storytel",
        dune.book_key,
        title="Dune",
        author="Frank Herbert",
        url="https://storytel.com/dune",
        output_path="/books/Dune.m4b",
        audio_format="m4b",
        profile="family",
        series="Dune Saga",
        series_order=1,
        narrator="Scott Brick",
        asin="B0000001",
    )
    assert archive.contains("storytel", dune.book_key) is True

    inserted2, updated2 = archive.upsert_catalog_items(
        "storytel",
        [CatalogBook(title="Dune", url="https://storytel.com/dune", author="Frank Herbert")],
        profile="family",
    )
    assert (inserted2, updated2) == (0, 1)
    assert archive.get_entry_status("storytel", dune.book_key) == "downloaded"

    # Tags & hidden filter
    assert parse_tags_input("Sci-Fi, fav! hidden") == ["sci-fi", "fav", "hidden"]
    archive.add_tags(dune.id, ["Sci-Fi", "Fav"])
    foundation = archive.list_entries(query="author:asimov")[0]
    archive.add_tags(foundation.id, ["hidden", "scifi"])

    assert len(archive.list_entries(include_hidden=False)) == 1
    assert len(archive.list_entries(query="tag:sci-fi")) == 1
    assert len(archive.list_entries(query="-tag:hidden")) == 1
    assert len(archive.list_entries(query="status:pending")) == 1
    assert len(archive.list_entries(query="narrator:brick asin:B0000001")) == 1

    # Soft delete (trash) hides from default queries & contains(), and can be restored
    archive.set_status(dune.id, "trashed")
    assert archive.contains("storytel", dune.book_key) is False
    assert [e.id for e in archive.list_entries()] == [foundation.id]
    assert [e.id for e in archive.list_entries(query="status:trashed")] == [dune.id]

    archive.set_status(dune.id, "downloaded")
    assert archive.contains("storytel", dune.book_key) is True


def test_parse_library_query_tokens():
    terms = parse_library_query('author:"Frank Herbert" -tag:hidden status:pending dune')
    assert [(t.field, t.value, t.negated) for t in terms] == [
        ("author", "frank herbert", False),
        ("tag", "hidden", True),
        ("status", "pending", False),
        (None, "dune", False),
    ]


def test_skip_downloaded_uses_default_downloads_db_and_records_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.pipeline.download", lambda *a, **kw: None)

    class _Src:
        name = "storytel"
        skip_downloaded = False

        def on_download_complete(self, _book):
            pass

    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(
            title="Hyperion",
            authors=["Dan Simmons"],
            narrators=["Victor Bevine"],
            series="Hyperion Cantos",
            series_order=1,
            isbn="9780553283686",
            asin="B004G60EHS",
            scrape_url="https://storytel.com/hyperion",
        ),
        files=[AudiobookFile(url="https://example.com/h.mp3", ext="mp3")],
    )
    opts = args.build_default_options(
        database_directory=str(tmp_path),
        output_template=str(tmp_path / "{title}"),
        skip_downloaded=True,
        profile="Personal",
    )
    assert process_audiobook(_Src(), book, opts) is True
    # Second run skips via default downloads.db even when output file isn't on disk
    assert process_audiobook(_Src(), book, opts) is False

    archive = DownloadArchive(str(tmp_path / "downloads.db"))
    entry = archive.list_entries()[0]
    assert entry.book_key == make_book_key("storytel", book)
    assert entry.profile == "personal"
    assert entry.series == "Hyperion Cantos"
    assert entry.series_order == 1
    assert entry.narrator == "Victor Bevine"
    assert entry.isbn == "9780553283686"
    assert entry.asin == "B004G60EHS"


def test_cli_library_subcommand_list_tag_hide_trash_restore(tmp_path, capsys):
    db_path = tmp_path / "downloads.db"
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text("", encoding="utf-8")

    archive = DownloadArchive(str(db_path))
    entry_id = archive.record(
        "librivox",
        "https://librivox.org/book/1",
        title="Moby Dick",
        author="Herman Melville",
        url="https://librivox.org/book/1",
        output_path="/books/MobyDick.m4b",
    )

    with pytest.raises(SystemExit) as exc1:
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "tag", str(entry_id), "--add", "Classic", "Sea"])
    assert exc1.value.code == 0

    with pytest.raises(SystemExit) as exc2:
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "hide", str(entry_id)])
    assert exc2.value.code == 0

    with pytest.raises(SystemExit) as exc3:
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "list", "--query", "tag:classic"])
    assert exc3.value.code == 0
    out = capsys.readouterr().out
    assert "Moby Dick" in out
    assert "classic" in out and "hidden" in out

    with pytest.raises(SystemExit):
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "trash", str(entry_id)])
    assert archive.get_entry(entry_id).status == "trashed"

    with pytest.raises(SystemExit):
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "restore", str(entry_id)])
    assert archive.get_entry(entry_id).status == "downloaded"


def test_gui_archive_dialog_tags_hide_trash_and_queue(tmp_path):
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
        ledger.upsert_catalog_items(
            "librivox",
            [CatalogBook(title="Pending Book", url="https://librivox.org/pending", author="Jane Doe")],
        )

        dialog = app._show_archive_ledger_dialog()
        root.update_idletasks()
        try:
            iids = dialog._tree.get_children()
            assert len(iids) == 1
            dialog._tree.selection_set(iids[0])
            dialog._queue_selected()
            assert "https://librivox.org/pending" in app.urls_text.get("1.0", tk.END)

            dialog._toggle_hide_selected()
            assert len(dialog._tree.get_children()) == 0
            dialog._show_hidden_var.set(True)
            dialog._refresh()
            assert len(dialog._tree.get_children()) == 1

            dialog._tree.selection_set(dialog._tree.get_children()[0])
            dialog._toggle_trash_selected()
            assert len(dialog._tree.get_children()) == 0
            dialog._show_trash_var.set(True)
            dialog._refresh()
            assert len(dialog._tree.get_children()) == 1
        finally:
            dialog.destroy()
    finally:
        root.destroy()


def test_gui_series_and_podcast_grouping_in_catalog_and_archive(tmp_path):
    from audiobookdl.gui.catalog_dialog import CatalogBrowserDialog
    from audiobookdl.sources import ServiceInfo

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        added_urls = []
        services = [
            ServiceInfo(
                key="librivox",
                label="LibriVox",
                auth_methods=(),
                login_fields=(),
                supports_search=True,
                supports_library=True,
            )
        ]
        dialog = CatalogBrowserDialog(
            root,
            services,
            services[0],
            on_fetch=lambda *_a: None,
            on_add_urls=lambda urls: added_urls.extend(urls),
        )
        try:
            dialog.populate_items(
                [
                    CatalogBook(
                        title="Dune",
                        url="https://example.com/dune1",
                        author="Frank Herbert",
                        series="Dune",
                        series_order=1,
                    ),
                    CatalogBook(
                        title="Dune Messiah",
                        url="https://example.com/dune2",
                        author="Frank Herbert",
                        series="Dune",
                        series_order=2,
                    ),
                    CatalogBook(
                        title="Episode 10",
                        url="https://example.com/pod10",
                        author="Host",
                        series="Tech Talk",
                        item_type="podcast",
                    ),
                    CatalogBook(
                        title="Standalone",
                        url="https://example.com/solo",
                        author="Author",
                    ),
                ]
            )
            roots = dialog._tree.get_children()
            # Expect: Dune group (2 children), Tech Talk podcast group (1 child), Standalone leaf
            assert len(roots) == 3
            assert len(dialog._tree.get_children(roots[0])) == 2
            assert len(dialog._tree.get_children(roots[1])) == 1
            assert dialog._tree.get_children(roots[2]) == ()

            # Toggle flat mode
            dialog._group_var.set(False)
            dialog._render_tree()
            assert len(dialog._tree.get_children()) == 4

            # Back to grouped and select the Dune parent group node
            dialog._group_var.set(True)
            dialog._render_tree()
            dune_group_iid = dialog._tree.get_children()[0]
            dialog._tree.selection_set((dune_group_iid,))
            dialog.add_selected()
            assert added_urls == ["https://example.com/dune1", "https://example.com/dune2"]
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

        app = AudiobookDLApp(root, config_location=str(tmp_path / "audiobook-dl.toml"))
        archive_path = tmp_path / "downloads.db"
        app.controller.config.download_archive = str(archive_path)
        ledger = DownloadArchive(str(archive_path))
        ledger.upsert_catalog_items(
            "librivox",
            [
                CatalogBook(title="Book One", url="https://example.com/b1", author="A", series="Saga", series_order=1),
                CatalogBook(title="Book Two", url="https://example.com/b2", author="A", series="Saga", series_order=2),
                CatalogBook(title="Single", url="https://example.com/s", author="B"),
            ],
        )
        arch_dialog = app._show_archive_ledger_dialog()
        root.update_idletasks()
        try:
            arch_roots = arch_dialog._tree.get_children()
            assert len(arch_roots) == 2  # 1 group node ("Saga") + 1 standalone leaf
            group_iid = next(r for r in arch_roots if arch_dialog._tree.get_children(r))
            arch_dialog._tree.selection_set((group_iid,))
            arch_dialog._queue_selected()
            queued_text = app.urls_text.get("1.0", tk.END)
            assert "https://example.com/b1" in queued_text
            assert "https://example.com/b2" in queued_text

            arch_dialog._group_var.set(False)
            arch_dialog._refresh()
            assert len(arch_dialog._tree.get_children()) == 3
        finally:
            arch_dialog.destroy()
    finally:
        root.destroy()


def test_archive_handles_over_1100_rows_without_sqlite_variable_limit(tmp_path):
    db_path = tmp_path / "large.db"
    archive = DownloadArchive(str(db_path), persistent=True)
    try:
        items = [
            CatalogBook(
                title=f"Book {idx:04d}",
                url=f"https://example.com/book/{idx}",
                author=f"Author {idx % 10}",
                source="storytel",
            )
            for idx in range(1150)
        ]
        inserted, _ = archive.upsert_catalog_items("storytel", items)
        assert inserted == 1150

        # Hide first entry and verify include_hidden=False + large query do not raise
        first = archive.list_entries(query="title:Book 0000", limit=10)
        assert len(first) == 1
        archive.add_tags(first[0].id, ["hidden"])

        visible = archive.list_entries(include_hidden=False, limit=1200)
        assert len(visible) == 1149
        assert archive.find_matching_entry_id(scrape_url="https://example.com/book/999") is not None
    finally:
        archive.close()


def test_resolve_archive_path_precedence(tmp_path):
    from audiobookdl.config import Config
    from audiobookdl.utils.archive import default_archive_path, resolve_archive_path

    db_dir = str(tmp_path / "db")
    cfg = Config(
        sources={},
        output_template=None,
        database_directory=db_dir,
        skip_downloaded=None,
        download_archive=str(tmp_path / "configured.db"),
    )
    assert resolve_archive_path(cfg, str(tmp_path / "explicit.db")) == str(tmp_path / "explicit.db")
    assert resolve_archive_path(cfg) == str(tmp_path / "configured.db")
    cfg.download_archive = None
    assert resolve_archive_path(cfg) == default_archive_path(db_dir)


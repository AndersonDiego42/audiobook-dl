import sqlite3
import tkinter as tk
from typing import cast

import pytest
import requests

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, args
from audiobookdl.__main__ import main as cli_main
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.pipeline import process_audiobook
from audiobookdl.sources.source import CatalogBook, Source
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
    src = cast(Source, _Src())
    assert process_audiobook(src, book, opts) is True
    # Second run skips via default downloads.db even when output file isn't on disk
    assert process_audiobook(src, book, opts) is False

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
    trashed = archive.get_entry(entry_id)
    assert trashed is not None and trashed.status == "trashed"

    with pytest.raises(SystemExit):
        cli_main(["library", "--config", str(cfg_path), "--download-archive", str(db_path), "restore", str(entry_id)])
    restored = archive.get_entry(entry_id)
    assert restored is not None and restored.status == "downloaded"


@pytest.mark.gui
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

        lib_page = app.open_library_tab("archive")
        tab = lib_page.archive_tab
        root.update_idletasks()
        iids = tab._tree.get_children()
        assert len(iids) == 1
        tab._tree.selection_set(iids[0])
        tab._queue_selected()
        assert "https://librivox.org/pending" in app.urls_text.get("1.0", tk.END)

        tab._toggle_hide_selected()
        assert len(tab._tree.get_children()) == 0
        tab._show_hidden_var.set(True)
        tab._refresh()
        assert len(tab._tree.get_children()) == 1

        tab._tree.selection_set(tab._tree.get_children()[0])
        tab._toggle_trash_selected()
        assert len(tab._tree.get_children()) == 0
        tab._show_trash_var.set(True)
        tab._refresh()
        assert len(tab._tree.get_children()) == 1
    finally:
        root.destroy()


@pytest.mark.gui
def test_gui_series_and_podcast_grouping_in_catalog_and_archive(tmp_path):
    from audiobookdl.gui.pages.library_page import CatalogTab
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
        dialog = CatalogTab(
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
        arch_tab = app.open_library_tab("archive").archive_tab
        root.update_idletasks()
        arch_roots = arch_tab._tree.get_children()
        assert len(arch_roots) == 2  # 1 group node ("Saga") + 1 standalone leaf
        group_iid = next(r for r in arch_roots if arch_tab._tree.get_children(r))
        arch_tab._tree.selection_set((group_iid,))
        arch_tab._queue_selected()
        queued_text = app.urls_text.get("1.0", tk.END)
        assert "https://example.com/b1" in queued_text
        assert "https://example.com/b2" in queued_text

        arch_tab._group_var.set(False)
        arch_tab._refresh()
        assert len(arch_tab._tree.get_children()) == 3
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


def test_lookup_catalog_statuses_badges(tmp_path):
    db_path = tmp_path / "catalog_status.db"
    archive = DownloadArchive(str(db_path), persistent=True)
    try:
        b_dl = CatalogBook(title="Downloaded Book", url="https://example.com/dl", author="Auth A", source="librivox")
        b_pend = CatalogBook(title="Pending Book", url="https://example.com/pend", author="Auth B", source="librivox")
        b_hid = CatalogBook(title="Hidden Book", url="https://example.com/hid", author="Auth C", source="librivox")
        b_dl_hid = CatalogBook(title="Hidden DL", url="https://example.com/dlh", author="Auth D", source="librivox")
        b_trash = CatalogBook(title="Trashed Book", url="https://example.com/trash", author="Auth E", source="librivox")
        b_new = CatalogBook(title="Fresh Book", url="https://example.com/new", author="Auth F", source="librivox")
        b_title_match = CatalogBook(title="Local Title Match", url="https://example.com/alt-url", author="Auth G", source="librivox")

        archive.record("librivox", b_dl.url, title=b_dl.title, author=b_dl.author, url=b_dl.url, status="downloaded")
        archive.record("librivox", b_pend.url, title=b_pend.title, author=b_pend.author, url=b_pend.url, status="pending")
        hid_id = archive.record("librivox", b_hid.url, title=b_hid.title, author=b_hid.author, url=b_hid.url, status="pending")
        archive.add_tags(hid_id, ["hidden"])
        dl_hid_id = archive.record("librivox", b_dl_hid.url, title=b_dl_hid.title, author=b_dl_hid.author, url=b_dl_hid.url, status="downloaded")
        archive.add_tags(dl_hid_id, ["hidden"])
        archive.record("librivox", b_trash.url, title=b_trash.title, author=b_trash.author, url=b_trash.url, status="trashed")
        archive.record("local", "local-1", title="Local Title Match", author="Auth G", status="downloaded")

        badges = archive.lookup_catalog_statuses(
            "librivox",
            [b_dl, b_pend, b_hid, b_dl_hid, b_trash, b_new, b_title_match],
        )
        assert badges == [
            "Downloaded",
            "Pending",
            "Hidden",
            "Downloaded (Hidden)",
            "Trashed",
            "",
            "Downloaded",
        ]
    finally:
        archive.close()


def test_archive_count_offset_pagination_and_update_entry(tmp_path):
    db_path = tmp_path / "paginated.db"
    archive = DownloadArchive(str(db_path), persistent=True)
    try:
        for i in range(7):
            archive.record(
                "librivox",
                f"k{i}",
                title=f"Book {i:02d}",
                author="Author X" if i % 2 == 0 else "Author Y",
                status="downloaded",
            )
        assert archive.count_entries() == 7
        assert archive.count_entries(query='author:"Author X"') == 4

        page1 = archive.list_entries(limit=3, offset=0)
        page2 = archive.list_entries(limit=3, offset=3)
        page3 = archive.list_entries(limit=3, offset=6)
        assert [e.title for e in page1] == ["Book 06", "Book 05", "Book 04"]
        assert [e.title for e in page2] == ["Book 03", "Book 02", "Book 01"]
        assert [e.title for e in page3] == ["Book 00"]

        # Python-filter fallback path (negated term) respects count_entries and offset
        assert archive.count_entries(query='-author:"Author Y"') == 4
        neg_p2 = archive.list_entries(query='-author:"Author Y"', limit=2, offset=2)
        assert len(neg_p2) == 2

        target_id = page3[0].id
        moved_file = tmp_path / "Relinked.m4b"
        updated = archive.update_entry(
            target_id,
            title="Book 00 Revised",
            author="New Author",
            narrator="New Narrator",
            series="Saga",
            series_order=1,
            isbn="9780000000001",
            asin="B0NEW123",
            output_path=str(moved_file),
            status="pending",
            tags=["Sci-Fi", "Edited"],
        )
        assert updated is not None
        assert updated.title == "Book 00 Revised"
        assert updated.author == "New Author"
        assert updated.narrator == "New Narrator"
        assert updated.series == "Saga"
        assert updated.series_order == 1
        assert updated.isbn == "9780000000001"
        assert updated.asin == "B0NEW123"
        assert updated.output_path == str(moved_file)
        assert updated.status == "pending"
        assert updated.tags == ("edited", "sci-fi")

        with pytest.raises(ValueError, match="Title cannot be empty"):
            archive.update_entry(target_id, title="   ")
        with pytest.raises(ValueError, match="Invalid library status"):
            archive.update_entry(target_id, status="bogus")
    finally:
        archive.close()



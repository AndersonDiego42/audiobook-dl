"""End-to-end tests for the desktop Tkinter GUI and `--self-test` entry point."""

from __future__ import annotations

import json
import tkinter as tk

from mutagen.mp4 import MP4
import pytest

from audiobookdl.config import load_config
from audiobookdl.utils import ffprobe_duration
from tests.e2e.fixtures import MediaFixtureFactory, build_librivox_html
from tests.e2e.harness import E2EWorkspace, GuiHarness, GuiSelfTestRunner
from tests.e2e.server import LocalAudiobookServer


pytestmark = [pytest.mark.e2e, pytest.mark.gui]


def test_gui_self_test_subprocess(gui_self_test_runner: GuiSelfTestRunner) -> None:
    res = gui_self_test_runner.run_gui_self_test()
    assert res.returncode == 0, res.stderr
    assert not res.timed_out


@pytest.mark.ffmpeg
def test_gui_preview_and_download_m4b_end_to_end(
    gui_harness: GuiHarness,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = ffmpeg_tools
    part1 = media_factory.make_mp3("gui_p1.mp3", duration_sec=1.0, freq_hz=440)
    part2 = media_factory.make_mp3("gui_p2.mp3", duration_sec=1.0, freq_hz=550)
    cover = media_factory.make_jpeg()

    book_url = "http://librivox.org/books/gui-e2e-book"
    e2e_server.add_route(
        "/books/gui-e2e-book",
        build_librivox_html(
            title="GUI E2E Audiobook",
            cover_url="http://librivox.org/assets/gui-cover.jpg",
            parts=[
                ("Chapter 1", "http://librivox.org/audio/gui1.mp3"),
                ("Chapter 2", "http://librivox.org/audio/gui2.mp3"),
            ],
        ),
        content_type="text/html; charset=utf-8",
        host="librivox.org",
    )
    e2e_server.add_route("/assets/gui-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")
    e2e_server.add_route("/audio/gui1.mp3", part1, content_type="audio/mpeg", host="librivox.org")
    e2e_server.add_route("/audio/gui2.mp3", part2, content_type="audio/mpeg", host="librivox.org")

    monkeypatch.setenv("HTTP_PROXY", e2e_server.url)
    monkeypatch.setenv("http_proxy", e2e_server.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    app = gui_harness.start()
    app.urls_text.insert("1.0", f"{book_url}\n")
    app._urls.on_urls_changed()
    assert app.service_label_var.get() == "Librivox"

    # 1. Preview metadata through real background fetch + Tkinter dialog
    app.preview_metadata()
    gui_harness.pump_until(
        lambda: not app.is_previewing and app._preview.dialog is not None,
        description="preview dialog to open",
    )
    assert app._preview.dialog is not None
    assert "GUI E2E Audiobook" in app._preview.dialog.title()
    assert hasattr(app._preview.dialog, "_cover_photo")
    app._preview.dialog.destroy()

    # 2. Configure .m4b + JSON sidecar and run full download via real worker thread
    app.m4b_var.set(True)
    app._options.on_m4b_toggled()
    app.write_json_var.set(True)
    app.start_download()
    assert app.is_running

    gui_harness.pump_until(
        lambda: not app.is_running,
        description="GUI download completion",
    )

    assert app.status_var.get() == "Completed"
    assert float(app.progressbar.cget("value")) == pytest.approx(100.0)
    assert float(app.shell.footer_progressbar.cget("value")) == pytest.approx(100.0)
    log_text = app.log_text.get("1.0", tk.END)
    assert "GUI E2E Audiobook" in log_text
    assert "--- Completed ---" in log_text

    # Sidebar navigation updates Library archive tab on visit
    lib_page = app.navigate("library")
    assert len(lib_page.archive_tab._tree.get_children()) >= 0
    app.navigate("settings")
    app.navigate("download")
    assert app.shell.active_page_id == "download"

    out_m4b = e2e_workspace.output_dir / "GUI E2E Audiobook.m4b"
    out_json = e2e_workspace.output_dir / "GUI E2E Audiobook.m4b.json"
    assert out_m4b.is_file()
    assert out_json.is_file()
    assert json.loads(out_json.read_text(encoding="utf-8"))["title"] == "GUI E2E Audiobook"
    assert 1.8 <= ffprobe_duration(str(out_m4b)) <= 2.3
    mp4 = MP4(str(out_m4b))
    assert mp4.tags is not None
    assert mp4.tags["\xa9nam"] == ["GUI E2E Audiobook"]

    # 3. Verify preferences persist on close
    gui_harness.close()
    reloaded = load_config(str(e2e_workspace.config_path))
    assert reloaded.gui.output_dir == str(e2e_workspace.output_dir)
    assert reloaded.gui.output_template == "{title}"


@pytest.mark.ffmpeg
def test_gui_pack_page_scan_chapter_edit_and_m4b_end_to_end(
    gui_harness: GuiHarness,
    e2e_workspace: E2EWorkspace,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
) -> None:
    _ = ffmpeg_tools
    src_folder = e2e_workspace.work_dir / "Local E2E Album"
    src_folder.mkdir(parents=True, exist_ok=True)
    p1 = media_factory.make_mp3("01 Intro.mp3", duration_sec=1.0, freq_hz=440)
    p2 = media_factory.make_mp3("02 Chapter.mp3", duration_sec=1.0, freq_hz=550)
    (src_folder / "01 Intro.mp3").write_bytes(p1)
    (src_folder / "02 Chapter.mp3").write_bytes(p2)

    app = gui_harness.start()
    app.template_var.set("{title}")
    pack_page = app.navigate("pack")
    pack_page.add_initial_paths([str(src_folder)])
    pack_page._start_scan()
    gui_harness.pump_until(
        lambda: len(pack_page._preview_specs) == 1,
        description="PackPage folder scan",
    )
    pack_page._tree.selection_set("0")
    pack_page._on_book_selected()
    chapters = pack_page._chapter_editor.get_chapters()
    assert len(chapters) == 2
    from audiobookdl.utils.audiobook import Chapter

    chapters[1] = Chapter(start=chapters[1].start, title="Edited Chapter Two")
    pack_page._on_chapters_edited(chapters)

    pack_page._submit()
    assert app.is_running
    gui_harness.pump_until(
        lambda: not app.is_running,
        description="PackPage M4B encode completion",
    )
    assert app.status_var.get() == "Completed"
    out_m4b = e2e_workspace.output_dir / "Local E2E Album.m4b"
    assert out_m4b.is_file()
    mp4 = MP4(str(out_m4b))
    assert mp4.chapters is not None and len(mp4.chapters) == 2
    assert mp4.chapters[1].title == "Edited Chapter Two"


def test_gui_library_catalog_archive_and_export_end_to_end(
    gui_harness: GuiHarness,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cover = media_factory.make_jpeg()
    search_json = json.dumps(
        {
            "books": [
                {
                    "id": "99",
                    "title": "Catalog E2E Book",
                    "url_librivox": "http://librivox.org/books/catalog-e2e",
                    "authors": [{"first_name": "Jane", "last_name": "Reader"}],
                    "totaltimesecs": 120,
                    "url_thumb": "http://librivox.org/assets/cat-cover.jpg",
                }
            ]
        }
    ).encode("utf-8")
    e2e_server.add_route(
        "/api/feed/audiobooks",
        search_json,
        content_type="application/json; charset=utf-8",
        host="librivox.org",
    )
    e2e_server.add_route("/assets/cat-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")

    monkeypatch.setenv("HTTP_PROXY", e2e_server.url)
    monkeypatch.setenv("http_proxy", e2e_server.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    import requests as req_mod

    orig_request = req_mod.Session.request

    def _http_proxy_rewrite(self_sess, method, url, *a, **kw):
        if isinstance(url, str) and url.startswith("https://librivox.org/"):
            url = "http://" + url[len("https://") :]
        return orig_request(self_sess, method, url, *a, **kw)

    monkeypatch.setattr(req_mod.Session, "request", _http_proxy_rewrite)

    app = gui_harness.start()
    lib_page = app.open_library_tab("catalog")
    cat = lib_page.catalog_tab
    cat._service_var.set("Librivox")
    cat._query_var.set("Catalog E2E Book")
    cat.run_fetch("search")
    gui_harness.pump_until(
        lambda: len(cat._tree.get_children()) == 1,
        description="CatalogTab search results",
    )
    assert "Catalog E2E Book" in str(cat._tree.item("book-0", "values"))

    # Verify pooled HTTP proxy cover loading + ArchiveTab export after saving to library
    from audiobookdl.sources.source import CatalogBook

    cat.populate_items(
        [
            CatalogBook(
                title="Catalog E2E Book",
                url="http://librivox.org/books/catalog-e2e",
                author="Jane Reader",
                source="librivox",
                cover_url="http://librivox.org/assets/cat-cover.jpg",
            )
        ]
    )
    gui_harness.pump_until(
        lambda: "http://librivox.org/assets/cat-cover.jpg" in cat._url_photos,
        description="CatalogTab cover thumbnail fetch",
    )

    # ArchiveTab export after saving catalog selection into downloads.db
    cat._tree.selection_set("book-0")
    cat.save_to_library()
    arch_tab = app.open_library_tab("archive").archive_tab
    arch_tab.refresh()
    assert len(arch_tab._tree.get_children()) >= 1
    export_json = e2e_workspace.work_dir / "lib_export.json"
    from audiobookdl.utils.export import export_library_entries

    export_library_entries(list(arch_tab._entries_by_id.values()), str(export_json), fmt="json")
    assert "Catalog E2E Book" in export_json.read_text(encoding="utf-8")


def test_gui_startup_non_download_page_restores_queue_without_duplication(
    gui_harness: GuiHarness,
    e2e_workspace: E2EWorkspace,
) -> None:
    e2e_workspace.write_toml(
        f'database_directory = "{e2e_workspace.db_dir.as_posix()}"\n'
        '[gui]\nactive_page = "library"\n'
        f'output_dir = "{e2e_workspace.output_dir.as_posix()}"\n'
        'output_template = "{title}"\n'
    )
    queue_dir = e2e_workspace.db_dir
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_file = queue_dir / "gui_queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {
                    "job_kind": "download",
                    "url": "https://librivox.org/restored-1",
                    "service": "Librivox",
                    "profile": "default",
                    "status": "Interrupted",
                    "detail": "Interrupted",
                }
            ]
        ),
        encoding="utf-8",
    )
    app = gui_harness.start()
    assert app.shell.active_page_id == "library"
    assert len(app.controller.queue_records) == 1
    dl_page = app.navigate("download")
    assert len(dl_page.queue_panel.queue_tree.get_children()) == 1
    gui_harness.close()
    saved = json.loads(queue_file.read_text(encoding="utf-8"))
    assert len(saved) == 1

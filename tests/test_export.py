import csv
import importlib.util
import json
import tkinter as tk

import pytest

from audiobookdl.exceptions import GenericAudiobookDLException
from audiobookdl.gui.app import AudiobookDLApp
from audiobookdl.utils.archive import DownloadArchive
from audiobookdl.utils.export import export_library_entries


def _seed_entries(db_path):
    archive = DownloadArchive(str(db_path))
    id1 = archive.record(
        "audible",
        "https://audible.com/pd/B001",
        title="Dune",
        author="Frank Herbert",
        url="https://audible.com/pd/B001",
        output_path="/books/Dune.m4b",
        audio_format="m4b",
        profile="us",
        series="Dune",
        series_order=1,
        narrator="Scott Brick",
        isbn="9780441172719",
        asin="B002V1OF70",
    )
    archive.add_tags(id1, ["scifi", "favorite"])
    id2 = archive.record(
        "librivox",
        "https://librivox.org/2",
        title="Hidden Gem",
        author="Jane Doe",
        status="pending",
    )
    archive.add_tags(id2, ["hidden"])
    return archive


def test_export_csv_and_json_via_api(tmp_path):
    db_path = tmp_path / "downloads.db"
    archive = _seed_entries(db_path)

    visible_entries = archive.list_entries(include_hidden=False)
    csv_path = tmp_path / "out" / "library.csv"
    count_csv = export_library_entries(visible_entries, str(csv_path))
    assert count_csv == 1

    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["title"] == "Dune"
    assert rows[0]["profile"] == "us"
    assert rows[0]["tags"] == "favorite scifi"
    assert rows[0]["asin"] == "B002V1OF70"

    pending_entries = archive.list_entries(query="status:pending", include_hidden=True)
    json_path = tmp_path / "library.json"
    count_json = export_library_entries(pending_entries, str(json_path))
    assert count_json == 1
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["title"] == "Hidden Gem"
    assert data[0]["tags"] == ["hidden"]


@pytest.mark.gui
def test_export_xlsx_behavior_and_gui_export_to_path(tmp_path):
    db_path = tmp_path / "downloads.db"
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text("", encoding="utf-8")
    archive = _seed_entries(db_path)
    entries = archive.list_entries(include_hidden=True)

    xlsx_path = tmp_path / "library.xlsx"
    if importlib.util.find_spec("openpyxl") is None:
        with pytest.raises(GenericAudiobookDLException, match="openpyxl"):
            export_library_entries(entries, str(xlsx_path))
    else:
        count = export_library_entries(entries, str(xlsx_path))
        assert count == 2
        assert xlsx_path.exists()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        app = AudiobookDLApp(root, config_location=str(cfg_path))
        app.controller.config.download_archive = str(db_path)
        tab = app.open_library_tab("archive").archive_tab
        gui_csv = tmp_path / "gui.csv"
        exported_count = tab.export_to_path(str(gui_csv))
        # Show Hidden is unchecked by default, so 1 visible book exported
        assert exported_count == 1
        assert "Dune" in gui_csv.read_text(encoding="utf-8")
    finally:
        root.destroy()

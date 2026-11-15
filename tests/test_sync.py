import json
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from audiobookdl import args
from audiobookdl.__main__ import main as cli_main
from audiobookdl.config import Config, SourceConfig
from audiobookdl.sources.source import CatalogBook
from audiobookdl.sync import run_sync_loop, run_sync_once
from audiobookdl.utils.archive import DownloadArchive, make_catalog_book_key


def test_sync_once_downloads_pending_and_skips_hidden_trashed_and_downloaded(tmp_path):
    db_path = tmp_path / "downloads.db"
    archive = DownloadArchive(str(db_path))

    # Pre-seed book 2 as already downloaded, book 3 as trashed, book 4 as hidden
    b1 = CatalogBook(title="New Book", url="https://librivox.org/b1", author="A")
    b2 = CatalogBook(title="Already Done", url="https://librivox.org/b2", author="B")
    b3 = CatalogBook(title="Trashed Book", url="https://librivox.org/b3", author="C")
    b4 = CatalogBook(title="Hidden Book", url="https://librivox.org/b4", author="D")

    archive.record("librivox", make_catalog_book_key("librivox", b2), title=b2.title, url=b2.url, profile="us")
    id3 = archive.record(
        "librivox", make_catalog_book_key("librivox", b3), title=b3.title, url=b3.url, status="trashed", profile="us"
    )
    id4 = archive.record(
        "librivox", make_catalog_book_key("librivox", b4), title=b4.title, url=b4.url, status="pending", profile="us"
    )
    archive.add_tags(id4, ["hidden"])
    assert id3 > 0

    cfg = Config(
        sources={
            "librivox:us": SourceConfig(username="u1", password=None, library=None, cookie_file=None),
            "audible:uk": SourceConfig(username="u2", password=None, library=None, cookie_file=None),
        },
        output_template=str(tmp_path / "{title}"),
        database_directory=str(tmp_path),
        skip_downloaded=True,
        download_archive=str(db_path),
    )

    downloaded_batches = []

    class _DummyDownloader:
        def __init__(self, config, options, cancel_event=None):
            self.options = options

        def download(self, urls):
            downloaded_batches.append((self.options.profile, list(urls)))
            from audiobookdl.pipeline import DownloadSummary

            s = DownloadSummary()
            s.books_completed = len(urls)
            return s

    sync_opts = args.parse_sync_arguments(
        ["--download-archive", str(db_path)]
    )
    with patch("audiobookdl.sync.list_service_library", return_value=[b1, b2, b3, b4]), patch(
        "audiobookdl.sync.Downloader", _DummyDownloader
    ):
        cycle = run_sync_once(sync_opts, config=cfg)

    assert cycle.profiles_checked == 1
    assert cycle.books_discovered == 4
    assert cycle.newly_added == 1
    assert cycle.pending_count == 1
    assert cycle.skipped == 3
    assert cycle.downloaded == 1
    assert downloaded_batches == [("us", ["https://librivox.org/b1"])]


def test_sync_cli_scan_only_json_and_watch_loop(tmp_path, capsys):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_path = tmp_path / "downloads.db"
    cfg_path.write_text('[sources.librivox]\nusername = "demo"\n', encoding="utf-8")

    books = [CatalogBook(title="Sync Item", url="https://librivox.org/s1", author="Author")]
    with patch("audiobookdl.sync.list_service_library", return_value=books), patch(
        "audiobookdl.sync.Downloader"
    ) as mock_dl:
        with pytest.raises(SystemExit) as exc:
            cli_main(
                [
                    "sync",
                    "--config",
                    str(cfg_path),
                    "--download-archive",
                    str(db_path),
                    "--scan-only",
                    "--json",
                ]
            )
        assert exc.value.code == 0
        mock_dl.assert_not_called()

    payload = json.loads(capsys.readouterr().out)
    assert payload["books_discovered"] == 1
    assert payload["newly_added"] == 1
    assert payload["pending_count"] == 1
    assert payload["downloaded"] == 0

    # Verify watch loop runs multiple cycles and stops cleanly on event
    sync_opts = args.parse_sync_arguments(
        ["--config", str(cfg_path), "--download-archive", str(db_path), "--scan-only", "--watch", "--interval", "1"]
    )
    cycles = []
    stop_event = threading.Event()
    with patch("audiobookdl.sync.list_service_library", return_value=books):
        code = run_sync_loop(
            sync_opts,
            stop_event=stop_event,
            max_cycles=2,
            on_cycle=lambda c: (cycles.append(c), stop_event.set() if len(cycles) >= 2 else None),
        )
    assert code == 0
    assert len(cycles) == 2


def test_dockerfile_defines_non_root_entrypoint_and_volumes():
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["audiobook-dl"]' in dockerfile
    assert "USER audiobookdl" in dockerfile
    assert 'VOLUME ["/config", "/audiobooks"]' in dockerfile

from unittest.mock import patch

from audiobookdl.config import Config, SourceConfig
from audiobookdl.options_schema import SyncOptions
from audiobookdl.sources.source import CatalogBook
from audiobookdl.sync import run_sync_once
from audiobookdl.utils.archive import DownloadArchive, make_catalog_book_key


def test_sync_once_downloads_pending_and_skips_hidden_trashed_and_downloaded(tmp_path):
    db_path = tmp_path / "downloads.db"
    archive = DownloadArchive(str(db_path))

    # Pre-seed book 2 as already downloaded, book 3 as trashed, book 4 as hidden
    b1 = CatalogBook(title="New Book", url="https://storytel.com/b1", author="A")
    b2 = CatalogBook(title="Already Done", url="https://storytel.com/b2", author="B")
    b3 = CatalogBook(title="Trashed Book", url="https://storytel.com/b3", author="C")
    b4 = CatalogBook(title="Hidden Book", url="https://storytel.com/b4", author="D")

    archive.record("storytel", make_catalog_book_key("storytel", b2), title=b2.title, url=b2.url, profile="us")
    id3 = archive.record(
        "storytel", make_catalog_book_key("storytel", b3), title=b3.title, url=b3.url, status="trashed", profile="us"
    )
    id4 = archive.record(
        "storytel", make_catalog_book_key("storytel", b4), title=b4.title, url=b4.url, status="pending", profile="us"
    )
    archive.add_tags(id4, ["hidden"])
    assert id3 > 0

    cfg = Config(
        sources={
            "storytel:us": SourceConfig(username="u1", password=None, library=None, cookie_file=None),
            "librivox": SourceConfig(username="u2", password=None, library=None, cookie_file=None),
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

    sync_opts = SyncOptions(
        archive_path=str(db_path),
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
    assert downloaded_batches == [("us", ["https://storytel.com/b1"])]


def test_sync_once_scan_only_updates_db_without_downloading(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_path = tmp_path / "downloads.db"
    cfg_path.write_text('[sources.storytel]\nusername = "demo"\n', encoding="utf-8")

    books = [CatalogBook(title="Sync Item", url="https://storytel.com/s1", author="Author")]
    sync_opts = SyncOptions(
        config_location=str(cfg_path),
        archive_path=str(db_path),
        scan_only=True,
    )
    with patch("audiobookdl.sync.list_service_library", return_value=books), patch(
        "audiobookdl.sync.Downloader"
    ) as mock_dl:
        cycle = run_sync_once(sync_opts)
        mock_dl.assert_not_called()

    assert cycle.books_discovered == 1
    assert cycle.newly_added == 1
    assert cycle.pending_count == 1
    assert cycle.downloaded == 0


def test_sync_once_preserves_config_output_template_and_format_when_sync_opts_unset(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_path = tmp_path / "downloads.db"
    cfg_path.write_text(
        'output_template = "custom/{author}/{title}"\n'
        'output_format = "m4b"\n\n'
        '[sources.storytel]\n'
        'username = "demo"\n',
        encoding="utf-8",
    )

    captured_options = []

    class _CapturingDownloader:
        def __init__(self, config, options, cancel_event=None):
            captured_options.append(options)

        def download(self, urls):
            from audiobookdl.pipeline import DownloadSummary

            s = DownloadSummary()
            s.books_completed = len(urls)
            return s

    books = [CatalogBook(title="Sync Item", url="https://storytel.com/s1", author="Author")]
    sync_opts = SyncOptions(
        config_location=str(cfg_path),
        archive_path=str(db_path),
    )
    with patch("audiobookdl.sync.list_service_library", return_value=books), patch(
        "audiobookdl.sync.Downloader", _CapturingDownloader
    ):
        cycle = run_sync_once(sync_opts)

    assert cycle.downloaded == 1
    assert len(captured_options) == 1
    assert captured_options[0].output_template == "custom/{author}/{title}"
    assert captured_options[0].output_format == "m4b"
    assert captured_options[0].skip_unavailable is True


def test_sync_once_propagates_skipped_unavailable_books(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_path = tmp_path / "downloads.db"
    cfg_path.write_text('[sources.storytel]\nusername = "demo"\n', encoding="utf-8")

    class _FakeDownloader:
        def __init__(self, config, options, cancel_event=None):
            self.options = options

        def download(self, _urls):
            from audiobookdl.pipeline import DownloadSummary

            assert self.options.skip_unavailable is True
            return DownloadSummary(completed=2, failed=0, books_completed=1, books_skipped=1)

    books = [
        CatalogBook(title="Good", url="https://storytel.com/good", author="Author"),
        CatalogBook(title="Locked", url="https://storytel.com/locked", author="Author"),
    ]
    sync_opts = SyncOptions(config_location=str(cfg_path), archive_path=str(db_path))
    with patch("audiobookdl.sync.list_service_library", return_value=books), patch(
        "audiobookdl.sync.Downloader", _FakeDownloader
    ):
        cycle = run_sync_once(sync_opts)

    assert cycle.downloaded == 1
    assert cycle.skipped == 1
    assert cycle.failed == 0



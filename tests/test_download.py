from types import SimpleNamespace
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.output.download import (
    DOWNLOAD_CHUNK_SIZE,
    MAX_DOWNLOAD_WORKERS,
    download_audiobook,
    download_file,
    download_files,
)
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT


def _make_audiobook(session, url="https://example.com/part.mp3"):
    return Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Test Book"),
        files=[AudiobookFile(url=url, ext="mp3")],
    )


def _mock_response(headers, chunks, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers
    resp.iter_content.return_value = iter(chunks)
    return resp


def test_download_file_passes_default_timeout_and_handles_missing_content_length(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Transfer-Encoding": "chunked", "Content-Type": "audio/mpeg"},
        chunks=[b"abc", b"", b"defgh"],
    )
    audiobook = _make_audiobook(session)
    output_dir = str(tmp_path / "book")
    progress = []

    out_path = download_file((audiobook, output_dir, 0, progress.append, None))

    assert out_path == f"{output_dir}.mp3"
    with open(out_path, "rb") as f:
        assert f.read() == b"abcdefgh"
    session.get.assert_called_once_with(
        "https://example.com/part.mp3",
        headers={},
        stream=True,
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    assert DOWNLOAD_CHUNK_SIZE == 64 * 1024
    session.get.return_value.iter_content.assert_called_once_with(chunk_size=DOWNLOAD_CHUNK_SIZE)
    assert progress == [1.0]
    session.get.return_value.close.assert_called_once()


def test_download_file_handles_zero_content_length_without_zero_division(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "0"},
        chunks=[b"data"],
    )
    audiobook = _make_audiobook(session)
    output_dir = str(tmp_path / "zero")
    progress = []

    out_path = download_file((audiobook, output_dir, 0, progress.append, None))

    with open(out_path, "rb") as f:
        assert f.read() == b"data"
    assert progress == [1.0]


def test_download_file_tracks_fractional_progress_with_content_length(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "10"},
        chunks=[b"1234", b"567890"],
    )
    audiobook = _make_audiobook(session)
    output_dir = str(tmp_path / "sized")
    progress = []

    download_file((audiobook, output_dir, 0, progress.append, None))

    assert progress == [pytest.approx(0.4), pytest.approx(0.6)]


def test_download_file_retries_on_timeout_and_resets_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)
    session = MagicMock()
    session.get.side_effect = [
        requests.exceptions.ReadTimeout("timed out"),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"1234"]),
    ]
    audiobook = _make_audiobook(session)
    output_dir = str(tmp_path / "retry")
    progress = []

    out_path = download_file((audiobook, output_dir, 0, progress.append, None))

    with open(out_path, "rb") as f:
        assert f.read() == b"1234"
    assert sum(progress) == pytest.approx(1.0)
    assert session.get.call_count == 2


def test_download_files_sizes_thread_pool_dynamically(tmp_path):
    def make_book(count: int) -> Audiobook:
        return Audiobook(
            session=MagicMock(),
            metadata=AudiobookMetadata(title="Book"),
            files=[AudiobookFile(url=f"https://example.com/{i}.mp3", ext="mp3") for i in range(count)],
        )

    # 0 files: returns [] without spawning a pool
    with patch("audiobookdl.utils.concurrency.ThreadPoolExecutor") as pool_cls:
        assert download_files(make_book(0), str(tmp_path), lambda _: None) == []
        pool_cls.assert_not_called()

    # 1 file: skips ThreadPoolExecutor and calls download_file directly
    with (
        patch("audiobookdl.utils.concurrency.ThreadPoolExecutor") as pool_cls,
        patch("audiobookdl.output.download.download_file", return_value="single.mp3") as mock_dl,
    ):
        assert download_files(make_book(1), str(tmp_path), lambda _: None) == ["single.mp3"]
        pool_cls.assert_not_called()
        mock_dl.assert_called_once()

    # 4 files: pool sized to 4 rather than 20; 25 files capped at 20
    pool_sizes = []
    from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

    def tracking_executor(*, max_workers: int):
        pool_sizes.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers)

    with (
        patch("audiobookdl.utils.concurrency.ThreadPoolExecutor", side_effect=tracking_executor),
        patch("audiobookdl.output.download.download_file", side_effect=lambda args: f"p{args[2]}.mp3"),
    ):
        paths_4 = download_files(make_book(4), str(tmp_path), lambda _: None)
        paths_25 = download_files(make_book(25), str(tmp_path), lambda _: None)

    assert paths_4 == [f"p{i}.mp3" for i in range(4)]
    assert paths_25 == [f"p{i}.mp3" for i in range(25)]
    assert pool_sizes == [4, MAX_DOWNLOAD_WORKERS]
    assert MAX_DOWNLOAD_WORKERS == 20


def test_download_files_cancels_queued_futures_promptly(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.MAX_DOWNLOAD_WORKERS", 2)
    cancel_event = threading.Event()
    started = []
    first_started = threading.Event()

    def slow_download(args):
        _, _, index, _, ev = args
        started.append(index)
        if len(started) == 1:
            first_started.set()
        ev.wait(timeout=2.0)
        raise DownloadCancelled

    audiobook = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Book"),
        files=[AudiobookFile(url=f"https://example.com/{i}.mp3", ext="mp3") for i in range(10)],
    )

    timer = threading.Timer(0.05, lambda: (first_started.wait(1.0), cancel_event.set()))
    timer.start()
    try:
        with patch("audiobookdl.output.download.download_file", side_effect=slow_download):
            with pytest.raises(DownloadCancelled):
                download_files(audiobook, str(tmp_path), lambda _: None, cancel_event=cancel_event)
    finally:
        timer.cancel()

    # Only the active workers started; queued futures were cancelled instead of executing
    assert len(started) <= 2


def test_single_file_overwrite_guard_respects_confirmation(tmp_path):
    class FakeInteraction:
        def __init__(self, confirm_value: bool) -> None:
            self.confirm_value = confirm_value
            self.confirms: list[str] = []

        def ask_input(self, prompt: str, hidden: bool = False) -> str:
            return ""

        def confirm(self, message: str) -> bool:
            self.confirms.append(message)
            return self.confirm_value

    existing = tmp_path / "Single Book.mp3"
    existing.write_bytes(b"original-audio")
    output_dir = str(tmp_path / "Single Book")
    options = SimpleNamespace(
        skip_downloaded=False,
        combine=False,
        output_format=None,
        write_json_metadata=False,
        no_chapters=True,
    )

    # Declining overwrite cancels before making any HTTP request
    session_decline = MagicMock()
    decline = FakeInteraction(confirm_value=False)
    with pytest.raises(DownloadCancelled):
        download_audiobook(_make_audiobook(session_decline), output_dir, options, interaction=decline)
    assert existing.read_bytes() == b"original-audio"
    assert len(decline.confirms) == 1
    session_decline.get.assert_not_called()

    # Confirming overwrite replaces existing file atomically (even on Windows)
    session_accept = MagicMock()
    session_accept.get.return_value = _mock_response(
        headers={"Content-Length": "11"},
        chunks=[b"replacement"],
    )
    accept = FakeInteraction(confirm_value=True)
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        download_audiobook(_make_audiobook(session_accept), output_dir, options, interaction=accept)
    assert existing.read_bytes() == b"replacement"
    assert len(accept.confirms) == 1

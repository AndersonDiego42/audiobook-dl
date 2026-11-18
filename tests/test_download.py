from collections.abc import Sequence
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, DownloadOptions
from audiobookdl.exceptions import DownloadCancelled, DownloadError
from audiobookdl.output.download import (
    DOWNLOAD_ATTEMPTS,
    DOWNLOAD_CHUNK_SIZE,
    MAX_DOWNLOAD_WORKERS,
    download,
    download_audiobook,
    download_file,
    download_files,
)
from audiobookdl.utils.audiobook import AESCBCEncryption, Cover, TrackTagContext
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT


@pytest.fixture(autouse=True)
def _mock_probe_for_dummy_download_bytes(request, monkeypatch):
    """Stub ffprobe in dummy-byte download unit tests unless opted out."""
    if (
        request.node.get_closest_marker("real_integrity") is not None
        or request.node.get_closest_marker("no_probe_stub") is not None
    ):
        return
    from audiobookdl.output import output as output_mod
    from audiobookdl.utils import AudioProbeInfo

    monkeypatch.setattr(
        output_mod,
        "_probe_audio_info",
        lambda _path: AudioProbeInfo(duration=10.0, codec_name="mp3"),
    )


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


@pytest.mark.no_worker_cap
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
        paths_25_override = download_files(make_book(25), str(tmp_path), lambda _: None, max_workers=99)

    assert paths_4 == [f"p{i}.mp3" for i in range(4)]
    assert paths_25 == [f"p{i}.mp3" for i in range(25)]
    assert paths_25_override == [f"p{i}.mp3" for i in range(25)]
    from audiobookdl.output.download import DEFAULT_DOWNLOAD_WORKERS

    assert pool_sizes == [4, DEFAULT_DOWNLOAD_WORKERS, MAX_DOWNLOAD_WORKERS]
    assert DEFAULT_DOWNLOAD_WORKERS == 6
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

        def choose_index(
            self,
            title: str,
            choices: Sequence[str],
            default: int | None = None,
        ) -> int | None:
            return default

    existing = tmp_path / "Single Book.mp3"
    existing.write_bytes(b"original-audio")
    output_dir = str(tmp_path / "Single Book")
    options = DownloadOptions(
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


def test_download_file_midstream_cancel_cleans_tmp_and_raises(tmp_path):
    cancel_event = threading.Event()
    session = MagicMock()

    def chunk_stream():
        yield b"part-1"
        cancel_event.set()
        yield b"part-2"

    resp = MagicMock(status_code=200, headers={"Content-Length": "12"})
    resp.iter_content.return_value = chunk_stream()
    session.get.return_value = resp

    audiobook = _make_audiobook(session)
    output_dir = str(tmp_path / "cancelled_book")

    with pytest.raises(DownloadCancelled):
        download_file((audiobook, output_dir, 0, lambda _: None, cancel_event))

    assert not (tmp_path / "cancelled_book.mp3").exists()
    assert not (tmp_path / "cancelled_book.mp3.tmp").exists()
    resp.close.assert_called_once()


def test_download_file_raises_download_error_on_non_200_with_escaped_markup(tmp_path):
    session = MagicMock()
    resp = MagicMock(
        status_code=403,
        headers={},
        content=b"[red]Forbidden[/red]",
    )
    session.get.return_value = resp
    audiobook = _make_audiobook(session)

    with pytest.raises(DownloadError) as exc_info:
        download_file((audiobook, str(tmp_path / "err"), 0, lambda _: None, None))

    assert r"\[red]Forbidden\[/red]" in exc_info.value.data["body"]
    resp.close.assert_called_once()


def test_download_file_exhausts_retries_and_reraises_request_exception(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)
    session = MagicMock()
    session.get.side_effect = requests.exceptions.ConnectionError("reset")
    audiobook = _make_audiobook(session)

    with pytest.raises(requests.exceptions.ConnectionError):
        download_file((audiobook, str(tmp_path / "fail"), 0, lambda _: None, None))

    assert session.get.call_count == DOWNLOAD_ATTEMPTS


def test_download_file_resumes_partial_tmp_with_http_range_206(tmp_path):
    output_dir = str(tmp_path / "resumed_book")
    tmp_file = tmp_path / "resumed_book.mp3.tmp"
    tmp_file.write_bytes(b"hello-")

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={
            "Content-Length": "5",
            "Content-Range": "bytes 6-10/11",
            "Accept-Ranges": "bytes",
        },
        chunks=[b"world"],
        status_code=206,
    )
    audiobook = _make_audiobook(session)
    progress = []
    byte_counts = []

    out_path = download_file(
        (audiobook, output_dir, 0, progress.append, None, byte_counts.append),
        resume=True,
    )

    assert out_path == str(tmp_path / "resumed_book.mp3")
    with open(out_path, "rb") as f:
        assert f.read() == b"hello-world"
    assert not tmp_file.exists()
    session.get.assert_called_once_with(
        "https://example.com/part.mp3",
        headers={"Range": "bytes=6-"},
        stream=True,
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    assert sum(progress) == pytest.approx(1.0)
    assert byte_counts == [5]


def test_download_file_resume_handles_200_full_restart_and_416(tmp_path):
    # 1. Server ignores Range and replies 200 OK -> restarts cleanly without duplicating bytes
    output_dir = str(tmp_path / "no_range")
    (tmp_path / "no_range.mp3.tmp").write_bytes(b"stale-")
    session_200 = MagicMock()
    session_200.get.return_value = _mock_response(
        headers={"Content-Length": "9"},
        chunks=[b"freshdata"],
        status_code=200,
    )
    out_200 = download_file(
        (_make_audiobook(session_200), output_dir, 0, lambda _: None, None),
        resume=True,
    )
    assert open(out_200, "rb").read() == b"freshdata"

    # 2. Server replies 416 with matching Content-Range total -> promotes completed .tmp
    done_dir = str(tmp_path / "already_done")
    (tmp_path / "already_done.mp3.tmp").write_bytes(b"complete")
    session_416_done = MagicMock()
    session_416_done.get.return_value = _mock_response(
        headers={"Content-Range": "bytes */8"},
        chunks=[],
        status_code=416,
    )
    progress = []
    out_done = download_file(
        (_make_audiobook(session_416_done), done_dir, 0, progress.append, None),
        resume=True,
    )
    assert open(out_done, "rb").read() == b"complete"
    assert progress == [1.0]

    # 3. Server replies 416 with smaller total (stale oversized .tmp) -> discards .tmp and retries
    stale_dir = str(tmp_path / "stale_oversized")
    (tmp_path / "stale_oversized.mp3.tmp").write_bytes(b"oversized-stale-bytes")
    session_416_stale = MagicMock()
    session_416_stale.get.side_effect = [
        _mock_response(headers={"Content-Range": "bytes */4"}, chunks=[], status_code=416),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"good"], status_code=200),
    ]
    out_stale = download_file(
        (_make_audiobook(session_416_stale), stale_dir, 0, lambda _: None, None),
        resume=True,
    )
    assert open(out_stale, "rb").read() == b"good"
    assert session_416_stale.get.call_count == 2


def test_download_audiobook_multipart_resume_preserves_parts_on_cancel_and_resumes(tmp_path):
    book_dir = tmp_path / "Multi Book"
    options_resume = DownloadOptions(
        resume=True,
        combine=False,
        output_format=None,
        skip_downloaded=False,
        write_json_metadata=False,
        no_chapters=True,
        max_workers=1,
    )

    # First run: part 0 completes, part 1 writes partial bytes and cancels mid-stream
    cancel_ev = threading.Event()
    session1 = MagicMock()

    def get_run1(url, headers=None, stream=True, timeout=None):
        if url.endswith("/0.mp3"):
            return _mock_response({"Content-Length": "5"}, [b"part0"])
        if url.endswith("/1.mp3"):
            def stream_part1():
                yield b"par"
                cancel_ev.set()
                yield b"t1"
            resp = MagicMock(status_code=200, headers={"Content-Length": "5"})
            resp.iter_content.return_value = stream_part1()
            return resp
        return _mock_response({"Content-Length": "5"}, [b"part2"])

    session1.get.side_effect = get_run1
    book1 = Audiobook(
        session=session1,
        metadata=AudiobookMetadata(title="Multi Book"),
        files=[
            AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/2.mp3", ext="mp3"),
        ],
    )

    with pytest.raises(DownloadCancelled):
        download_audiobook(book1, str(book_dir), options_resume, cancel_event=cancel_ev)

    assert (book_dir / "Part 0.mp3").read_bytes() == b"part0"
    assert (book_dir / "Part 1.mp3.tmp").read_bytes() == b"par"

    # Second run with resume=True + skip_downloaded=True:
    # must NOT skip incomplete directory; skips part 0 HTTP, resumes part 1 via Range, downloads part 2
    session2 = MagicMock()
    requested_urls = []

    def get_run2(url, headers=None, stream=True, timeout=None):
        requested_urls.append((url, dict(headers or {})))
        if url.endswith("/1.mp3"):
            assert headers == {"Range": "bytes=3-"}
            return _mock_response(
                {"Content-Length": "2", "Content-Range": "bytes 3-4/5"},
                [b"t1"],
                status_code=206,
            )
        if url.endswith("/2.mp3"):
            return _mock_response({"Content-Length": "5"}, [b"part2"])
        raise AssertionError(f"Unexpected request for {url}")

    session2.get.side_effect = get_run2
    book2 = Audiobook(
        session=session2,
        metadata=AudiobookMetadata(title="Multi Book"),
        files=book1.files,
    )
    options_resume_skip = DownloadOptions(
        resume=True,
        skip_downloaded=True,
        combine=False,
        output_format=None,
        write_json_metadata=False,
        no_chapters=True,
    )

    with patch("audiobookdl.output.download.metadata.add_metadata"):
        download_audiobook(book2, str(book_dir), options_resume_skip)

    assert (book_dir / "Part 0.mp3").read_bytes() == b"part0"
    assert (book_dir / "Part 1.mp3").read_bytes() == b"part1"
    assert (book_dir / "Part 2.mp3").read_bytes() == b"part2"
    assert {url for url, _ in requested_urls} == {
        "https://example.com/1.mp3",
        "https://example.com/2.mp3",
    }

    # Third run with skip_downloaded=True & resume=True skips completely without HTTP calls
    session3 = MagicMock()
    book3 = Audiobook(session=session3, metadata=book2.metadata, files=book2.files)
    download_audiobook(book3, str(book_dir), options_resume_skip)
    session3.get.assert_not_called()


def test_download_file_retries_and_fails_on_truncated_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)

    # 1. Truncated first attempt retries and succeeds when second delivers full length
    session_recover = MagicMock()
    session_recover.get.side_effect = [
        _mock_response(headers={"Content-Length": "10"}, chunks=[b"1234"]),
        _mock_response(headers={"Content-Length": "10"}, chunks=[b"1234567890"]),
    ]
    progress = []
    out_path = download_file(
        (_make_audiobook(session_recover), str(tmp_path / "recover"), 0, progress.append, None)
    )
    assert open(out_path, "rb").read() == b"1234567890"
    assert session_recover.get.call_count == 2
    assert sum(progress) == pytest.approx(1.0)

    # 2. Persistent truncation exhausts retries and does NOT promote truncated .tmp
    session_trunc = MagicMock()
    session_trunc.get.side_effect = [
        _mock_response(headers={"Content-Length": "10"}, chunks=[b"1234"])
        for _ in range(DOWNLOAD_ATTEMPTS)
    ]
    with pytest.raises(requests.exceptions.ChunkedEncodingError, match="Incomplete download"):
        download_file((_make_audiobook(session_trunc), str(tmp_path / "trunc"), 0, lambda _: None, None))
    assert session_trunc.get.call_count == DOWNLOAD_ATTEMPTS
    assert not (tmp_path / "trunc.mp3").exists()


def test_download_file_allows_content_encoding_compressed_length_mismatch(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "20", "Content-Encoding": "gzip"},
        chunks=[b"short"],
    )
    out_path = download_file(
        (_make_audiobook(session), str(tmp_path / "gzipped"), 0, lambda _: None, None)
    )
    assert open(out_path, "rb").read() == b"short"


def test_download_file_encrypted_resume_preserves_ciphertext_across_decrypt_failure(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = b"0123456789abcdef" * 4 + b"xyz"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    enc = AESCBCEncryption(key=key, iv=iv)

    output_dir = str(tmp_path / "enc_book")
    tmp_file = tmp_path / "enc_book.mp3.tmp"
    final_file = tmp_path / "enc_book.mp3"

    # First run: download completes to .tmp, but os.replace fails during decryption output
    session1 = MagicMock()
    session1.get.return_value = _mock_response(
        headers={"Content-Length": str(len(ciphertext))},
        chunks=[ciphertext],
    )
    book1 = Audiobook(
        session=session1,
        metadata=AudiobookMetadata(title="Enc Book"),
        files=[AudiobookFile(url="https://example.com/enc.mp3", ext="mp3", encryption_method=enc)],
    )

    with patch("audiobookdl.output.encryption.os.replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError, match="disk error"):
            download_file((book1, output_dir, 0, lambda _: None, None), resume=True)

    assert not final_file.exists()
    assert tmp_file.read_bytes() == ciphertext

    # Second run with resume=True: server returns 416 matching complete ciphertext,
    # decrypts intact ciphertext once into final_file, and cleans up .tmp
    session2 = MagicMock()
    session2.get.return_value = _mock_response(
        headers={"Content-Range": f"bytes */{len(ciphertext)}"},
        chunks=[],
        status_code=416,
    )
    book2 = Audiobook(
        session=session2,
        metadata=book1.metadata,
        files=book1.files,
    )
    out_path = download_file((book2, output_dir, 0, lambda _: None, None), resume=True)
    assert out_path == str(final_file)
    assert final_file.read_bytes() == plaintext
    assert not tmp_file.exists()


def test_download_converts_keyboard_interrupt_to_download_cancelled(tmp_path):
    book = _make_audiobook(MagicMock())
    options = DownloadOptions(output_template=str(tmp_path / "{title}"))

    with patch("audiobookdl.output.download.download_audiobook", side_effect=KeyboardInterrupt):
        with pytest.raises(DownloadCancelled):
            download(book, options)


def test_single_file_conversion_prompts_once_on_target_even_when_intermediate_exists(tmp_path):
    class RecordingInteraction:
        def __init__(self, confirm_value: bool) -> None:
            self.confirm_value = confirm_value
            self.confirms: list[str] = []

        def ask_input(self, prompt: str, hidden: bool = False) -> str:
            return ""

        def confirm(self, message: str) -> bool:
            self.confirms.append(message)
            return self.confirm_value

        def choose_index(
            self,
            title: str,
            choices: Sequence[str],
            default: int | None = None,
        ) -> int | None:
            return default

    output_dir = str(tmp_path / "Converted Book")
    final_m4b = tmp_path / "Converted Book.m4b"
    intermediate_mp3 = tmp_path / "Converted Book.mp3"
    final_m4b.write_bytes(b"old-m4b")
    intermediate_mp3.write_bytes(b"stale-mp3")

    options = DownloadOptions(
        skip_downloaded=False,
        resume=False,
        combine=False,
        output_format="m4b",
        write_json_metadata=False,
        no_chapters=True,
    )

    # 1. Declining prompts once for the final .m4b target and makes no HTTP calls
    session_decline = MagicMock()
    decline = RecordingInteraction(confirm_value=False)
    with pytest.raises(DownloadCancelled):
        download_audiobook(_make_audiobook(session_decline), output_dir, options, interaction=decline)
    assert len(decline.confirms) == 1
    assert "Converted Book.m4b" in decline.confirms[0]
    assert final_m4b.read_bytes() == b"old-m4b"
    session_decline.get.assert_not_called()

    # 2. Accepting prompts ONLY once (never a second prompt for intermediate .mp3)
    session_accept = MagicMock()
    session_accept.get.return_value = _mock_response(
        headers={"Content-Length": "7"},
        chunks=[b"new-mp3"],
    )
    accept = RecordingInteraction(confirm_value=True)

    def fake_convert(src, target_format, cancel_event=None, audio_bitrate=None, chapters=None, **_kw):
        dst = f"{src.rsplit('.', 1)[0]}.{target_format}"
        with open(dst, "wb") as f:
            f.write(b"new-m4b")
        return dst, False

    with (
        patch("audiobookdl.output.download.output.convert_single_file", side_effect=fake_convert),
        patch("audiobookdl.output.download.metadata.add_metadata"),
    ):
        download_audiobook(_make_audiobook(session_accept), output_dir, options, interaction=accept)

    assert len(accept.confirms) == 1
    assert "Converted Book.m4b" in accept.confirms[0]
    assert final_m4b.read_bytes() == b"new-m4b"

    # 3. When only intermediate .mp3 exists and final .m4b does not, no overwrite prompt is shown
    final_m4b.unlink()
    intermediate_mp3.write_bytes(b"stale-mp3")
    session_no_target = MagicMock()
    session_no_target.get.return_value = _mock_response(
        headers={"Content-Length": "7"},
        chunks=[b"new-mp3"],
    )
    only_intermediate = RecordingInteraction(confirm_value=False)
    with (
        patch("audiobookdl.output.download.output.convert_single_file", side_effect=fake_convert),
        patch("audiobookdl.output.download.metadata.add_metadata"),
    ):
        download_audiobook(
            _make_audiobook(session_no_target),
            output_dir,
            options,
            interaction=only_intermediate,
        )
    assert only_intermediate.confirms == []
    assert final_m4b.read_bytes() == b"new-m4b"


def test_single_file_conversion_resume_honors_existing_target_and_intermediate(tmp_path):
    class FailingInteraction:
        def ask_input(self, prompt: str, hidden: bool = False) -> str:
            raise AssertionError("Should not prompt on resume")

        def confirm(self, message: str) -> bool:
            raise AssertionError(f"Should not prompt on resume: {message}")

        def choose_index(
            self,
            title: str,
            choices: Sequence[str],
            default: int | None = None,
        ) -> int | None:
            raise AssertionError(f"Should not prompt on resume: {title}")

    output_dir = str(tmp_path / "Resumed Convert")
    final_m4b = tmp_path / "Resumed Convert.m4b"
    intermediate_mp3 = tmp_path / "Resumed Convert.mp3"

    options_resume = DownloadOptions(
        skip_downloaded=False,
        resume=True,
        combine=False,
        output_format="m4b",
        write_json_metadata=False,
        no_chapters=True,
    )

    # 1. Final .m4b already exists (non-empty) and intermediate .mp3 was deleted after conversion:
    #    resume=True without skip_downloaded must NOT prompt, re-download, or reconvert.
    final_m4b.write_bytes(b"existing-m4b")
    session_done = MagicMock()
    with (
        patch("audiobookdl.output.download.output.convert_single_file") as mock_convert,
        patch("audiobookdl.output.download.metadata.add_metadata") as mock_meta,
    ):
        download_audiobook(
            _make_audiobook(session_done),
            output_dir,
            options_resume,
            interaction=FailingInteraction(),
        )
    session_done.get.assert_not_called()
    mock_convert.assert_not_called()
    mock_meta.assert_not_called()
    assert final_m4b.read_bytes() == b"existing-m4b"

    # 2. Final .m4b does not exist, intermediate .mp3 already finished downloading:
    #    resume=True skips HTTP download and converts existing .mp3 -> .m4b without prompts.
    final_m4b.unlink()
    intermediate_mp3.write_bytes(b"cached-mp3")
    session_cached_mp3 = MagicMock()

    def fake_convert(src, target_format, cancel_event=None, audio_bitrate=None, chapters=None, **_kw):
        assert src == str(intermediate_mp3)
        dst = str(final_m4b)
        final_m4b.write_bytes(b"converted-from-cached")
        intermediate_mp3.unlink()
        return dst, False

    with (
        patch("audiobookdl.output.download.output.convert_single_file", side_effect=fake_convert) as mock_convert,
        patch("audiobookdl.output.download.metadata.add_metadata") as mock_meta,
    ):
        download_audiobook(
            _make_audiobook(session_cached_mp3),
            output_dir,
            options_resume,
            interaction=FailingInteraction(),
        )
    session_cached_mp3.get.assert_not_called()
    mock_convert.assert_called_once()
    mock_meta.assert_called_once()
    assert final_m4b.read_bytes() == b"converted-from-cached"
    assert not intermediate_mp3.exists()

    # 3. Empty (0-byte) final .m4b is NOT treated as complete under resume=True
    final_m4b.write_bytes(b"")
    session_empty = MagicMock()
    session_empty.get.return_value = _mock_response(
        headers={"Content-Length": "9"},
        chunks=[b"fresh-mp3"],
    )

    def fake_convert_fresh(src, target_format, cancel_event=None, audio_bitrate=None, chapters=None, **_kw):
        final_m4b.write_bytes(b"rebuilt-m4b")
        return str(final_m4b), False

    with (
        patch("audiobookdl.output.download.output.convert_single_file", side_effect=fake_convert_fresh),
        patch("audiobookdl.output.download.metadata.add_metadata"),
    ):
        download_audiobook(
            _make_audiobook(session_empty),
            output_dir,
            options_resume,
            interaction=FailingInteraction(),
        )
    session_empty.get.assert_called_once()
    assert final_m4b.read_bytes() == b"rebuilt-m4b"


def test_combined_multifile_overwrite_prompts_once_and_resume_skips_completed(tmp_path):
    class RecordingInteraction:
        def __init__(self, confirm_value: bool) -> None:
            self.confirm_value = confirm_value
            self.confirms: list[str] = []

        def ask_input(self, prompt: str, hidden: bool = False) -> str:
            return ""

        def confirm(self, message: str) -> bool:
            self.confirms.append(message)
            return self.confirm_value

        def choose_index(
            self,
            title: str,
            choices: Sequence[str],
            default: int | None = None,
        ) -> int | None:
            return default

    book_dir = tmp_path / "Combined Book"
    book_dir.mkdir()
    (book_dir / "Part 0.mp3").write_bytes(b"old0")
    final_m4b = tmp_path / "Combined Book.m4b"
    final_m4b.write_bytes(b"old-combined")

    files = [
        AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
        AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
    ]

    # 1. Resume=True skips when Combined Book.m4b already exists
    session_resume = MagicMock()
    book_resume = Audiobook(session=session_resume, metadata=AudiobookMetadata(title="Combined Book"), files=files)
    opts_resume = DownloadOptions(
        skip_downloaded=False,
        resume=True,
        combine=True,
        output_format="m4b",
        write_json_metadata=False,
        no_chapters=True,
    )
    interaction_none = RecordingInteraction(confirm_value=False)
    download_audiobook(book_resume, str(book_dir), opts_resume, interaction=interaction_none)
    assert interaction_none.confirms == []
    session_resume.get.assert_not_called()

    # 2. Resume=False prompts only once for Combined Book.m4b even when intermediate dir exists
    session_overwrite = MagicMock()
    session_overwrite.get.side_effect = [
        _mock_response({"Content-Length": "4"}, [b"new0"]),
        _mock_response({"Content-Length": "4"}, [b"new1"]),
    ]
    book_overwrite = Audiobook(
        session=session_overwrite,
        metadata=AudiobookMetadata(title="Combined Book"),
        files=files,
    )
    opts_overwrite = DownloadOptions(
        skip_downloaded=False,
        resume=False,
        combine=True,
        output_format="m4b",
        write_json_metadata=False,
        no_chapters=True,
    )
    accept = RecordingInteraction(confirm_value=True)

    def fake_combine(filepaths, tmp_dir, output_path, cancel_event=None, chapters=None, audio_bitrate=None, **_kw):
        with open(output_path, "wb") as f:
            f.write(b"new-combined-m4b")
        return False

    with (
        patch("audiobookdl.output.download.output.combine_audiofiles", side_effect=fake_combine),
        patch("audiobookdl.output.download.metadata.add_metadata"),
    ):
        download_audiobook(book_overwrite, str(book_dir), opts_overwrite, interaction=accept)

    assert len(accept.confirms) == 1
    assert "Combined Book.m4b" in accept.confirms[0]
    assert final_m4b.read_bytes() == b"new-combined-m4b"


def test_write_json_metadata_uses_utf8_and_preserves_unicode(tmp_path):
    import json
    from audiobookdl.output.download import add_metadata_to_dir, add_metadata_to_file

    title = "日本語・Книга — Test"
    book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title=title, authors=["Алексей"]),
        files=[],
    )
    opts = DownloadOptions(write_json_metadata=True, no_chapters=True)

    single_file = tmp_path / "book.mp3"
    single_file.write_bytes(b"audio")
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        add_metadata_to_file(book, str(single_file), opts)
    raw_single = (tmp_path / "book.mp3.json").read_text(encoding="utf-8")
    assert title in raw_single
    assert json.loads(raw_single)["title"] == title

    book_dir = tmp_path / "multi"
    book_dir.mkdir()
    part_file = book_dir / "Part 0.mp3"
    part_file.write_bytes(b"audio")
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        add_metadata_to_dir(book, [str(part_file)], str(book_dir), opts)
    raw_dir = (book_dir / "metadata.json").read_text(encoding="utf-8")
    assert title in raw_dir
    assert json.loads(raw_dir)["title"] == title


def test_download_file_streams_inline_aes_cbc_without_resume(tmp_path):
    from audiobookdl.utils.audiobook import AESCBCEncryption

    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = b"0123456789abcdef" * 100 + b"tail-bytes"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    # Split ciphertext across odd chunk boundaries (not aligned to 16-byte AES blocks)
    chunks = [ciphertext[i : i + 17] for i in range(0, len(ciphertext), 17)]

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(ciphertext))},
        chunks=chunks,
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="AES Stream"),
        files=[AudiobookFile(url="https://example.com/enc.ts", ext="ts", encryption_method=AESCBCEncryption(key=key, iv=iv))],
    )
    progress = []
    with patch("audiobookdl.output.download.encryption.decrypt_file") as mock_decrypt_file:
        out_path = download_file((book, str(tmp_path / "aes"), 0, progress.append, None))

    mock_decrypt_file.assert_not_called()
    assert open(out_path, "rb").read() == plaintext
    assert sum(progress) == pytest.approx(1.0)


def test_download_file_batches_large_progress_callbacks(tmp_path):
    chunk = b"x" * 30_000
    total = len(chunk) * 20
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(total)},
        chunks=[chunk] * 20,
    )
    audiobook = _make_audiobook(session)
    progress = []

    with patch("audiobookdl.output.download.time.monotonic", return_value=100.0):
        out_path = download_file((audiobook, str(tmp_path / "batched"), 0, progress.append, None))

    assert len(progress) < 20
    assert sum(progress) == pytest.approx(1.0)
    assert len(open(out_path, "rb").read()) == total


def test_overwrite_flag_bypasses_interactive_prompts(tmp_path):
    target = tmp_path / "Existing Book.mp3"
    target.write_bytes(b"old")

    class FailingInteraction:
        def ask_input(self, prompt: str, hidden: bool = False) -> str:
            raise AssertionError("Unexpected prompt")

        def confirm(self, message: str) -> bool:
            raise AssertionError("Unexpected confirm prompt")

        def choose_index(
            self,
            title: str,
            choices: Sequence[str],
            default: int | None = None,
        ) -> int | None:
            raise AssertionError("Unexpected choice prompt")

    # overwrite=False cancels immediately without prompting
    opts_no = DownloadOptions(overwrite=False, skip_downloaded=False, resume=False, write_json_metadata=False, no_chapters=True)
    with pytest.raises(DownloadCancelled):
        download_audiobook(_make_audiobook(MagicMock()), str(tmp_path / "Existing Book"), opts_no, interaction=FailingInteraction())
    assert target.read_bytes() == b"old"

    # overwrite=True replaces without prompting
    session = MagicMock()
    session.get.return_value = _mock_response({"Content-Length": "3"}, [b"new"])
    opts_yes = DownloadOptions(overwrite=True, skip_downloaded=False, resume=False, write_json_metadata=False, no_chapters=True)
    with patch("audiobookdl.output.download.metadata.add_metadata"):
        download_audiobook(_make_audiobook(session), str(tmp_path / "Existing Book"), opts_yes, interaction=FailingInteraction())
    assert target.read_bytes() == b"new"


def test_download_file_resolves_lazy_url_resolver(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "4"},
        chunks=[b"lazy"],
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Lazy URL"),
        files=[
            AudiobookFile(
                url="https://example.com/placeholder",
                ext="mp3",
                url_resolver=lambda: "https://cdn.example.com/fresh-signed.mp3",
            )
        ],
    )
    out_path = download_file((book, str(tmp_path / "lazy"), 0, lambda _: None, None))
    assert open(out_path, "rb").read() == b"lazy"
    assert session.get.call_args.args[0] == "https://cdn.example.com/fresh-signed.mp3"


def test_download_ebook_preserves_epub_skips_audio_conversion_and_writes_sidecars(tmp_path):
    session = MagicMock()
    epub_bytes = b"PK\x03\x04fake-epub-archive"
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(epub_bytes))},
        chunks=[epub_bytes],
    )
    meta = AudiobookMetadata(title="My Ebook")
    meta.add_author("Ebook Author")
    meta.add_genre("Ebook")
    book = Audiobook(
        session=session,
        metadata=meta,
        files=[AudiobookFile(url="https://cdn.example.com/book.epub", ext="epub")],
        cover=Cover(b"\xff\xd8\xffcover", "jpg"),
    )
    output_dir = str(tmp_path / "My Ebook")
    opts = DownloadOptions(
        output_format="m4b",
        combine=True,
        write_cover=True,
        write_opf=True,
        write_json_metadata=True,
        write_cue=True,
        skip_downloaded=True,
    )

    with (
        patch("audiobookdl.output.download.output.convert_single_file") as mock_convert,
        patch("audiobookdl.output.download.output.combine_audiofiles") as mock_combine,
        patch("audiobookdl.output.download.metadata.add_metadata") as mock_add_meta,
        patch("audiobookdl.output.download.metadata.embed_cover") as mock_embed_cover,
    ):
        download_audiobook(book, output_dir, opts)

    mock_convert.assert_not_called()
    mock_combine.assert_not_called()
    mock_add_meta.assert_not_called()
    mock_embed_cover.assert_not_called()

    epub_file = tmp_path / "My Ebook.epub"
    assert epub_file.exists()
    assert epub_file.read_bytes() == epub_bytes
    assert (tmp_path / "My Ebook.jpg").read_bytes() == b"\xff\xd8\xffcover"
    assert (tmp_path / "My Ebook.opf").exists()
    assert (tmp_path / "My Ebook.epub.json").exists()
    assert not (tmp_path / "My Ebook.cue").exists()

    # Second run with skip_downloaded=True skips re-downloading existing .epub
    session.get.reset_mock()
    download_audiobook(book, output_dir, opts)
    session.get.assert_not_called()


def test_download_file_retries_on_429_and_5xx_with_retry_after(tmp_path, monkeypatch):
    from audiobookdl.exceptions import DownloadError
    from audiobookdl.output.download import DOWNLOAD_ATTEMPTS, download_file

    resp_429 = _mock_response(status_code=429, headers={"Retry-After": "3"}, chunks=[])
    resp_503 = _mock_response(status_code=503, headers={}, chunks=[])
    resp_ok = _mock_response(status_code=200, headers={"Content-Length": "4"}, chunks=[b"data"])
    session = MagicMock()
    session.get.side_effect = [resp_429, resp_503, resp_ok]

    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Retry Book"),
        files=[AudiobookFile(url="https://cdn.example.com/part.mp3", ext="mp3", expected_status_code=200)],
    )
    delays: list[float] = []
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", delays.append)

    out_file = download_file((book, str(tmp_path / "retry"), 0, lambda _d: None, None))
    assert out_file.endswith(".mp3")
    assert session.get.call_count == 3
    assert resp_429.close.call_count == 1
    assert resp_503.close.call_count == 1
    assert delays[0] == pytest.approx(3.0)
    assert delays[1] >= 2.0

    # Exhausting retries on persistent 502 raises DownloadError
    fail_session = MagicMock()
    fail_resps = []
    for _ in range(DOWNLOAD_ATTEMPTS):
        r = _mock_response(status_code=502, headers={}, chunks=[])
        r.content = b"Bad Gateway"
        fail_resps.append(r)
    fail_session.get.side_effect = fail_resps
    fail_book = Audiobook(
        session=fail_session,
        metadata=AudiobookMetadata(title="Fail Book"),
        files=[AudiobookFile(url="https://cdn.example.com/fail.mp3", ext="mp3", expected_status_code=200)],
    )
    with pytest.raises(DownloadError):
        download_file((fail_book, str(tmp_path / "fail"), 0, lambda _d: None, None))
    assert fail_session.get.call_count == DOWNLOAD_ATTEMPTS


def test_download_audiobook_atomic_staging_preserves_existing_dir_on_cancel_or_error(tmp_path):
    from unittest.mock import patch as _patch

    book_dir = tmp_path / "Atomic Book"
    book_dir.mkdir()
    existing_part = book_dir / "Part 0.mp3"
    existing_part.write_bytes(b"original-part-0")

    cancel_event = threading.Event()
    session_fail = MagicMock()
    calls = 0

    def _side_effect_get(url, **_kwargs):
        nonlocal calls
        calls += 1
        if url.endswith("/0.mp3"):
            return _mock_response(headers={"Content-Length": "5"}, chunks=[b"new-0"])
        cancel_event.set()
        return _mock_response(headers={"Content-Length": "5"}, chunks=[b"new-1"])

    session_fail.get.side_effect = _side_effect_get
    book = Audiobook(
        session=session_fail,
        metadata=AudiobookMetadata(title="Atomic Book"),
        files=[
            AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
        ],
    )
    opts = DownloadOptions(skip_downloaded=False, resume=False, overwrite=True, no_chapters=True)

    with pytest.raises(DownloadCancelled):
        download_audiobook(book, str(book_dir), opts, cancel_event=cancel_event)

    # Pre-existing directory and contents must survive intact when download cancels mid-batch
    assert existing_part.exists()
    assert existing_part.read_bytes() == b"original-part-0"
    # Temporary staging directories must be cleaned up
    assert [p.name for p in tmp_path.iterdir()] == ["Atomic Book"]

    # Now succeed cleanly and verify atomic swap updates contents
    session_ok = MagicMock()
    session_ok.get.side_effect = [
        _mock_response(headers={"Content-Length": "5"}, chunks=[b"new-0"]),
        _mock_response(headers={"Content-Length": "5"}, chunks=[b"new-1"]),
    ]
    book_ok = Audiobook(
        session=session_ok,
        metadata=AudiobookMetadata(title="Atomic Book"),
        files=[
            AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
        ],
    )
    with _patch("audiobookdl.output.download.metadata.add_metadata"):
        download_audiobook(book_ok, str(book_dir), opts)
    assert existing_part.read_bytes() == b"new-0"
    assert (book_dir / "Part 1.mp3").read_bytes() == b"new-1"
    assert [p.name for p in tmp_path.iterdir()] == ["Atomic Book"]


@pytest.mark.no_probe_stub
def test_download_file_rejects_corrupt_audio_before_replacing_existing_file(tmp_path):
    from audiobookdl.exceptions import GenericAudiobookDLException
    from audiobookdl.output import output as output_mod
    from audiobookdl.utils import AudioProbeInfo

    target = tmp_path / "Existing.mp3"
    target.write_bytes(b"valid-existing-audio")

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "26"},
        chunks=[b"<html>403 Forbidden</html>"],
    )
    book = _make_audiobook(session)

    with (
        patch("audiobookdl.output.output.find_executable", return_value="ffprobe"),
        patch.object(output_mod, "_probe_audio_info", return_value=AudioProbeInfo(duration=0.0, codec_name="")),
    ):
        with pytest.raises(GenericAudiobookDLException):
            download_file((book, str(tmp_path / "Existing"), 0, lambda _d: None, None))

    # Original file is preserved untouched and corrupt .tmp file is cleaned up
    assert target.read_bytes() == b"valid-existing-audio"
    assert not (tmp_path / "Existing.mp3.tmp").exists()


@pytest.mark.no_probe_stub
def test_download_file_surfaces_ffprobe_execution_error_without_false_integrity_message(tmp_path):
    from audiobookdl.exceptions import GenericAudiobookDLException

    target = tmp_path / "Existing.mp3"
    target.write_bytes(b"valid-existing-audio")

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": "11"},
        chunks=[b"valid-bytes"],
    )
    book = _make_audiobook(session)

    with (
        patch("audiobookdl.utils.find_executable", return_value="C:/bad/ffprobe.exe"),
        patch(
            "audiobookdl.utils.subprocess.run",
            side_effect=OSError(216, "This version of %1 is not compatible"),
        ),
    ):
        with pytest.raises(GenericAudiobookDLException) as exc_info:
            download_file((book, str(tmp_path / "Existing"), 0, lambda _d: None, None))

    heading = exc_info.value.data["heading"]
    body = exc_info.value.data["body"]
    assert "ffprobe could not run" in heading
    assert "not compatible" in body
    assert "failed media integrity verification" not in heading
    assert target.read_bytes() == b"valid-existing-audio"


def _generate_real_mp3(tmp_path: Path, name: str = "valid.mp3") -> Path:
    import shutil
    import subprocess

    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        pytest.skip("ffmpeg/ffprobe unavailable for real_integrity test")
    assert ffmpeg_bin is not None
    out = tmp_path / name
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-t",
            "1.0",
            "-threads",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    return out


@pytest.mark.real_integrity
@pytest.mark.ffmpeg
def test_real_ffprobe_integrity_accepts_valid_mp3_and_rejects_corrupt_bytes(tmp_path):
    from audiobookdl.exceptions import GenericAudiobookDLException

    valid_bytes = _generate_real_mp3(tmp_path, "source.mp3").read_bytes()
    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(valid_bytes)), "Content-Type": "audio/mpeg"},
        chunks=[valid_bytes],
    )
    book = _make_audiobook(session)
    target = tmp_path / "Book.mp3"

    committed = download_file((book, str(tmp_path / "Book"), 0, lambda _d: None, None))
    assert committed == str(target)
    assert target.read_bytes() == valid_bytes

    corrupt = b"<html>403 Forbidden - not an audio stream</html>"
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(corrupt)), "Content-Type": "audio/mpeg"},
        chunks=[corrupt],
    )
    with pytest.raises(GenericAudiobookDLException) as exc_info:
        download_file((book, str(tmp_path / "Book"), 0, lambda _d: None, None))

    assert "failed media integrity verification" in exc_info.value.data["heading"]
    assert target.read_bytes() == valid_bytes
    assert not (tmp_path / "Book.mp3.tmp").exists()


@pytest.mark.real_integrity
@pytest.mark.ffmpeg
def test_real_ffprobe_integrity_rejects_truncated_audio(tmp_path):
    from audiobookdl.exceptions import FailedCombining
    from audiobookdl.output import output as output_mod

    valid_mp3 = _generate_real_mp3(tmp_path, "short.mp3")
    info = output_mod.verify_audio_integrity(str(valid_mp3), require_ffprobe=True)
    assert info.codec_name == "mp3"
    assert 0.5 <= info.duration <= 2.0

    with pytest.raises(FailedCombining, match="truncated"):
        output_mod.verify_audio_integrity(
            str(valid_mp3),
            expected_duration=10.0,
            require_ffprobe=True,
        )


def test_download_file_streams_inline_aes_on_multi_segment_hls_even_with_resume(tmp_path):
    from audiobookdl.utils.audiobook import AESCBCEncryption

    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = b"0123456789abcdef" * 20 + b"hls-seg-tail"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(ciphertext))},
        chunks=[ciphertext],
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Multi HLS"),
        files=[
            AudiobookFile(url="https://example.com/seg0.ts", ext="ts", encryption_method=AESCBCEncryption(key=key, iv=iv)),
            AudiobookFile(url="https://example.com/seg1.ts", ext="ts", encryption_method=AESCBCEncryption(key=key, iv=iv)),
        ],
    )
    out_dir = tmp_path / "hls_dir"
    out_dir.mkdir()
    with patch("audiobookdl.output.download.encryption.decrypt_file") as mock_decrypt_file:
        out_path = download_file((book, str(out_dir), 0, lambda _: None, None), resume=True)

    mock_decrypt_file.assert_not_called()
    assert open(out_path, "rb").read() == plaintext


def test_download_file_streams_inline_aes_when_accept_ranges_none_with_resume(tmp_path):
    from audiobookdl.utils.audiobook import AESCBCEncryption

    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = b"0123456789abcdef" * 8 + b"small-file"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    session = MagicMock()
    session.get.return_value = _mock_response(
        headers={"Content-Length": str(len(ciphertext)), "Accept-Ranges": "none"},
        chunks=[ciphertext],
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Single Resume Small"),
        files=[AudiobookFile(url="https://example.com/single.ts", ext="ts", encryption_method=AESCBCEncryption(key=key, iv=iv))],
    )
    out_dir = tmp_path / "single_small_dir"
    out_dir.mkdir()
    with patch("audiobookdl.output.download.encryption.decrypt_file") as mock_decrypt_file:
        out_path = download_file((book, str(out_dir), 0, lambda _: None, None), resume=True)

    mock_decrypt_file.assert_not_called()
    assert open(out_path, "rb").read() == plaintext


def test_download_audiobook_enables_faststart_on_combine_and_convert(tmp_path):
    session = MagicMock()
    session.get.return_value = _mock_response({"Content-Length": "3"}, [b"raw"])
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Faststart Book"),
        files=[AudiobookFile(url="https://example.com/p1.mp3", ext="mp3")],
    )
    opts = DownloadOptions(
        output_format="m4b",
        combine=True,
        write_json_metadata=False,
        no_chapters=True,
        overwrite=True,
        skip_downloaded=False,
    )
    with (
        patch("audiobookdl.output.download.output.convert_single_file", return_value=(str(tmp_path / "Faststart Book.m4b"), False)) as mock_conv,
        patch("audiobookdl.output.download.metadata.add_metadata"),
    ):
        (tmp_path / "Faststart Book.m4b").write_bytes(b"m4b")
        download_audiobook(book, str(tmp_path / "Faststart Book"), opts)
    assert mock_conv.call_args.kwargs["faststart"] is True


def test_parse_byte_rate_and_token_bucket_rate_limiter():
    from audiobookdl.utils.http import TokenBucketRateLimiter, parse_byte_rate

    assert parse_byte_rate(None) is None
    assert parse_byte_rate("") is None
    assert parse_byte_rate("500K") == 500 * 1024
    assert parse_byte_rate("2M/s") == 2 * 1024 * 1024
    assert parse_byte_rate("1048576") == 1048576
    with pytest.raises(ValueError):
        parse_byte_rate("invalid-rate")

    limiter = TokenBucketRateLimiter(128 * 1024)
    cancel = threading.Event()
    limiter.acquire(1024, cancel_event=cancel)
    cancel.set()
    with pytest.raises(DownloadCancelled):
        limiter.acquire(1024, cancel_event=cancel)


def test_download_file_handles_custom_byterange_206_and_init_section(tmp_path):
    from audiobookdl.utils.audiobook import AESCBCEncryption

    init_bytes = b"INIT_HEADER_"
    seg_payload = b"SEGMENT_AUDIO_FRAMES"
    session = MagicMock()
    session.get.return_value = _mock_response(
        status_code=206,
        headers={"Content-Length": str(len(seg_payload))},
        chunks=[seg_payload],
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="fMP4 HLS"),
        files=[
            AudiobookFile(
                url="https://cdn.example.com/book.m4s",
                ext="m4s",
                headers={"Range": "bytes=720-739"},
                init_section=init_bytes,
            )
        ],
    )
    out_dir = tmp_path / "fmp4_unenc"
    out_dir.mkdir()
    out_path = download_file((book, str(out_dir), 0, lambda _: None, None), resume=True)
    assert open(out_path, "rb").read() == init_bytes + seg_payload
    assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=720-739"

    # Encrypted fMP4 segment also prepends unencrypted init_section before decrypted payload
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(seg_payload, AES.block_size))
    session.get.return_value = _mock_response(
        status_code=206,
        headers={"Content-Length": str(len(ciphertext))},
        chunks=[ciphertext],
    )
    enc_book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="fMP4 HLS Enc"),
        files=[
            AudiobookFile(
                url="https://cdn.example.com/enc.m4s",
                ext="m4s",
                headers={"Range": "bytes=100-131"},
                encryption_method=AESCBCEncryption(key=key, iv=iv),
                init_section=init_bytes,
            )
        ],
    )
    out_enc_dir = tmp_path / "fmp4_enc"
    out_enc_dir.mkdir()
    enc_out_path = download_file((enc_book, str(out_enc_dir), 0, lambda _: None, None), resume=False)
    assert open(enc_out_path, "rb").read() == init_bytes + seg_payload



def test_aes_cbc_multipart_resume_does_not_mix_plaintext_and_ciphertext(tmp_path):
    key = b"0123456789abcdef"
    iv = b"abcdef9876543210"
    plaintext = b"Chapter 1 secret audio stream payload!!"
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16))
    enc = AESCBCEncryption(key=key, iv=iv)

    part1 = AudiobookFile(url="https://example.com/p1.mp3", ext="mp3", encryption_method=enc)
    part2 = AudiobookFile(url="https://example.com/p2.mp3", ext="mp3", encryption_method=enc)

    class _FakeResp:
        def __init__(self, payload: bytes, headers: dict[str, str]):
            self.status_code = 200
            self.headers = headers
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=1):
            for i in range(0, len(self._payload), chunk_size):
                yield self._payload[i : i + chunk_size]

        def close(self):
            return None

    seen_headers = []

    def fake_get(url, stream=True, headers=None, timeout=None):
        seen_headers.append(dict(headers or {}))
        return _FakeResp(
            ciphertext,
            {
                "Content-Length": str(len(ciphertext)),
                "Accept-Ranges": "bytes",
            },
        )

    session = MagicMock()
    session.get.side_effect = fake_get
    audiobook = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="AES Resume Book"),
        files=[part1, part2],
    )
    out_dir = tmp_path / "book_dir"
    out_dir.mkdir()
    out_path = out_dir / "Part 0.mp3"
    # Pre-seed stale plaintext partial .tmp file from an interrupted non-resume run
    tmp_partial = out_dir / "Part 0.mp3.tmp"
    tmp_partial.write_bytes(b"stale-plaintext-prefix")

    downloaded = download_file(
        (audiobook, str(out_dir), 0, lambda _: None, None),
        resume=True,
    )

    assert downloaded == str(out_path)
    assert out_path.read_bytes() == plaintext
    assert not tmp_partial.exists()
    assert not list(out_dir.glob("*.inline.*.tmp"))
    assert all("Range" not in h for h in seen_headers)


def test_download_audiobook_forwards_loudnorm_mono_and_silence_split(tmp_path, monkeypatch):
    from audiobookdl import Chapter
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    src_mp3 = tmp_path / "book.mp3"
    src_mp3.write_bytes(b"fake-mp3")

    monkeypatch.setattr(dl_mod, "download_files_with_progress", lambda *a, **kw: [str(src_mp3)])
    monkeypatch.setattr(dl_mod, "add_metadata_to_file", lambda *a, **kw: None)
    monkeypatch.setattr(dl_mod, "add_metadata_to_dir", lambda *a, **kw: None)
    monkeypatch.setattr(
        out_mod,
        "detect_silence_chapters",
        lambda *a, **kw: [Chapter(0, "Chapter 1"), Chapter(10000, "Chapter 2")],
    )

    split_calls = []
    out_dir = tmp_path / "out_dir"

    def fake_split(master_path, output_dir, chapters, **kwargs):
        split_calls.append(kwargs)
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        p1 = target_dir / "01 - Chapter 1.mp3"
        p2 = target_dir / "02 - Chapter 2.mp3"
        p1.write_bytes(b"c1")
        p2.write_bytes(b"c2")
        return [str(p1), str(p2)]

    monkeypatch.setattr(out_mod, "split_audio_by_chapters", fake_split)

    session = MagicMock()
    audiobook = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Silence Split Book"),
        files=[AudiobookFile(url="https://example.com/single.mp3", ext="mp3")],
    )
    opts = DownloadOptions(
        split_chapters=True,
        chapters_from_silence=True,
        loudnorm=True,
        mono=True,
    )
    download_audiobook(audiobook, str(out_dir), opts)

    assert len(split_calls) == 1
    assert split_calls[0]["loudnorm"] is True
    assert split_calls[0]["mono"] is True
    assert (out_dir / "01 - Chapter 1.mp3").exists()


def test_download_audiobook_combine_silence_chapters_embeds_cover_after_combine(tmp_path, monkeypatch):
    from audiobookdl import Chapter
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    p1 = tmp_path / "p1.mp3"
    p2 = tmp_path / "p2.mp3"
    p1.write_bytes(b"a")
    p2.write_bytes(b"b")

    monkeypatch.setattr(dl_mod, "download_files_with_progress", lambda *a, **kw: [str(p1), str(p2)])
    monkeypatch.setattr(
        out_mod,
        "combine_audiofiles",
        lambda files, tmp, out, **kwargs: (open(out, "wb").write(b"m4b") and False),
    )
    monkeypatch.setattr(
        out_mod,
        "detect_silence_chapters",
        lambda *a, **kw: [Chapter(0, "Chapter 1"), Chapter(5000, "Chapter 2")],
    )
    add_meta_calls = []
    monkeypatch.setattr(
        dl_mod,
        "add_metadata_to_file",
        lambda ab, fp, opts, **kwargs: add_meta_calls.append(kwargs),
    )

    session = MagicMock()
    audiobook = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Combined Silence"),
        files=[
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/2.mp3", ext="mp3"),
        ],
        cover=Cover(b"img", "jpg"),
    )
    opts = DownloadOptions(
        combine=True,
        output_format="m4b",
        chapters_from_silence=True,
    )
    download_audiobook(audiobook, str(tmp_path / "out"), opts)

    assert len(add_meta_calls) == 1
    assert add_meta_calls[0]["cover_embedded"] is False


def test_add_metadata_to_dir_passes_track_context_and_cover(tmp_path, monkeypatch):
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import metadata as meta_mod

    f1 = tmp_path / "01 - Opening.mp3"
    f2 = tmp_path / "02 - Closing.mp3"
    f1.write_bytes(b"1")
    f2.write_bytes(b"2")

    calls = []

    def fake_add_metadata(
        filepath: str,
        meta: AudiobookMetadata,
        cover: Cover | None = None,
        track_context: TrackTagContext | None = None,
    ) -> bool:
        calls.append((filepath, meta, cover, track_context))
        return True

    monkeypatch.setattr(meta_mod, "add_metadata", fake_add_metadata)
    cover = Cover(b"cover-bytes", "jpg")
    ab = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Dir Book", authors=["Author"]),
        files=[AudiobookFile(url="u1", ext="mp3"), AudiobookFile(url="u2", ext="mp3")],
        cover=cover,
    )
    dl_mod.add_metadata_to_dir(
        ab,
        [str(f1), str(f2)],
        str(tmp_path),
        DownloadOptions(),
    )

    assert len(calls) == 2
    assert calls[0][2] is cover
    ctx0 = calls[0][3]
    ctx1 = calls[1][3]
    assert ctx0 is not None
    assert ctx1 is not None
    assert ctx0.track_number == 1
    assert ctx0.total_tracks == 2
    assert ctx0.album == "Dir Book"
    assert ctx1.track_number == 2
    assert ctx1.total_tracks == 2


def test_download_reuses_probe_cache_from_download_file_for_combine(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod
    from audiobookdl.utils import AudioProbeInfo

    probed_paths: list[str] = []

    def counting_probe(path: str) -> AudioProbeInfo:
        probed_paths.append(path)
        dur = 25.0 if ".tmp." in os.path.basename(path) else 12.5
        return AudioProbeInfo(duration=dur, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2)

    monkeypatch.setattr(out_mod, "_probe_audio_info", counting_probe)
    monkeypatch.setattr(out_mod, "find_executable", lambda _name: "ffprobe")
    monkeypatch.setattr(out_mod, "_require_executable", lambda name: name)
    monkeypatch.setattr(out_mod, "_remux_to_mpegts", lambda _src, dst, **_kw: open(dst, "wb").write(b"ts") > 0)
    monkeypatch.setattr(dl_mod, "add_metadata_to_file", lambda *a, **kw: None)

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"combined")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(out_mod, "run_cancellable_subprocess", fake_run)

    session = MagicMock()
    session.get.side_effect = [
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3a"]),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3b"]),
    ]
    ab = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Cached Book"),
        files=[
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/2.mp3", ext="mp3"),
        ],
    )
    opts = DownloadOptions(combine=True, output_format="mp3")
    download_audiobook(ab, str(tmp_path / "Cached Book"), opts)

    # Exactly 2 probes for the 2 downloaded parts (verified on .mp3.tmp), zero duplicate .mp3 probes.
    part_probes = [
        p for p in probed_paths
        if p.endswith(".mp3") and os.path.basename(p).startswith("Part ")
    ]
    assert part_probes == []
    tmp_part_probes = [p for p in probed_paths if p.endswith(".mp3.tmp")]
    assert len(tmp_part_probes) == 2


def test_download_reuses_probe_cache_for_multipart_convert_output(tmp_path, monkeypatch):
    import json
    import os
    from types import SimpleNamespace
    from audiobookdl import Chapter
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod
    from audiobookdl.utils import AudioProbeInfo

    probed_paths: list[str] = []

    def counting_probe(path: str) -> AudioProbeInfo:
        probed_paths.append(path)
        if path.endswith(".mp3.tmp"):
            return AudioProbeInfo(duration=12.5, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2)
        return AudioProbeInfo(duration=12.5, codec_name="aac", bitrate=128000, sample_rate=44100, channels=2)

    monkeypatch.setattr(out_mod, "_probe_audio_info", counting_probe)
    monkeypatch.setattr(out_mod, "find_executable", lambda _name: "ffprobe")
    monkeypatch.setattr(out_mod, "_require_executable", lambda name: name)
    monkeypatch.setattr(dl_mod.metadata, "add_metadata", lambda *a, **kw: False)
    monkeypatch.setattr(
        dl_mod,
        "resolve_audio_duration_ms",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should use probe_cache")),
    )

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"m4b")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(out_mod, "run_cancellable_subprocess", fake_run)

    session = MagicMock()
    session.get.side_effect = [
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3a"]),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3b"]),
    ]
    ab = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Convert Cached Book"),
        files=[
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/2.mp3", ext="mp3"),
        ],
        chapters=[Chapter(start=0, title="Part 1")],
    )
    opts = DownloadOptions(combine=False, output_format="m4b", write_abs_metadata=True)
    out_dir = tmp_path / "Convert Cached Book"
    download_audiobook(ab, str(out_dir), opts)

    # 2 probes on .mp3.tmp during download + 2 probes on staged .m4b during convert_output;
    # zero duplicate probes on Part *.mp3 or final Part *.m4b.
    assert [p for p in probed_paths if p.endswith(".mp3")] == []
    assert [p for p in probed_paths if p.endswith(".m4b") and ".tmp." not in os.path.basename(p)] == []
    assert len([p for p in probed_paths if p.endswith(".mp3.tmp")]) == 2
    assert len([p for p in probed_paths if ".tmp." in os.path.basename(p) and p.endswith(".m4b")]) == 2
    abs_meta = json.loads((out_dir / "metadata.json").read_text(encoding="utf-8"))
    assert abs_meta["chapters"][-1]["end"] == 25.0


def test_download_split_silence_fallback_preserves_probe_cache(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod
    from audiobookdl.utils import AudioProbeInfo

    probed_paths: list[str] = []

    def counting_probe(path: str) -> AudioProbeInfo:
        probed_paths.append(path)
        if path.endswith(".mp3.tmp"):
            return AudioProbeInfo(duration=10.0, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2)
        return AudioProbeInfo(duration=20.0, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2)

    monkeypatch.setattr(out_mod, "_probe_audio_info", counting_probe)
    monkeypatch.setattr(out_mod, "find_executable", lambda _name: "ffprobe")
    monkeypatch.setattr(out_mod, "_require_executable", lambda name: name)
    monkeypatch.setattr(out_mod, "_remux_to_mpegts", lambda _src, dst, **_kw: open(dst, "wb").write(b"ts") > 0)
    monkeypatch.setattr(dl_mod.metadata, "add_metadata", lambda *a, **kw: False)
    monkeypatch.setattr(
        dl_mod,
        "resolve_audio_duration_ms",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should use probe_cache")),
    )

    def fake_run(cmd, **_kwargs):
        if "-af" in cmd and any("silencedetect" in str(arg) for arg in cmd):
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"[silencedetect] silence_end: 2.0\n")
        Path(cmd[-1]).write_bytes(b"master")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(out_mod, "run_cancellable_subprocess", fake_run)

    session = MagicMock()
    session.get.side_effect = [
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3a"]),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3b"]),
    ]
    ab = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Silence Fallback"),
        files=[
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/2.mp3", ext="mp3"),
        ],
    )
    opts = DownloadOptions(
        split_chapters=True,
        chapters_from_silence=True,
        output_format="mp3",
        write_abs_metadata=True,
    )
    out_base = tmp_path / "Silence Fallback"
    download_audiobook(ab, str(out_base), opts)

    final_mp3 = tmp_path / "Silence Fallback.mp3"
    assert final_mp3.is_file()
    assert str(final_mp3) not in probed_paths
    abs_meta = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert abs_meta["chapters"][-1]["end"] == 20.0


def test_add_metadata_to_dir_respects_write_cover_false(tmp_path):
    from audiobookdl.output.download import add_metadata_to_dir

    book_dir = tmp_path / "book_out"
    book_dir.mkdir()
    ab = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="No Cover Book", authors=["Author"]),
        files=[],
        cover=Cover(image=b"\xff\xd8\xff\xe0fake-jpeg", extension="jpg"),
    )

    add_metadata_to_dir(ab, [], str(book_dir), DownloadOptions(write_cover=False))
    assert not (book_dir / "cover.jpg").exists()

    add_metadata_to_dir(ab, [], str(book_dir), DownloadOptions(write_cover=True))
    assert (book_dir / "cover.jpg").read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg"


def test_download_audiobook_split_intro_chapter_mode_updates_filenames_tags_and_sidecars(tmp_path, monkeypatch):
    import json
    import os
    from types import SimpleNamespace
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod
    from audiobookdl.utils import AudioProbeInfo

    monkeypatch.setattr(out_mod, "_require_executable", lambda _n: "ffmpeg")
    monkeypatch.setattr(out_mod, "find_executable", lambda _n: "ffprobe")
    monkeypatch.setattr(
        out_mod,
        "_probe_audio_info",
        lambda _p: AudioProbeInfo(duration=30.0, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2),
    )

    def fake_run(cmd, **_kwargs):
        if "-af" in cmd and any("silencedetect" in str(arg) for arg in cmd):
            return SimpleNamespace(
                returncode=0,
                stdout=b"",
                stderr=b"[silencedetect] silence_end: 10.0\n[silencedetect] silence_end: 20.0\n",
            )
        Path(cmd[-1]).write_bytes(b"mp3")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(out_mod, "run_cancellable_subprocess", fake_run)

    tagged_calls: list[tuple[str, str]] = []

    def fake_add_metadata(
        filepath: str,
        _meta: AudiobookMetadata,
        cover: Cover | None = None,
        track_context: TrackTagContext | None = None,
        **_kw: object,
    ) -> bool:
        assert track_context is not None
        tagged_calls.append((os.path.basename(filepath), track_context.title))
        return True

    monkeypatch.setattr(dl_mod.metadata, "add_metadata", fake_add_metadata)

    session = MagicMock()
    session.get.return_value = _mock_response(headers={"Content-Length": "4"}, chunks=[b"mp3a"])
    ab = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Intro Split Book"),
        files=[AudiobookFile(url="https://example.com/single.mp3", ext="mp3")],
    )
    opts = DownloadOptions(
        split_chapters=True,
        chapters_from_silence=True,
        intro_chapter_mode="always",
        write_json_metadata=True,
    )
    out_dir = tmp_path / "Intro Split Book"
    download_audiobook(ab, str(out_dir), opts)

    produced_mp3s = sorted(p.name for p in out_dir.glob("*.mp3"))
    assert produced_mp3s == [
        "00 - Introduction.mp3",
        "01 - Chapter 1.mp3",
        "02 - Chapter 2.mp3",
    ]
    assert tagged_calls == [
        ("00 - Introduction.mp3", "Introduction"),
        ("01 - Chapter 1.mp3", "Chapter 1"),
        ("02 - Chapter 2.mp3", "Chapter 2"),
    ]
    meta_json = json.loads((out_dir / "metadata.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in meta_json["chapters"]] == [
        "Introduction",
        "Chapter 1",
        "Chapter 2",
    ]


def test_resume_combine_preserves_preexisting_files_in_output_dir(tmp_path, monkeypatch):
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    book_dir = tmp_path / "Shared Book"
    book_dir.mkdir()
    sibling_file = book_dir / "notes.txt"
    sibling_file.write_bytes(b"keep-me")

    session = MagicMock()
    session.get.side_effect = [
        _mock_response({"Content-Length": "5"}, [b"part0"]),
        _mock_response({"Content-Length": "5"}, [b"part1"]),
    ]
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Shared Book"),
        files=[
            AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
        ],
    )

    def fake_combine(filepaths, _in_dir, out_path, **_kwargs):
        assert len(filepaths) == 2
        Path(out_path).write_bytes(b"combined-m4b")
        return True

    monkeypatch.setattr(out_mod, "combine_audiofiles", fake_combine)
    monkeypatch.setattr(dl_mod.metadata, "add_metadata", lambda *_a, **_kw: True)

    opts = DownloadOptions(
        resume=True,
        combine=True,
        output_format="m4b",
        skip_downloaded=False,
        write_json_metadata=False,
        no_chapters=True,
    )
    download_audiobook(book, str(book_dir), opts)

    assert (tmp_path / "Shared Book.m4b").read_bytes() == b"combined-m4b"
    assert sibling_file.read_bytes() == b"keep-me"
    assert not (book_dir / "Part 0.mp3").exists()
    assert not (book_dir / "Part 1.mp3").exists()


def test_download_file_normalizes_content_type_parameters_and_case(tmp_path):
    from audiobookdl.exceptions import DownloadError
    from audiobookdl.output.download import download_file

    session_ok = MagicMock()
    session_ok.get.return_value = _mock_response(
        {"Content-Type": "Audio/MPEG; charset=utf-8", "Content-Length": "4"},
        [b"mp3!"],
    )
    book_ok = Audiobook(
        session=session_ok,
        metadata=AudiobookMetadata(title="CT OK"),
        files=[
            AudiobookFile(
                url="https://example.com/ok.mp3",
                ext="mp3",
                expected_content_type="audio/mpeg",
            )
        ],
    )
    out_path = download_file((book_ok, str(tmp_path / "ct_ok"), 0, lambda _d: None, None))
    assert Path(out_path).read_bytes() == b"mp3!"

    session_bad = MagicMock()
    bad_resp = _mock_response(
        {"Content-Type": "text/html; charset=utf-8", "Content-Length": "4"},
        [b"html"],
    )
    bad_resp.content = b"html"
    session_bad.get.return_value = bad_resp
    book_bad = Audiobook(
        session=session_bad,
        metadata=AudiobookMetadata(title="CT Bad"),
        files=[
            AudiobookFile(
                url="https://example.com/bad.mp3",
                ext="mp3",
                expected_content_type="audio/mpeg",
            )
        ],
    )
    with pytest.raises(DownloadError):
        download_file((book_bad, str(tmp_path / "ct_bad"), 0, lambda _d: None, None))


def test_combine_tags_staged_file_before_os_replace_and_passes_silence_thresholds(
    tmp_path, monkeypatch
):
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    seen_tagged_paths: list[tuple[str, bool]] = []
    silence_calls: list[tuple[float, float]] = []
    final_path = tmp_path / "Atomic Book.m4b"

    session = MagicMock()
    session.get.side_effect = [
        _mock_response({"Content-Length": "4"}, [b"pt0!"]),
        _mock_response({"Content-Length": "4"}, [b"pt1!"]),
    ]
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Atomic Book"),
        files=[
            AudiobookFile(url="https://example.com/0.mp3", ext="mp3"),
            AudiobookFile(url="https://example.com/1.mp3", ext="mp3"),
        ],
    )

    def fake_combine(_parts, _dir, staged_out, **_kw):
        Path(staged_out).write_bytes(b"combined-staged")
        return False

    def fake_detect(_path, *, noise_db=-35.0, min_silence_sec=1.8, **_kw):
        silence_calls.append((noise_db, min_silence_sec))
        return []

    def fake_add_metadata(filepath, _meta, **_kw):
        seen_tagged_paths.append((filepath, final_path.exists()))
        raise RuntimeError("mutagen disk full")

    monkeypatch.setattr(out_mod, "combine_audiofiles", fake_combine)
    monkeypatch.setattr(out_mod, "detect_silence_chapters", fake_detect)
    monkeypatch.setattr(dl_mod.metadata, "add_metadata", fake_add_metadata)

    opts = DownloadOptions(
        combine=True,
        output_format="m4b",
        chapters_from_silence=True,
        silence_noise_db=-42.5,
        silence_min_seconds=2.5,
        skip_downloaded=False,
        write_json_metadata=False,
    )
    with pytest.raises(RuntimeError, match="mutagen disk full"):
        download_audiobook(book, str(tmp_path / "Atomic Book"), opts)

    assert len(seen_tagged_paths) == 1
    assert ".tmp." in seen_tagged_paths[0][0]
    assert seen_tagged_paths[0][1] is False
    assert not final_path.exists()
    assert silence_calls == [(-42.5, 2.5)]
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_download_audiobook_override_chapters_with_silence_detection(tmp_path, monkeypatch):
    from audiobookdl import Chapter
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    src_mp3 = tmp_path / "single.mp3"
    src_mp3.write_bytes(b"mp3")
    monkeypatch.setattr(dl_mod, "download_files_with_progress", lambda *a, **kw: [str(src_mp3)])
    monkeypatch.setattr(
        out_mod,
        "detect_silence_chapters",
        lambda *a, **kw: [Chapter(0, "Chapter 1"), Chapter(8000, "Chapter 2")],
    )
    monkeypatch.setattr(
        out_mod,
        "convert_single_file",
        lambda old_path, out_fmt, **kwargs: (str(tmp_path / f"book.{out_fmt}"), True),
    )
    recorded: list[list[tuple[int, str]]] = []
    monkeypatch.setattr(
        dl_mod,
        "add_metadata_to_file",
        lambda ab, fp, opts, **kwargs: recorded.append([(c.start, c.title) for c in ab.chapters]),
    )

    # With override_chapters=True, existing bad Storytel chapters are replaced by silence chapters
    book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Storytel Book"),
        files=[AudiobookFile(url="https://example.com/single.mp3", ext="mp3")],
        chapters=[Chapter(0, "Track 1"), Chapter(3000, "Track 2"), Chapter(6000, "Track 3")],
    )
    opts = DownloadOptions(
        output_format="m4b",
        chapters_from_silence=True,
        override_chapters=True,
        skip_downloaded=False,
        write_json_metadata=False,
    )
    download_audiobook(book, str(tmp_path / "Storytel Book"), opts)
    assert recorded == [[(0, "Chapter 1"), (8000, "Chapter 2")]]

    # When enrichment already replaced chapters (chapters_already_overridden=True),
    # silence detection must NOT clobber them.
    recorded.clear()
    enriched_book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Enriched Book"),
        files=[AudiobookFile(url="https://example.com/single.mp3", ext="mp3")],
        chapters=[Chapter(0, "Audible Prologue"), Chapter(10000, "Audible Ch 1")],
    )
    download_audiobook(
        enriched_book,
        str(tmp_path / "Enriched Book"),
        opts,
        chapters_already_overridden=True,
    )
    assert recorded == [[(0, "Audible Prologue"), (10000, "Audible Ch 1")]]


def test_download_file_configurable_retry_count_and_exponential_backoff(tmp_path, monkeypatch):
    from audiobookdl.exceptions import DownloadError

    # 1. max_retries=0 performs a single attempt with zero sleeps
    delays_zero: list[float] = []
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", delays_zero.append)
    monkeypatch.setattr("audiobookdl.utils.http.random.uniform", lambda _a, _b: 0.0)

    session_zero = MagicMock()
    resp_503 = _mock_response(status_code=503, headers={}, chunks=[])
    resp_503.content = b"unavailable"
    session_zero.get.return_value = resp_503
    book_zero = _make_audiobook(session_zero)

    with pytest.raises(DownloadError):
        download_file((book_zero, str(tmp_path / "zero"), 0, lambda _: None, None), max_retries=0)
    assert session_zero.get.call_count == 1
    assert delays_zero == []

    # 2. max_retries=3 attempts up to 4 times with strictly increasing exponential backoff (1s, 2s, 4s)
    delays_exp: list[float] = []
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", delays_exp.append)
    session_exp = MagicMock()
    session_exp.get.side_effect = [
        requests.exceptions.Timeout("t1"),
        requests.exceptions.Timeout("t2"),
        requests.exceptions.Timeout("t3"),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"done"]),
    ]
    out_path = download_file(
        (_make_audiobook(session_exp), str(tmp_path / "exp"), 0, lambda _: None, None),
        max_retries=3,
    )
    assert Path(out_path).read_bytes() == b"done"
    assert session_exp.get.call_count == 4
    assert delays_exp == [1.0, 2.0, 4.0]


def test_download_audiobook_forwards_max_retries_option(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "audiobookdl.output.output.verify_audio_integrity",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "audiobookdl.output.download.add_metadata_to_file",
        lambda *_a, **_kw: None,
    )
    session = MagicMock()
    session.get.side_effect = [
        requests.exceptions.ConnectionError("drop-1"),
        requests.exceptions.ConnectionError("drop-2"),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"data"]),
    ]
    book = _make_audiobook(session)
    opts = DownloadOptions(
        max_retries=1,
        skip_downloaded=False,
        write_json_metadata=False,
    )
    with pytest.raises(requests.exceptions.ConnectionError):
        download_audiobook(book, str(tmp_path / "book_fail"), opts)
    assert session.get.call_count == 2  # 1 initial + 1 retry

    session.get.reset_mock()
    session.get.side_effect = [
        requests.exceptions.ConnectionError("drop-1"),
        requests.exceptions.ConnectionError("drop-2"),
        _mock_response(headers={"Content-Length": "4"}, chunks=[b"data"]),
    ]
    opts_ok = DownloadOptions(
        max_retries=2,
        skip_downloaded=False,
        write_json_metadata=False,
    )
    download_audiobook(book, str(tmp_path / "book_ok"), opts_ok)
    assert session.get.call_count == 3  # 1 initial + 2 retries
    assert (tmp_path / "book_ok.mp3").read_bytes() == b"data"


def test_download_file_retries_curl_cffi_transient_and_refreshes_url_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)

    class _CurlTimeoutError(Exception):
        __module__ = "curl_cffi.requests.exceptions"

    urls = iter([
        "https://cdn.example.com/signed-v1.mp3",
        "https://cdn.example.com/signed-v2.mp3",
        "https://cdn.example.com/signed-v3.mp3",
    ])
    requested_urls: list[str] = []
    session = MagicMock()

    def fake_get(url, **_kwargs):
        requested_urls.append(url)
        if len(requested_urls) == 1:
            raise _CurlTimeoutError("curl timeout")
        if len(requested_urls) == 2:
            resp = _mock_response(status_code=403, headers={}, chunks=[])
            resp.content = b"expired"
            return resp
        return _mock_response(headers={"Content-Length": "5"}, chunks=[b"fresh"])

    session.get.side_effect = fake_get
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Resolver Retry"),
        files=[
            AudiobookFile(
                url="https://cdn.example.com/placeholder.mp3",
                ext="mp3",
                expected_status_code=200,
                url_resolver=lambda: next(urls),
            )
        ],
    )
    out_path = download_file((book, str(tmp_path / "resolver"), 0, lambda _: None, None), max_retries=2)
    assert Path(out_path).read_bytes() == b"fresh"
    assert requested_urls == [
        "https://cdn.example.com/signed-v1.mp3",
        "https://cdn.example.com/signed-v2.mp3",
        "https://cdn.example.com/signed-v3.mp3",
    ]


def test_estimate_download_size_from_metadata_ranges_and_head_probes():
    from audiobookdl.output.download import estimate_download_size

    # 1. Exact sum from file.size and Range headers (no HTTP calls)
    session_unused = MagicMock()
    book_known = Audiobook(
        session=session_unused,
        metadata=AudiobookMetadata(title="Known Sizes"),
        files=[
            AudiobookFile(url="https://cdn.example.com/a.mp3", ext="mp3", size=1000),
            AudiobookFile(
                url="https://cdn.example.com/b.m4s",
                ext="m4s",
                headers={"Range": "bytes=100-599"},
            ),
        ],
    )
    est_known = estimate_download_size(book_known, probe_remote=True)
    assert est_known.estimated_bytes == 1500
    assert est_known.known_bytes == 1500
    assert est_known.known_files == 2
    assert est_known.total_files == 2
    assert est_known.is_exact is True
    session_unused.head.assert_not_called()

    # 2. Sampled HEAD probing extrapolates across large multi-segment streams
    session_probe = MagicMock()
    session_probe.head.side_effect = lambda *_a, **_kw: MagicMock(
        status_code=200,
        headers={"Content-Length": "50000"},
    )
    book_hls = Audiobook(
        session=session_probe,
        metadata=AudiobookMetadata(title="Large Stream"),
        files=[
            AudiobookFile(url=f"https://cdn.example.com/seg{i}.ts", ext="ts")
            for i in range(25)
        ],
    )
    est_hls = estimate_download_size(book_hls, probe_remote=True, max_probe_files=5)
    assert session_probe.head.call_count == 5
    assert est_hls.known_files == 5
    assert est_hls.total_files == 25
    assert est_hls.is_exact is False
    assert est_hls.estimated_bytes == 25 * 50000


def test_download_files_with_progress_reports_live_size_estimates_and_byte_eta(tmp_path, monkeypatch):
    from audiobookdl import logging as dl_logging
    from audiobookdl.output.download import download_files_with_progress

    clock = [10.0]
    monkeypatch.setattr("audiobookdl.logging.time.monotonic", lambda: clock[0])

    estimates: list[tuple[int | None, bool, int, int]] = []

    class _EstimateSink:
        def on_log(self, _lvl: str, _msg: str) -> None:
            pass

        def on_progress_start(self, _desc: str, _total: float) -> None:
            pass

        def on_progress_advance(self, _completed: float, _total: float) -> None:
            pass

        def on_progress_finish(self) -> None:
            pass

        def on_download_estimate(
            self,
            estimated_bytes: int | None,
            is_exact: bool,
            known_files: int,
            total_files: int,
        ) -> None:
            estimates.append((estimated_bytes, is_exact, known_files, total_files))

    session = MagicMock()
    session.get.side_effect = [
        _mock_response(headers={"Content-Length": "1000"}, chunks=[b"a" * 1000]),
        _mock_response(headers={"Content-Length": "2000"}, chunks=[b"b" * 2000]),
    ]
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="Live Size"),
        files=[
            AudiobookFile(url="https://cdn.example.com/1.mp3", ext="mp3"),
            AudiobookFile(url="https://cdn.example.com/2.mp3", ext="mp3"),
        ],
    )
    monkeypatch.setattr("audiobookdl.output.output.verify_audio_integrity", lambda *_a, **_kw: None)

    sink = _EstimateSink()
    with dl_logging.job_context(dl_logging.JobContext(sink=sink)):
        download_files_with_progress(book, str(tmp_path / "live"), max_workers=1)

    # First GET learns file 0 = 1000 -> extrapolates 2000; second GET learns file 1 = 2000 -> exact 3000
    assert (2000, False, 1, 2) in estimates
    assert estimates[-1] == (3000, True, 2, 2)


def test_download_audiobook_forwards_bitrate_and_sample_rate_on_original_format(tmp_path, monkeypatch):
    from audiobookdl.output import download as dl_mod
    from audiobookdl.output import output as out_mod

    src_mp3 = tmp_path / "book.mp3"
    src_mp3.write_bytes(b"fake-mp3")

    monkeypatch.setattr(dl_mod, "download_files_with_progress", lambda *a, **kw: [str(src_mp3)])
    monkeypatch.setattr(dl_mod, "add_metadata_to_file", lambda *a, **kw: None)

    convert_calls = []

    def fake_convert_single(old_path, output_format, **kwargs):
        convert_calls.append((old_path, output_format, kwargs))
        return old_path, False

    monkeypatch.setattr(out_mod, "convert_single_file", fake_convert_single)

    audiobook = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Sample Rate Book"),
        files=[AudiobookFile(url="https://example.com/single.mp3", ext="mp3")],
    )
    opts = DownloadOptions(
        output_format=None,
        audio_bitrate="96k",
        sample_rate=22050,
    )
    download_audiobook(audiobook, str(tmp_path / "Sample Rate Book"), opts)

    assert len(convert_calls) == 1
    _path, target_fmt, kwargs = convert_calls[0]
    assert target_fmt == "mp3"
    assert kwargs["audio_bitrate"] == "96k"
    assert kwargs["sample_rate"] == 22050


def test_download_file_cdn_content_type_compatibility_and_soft_errors(tmp_path):
    from audiobookdl.exceptions import CloudflareBlocked

    # 1. MIME aliases, generic binary types, and omitted Content-Type succeed on media files
    for idx, (resp_headers, expected_ct, ext) in enumerate(
        [
            ({"Content-Type": "audio/mp3", "Content-Length": "4"}, "audio/mpeg", "mp3"),
            ({"Content-Type": "binary/octet-stream", "Content-Length": "4"}, "audio/mpeg", "mp3"),
            ({"Content-Length": "4"}, "audio/mpeg", "mp3"),
            ({"Content-Type": "video/mp2t", "Content-Length": "4"}, "application/octet-stream", "ts"),
            ({"Content-Type": "audio/x-m4a", "Content-Length": "4"}, "audio/mp4", "m4a"),
        ]
    ):
        session_ok = MagicMock()
        session_ok.get.return_value = _mock_response(resp_headers, [b"data"])
        book = Audiobook(
            session=session_ok,
            metadata=AudiobookMetadata(title=f"Compat {idx}"),
            files=[
                AudiobookFile(
                    url=f"https://cdn.example.com/{idx}.{ext}",
                    ext=ext,
                    expected_content_type=expected_ct,
                )
            ],
        )
        out = download_file((book, str(tmp_path / f"compat_{idx}"), 0, lambda _: None, None))
        assert Path(out).read_bytes() == b"data"

    # 2. HTTP 200 soft-error HTML/XML pages are rejected even for ebooks (which skip ffprobe)
    session_epub = MagicMock()
    epub_err_resp = _mock_response(
        {"Content-Type": "application/xml; charset=utf-8", "Content-Length": "38"},
        [b"<Error><Code>AccessDenied</Code></Error>"],
    )
    epub_err_resp.content = b"<Error><Code>AccessDenied</Code></Error>"
    session_epub.get.return_value = epub_err_resp
    epub_book = Audiobook(
        session=session_epub,
        metadata=AudiobookMetadata(title="Soft Error Ebook"),
        files=[AudiobookFile(url="https://cdn.example.com/book.epub", ext="epub")],
    )
    with pytest.raises(DownloadError):
        download_file((epub_book, str(tmp_path / "soft_epub"), 0, lambda _: None, None))
    assert not (tmp_path / "soft_epub.epub").exists()

    # 3. Cloudflare challenge fails fast with CloudflareBlocked instead of retrying
    session_cf = MagicMock()
    cf_resp = _mock_response(
        {"cf-mitigated": "challenge", "server": "cloudflare", "Content-Type": "text/html"},
        [b"<html><title>Just a moment...</title></html>"],
        status_code=429,
    )
    cf_resp.content = b"<html><title>Just a moment...</title></html>"
    session_cf.get.return_value = cf_resp
    cf_book = _make_audiobook(session_cf)
    with pytest.raises(CloudflareBlocked):
        download_file((cf_book, str(tmp_path / "cf"), 0, lambda _: None, None), max_retries=3)
    assert session_cf.get.call_count == 1


def test_download_file_slices_200_when_cdn_ignores_custom_range_and_handles_duplicate_cl(tmp_path, monkeypatch):
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", lambda _: None)

    # 1. Custom byte-range request where CDN ignores Range and returns 200 OK full payload
    full_payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    session_slice = MagicMock()
    resp_full = _mock_response(
        {"Content-Length": f"{len(full_payload)}, {len(full_payload)}"},
        [full_payload[:8], full_payload[8:20], full_payload[20:]],
        status_code=200,
    )
    session_slice.get.return_value = resp_full
    progress: list[float] = []
    book_slice = Audiobook(
        session=session_slice,
        metadata=AudiobookMetadata(title="Range Slice"),
        files=[
            AudiobookFile(
                url="https://cdn.example.com/big.m4a",
                ext="m4a",
                headers={"Range": "bytes=10-19"},
            )
        ],
    )
    out_slice = download_file((book_slice, str(tmp_path / "slice"), 0, progress.append, None))
    assert Path(out_slice).read_bytes() == b"ABCDEFGHIJ"
    assert sum(progress) == pytest.approx(1.0)

    # 2. Empty 200 OK response retries and recovers when CDN delivers non-empty body
    session_empty = MagicMock()
    session_empty.get.side_effect = [
        _mock_response({"Content-Type": "audio/mpeg"}, []),
        _mock_response({"Content-Length": "4", "Content-Type": "audio/mpeg"}, [b"mp3!"]),
    ]
    out_empty = download_file((_make_audiobook(session_empty), str(tmp_path / "empty_retry"), 0, lambda _: None, None))
    assert Path(out_empty).read_bytes() == b"mp3!"
    assert session_empty.get.call_count == 2


def test_download_file_retries_408_rate_limit_reset_and_401_url_resolver(tmp_path, monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr("audiobookdl.output.download.time.sleep", delays.append)

    urls = iter(["https://cdn.example.com/v1.mp3", "https://cdn.example.com/v2.mp3", "https://cdn.example.com/v3.mp3"])
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(status_code=408, headers={"X-RateLimit-Reset": "2"}, chunks=[]),
        _mock_response(status_code=401, headers={}, chunks=[]),
        _mock_response(status_code=200, headers={"Content-Length": "4"}, chunks=[b"good"]),
    ]
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="CDN Retries"),
        files=[
            AudiobookFile(
                url="https://cdn.example.com/init.mp3",
                ext="mp3",
                url_resolver=lambda: next(urls),
            )
        ],
    )
    out = download_file((book, str(tmp_path / "cdn_retry"), 0, lambda _: None, None))
    assert Path(out).read_bytes() == b"good"
    assert delays[0] == pytest.approx(2.0)


def test_estimate_download_size_falls_back_to_range_get_when_head_rejected():
    from audiobookdl.output.download import estimate_download_size

    session = MagicMock()
    head_resp = MagicMock(status_code=405, headers={})
    get_resp = MagicMock(status_code=206, headers={"Content-Range": "bytes 0-0/123456"})
    session.head.return_value = head_resp
    session.get.return_value = get_resp
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="No HEAD CDN"),
        files=[AudiobookFile(url="https://cdn.example.com/signed.mp3", ext="mp3")],
    )
    est = estimate_download_size(book, probe_remote=True)
    assert est.estimated_bytes == 123456
    assert est.is_exact is True
    session.get.assert_called_once()
    head_resp.close.assert_called_once()
    get_resp.close.assert_called_once()







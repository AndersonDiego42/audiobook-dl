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
from audiobookdl.utils.audiobook import AESEncryption, Cover
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
    enc = AESEncryption(key=key, iv=iv)

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
    #    --resume without --skip-downloaded must NOT prompt, re-download, or reconvert.
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
    #    --resume skips HTTP download and converts existing .mp3 -> .m4b without prompts.
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

    # 3. Empty (0-byte) final .m4b is NOT treated as complete under --resume
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
    from audiobookdl.utils.audiobook import AESEncryption

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
        files=[AudiobookFile(url="https://example.com/enc.ts", ext="ts", encryption_method=AESEncryption(key=key, iv=iv))],
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


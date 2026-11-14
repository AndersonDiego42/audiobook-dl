import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import patch

import pytest

from audiobookdl import AudiobookMetadata, Chapter
from audiobookdl.exceptions import DownloadCancelled, FailedCombining, GenericAudiobookDLException
from audiobookdl.output import output
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.output.output import combine_audiofiles, convert_output, gen_output_location, get_extension
from audiobookdl.output.download import get_output_audio_format
from audiobookdl.utils import find_executable
from audiobookdl.utils.concurrency import run_cancellable_subprocess

class _OutputTestCase(TypedDict):
    template: str
    metadata: AudiobookMetadata
    remove_chars: str
    output: str


TEST_DATA: list[_OutputTestCase] = [
    {
        "template": "{author} - {title}",
        "metadata": AudiobookMetadata("The Way of Kings", authors = ["Brandon Sanderson"]),
        "remove_chars": "",
        "output": "Brandon Sanderson - The Way of Kings"
    },
    {
        "template": "{title}",
        "metadata": AudiobookMetadata("The Deal of a Lifetime: A Novella"),
        "remove_chars": ":,.",
        "output": "The Deal of a Lifetime A Novella",
    }
]

def test_gen_output_location():
    for test in TEST_DATA:
        assert gen_output_location(test["template"], test["metadata"], test["remove_chars"]) == test["output"]


def test_gen_output_audio_format_with_option():
    assert get_output_audio_format("m4b", ["file1.mp3","file2.mp3","file3.mp3"]) == ("mp3","m4b")


def test_gen_output_audio_format_without_option():
    assert get_output_audio_format(None, ["file1.mp3","file2.mp3","file3.mp3"]) == ("mp3", "mp3")


def test_get_extension_and_audio_format_normalize_case():
    assert get_extension("Book/Part 01.MP3") == "mp3"
    assert get_extension("Book/Audiobook.M4B") == "m4b"
    assert get_output_audio_format("MP3", ["file1.MP3"]) == ("mp3", "mp3")
    assert get_output_audio_format(None, ["file1.M4B"]) == ("m4b", "m4b")


def test_convert_output_same_extension_is_noop(tmp_path):
    m4b = tmp_path / "book.m4b"
    m4b.write_bytes(b"data")
    with patch("audiobookdl.output.output.run_cancellable_subprocess") as mock_run:
        result = convert_output([str(m4b)], "m4b")
    assert result == [str(m4b)]
    assert m4b.exists()
    mock_run.assert_not_called()


def test_convert_output_preserves_original_on_ffmpeg_failure(tmp_path):
    mp3 = tmp_path / "book.mp3"
    mp3.write_bytes(b"mp3-bytes")
    with patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"), \
         patch("audiobookdl.output.output._ffmpeg_audio_codec", return_value="mp3"), \
         patch("audiobookdl.output.output.run_cancellable_subprocess", return_value=SimpleNamespace(returncode=1, stderr=b"boom")):
        with pytest.raises(GenericAudiobookDLException):
            convert_output([str(mp3)], "m4b")
    assert mp3.exists()


def test_convert_output_aac_to_m4b_uses_stream_copy(tmp_path):
    ts_file = tmp_path / "book.ts"
    ts_file.write_bytes(b"ts-bytes")
    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        open(cmd[-1], "wb").write(b"m4b-bytes")
        return SimpleNamespace(returncode=0, stderr=b"")

    with patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"), \
         patch("audiobookdl.output.output._ffmpeg_audio_codec", return_value="aac"), \
         patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run):
        result = convert_output([str(ts_file)], "m4b")

    assert result == [str(tmp_path / "book.m4b")]
    assert not ts_file.exists()
    assert (tmp_path / "book.m4b").exists()
    cmd = captured_cmds[0]
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-bsf:a" in cmd and cmd[cmd.index("-bsf:a") + 1] == "aac_adtstoasc"
    assert "-movflags" in cmd and cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert "-vn" in cmd


def test_real_ffmpeg_mp3_and_combine_to_m4b(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    assert ffmpeg is not None

    # 1. Single MP3 -> M4B transcode
    mp3 = tmp_path / "single.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.2", str(mp3)],
        check=True,
        capture_output=True,
    )
    converted = convert_output([str(mp3)], "m4b")
    assert converted == [str(tmp_path / "single.m4b")]
    assert (tmp_path / "single.m4b").stat().st_size > 0
    assert output._ffmpeg_audio_codec(converted[0]) == "aac"

    # 2. Combine AAC parts directly into M4B
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    p1 = parts_dir / "part0.ts"
    p2 = parts_dir / "part1.ts"
    for p in (p1, p2):
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1.0", "-c:a", "aac", "-f", "mpegts", str(p)],
            check=True,
            capture_output=True,
        )
    combined_m4b = tmp_path / "combined.m4b"
    combine_audiofiles([str(p1), str(p2)], str(parts_dir), str(combined_m4b))
    assert combined_m4b.exists() and combined_m4b.stat().st_size > 0
    assert output._ffmpeg_audio_codec(str(combined_m4b)) == "aac"


def test_gen_output_location_sanitizes_all_metadata_fields_and_traversal():
    metadata = AudiobookMetadata(
        title="Book: Part *1?",
        authors=["../Evil: Author*"],
        series=r"..\..\Series<>|",
        narrators=["Narrator/One"],
        genres=["Sci-Fi?"],
    )
    with patch("audiobookdl.output.output.platform.system", return_value="Windows"):
        result = gen_output_location(
            r"C:\Users\Reader\Downloads\{author}\{series}\{narrator} - {genre} - {title}",
            metadata,
            remove_chars=":",
        )

    assert result.startswith(r"C:\Users\Reader\Downloads\\".rstrip("\\") + "\\")
    rel = result[len(r"C:\Users\Reader\Downloads\\") - 1 :]
    assert ".." not in rel.split("\\")
    for forbidden in ('*', '?', '<', '>', '|', ':'):
        assert forbidden not in rel
    assert rel.endswith(r"Narrator-One - Sci-Fi - Book Part 1")

    # Literal ".." author must not become a parent-directory traversal segment
    dotdot = AudiobookMetadata(title="Safe", authors=[".."])
    assert gen_output_location("{author}/{title}", dotdot, "") == "--/Safe"


def test_gen_output_location_enforces_utf8_component_limit_and_windows_max_path():
    # Multi-byte UTF-8 title exceeding 255 bytes
    utf8_title = "あ" * 120  # 360 bytes in UTF-8
    meta = AudiobookMetadata(title=utf8_title, authors=["Author"])
    res = gen_output_location("{author}/{title}", meta, "")
    last_component = res.split("/")[-1]
    assert len(last_component.encode("utf-8")) <= 255 - output.OUTPUT_PATH_EXT_RESERVE
    assert last_component.encode("utf-8").decode("utf-8") == last_component

    # Long Windows parent prefix + multi-segment template stays within MAX_PATH
    long_prefix = r"C:\Users\VeryLongUsernameHere\Downloads\Audiobooks\SubfolderOne\SubfolderTwo"
    long_meta = AudiobookMetadata(
        title="T" * 160,
        authors=["A" * 160],
    )
    with patch("audiobookdl.output.output.platform.system", return_value="Windows"):
        win_path = gen_output_location(
            os.path.join(long_prefix, "{author}", "{title}"),
            long_meta,
            "",
        )
    assert len(win_path) + output.OUTPUT_PATH_EXT_RESERVE < output.WINDOWS_MAX_PATH
    assert win_path.startswith(long_prefix + os.sep)


def test_combine_audiofiles_sizes_remux_pool_dynamically(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: name)
    monkeypatch.setattr(output, "_remux_to_mpegts", lambda src, dst, cancel_event=None: open(dst, "wb").write(b"ts") > 0)
    monkeypatch.setattr(output, "_ffmpeg_audio_codec", lambda path: "mp3")
    monkeypatch.setattr(output, "_ffprobe_duration", lambda path: 10.0)

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"out")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    pool_sizes = []
    from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

    def tracking_executor(*, max_workers: int):
        pool_sizes.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr("audiobookdl.utils.concurrency.ThreadPoolExecutor", tracking_executor)

    # 1 file: skips ThreadPoolExecutor
    src = tmp_path / "0.mp3"
    src.write_bytes(b"a")
    combine_audiofiles([str(src)], str(tmp_path / "tmp1"), str(tmp_path / "out1.mp3"))
    assert pool_sizes == []

    # 3 files: pool size 3
    files_3 = []
    for i in range(3):
        p = tmp_path / f"f{i}.mp3"
        p.write_bytes(b"a")
        files_3.append(str(p))
    combine_audiofiles(files_3, str(tmp_path / "tmp3"), str(tmp_path / "out3.mp3"))
    assert pool_sizes == [3]

    # 20 files: capped at COMBINE_REMUX_THREADS (16)
    files_20 = []
    for i in range(20):
        p = tmp_path / f"g{i}.mp3"
        p.write_bytes(b"a")
        files_20.append(str(p))
    combine_audiofiles(files_20, str(tmp_path / "tmp20"), str(tmp_path / "out20.mp3"))
    assert pool_sizes == [3, output.COMBINE_REMUX_THREADS]


def test_run_cancellable_subprocess_terminates_child_promptly():
    cancel_event = threading.Event()
    timer = threading.Timer(0.15, cancel_event.set)
    start = time.monotonic()
    timer.start()
    try:
        with pytest.raises(DownloadCancelled):
            run_cancellable_subprocess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                cancel_event=cancel_event,
                poll_interval=0.05,
            )
    finally:
        timer.cancel()
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


def test_convert_combine_and_chapters_clean_partial_output_on_cancel(tmp_path, monkeypatch):
    # 1. convert_output removes partial output and keeps source
    src_mp3 = tmp_path / "book.mp3"
    src_mp3.write_bytes(b"mp3")
    cancel_event = threading.Event()

    def cancelling_convert(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"partial")
        raise DownloadCancelled

    monkeypatch.setattr(output, "_require_executable", lambda name: name)
    monkeypatch.setattr(output, "_ffmpeg_audio_codec", lambda path: "mp3")
    monkeypatch.setattr(output, "run_cancellable_subprocess", cancelling_convert)

    with pytest.raises(DownloadCancelled):
        convert_output([str(src_mp3)], "m4b", cancel_event=cancel_event)
    assert src_mp3.exists()
    assert not (tmp_path / "book.m4b").exists()

    # 2. combine_audiofiles removes partial output and temp ts artifacts on cancel
    part = tmp_path / "part0.mp3"
    part.write_bytes(b"a")
    tmp_dir = tmp_path / "combine_tmp"
    out_m4b = tmp_path / "combined.m4b"
    monkeypatch.setattr(output, "_remux_to_mpegts", lambda s, d, cancel_event=None: open(d, "wb").write(b"ts") > 0)
    with pytest.raises(DownloadCancelled):
        combine_audiofiles([str(part)], str(tmp_dir), str(out_m4b), cancel_event=cancel_event)
    assert not out_m4b.exists()
    assert not (tmp_dir / "combined.ts").exists()
    assert not (tmp_dir / "ts_parts").exists()
    assert not (tmp_path / "combine_tmp.work").exists()

    # 3. add_chapters_ffmpeg removes temp chapter/media files on cancel
    m4b = tmp_path / "chap.m4b"
    m4b.write_bytes(b"orig")
    monkeypatch.setattr(ffmpeg_metadata.utils, "find_executable", lambda name: name)
    monkeypatch.setattr(ffmpeg_metadata, "create_tmp_chapter_file", lambda fp, ch: ";FFMETADATA1\n")

    def cancelling_chapters(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"partial")
        raise DownloadCancelled

    monkeypatch.setattr(ffmpeg_metadata, "run_cancellable_subprocess", cancelling_chapters)
    with pytest.raises(DownloadCancelled):
        ffmpeg_metadata.add_chapters_ffmpeg(
            str(m4b),
            [Chapter(0, "Ch 1")],
            cancel_event=cancel_event,
        )
    assert m4b.read_bytes() == b"orig"
    assert not (tmp_path / "chap.tmp.m4b").exists()
    assert not (tmp_path / "chap.m4b.chapters.tmp.txt").exists()


def test_combine_audiofiles_isolates_work_dir_and_cleans_on_failure(tmp_path, monkeypatch):
    parts_dir = tmp_path / "Book"
    parts_dir.mkdir()
    part0 = parts_dir / "Part 0.mp3"
    part0.write_bytes(b"part0")
    out_m4b = tmp_path / "Book.m4b"

    monkeypatch.setattr(output, "_require_executable", lambda name: name)
    monkeypatch.setattr(output, "_remux_to_mpegts", lambda s, d, cancel_event=None: open(d, "wb").write(b"ts") > 0)
    monkeypatch.setattr(output, "_ffmpeg_audio_codec", lambda path: "mp3")

    def failing_mux(cmd, **kwargs):
        # Simulate ffmpeg writing a corrupt partial file before failing
        with open(cmd[-1], "wb") as f:
            f.write(b"corrupt")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"mux failed")

    # Empty output triggers FailedCombining when partial file exists? Force size 0 or truncation:
    monkeypatch.setattr(output, "run_cancellable_subprocess", failing_mux)
    monkeypatch.setattr(output, "_ffprobe_duration", lambda path: 100.0 if path.endswith("combined.ts") else 10.0)

    with pytest.raises(FailedCombining):
        combine_audiofiles([str(part0)], str(parts_dir), str(out_m4b))

    # Work directory and corrupt output are cleaned up; source part remains intact for retry
    assert not out_m4b.exists()
    assert not (tmp_path / "Book.work").exists()
    assert not (parts_dir / "ts_parts").exists()
    assert part0.read_bytes() == b"part0"


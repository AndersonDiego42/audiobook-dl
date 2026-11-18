import os
from pathlib import Path
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
from audiobookdl.output.output import (
    combine_audiofiles,
    convert_output,
    gen_output_location,
    get_extension,
)
from audiobookdl.output.download import get_output_audio_format
from audiobookdl.utils import AudioProbeInfo, find_executable, normalize_audio_bitrate, resolve_audio_bitrate
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
         patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(codec_name="mp3")), \
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
         patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(duration=10.0, codec_name="aac")), \
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


@pytest.mark.ffmpeg
def test_real_ffmpeg_mp3_and_combine_to_m4b(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    assert ffmpeg is not None

    # 1. Single MP3 -> M4B transcode
    mp3 = tmp_path / "single.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.2", "-threads", "1", str(mp3)],
        check=True,
        capture_output=True,
    )
    converted = convert_output([str(mp3)], "m4b")
    assert converted == [str(tmp_path / "single.m4b")]
    assert (tmp_path / "single.m4b").stat().st_size > 0
    assert output._probe_audio_info(converted[0]).codec_name == "aac"

    # 2. Combine AAC parts directly into M4B
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    p1 = parts_dir / "part0.ts"
    p2 = parts_dir / "part1.ts"
    for p in (p1, p2):
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1.0", "-threads", "1", "-c:a", "aac", "-f", "mpegts", str(p)],
            check=True,
            capture_output=True,
        )
    combined_m4b = tmp_path / "combined.m4b"
    combine_audiofiles([str(p1), str(p2)], str(parts_dir), str(combined_m4b))
    assert combined_m4b.exists() and combined_m4b.stat().st_size > 0
    assert output._probe_audio_info(str(combined_m4b)).codec_name == "aac"


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


@pytest.mark.no_worker_cap
def test_combine_audiofiles_sizes_remux_pool_dynamically(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: name)
    monkeypatch.setattr(
        output,
        "_remux_to_mpegts",
        lambda src, dst, cancel_event=None, *, preserve_video=False: open(dst, "wb").write(b"ts") > 0,
    )
    def fake_probe(path: str) -> AudioProbeInfo:
        if path.endswith("out3.mp3"):
            return AudioProbeInfo(duration=30.0, codec_name="mp3")
        if path.endswith("out20.mp3"):
            return AudioProbeInfo(duration=200.0, codec_name="mp3")
        return AudioProbeInfo(duration=10.0, codec_name="mp3")

    monkeypatch.setattr(output, "_probe_audio_info", fake_probe)

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

    # 3 files without pre-warmed probe cache: probe pool (3) + remux pool (3)
    files_3 = []
    for i in range(3):
        p = tmp_path / f"f{i}.mp3"
        p.write_bytes(b"a")
        files_3.append(str(p))
    combine_audiofiles(files_3, str(tmp_path / "tmp3"), str(tmp_path / "out3.mp3"))
    assert pool_sizes == [3, 3]

    # Pre-warmed probe_cache skips the probe ThreadPoolExecutor completely
    pool_sizes.clear()
    warm_cache = {p: AudioProbeInfo(duration=10.0, codec_name="mp3") for p in files_3}
    combine_audiofiles(
        files_3,
        str(tmp_path / "tmp3_cached"),
        str(tmp_path / "out3.mp3"),
        probe_cache=warm_cache,
    )
    assert pool_sizes == [3]

    # 20 files without pre-warmed cache: both pools bounded by resolve_ffmpeg_pool_workers (I/O)
    pool_sizes.clear()
    files_20 = []
    for i in range(20):
        p = tmp_path / f"g{i}.mp3"
        p.write_bytes(b"a")
        files_20.append(str(p))
    combine_audiofiles(files_20, str(tmp_path / "tmp20"), str(tmp_path / "out20.mp3"))
    expected_io = output.resolve_ffmpeg_pool_workers(None, cpu_bound=False)
    assert pool_sizes == [expected_io, expected_io]

    # Explicit max_workers=2 bounds both probe and remux pools
    pool_sizes.clear()
    combine_audiofiles(files_20, str(tmp_path / "tmp20_cap"), str(tmp_path / "out20.mp3"), max_workers=2)
    assert pool_sizes == [2, 2]


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
    monkeypatch.setattr(output, "_probe_audio_info", lambda path: AudioProbeInfo(codec_name="mp3"))
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
    monkeypatch.setattr(
        output,
        "_remux_to_mpegts",
        lambda s, d, cancel_event=None, *, preserve_video=False: open(d, "wb").write(b"ts") > 0,
    )
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
    monkeypatch.setattr(
        output,
        "_remux_to_mpegts",
        lambda s, d, cancel_event=None, *, preserve_video=False: open(d, "wb").write(b"ts") > 0,
    )
    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda path: AudioProbeInfo(
            duration=100.0 if path.endswith("combined.ts") else 10.0,
            codec_name="mp3",
        ),
    )

    def failing_mux(cmd, **kwargs):
        # Simulate ffmpeg writing a corrupt partial file before failing
        with open(cmd[-1], "wb") as f:
            f.write(b"corrupt")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"mux failed")

    # Empty output triggers FailedCombining when partial file exists? Force size 0 or truncation:
    monkeypatch.setattr(output, "run_cancellable_subprocess", failing_mux)

    with pytest.raises(FailedCombining):
        combine_audiofiles([str(part0)], str(parts_dir), str(out_m4b))

    # Work directory and corrupt output are cleaned up; source part remains intact for retry
    assert not out_m4b.exists()
    assert not (tmp_path / "Book.work").exists()
    assert not (parts_dir / "ts_parts").exists()
    assert part0.read_bytes() == b"part0"


def test_normalize_and_resolve_aac_bitrate(monkeypatch):
    assert normalize_audio_bitrate(None) is None
    assert normalize_audio_bitrate("") is None
    assert normalize_audio_bitrate("auto") is None
    assert normalize_audio_bitrate("Auto") is None
    assert normalize_audio_bitrate("64k") == "64k"
    assert normalize_audio_bitrate("128K") == "128k"
    assert normalize_audio_bitrate("192") == "192k"

    for bad in ("fast", "0k", "2000k", "-64k"):
        with pytest.raises(ValueError):
            normalize_audio_bitrate(bad)

    # Explicit bitrate bypasses probe
    assert resolve_audio_bitrate("dummy.mp3", "192k", probe_bps=lambda _p: 32000) == "192k"

    # Auto mode probes and clamps to [64k, 192k]
    assert resolve_audio_bitrate("dummy.mp3", "auto", probe_bps=lambda _p: 32000) == "64k"
    assert resolve_audio_bitrate("dummy.mp3", None, probe_bps=lambda _p: 96000) == "96k"
    assert resolve_audio_bitrate("dummy.mp3", None, probe_bps=lambda _p: 320000) == "192k"
    assert resolve_audio_bitrate("dummy.mp3", None, probe_bps=lambda _p: None) == "128k"


def test_convert_output_passes_auto_and_explicit_aac_bitrate(tmp_path):
    mp3 = tmp_path / "book.mp3"
    mp3.write_bytes(b"mp3-bytes")
    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        open(cmd[-1], "wb").write(b"m4b-bytes")
        return SimpleNamespace(returncode=0, stderr=b"")

    # Auto mode uses probed bitrate (e.g., 96k)
    with patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"), \
         patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(duration=10.0, codec_name="mp3", bitrate=96000)), \
         patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run):
        convert_output([str(mp3)], "m4b")

    auto_cmd = captured_cmds[-1]
    assert auto_cmd[auto_cmd.index("-b:a") + 1] == "96k"

    # Explicit bitrate overrides probe
    mp3.write_bytes(b"mp3-bytes")
    (tmp_path / "book.m4b").unlink()
    with patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"), \
         patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(duration=10.0, codec_name="mp3", bitrate=64000)), \
         patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run):
        convert_output([str(mp3)], "m4b", audio_bitrate="192k")

    explicit_cmd = captured_cmds[-1]
    assert explicit_cmd[explicit_cmd.index("-b:a") + 1] == "192k"

    # Invalid bitrate fails fast before running ffmpeg
    mp3.write_bytes(b"mp3-bytes")
    with pytest.raises(ValueError):
        convert_output([str(mp3)], "m4b", audio_bitrate="invalid")


@pytest.mark.ffmpeg
def test_real_ffprobe_and_m4b_bitrate_conversion(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe not available")
    assert ffmpeg is not None
    assert ffprobe is not None

    mp3 = tmp_path / "speech_64k.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1.0", "-threads", "1", "-b:a", "64k", str(mp3)],
        check=True,
        capture_output=True,
    )
    probed_bps = output._probe_audio_info(str(mp3)).bitrate
    assert probed_bps is not None
    assert 48000 <= probed_bps <= 80000
    assert resolve_audio_bitrate(str(mp3)) == "64k"

    converted = convert_output([str(mp3)], "m4b", audio_bitrate="192k")
    assert converted == [str(tmp_path / "speech_64k.m4b")]
    assert output._probe_audio_info(converted[0]).codec_name == "aac"


def test_gen_output_location_missing_optional_metadata_and_collapsed_segments():
    standalone = AudiobookMetadata(title="Standalone Book", authors=["Jane Author"])
    assert gen_output_location("{author}/{series}/{title}", standalone, "") == "Jane Author/Standalone Book"
    assert gen_output_location("{series}/{title}", standalone, "") == "Standalone Book"
    assert (
        gen_output_location("/downloads/{series}/{publisher}/{isbn}/{release_date}/{language}/{title}", standalone, "")
        == "/downloads/Standalone Book"
    )

    minimal = AudiobookMetadata(title="Only Title")
    assert gen_output_location("{author}/{series}/{unknown_key}/{title}", minimal, "") == "Only Title"
    assert gen_output_location("{author}/{series}", minimal, "") == "_"

    with patch("audiobookdl.output.output.platform.system", return_value="Windows"):
        assert (
            gen_output_location(r"C:\Books\{series}\{author}\{title}", minimal, "")
            == r"C:\Books\Only Title"
        )
        assert (
            gen_output_location(r"C:\Books\{series}\{author}\{title}", standalone, "")
            == r"C:\Books\Jane Author\Standalone Book"
        )


def test_gen_output_location_sanitizes_windows_reserved_names_and_reserves_temp_suffixes():
    reserved_cases = [
        ("CON", "CON_"),
        ("prn", "prn_"),
        ("Aux", "Aux_"),
        ("nul. ", "nul_"),
        ("COM1", "COM1_"),
        ("com9.extra", "com9_.extra"),
        ("LPT1", "LPT1_"),
        ("lpt9", "lpt9_"),
        ("C\x00ON", "CON_"),
    ]
    with patch("audiobookdl.output.output.platform.system", return_value="Windows"):
        for raw, expected in reserved_cases:
            meta = AudiobookMetadata(title=raw, authors=[raw])
            assert gen_output_location(r"C:\Books\{author}\{title}", meta, "") == rf"C:\Books\{expected}\{expected}"

        # Combined template segments that form a reserved name are also sanitized
        split_meta = AudiobookMetadata(title="ul", series="N")
        assert gen_output_location(r"C:\Books\{series}{title}", split_meta, "") == r"C:\Books\Nul_"

        # Non-reserved words starting with reserved prefixes remain untouched
        normal_meta = AudiobookMetadata(title="Console", authors=["Nullify"])
        assert gen_output_location(r"C:\Books\{author}\{title}", normal_meta, "") == r"C:\Books\Nullify\Console"

    assert output.OUTPUT_PATH_EXT_RESERVE >= len(".work/ts_parts/0000.ts")
    assert output.OUTPUT_PATH_EXT_RESERVE >= len(".m4b.chapters.tmp.txt")


def test_probe_audio_info_single_ffprobe_call(tmp_path):
    from unittest.mock import MagicMock

    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"audio")
    payload = '{"format": {"duration": "123.45", "bit_rate": "128000", "format_name": "mp3"}, "streams": [{"codec_name": "mp3", "bit_rate": "96000"}]}'
    mock_result = MagicMock(returncode=0, stdout=payload)

    with (
        patch("audiobookdl.utils.find_executable", return_value="ffprobe"),
        patch("audiobookdl.utils.subprocess.run", return_value=mock_result) as mock_run,
    ):
        info = output._probe_audio_info(str(sample))

    mock_run.assert_called_once()
    assert info.duration == pytest.approx(123.45)
    assert info.bitrate == 96000
    assert info.codec_name == "mp3"
    assert info.format_name == "mp3"


def test_combine_audiofiles_skips_remux_on_mpegts_and_single_file_embeds_chapters(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock
    from audiobookdl import Chapter

    tmp_dir = tmp_path / "parts"
    tmp_dir.mkdir()
    sync_pkt = b"\x47" + b"\x00" * 187
    p0 = tmp_dir / "0.ts"
    p1 = tmp_dir / "1.ts"
    p0.write_bytes(sync_pkt * 4)
    p1.write_bytes(sync_pkt * 4)
    out_path = tmp_path / "Book.m4b"
    cmds = []

    def fake_run(cmd, capture_output=False, cancel_event=None):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"combined-m4b")
        return MagicMock(returncode=0, stderr=b"")

    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output._remux_to_mpegts") as mock_remux,
        patch(
            "audiobookdl.output.output._probe_audio_info",
            side_effect=lambda path: output.AudioProbeInfo(
                duration=5.0 if path.endswith(("0.ts", "1.ts")) else 10.0,
                bitrate=128000,
                codec_name="aac",
                format_name="mpegts",
            ),
        ),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run),
    ):
        chapters_embedded = output.combine_audiofiles(
            [str(p0), str(p1)],
            str(tmp_dir),
            str(out_path),
            chapters=[Chapter(start=0, title="Intro"), Chapter(start=5000, title="Ch 1")],
        )
        assert chapters_embedded is True
        assert "-map_chapters" in cmds[0]
        mock_remux.assert_not_called()

        single_mp3 = tmp_path / "Single.mp3"
        single_mp3.write_bytes(b"mp3")
        new_path, single_embedded = output.convert_single_file(
            str(single_mp3),
            "m4b",
            chapters=[Chapter(start=0, title="Intro"), Chapter(start=5000, title="Ch 1")],
        )
        assert new_path == str(tmp_path / "Single.m4b")
        assert single_embedded is True
        assert "-map_chapters" in cmds[-1]


def test_convert_output_parallel_preserves_sources_on_failure(tmp_path):
    f0 = tmp_path / "part0.mp3"
    f1 = tmp_path / "part1.mp3"
    f0.write_bytes(b"p0")
    f1.write_bytes(b"p1")
    out0 = tmp_path / "part0.m4b"
    out1 = tmp_path / "part1.m4b"

    def fake_convert_single(
        old_path,
        target_format,
        cancel_event=None,
        audio_bitrate=None,
        remove_source=True,
        **_kwargs,
    ):
        if old_path == str(f0):
            out0.write_bytes(b"m4b0")
            return str(out0), False
        raise GenericAudiobookDLException("boom")

    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output.convert_single_file", side_effect=fake_convert_single),
    ):
        with pytest.raises(GenericAudiobookDLException, match="boom"):
            convert_output([str(f0), str(f1)], "m4b")

    assert f0.exists()
    assert f1.exists()
    assert not out0.exists()
    assert not out1.exists()


def test_run_cancellable_subprocess_ffmpeg_progress_callback():
    from audiobookdl.utils.concurrency import ffmpeg_progress_callback, run_cancellable_subprocess

    reported = []
    script = (
        "import sys; "
        "sys.stdout.write('out_time_us=1500000\\nprogress=continue\\nout_time_us=3000000\\nprogress=end\\n'); "
        "sys.stdout.flush()"
    )
    with ffmpeg_progress_callback(reported.append):
        proc = run_cancellable_subprocess(
            [sys.executable, "-c", script, "-progress", "pipe:1"],
            capture_output=True,
        )

    assert proc.returncode == 0
    assert reported == [1.5, 3.0]


def test_gen_output_location_conditional_segments_and_presets():
    from datetime import date
    import pycountry
    from audiobookdl.output.output import OUTPUT_TEMPLATE_PRESETS

    with_series = AudiobookMetadata(
        title="Dune Messiah",
        authors=["Frank Herbert"],
        series="Dune Saga",
        series_order=2,
        language=pycountry.languages.get(alpha_2="en"),
        release_date=date(1969, 10, 15),
        source="storytel",
    )
    abs_path = gen_output_location(OUTPUT_TEMPLATE_PRESETS["audiobookshelf"], with_series, "")
    assert abs_path.replace("\\", "/") == "Frank Herbert/Dune Saga/Book 02 - Dune Messiah"

    standalone = AudiobookMetadata(
        title="Standalone Title",
        authors=["Author One"],
        language=pycountry.languages.get(alpha_2="de"),
        release_date=date(2021, 3, 1),
        scrape_url="https://www.librivox.org/book/123",
    )
    abs_standalone = gen_output_location(OUTPUT_TEMPLATE_PRESETS["audiobookshelf"], standalone, "")
    assert abs_standalone.replace("\\", "/") == "Author One/Standalone Title"

    kavita_path = gen_output_location(OUTPUT_TEMPLATE_PRESETS["kavita"], standalone, "")
    assert kavita_path.replace("\\", "/") == "Author One/(2021) Standalone Title"

    else_tpl = "{source}/{language}/{series?Series {series}|Standalone} - {title}"
    rendered_else = gen_output_location(else_tpl, standalone, "")
    assert rendered_else.replace("\\", "/") == "librivox/deu/Standalone - Standalone Title"


@pytest.mark.ffmpeg
def test_real_ffmpeg_split_chapters_and_hls_smart_merge(tmp_path):
    ffmpeg_bin = find_executable("ffmpeg")
    if not ffmpeg_bin:
        pytest.skip("ffmpeg not available")
    assert ffmpeg_bin is not None
    from audiobookdl.output.output import (
        merge_hls_segments_into_chapters,
        should_smart_merge_hls,
        split_audio_by_chapters,
    )

    # Create 4x 1-second MPEG-TS AAC segments
    seg_paths = []
    for idx in range(4):
        seg = tmp_path / f"seg_{idx}.ts"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={440 + idx * 110}:sample_rate=22050:duration=1",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-f",
                "mpegts",
                str(seg),
            ],
            check=True,
            capture_output=True,
        )
        seg_paths.append(str(seg))

    chapters = [
        Chapter(start=0, title="Intro"),
        Chapter(start=2000, title="Act I"),
    ]
    assert should_smart_merge_hls(seg_paths, chapters) is True

    merge_dir = tmp_path / "merged_chapters"
    merge_dir.mkdir()
    copied_segs = []
    for i, sp in enumerate(seg_paths):
        dst = merge_dir / f"seg_{i}.ts"
        dst.write_bytes(open(sp, "rb").read())
        copied_segs.append(str(dst))

    merged_files = merge_hls_segments_into_chapters(
        copied_segs,
        str(merge_dir),
        chapters,
        output_format="m4b",
    )
    assert len(merged_files) == 2
    assert os.path.basename(merged_files[0]) == "00 - Intro.m4b"
    assert os.path.basename(merged_files[1]) == "01 - Act I.m4b"
    assert all(os.path.exists(p) and os.path.getsize(p) > 0 for p in merged_files)

    # Direct split_audio_by_chapters on single m4b
    master = tmp_path / "single.mp3"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=22050:duration=3",
            "-threads",
            "1",
            "-c:a",
            "libmp3lame",
            str(master),
        ],
        check=True,
        capture_output=True,
    )
    split_dir = tmp_path / "split_mp3"
    split_paths = split_audio_by_chapters(str(master), str(split_dir), chapters)
    assert len(split_paths) == 2
    assert os.path.basename(split_paths[0]) == "00 - Intro.mp3"
    assert os.path.basename(split_paths[1]) == "01 - Act I.mp3"
    assert all(os.path.getsize(p) > 0 for p in split_paths)

    split_off_dir = tmp_path / "split_mp3_off"
    split_off_paths = split_audio_by_chapters(
        str(master), str(split_off_dir), chapters, intro_chapter_mode="off"
    )
    assert [os.path.basename(p) for p in split_off_paths] == [
        "01 - Intro.mp3",
        "02 - Act I.mp3",
    ]


def test_combine_audiofiles_reuses_ffprobe_cache_and_skips_redundant_combined_probe(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    tmp_dir = tmp_path / "single_ts"
    tmp_dir.mkdir()
    sync_pkt = b"\x47" + b"\x00" * 187
    p0 = tmp_dir / "0.ts"
    p0.write_bytes(sync_pkt * 4)
    out_path = tmp_path / "Book.m4b"
    probed_paths: list[str] = []

    def fake_probe(path: str):
        probed_paths.append(path)
        return output.AudioProbeInfo(duration=10.0, bitrate=128000, codec_name="aac", format_name="mpegts")

    def fake_run(cmd, capture_output=False, cancel_event=None):
        Path(cmd[-1]).write_bytes(b"combined-m4b")
        return MagicMock(returncode=0, stderr=b"")

    cache = {}
    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output._probe_audio_info", side_effect=fake_probe),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run),
    ):
        output.combine_audiofiles([str(p0)], str(tmp_dir), str(out_path), probe_cache=cache)

    # Only 0.ts (once for _all_inputs_mpegts + cached for sample_info) and out_path are probed; combined.ts is skipped
    assert probed_paths == [str(p0), str(out_path)]
    assert str(p0) in cache and str(out_path) in cache


def test_split_audio_by_chapters_parallel_accurate_and_fallback(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock
    from audiobookdl import Chapter

    master = tmp_path / "master.m4b"
    master.write_bytes(b"m4b")
    chapters = [
        Chapter(start=0, title="One"),
        Chapter(start=5000, title="Two"),
        Chapter(start=10000, title="Three"),
    ]
    cmds: list[list[str]] = []

    # 1. accurate=True places -ss AFTER -i and uses re-encode flags via thread pool
    def fake_run_ok(cmd, capture_output=False, cancel_event=None):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"chunk")
        return MagicMock(returncode=0, stderr=b"")

    out_acc = tmp_path / "acc"
    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch(
            "audiobookdl.output.output._probe_audio_info",
            return_value=output.AudioProbeInfo(duration=15.0, bitrate=128000, codec_name="aac", format_name="mov,mp4,m4a"),
        ),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run_ok),
    ):
        result = output.split_audio_by_chapters(str(master), str(out_acc), chapters, accurate=True)

    assert len(result) == 3
    for cmd in cmds:
        assert cmd.index("-i") < cmd.index("-ss")
        assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"

    # 2. accurate=False uses -avoid_negative_ts make_zero and falls back per-chapter on stream copy failure
    fallback_cmds: list[list[str]] = []
    call_count = 0

    def fake_run_fallback(cmd, capture_output=False, cancel_event=None):
        nonlocal call_count
        call_count += 1
        fallback_cmds.append(cmd)
        if "-avoid_negative_ts" in cmd and "copy" in cmd and cmd[-1].endswith("02 - Two.m4b"):
            return MagicMock(returncode=1, stderr=b"copy error")
        Path(cmd[-1]).write_bytes(b"chunk")
        return MagicMock(returncode=0, stderr=b"")

    out_fb = tmp_path / "fb"
    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch(
            "audiobookdl.output.output._probe_audio_info",
            return_value=output.AudioProbeInfo(duration=15.0, bitrate=128000, codec_name="aac", format_name="mov,mp4,m4a"),
        ),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run_fallback),
    ):
        fb_result = output.split_audio_by_chapters(str(master), str(out_fb), chapters, accurate=False)

    assert len(fb_result) == 3
    assert call_count == 4  # 3 fast copy attempts + 1 re-encode retry for chapter 2
    assert any(cmd.index("-i") < cmd.index("-ss") and "aac" in cmd for cmd in fallback_cmds)


def test_combine_audiofiles_parallel_aac_transcode_concat_and_fallback(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    p0 = tmp_path / "01.mp3"
    p1 = tmp_path / "02.m4a"
    p0.write_bytes(b"mp3")
    p1.write_bytes(b"m4a")
    out_m4b = tmp_path / "Mixed.m4b"
    cmds: list[list[str]] = []
    probed_paths: list[str] = []

    def fake_probe(path: str) -> AudioProbeInfo:
        probed_paths.append(path)
        if path == str(p0):
            return AudioProbeInfo(duration=6.0, bitrate=96000, codec_name="mp3", format_name="mp3", sample_rate=44100, channels=1)
        if path == str(p1):
            return AudioProbeInfo(duration=4.0, bitrate=128000, codec_name="aac", format_name="mp4", sample_rate=44100, channels=2)
        return AudioProbeInfo(duration=10.0, bitrate=128000, codec_name="aac", format_name="mp4", sample_rate=44100, channels=2)

    def fake_run(cmd, capture_output=False, cancel_event=None):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"data")
        return MagicMock(returncode=0, stderr=b"")

    cache = {
        str(p0): fake_probe(str(p0)),
        str(p1): fake_probe(str(p1)),
    }
    probed_paths.clear()
    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output._probe_audio_info", side_effect=fake_probe),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run),
    ):
        chapters_ok = combine_audiofiles(
            [str(p0), str(p1)],
            str(tmp_path / "work1"),
            str(out_m4b),
            chapters=[Chapter(start=0, title="Ch 1"), Chapter(start=6000, title="Ch 2")],
            probe_cache=cache,
            faststart=False,
        )

    assert chapters_ok is True
    # Part 0 (mono MP3) encodes to 44100 Hz stereo AAC; Part 1 (already 44100 Hz stereo AAC) copies directly:
    assert len(cmds) == 3
    part_cmds = {c[c.index("-i") + 1]: c for c in cmds if c[-1].endswith(".ts")}
    mux_cmd = [c for c in cmds if c[-1] == str(out_m4b)][0]
    p0_cmd = part_cmds[str(p0)]
    assert p0_cmd[p0_cmd.index("-c:a") + 1] == "aac"
    assert p0_cmd[p0_cmd.index("-b:a") + 1] == "96k"
    assert p0_cmd[p0_cmd.index("-ar") + 1] == "44100"
    assert p0_cmd[p0_cmd.index("-ac") + 1] == "2"
    p1_cmd = part_cmds[str(p1)]
    assert p1_cmd[p1_cmd.index("-c:a") + 1] == "copy"
    assert p1_cmd[p1_cmd.index("-f") + 1] == "mpegts"
    assert mux_cmd[mux_cmd.index("-i") + 1].startswith("concat:")
    assert mux_cmd[mux_cmd.index("-c:a") + 1] == "copy"
    assert mux_cmd[mux_cmd.index("-bsf:a") + 1] == "aac_adtstoasc"
    assert "-movflags" not in mux_cmd
    # Only out_m4b should have been probed (cached input durations propagated to ts_parts)
    assert probed_paths == [str(out_m4b)]

    # Fallback to combined.ts when concat: length exceeds limit
    cmds.clear()
    out_m4b_2 = tmp_path / "Fallback.m4b"
    with (
        patch("audiobookdl.output.output._MAX_CONCAT_ARG_CHARS", 10),
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output._probe_audio_info", side_effect=fake_probe),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run),
    ):
        combine_audiofiles(
            [str(p0), str(p1)],
            str(tmp_path / "work2"),
            str(out_m4b_2),
            probe_cache={str(p0): cache[str(p0)], str(p1): cache[str(p1)]},
        )
    mux_cmd_2 = [c for c in cmds if c[-1] == str(out_m4b_2)][0]
    assert "-f" in mux_cmd_2 and "concat" in mux_cmd_2
    assert mux_cmd_2[mux_cmd_2.index("-i") + 1].endswith("concat_list.txt")
    assert "+faststart" in mux_cmd_2


@pytest.mark.ffmpeg
def test_real_ffmpeg_combine_mixed_mp3_and_m4a_to_m4b(tmp_path):
    ffmpeg_bin = find_executable("ffmpeg")
    ffprobe_bin = find_executable("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        pytest.skip("ffmpeg/ffprobe not available")
    assert ffmpeg_bin is not None
    assert ffprobe_bin is not None

    p1 = tmp_path / "01.mp3"
    p2 = tmp_path / "02.m4a"
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050:duration=1.2", "-threads", "1", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k", str(p1)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=550:sample_rate=22050:duration=1.2", "-threads", "1", "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(p2)],
        check=True,
        capture_output=True,
    )
    out_m4b = tmp_path / "CombinedMixed.m4b"
    chapters = [Chapter(start=0, title="Part One"), Chapter(start=1200, title="Part Two")]
    embedded = combine_audiofiles(
        [str(p1), str(p2)],
        str(tmp_path / "work"),
        str(out_m4b),
        chapters=chapters,
        faststart=False,
    )
    assert embedded is True
    info = output._probe_audio_info(str(out_m4b))
    assert info.codec_name == "aac"
    assert 2.2 <= info.duration <= 2.7
    assert info.channels == 2


def test_faststart_flag_on_convert_single_file_and_explicit_output_path(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    src = tmp_path / "book.m4b"
    src.write_bytes(b"m4b")
    dst = tmp_path / "staged.m4b"
    cmds: list[list[str]] = []

    def fake_run(cmd, capture_output=False, cancel_event=None):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"out")
        return MagicMock(returncode=0, stderr=b"")

    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(duration=10.0, codec_name="aac")),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=fake_run),
    ):
        new_path, chapters_ok = output.convert_single_file(
            str(src),
            "m4b",
            chapters=[Chapter(start=0, title="Intro")],
            remove_source=False,
            output_path=str(dst),
            faststart=False,
        )

    assert new_path == str(dst)
    assert chapters_ok is True
    assert src.exists()
    assert dst.exists()
    assert cmds[0][cmds[0].index("-c:a") + 1] == "copy"
    assert "-movflags" not in cmds[0]


def test_verify_audio_integrity_and_staged_convert_preserve_existing_on_failure(tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock

    src = tmp_path / "book.mp3"
    src.write_bytes(b"src-mp3")
    existing_dst = tmp_path / "book.m4b"
    existing_dst.write_bytes(b"previous-good-m4b")

    def _corrupt_ffmpeg(cmd, capture_output=False, cancel_event=None):
        Path(cmd[-1]).write_bytes(b"corrupt-m4b")
        return MagicMock(returncode=0, stderr=b"")

    def _probe_side_effect(path: str) -> AudioProbeInfo:
        if path == str(src):
            return AudioProbeInfo(duration=12.0, bitrate=128000, codec_name="mp3")
        return AudioProbeInfo(duration=0.0, codec_name="")

    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output.find_executable", return_value="ffprobe"),
        patch("audiobookdl.output.output._probe_audio_info", side_effect=_probe_side_effect),
        patch("audiobookdl.output.output.run_cancellable_subprocess", side_effect=_corrupt_ffmpeg),
    ):
        with pytest.raises(GenericAudiobookDLException):
            output.convert_single_file(str(src), "m4b", remove_source=True)

    # Original source AND existing target remain untouched, and no temp file lingers
    assert src.read_bytes() == b"src-mp3"
    assert existing_dst.read_bytes() == b"previous-good-m4b"
    assert [p.name for p in tmp_path.iterdir()] == sorted(["book.m4b", "book.mp3"])


def test_probe_audio_info_distinguishes_ffprobe_execution_failure_from_corrupt_media(tmp_path):
    from unittest.mock import MagicMock
    from audiobookdl.utils import probe_audio_info, validate_ffmpeg_dir

    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"not-valid-mp3")

    # 1. OSError (e.g. WinError 216 wrong architecture) sets probe_error and raises GenericAudiobookDLException
    with (
        patch("audiobookdl.utils.find_executable", return_value="/bad/ffprobe"),
        patch("audiobookdl.utils.subprocess.run", side_effect=OSError("Exec format error")),
    ):
        info = probe_audio_info(str(sample))
        assert info.probe_error is not None
        assert "Exec format error" in info.probe_error
        with pytest.raises(GenericAudiobookDLException, match="ffprobe could not run"):
            output.verify_audio_integrity(str(sample))

    # 2. Non-zero ffprobe exit when `ffprobe -version` works -> normal corrupt-media check (FailedCombining)
    probe_fail = MagicMock(returncode=1, stdout="", stderr="Invalid data found")
    version_ok = MagicMock(returncode=0, stdout="ffprobe version 7.0", stderr="")
    with (
        patch("audiobookdl.utils.find_executable", return_value="/good/ffprobe"),
        patch("audiobookdl.output.output.find_executable", return_value="/good/ffprobe"),
        patch("audiobookdl.utils.subprocess.run", side_effect=[probe_fail, version_ok]),
    ):
        info_corrupt = probe_audio_info(str(sample))
        assert info_corrupt.probe_error is None
        with patch("audiobookdl.utils.subprocess.run", side_effect=[probe_fail, version_ok]):
            with pytest.raises(FailedCombining, match="Invalid or corrupt"):
                output.verify_audio_integrity(str(sample))

    # 3. validate_ffmpeg_dir rejects stub/unrunnable binaries
    ff_dir = tmp_path / "bad_ff" / "bin"
    ff_dir.mkdir(parents=True)
    exe_suffix = ".exe" if os.name == "nt" else ""
    (ff_dir / f"ffmpeg{exe_suffix}").write_text("stub")
    resolved, err = validate_ffmpeg_dir(str(ff_dir.parent))
    assert resolved is None
    assert err is not None


def test_chapter_offset_loudnorm_mono_and_silence_detection():
    from unittest.mock import MagicMock

    chapters = [Chapter(start=0, title="Intro"), Chapter(start=10000, title="Ch 1")]
    shifted = output.apply_chapter_offset(chapters, -2500)
    assert [(c.start, c.title) for c in shifted] == [(0, "Intro"), (7500, "Ch 1")]

    codec_args = output._build_mux_codec_args(
        "m4b",
        "m4b",
        AudioProbeInfo(duration=60.0, bitrate=96000, codec_name="aac"),
        None,
        loudnorm=True,
        mono=True,
    )
    assert "-af" in codec_args
    assert any("loudnorm" in a for a in codec_args)
    assert "-ac" in codec_args and codec_args[codec_args.index("-ac") + 1] == "1"

    fake_res = MagicMock(
        returncode=0,
        stderr=b"[silencedetect] silence_end: 15.5 | silence_duration: 2.1\n[silencedetect] silence_end: 32.0 | silence_duration: 1.9\n",
    )
    with (
        patch("audiobookdl.output.output._require_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.output.run_cancellable_subprocess", return_value=fake_res),
        patch("audiobookdl.output.output._probe_audio_info", return_value=AudioProbeInfo(duration=60.0)),
    ):
        detected = output.detect_silence_chapters("book.m4b")
    assert [(c.start, c.title) for c in detected] == [
        (0, "Chapter 1"),
        (15500, "Chapter 2"),
        (32000, "Chapter 3"),
    ]


def test_combine_audiofiles_flac_concat_demuxer_preserves_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: "ffmpeg")
    user_tmp_dir = tmp_path / "keep_tmp"
    user_tmp_dir.mkdir()
    sentinel = user_tmp_dir / "existing.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    f1 = tmp_path / "p1.flac"
    f2 = tmp_path / "p2.flac"
    f1.write_bytes(b"flac1")
    f2.write_bytes(b"flac2")

    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda path: AudioProbeInfo(
            duration=24.0 if path.endswith("book.flac") else 12.0,
            codec_name="flac",
            sample_rate=44100,
            channels=2,
        ),
    )

    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
            fh.write(b"combined-flac")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    out_flac = tmp_path / "book.flac"
    combine_audiofiles([str(f1), str(f2)], str(user_tmp_dir), str(out_flac))

    assert out_flac.read_bytes() == b"combined-flac"
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert len(commands) == 1
    cmd = commands[0]
    assert "-f" in cmd and "concat" in cmd


def test_combine_audiofiles_mixed_non_mp4_normalizes_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: "ffmpeg")
    f1 = tmp_path / "p1.flac"
    f2 = tmp_path / "p2.flac"
    f1.write_bytes(b"f1")
    f2.write_bytes(b"f2")

    def fake_probe(path: str) -> AudioProbeInfo:
        if path.endswith("mixed.flac"):
            return AudioProbeInfo(duration=12.0, codec_name="flac", sample_rate=48000, channels=2)
        if path.endswith("p1.flac"):
            return AudioProbeInfo(duration=5.0, codec_name="flac", sample_rate=44100, channels=1)
        return AudioProbeInfo(duration=7.0, codec_name="flac", sample_rate=48000, channels=2)

    monkeypatch.setattr(output, "_probe_audio_info", fake_probe)

    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        with open(cmd[-1], "wb") as fh:
            fh.write(b"norm-or-out")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    out_flac = tmp_path / "mixed.flac"
    combine_audiofiles([str(f1), str(f2)], str(tmp_path / "tmp"), str(out_flac))

    assert out_flac.exists()
    # 2 normalization encodes + 1 final concat mux
    assert len(commands) == 3
    final_cmd = commands[-1]
    assert "-f" in final_cmd and "concat" in final_cmd
    assert any("norm_parts" in part or "concat_list.txt" in part for part in final_cmd)


def test_combine_audiofiles_exceeding_max_concat_files_uses_concat_demuxer(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: "ffmpeg")
    monkeypatch.setattr(
        output,
        "_remux_to_mpegts",
        lambda src, dst, cancel_event=None, *, preserve_video=False: open(dst, "wb").write(b"ts") > 0,
    )
    total_parts = output._MAX_CONCAT_PROTOCOL_FILES + 1
    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda path: AudioProbeInfo(
            duration=float(total_parts) if path.endswith("many.mp3") else 1.0,
            codec_name="mp3",
        ),
    )

    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(list(cmd))
        with open(cmd[-1], "wb") as fh:
            fh.write(b"mp3")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    files = []
    for i in range(output._MAX_CONCAT_PROTOCOL_FILES + 1):
        p = tmp_path / f"seg_{i:03d}.mp3"
        p.write_bytes(b"m")
        files.append(str(p))

    out_mp3 = tmp_path / "many.mp3"
    work_tmp = tmp_path / "tmp"
    combine_audiofiles(files, str(work_tmp), str(out_mp3))

    assert out_mp3.exists()
    assert len(seen_cmds) == 1
    cmd = seen_cmds[0]
    cmd_str = " ".join(cmd)
    assert "concat:" not in cmd_str
    assert "-f" in cmd and "concat" in cmd
    assert "concat_list.txt" in cmd_str
    assert not (tmp_path / "tmp.work" / "combined.ts").exists()


def test_add_chapters_ffmpeg_applies_faststart_only_to_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_metadata.utils, "find_executable", lambda name: "ffmpeg")
    monkeypatch.setattr(ffmpeg_metadata, "create_tmp_chapter_file", lambda filepath, chapters: ";FFMETADATA1\n")
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"tagged")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_metadata, "run_cancellable_subprocess", fake_run)

    chapters = [Chapter(start=0, title="Ch1"), Chapter(start=5000, title="Ch2")]

    m4b = tmp_path / "book.m4b"
    m4b.write_bytes(b"orig")
    ffmpeg_metadata.add_chapters_ffmpeg(str(m4b), chapters)
    assert "-movflags" in commands[0] and "+faststart" in commands[0]

    mp3 = tmp_path / "book.mp3"
    mp3.write_bytes(b"orig")
    ffmpeg_metadata.add_chapters_ffmpeg(str(mp3), chapters)
    assert "-movflags" not in commands[1]


def test_split_audio_by_chapters_normalizes_chapters_and_applies_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "_require_executable", lambda name: "ffmpeg")
    master = tmp_path / "master.mp3"
    master.write_bytes(b"audio")
    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda path: AudioProbeInfo(duration=20.0, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2),
    )

    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"part")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    # Unsorted, duplicate timestamp, and past-EOF chapter (duration = 20000 ms)
    dirty_chapters = [
        Chapter(start=10000, title="Ch2"),
        Chapter(start=0, title="Ch1"),
        Chapter(start=10000, title="DupCh2"),
        Chapter(start=25000, title="PastEOF"),
    ]
    out_paths = output.split_audio_by_chapters(
        str(master),
        str(tmp_path / "split_out"),
        dirty_chapters,
        loudnorm=True,
        mono=True,
    )

    # Past-EOF dropped and duplicate deduped -> 2 valid chapters
    assert len(out_paths) == 2
    assert len(commands) == 2
    for cmd in commands:
        assert "-af" in cmd
        af_val = cmd[cmd.index("-af") + 1]
        assert "loudnorm" in af_val
        assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
        assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] != "copy"


def test_detect_silence_chapters_reuses_probe_cache(tmp_path, monkeypatch):
    audio_path = tmp_path / "silence.mp3"
    audio_path.write_bytes(b"mp3")
    monkeypatch.setattr(output, "_require_executable", lambda _n: "ffmpeg")
    monkeypatch.setattr(
        output,
        "run_cancellable_subprocess",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0,
            stderr=b"[silencedetect] silence_end: 10.0 | silence_duration: 2.0\n",
        ),
    )
    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda _p: (_ for _ in ()).throw(AssertionError("should not spawn ffprobe on cache hit")),
    )
    cache = {str(audio_path): AudioProbeInfo(duration=30.0, codec_name="mp3")}
    chapters = output.detect_silence_chapters(str(audio_path), probe_cache=cache)
    assert [(c.start, c.title) for c in chapters] == [(0, "Chapter 1"), (10000, "Chapter 2")]


@pytest.mark.no_worker_cap
def test_run_cancellable_pool_bounds_in_flight_futures():
    from audiobookdl.utils.concurrency import run_cancellable_pool

    active = 0
    peak_active = 0
    lock = threading.Lock()

    def work(x: int) -> int:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return x * 2

    results = run_cancellable_pool(work, list(range(20)), max_workers=3)
    assert results == [i * 2 for i in range(20)]
    assert 1 <= peak_active <= 3


def test_convert_output_reuses_and_updates_probe_cache_without_stale_staged_keys(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    src0 = tmp_path / "01.mp3"
    src1 = tmp_path / "02.mp3"
    src0.write_bytes(b"mp3-0")
    src1.write_bytes(b"mp3-1")
    out0 = tmp_path / "01.m4b"
    out1 = tmp_path / "02.m4b"

    probed: list[str] = []

    def fake_probe(path: str) -> AudioProbeInfo:
        probed.append(path)
        return AudioProbeInfo(duration=15.0, bitrate=128000, codec_name="aac", format_name="mov,mp4,m4a")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"m4b")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "_require_executable", lambda _n: "ffmpeg")
    monkeypatch.setattr(output, "find_executable", lambda _n: "ffprobe")
    monkeypatch.setattr(output, "_probe_audio_info", fake_probe)
    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    cache: dict[str, AudioProbeInfo] = {
        str(src0): AudioProbeInfo(duration=15.0, bitrate=128000, codec_name="mp3", format_name="mp3"),
        str(src1): AudioProbeInfo(duration=15.0, bitrate=128000, codec_name="mp3", format_name="mp3"),
    }
    converted = convert_output([str(src0), str(src1)], "m4b", probe_cache=cache)

    assert converted == [str(out0), str(out1)]
    # Sources hit cache (0 probes); only the 2 staged output files are verified via ffprobe.
    assert str(src0) not in probed and str(src1) not in probed
    assert len(probed) == 2
    assert str(out0) in cache and str(out1) in cache
    assert not any(".tmp." in k for k in cache)


def test_resolve_ffmpeg_pool_workers_cpu_and_explicit_caps(monkeypatch):
    from audiobookdl.output.output import resolve_ffmpeg_pool_workers

    monkeypatch.setattr(output, "COMBINE_REMUX_THREADS", 8)
    monkeypatch.setattr(output.os, "cpu_count", lambda: 16)
    assert resolve_ffmpeg_pool_workers(None, cpu_bound=False) == 8
    assert resolve_ffmpeg_pool_workers(None, cpu_bound=True) == 8
    assert resolve_ffmpeg_pool_workers(3, cpu_bound=True) == 3

    monkeypatch.setattr(output.os, "cpu_count", lambda: 2)
    assert resolve_ffmpeg_pool_workers(None, cpu_bound=False) == 4
    assert resolve_ffmpeg_pool_workers(None, cpu_bound=True) == 2
    assert resolve_ffmpeg_pool_workers(6, cpu_bound=True) == 2


def test_intro_chapter_detection_and_split_resolution():
    from audiobookdl.output.metadata._chapters import (
        apply_intro_chapter_titles,
        is_intro_chapter_title,
        resolve_split_chapter_entries,
    )

    assert is_intro_chapter_title("Introduction") is True
    assert is_intro_chapter_title("Intro") is True
    assert is_intro_chapter_title("Prologue") is True
    assert is_intro_chapter_title("Opening Credits") is True
    assert is_intro_chapter_title("Opening") is True
    assert is_intro_chapter_title("Foreword") is True
    assert is_intro_chapter_title("Chapter 0") is True
    assert is_intro_chapter_title("Chapter 00") is True
    assert is_intro_chapter_title("01 - Introduction") is True
    assert is_intro_chapter_title("Introduktion") is True

    assert is_intro_chapter_title("Chapter 1") is False
    assert is_intro_chapter_title("Chapter 1: Introduction") is False
    assert is_intro_chapter_title("Opening the Gate") is False
    assert is_intro_chapter_title("Introspection") is False
    assert is_intro_chapter_title("") is False

    # Auto mode: explicit intro starts at 00 and shifts off-by-one generic Chapter N titles
    assert resolve_split_chapter_entries(
        ["Introduction", "Chapter 2", "Chapter 3"],
        intro_mode="auto",
    ) == [(0, "Introduction"), (1, "Chapter 1"), (2, "Chapter 2")]

    # Auto mode: explicit intro followed by already-1-based Chapter 1 keeps Chapter 1 intact
    assert resolve_split_chapter_entries(
        ["Introduction", "Chapter 1", "Chapter 2"],
        intro_mode="auto",
    ) == [(0, "Introduction"), (1, "Chapter 1"), (2, "Chapter 2")]

    # Auto mode: untitled/generic Chapter 1..3 without explicit intro stays 01..03
    assert resolve_split_chapter_entries(
        ["Chapter 1", "Chapter 2", "Chapter 3"],
        intro_mode="auto",
    ) == [(1, "Chapter 1"), (2, "Chapter 2"), (3, "Chapter 3")]

    # Always mode: forces first generic/untitled track to 00 - Introduction and shifts Chapter 2 -> Chapter 1
    assert resolve_split_chapter_entries(
        ["Chapter 01", "Chapter 02", "Chapter 03"],
        intro_mode="always",
    ) == [(0, "Introduction"), (1, "Chapter 01"), (2, "Chapter 02")]

    # Always mode with real title keeps real title at 00
    assert resolve_split_chapter_entries(
        ["The Setup", "The Conflict", "The Resolution"],
        intro_mode="always",
    ) == [(0, "The Setup"), (1, "The Conflict"), (2, "The Resolution")]

    # Off mode: never shifts to 00 even when titled Introduction
    assert resolve_split_chapter_entries(
        ["Introduction", "Chapter 1", "Chapter 2"],
        intro_mode="off",
    ) == [(1, "Introduction"), (2, "Chapter 1"), (3, "Chapter 2")]

    # Multiple leading front-matter tracks
    assert resolve_split_chapter_entries(
        ["Opening Credits", "Prologue", "Chapter 3", "Chapter 4"],
        intro_mode="auto",
    ) == [(0, "Opening Credits"), (1, "Prologue"), (2, "Chapter 1"), (3, "Chapter 2")]

    chapters = [Chapter(0, "Chapter 1"), Chapter(5000, "Chapter 2"), Chapter(10000, "Chapter 3")]
    updated = apply_intro_chapter_titles(chapters, intro_mode="always")
    assert [(c.start, c.title) for c in updated] == [
        (0, "Introduction"),
        (5000, "Chapter 1"),
        (10000, "Chapter 2"),
    ]


def test_split_and_merge_segment_groups_respect_intro_chapter_mode(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path

    monkeypatch.setattr(output, "_require_executable", lambda _n: "ffmpeg")
    monkeypatch.setattr(output, "find_executable", lambda _n: "ffprobe")
    monkeypatch.setattr(
        output,
        "_probe_audio_info",
        lambda _p: AudioProbeInfo(duration=30.0, codec_name="mp3", bitrate=128000, sample_rate=44100, channels=2),
    )

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"out")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(output, "run_cancellable_subprocess", fake_run)

    master = tmp_path / "master.mp3"
    master.write_bytes(b"master")
    chapters = [Chapter(0, "Chapter 1"), Chapter(10000, "Chapter 2"), Chapter(20000, "Chapter 3")]

    split_always = output.split_audio_by_chapters(
        str(master), str(tmp_path / "split_always"), chapters, intro_chapter_mode="always"
    )
    assert [os.path.basename(p) for p in split_always] == [
        "00 - Introduction.mp3",
        "01 - Chapter 1.mp3",
        "02 - Chapter 2.mp3",
    ]

    seg_dir = tmp_path / "segs"
    seg_dir.mkdir()
    s0, s1, s2 = seg_dir / "s0.mp3", seg_dir / "s1.mp3", seg_dir / "s2.mp3"
    for s in (s0, s1, s2):
        s.write_bytes(b"seg")

    merged = output.merge_segment_groups_into_chapters(
        [("Introduction", [str(s0)]), ("Chapter 2", [str(s1)]), ("Chapter 3", [str(s2)])],
        str(tmp_path / "merged_auto"),
        output_format="mp3",
        intro_chapter_mode="auto",
    )
    assert [os.path.basename(p) for p in merged] == [
        "00 - Introduction.mp3",
        "01 - Chapter 1.mp3",
        "02 - Chapter 2.mp3",
    ]






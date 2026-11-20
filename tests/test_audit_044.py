"""Regression tests for AUD-044: atomic writes, os.replace retry, ffmpeg temp
paths, integrity verification, cancel cleanup, and _fix_output rules."""

import os
import threading
from unittest.mock import patch

import pytest

from audiobookdl import Chapter
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.output import output
from audiobookdl.output.metadata import ffmpeg as ffmpeg_metadata
from audiobookdl.utils import fs


def test_replace_with_retry_recovers_from_transient_permission_error(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"payload")
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(s, d):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(s, d)

    with patch("audiobookdl.utils.fs.os.replace", side_effect=flaky_replace), patch(
        "audiobookdl.utils.fs.time.sleep"
    ):
        fs.replace_with_retry(str(src), str(dst), base_delay=0.001)
    assert dst.read_bytes() == b"payload"
    assert calls["count"] == 3


def test_replace_with_retry_reraises_persistent_permission_error(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"payload")

    with patch(
        "audiobookdl.utils.fs.os.replace",
        side_effect=PermissionError(13, "Access is denied"),
    ), patch("audiobookdl.utils.fs.time.sleep"), pytest.raises(PermissionError):
        fs.replace_with_retry(str(src), str(dst), attempts=3, base_delay=0.001)
    assert src.exists()


def test_replace_with_retry_does_not_retry_other_oserror(tmp_path):
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"payload")

    with patch(
        "audiobookdl.utils.fs.os.replace",
        side_effect=OSError(2, "No such file or directory"),
    ), pytest.raises(OSError):
        fs.replace_with_retry(str(src), str(dst))


def test_atomic_write_bytes_roundtrip(tmp_path):
    target = tmp_path / "cover.jpg"
    fs.atomic_write_bytes(str(target), b"\xff\xd8\xff\xe0fake")
    assert target.read_bytes() == b"\xff\xd8\xff\xe0fake"
    leftovers = [p for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == []


def test_fix_output_strips_colon_on_all_platforms():
    assert output._fix_output("Title: Subtitle") == "Title Subtitle"
    assert output._fix_output("AC/DC: Live") == "AC-DC Live"


def test_fix_output_preserves_apostrophes_on_windows(monkeypatch):
    monkeypatch.setattr(output.platform, "system", lambda: "Windows")
    assert output._fix_output("It’s John's Book") == "It’s John's Book"
    # Windows-illegal characters are still removed
    assert output._fix_output('a*b?c<d>e|f"g') == "abcdefg"
    # Reserved device names still sanitized
    assert output._fix_output("CON") == "CON_"


def test_fix_output_posix_still_strips_colon(monkeypatch):
    monkeypatch.setattr(output.platform, "system", lambda: "Linux")
    assert output._fix_output("Title: Subtitle") == "Title Subtitle"
    assert output._fix_output("It's") == "It's"


def test_sidecar_writers_produce_files(tmp_path):
    from audiobookdl import AudiobookMetadata, Chapter, Cover
    from audiobookdl.output.metadata import sidecars

    meta = AudiobookMetadata("Title", authors=["Author"])
    chapters = [Chapter(start=0, title="One"), Chapter(start=5000, title="Two")]

    cover_path = tmp_path / "cover.jpg"
    sidecars.write_sidecar_cover(Cover(image=b"\xff\xd8\xff", extension="jpg"), str(cover_path))
    assert cover_path.read_bytes() == b"\xff\xd8\xff"

    opf_path = tmp_path / "book.opf"
    sidecars.write_sidecar_opf(meta, str(opf_path), cover_filename="cover.jpg")
    assert "<dc:title>Title</dc:title>" in opf_path.read_text(encoding="utf-8")

    cue_path = tmp_path / "book.cue"
    sidecars.write_sidecar_cue(meta, "book.mp3", chapters, str(cue_path))
    assert 'FILE "book.mp3" MP3' in cue_path.read_text(encoding="utf-8")

    abs_path = tmp_path / "metadata.json"
    sidecars.write_sidecar_abs_metadata(meta, str(abs_path), chapters=chapters)
    assert '"title": "Title"' in abs_path.read_text(encoding="utf-8")

    # No atomic-write temp files left behind
    assert [p.name for p in tmp_path.iterdir()] == [
        "book.cue",
        "book.opf",
        "cover.jpg",
        "metadata.json",
    ]


def test_split_single_chapter_removes_partial_output_on_cancel(tmp_path):
    master = tmp_path / "master.m4a"
    master.write_bytes(b"fake")
    out_path = tmp_path / "01 - Ch.m4a"
    cancel = threading.Event()
    cancel.set()

    def fake_run(cmd, cancel_event=None, **kwargs):
        # Simulate ffmpeg leaving a partial file, then cancellation unwinding
        out_path.write_bytes(b"partial")
        raise DownloadCancelled

    with patch(
        "audiobookdl.output.output.run_cancellable_subprocess",
        side_effect=fake_run,
    ), pytest.raises(DownloadCancelled):
        output._split_single_chapter(
            ffmpeg="ffmpeg",
            master_path=str(master),
            out_path=str(out_path),
            start_sec=0.0,
            duration_sec=10.0,
            chapter_index=0,
            chapter_title="Ch",
            fast_codec_args=["-c", "copy"],
            reencode_codec_args=["-c:a", "aac"],
            format_args=[],
            accurate=True,
            cancel_event=cancel,
        )
    assert not out_path.exists()


def _fake_ffmpeg_result(cmd, cancel_event=None, **kwargs):
    from types import SimpleNamespace

    out_file = cmd[-1]
    with open(out_file, "wb") as fh:
        fh.write(b"remuxed-bytes")
    return SimpleNamespace(returncode=0, stderr=b"")


def test_add_chapters_ffmpeg_replaces_file_on_success(tmp_path):
    from types import SimpleNamespace

    audio = tmp_path / "book.m4a"
    audio.write_bytes(b"original")

    with patch.object(ffmpeg_metadata.utils, "find_executable", return_value="ffmpeg"), patch.object(
        ffmpeg_metadata, "run_cancellable_subprocess", side_effect=_fake_ffmpeg_result
    ), patch.object(
        ffmpeg_metadata.utils, "probe_audio_info", return_value=SimpleNamespace(duration=10.0)
    ), patch(
        "audiobookdl.output.output.verify_audio_integrity", return_value=SimpleNamespace()
    ):
        ffmpeg_metadata.add_chapters_ffmpeg(
            str(audio), [Chapter(start=0, title="One")]
        )
    assert audio.read_bytes() == b"remuxed-bytes"
    # No predictable/predictable-suffix temp files remain
    assert [p.name for p in tmp_path.iterdir()] == ["book.m4a"]


def test_add_chapters_ffmpeg_keeps_original_when_integrity_fails(tmp_path):
    from types import SimpleNamespace

    from audiobookdl.exceptions import FailedCombining

    audio = tmp_path / "book.m4a"
    audio.write_bytes(b"original-valid")

    with patch.object(ffmpeg_metadata.utils, "find_executable", return_value="ffmpeg"), patch.object(
        ffmpeg_metadata, "run_cancellable_subprocess", side_effect=_fake_ffmpeg_result
    ), patch.object(
        ffmpeg_metadata.utils, "probe_audio_info", return_value=SimpleNamespace(duration=10.0)
    ), patch(
        "audiobookdl.output.output.verify_audio_integrity",
        side_effect=FailedCombining(message="truncated"),
    ):
        ffmpeg_metadata.add_chapters_ffmpeg(
            str(audio), [Chapter(start=0, title="One")]
        )
    # Original file is preserved and the unverified remux is cleaned up
    assert audio.read_bytes() == b"original-valid"
    assert [p.name for p in tmp_path.iterdir()] == ["book.m4a"]


def test_add_chapters_ffmpeg_no_ffmpeg_is_noop(tmp_path):
    audio = tmp_path / "book.m4a"
    audio.write_bytes(b"original")
    with patch.object(ffmpeg_metadata.utils, "find_executable", return_value=None):
        ffmpeg_metadata.add_chapters_ffmpeg(str(audio), [Chapter(start=0, title="One")])
    assert audio.read_bytes() == b"original"

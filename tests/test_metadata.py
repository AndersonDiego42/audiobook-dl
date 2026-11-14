from io import BytesIO
import shutil
import subprocess
from unittest.mock import MagicMock

from PIL import Image
from mutagen.id3 import ID3, CTOCFlags
from mutagen.mp4 import MP4, MP4Cover
import pytest

from audiobookdl import AudiobookMetadata, Chapter, Cover
from audiobookdl.output.metadata import ffmpeg as ffmeta
from audiobookdl.output.metadata import id3, mp4


def _make_image_bytes(fmt: str, mode: str = "RGB") -> bytes:
    buf = BytesIO()
    color = (255, 0, 0, 128) if "A" in mode else (255, 0, 0)
    Image.new(mode, (4, 4), color).save(buf, format=fmt)
    return buf.getvalue()


def _create_silent_audio(path, codec_args):
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "2",
            *codec_args,
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def test_ffmetadata_escapes_special_characters():
    text = ffmeta.create_chapter_text(
        r"Ch 1: A = B; C # D \ E" + "\r\nLine 2",
        start=0,
        end=1000,
    )
    assert (
        r"title=Ch 1: A \= B\; C \# D \\ E" + "\\\nLine 2"
    ) in text


def test_create_tmp_chapter_file_with_special_chars_and_empty_list(monkeypatch):
    fake_file = MagicMock()
    fake_file.info.length = 5.5
    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: fake_file)

    assert ffmeta.create_tmp_chapter_file("ignored.m4b", []) == ";FFMETADATA1\n"

    content = ffmeta.create_tmp_chapter_file(
        "ignored.m4b",
        [
            Chapter(start=0, title="Part 1; A = B"),
            Chapter(start=2500, title="Part 2 # Finale"),
        ],
    )
    assert content.startswith(";FFMETADATA1\n")
    assert r"title=Part 1\; A \= B" in content
    assert r"title=Part 2 \# Finale" in content
    assert "END=5500\n" in content


def test_extension_matching_is_case_insensitive():
    assert id3.is_id3_file("book.mp3")
    assert id3.is_id3_file("BOOK.MP3")
    assert not id3.is_id3_file("book.m4b")

    assert mp4.is_mp4_file("book.m4b")
    assert mp4.is_mp4_file("BOOK.M4B")
    assert mp4.is_mp4_file("BOOK.M4A")
    assert not mp4.is_mp4_file("book.mp3")


def test_mp4_metadata_publisher_and_webp_cover(tmp_path):
    m4b_path = tmp_path / "sample.M4B"
    _create_silent_audio(m4b_path, ["-c:a", "aac"])

    metadata = AudiobookMetadata(
        title="Sample",
        authors=["Author One"],
        publisher="Acme Publishing",
    )
    mp4.add_mp4_metadata(str(m4b_path), metadata)
    mp4.embed_mp4_cover(
        str(m4b_path),
        Cover(image=_make_image_bytes("WEBP"), extension="webp"),
    )

    tags = MP4(str(m4b_path)).tags
    assert tags["\xa9pub"] == ["Acme Publishing"]
    assert len(tags["covr"]) == 1
    assert isinstance(tags["covr"][0], MP4Cover)
    assert tags["covr"][0].imageformat == MP4Cover.FORMAT_JPEG


def test_id3_cover_type_3_webp_conversion_and_ctoc_chap_frames(tmp_path):
    mp3_path = tmp_path / "sample.MP3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])

    metadata = AudiobookMetadata(title="MP3 Book", authors=["Author"])
    id3.add_id3_metadata(str(mp3_path), metadata)
    id3.embed_id3_cover(
        str(mp3_path),
        Cover(image=_make_image_bytes("WEBP", mode="RGBA"), extension="webp"),
    )
    id3.add_id3_chapters(
        str(mp3_path),
        [
            Chapter(start=0, title="Intro"),
            Chapter(start=1000, title="Chapter 2"),
        ],
    )

    tags = ID3(str(mp3_path))
    apics = tags.getall("APIC")
    assert len(apics) == 1
    assert apics[0].type == 3
    assert apics[0].mime == "image/png"

    ctocs = tags.getall("CTOC")
    assert len(ctocs) == 1
    assert ctocs[0].flags == (CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED)
    assert ctocs[0].child_element_ids == ["chp1", "chp2"]

    chaps = tags.getall("CHAP")
    assert [c.element_id for c in chaps] == ["chp1", "chp2"]
    assert isinstance(chaps[1].end_time, int)
    assert chaps[1].end_time >= 1000

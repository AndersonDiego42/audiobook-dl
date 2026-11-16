from datetime import date
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from collections.abc import Sequence
from unittest.mock import MagicMock

from PIL import Image
from mutagen import MutagenError
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import ID3, CTOCFlags
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
import pycountry
import pytest

from audiobookdl import AudiobookMetadata, Chapter, Cover
from audiobookdl.output import metadata as output_metadata
from audiobookdl.output.metadata import _chapters, ffmpeg as ffmeta
from audiobookdl.output.metadata import id3, mp4, sidecars, vorbis


def _make_image_bytes(fmt: str, mode: str = "RGB") -> bytes:
    buf = BytesIO()
    color = (255, 0, 0, 128) if "A" in mode else (255, 0, 0)
    Image.new(mode, (4, 4), color).save(buf, format=fmt)
    return buf.getvalue()


def _create_silent_audio(path: Path, codec_args: Sequence[str]) -> None:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        pytest.skip("ffmpeg not available")
    assert ffmpeg_bin is not None
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


def test_mp4_metadata_standard_atoms_and_no_runtime_class_mutation(tmp_path):
    m4b_path = tmp_path / "full.m4b"
    _create_silent_audio(m4b_path, ["-c:a", "aac"])

    before_get = set(EasyMP4Tags.Get.keys())
    before_set = set(EasyMP4Tags.Set.keys())

    metadata = AudiobookMetadata(
        title="Book One",
        scrape_url="https://example.com/book/1",
        series="Epic Saga",
        series_order=3,
        authors=["Author A"],
        narrators=["Narrator B"],
        genres=["Fantasy"],
        language=pycountry.languages.get(alpha_3="eng"),
        description="A long description of the audiobook.",
        isbn="9781234567890",
        publisher="Acme Publishing",
        release_date=date(2024, 6, 15),
    )
    mp4.add_mp4_metadata(str(m4b_path), metadata)

    assert set(EasyMP4Tags.Get.keys()) == before_get
    assert set(EasyMP4Tags.Set.keys()) == before_set

    tags = MP4(str(m4b_path)).tags
    assert tags is not None
    assert tags["stik"] == [2]
    assert tags["\xa9nam"] == ["Book One"]
    assert tags["\xa9alb"] == ["Epic Saga"]
    assert tags["\xa9ART"] == ["Author A"]
    assert tags["\xa9nrt"] == ["Narrator B"]
    assert tags["\xa9pub"] == ["Acme Publishing"]
    assert tags["\xa9gen"] == ["Fantasy"]
    assert tags["\xa9day"] == ["2024-06-15"]
    assert tags["yrrc"] == ["2024"]
    assert tags["desc"] == ["A long description of the audiobook."]
    assert tags["ldes"] == ["A long description of the audiobook."]
    assert tags["trkn"] == [(3, 0)]
    assert "\xa9trk" not in tags
    assert tags["----:com.apple.iTunes:URL"] == [MP4FreeForm(b"https://example.com/book/1")]
    assert tags["----:com.apple.iTunes:Language"] == [MP4FreeForm(b"eng")]
    assert tags["----:com.apple.iTunes:ISBN"] == [MP4FreeForm(b"9781234567890")]


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

    # Re-embedding a cover replaces APIC rather than duplicating it
    new_jpeg = _make_image_bytes("JPEG")
    id3.embed_id3_cover(str(mp3_path), Cover(image=new_jpeg, extension="jpg"))
    apics_after = ID3(str(mp3_path)).getall("APIC")
    assert len(apics_after) == 1
    assert apics_after[0].mime == "image/jpeg"
    assert apics_after[0].data == new_jpeg

    ctocs = tags.getall("CTOC")
    assert len(ctocs) == 1
    assert ctocs[0].flags == (CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED)
    assert ctocs[0].child_element_ids == ["chp1", "chp2"]

    chaps = tags.getall("CHAP")
    assert [c.element_id for c in chaps] == ["chp1", "chp2"]
    assert isinstance(chaps[1].end_time, int)
    assert chaps[1].end_time >= 1000


def test_create_tmp_chapter_file_falls_back_to_ffprobe_when_mutagen_fails(monkeypatch):
    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: None)
    monkeypatch.setattr(_chapters, "ffprobe_duration", lambda _: 6.0)

    content = ffmeta.create_tmp_chapter_file(
        "unknown.mka",
        [
            Chapter(start=0, title="Ch 1"),
            Chapter(start=2000, title="Ch 2"),
        ],
    )
    assert "START=0\nEND=2000\ntitle=Ch 1" in content
    assert "START=2000\nEND=6000\ntitle=Ch 2" in content

    # Mutagen raising MutagenError or returning info=None also falls back
    def raising_mutagen(_):
        raise MutagenError("bad header")

    monkeypatch.setattr(ffmeta, "MutagenFile", raising_mutagen)
    content_err = ffmeta.create_tmp_chapter_file(
        "corrupt.m4b",
        [Chapter(start=0, title="Only")],
    )
    assert "START=0\nEND=6000\ntitle=Only" in content_err

    broken_info = MagicMock()
    broken_info.info = None
    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: broken_info)
    content_no_info = ffmeta.create_tmp_chapter_file(
        "noinfo.m4b",
        [Chapter(start=0, title="Only")],
    )
    assert "START=0\nEND=6000\ntitle=Only" in content_no_info

    # Both Mutagen and ffprobe failing returns header-only FFMETADATA1 cleanly
    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: None)
    monkeypatch.setattr(_chapters, "ffprobe_duration", lambda _: 0.0)
    assert (
        ffmeta.create_tmp_chapter_file("missing.m4b", [Chapter(start=0, title="Ch 1")])
        == ";FFMETADATA1\n"
    )


def test_create_tmp_chapter_file_sorts_clamps_and_skips_invalid_bounds(monkeypatch):
    fake_file = MagicMock()
    fake_file.info.length = 5.0
    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: fake_file)

    content = ffmeta.create_tmp_chapter_file(
        "ignored.m4b",
        [
            Chapter(start=7000, title="Too Late 2"),
            Chapter(start=3000, title="Ch 2"),
            Chapter(start=5000, title="Exact End"),
            Chapter(start=-250, title="Ch 1"),
            Chapter(start=3000, title="Zero Mid"),
        ],
    )
    # Ordered by start: -250 -> 0 ("Ch 1"), 3000 ("Ch 2"), 3000 ("Zero Mid"), 5000, 7000
    assert "START=0\nEND=3000\ntitle=Ch 1" in content
    assert "Zero Mid" in content
    assert "Ch 2" not in content
    assert "START=3000\nEND=5000\ntitle=Zero Mid" in content
    assert "Exact End" not in content
    assert "Too Late 2" not in content


def test_id3_chapters_mutagen_none_fallback_and_bounds(tmp_path, monkeypatch):
    mp3_path = tmp_path / "bounded.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])

    monkeypatch.setattr(id3, "MutagenFile", lambda _: None)
    monkeypatch.setattr(_chapters, "ffprobe_duration", lambda _: 2.0)

    id3.add_id3_chapters(
        str(mp3_path),
        [
            Chapter(start=5000, title="Out of range"),
            Chapter(start=1000, title="Second"),
            Chapter(start=-100, title="First"),
        ],
    )

    tags = ID3(str(mp3_path))
    ctocs = tags.getall("CTOC")
    assert len(ctocs) == 1
    assert ctocs[0].child_element_ids == ["chp1", "chp2"]

    chaps = tags.getall("CHAP")
    assert [(c.element_id, c.start_time, c.end_time) for c in chaps] == [
        ("chp1", 0, 1000),
        ("chp2", 1000, 2000),
    ]

    # When both Mutagen and ffprobe fail, add_id3_chapters skips without crashing
    monkeypatch.setattr(_chapters, "ffprobe_duration", lambda _: 0.0)
    id3.add_id3_chapters(str(mp3_path), [Chapter(start=0, title="Ignored")])
    tags_after = ID3(str(mp3_path))
    assert len(tags_after.getall("CHAP")) == 2


def test_add_chapters_ffmpeg_succeeds_with_ffprobe_fallback_and_clamped_chapters(
    tmp_path, monkeypatch
):
    m4b_path = tmp_path / "chapters.m4b"
    _create_silent_audio(m4b_path, ["-c:a", "aac"])

    monkeypatch.setattr(ffmeta, "MutagenFile", lambda _: None)
    ffmeta.add_chapters_ffmpeg(
        str(m4b_path),
        [
            Chapter(start=99999, title="Out Of Range"),
            Chapter(start=1000, title="Second"),
            Chapter(start=0, title="First"),
        ],
    )

    mp4_file = MP4(str(m4b_path))
    assert mp4_file.chapters is not None
    assert [c.title for c in mp4_file.chapters] == ["First", "Second"]
    assert mp4_file.chapters[0].start == pytest.approx(0.0, abs=0.05)
    assert mp4_file.chapters[1].start == pytest.approx(1.0, abs=0.05)


def test_add_chapters_ffmpeg_fallback_respects_container_bitrate_and_os_replace(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ffmeta.utils, "find_executable", lambda name: name)
    monkeypatch.setattr(ffmeta, "create_tmp_chapter_file", lambda fp, ch: ";FFMETADATA1\n")

    def run_fallback_test(filename: str, audio_bitrate, probed_bps=None):
        target = tmp_path / filename
        target.write_bytes(b"original-audio")
        cmds = []

        def fake_subprocess(cmd, **kwargs):
            cmds.append(cmd)
            if "-c" in cmd and "copy" in cmd:
                return SimpleNamespace(returncode=1)
            Path(cmd[-1]).write_bytes(b"reencoded-audio")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(ffmeta, "run_cancellable_subprocess", fake_subprocess)
        monkeypatch.setattr(ffmeta.utils, "ffprobe_audio_bitrate", lambda _p: probed_bps)

        replace_calls = []
        real_replace = ffmeta.os.replace

        def spy_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr(ffmeta.os, "replace", spy_replace)

        ffmeta.add_chapters_ffmpeg(
            str(target),
            [Chapter(0, "Ch 1")],
            audio_bitrate=audio_bitrate,
        )
        assert target.read_bytes() == b"reencoded-audio"
        assert len(replace_calls) == 1
        assert len(cmds) == 2
        return cmds[1]

    # MP4 container with explicit bitrate
    m4b_explicit = run_fallback_test("book.m4b", "192k", probed_bps=64000)
    assert m4b_explicit[m4b_explicit.index("-c:a") + 1] == "aac"
    assert m4b_explicit[m4b_explicit.index("-b:a") + 1] == "192k"

    # MP4 container in auto mode probes source bitrate
    m4b_auto = run_fallback_test("auto.m4b", None, probed_bps=96000)
    assert m4b_auto[m4b_auto.index("-c:a") + 1] == "aac"
    assert m4b_auto[m4b_auto.index("-b:a") + 1] == "96k"

    # Non-MP4 lossy container omits -c:a aac and applies explicit bitrate when provided
    opus_explicit = run_fallback_test("book.opus", "192k")
    assert "-c:a" not in opus_explicit
    assert opus_explicit[opus_explicit.index("-b:a") + 1] == "192k"

    # Non-MP4 lossy container in auto mode omits -c:a and -b:a
    mka_auto = run_fallback_test("book.mka", None)
    assert "-c:a" not in mka_auto
    assert "-b:a" not in mka_auto

    # Lossless container omits both -c:a and -b:a even if bitrate was configured
    flac_cmd = run_fallback_test("book.flac", "192k")
    assert "-c:a" not in flac_cmd
    assert "-b:a" not in flac_cmd


def test_add_chapters_ffmpeg_preserves_original_when_fallback_nonzero(
    tmp_path, monkeypatch
):
    target = tmp_path / "keep.m4b"
    target.write_bytes(b"intact-original")
    monkeypatch.setattr(ffmeta.utils, "find_executable", lambda name: name)
    monkeypatch.setattr(ffmeta, "create_tmp_chapter_file", lambda fp, ch: ";FFMETADATA1\n")

    def failing_both(cmd, **kwargs):
        # Even if ffmpeg wrote partial bytes before returning non-zero, do not replace original
        Path(cmd[-1]).write_bytes(b"corrupt-partial")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ffmeta, "run_cancellable_subprocess", failing_both)
    ffmeta.add_chapters_ffmpeg(str(target), [Chapter(0, "Ch 1")], audio_bitrate="128k")
    assert target.read_bytes() == b"intact-original"


def test_add_id3_metadata_sets_4_digit_tyer_and_full_iso_tdrc_tdor(
    tmp_path, monkeypatch
):
    mp3_path = tmp_path / "release_date.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])

    recorded_easy_keys = {}
    real_mp3 = id3.MP3

    def spy_mp3(path, **kwargs):
        audio = real_mp3(path, **kwargs)
        orig_save = audio.save

        def capture_save(*args, **save_kwargs):
            recorded_easy_keys["year"] = [str(x) for x in audio["TYER"].text]
            recorded_easy_keys["date"] = [str(x) for x in audio["TDRC"].text]
            recorded_easy_keys["originaldate"] = [str(x) for x in audio["TDOR"].text]
            return orig_save(*args, **save_kwargs)

        audio.save = capture_save
        return audio

    monkeypatch.setattr(id3, "MP3", spy_mp3)

    metadata = AudiobookMetadata(
        title="Dated Book",
        authors=["Author"],
        release_date=date(2024, 6, 15),
    )
    id3.add_id3_metadata(str(mp3_path), metadata)

    assert recorded_easy_keys == {
        "year": ["2024"],
        "date": ["2024-06-15"],
        "originaldate": ["2024-06-15"],
    }
    tags = ID3(str(mp3_path))
    assert str(tags["TDRC"]) == "2024-06-15"
    assert str(tags["TDOR"]) == "2024-06-15"


def test_add_chapters_forwards_audio_bitrate_to_ffmpeg(monkeypatch):
    seen = {}
    monkeypatch.setattr(output_metadata.id3, "is_id3_file", lambda _p: False)
    monkeypatch.setattr(output_metadata, "program_in_path", lambda _name: True)

    def fake_add_chapters_ffmpeg(filepath, chapters, cancel_event=None, audio_bitrate=None):
        seen["filepath"] = filepath
        seen["audio_bitrate"] = audio_bitrate

    monkeypatch.setattr(output_metadata.ffmpeg, "add_chapters_ffmpeg", fake_add_chapters_ffmpeg)
    output_metadata.add_chapters("book.m4b", [Chapter(0, "Ch 1")], audio_bitrate="192k")
    assert seen == {"filepath": "book.m4b", "audio_bitrate": "192k"}


@pytest.mark.parametrize(
    ("ext", "codec_args"),
    [
        ("flac", ["-c:a", "flac"]),
        ("ogg", ["-c:a", "libvorbis"]),
        ("opus", ["-c:a", "libopus"]),
    ],
)
def test_vorbis_metadata_and_cover_roundtrip(tmp_path, ext, codec_args):
    audio_path = tmp_path / f"sample.{ext}"
    _create_silent_audio(audio_path, codec_args)

    meta = AudiobookMetadata(
        title="Vorbis Book",
        authors=["Author A", "Author B"],
        narrators=["Narrator X"],
        series="Saga",
        series_order=2,
        genres=["Sci-Fi"],
        language=pycountry.languages.get(alpha_2="en"),
        description="A sample description",
        isbn="9781234567890",
        publisher="Press",
        release_date=date(2023, 5, 10),
        scrape_url="https://example.com/book",
    )
    output_metadata.add_metadata(str(audio_path), meta)
    png_bytes = _make_image_bytes("PNG", mode="RGB")
    output_metadata.embed_cover(str(audio_path), Cover(png_bytes, "png"))

    from mutagen import File as MutagenFile
    reloaded = MutagenFile(str(audio_path))
    assert reloaded is not None
    assert reloaded["title"] == ["Vorbis Book"]
    assert reloaded["artist"] == ["Author A", "Author B"]
    assert reloaded["narrator"] == ["Narrator X"]
    assert reloaded["album"] == ["Saga"]
    assert reloaded["series-part"] == ["2"]
    assert reloaded["language"] == ["eng"]
    assert reloaded["date"] == ["2023-05-10"]
    assert reloaded["isbn"] == ["9781234567890"]
    if ext == "flac":
        assert len(reloaded.pictures) == 1
        assert reloaded.pictures[0].mime == "image/png"
    else:
        assert "metadata_block_picture" in reloaded


def test_sidecars_opf_cue_and_enriched_as_json(tmp_path):
    meta = AudiobookMetadata(
        title='Book "One" & More',
        authors=["Jane Doe"],
        narrators=["John Smith"],
        series="Chronicles",
        series_order=3,
        genres=["Fiction"],
        language=pycountry.languages.get(alpha_2="en"),
        description="Summary & notes",
        isbn="9780000000001",
        publisher="AudioHouse",
        release_date=date(2022, 11, 1),
    )
    chapters = [
        Chapter(start=0, title="Prologue"),
        Chapter(start=65_400, title='Chapter "1"'),
    ]

    import json
    parsed_json = json.loads(meta.as_json(chapters=chapters))
    assert parsed_json["series"] == ["Chronicles #3"]
    assert parsed_json["series_order"] == 3
    assert parsed_json["language"] == "eng"
    assert len(parsed_json["chapters"]) == 2
    assert parsed_json["chapters"][1]["start_seconds"] == 65.4

    opf_path = tmp_path / "metadata.opf"
    sidecars.write_sidecar_opf(meta, str(opf_path), cover_filename="cover.jpg")
    opf_text = opf_path.read_text(encoding="utf-8")
    assert "Book &quot;One&quot; &amp; More" in opf_text or "Book \"One\" &amp; More" in opf_text
    assert 'calibre:series" content="Chronicles"' in opf_text
    assert 'calibre:series_index" content="3"' in opf_text
    assert 'href="cover.jpg"' in opf_text

    cue_path = tmp_path / "Book.cue"
    sidecars.write_sidecar_cue(meta, "Book.mp3", chapters, str(cue_path))
    cue_text = cue_path.read_text(encoding="utf-8")
    assert 'PERFORMER "Jane Doe"' in cue_text
    assert 'FILE "Book.mp3" MP3' in cue_text
    assert "TRACK 02 AUDIO" in cue_text
    assert "INDEX 01 01:05:30" in cue_text


def test_id3_metadata_persists_source_and_isbn_txxx_frames(tmp_path):
    mp3_path = tmp_path / "tagged.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])

    metadata = AudiobookMetadata(
        title="Tagged Book",
        authors=["Author A"],
        isbn="9781234567890",
        source="storytel",
    )
    id3.add_id3_metadata(str(mp3_path), metadata)

    tags = ID3(str(mp3_path))
    assert list(tags["TXXX:ISBN"].text) == ["9781234567890"]
    assert list(tags["TXXX:Source"].text) == ["storytel"]


def test_audiobook_metadata_all_properties_and_dict_value_types():
    lang = pycountry.languages.get(alpha_2="en")
    meta = AudiobookMetadata(
        title="Dune",
        authors=["Frank Herbert", "Co-Author"],
        narrators=["Scott Brick"],
        genres=["Sci-Fi"],
        series="Dune Chronicles",
        series_order=1,
        language=lang,
        release_date=date(1965, 8, 1),
        source="storytel",
    )

    props_dict = meta.all_properties_dict()
    assert props_dict["series_order"] == 1
    assert isinstance(props_dict["series_order"], int)
    assert props_dict["release_date"] == date(1965, 8, 1)
    assert isinstance(props_dict["language"], pycountry.db.Data)
    assert props_dict["source"] == "storytel"
    assert props_dict["author"] == "Frank Herbert; Co-Author"

    grouped = dict(meta.all_properties(allow_duplicate_keys=None))
    assert grouped["authors"] == ["Frank Herbert", "Co-Author"]
    assert grouped["narrators"] == ["Scott Brick"]
    assert grouped["genres"] == ["Sci-Fi"]

    multi = meta.all_properties(allow_duplicate_keys=True)
    assert [v for k, v in multi if k == "author"] == ["Frank Herbert", "Co-Author"]


def test_single_save_pass_for_mp4_metadata_and_id3_chapters_cover(tmp_path):
    from unittest.mock import patch
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from audiobookdl import Chapter

    mp4_path = tmp_path / "book.m4b"
    _create_silent_audio(mp4_path, ["-c:a", "aac", "-f", "mp4"])
    meta = AudiobookMetadata(title="Single Save M4B", authors=["Author"])
    cover = Cover(_make_image_bytes("JPEG", mode="RGB"), "jpg")

    save_calls = []
    orig_mp4_save = MP4.save

    def tracking_mp4_save(self, *args, **kwargs):
        save_calls.append(1)
        return orig_mp4_save(self, *args, **kwargs)

    with patch.object(MP4, "save", tracking_mp4_save):
        embedded = output_metadata.add_metadata(str(mp4_path), meta, cover=cover)
    assert embedded is True
    assert len(save_calls) == 1
    reloaded = MP4(str(mp4_path))
    assert reloaded["\xa9nam"] == ["Single Save M4B"]
    assert len(reloaded["covr"]) == 1

    mp3_path = tmp_path / "book.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])
    id3_save_calls = []
    orig_id3_save = ID3.save

    def tracking_id3_save(self, *args, **kwargs):
        id3_save_calls.append(1)
        return orig_id3_save(self, *args, **kwargs)

    with patch.object(ID3, "save", tracking_id3_save):
        id3_embedded = output_metadata.add_chapters(
            str(mp3_path),
            [Chapter(0, "Opening"), Chapter(1000, "Middle")],
            cover=cover,
        )
    assert id3_embedded is True
    assert len(id3_save_calls) == 1
    tags = ID3(str(mp3_path))
    assert len(tags.getall("APIC")) == 1
    assert len(tags.getall("CHAP")) == 2


def test_asin_metadata_across_mp4_id3_vorbis_opf_json_and_template(tmp_path):
    import json
    from mutagen.flac import FLAC
    from audiobookdl.output import output as out_mod

    meta = AudiobookMetadata(
        title="ASIN Book",
        authors=["Jane Doe"],
        asin="B08G9PRS1K",
    )
    assert meta.all_properties_dict()["asin"] == "B08G9PRS1K"
    assert json.loads(meta.as_json())["asin"] == "B08G9PRS1K"
    assert out_mod.gen_output_location("{title} [{asin}]", meta, "") == "ASIN Book [B08G9PRS1K]"

    opf_path = tmp_path / "metadata.opf"
    sidecars.write_sidecar_opf(meta, str(opf_path))
    assert '<dc:identifier opf:scheme="ASIN">B08G9PRS1K</dc:identifier>' in opf_path.read_text(encoding="utf-8")

    mp4_path = tmp_path / "asin.m4b"
    _create_silent_audio(mp4_path, ["-c:a", "aac", "-f", "mp4"])
    mp4.add_mp4_metadata(str(mp4_path), meta)
    assert bytes(MP4(str(mp4_path))["----:com.apple.iTunes:ASIN"][0]) == b"B08G9PRS1K"

    mp3_path = tmp_path / "asin.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])
    id3.add_id3_metadata(str(mp3_path), meta)
    assert list(ID3(str(mp3_path))["TXXX:ASIN"].text) == ["B08G9PRS1K"]

    flac_path = tmp_path / "asin.flac"
    _create_silent_audio(flac_path, ["-c:a", "flac"])
    vorbis.add_vorbis_metadata(str(flac_path), meta)
    assert FLAC(str(flac_path))["asin"] == ["B08G9PRS1K"]


def test_cover_square_padding_and_max_size_normalization():
    from io import BytesIO
    from PIL import Image
    from audiobookdl import Cover
    from audiobookdl.output.metadata._cover import normalize_cover_image

    buf = BytesIO()
    Image.new("RGB", (400, 200), (120, 60, 30)).save(buf, format="JPEG")
    cover = Cover(image=buf.getvalue(), extension="jpg")

    data, ext = normalize_cover_image(cover, square=True, max_size=128)
    assert ext == "jpg"
    with Image.open(BytesIO(data)) as out_img:
        assert out_img.size == (128, 128)


def test_audiobook_metadata_normalizes_language_and_release_date_and_serializes_cleanly(tmp_path):
    from audiobookdl.output import output as out_mod
    from audiobookdl.utils.audiobook import parse_language, parse_release_date

    # parse_language resolves 2-letter, 3-letter, bibliographic, locale, English, and localized names
    assert parse_language("en") == pycountry.languages.get(alpha_3="eng")
    assert parse_language("en-US") == pycountry.languages.get(alpha_3="eng")
    assert parse_language("eng") == pycountry.languages.get(alpha_3="eng")
    assert parse_language("ger") == pycountry.languages.get(alpha_3="deu")
    assert parse_language("English") == pycountry.languages.get(alpha_3="eng")
    assert parse_language("Dansk") == pycountry.languages.get(alpha_3="dan")
    assert parse_language("Svenska") == pycountry.languages.get(alpha_3="swe")
    assert parse_language("   ") is None
    assert parse_language("UnknownLanguageToken") is None

    # parse_release_date handles ISO timestamps, partial dates, human dates, ints, and invalid inputs
    assert parse_release_date("2024") == date(2024, 1, 1)
    assert parse_release_date("2024-06") == date(2024, 6, 1)
    assert parse_release_date("2024-06-15T12:30:00Z") == date(2024, 6, 15)
    assert parse_release_date("May 4, 2021") == date(2021, 5, 4)
    assert parse_release_date("Tue, 02 Jan 2024 10:00:00 GMT") == date(2024, 1, 2)
    assert parse_release_date(1998) == date(1998, 1, 1)
    assert parse_release_date("not-a-date") is None

    # Field converters enforce contract both at construction and post-init assignment
    meta = AudiobookMetadata(
        title="Contract Book",
        authors=["Author A"],
        language="en",
        release_date="May 4, 2021",
    )
    assert meta.language == pycountry.languages.get(alpha_3="eng")
    assert meta.release_date == date(2021, 5, 4)

    meta.language = "Dansk"
    meta.release_date = "2020-05"
    assert meta.language == pycountry.languages.get(alpha_3="dan")
    assert meta.release_date == date(2020, 5, 1)

    # Unparseable sentinels coerce to None rather than leaving raw str to crash taggers
    meta.language = "Multi-Language"
    meta.release_date = "TBD"
    assert meta.language is None
    assert meta.release_date is None

    # Downstream taggers, OPF sidecar, and output template consume normalized metadata safely
    tagged_meta = AudiobookMetadata(
        title="Taggable Book",
        authors=["Author A"],
        language="en",
        release_date="2022-03-14",
    )
    assert out_mod.gen_output_location("{title} [{language}] ({year})", tagged_meta, "") == "Taggable Book [eng] (2022)"
    opf_path = tmp_path / "contract.opf"
    sidecars.write_sidecar_opf(tagged_meta, str(opf_path))
    opf_xml = opf_path.read_text(encoding="utf-8")
    assert "<dc:language>en</dc:language>" in opf_xml
    assert "<dc:date>2022-03-14</dc:date>" in opf_xml

    mp3_path = tmp_path / "contract.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])
    id3.add_id3_metadata(str(mp3_path), tagged_meta)
    assert list(ID3(str(mp3_path))["TLAN"].text) == ["eng"]
    assert str(ID3(str(mp3_path))["TDRC"]) == "2022-03-14"

    m4b_path = tmp_path / "contract.m4b"
    _create_silent_audio(m4b_path, ["-c:a", "aac", "-f", "mp4"])
    mp4.add_mp4_metadata(str(m4b_path), tagged_meta)
    assert MP4(str(m4b_path))["----:com.apple.iTunes:Language"] == [MP4FreeForm(b"eng")]
    assert MP4(str(m4b_path))["\xa9day"] == ["2022-03-14"]

    from audiobookdl.output.metadata.reader import read_track_tags

    reloaded = read_track_tags(str(m4b_path))
    assert reloaded.metadata.language == pycountry.languages.get(alpha_3="eng")
    assert reloaded.metadata.release_date == date(2022, 3, 14)


def test_series_order_zero_included_and_roundtripped(tmp_path):
    from audiobookdl.output.metadata.reader import read_track_tags

    meta = AudiobookMetadata(
        title="Prequel Story",
        series="Grand Saga",
        series_order=0,
        description="First line; contains semicolon; end",
    )
    props = dict(meta.all_properties())
    assert props["series_order"] == 0

    mp3_path = tmp_path / "prequel.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])
    output_metadata.add_metadata(str(mp3_path), meta)
    id3_tags = ID3(str(mp3_path))
    assert str(id3_tags["COMM::eng"]) == "First line; contains semicolon; end"
    assert str(id3_tags["COMM::eng"].encoding) == "Encoding.UTF8" or id3_tags["COMM::eng"].encoding == 3
    mp3_reloaded = read_track_tags(str(mp3_path))
    assert mp3_reloaded.metadata.series == "Grand Saga"
    assert mp3_reloaded.metadata.series_order == 0
    assert mp3_reloaded.metadata.description == "First line; contains semicolon; end"

    m4b_path = tmp_path / "prequel.m4b"
    _create_silent_audio(m4b_path, ["-c:a", "aac", "-f", "mp4"])
    output_metadata.add_metadata(str(m4b_path), meta)
    m4b_reloaded = read_track_tags(str(m4b_path))
    assert m4b_reloaded.metadata.series == "Grand Saga"
    assert m4b_reloaded.metadata.series_order == 0
    assert m4b_reloaded.metadata.description == "First line; contains semicolon; end"

    flac_path = tmp_path / "prequel.flac"
    _create_silent_audio(flac_path, ["-c:a", "flac"])
    output_metadata.add_metadata(str(flac_path), meta)
    flac_reloaded = read_track_tags(str(flac_path))
    assert flac_reloaded.metadata.series == "Grand Saga"
    assert flac_reloaded.metadata.series_order == 0
    assert flac_reloaded.metadata.description == "First line; contains semicolon; end"


def test_add_metadata_with_track_context_writes_track_and_preserves_series(tmp_path):
    from audiobookdl.output.metadata.reader import read_track_tags
    from audiobookdl.utils.audiobook import TrackTagContext

    meta = AudiobookMetadata(
        title="Full Novel",
        authors=["Author One"],
        series="Saga Name",
        series_order=2,
    )
    track_ctx = TrackTagContext(title="Chapter 2", track_number=2, total_tracks=5)

    for ext, codec_args in [
        ("mp3", ["-c:a", "libmp3lame"]),
        ("m4b", ["-c:a", "aac", "-f", "mp4"]),
        ("flac", ["-c:a", "flac"]),
    ]:
        path = tmp_path / f"part2.{ext}"
        _create_silent_audio(path, codec_args)
        output_metadata.add_metadata(str(path), meta, track_context=track_ctx)
        info = read_track_tags(str(path))
        assert info.title == "Chapter 2"
        assert info.album == "Full Novel"
        assert info.track_number == 2
        assert info.metadata.series == "Saga Name"
        assert info.metadata.series_order == 2


def test_add_id3_metadata_writes_tags_chapters_and_cover_in_single_save(tmp_path, monkeypatch):
    from audiobookdl import Cover

    mp3_path = tmp_path / "single_save.mp3"
    _create_silent_audio(mp3_path, ["-c:a", "libmp3lame"])

    save_calls = []
    real_mp3 = id3.MP3

    def spy_mp3(path, **kwargs):
        audio = real_mp3(path, **kwargs)
        orig_save = audio.save

        def count_save(*args, **save_kwargs):
            save_calls.append((args, save_kwargs))
            return orig_save(*args, **save_kwargs)

        audio.save = count_save
        return audio

    monkeypatch.setattr(id3, "MP3", spy_mp3)

    meta = AudiobookMetadata(title="Single Save", authors=["Author"])
    chapters = [Chapter(start=0, title="Intro")]
    cover = Cover(image=_make_image_bytes("PNG", mode="RGB"), extension="png")

    embedded = output_metadata.add_metadata(
        str(mp3_path),
        meta,
        cover=cover,
        chapters=chapters,
    )
    assert embedded is True
    assert len(save_calls) == 1
    tags = ID3(str(mp3_path))
    assert str(tags["TIT2"]) == "Single Save"
    assert tags.getall("APIC")
    assert tags.getall("CHAP")
    assert tags.getall("CTOC")


from datetime import date
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import time
from collections.abc import Sequence
from unittest.mock import patch

from PIL import Image
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPOS, TRCK
from mutagen.mp4 import MP4
import pytest

from audiobookdl import AudiobookMetadata, Chapter, Cover, args
from audiobookdl.__main__ import main as cli_main
from audiobookdl.config import Config
from audiobookdl.exceptions import GenericAudiobookDLException
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.output.metadata import id3, mp4, sidecars, vorbis
from audiobookdl.output.metadata.reader import (
    infer_metadata_from_path,
    read_sidecar_metadata,
    read_track_tags,
)
from audiobookdl.pack import (
    PackOverrides,
    discover_pack_groups,
    inspect_local_book,
    pack_book,
    run_pack,
    sort_track_paths,
)
from audiobookdl.utils import AudioProbeInfo


def _make_cover_bytes(fmt: str = "JPEG") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 210)).save(buf, format=fmt)
    return buf.getvalue()


def _ffmpeg_silent_file(path: Path, codec_args: Sequence[str], seconds: float = 1.0) -> None:
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
            str(seconds),
            *codec_args,
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def _empty_config() -> Config:
    return Config(
        sources={},
        output_template=None,
        database_directory=None,
        skip_downloaded=None,
    )


def _create_empty_id3_mp3(path: Path) -> None:
    # Minimal valid MPEG1 Layer III frame + empty ID3 header for offline mutagen unit tests
    frame = b"\xff\xfb\x90\x00" + (b"\x00" * 413)
    path.write_bytes(frame * 3)


def test_discover_pack_groups_single_recursive_and_errors(tmp_path: Path):
    book_a = tmp_path / "Book A"
    book_b = tmp_path / "Nested" / "Book 2"
    book_c = tmp_path / "Nested" / "Book 10"
    for folder in (book_a, book_b, book_c):
        folder.mkdir(parents=True)
        (folder / "01.mp3").write_bytes(b"audio")
        (folder / "notes.txt").write_text("ignore", encoding="utf-8")

    # Non-recursive folder with direct audio files
    groups_single = discover_pack_groups([str(book_a)])
    assert len(groups_single) == 1
    assert groups_single[0][0] == str(book_a.resolve())
    assert [Path(p).name for p in groups_single[0][1]] == ["01.mp3"]

    # Non-recursive parent folder without direct audio files hints at --recursive
    with pytest.raises(GenericAudiobookDLException, match="--recursive"):
        discover_pack_groups([str(tmp_path / "Nested")])

    # Recursive discovery walks subfolders in natural order
    groups_rec = discover_pack_groups([str(tmp_path)], recursive=True)
    assert [Path(root).name for root, _ in groups_rec] == ["Book A", "Book 2", "Book 10"]

    # Standalone explicit files become one group
    f1 = book_a / "01.mp3"
    groups_files = discover_pack_groups([str(f1)])
    assert len(groups_files) == 1
    assert groups_files[0][1] == [str(f1.resolve())]

    # Invalid paths or extensions
    with pytest.raises(GenericAudiobookDLException, match="not found"):
        discover_pack_groups([str(tmp_path / "missing")])

    txt_file = book_a / "notes.txt"
    with pytest.raises(GenericAudiobookDLException, match="Unsupported"):
        discover_pack_groups([str(txt_file)])


def test_sort_track_paths_prefers_disc_and_track_tags_over_filename(tmp_path: Path):
    f10 = tmp_path / "Part 10.mp3"
    f2 = tmp_path / "Part 2.mp3"
    f1 = tmp_path / "Part 1.mp3"
    for p in (f10, f2, f1):
        _create_empty_id3_mp3(p)

    # Without track tags: natural numeric filename order
    sorted_nat, _ = sort_track_paths([str(f10), str(f2), str(f1)])
    assert [Path(p).name for p in sorted_nat] == ["Part 1.mp3", "Part 2.mp3", "Part 10.mp3"]

    # Add explicit disc/track tags reversing filename appearance
    for path, disc, track in ((f10, "1", "1"), (f1, "1", "2"), (f2, "2", "1")):
        tags = ID3()
        tags.add(TPOS(encoding=3, text=[disc]))
        tags.add(TRCK(encoding=3, text=[track]))
        tags.save(str(path))

    sorted_tagged, _ = sort_track_paths([str(f2), str(f1), str(f10)])
    assert [Path(p).name for p in sorted_tagged] == ["Part 10.mp3", "Part 1.mp3", "Part 2.mp3"]


def test_read_track_tags_id3_mp4_vorbis_roundtrip(tmp_path: Path):
    cover_bytes = _make_cover_bytes("JPEG")
    cover = Cover(image=cover_bytes, extension="jpg")
    meta = AudiobookMetadata(
        "Project Hail Mary",
        authors=["Andy Weir"],
        narrators=["Ray Porter"],
        series="Standalone",
        series_order=1,
        genres=["Sci-Fi"],
        description="A lone astronaut wakes up.",
        publisher="Audible Studios",
        release_date=date(2021, 5, 4),
        isbn="9780593135204",
        asin="B08G9PRS1K",
    )

    # ID3 roundtrip
    mp3_path = tmp_path / "track.mp3"
    _create_empty_id3_mp3(mp3_path)
    id3.add_id3_metadata(str(mp3_path), meta)
    id3.embed_id3_cover(str(mp3_path), cover)
    id3_info = read_track_tags(str(mp3_path))
    assert id3_info.title == "Project Hail Mary"
    assert id3_info.album == "Standalone"
    assert id3_info.metadata.authors == ["Andy Weir"]
    assert id3_info.metadata.asin == "B08G9PRS1K"
    assert id3_info.metadata.isbn == "9780593135204"
    assert id3_info.cover is not None and id3_info.cover.extension == "jpg"

    # Optional real MP4 & FLAC roundtrip if ffmpeg exists
    if shutil.which("ffmpeg"):
        m4a_path = tmp_path / "track.m4a"
        _ffmpeg_silent_file(m4a_path, ["-c:a", "aac", "-b:a", "64k"], seconds=0.5)
        mp4.add_mp4_metadata(str(m4a_path), meta, cover=cover)
        mp4_info = read_track_tags(str(m4a_path))
        assert mp4_info.title == "Project Hail Mary"
        assert mp4_info.metadata.authors == ["Andy Weir"]
        assert mp4_info.metadata.narrators == ["Ray Porter"]
        assert mp4_info.metadata.asin == "B08G9PRS1K"
        assert mp4_info.cover is not None

        flac_path = tmp_path / "track.flac"
        _ffmpeg_silent_file(flac_path, ["-c:a", "flac"], seconds=0.5)
        vorbis.add_vorbis_metadata(str(flac_path), meta, cover=cover)
        vorbis_info = read_track_tags(str(flac_path))
        assert vorbis_info.title == "Project Hail Mary"
        assert vorbis_info.metadata.authors == ["Andy Weir"]
        assert vorbis_info.metadata.series == "Standalone"
        assert vorbis_info.cover is not None


def test_read_sidecars_and_path_heuristics(tmp_path: Path):
    book_dir = tmp_path / "Frank Herbert - Dune (1965)"
    book_dir.mkdir()
    cover_data = _make_cover_bytes("PNG")
    (book_dir / "Cover.png").write_bytes(cover_data)

    meta_in = AudiobookMetadata(
        "Dune",
        authors=["Frank Herbert"],
        narrators=["Scott Brick"],
        series="Dune Chronicles",
        series_order=1,
        asin="B002V1OF70",
    )
    sidecars.write_sidecar_opf(meta_in, str(book_dir / "metadata.opf"), cover_filename="Cover.png")
    (book_dir / "chapters.cue").write_text(
        sidecars.build_cue_sheet(
            "Dune",
            "Dune.m4b",
            [Chapter(0, "Book I"), Chapter(60_000, "Book II")],
            authors=["Frank Herbert"],
        ),
        encoding="utf-8",
    )

    meta_out, cover_out, chapters_out = read_sidecar_metadata(str(book_dir))
    assert meta_out is not None
    assert meta_out.title == "Dune"
    assert meta_out.authors == ["Frank Herbert"]
    assert meta_out.narrators == ["Scott Brick"]
    assert meta_out.series == "Dune Chronicles"
    assert meta_out.series_order == 1
    assert meta_out.asin == "B002V1OF70"
    assert cover_out is not None and cover_out.extension == "png"
    assert [(c.start, c.title) for c in chapters_out] == [(0, "Book I"), (60_000, "Book II")]

    inferred = infer_metadata_from_path(str(book_dir))
    assert inferred.authors == ["Frank Herbert"]
    assert inferred.title == "Dune"
    assert inferred.release_date == date(1965, 1, 1)


def test_inspect_local_book_chapters_metadata_and_overrides(tmp_path: Path):
    book_dir = tmp_path / "Some Author - Folder Title"
    book_dir.mkdir()
    t1 = book_dir / "01 - Opening.mp3"
    t2 = book_dir / "02 - Closing.mp3"
    for p in (t1, t2):
        _create_empty_id3_mp3(p)

    # Multi-track album tags where album is book title and TIT2 is chapter title
    for idx, (p, ch_title) in enumerate(((t1, "Prologue"), (t2, "Epilogue")), start=1):
        tags = ID3()
        tags.add(TRCK(encoding=3, text=[str(idx)]))
        tags.add(TALB(encoding=3, text=["Real Book Title"]))
        tags.add(TIT2(encoding=3, text=[ch_title]))
        tags.add(TPE1(encoding=3, text=["Real Author"]))
        tags.add(APIC(type=3, mime="image/jpeg", data=_make_cover_bytes("JPEG")))
        tags.save(str(p))

    out_dir = tmp_path / "packed"
    opts = args.build_default_options(output_template="{author}/{title}")

    def _fake_probe(path: str) -> AudioProbeInfo:
        dur = 12.5 if Path(path).name.startswith("01") else 7.5
        return AudioProbeInfo(duration=dur, bitrate=128_000, codec_name="mp3", format_name="mp3")

    with patch("audiobookdl.pack.probe_audio_info", side_effect=_fake_probe):
        spec = inspect_local_book(
            str(book_dir),
            [str(t2), str(t1)],
            opts,
            output_dir=str(out_dir),
        )
        assert spec.audiobook.title == "Real Book Title"
        assert spec.audiobook.metadata.authors == ["Real Author"]
        assert spec.output_format == "m4b"
        assert spec.output_path == str((out_dir / "Real Author" / "Real Book Title.m4b").resolve())
        assert [(c.start, c.title) for c in spec.audiobook.chapters] == [
            (0, "Prologue"),
            (12_500, "Epilogue"),
        ]
        assert spec.audiobook.cover is not None

        # Overrides take top priority
        custom_cover = tmp_path / "custom.png"
        custom_cover.write_bytes(_make_cover_bytes("PNG"))
        overrides = PackOverrides(
            title="Overridden Title",
            authors=["Custom Author"],
            series="My Series",
            series_order=3,
            asin="B0OVERRIDE1",
            cover_path=str(custom_cover),
        )
        spec_over = inspect_local_book(
            str(book_dir),
            [str(t1), str(t2)],
            opts,
            output_dir=str(out_dir),
            overrides=overrides,
        )
        assert spec_over.audiobook.title == "Overridden Title"
        assert spec_over.audiobook.metadata.authors == ["Custom Author"]
        assert spec_over.audiobook.metadata.series == "My Series"
        assert spec_over.audiobook.metadata.series_order == 3
        assert spec_over.audiobook.metadata.asin == "B0OVERRIDE1"
        assert spec_over.audiobook.cover is not None and spec_over.audiobook.cover.extension == "png"


def test_run_pack_dry_run_delete_source_and_failure_safety(tmp_path: Path):
    src_dir = tmp_path / "Input Author - Sample Book"
    src_dir.mkdir()
    t1 = src_dir / "01.mp3"
    t2 = src_dir / "02.mp3"
    for p in (t1, t2):
        _create_empty_id3_mp3(p)

    out_dir = tmp_path / "out"
    opts = args.build_default_options(output_template="{author} - {title}", overwrite=True)

    fake_probe = AudioProbeInfo(duration=5.0, bitrate=128_000, codec_name="mp3", format_name="mp3")

    with patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe):
        # 1. Dry-run leaves disk completely unchanged
        dry_summary = run_pack(
            [str(src_dir)],
            opts,
            _empty_config(),
            output_dir=str(out_dir),
            dry_run=True,
        )
        assert dry_summary.books_completed == 1
        assert dry_summary.output_paths == [str((out_dir / "Input Author - Sample Book.m4b").resolve())]
        assert not (out_dir / "Input Author - Sample Book.m4b").exists()
        assert t1.exists() and t2.exists()

        # 2. Failed combine never deletes source files even with delete_source=True
        with patch("audiobookdl.pack.output.combine_audiofiles", side_effect=RuntimeError("mux boom")):
            fail_summary = run_pack(
                [str(src_dir)],
                opts,
                _empty_config(),
                output_dir=str(out_dir),
                delete_source=True,
            )
            assert fail_summary.books_failed == 1
            assert fail_summary.books_completed == 0
            assert t1.exists() and t2.exists()

        # 3. Successful pack preserves source files by default, and deletes when delete_source=True
        def _fake_combine(_tracks, _tmp, out_path, **_kwargs):
            Path(out_path).write_bytes(b"fake-m4b-data")
            return True

        with (
            patch("audiobookdl.pack.output.combine_audiofiles", side_effect=_fake_combine),
            patch("audiobookdl.pack.output_download.add_metadata_to_file") as mock_add_meta,
        ):
            keep_summary = run_pack(
                [str(src_dir)],
                args.build_default_options(output_template="{author} - {title}", overwrite=True),
                _empty_config(),
                output_dir=str(out_dir),
                delete_source=False,
            )
            assert keep_summary.books_completed == 1
            expected_out = out_dir / "Input Author - Sample Book.m4b"
            assert expected_out.exists()
            assert t1.exists() and t2.exists()
            assert mock_add_meta.call_args.kwargs["chapters_embedded"] is True
            assert mock_add_meta.call_args.kwargs["cover_embedded"] is False

            del_summary = run_pack(
                [str(src_dir)],
                args.build_default_options(output_template="{author} - {title}", overwrite=True),
                _empty_config(),
                output_dir=str(out_dir),
                delete_source=True,
            )
            assert del_summary.books_completed == 1
            assert expected_out.exists()
            assert not t1.exists() and not t2.exists()
            assert not src_dir.exists()


def test_run_pack_respects_overwrite_false(tmp_path: Path):
    src_dir = tmp_path / "Existing Book"
    src_dir.mkdir()
    t1 = src_dir / "01.mp3"
    _create_empty_id3_mp3(t1)

    existing_out = tmp_path / "Existing Book.m4b"
    existing_out.write_bytes(b"original-content")

    fake_probe = AudioProbeInfo(duration=4.0, bitrate=128_000, codec_name="mp3", format_name="mp3")
    with patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe):
        summary = run_pack(
            [str(src_dir)],
            args.build_default_options(overwrite=False),
            _empty_config(),
            output_dir=str(tmp_path),
        )
    assert summary.cancelled is True
    assert existing_out.read_bytes() == b"original-content"


def test_run_pack_enrichment_updates_output_template(tmp_path: Path):
    src_dir = tmp_path / "Dune"
    src_dir.mkdir()
    t1 = src_dir / "01.mp3"
    t2 = src_dir / "02.mp3"
    for p in (t1, t2):
        _create_empty_id3_mp3(p)

    fake_probe = AudioProbeInfo(duration=3.0, bitrate=128_000, codec_name="mp3", format_name="mp3")

    def _fake_enrich(audiobook, _options, **_kwargs):
        audiobook.metadata.authors = ["Frank Herbert"]
        audiobook.metadata.series = "Dune"
        audiobook.metadata.asin = "B002V1OF70"

    def _fake_combine(_tracks, _tmp, out_path, **_kwargs):
        Path(out_path).write_bytes(b"m4b")
        return True

    opts = args.build_default_options(
        output_template="{author}/{title}",
        enrich_metadata=True,
        overwrite=True,
    )
    with (
        patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe),
        patch("audiobookdl.pack.enrich_audiobook_with_options", side_effect=_fake_enrich) as mock_enrich,
        patch("audiobookdl.pack.output.combine_audiofiles", side_effect=_fake_combine),
        patch("audiobookdl.pack.output_download.add_metadata_to_file"),
    ):
        summary = run_pack([str(src_dir)], opts, _empty_config(), output_dir=str(tmp_path / "out"))

    mock_enrich.assert_called_once()
    assert summary.books_completed == 1
    assert summary.output_paths == [str((tmp_path / "out" / "Frank Herbert" / "Dune.m4b").resolve())]


def test_pack_end_to_end_real_ffmpeg(tmp_path: Path):
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        pytest.skip("ffmpeg/ffprobe not available")

    book_dir = tmp_path / "Author Name - Packed Story"
    book_dir.mkdir()
    (book_dir / "cover.jpg").write_bytes(_make_cover_bytes("JPEG"))

    t1 = book_dir / "01 - First Part.mp3"
    t2 = book_dir / "02 - Second Part.mp3"
    _ffmpeg_silent_file(t1, ["-c:a", "libmp3lame", "-b:a", "64k"], seconds=1.2)
    _ffmpeg_silent_file(t2, ["-c:a", "libmp3lame", "-b:a", "64k"], seconds=1.2)

    out_dir = tmp_path / "library"
    opts = args.build_default_options(
        output_template="{author}/{title}",
        write_json_metadata=True,
        write_opf=True,
        write_cue=True,
        write_cover=True,
        overwrite=True,
    )
    summary = run_pack([str(book_dir)], opts, _empty_config(), output_dir=str(out_dir))
    assert summary.books_completed == 1
    assert summary.books_failed == 0

    final_m4b = out_dir / "Author Name" / "Packed Story.m4b"
    assert final_m4b.is_file() and final_m4b.stat().st_size > 0
    # Originals kept untouched by default
    assert t1.exists() and t2.exists()

    # Sidecars emitted cleanly beside final .m4b
    assert (final_m4b.parent / "Packed Story.opf").is_file()
    assert (final_m4b.parent / "Packed Story.cue").is_file()
    assert (final_m4b.parent / "Packed Story.jpg").is_file()
    meta_json = json.loads((final_m4b.parent / "Packed Story.m4b.json").read_text(encoding="utf-8"))
    assert meta_json["title"] == "Packed Story"
    assert meta_json["authors"] == ["Author Name"]
    assert len(meta_json["chapters"]) == 2

    # Embedded MP4 atoms + chapters verified
    mp4_file = MP4(str(final_m4b))
    assert mp4_file["\xa9nam"] == ["Packed Story"]
    assert mp4_file["\xa9ART"] == ["Author Name"]
    assert mp4_file["stik"] == [2]
    assert "covr" in mp4_file and len(mp4_file["covr"]) == 1

    ch_probe = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_chapters", "-of", "json", str(final_m4b)],
        capture_output=True,
        text=True,
        check=True,
    )
    chapters_payload = json.loads(ch_probe.stdout)["chapters"]
    assert [c["tags"]["title"] for c in chapters_payload] == ["First Part", "Second Part"]


def test_parse_pack_arguments_and_cli_dispatch(tmp_path: Path):
    src_dir = tmp_path / "Local Book"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")

    dl_opts, pack_opts = args.parse_pack_arguments([
        str(src_dir),
        "-r",
        "--delete-source",
        "--dry-run",
        "--output-dir",
        str(tmp_path / "dest"),
        "--output-template",
        "{author}/{title}",
        "--title",
        "My Book",
        "--author",
        "Alice",
        "--author",
        "Bob",
        "--series",
        "Saga",
        "--series-order",
        "2",
        "--asin",
        "B012345678",
        "--enrich",
        "-y",
    ])
    assert pack_opts.paths == [str(src_dir)]
    assert pack_opts.recursive is True
    assert pack_opts.delete_source is True
    assert pack_opts.dry_run is True
    assert pack_opts.output_dir == str(tmp_path / "dest")
    assert pack_opts.title == "My Book"
    assert pack_opts.authors == ["Alice", "Bob"]
    assert pack_opts.series == "Saga"
    assert pack_opts.series_order == 2
    assert pack_opts.asin == "B012345678"
    assert dl_opts.output_template == "{author}/{title}"
    assert dl_opts.enrich_metadata is True
    assert dl_opts.overwrite is True

    # CLI entrypoint dispatches cleanly on dry-run
    fake_probe = AudioProbeInfo(duration=2.0, bitrate=128_000, codec_name="mp3", format_name="mp3")
    with patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe):
        cli_main([
            "pack",
            str(src_dir),
            "--dry-run",
            "--output-dir",
            str(tmp_path / "dest"),
        ])


def test_gui_controller_executes_pack_job(tmp_path: Path):
    src_dir = tmp_path / "Gui Author - Gui Book"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")
    _create_empty_id3_mp3(src_dir / "02.mp3")
    out_dir = tmp_path / "gui-out"

    controller = GuiController()
    opts = args.build_default_options(output_format="m4b", output_template="{author}/{title}", overwrite=True)
    req = PackJobRequest(
        paths=[str(src_dir)],
        recursive=False,
        delete_source=False,
        output_dir=str(out_dir),
        label="Gui Book",
    )

    fake_probe = AudioProbeInfo(duration=2.0, bitrate=128_000, codec_name="mp3", format_name="mp3")

    def _fake_combine(_tracks, _tmp, out_path, **_kwargs):
        Path(out_path).write_bytes(b"fake-gui-m4b")
        return True

    events: list[tuple] = []
    with (
        patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe),
        patch("audiobookdl.pack.output.combine_audiofiles", side_effect=_fake_combine),
        patch("audiobookdl.pack.output_download.add_metadata_to_file"),
    ):
        appended, run_id, jobs, total = controller.start_or_enqueue_pack_jobs([(req, opts)])
        assert appended is False
        assert total == 1
        assert len(jobs) == 1
        controller.register_queue_record(jobs[0], "Local Pack")

        deadline = time.monotonic() + 5.0
        done_seen = False
        while time.monotonic() < deadline and not done_seen:
            controller.drain_events(lambda item: events.append(item))
            done_seen = any(ev[0] == "done" and ev[1] == run_id for ev in events)
            if not done_seen:
                time.sleep(0.02)

    controller.shutdown(is_running=False, is_previewing=False)
    assert done_seen is True
    assert (out_dir / "Gui Author" / "Gui Book.m4b").is_file()
    status_events = [ev for ev in events if ev[0] == "job_status" and ev[1] == jobs[0].job_id]
    assert status_events[-1][2] == "Completed"
    assert controller.retryable_urls([jobs[0].job_id]) == []


def test_pack_dialog_renders_and_submits(tmp_path: Path):
    import tkinter as tk
    from audiobookdl.gui.pages.pack_page import PackPage

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    src_dir = tmp_path / "Dialog Author - Dialog Title"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")

    page: PackPage | None = None
    submitted: list = []
    controller = GuiController()
    opts = args.build_default_options(output_format="m4b")
    try:
        page = PackPage(
            root,
            controller,
            options_provider=lambda: opts,
            output_dir_provider=lambda: str(tmp_path),
            ensure_ffmpeg=lambda _o: True,
            on_submit=lambda reqs, fmt: submitted.append((reqs, fmt)),
        )
        page._append_path(str(src_dir))
        page.title_var.set("Custom Dialog Title")
        page._submit()
        assert len(submitted) == 1
        reqs, fmt = submitted[0]
        assert fmt == "m4b"
        assert reqs[0].paths == [str(src_dir)]
        assert reqs[0].overrides is not None
        assert reqs[0].overrides.title == "Custom Dialog Title"
    finally:
        import gc
        page = None
        gc.collect()
        controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_app_wires_pack_button_and_submission(tmp_path: Path):
    import gc
    import tkinter as tk
    from audiobookdl.gui.app import AudiobookDLApp

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    src_dir = tmp_path / "App Author - App Book"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")
    _create_empty_id3_mp3(src_dir / "02.mp3")

    fake_probe = AudioProbeInfo(duration=1.5, bitrate=128_000, codec_name="mp3", format_name="mp3")

    def _fake_combine(_tracks, _tmp, out_path, **_kwargs):
        Path(out_path).write_bytes(b"app-pack")
        return True

    app = AudiobookDLApp(root, config_location=str(tmp_path / "missing-config.toml"))
    try:
        assert app.pack_btn.cget("text") == "Pack Local..."
        app.output_dir_var.set(str(tmp_path / "out"))
        app.template_var.set("{author}/{title}")
        req = PackJobRequest(paths=[str(src_dir)], label="App Book")
        with (
            patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe),
            patch("audiobookdl.pack.output.combine_audiofiles", side_effect=_fake_combine),
            patch("audiobookdl.pack.output_download.add_metadata_to_file"),
        ):
            app._on_pack_submitted([req], "m4b")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and app._is_running:
                root.update()
                time.sleep(0.02)
            root.update()
        children = app.queue_tree.get_children()
        assert len(children) == 1
        row_values = app.queue_tree.item(children[0], "values")
        assert row_values[0] == "Local Pack"
        assert row_values[2] == "Completed", (
            f"row={row_values}, running={app._is_running}, "
            f"status={app.status_var.get()!r}, log={app.log_text.get('1.0', tk.END)!r}"
        )
    finally:
        app._controller.shutdown(is_running=False, is_previewing=False)
        del app
        gc.collect()
        root.destroy()


def test_pack_single_track_direct_staging_and_cached_chapter_probing(tmp_path: Path):
    src_mp3 = tmp_path / "Solo Track.mp3"
    _create_empty_id3_mp3(src_mp3)
    out_dir = tmp_path / "out"

    cached_info = AudioProbeInfo(duration=12.5, bitrate=128_000, codec_name="mp3", format_name="mp3")
    probe_cache = {str(src_mp3.resolve()): cached_info}
    convert_calls: list[dict] = []

    def fake_convert(old_path, output_format, **kwargs):
        convert_calls.append({"old_path": old_path, "output_format": output_format, **kwargs})
        out_p = Path(kwargs["output_path"])
        out_p.write_bytes(b"packed-m4b")
        return str(out_p), False

    opts = args.build_default_options(output_format="m4b", output_template="{title}", overwrite=True)
    with (
        patch("audiobookdl.pack.probe_audio_info", side_effect=AssertionError("should reuse probe_cache")),
        patch("audiobookdl.pack.output.convert_single_file", side_effect=fake_convert),
        patch("audiobookdl.pack.output_download.add_metadata_to_file"),
    ):
        spec = inspect_local_book(
            str(src_mp3),
            [str(src_mp3)],
            opts,
            output_dir=str(out_dir),
            probe_cache=probe_cache,
        )
        final_path = pack_book(spec, opts, output_dir=str(out_dir), probe_cache=probe_cache)

    assert final_path == str((out_dir / "Solo Track.m4b").resolve())
    assert Path(final_path).read_bytes() == b"packed-m4b"
    assert len(convert_calls) == 1
    assert convert_calls[0]["old_path"] == str(src_mp3.resolve())
    assert convert_calls[0]["remove_source"] is False
    assert convert_calls[0]["faststart"] is True
    assert convert_calls[0]["output_path"] is not None


def test_pack_book_runs_silence_detection_when_chapters_from_silence_enabled(tmp_path):
    src_mp3 = tmp_path / "Solo Track.mp3"
    _create_empty_id3_mp3(src_mp3)
    out_dir = tmp_path / "out"

    detected = [Chapter(start=0, title="Part 1"), Chapter(start=12000, title="Part 2")]

    def fake_convert(old_path, output_format, *, output_path=None, probe_cache=None, **kwargs):
        assert output_path is not None
        Path(output_path).write_bytes(b"m4b")
        if probe_cache is not None:
            probe_cache[output_path] = cached_info
        return output_path, True

    opts = args.build_default_options(
        output_format="m4b",
        output_template="{title}",
        overwrite=True,
        chapters_from_silence=True,
    )
    cached_info = AudioProbeInfo(duration=25.0, bitrate=128_000, codec_name="mp3", format_name="mp3")
    probe_cache = {str(src_mp3.resolve()): cached_info}
    meta_calls = []

    def fake_add_meta(audiobook, filepath, options, **kwargs):
        meta_calls.append((list(audiobook.chapters), kwargs.get("probe_cache")))

    with (
        patch("audiobookdl.pack.output.detect_silence_chapters", return_value=detected) as silence_mock,
        patch("audiobookdl.pack.output.convert_single_file", side_effect=fake_convert),
        patch("audiobookdl.pack.output_download.add_metadata_to_file", side_effect=fake_add_meta),
    ):
        spec = inspect_local_book(
            str(src_mp3),
            [str(src_mp3)],
            opts,
            output_dir=str(out_dir),
            probe_cache=probe_cache,
        )
        final_path = pack_book(spec, opts, output_dir=str(out_dir), probe_cache=probe_cache)

    assert final_path == str((out_dir / "Solo Track.m4b").resolve())
    silence_mock.assert_called_once()
    assert silence_mock.call_args.kwargs.get("probe_cache") is probe_cache
    assert final_path in probe_cache
    assert meta_calls == [(detected, probe_cache)]


def test_pack_dialog_preserves_explicit_fields_passes_output_dir_and_keeps_global_format(
    tmp_path: Path,
):
    import gc
    import time
    import tkinter as tk
    from audiobookdl.config import load_config, save_output_preferences
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.pages.pack_page import PackPage

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    save_output_preferences(config_location=str(cfg_path), output_format="mp3")

    src_dir = tmp_path / "Author A - Book One"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")
    pack_out = tmp_path / "pack_output"
    pack_out.mkdir()

    app = AudiobookDLApp(root, config_location=str(cfg_path))
    app.output_format_var.set("mp3")
    submitted: list[tuple[list[PackJobRequest], str]] = []
    base_opts = args.build_default_options(output_format="mp3")
    page: PackPage | None = None
    try:
        page = PackPage(
            root,
            app.controller,
            options_provider=lambda: base_opts,
            output_dir_provider=lambda: str(pack_out),
            ensure_ffmpeg=lambda _o: True,
            on_submit=lambda reqs, fmt: submitted.append((reqs, fmt)),
        )
        page.format_var.set("m4b")
        cloned = page._clone_options_for_pack()
        assert cloned is not None
        assert cloned.output_format == "m4b"
        assert cloned._explicit_fields == frozenset({"output_format"})

        page._append_path(str(src_dir))
        fake_probe = AudioProbeInfo(duration=10.0, bitrate=128_000, codec_name="mp3", format_name="mp3")
        with patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe):
            page._start_scan()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and page._is_scanning:
                root.update()
                time.sleep(0.01)
        assert len(page._preview_specs) == 1
        assert Path(page._preview_specs[0].output_path).parent == pack_out

        page._submit()
        assert len(submitted) == 1
        assert submitted[0][0][0].output_dir == str(pack_out)

        # Submitting a pack job with m4b must NOT overwrite global saved output_format="mp3"
        with patch.object(app.controller, "start_or_enqueue_pack_jobs", return_value=(True, 1, [], 1)):
            app._on_pack_submitted(submitted[0][0], "m4b")
        reloaded = load_config(str(cfg_path))
        assert reloaded.output_format == "mp3"
    finally:
        page = None
        gc.collect()
        app.controller.shutdown(is_running=False, is_previewing=False)
        root.destroy()


def test_pack_job_persistence_and_retry_across_restart(tmp_path: Path, monkeypatch):
    import gc
    import time
    import tkinter as tk
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.controller import GuiInteractionHandler, QueuedJob

    # Verify PackJobRequest serialization round-trip with PackOverrides and output_format
    req = PackJobRequest(
        paths=[str(tmp_path / "book_src")],
        recursive=True,
        delete_source=False,
        output_dir=str(tmp_path / "out"),
        overrides=PackOverrides(
            title="Persisted Title",
            authors=["Author One", "Author Two"],
            narrators=["Narrator One"],
            series="My Series",
            series_order=2,
            isbn="9781234567890",
            asin="B00TEST123",
            cover_path=str(tmp_path / "cover.jpg"),
        ),
        label="Persisted Title",
        output_format="mp3",
    )
    restored_req = PackJobRequest.from_dict(req.to_dict())
    assert restored_req == req
    assert PackJobRequest.from_dict({"paths": []}) is None

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text("", encoding="utf-8")

    src_dir = tmp_path / "Persist Author - Persist Book"
    src_dir.mkdir()
    _create_empty_id3_mp3(src_dir / "01.mp3")
    _create_empty_id3_mp3(src_dir / "02.mp3")
    out_dir = tmp_path / "pack_out"
    out_dir.mkdir()

    try:
        app1 = AudiobookDLApp(root, config_location=str(cfg_path))
        interaction = GuiInteractionHandler(app1.controller.event_queue, app1.controller.cancel_event)
        pack_req = PackJobRequest(
            paths=[str(src_dir)],
            recursive=False,
            delete_source=False,
            output_dir=str(out_dir),
            overrides=PackOverrides(title="Persisted Book", authors=["Persist Author"]),
            label="Persisted Book",
            output_format="m4b",
        )
        pack_opts = args.build_default_options(output_format="m4b")
        j_pending = QueuedJob(
            job_id="p_pend",
            run_id=1,
            url="Persisted Book",
            options=pack_opts,
            interaction=interaction,
            pack_request=pack_req,
        )
        j_done = QueuedJob(
            job_id="p_done",
            run_id=1,
            url="Completed Pack",
            options=pack_opts,
            interaction=interaction,
            pack_request=PackJobRequest(paths=[str(src_dir)], label="Completed Pack", output_format="m4b"),
        )
        app1._queue_panel.register_queue_job(j_pending, persist=False)
        app1._queue_panel.register_queue_job(j_done, persist=False)
        app1._queue_panel.update_queue_row("p_done", "Completed", "1 ok")
        app1.controller.shutdown(is_running=False, is_previewing=False)
    finally:
        gc.collect()
        root.destroy()

    root2 = tk.Tk()
    root2.withdraw()
    try:
        app2 = AudiobookDLApp(root2, config_location=str(cfg_path))
        children = app2.queue_tree.get_children()
        assert len(children) == 1
        restored_id = children[0]
        row_values = app2.queue_tree.item(restored_id, "values")
        assert row_values[0] == "Local Pack"
        assert row_values[1] == "Persisted Book"
        assert row_values[2] == "Interrupted"

        retryable = app2.controller.retryable_jobs(children)
        assert len(retryable) == 1
        assert retryable[0]["job_kind"] == "pack"

        fake_probe = AudioProbeInfo(duration=1.5, bitrate=128_000, codec_name="mp3", format_name="mp3")

        def _fake_combine(_tracks, _tmp, out_path, **_kwargs):
            Path(out_path).write_bytes(b"retried-pack")
            return True

        monkeypatch.setattr("audiobookdl.gui.ffmpeg_assistant.find_executable", lambda name: name)
        app2.output_dir_var.set(str(out_dir))
        app2.template_var.set("{author}/{title}")
        with (
            patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe),
            patch("audiobookdl.pack.output.combine_audiofiles", side_effect=_fake_combine),
            patch("audiobookdl.pack.output_download.add_metadata_to_file"),
        ):
            app2._retry_failed_queue_items([restored_id])
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and app2._is_running:
                root2.update()
                time.sleep(0.02)
            root2.update()

        all_children = app2.queue_tree.get_children()
        assert len(all_children) == 2
        retried_id = all_children[-1]
        retried_rec = app2.controller.queue_records[retried_id]
        expected_out = out_dir / "Persist Author" / "Persisted Book.m4b"
        assert expected_out.is_file()
        assert retried_rec["status"] == "Completed"
        assert retried_rec["output_path"] == str(expected_out)
    finally:
        app2.controller.shutdown(is_running=False, is_previewing=False)
        gc.collect()
        root2.destroy()


def test_pack_extended_metadata_and_chapter_overrides(tmp_path: Path):
    from audiobookdl.gui.controller import PackJobRequest

    book_dir = tmp_path / "Extended Book"
    book_dir.mkdir()
    t1 = book_dir / "01.mp3"
    t2 = book_dir / "02.mp3"
    _create_empty_id3_mp3(t1)
    _create_empty_id3_mp3(t2)

    out_dir = tmp_path / "out"
    opts = args.build_default_options(output_template="{author}/{title}")
    custom_chapters = [Chapter(0, "Prelude"), Chapter(45_000, "Chapter One"), Chapter(120_000, "Finale")]
    overrides = PackOverrides(
        title="Custom Extended",
        authors=["Author One"],
        genres=["Sci-Fi", "Adventure"],
        publisher="Orbit Press",
        release_date="2021-05-11",
        language="en",
        description="Extended description body",
        chapters=custom_chapters,
    )

    fake_probe = AudioProbeInfo(duration=90.0, bitrate=128_000, codec_name="mp3", format_name="mp3")
    with patch("audiobookdl.pack.probe_audio_info", return_value=fake_probe):
        spec = inspect_local_book(
            str(book_dir),
            [str(t1), str(t2)],
            opts,
            output_dir=str(out_dir),
            overrides=overrides,
        )
    assert spec.audiobook.metadata.genres == ["Sci-Fi", "Adventure"]
    assert spec.audiobook.metadata.publisher == "Orbit Press"
    assert spec.audiobook.metadata.release_date == date(2021, 5, 11)
    assert spec.audiobook.metadata.language is not None and spec.audiobook.metadata.language.alpha_2 == "en"
    assert spec.audiobook.metadata.description == "Extended description body"
    assert [(c.start, c.title) for c in spec.audiobook.chapters] == [
        (0, "Prelude"),
        (45_000, "Chapter One"),
        (120_000, "Finale"),
    ]

    req = PackJobRequest(paths=[str(book_dir)], overrides=overrides, output_format="m4b")
    restored = PackJobRequest.from_dict(req.to_dict())
    assert restored is not None and restored.overrides is not None
    assert restored.overrides.genres == ["Sci-Fi", "Adventure"]
    assert restored.overrides.publisher == "Orbit Press"
    assert restored.overrides.release_date == "2021-05-11"
    assert restored.overrides.description == "Extended description body"
    assert restored.overrides.chapters is not None and len(restored.overrides.chapters) == 3








"""End-to-end tests exercising the real `audiobook-dl` CLI subprocess."""

from __future__ import annotations

import json

from mutagen.id3 import ID3
from mutagen.mp4 import MP4, MP4Cover
import pytest

from audiobookdl.utils import ffprobe_audio_bitrate, ffprobe_duration
from tests.e2e.fixtures import MediaFixtureFactory, build_librivox_html
from tests.e2e.harness import CliRunner, E2EWorkspace
from tests.e2e.server import LocalAudiobookServer, RouteRequest, RouteResponse


pytestmark = pytest.mark.e2e


def test_cli_readiness_help_and_error_exit_codes(
    cli_runner: CliRunner,
    e2e_workspace: E2EWorkspace,
) -> None:
    help_res = cli_runner.run([], use_proxy=False)
    assert help_res.returncode == 0
    assert "audiobook-dl" in help_res.stdout or "Usage" in help_res.stdout or help_res.stderr

    missing_cfg = e2e_workspace.config_dir / "missing.toml"
    cfg_res = cli_runner.run(["--config", str(missing_cfg), "http://librivox.org/any"])
    assert cfg_res.returncode != 0

    missing_input = e2e_workspace.work_dir / "does-not-exist.txt"
    input_res = cli_runner.run(["--input-file", str(missing_input)])
    assert input_res.returncode != 0
    assert "does-not-exist.txt" in (input_res.stdout + input_res.stderr)

    unknown_cfg = e2e_workspace.write_toml('output_template = "{title}"\n')
    unknown_res = cli_runner.run(
        ["--config", str(unknown_cfg), "https://unknown.example.invalid/book"]
    )
    assert unknown_res.returncode == 1

    not_found_res = cli_runner.run(
        ["--config", str(e2e_workspace.config_path), "http://librivox.org/missing-book"]
    )
    assert not_found_res.returncode == 1


@pytest.mark.ffmpeg
def test_cli_download_combine_m4b_metadata_and_json_sidecar(
    cli_runner: CliRunner,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
) -> None:
    _ = ffmpeg_tools
    part1 = media_factory.make_mp3("cli_p1.mp3", duration_sec=1.0, freq_hz=440)
    part2 = media_factory.make_mp3("cli_p2.mp3", duration_sec=1.0, freq_hz=660)
    cover = media_factory.make_jpeg()

    html = build_librivox_html(
        title="E2E Odyssey",
        cover_url="http://librivox.org/assets/odyssey-cover.jpg",
        parts=[
            ("Chapter 1", "http://librivox.org/audio/part1.mp3"),
            ("Chapter 2", "http://librivox.org/audio/part2.mp3"),
        ],
    )
    e2e_server.add_route("/books/e2e-odyssey", html, content_type="text/html; charset=utf-8", host="librivox.org")
    e2e_server.add_route("/assets/odyssey-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")
    e2e_server.add_route("/audio/part1.mp3", part1, content_type="audio/mpeg", host="librivox.org")
    e2e_server.add_route("/audio/part2.mp3", part2, content_type="audio/mpeg", host="librivox.org")

    db_dir_toml = e2e_workspace.db_dir.as_posix()
    cfg_path = e2e_workspace.write_toml(
        f'audio_bitrate = "128k"\n'
        f"database_directory = {json.dumps(db_dir_toml)}\n"
    )

    result = cli_runner.run([
        "--config", str(cfg_path),
        "-o", str(e2e_workspace.output_dir / "{title}"),
        "--combine",
        "-f", "m4b",
        "--write-json-metadata",
        "http://librivox.org/books/e2e-odyssey",
    ])
    assert result.returncode == 0, result.stderr
    assert not result.timed_out

    out_m4b = e2e_workspace.output_dir / "E2E Odyssey.m4b"
    out_json = e2e_workspace.output_dir / "E2E Odyssey.m4b.json"
    assert out_m4b.is_file()
    assert out_json.is_file()
    assert not (e2e_workspace.output_dir / "E2E Odyssey").exists()
    assert not (e2e_workspace.output_dir / "E2E Odyssey.work").exists()

    sidecar = json.loads(out_json.read_text(encoding="utf-8"))
    assert sidecar["title"] == "E2E Odyssey"

    duration = ffprobe_duration(str(out_m4b))
    assert 1.8 <= duration <= 2.3
    bitrate = ffprobe_audio_bitrate(str(out_m4b))
    assert bitrate is not None and bitrate > 0

    mp4 = MP4(str(out_m4b))
    assert mp4.tags is not None
    assert mp4.tags["\xa9nam"] == ["E2E Odyssey"]
    assert mp4.tags["stik"] == [2]
    assert len(mp4.tags["covr"]) == 1
    assert isinstance(mp4.tags["covr"][0], MP4Cover)
    assert mp4.tags["covr"][0].imageformat == MP4Cover.FORMAT_JPEG

    requested_paths = {req.path for req in e2e_server.recorded_requests()}
    assert requested_paths == {
        "/books/e2e-odyssey",
        "/assets/odyssey-cover.jpg",
        "/audio/part1.mp3",
        "/audio/part2.mp3",
    }


@pytest.mark.ffmpeg
def test_cli_resume_http_range_and_skip_downloaded(
    cli_runner: CliRunner,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
) -> None:
    _ = ffmpeg_tools
    part1 = media_factory.make_mp3("resume_p1.mp3", duration_sec=1.0, freq_hz=440)
    part2 = media_factory.make_mp3("resume_p2.mp3", duration_sec=1.0, freq_hz=550)
    cover = media_factory.make_jpeg()

    html = build_librivox_html(
        title="Resume Odyssey",
        cover_url="http://librivox.org/assets/resume-cover.jpg",
        parts=[
            ("Part 1", "http://librivox.org/audio/res1.mp3"),
            ("Part 2", "http://librivox.org/audio/res2.mp3"),
        ],
    )
    e2e_server.add_route("/books/e2e-resume", html, content_type="text/html; charset=utf-8", host="librivox.org")
    e2e_server.add_route("/assets/resume-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")
    e2e_server.add_route("/audio/res1.mp3", part1, content_type="audio/mpeg", host="librivox.org")

    fail_state = {"fail_part2": True}

    def part2_handler(_req: RouteRequest) -> RouteResponse:
        if fail_state["fail_part2"]:
            return RouteResponse(
                body=b"upstream temporarily unavailable",
                content_type="text/plain",
                status_code=503,
                support_range=False,
            )
        return RouteResponse(body=part2, content_type="audio/mpeg", status_code=200, support_range=True)

    e2e_server.add_handler("GET", "/audio/res2.mp3", part2_handler, host="librivox.org")

    out_template = str(e2e_workspace.output_dir / "{title}")
    cfg_path = e2e_workspace.write_toml("")

    # Run 1: Part 0 completes while Part 1 fails with HTTP 503 -> exit 1, Part 0 preserved
    run1 = cli_runner.run([
        "--config", str(cfg_path),
        "--database_directory", str(e2e_workspace.db_dir),
        "--resume",
        "-o", out_template,
        "http://librivox.org/books/e2e-resume",
    ])
    assert run1.returncode == 1
    book_dir = e2e_workspace.output_dir / "Resume Odyssey"
    assert (book_dir / "Part 0.mp3").read_bytes() == part1
    assert not (book_dir / "Part 1.mp3").exists()

    # Seed partial .tmp for Part 1 and allow Part 2 endpoint to serve via HTTP 206 Range
    split = len(part2) // 2
    (book_dir / "Part 1.mp3.tmp").write_bytes(part2[:split])
    fail_state["fail_part2"] = False
    before_run2 = len(e2e_server.recorded_requests())

    # Run 2: --resume skips Part 0 and resumes Part 1 with Range: bytes=<split>-
    run2 = cli_runner.run([
        "--config", str(cfg_path),
        "--database_directory", str(e2e_workspace.db_dir),
        "--resume",
        "-o", out_template,
        "http://librivox.org/books/e2e-resume",
    ])
    assert run2.returncode == 0, run2.stderr
    part0_path = book_dir / "Part 0.mp3"
    part1_path = book_dir / "Part 1.mp3"
    assert part0_path.is_file()
    assert part1_path.is_file()
    assert not (book_dir / "Part 1.mp3.tmp").exists()
    assert (book_dir / "cover.jpg").read_bytes() == cover
    assert str(ID3(str(part0_path))["TIT2"]) == "Resume Odyssey"
    assert str(ID3(str(part1_path))["TIT2"]) == "Resume Odyssey"
    assert 0.8 <= ffprobe_duration(str(part0_path)) <= 1.2
    assert 0.8 <= ffprobe_duration(str(part1_path)) <= 1.2

    run2_reqs = e2e_server.recorded_requests()[before_run2:]
    assert not any(r.path == "/audio/res1.mp3" for r in run2_reqs)
    res2_reqs = [r for r in run2_reqs if r.path == "/audio/res2.mp3"]
    assert len(res2_reqs) == 1
    assert res2_reqs[0].headers.get("Range") == f"bytes={split}-"
    assert res2_reqs[0].status_code == 206

    # Run 3: --skip-downloaded skips downloading any audio files
    before_run3 = len(e2e_server.recorded_requests())
    run3 = cli_runner.run([
        "--config", str(cfg_path),
        "--database_directory", str(e2e_workspace.db_dir),
        "--resume",
        "--skip-downloaded",
        "-o", out_template,
        "http://librivox.org/books/e2e-resume",
    ])
    assert run3.returncode == 0, run3.stderr
    run3_reqs = e2e_server.recorded_requests()[before_run3:]
    assert not any(r.path.startswith("/audio/") for r in run3_reqs)


@pytest.mark.ffmpeg
def test_cli_input_file_batch_cover_and_print_output(
    cli_runner: CliRunner,
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
) -> None:
    _ = ffmpeg_tools
    mp3_a = media_factory.make_mp3("batch_a.mp3", duration_sec=0.8, freq_hz=440)
    mp3_b = media_factory.make_mp3("batch_b.mp3", duration_sec=0.8, freq_hz=660)
    cover = media_factory.make_jpeg()

    e2e_server.add_route(
        "/books/batch-a",
        build_librivox_html(
            title="Batch Alpha",
            cover_url="http://librivox.org/assets/batch-cover.jpg",
            parts=[("Only Part", "http://librivox.org/audio/batch-a.mp3")],
        ),
        content_type="text/html; charset=utf-8",
        host="librivox.org",
    )
    e2e_server.add_route(
        "/books/batch-b",
        build_librivox_html(
            title="Batch Beta",
            cover_url="http://librivox.org/assets/batch-cover.jpg",
            parts=[("Only Part", "http://librivox.org/audio/batch-b.mp3")],
        ),
        content_type="text/html; charset=utf-8",
        host="librivox.org",
    )
    e2e_server.add_route("/assets/batch-cover.jpg", cover, content_type="image/jpeg", host="librivox.org")
    e2e_server.add_route("/audio/batch-a.mp3", mp3_a, content_type="audio/mpeg", host="librivox.org")
    e2e_server.add_route("/audio/batch-b.mp3", mp3_b, content_type="audio/mpeg", host="librivox.org")

    cfg_path = e2e_workspace.write_toml("")
    out_template = str(e2e_workspace.output_dir / "{title}")

    # 1. --print-output resolves target location without downloading audio
    print_res = cli_runner.run([
        "--config", str(cfg_path),
        "--print-output",
        "-o", out_template,
        "http://librivox.org/books/batch-a",
    ])
    assert print_res.returncode == 0
    assert "Batch Alpha" in print_res.stdout
    assert not (e2e_workspace.output_dir / "Batch Alpha.mp3").exists()
    assert not any(r.path.startswith("/audio/") for r in e2e_server.recorded_requests())

    # 2. --cover downloads only the cover image
    cover_res = cli_runner.run([
        "--config", str(cfg_path),
        "--cover",
        "-o", str(e2e_workspace.output_dir / "covers" / "{title}"),
        "http://librivox.org/books/batch-a",
    ])
    assert cover_res.returncode == 0
    assert (e2e_workspace.output_dir / "covers" / "Batch Alpha.jpg").read_bytes() == cover
    assert not any(r.path.startswith("/audio/") for r in e2e_server.recorded_requests())

    # 3. --input-file with UTF-8 BOM downloads multiple books in one invocation
    urls_file = e2e_workspace.write_input_file(
        "urls.txt",
        [
            "http://librivox.org/books/batch-a",
            "http://librivox.org/books/batch-b",
        ],
        bom=True,
    )
    batch_res = cli_runner.run([
        "--config", str(cfg_path),
        "--database_directory", str(e2e_workspace.db_dir),
        "--input-file", str(urls_file),
        "-o", out_template,
    ])
    assert batch_res.returncode == 0, batch_res.stderr
    alpha_mp3 = e2e_workspace.output_dir / "Batch Alpha.mp3"
    beta_mp3 = e2e_workspace.output_dir / "Batch Beta.mp3"
    assert alpha_mp3.is_file()
    assert beta_mp3.is_file()
    assert 0.6 <= ffprobe_duration(str(alpha_mp3)) <= 1.0
    assert 0.6 <= ffprobe_duration(str(beta_mp3)) <= 1.0
    alpha_tags = ID3(str(alpha_mp3))
    beta_tags = ID3(str(beta_mp3))
    assert str(alpha_tags["TIT2"]) == "Batch Alpha"
    assert str(beta_tags["TIT2"]) == "Batch Beta"
    assert len(alpha_tags.getall("APIC")) == 1
    assert len(beta_tags.getall("APIC")) == 1

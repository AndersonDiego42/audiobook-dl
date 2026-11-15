from __future__ import annotations

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import subprocess
import threading
from typing import Dict, List, Tuple

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from mutagen.id3 import ID3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PIL import Image
import pycountry
import pytest

from audiobookdl import (
    Audiobook,
    AudiobookMetadata,
    Chapter,
    Cover,
    DownloadEvent,
    DownloadSummary,
    Downloader,
    args,
    logging,
)
from audiobookdl.config import load_config
from audiobookdl.output.download import add_metadata_to_file
from audiobookdl.sources.source import Source
from audiobookdl.utils import ffprobe_duration, find_executable


def _make_aac_mpegts_segment(ffmpeg: str, path: Path, duration_sec: float = 1.0) -> bytes:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            str(duration_sec),
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _make_jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (16, 16), (40, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


class _CookieGatedMediaHandler(BaseHTTPRequestHandler):
    routes: Dict[str, Tuple[str, bytes]] = {}
    expected_cookie: str = ""
    expected_header: Tuple[str, str] = ("", "")
    recorded_requests: List[Tuple[str, str, str]] = []

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:
        cookie_hdr = self.headers.get("Cookie", "")
        hdr_name, hdr_val = self.expected_header
        self.recorded_requests.append((self.path, cookie_hdr, self.headers.get(hdr_name, "")))

        if self.expected_cookie not in cookie_hdr:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"missing session cookie")
            return

        if self.path in ("/hls/master.m3u8", "/hls/key.bin", "/hls/seg0.ts", "/hls/seg1.ts"):
            if self.headers.get(hdr_name) != hdr_val:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"missing stream auth header")
                return

        entry = self.routes.get(self.path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        content_type, payload = entry
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _LocalHlsTestSource(Source):
    names = ["localhls"]
    match = [r"http://127\.0\.0\.1:\d+/books/\d+"]
    _authentication_methods = ["cookies"]
    create_storage_dir = True

    def download(self, url: str) -> Audiobook:
        book_meta_json = self.get_json(url)
        stream_headers = {"X-Stream-Token": book_meta_json["stream_token"]}
        files = self.get_stream_files(
            book_meta_json["playlist_url"],
            headers=stream_headers,
            expected_content_type="video/mp2t",
        )
        cover_bytes = self.get(book_meta_json["cover_url"])
        metadata = AudiobookMetadata(
            title=book_meta_json["title"],
            scrape_url=url,
            series=book_meta_json["series"],
            series_order=book_meta_json["series_order"],
            authors=book_meta_json["authors"],
            narrators=book_meta_json["narrators"],
            genres=book_meta_json["genres"],
            language=pycountry.languages.get(alpha_3=book_meta_json["language"]),
            description=book_meta_json["description"],
            isbn=book_meta_json["isbn"],
            publisher=book_meta_json["publisher"],
            release_date=date.fromisoformat(book_meta_json["release_date"]),
        )
        chapters = [
            Chapter(start=ch["start"], title=ch["title"])
            for ch in book_meta_json["chapters"]
        ]
        return Audiobook(
            session=self._session,
            metadata=metadata,
            files=files,
            chapters=chapters,
            cover=Cover(image=cover_bytes, extension="jpg"),
            source_data={"id": book_meta_json["id"]},
        )

    def on_download_complete(self, audiobook: Audiobook) -> None:
        marker = Path(self.database_directory) / f"{audiobook.source_data['id']}.json"
        marker.write_text(json.dumps({"title": audiobook.title}), encoding="utf-8")


def test_downloader_hls_aes_combine_and_metadata_integration(tmp_path, monkeypatch):
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe not available")
    assert ffmpeg is not None
    assert ffprobe is not None

    # Generate two real 1s AAC-in-MPEG-TS segments and encrypt them with AES-128-CBC
    raw_dir = tmp_path / "raw_segments"
    raw_dir.mkdir()
    seg0_plain = _make_aac_mpegts_segment(ffmpeg, raw_dir / "s0.ts", duration_sec=1.0)
    seg1_plain = _make_aac_mpegts_segment(ffmpeg, raw_dir / "s1.ts", duration_sec=1.0)

    aes_key = b"0123456789abcdef"
    iv0 = bytes.fromhex("00000000000000000000000000000001")
    iv1 = bytes.fromhex("00000000000000000000000000000002")
    seg0_enc = AES.new(aes_key, AES.MODE_CBC, iv0).encrypt(pad(seg0_plain, AES.block_size))
    seg1_enc = AES.new(aes_key, AES.MODE_CBC, iv1).encrypt(pad(seg1_plain, AES.block_size))
    cover_bytes = _make_jpeg_bytes()

    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:2\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001\n'
        "#EXTINF:1.0,\n"
        "seg0.ts\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000002\n'
        "#EXTINF:1.0,\n"
        "seg1.ts\n"
        "#EXT-X-ENDLIST\n"
    ).encode("utf-8")

    # Serve everything over a real local HTTP server requiring session cookies + stream token
    recorded_requests: List[Tuple[str, str, str]] = []
    routes: Dict[str, Tuple[str, bytes]] = {}

    class _Handler(_CookieGatedMediaHandler):
        pass

    _Handler.routes = routes
    _Handler.expected_cookie = "session_id=local-auth-cookie"
    _Handler.expected_header = ("X-Stream-Token", "stream-secret-42")
    _Handler.recorded_requests = recorded_requests

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    book_url = f"{base_url}/books/42"

    book_payload = {
        "id": "42",
        "title": "Integration Odyssey",
        "series": "Test Saga",
        "series_order": 2,
        "authors": ["Ada Lovelace"],
        "narrators": ["Grace Hopper"],
        "genres": ["Science Fiction"],
        "language": "eng",
        "description": "End-to-end integration verification.",
        "isbn": "9780123456789",
        "publisher": "Test Press",
        "release_date": "2024-05-15",
        "playlist_url": f"{base_url}/hls/master.m3u8",
        "cover_url": f"{base_url}/cover.jpg",
        "stream_token": "stream-secret-42",
        "chapters": [
            {"start": 0, "title": "Prologue"},
            {"start": 1000, "title": "Chapter One"},
        ],
    }
    routes["/books/42"] = ("application/json", json.dumps(book_payload).encode("utf-8"))
    routes["/hls/master.m3u8"] = ("application/vnd.apple.mpegurl", playlist)
    routes["/hls/key.bin"] = ("application/octet-stream", aes_key)
    routes["/hls/seg0.ts"] = ("video/mp2t", seg0_enc)
    routes["/hls/seg1.ts"] = ("video/mp2t", seg1_enc)
    routes["/cover.jpg"] = ("image/jpeg", cover_bytes)

    # Persist real cookie file and TOML config pointing to it via relative path
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cookies_path = cfg_dir / "cookies.txt"
    cookies_path.write_text(
        "# Netscape HTTP Cookie File\n"
        "127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession_id\tlocal-auth-cookie\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "output"
    db_dir = tmp_path / "db"
    cfg_path = cfg_dir / "audiobook-dl.toml"
    cfg_path.write_text(
        "[sources.localhls]\n"
        'cookie_file = "cookies.txt"\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("audiobookdl.sources.get_source_classes", lambda: [_LocalHlsTestSource])

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        config = load_config(str(cfg_path))
        options = args.build_default_options(
            config_location=str(cfg_path),
            output_template=str(out_dir / "{author}" / "{title}"),
            database_directory=str(db_dir),
            output_format="m4b",
            combine=True,
            write_json_metadata=True,
        )
        events: List[DownloadEvent] = []
        downloader = Downloader(config=config, options=options, on_event=events.append)
        summary = downloader.download([book_url])
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)

    assert summary == DownloadSummary(completed=1, failed=0, cancelled=False)
    assert logging.get_job_context() is None

    event_kinds = {ev.kind for ev in events}
    assert {"log", "progress_start", "progress_finish", "item_progress"}.issubset(event_kinds)
    log_messages = [ev.data["message"] for ev in events if ev.kind == "log"]
    chapters_idx = next(i for i, m in enumerate(log_messages) if "Adding chapters" in m)
    metadata_idx = next(i for i, m in enumerate(log_messages) if "Adding metadata" in m)
    cover_idx = next(i for i, m in enumerate(log_messages) if "Embedding cover" in m)
    assert chapters_idx < metadata_idx < cover_idx

    # Verify HTTP cookies and stream auth headers were transmitted to every endpoint
    requested_paths = {path for path, _, _ in recorded_requests}
    assert requested_paths == {
        "/books/42",
        "/hls/master.m3u8",
        "/hls/key.bin",
        "/hls/seg0.ts",
        "/hls/seg1.ts",
        "/cover.jpg",
    }

    # Verify final combined M4B, JSON sidecar, DB persistence, and temp cleanup
    expected_m4b = out_dir / "Ada Lovelace" / "Integration Odyssey.m4b"
    expected_json = out_dir / "Ada Lovelace" / "Integration Odyssey.m4b.json"
    assert expected_m4b.is_file()
    assert expected_json.is_file()
    assert not (out_dir / "Ada Lovelace" / "Integration Odyssey").exists()
    assert not (out_dir / "Ada Lovelace" / "Integration Odyssey.work").exists()
    assert json.loads((db_dir / "localhls" / "42.json").read_text(encoding="utf-8")) == {
        "title": "Integration Odyssey"
    }

    sidecar = json.loads(expected_json.read_text(encoding="utf-8"))
    assert sidecar["title"] == "Integration Odyssey"
    assert sidecar["narrators"] == ["Grace Hopper"]
    assert sidecar["isbn"] == "9780123456789"

    # Verify combined audio duration (~2s from two 1s segments)
    duration = ffprobe_duration(str(expected_m4b))
    assert 1.8 <= duration <= 2.3

    # Verify chapters, cover, and all MP4 metadata tags survive the full pipeline
    mp4 = MP4(str(expected_m4b))
    assert mp4.chapters is not None
    assert [c.title for c in mp4.chapters] == ["Prologue", "Chapter One"]

    tags = mp4.tags
    assert tags is not None
    assert tags["stik"] == [2]
    assert tags["\xa9nam"] == ["Integration Odyssey"]
    assert tags["\xa9alb"] == ["Test Saga"]
    assert tags["\xa9ART"] == ["Ada Lovelace"]
    assert tags["\xa9nrt"] == ["Grace Hopper"]
    assert tags["\xa9pub"] == ["Test Press"]
    assert tags["\xa9gen"] == ["Science Fiction"]
    assert tags["\xa9day"] == ["2024-05-15"]
    assert tags["yrrc"] == ["2024"]
    assert tags["desc"] == ["End-to-end integration verification."]
    assert tags["ldes"] == ["End-to-end integration verification."]
    assert tags["trkn"] == [(2, 0)]
    assert tags["----:com.apple.iTunes:URL"] == [MP4FreeForm(book_url.encode("utf-8"))]
    assert tags["----:com.apple.iTunes:Language"] == [MP4FreeForm(b"eng")]
    assert tags["----:com.apple.iTunes:ISBN"] == [MP4FreeForm(b"9780123456789")]
    assert len(tags["covr"]) == 1
    assert isinstance(tags["covr"][0], MP4Cover)
    assert tags["covr"][0].imageformat == MP4Cover.FORMAT_JPEG


def test_add_metadata_to_file_preserves_mp3_chapters_tags_and_cover(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    assert ffmpeg is not None

    mp3_path = tmp_path / "book.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "2",
            "-c:a",
            "libmp3lame",
            str(mp3_path),
        ],
        check=True,
        capture_output=True,
    )
    audiobook = Audiobook(
        session=None,  # type: ignore[arg-type]
        metadata=AudiobookMetadata(
            title="MP3 Integration Book",
            authors=["Ada Lovelace"],
            narrators=["Grace Hopper"],
            isbn="9780123456789",
        ),
        files=[],
        chapters=[Chapter(0, "Opening"), Chapter(1000, "Middle")],
        cover=Cover(image=_make_jpeg_bytes(), extension="jpg"),
    )

    add_metadata_to_file(audiobook, str(mp3_path), args.build_default_options())

    tags = ID3(str(mp3_path))
    assert str(tags["TIT2"]) == "MP3 Integration Book"
    assert list(tags["TPE1"].text) == ["Ada Lovelace"]
    assert list(tags["TXXX:ISBN"].text) == ["9780123456789"]
    assert [tuple(c.child_element_ids) for c in tags.getall("CTOC")] == [("chp1", "chp2")]
    chaps_by_start = sorted(tags.getall("CHAP"), key=lambda c: c.start_time)
    assert [(c.element_id, c.start_time) for c in chaps_by_start] == [
        ("chp1", 0),
        ("chp2", 1000),
    ]
    assert len(tags.getall("APIC")) == 1


def test_downloader_multipart_convert_output_m4b_integration(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe not available")
    assert ffmpeg is not None

    def _make_mp3_bytes(name: str, freq_hz: int) -> bytes:
        p = tmp_path / name
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq_hz}:duration=0.8",
                "-c:a",
                "libmp3lame",
                str(p),
            ],
            check=True,
            capture_output=True,
        )
        return p.read_bytes()

    routes: Dict[str, Tuple[str, bytes]] = {
        "/parts/part1.mp3": ("audio/mpeg", _make_mp3_bytes("src_p1.mp3", 440)),
        "/parts/part2.mp3": ("audio/mpeg", _make_mp3_bytes("src_p2.mp3", 660)),
        "/parts/cover.jpg": ("image/jpeg", _make_jpeg_bytes()),
    }

    class _MultipartHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            item = routes.get(self.path)
            if item is None:
                self.send_response(404)
                self.end_headers()
                return
            ctype, body = item
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _MultipartHandler)
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        from audiobookdl import AudiobookFile

        class _MultiPartHttpSource(Source):
            name = "localmp"
            _authentication_methods = []
            match = [r"http://127\.0\.0\.1:\d+/mp/.+"]

            def download(self, url: str) -> Audiobook:
                return Audiobook(
                    session=self._session,
                    metadata=AudiobookMetadata(
                        title="Multipart Convert Book",
                        authors=["Ada Lovelace"],
                    ),
                    files=[
                        AudiobookFile(url=f"{base}/parts/part1.mp3", title="Track 1", ext="mp3"),
                        AudiobookFile(url=f"{base}/parts/part2.mp3", title="Track 2", ext="mp3"),
                    ],
                    chapters=[Chapter(0, "Intro"), Chapter(800, "Second")],
                    cover=Cover(image=routes["/parts/cover.jpg"][1], extension="jpg"),
                )

        out_dir = tmp_path / "out"
        db_dir = tmp_path / "db"
        out_dir.mkdir()
        db_dir.mkdir()

        cfg = load_config(None)
        opts = args.build_default_options(
            urls=[f"{base}/mp/1"],
            combine=True,
            output_format="m4b",
            audio_bitrate="96k",
            output_template=str(out_dir / "{title}"),
            database_directory=str(db_dir),
            skip_downloaded=False,
        )
        src_inst = _MultiPartHttpSource(opts)
        ab = src_inst.download(opts.urls[0])
        summary = Downloader(cfg, opts).download(opts.urls, prefetched=(src_inst, ab))
        assert summary.completed == 1
        out_m4b = out_dir / "Multipart Convert Book.m4b"
        assert out_m4b.is_file()
        assert 1.3 <= ffprobe_duration(str(out_m4b)) <= 2.0
        mp4 = MP4(str(out_m4b))
        assert [c.title for c in mp4.chapters] == ["Intro", "Second"]
    finally:
        server.shutdown()
        server.server_close()

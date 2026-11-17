import json
from types import SimpleNamespace
import zipfile

import pytest

from audiobookdl import Audiobook, DownloadOptions, Series
from audiobookdl.exceptions import (
    BookHasNoAudiobook,
    BookHasNoEbook,
    UserNotAuthorized,
)
from audiobookdl.output.download import download_audiobook
from audiobookdl.sources import find_compatible_source, find_source_by_name
from audiobookdl.sources.oreilly import OReillySource


def _opts(tmp_path, *, media_type: str = "auto") -> DownloadOptions:
    return DownloadOptions(
        database_directory=str(tmp_path),
        skip_downloaded=False,
        media_type=media_type,
    )


def _json_resp(url: str, payload: dict | list, status_code: int = 200):
    return SimpleNamespace(
        status_code=status_code,
        url=url,
        headers={"Content-Type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
        json=lambda: payload,
    )


def test_oreilly_url_matching_and_aliases():
    urls = [
        "https://learning.oreilly.com/library/view/fluent-python-2nd/9781492056348/",
        "https://learning.oreilly.com/audiobooks/-/9781098123456/",
        "https://learning.oreilly.com/videos/python-deep-dive/9780134567890/",
        "https://learning.oreilly.com/course/rust-bootcamp/9781234567890/",
        "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:9781492056348/",
        "https://learning.oreilly.com/api/v2/metadata/urn:orm:audiobook:0642572102746/",
    ]
    for url in urls:
        assert find_compatible_source(url) is OReillySource

    assert find_source_by_name("oreilly") is OReillySource
    assert find_source_by_name("o'reilly") is OReillySource
    assert find_source_by_name("oreillylearning") is OReillySource


def test_oreilly_verify_auth_handles_cookies_and_unauthorized(tmp_path):
    src = OReillySource(_opts(tmp_path))
    assert src.verify_auth() is False

    src._session.cookies.set("orm-jwt", "valid-jwt", domain=".oreilly.com")
    src.request = lambda _m, url, **_kw: _json_resp(url, {"session": "ks-ok"})  # type: ignore[method-assign]
    assert src.verify_auth() is True

    src.request = lambda _m, url, **_kw: _json_resp(url, {"detail": "nope"}, status_code=401)  # type: ignore[method-assign]
    assert src.verify_auth() is False

    html_login = SimpleNamespace(
        status_code=200,
        url="https://learning.oreilly.com/accounts/login/",
        headers={"Content-Type": "text/html"},
        content=b"<html><body>Sign in</body></html>",
    )
    src.request = lambda _m, _url, **_kw: html_login  # type: ignore[method-assign]
    assert src.verify_auth() is False


def test_oreilly_audiobook_and_video_audio_kaltura_hls(tmp_path):
    src = OReillySource(_opts(tmp_path))
    src._session.cookies.set("orm-jwt", "jwt-123", domain=".oreilly.com")

    master_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/prog.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=1500000,AUDIO="aud"\n'
        b"video/1080p.m3u8\n"
    )
    audio_m3u8 = (
        b"#EXTM3U\n"
        b"#EXTINF:45.0,\n"
        b"seg0.aac\n"
        b"#EXT-X-ENDLIST\n"
    )

    def _request(_m: str, url: str, **_kw):
        if url.endswith("/api/v2/metadata/urn:orm:video:9780134567890/"):
            return _json_resp(
                url,
                {
                    "identifier": "9780134567890",
                    "ourn": "urn:orm:video:9780134567890",
                    "name": "Python Deep Dive",
                    "type": "video",
                    "talent": {
                        "contributors": [
                            {"name": "Jane Dev", "contributor_type": "author"},
                        ]
                    },
                    "publishers": [{"name": "O'Reilly Media"}],
                    "language": "en",
                    "publication_date": "2024-06-10",
                    "description": {"text/html": "<p>Course summary.</p>"},
                },
            )
        if url.endswith("/api/v1/videotocs/9780134567890/"):
            return _json_resp(
                url,
                {
                    "toc": [
                        {"reference_id": "9780134567890-P1", "ourn": None, "duration": None, "title": "Module 1"},
                        {
                            "reference_id": "9780134567890-clip1",
                            "ourn": "urn:orm:video:9780134567890:video-clip:clip1",
                            "duration": 45,
                            "title": "Intro",
                        },
                        {
                            "reference_id": "9780134567890-clip2",
                            "ourn": "urn:orm:video:9780134567890:video-clip:clip2",
                            "duration": 90,
                            "kaltura_entry_id": "1_entry2",
                            "title": "Part 2",
                        },
                    ]
                },
            )
        if "/api/v1/videoclips/9780134567890-clip1/" in url:
            return _json_resp(url, {"kaltura_entry_id": "1_entry1"})
        if "/api/v1/player/kaltura_session/" in url:
            return _json_resp(url, {"session": "ks-abc", "partner_id": "1926081"})
        return _json_resp(url, {}, status_code=404)

    def _get(url: str, **_kw):
        if url.endswith("/a.m3u8"):
            return master_m3u8
        if url.endswith("/audio/prog.m3u8"):
            return audio_m3u8
        if "/covers/" in url:
            return b"cover-bytes"
        raise AssertionError(f"Unexpected GET {url}")

    src.request = _request  # type: ignore[method-assign]
    src.get = _get  # type: ignore[method-assign]

    result = src.download("https://learning.oreilly.com/videos/python-deep-dive/9780134567890/")
    assert isinstance(result, Audiobook)
    assert result.metadata.title == "Python Deep Dive"
    assert result.metadata.authors == ["Jane Dev"]
    assert result.metadata.publisher == "O'Reilly Media"
    assert result.metadata.description == "Course summary."
    assert [c.title for c in result.chapters] == ["Intro", "Part 2"]
    assert [c.start for c in result.chapters] == [0, 45000]
    assert len(result.files) == 2
    assert all(f.url.endswith("/audio/seg0.aac") for f in result.files)
    assert all(
        isinstance(f.expected_content_type, tuple) and "video/MP2T" in f.expected_content_type
        for f in result.files
    )
    assert result.cover is not None and result.cover.image == b"cover-bytes"


def test_oreilly_epub_assembly_and_media_type_both(tmp_path):
    src = OReillySource(_opts(tmp_path, media_type="both"))
    src._session.cookies.set("orm-jwt", "jwt-123", domain=".oreilly.com")

    def _request(_m: str, url: str, **_kw):
        if url.endswith("/api/v2/metadata/urn:orm:book:9781492056348/"):
            return _json_resp(
                url,
                {
                    "identifier": "9781492056348",
                    "ourn": "urn:orm:book:9781492056348",
                    "name": "Fluent Python",
                    "type": "book",
                    "talent": {"contributors": [{"name": "Luciano Ramalho", "contributor_type": "author"}]},
                    "language": "en",
                },
            )
        if url.endswith("/api/v2/epubs/urn:orm:book:9781492056348/"):
            return _json_resp(
                url,
                {
                    "ourn": "urn:orm:book:9781492056348",
                    "title": "Fluent Python",
                    "content_format": "book",
                    "authors": [{"name": "Luciano Ramalho"}],
                    "language": "en",
                    "isbn": "9781492056348",
                    "files": "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:9781492056348/files/",
                },
            )
        if url.endswith("/api/v2/epubs/urn:orm:book:9781492056348/files/"):
            return _json_resp(
                url,
                {
                    "results": [
                        {
                            "full_path": "OEBPS/ch01.xhtml",
                            "url": "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:9781492056348/files/OEBPS/ch01.xhtml",
                            "media_type": "application/xhtml+xml",
                        },
                        {
                            "full_path": "OEBPS/style.css",
                            "url": "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:9781492056348/files/OEBPS/style.css",
                            "media_type": "text/css",
                        },
                        {
                            "full_path": "../../evil.txt",
                            "url": "https://learning.oreilly.com/evil.txt",
                        },
                    ],
                    "next": None,
                },
            )
        if url.endswith("/api/v2/metadata/urn:orm:audiobook:9781492056348/"):
            return _json_resp(
                url,
                {
                    "identifier": "9781492056348",
                    "ourn": "urn:orm:audiobook:9781492056348",
                    "name": "Fluent Python",
                    "type": "audiobook",
                    "talent": {"contributors": [{"name": "Luciano Ramalho", "contributor_type": "author"}]},
                    "chapters": [{"title": "Track 1", "duration": 30, "audio_url": "https://cdn.example/t1.m4a"}],
                },
            )
        return _json_resp(url, {}, status_code=404)

    def _get(url: str, **_kw):
        if url.endswith("ch01.xhtml"):
            return b"<html><body><h1>Chapter 1</h1></body></html>"
        if url.endswith("style.css"):
            return b"body { color: black; }"
        if "/covers/" in url:
            return b"cover-jpg"
        raise AssertionError(f"Unexpected GET {url}")

    src.request = _request  # type: ignore[method-assign]
    src.get = _get  # type: ignore[method-assign]

    res = src.download("https://learning.oreilly.com/library/view/fluent-python/9781492056348/")
    assert isinstance(res, Series)
    assert len(res.books) == 2
    ebook_ab = res.books[0]
    audio_ab = res.books[1]
    assert isinstance(ebook_ab, Audiobook)
    assert isinstance(audio_ab, Audiobook)
    assert ebook_ab.files[0].ext == "epub"
    assert audio_ab.files[0].ext == "m4a"

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target_prefix = str(out_dir / "Fluent Python")
    download_audiobook(ebook_ab, target_prefix, _opts(tmp_path, media_type="ebook"))
    epub_path = out_dir / "Fluent Python.epub"
    assert epub_path.is_file()
    with zipfile.ZipFile(epub_path, "r") as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names
        assert "OEBPS/content.opf" in names
        assert "OEBPS/ch01.xhtml" in names
        assert "OEBPS/style.css" in names
        assert all(".." not in n for n in names)


def test_oreilly_media_type_errors_and_catalog_search_library(tmp_path):
    src = OReillySource(_opts(tmp_path, media_type="ebook"))
    src._session.cookies.set("orm-jwt", "jwt-123", domain=".oreilly.com")

    def _request(_m: str, url: str, **_kw):
        if url.endswith("/api/v2/metadata/urn:orm:audiobook:111/"):
            return _json_resp(url, {"identifier": "111", "ourn": "urn:orm:audiobook:111", "name": "Audio Only", "type": "audiobook"})
        if url.endswith("/api/v2/metadata/urn:orm:book:111/") or url.endswith("/api/v2/epubs/urn:orm:book:111/"):
            return _json_resp(url, {}, status_code=404)
        if "/api/v2/search/" in url:
            return _json_resp(
                url,
                {
                    "results": [
                        {
                            "ourn": "urn:orm:audiobook:222",
                            "title": "Clean Architecture",
                            "authors": ["Robert Martin"],
                            "format": "audiobook",
                            "web_url": "/videos/-/222/",
                        },
                        {
                            "ourn": "urn:orm:book:333",
                            "title": "Designing Data-Intensive Applications",
                            "authors": [{"name": "Martin Kleppmann"}],
                            "format": "book",
                            "web_url": "/library/view/-/333/",
                        },
                    ]
                },
            )
        if url.endswith("/api/v3/collections/col-1/"):
            return _json_resp(
                url,
                {
                    "items": [
                        {
                            "ourn": "urn:orm:audiobook:222",
                            "title": "Clean Architecture",
                            "authors": ["Robert Martin"],
                            "format": "audiobook",
                        }
                    ]
                },
            )
        if url.endswith("/api/v3/collections/"):
            return _json_resp(url, [{"id": "col-1"}])
        if "/api/v1/bookmarks/" in url:
            return _json_resp(
                url,
                {
                    "results": [
                        {
                            "ourn": "urn:orm:audiobook:222",
                            "title": "Clean Architecture",
                            "authors": ["Robert Martin"],
                            "format": "audiobook",
                        },
                        {
                            "ourn": "urn:orm:video:444",
                            "title": "Kubernetes Course",
                            "authors": ["K. Dev"],
                            "format": "video",
                        },
                    ],
                    "next": None,
                },
            )
        return _json_resp(url, {}, status_code=404)

    src.request = _request  # type: ignore[method-assign]

    with pytest.raises(BookHasNoEbook):
        src.download("https://learning.oreilly.com/audiobooks/-/111/")

    src._options.media_type = "audiobook"
    src_book = OReillySource(_opts(tmp_path, media_type="audiobook"))
    src_book.request = lambda _m, url, **_kw: (  # type: ignore[method-assign]
        _json_resp(url, {"identifier": "555", "ourn": "urn:orm:book:555", "name": "Book Only", "type": "book"})
        if url.endswith("/api/v2/metadata/urn:orm:book:555/")
        else _json_resp(url, {}, status_code=404)
    )
    with pytest.raises(BookHasNoAudiobook):
        src_book.download("https://learning.oreilly.com/library/view/-/555/")

    search_items = src.search("architecture")
    assert len(search_items) == 2
    assert search_items[0].formats == ("audiobook",)
    assert search_items[1].formats == ("ebook",)
    assert all(find_compatible_source(b.url) is OReillySource for b in search_items)

    lib_items = src.list_library()
    assert [b.title for b in lib_items] == ["Clean Architecture", "Kubernetes Course"]
    assert lib_items[1].formats == ("video", "audiobook")
    assert lib_items[1].has_video is True
    assert all(find_compatible_source(b.url) is OReillySource for b in lib_items)

    assert src.search("   ") == []

    src.request = lambda _m, url, **_kw: _json_resp(url, {}, status_code=401)  # type: ignore[method-assign]
    with pytest.raises(UserNotAuthorized):
        src.list_library()


def test_oreilly_video_mode_and_format_preserve_video_stream(tmp_path):
    from audiobookdl.exceptions import GenericAudiobookDLException
    from audiobookdl.utils.audiobook import is_video, resolve_audiobook_output_format

    master_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/prog.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=1500000,AUDIO="aud"\n'
        b"video/1080p.m3u8\n"
    )
    video_m3u8 = (
        b"#EXTM3U\n"
        b"#EXTINF:5.0,\n"
        b"seg0.ts\n"
        b"#EXTINF:5.0,\n"
        b"seg1.ts\n"
        b"#EXT-X-ENDLIST\n"
    )

    def _wire(source: OReillySource) -> None:
        source._session.cookies.set("orm-jwt", "jwt-123", domain=".oreilly.com")

        def _request(_m: str, url: str, **_kw):
            if url.endswith("/api/v2/metadata/urn:orm:video:9780134567890/"):
                return _json_resp(
                    url,
                    {
                        "identifier": "9780134567890",
                        "ourn": "urn:orm:video:9780134567890",
                        "name": "Python Video Course",
                        "type": "video",
                    },
                )
            if url.endswith("/api/v1/videotocs/9780134567890/"):
                return _json_resp(
                    url,
                    {
                        "toc": [
                            {
                                "reference_id": "9780134567890-c1",
                                "ourn": "urn:orm:video:9780134567890:video-clip:c1",
                                "duration": 10,
                                "kaltura_entry_id": "1_v1",
                                "title": "Lesson 1",
                            },
                            {
                                "reference_id": "9780134567890-c2",
                                "ourn": "urn:orm:video:9780134567890:video-clip:c2",
                                "duration": 10,
                                "kaltura_entry_id": "1_v2",
                                "title": "Lesson 2",
                            },
                        ]
                    },
                )
            if "/api/v1/player/kaltura_session/" in url:
                return _json_resp(url, {"session": "ks-abc", "partner_id": "1926081"})
            return _json_resp(url, {}, status_code=404)

        def _get(url: str, **_kw):
            if url.endswith("/a.m3u8"):
                return master_m3u8
            if url.endswith("/video/1080p.m3u8"):
                return video_m3u8
            if "/covers/" in url:
                return b"cover-bytes"
            raise AssertionError(f"Unexpected GET {url}")

        source.request = _request  # type: ignore[method-assign,assignment]
        source.get = _get  # type: ignore[method-assign,assignment]

    # Explicit `media_type="video"` chooses video stream and resolves `.mp4` even with default/audio format
    src_video = OReillySource(_opts(tmp_path, media_type="video"))
    _wire(src_video)
    res_video = src_video.download("https://learning.oreilly.com/videos/python-video-course/9780134567890/")
    assert isinstance(res_video, Audiobook)
    assert is_video(res_video) is True
    assert len(res_video.files) == 4
    assert all(f.url.endswith("/video/seg0.ts") or f.url.endswith("/video/seg1.ts") for f in res_video.files)
    assert [f.chapter_index for f in res_video.files] == [0, 0, 1, 1]
    assert resolve_audiobook_output_format(res_video, None) == "mp4"
    assert resolve_audiobook_output_format(res_video, "m4b") == "mp4"
    assert resolve_audiobook_output_format(res_video, "mkv") == "mkv"

    # `-f mp4` with `media_type="auto"` on a video course also preserves video stream
    opts_mp4 = _opts(tmp_path, media_type="auto")
    opts_mp4.output_format = "mp4"
    src_auto_mp4 = OReillySource(opts_mp4)
    _wire(src_auto_mp4)
    res_auto_mp4 = src_auto_mp4.download("https://learning.oreilly.com/videos/python-video-course/9780134567890/")
    assert isinstance(res_auto_mp4, Audiobook)
    assert is_video(res_auto_mp4) is True

    # Requesting video mode on a book-only item raises a clear error
    src_book = OReillySource(_opts(tmp_path, media_type="video"))
    src_book.request = lambda _m, url, **_kw: (  # type: ignore[method-assign]
        _json_resp(url, {"identifier": "555", "ourn": "urn:orm:book:555", "name": "Book Only", "type": "book"})
        if url.endswith("/api/v2/metadata/urn:orm:book:555/")
        else _json_resp(url, {}, status_code=404)
    )
    with pytest.raises(GenericAudiobookDLException):
        src_book.download("https://learning.oreilly.com/library/view/-/555/")


def test_real_ffmpeg_video_course_combines_and_merges_per_lesson_mp4(tmp_path):
    import shutil
    import subprocess
    from unittest.mock import MagicMock
    from mutagen.mp4 import MP4
    from audiobookdl import AudiobookFile, AudiobookMetadata, Chapter, Cover
    from audiobookdl.utils import find_executable, probe_audio_info

    ffmpeg_bin = shutil.which("ffmpeg") or find_executable("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe") or find_executable("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        pytest.skip("ffmpeg/ffprobe unavailable")

    def _make_ts(path, freq: int, duration: float = 1.0) -> bytes:
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-f", "lavfi", "-i", f"color=c=blue:s=160x120:d={duration}:r=15",
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "64k",
                "-f", "mpegts",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        return path.read_bytes()

    seg_bytes = [
        _make_ts(tmp_path / "s0.ts", 440, 1.0),
        _make_ts(tmp_path / "s1.ts", 554, 1.0),
        _make_ts(tmp_path / "s2.ts", 659, 1.0),
        _make_ts(tmp_path / "s3.ts", 880, 1.0),
    ]
    # 1x1 valid PNG cover
    import base64
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )

    def _build_ab() -> Audiobook:
        def _resp_for(payload: bytes):
            r = MagicMock()
            r.status_code = 200
            r.headers = {"Content-Length": str(len(payload))}
            r.iter_content.return_value = [payload]
            return r

        session = MagicMock()
        session.get.side_effect = lambda url, **_kw: _resp_for(
            seg_bytes[int(url.rsplit("_", 1)[-1].split(".")[0])]
        )
        files = [
            AudiobookFile(url=f"https://cdn.example.com/seg_{i}.ts", ext="ts", chapter_index=i // 2)
            for i in range(4)
        ]
        return Audiobook(
            session=session,
            files=files,
            metadata=AudiobookMetadata(title="Video Course"),
            chapters=[Chapter(start=0, title="Lesson One"), Chapter(start=2000, title="Lesson Two")],
            cover=Cover(image=tiny_png, extension="png"),
            media_kind="video",
        )

    # 1. Combined single `.mp4` output preserves H.264 video, AAC audio, chapters, cover, and no audiobook stik
    combined_prefix = tmp_path / "out_combined" / "Video Course"
    opts_comb = _opts(tmp_path, media_type="video")
    opts_comb.combine = True
    opts_comb.output_format = "mp4"
    download_audiobook(_build_ab(), str(combined_prefix), opts_comb)

    combined_mp4 = tmp_path / "out_combined" / "Video Course.mp4"
    assert combined_mp4.is_file()
    info = probe_audio_info(str(combined_mp4))
    assert info.video_codec_name == "h264"
    assert info.codec_name == "aac"
    assert info.duration >= 3.5
    chap_out = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_chapters", "-of", "json", str(combined_mp4)],
        check=True,
        capture_output=True,
        text=True,
    )
    chapters_json = json.loads(chap_out.stdout).get("chapters", [])
    assert len(chapters_json) == 2
    mp4_tags = MP4(str(combined_mp4))
    assert "stik" not in mp4_tags
    assert "covr" in mp4_tags

    # 2. Per-lesson output (`combine=False`) merges each clip's segments directly into playable `.mp4` files
    split_prefix = tmp_path / "out_lessons" / "Video Course"
    opts_split = _opts(tmp_path, media_type="video")
    opts_split.combine = False
    opts_split.output_format = "mp4"
    download_audiobook(_build_ab(), str(split_prefix), opts_split)

    lesson1 = tmp_path / "out_lessons" / "Video Course" / "01 - Lesson One.mp4"
    lesson2 = tmp_path / "out_lessons" / "Video Course" / "02 - Lesson Two.mp4"
    assert lesson1.is_file() and lesson2.is_file()
    for lesson_file in (lesson1, lesson2):
        linfo = probe_audio_info(str(lesson_file))
        assert linfo.video_codec_name == "h264"
        assert linfo.codec_name == "aac"
        assert 1.5 <= linfo.duration <= 2.5


@pytest.mark.no_worker_cap
def test_oreilly_parallel_kaltura_preserves_order_and_streaming_epub_zip(tmp_path):
    import threading
    from unittest.mock import MagicMock
    import zipfile
    from audiobookdl import AudiobookFile
    from audiobookdl.sources.oreilly import OReillySource

    src = OReillySource(_opts(tmp_path))
    raw_entries = [
        {"title": f"Ch {i}", "duration": 10 + i, "kaltura_entry_id": f"k_{i}"}
        for i in range(6)
    ]
    src._fetch_chapters_raw = lambda _urn, _info: raw_entries  # type: ignore[method-assign]

    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(4, timeout=2.0)

    def _fake_resolve(manifest_url: str, **_kwargs):
        nonlocal active, peak
        entry_id = manifest_url.rsplit("/", 1)[-1].split(".")[0]
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if entry_id in {"k_0", "k_1", "k_2", "k_3"}:
                barrier.wait()
            return [AudiobookFile(url=f"https://cdn.example.com/{entry_id}.mp4", ext="mp4")]
        finally:
            with lock:
                active -= 1

    src._get_kaltura_session = lambda: ("fake_ks", 12345)  # type: ignore[method-assign]
    src._kaltura_manifest_url = lambda eid: f"https://cdn.example.com/{eid}.m3u8"  # type: ignore[method-assign]
    src.get_stream_files = _fake_resolve  # type: ignore[method-assign]
    files, chapters = src._resolve_audio_files_and_chapters("9780000000001", {"title": "Parallel Book"})
    assert peak >= 2
    assert [f.url for f in files] == [f"https://cdn.example.com/k_{i}.mp4" for i in range(6)]
    assert [c.title for c in chapters] == [f"Ch {i}" for i in range(6)]

    # Streaming EPUB zip archive assembly preserves all files & mimetype
    epub_dest = tmp_path / "out.epub"
    epub_info = {"files": "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:9780000000001/files/"}
    src._fetch_paginated_results = lambda _url: [  # type: ignore[method-assign]
        {"full_path": "META-INF/container.xml", "url": "https://cdn.example.com/container.xml"},
        {"full_path": "OEBPS/content.opf", "url": "https://cdn.example.com/content.opf"},
        {"full_path": "OEBPS/ch01.xhtml", "url": "https://cdn.example.com/ch01.xhtml"},
    ]

    def _fake_get_asset(url, **_kwargs):
        return MagicMock(status_code=200, content=f"<data url='{url}'/>".encode())

    src._session.get = MagicMock(side_effect=_fake_get_asset)
    src._write_epub_archive("9780000000001", epub_info, str(epub_dest))
    assert epub_dest.is_file()
    with zipfile.ZipFile(epub_dest, "r") as zf:
        names = set(zf.namelist())
        assert {"mimetype", "META-INF/container.xml", "OEBPS/content.opf", "OEBPS/ch01.xhtml"} <= names


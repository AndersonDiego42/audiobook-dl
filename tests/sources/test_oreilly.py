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
    assert all(find_compatible_source(b.url) is OReillySource for b in lib_items)

    assert src.search("   ") == []

    src.request = lambda _m, url, **_kw: _json_resp(url, {}, status_code=401)  # type: ignore[method-assign]
    with pytest.raises(UserNotAuthorized):
        src.list_library()

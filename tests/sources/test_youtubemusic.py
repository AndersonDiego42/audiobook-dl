"""Tests for `YouTubeMusicSource` (`music.youtube.com`)."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yt_dlp

from audiobookdl import Audiobook, Series
from audiobookdl.config import normalize_source_key
from audiobookdl.exceptions import (
    BookNotFound,
    MissingBookAccess,
    NoFilesFound,
    NoSourceFound,
    UserNotAuthorized,
)
from audiobookdl.options_schema import build_default_options
from audiobookdl.sources import find_compatible_source, find_source_by_name
from audiobookdl.sources.youtubemusic import YouTubeMusicSource
from audiobookdl.utils.browser_cookies import default_domains_for_service


def _opts(tmp_path: Path, **overrides: Any):
    return build_default_options(database_directory=str(tmp_path / "db"), **overrides)


class _FakeYoutubeDL:
    """Stub for `yt_dlp.YoutubeDL` recording options and returning scripted responses."""

    calls: list[tuple[dict[str, Any], str, bool]] = []
    responses: list[Any] = []
    cookiefile_contents: list[str] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    def __enter__(self) -> _FakeYoutubeDL:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def extract_info(self, url: str, download: bool = False) -> Any:
        self.calls.append((self.opts, url, download))
        cookiefile = self.opts.get("cookiefile")
        if cookiefile and os.path.exists(cookiefile):
            self.cookiefile_contents.append(Path(cookiefile).read_text(encoding="utf-8"))
        if not self.responses:
            raise AssertionError(f"Unexpected YoutubeDL.extract_info call for {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _reset_fake_ydl():
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.responses = []
    _FakeYoutubeDL.cookiefile_contents = []


def test_url_matching_and_aliases():
    assert find_compatible_source("https://music.youtube.com/watch?v=abc123") is YouTubeMusicSource
    assert (
        find_compatible_source("https://music.youtube.com/watch?v=abc123&list=RDAMVMabc123")
        is YouTubeMusicSource
    )
    assert find_compatible_source("https://music.youtube.com/playlist?list=PLxyz") is YouTubeMusicSource
    assert find_compatible_source("https://music.youtube.com/browse/MPREb_12345") is YouTubeMusicSource
    assert find_compatible_source("https://music.youtube.com/browse/VLPLxyz") is YouTubeMusicSource
    assert find_compatible_source("https://music.youtube.com/browse/MPSP123") is YouTubeMusicSource
    assert find_compatible_source("https://music.youtube.com/podcast/show123") is YouTubeMusicSource

    with pytest.raises(NoSourceFound):
        find_compatible_source("https://www.youtube.com/watch?v=abc123")
    with pytest.raises(NoSourceFound):
        find_compatible_source("https://youtu.be/abc123")

    assert normalize_source_key("ytmusic") == "youtubemusic"
    assert normalize_source_key("youtube-music") == "youtubemusic"
    assert normalize_source_key("YouTube Music") == "youtubemusic"
    assert find_source_by_name("ytmusic") is YouTubeMusicSource
    assert default_domains_for_service("youtubemusic") == [
        "music.youtube.com",
        "youtube.com",
        "google.com",
    ]


def test_download_single_track_with_chapters_and_lazy_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    _FakeYoutubeDL.responses = [
        {
            "id": "trk1",
            "title": "Chaptered Audiobook",
            "uploader": "Jane Author - Topic",
            "album": "Audio Collection",
            "track_number": 2,
            "release_date": "20240510",
            "categories": ["Audiobooks"],
            "language": "en",
            "description": "<p>First chapter &amp; more</p>",
            "url": "https://r1---sn.googlevideo.com/videoplayback?id=initial",
            "ext": "m4a",
            "protocol": "https",
            "http_headers": {"User-Agent": "InitialUA"},
            "thumbnails": [
                {"url": "https://lh3.googleusercontent.com/small.jpg", "width": 120, "height": 120},
                {"url": "https://lh3.googleusercontent.com/large.jpg", "width": 1000, "height": 1000},
            ],
            "chapters": [
                {"start_time": 0.0, "title": "Intro"},
                {"start_time": 12.5, "title": "Chapter 1"},
            ],
        },
        {
            "id": "trk1",
            "url": "https://r1---sn.googlevideo.com/videoplayback?id=refreshed",
            "ext": "m4a",
            "protocol": "https",
            "http_headers": {"User-Agent": "RefreshedUA"},
        },
    ]

    src = YouTubeMusicSource(_opts(tmp_path, proxy="http://127.0.0.1:8080", timeout=15.0, max_retries=7))
    cover_bytes = b"\xff\xd8\xff\xe0fake-jpeg"
    fetched_cover_urls: list[str] = []
    src.get = lambda url, **_kw: fetched_cover_urls.append(url) or cover_bytes  # type: ignore[method-assign]

    res = src.download("https://music.youtube.com/watch?v=trk1&list=RDAMVMtrk1&si=abc")
    assert isinstance(res, Audiobook)
    assert res.metadata.title == "Chaptered Audiobook"
    assert res.metadata.authors == ["Jane Author"]
    assert res.metadata.series == "Audio Collection"
    assert res.metadata.series_order == 2
    assert res.metadata.release_date == date(2024, 5, 10)
    assert res.metadata.genres == ["Audiobooks"]
    assert res.metadata.description == "First chapter & more"
    assert res.metadata.source == "youtubemusic"
    assert res.metadata.scrape_url == "https://music.youtube.com/watch?v=trk1"
    assert [(c.start, c.title) for c in res.chapters] == [(0, "Intro"), (12500, "Chapter 1")]
    assert fetched_cover_urls == ["https://lh3.googleusercontent.com/large.jpg"]
    assert res.cover is not None and res.cover.extension == "jpg"

    # Initial call strips ambient list= and enforces noplaylist=True
    opts0, url0, dl0 = _FakeYoutubeDL.calls[0]
    assert url0 == "https://music.youtube.com/watch?v=trk1"
    assert dl0 is False
    assert opts0["noplaylist"] is True
    assert opts0["proxy"] == "http://127.0.0.1:8080"
    assert opts0["socket_timeout"] == 15.0
    assert opts0["retries"] == 7
    assert opts0["extractor_retries"] == 7

    # First url_resolver invocation returns already-extracted URL; second refreshes it
    assert len(res.files) == 1
    assert res.files[0].url_resolver is not None
    assert res.files[0].url_resolver() == "https://r1---sn.googlevideo.com/videoplayback?id=initial"
    assert res.files[0].headers["User-Agent"] == "InitialUA"
    assert len(_FakeYoutubeDL.calls) == 1

    assert res.files[0].url_resolver() == "https://r1---sn.googlevideo.com/videoplayback?id=refreshed"
    assert res.files[0].headers["User-Agent"] == "RefreshedUA"
    assert len(_FakeYoutubeDL.calls) == 2


def test_download_album_returns_chaptered_multitrack_audiobook(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    _FakeYoutubeDL.responses = [
        {
            "id": "MPREb_album1",
            "title": "Dune Audiobook",
            "artists": ["Frank Herbert"],
            "release_date": "20230115",
            "thumbnails": [{"url": "https://lh3.googleusercontent.com/album.jpg", "width": 800, "height": 800}],
            "entries": [
                {"id": "v1", "title": "Part 1", "duration": 60},
                {"id": "v_deleted", "title": "[Deleted video]", "duration": 10},
                {"id": "v2", "title": "Part 2", "duration": 90.5},
            ],
        },
        {
            "id": "v1",
            "url": "https://cdn.example/part1.m4a",
            "ext": "m4a",
            "protocol": "https",
            "http_headers": {"User-Agent": "Part1UA"},
        },
        {
            "id": "v2",
            "url": "https://cdn.example/part2.m4a",
            "ext": "m4a",
            "protocol": "https",
            "http_headers": {"User-Agent": "Part2UA"},
        },
    ]

    src = YouTubeMusicSource(_opts(tmp_path))
    src.get = lambda _url, **_kw: b"\x89PNG\r\n\x1a\nfake-png"  # type: ignore[method-assign]

    res = src.download("https://music.youtube.com/browse/MPREb_album1")
    assert isinstance(res, Audiobook)
    assert res.metadata.title == "Dune Audiobook"
    assert res.metadata.authors == ["Frank Herbert"]
    assert res.metadata.series is None
    assert len(res.files) == 2
    assert [f.title for f in res.files] == ["Part 1", "Part 2"]
    assert [(c.start, c.title) for c in res.chapters] == [(0, "Part 1"), (60000, "Part 2")]
    assert res.cover is not None and res.cover.extension == "png"

    opts0, url0, _ = _FakeYoutubeDL.calls[0]
    assert url0 == "https://music.youtube.com/browse/MPREb_album1"
    assert opts0["extract_flat"] == "in_playlist"
    assert opts0["noplaylist"] is False

    assert res.files[0].url_resolver is not None
    assert res.files[1].url_resolver is not None
    assert res.files[0].url_resolver() == "https://cdn.example/part1.m4a"
    assert res.files[0].headers["User-Agent"] == "Part1UA"
    assert res.files[1].url_resolver() == "https://cdn.example/part2.m4a"
    assert res.files[1].headers["User-Agent"] == "Part2UA"


def test_download_playlist_and_podcast_return_series_and_resolve_items(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    _FakeYoutubeDL.responses = [
        {
            "id": "PL123",
            "title": "Tech Podcast Show",
            "entries": [
                {
                    "id": "ep1",
                    "title": "Episode 1",
                    "uploader": "Host Name - Topic",
                    "thumbnails": [{"url": "https://cdn.example/ep1.jpg", "width": 500, "height": 500}],
                },
                {
                    "id": "ep2",
                    "title": "Episode 2",
                    "uploader": "Host Name - Topic",
                },
            ],
        },
        {
            "id": "ep1",
            "title": "Episode 1",
            "url": "https://cdn.example/ep1.m4a",
            "ext": "m4a",
            "protocol": "https",
            "http_headers": {"User-Agent": "Ep1UA"},
        },
    ]

    src = YouTubeMusicSource(_opts(tmp_path))
    src.get = lambda _url, **_kw: b"\xff\xd8\xff\xe0cover"  # type: ignore[method-assign]

    series = src.download("https://music.youtube.com/browse/MPSPshow1")
    assert isinstance(series, Series)
    assert series.title == "Tech Podcast Show"
    assert len(series.books) == 2
    assert series.books[0].title == "Episode 1"

    book_id = series.books[0]
    assert not isinstance(book_id, Audiobook)
    ep1 = src.download_from_id(book_id.id)
    assert isinstance(ep1, Audiobook)
    assert ep1.metadata.title == "Episode 1"
    assert ep1.metadata.series == "Tech Podcast Show"
    assert ep1.metadata.series_order == 1
    assert ep1.metadata.authors == ["Host Name"]
    assert ep1.files[0].url_resolver is not None
    assert ep1.files[0].url_resolver() == "https://cdn.example/ep1.m4a"


def test_cookies_forwarded_to_ytdlp_and_cleaned_up_and_verify_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    cookie_file = tmp_path / "yt_cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tsecret-sapisid\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsecret-sid\n",
        encoding="utf-8",
    )

    src = YouTubeMusicSource(_opts(tmp_path))
    assert src.verify_auth() is False

    src.load_cookie_file(str(cookie_file))
    src.get = lambda _url, **_kw: b'<html>"LOGGED_IN":true</html>'  # type: ignore[method-assign]
    assert src.verify_auth() is True

    src.get = lambda _url, **_kw: b'<html>"LOGGED_IN":false</html>'  # type: ignore[method-assign]
    assert src.verify_auth() is False

    _FakeYoutubeDL.responses = [
        {
            "id": "priv1",
            "title": "Liked Track",
            "url": "https://cdn.example/priv1.m4a",
            "ext": "m4a",
            "protocol": "https",
        }
    ]
    src.get = lambda _url, **_kw: b""  # type: ignore[method-assign]
    ab = src.download("https://music.youtube.com/watch?v=priv1")
    assert isinstance(ab, Audiobook)
    assert len(_FakeYoutubeDL.cookiefile_contents) == 1
    assert "SAPISID\tsecret-sapisid" in _FakeYoutubeDL.cookiefile_contents[0]
    used_cookie_path = _FakeYoutubeDL.calls[0][0]["cookiefile"]
    assert not os.path.exists(used_cookie_path)


def test_error_mapping_and_hls_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    src = YouTubeMusicSource(_opts(tmp_path))

    _FakeYoutubeDL.responses = [
        yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot"),
    ]
    with pytest.raises(UserNotAuthorized):
        src.download("https://music.youtube.com/watch?v=botcheck")

    _FakeYoutubeDL.responses = [
        yt_dlp.utils.DownloadError("Video unavailable. This video has been removed"),
    ]
    with pytest.raises(BookNotFound):
        src.download("https://music.youtube.com/watch?v=missing1")

    # Authenticated user denied access to members-only item raises MissingBookAccess
    src._session.cookies.set("SAPISID", "tok", domain=".youtube.com")
    _FakeYoutubeDL.responses = [
        yt_dlp.utils.DownloadError("Join this channel to get access to members-only content"),
    ]
    with pytest.raises(MissingBookAccess):
        src.download("https://music.youtube.com/watch?v=membersonly")

    # Track with missing stream URL raises NoFilesFound
    _FakeYoutubeDL.responses = [{"id": "empty1", "title": "No Formats", "formats": []}]
    with pytest.raises(NoFilesFound):
        src.download("https://music.youtube.com/watch?v=empty1")

    # HLS-only track delegates to get_stream_files
    _FakeYoutubeDL.responses = [
        {
            "id": "hls1",
            "title": "HLS Track",
            "url": "https://manifest.googlevideo.com/playlist.m3u8",
            "ext": "m4a",
            "protocol": "m3u8_native",
            "http_headers": {"User-Agent": "HlsUA"},
        }
    ]
    hls_calls: list[tuple[str, dict[str, str] | None]] = []
    fake_seg = SimpleNamespace(url="https://seg.example/0.aac", ext="aac", headers={})
    src.get_stream_files = lambda url, headers=None, **_kw: hls_calls.append((url, headers)) or [fake_seg]  # type: ignore[method-assign]
    src.get = lambda _url, **_kw: b""  # type: ignore[method-assign]
    ab = src.download("https://music.youtube.com/watch?v=hls1")
    assert isinstance(ab, Audiobook)
    assert hls_calls == [("https://manifest.googlevideo.com/playlist.m3u8", {"User-Agent": "HlsUA"})]
    assert ab.files == [fake_seg]

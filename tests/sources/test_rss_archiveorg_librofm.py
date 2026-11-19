"""Tests for RssSource, ArchiveOrgSource, and LibrofmSource."""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from audiobookdl.options_schema import build_default_options
from audiobookdl.exceptions import BookNotFound, UserNotAuthorized
from audiobookdl.sources import find_compatible_source
from audiobookdl.sources.archiveorg import ArchiveOrgSource
from audiobookdl.sources.librofm import LibrofmSource
from audiobookdl.sources.rss import RssSource


def _opts(tmp_path):
    return build_default_options(database_directory=str(tmp_path))


def test_source_url_matching_prioritizes_dedicated_adapters_before_rss():
    assert find_compatible_source("https://archive.org/details/iliad_0801_librivox") is ArchiveOrgSource
    assert find_compatible_source("https://libro.fm/audiobooks/9780593152393") is LibrofmSource
    assert find_compatible_source("https://example.org/podcasts/show.rss") is RssSource
    assert find_compatible_source("https://example.org/audiobook/feed/") is RssSource
    assert find_compatible_source("https://librivox.org/some-book/") is not RssSource


def test_rss_source_parses_reverse_chronological_podcast_and_builds_chapters(tmp_path):
    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <title>Serialized Odyssey</title>
        <itunes:author>Homer</itunes:author>
        <itunes:summary>&lt;p&gt;Epic tale.&lt;/p&gt;</itunes:summary>
        <language>en</language>
        <itunes:image href="/cover.jpg"/>
        <item>
          <title>Part 2: Return</title>
          <pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate>
          <itunes:duration>01:02:03</itunes:duration>
          <enclosure url="/audio/part2.mp3" type="audio/mpeg" length="200"/>
        </item>
        <item>
          <title>Part 1: Departure</title>
          <pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate>
          <itunes:duration>10:00</itunes:duration>
          <enclosure url="/audio/part1.mp3" type="audio/mpeg" length="100"/>
        </item>
      </channel>
    </rss>
    """
    src = RssSource(_opts(tmp_path))
    src.get = lambda url, **_kw: b"img-bytes" if url.endswith("cover.jpg") else rss_xml  # type: ignore[method-assign]

    book = src.download("https://example.org/books/odyssey.xml")
    assert book.title == "Serialized Odyssey"
    assert book.metadata.authors == ["Homer"]
    assert book.metadata.description == "Epic tale."
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "eng"
    assert book.metadata.source == "rss"
    assert book.cover is not None and book.cover.image == b"img-bytes"
    # Reverse chronological pubDates reordered chronologically
    assert [f.url for f in book.files] == [
        "https://example.org/audio/part1.mp3",
        "https://example.org/audio/part2.mp3",
    ]
    assert [c.title for c in book.chapters] == ["Part 1: Departure", "Part 2: Return"]
    assert book.chapters[0].start == 0
    assert book.chapters[1].start == 600_000


def test_archiveorg_source_dedupes_derived_formats_builds_chapters_and_searches(tmp_path):
    meta_payload = {
        "metadata": {
            "title": "The Time Machine",
            "creator": ["H. G. Wells"],
            "description": "<b>Classic</b> sci-fi.",
            "language": "eng",
            "date": "2020-05-01",
            "publisher": "Public Domain Audio",
        },
        "files": [
            {"name": "01_chap.flac", "format": "Flac", "track": "1", "length": "120.5", "title": "Intro"},
            {"name": "01_chap.mp3", "format": "VBR MP3", "track": "1", "length": "120.5", "title": "Intro"},
            {"name": "01_chap_64kb.mp3", "format": "64Kbps MP3", "track": "1", "length": "120.5"},
            {"name": "02_chap.mp3", "format": "VBR MP3", "track": "2", "length": "2:00", "title": "Epilogue"},
            {"name": "cover.jpg", "format": "JPEG"},
        ],
    }
    search_payload = {
        "response": {
            "docs": [
                {"identifier": "time_machine_librivox", "title": "The Time Machine", "creator": "H. G. Wells"}
            ]
        }
    }
    src = ArchiveOrgSource(_opts(tmp_path))

    def fake_get_json(url, **_kw):
        if "advancedsearch.php" in url:
            return search_payload
        return meta_payload

    src.get_json = fake_get_json  # type: ignore[method-assign]
    src.get = lambda _url, **_kw: b"cover-jpg"  # type: ignore[method-assign]

    book = src.download("https://archive.org/details/time_machine_librivox")
    assert book.title == "The Time Machine"
    assert book.metadata.authors == ["H. G. Wells"]
    assert book.metadata.publisher == "Public Domain Audio"
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "eng"
    assert book.metadata.release_date == date(2020, 5, 1)
    assert [f.url for f in book.files] == [
        "https://archive.org/download/time_machine_librivox/01_chap.mp3",
        "https://archive.org/download/time_machine_librivox/02_chap.mp3",
    ]
    assert [c.start for c in book.chapters] == [0, 120_500]
    assert [c.title for c in book.chapters] == ["Intro", "Epilogue"]
    assert book.cover is not None and book.cover.image == b"cover-jpg"

    results = src.search("Time Machine")
    assert len(results) == 1
    assert results[0].url == "https://archive.org/details/time_machine_librivox"

    src.get_json = lambda *_a, **_kw: {"is_dark": True}  # type: ignore[method-assign]
    with pytest.raises(BookNotFound):
        src.download("https://archive.org/details/removed")


def test_librofm_source_login_library_search_and_download_manifest(tmp_path):
    src = LibrofmSource(_opts(tmp_path))
    fake_session = MagicMock()

    def fake_request(method, url, **_kw):
        resp = MagicMock()
        resp.headers = {}
        if url.endswith("/oauth/token"):
            resp.status_code = 200
            resp.json.return_value = {"access_token": "tok_xyz"}
            return resp
        if "/api/v7/library" in url:
            resp.status_code = 200
            resp.content = json.dumps({
                "audiobooks": [
                    {
                        "isbn": "9780593152393",
                        "title": "Dune",
                        "authors": [{"name": "Frank Herbert"}],
                        "narrators": [{"name": "Scott Brick"}],
                        "cover_url": "https://cdn.libro.fm/dune.jpg",
                    }
                ],
                "total_pages": 1,
            }).encode("utf-8")
            return resp
        if "/api/v9/download-manifest" in url:
            resp.status_code = 200
            resp.content = json.dumps({
                "parts": [
                    {"url": "https://cdn.libro.fm/p1.mp3", "title": "Chapter 1", "duration": 60},
                    {"url": "https://cdn.libro.fm/p2.mp3", "title": "Chapter 2", "duration": 90},
                ]
            }).encode("utf-8")
            return resp
        resp.status_code = 200
        resp.content = b"cover-bytes"
        return resp

    src._session = fake_session
    src.request = fake_request  # type: ignore[method-assign]
    src._login("https://libro.fm/audiobooks/9780593152393", "reader@example.com", "pw")
    assert src._access_token == "tok_xyz"

    lib = src.list_library()
    assert len(lib) == 1 and lib[0].title == "Dune"
    assert src.search("frank") == lib

    book = src.download("https://libro.fm/audiobooks/9780593152393")
    assert book.title == "Dune"
    assert book.metadata.isbn == "9780593152393"
    assert [f.url for f in book.files] == [
        "https://cdn.libro.fm/p1.mp3",
        "https://cdn.libro.fm/p2.mp3",
    ]
    assert [c.start for c in book.chapters] == [0, 60_000]
    assert book.cover is not None and book.cover.image == b"cover-bytes"

    # Reject bad credentials cleanly
    bad_resp = MagicMock(status_code=401, headers={})
    src.request = (lambda *_a, **_kw: bad_resp)  # type: ignore[method-assign]
    with pytest.raises(UserNotAuthorized):
        src._login("https://libro.fm/audiobooks/9780593152393", "bad", "bad")


def test_archiveorg_select_audio_files_deduplicates_derivatives_and_m4b_chapters(tmp_path):
    src = ArchiveOrgSource(_opts(tmp_path))

    # 1. Deduplicate _128kb / _vbr / _64kb derivatives against original stem
    deriv_files = [
        {"name": "ch01_64kb.mp3", "format": "64Kbps MP3"},
        {"name": "ch01_128kb.mp3", "format": "128Kbps MP3"},
        {"name": "ch01_vbr.mp3", "format": "VBR MP3", "source": "derivative", "original": "ch01.mp3"},
        {"name": "ch01.mp3", "format": "VBR MP3", "source": "original"},
    ]
    selected_deriv = src._select_audio_files(deriv_files)
    assert [f["name"] for f in selected_deriv] == ["ch01.mp3"]

    # 2. Avoid downloading full-book .m4b AND chapter .mp3s together
    mixed_files = [
        {"name": "FullBook.m4b", "format": "M4B"},
        {"name": "Chapter01.mp3", "format": "VBR MP3", "track": "1"},
        {"name": "Chapter02.mp3", "format": "VBR MP3", "track": "2"},
    ]
    selected_mixed = src._select_audio_files(mixed_files)
    assert [f["name"] for f in selected_mixed] == ["FullBook.m4b"]


def test_rss_atom_iso8601_and_podcast_chapters(tmp_path):
    from audiobookdl.sources.rss import RssSource

    atom_xml = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
      xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <title>Serialized Story</title>
  <subtitle>Great serialized fiction.</subtitle>
  <author><name>Jane Author</name></author>
  <entry>
    <title>Episode 2</title>
    <updated>2024-03-16T10:30:00Z</updated>
    <itunes:duration>00:02:00</itunes:duration>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example.com/ep2.mp3" length="234567" />
  </entry>
  <entry>
    <title>Episode 1</title>
    <published>2024-03-15T10:30:00Z</published>
    <itunes:duration>00:01:00</itunes:duration>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example.com/ep1.mp3" length="123456" />
    <podcast:chapters url="https://cdn.example.com/chapters.json" type="application/json+chapters" />
  </entry>
</feed>
"""
    chapters_json = {
        "version": "1.2.0",
        "chapters": [
            {"startTime": 0.0, "title": "Opening"},
            {"startTime": 42.5, "title": "Plot Twist"},
        ],
    }

    src = RssSource(_opts(tmp_path))

    def _fake_get(url, **_kwargs):
        if url.endswith("feed.atom"):
            return MagicMock(status_code=200, content=atom_xml, headers={"Content-Type": "application/atom+xml"})
        if url.endswith("chapters.json"):
            resp = MagicMock(status_code=200, content=json.dumps(chapters_json).encode("utf-8"), headers={"Content-Type": "application/json"})
            resp.json.return_value = chapters_json
            return resp
        return MagicMock(status_code=404, content=b"", headers={})

    src._session.get = MagicMock(side_effect=_fake_get)
    book = src.download("https://substack.com/feed.atom")
    assert book.metadata.title == "Serialized Story"
    assert book.metadata.authors == ["Jane Author"]
    assert [f.url for f in book.files] == [
        "https://cdn.example.com/ep1.mp3",
        "https://cdn.example.com/ep2.mp3",
    ]
    assert [f.size for f in book.files] == [123456, 234567]
    assert [(c.start, c.title) for c in book.chapters] == [
        (0, "Opening"),
        (42500, "Plot Twist"),
        (60000, "Episode 2"),
    ]


def test_archiveorg_attaches_checksums_and_rss_accepts_flexible_media_mimes(tmp_path):
    sha1_hex = "a" * 40
    md5_hex = "b" * 32
    ia_payload = {
        "metadata": {"title": "Checked IA Book"},
        "files": [
            {
                "name": "01.mp3",
                "format": "VBR MP3",
                "size": "1234",
                "sha1": sha1_hex,
                "md5": md5_hex,
            }
        ],
    }
    ia_src = ArchiveOrgSource(_opts(tmp_path))
    ia_src.get_json = lambda *_a, **_kw: ia_payload  # type: ignore[method-assign]
    ia_book = ia_src.download("https://archive.org/details/checked_ia")
    assert ia_book.files[0].checksum == {"sha1": sha1_hex, "md5": md5_hex}

    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Flexible Feed</title>
        <item>
          <title>Ep 1</title>
          <enclosure url="https://cdn.example.com/stream?id=1" type="video/mp4" length="500"/>
        </item>
        <item>
          <title>Ep 2</title>
          <enclosure url="https://cdn.example.com/stream?id=2" type="application/octet-stream" length="600"/>
        </item>
        <item>
          <title>Cover Image Ignored</title>
          <enclosure url="https://cdn.example.com/cover.jpg" type="image/jpeg" length="50"/>
        </item>
      </channel>
    </rss>"""
    rss_src = RssSource(_opts(tmp_path))
    rss_src.get = lambda *_a, **_kw: rss_xml  # type: ignore[method-assign]
    rss_book = rss_src.download("https://example.org/feed.rss")
    assert [f.title for f in rss_book.files] == ["Ep 1", "Ep 2"]
    assert rss_book.files[0].ext == "mp4"


"""Source-to-tagging round-trip contract tests covering all 17 built-in source adapters."""

from __future__ import annotations

from datetime import date
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any
from collections.abc import Callable

from lxml import etree
from PIL import Image
import pycountry
import pytest

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Series, args
from audiobookdl.output import metadata as output_metadata
from audiobookdl.output.metadata import reader as metadata_reader
from audiobookdl.output.metadata import sidecars
from audiobookdl.output.output import gen_output_location
from audiobookdl.sources import _BUILTIN_SOURCES, find_compatible_source
from audiobookdl.sources.archiveorg import ArchiveOrgSource
from audiobookdl.sources.audiobooksdotcom import AudiobooksdotcomSource
from audiobookdl.sources.blinkist import BlinkistSource
from audiobookdl.sources.bookbeat import BookBeatSource
from audiobookdl.sources.chirp import ChirpSource
from audiobookdl.sources.ereolen import EreolenSource
from audiobookdl.sources.everand import EverandSource
from audiobookdl.sources.librivox import LibrivoxSource
from audiobookdl.sources.librofm import LibrofmSource
from audiobookdl.sources.nextory import NextorySource
from audiobookdl.sources.oreilly import OReillySource
from audiobookdl.sources.overdrive import OverdriveSource
from audiobookdl.sources.podimo import PodimoSource
from audiobookdl.sources.rss import RssSource
from audiobookdl.sources.saxo import SaxoSource
from audiobookdl.sources.source import CatalogBook, Source
from audiobookdl.sources.storytel import StorytelSource
from audiobookdl.sources.yourcloudlibrary import YourCloudLibrarySource


def _make_jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


_SAMPLE_COVER = _make_jpeg_bytes()


@pytest.fixture(scope="module")
def tag_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path] | None:
    """Create cached minimal `.mp3`, `.m4b`, and `.ogg` audio containers via `ffmpeg`."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    cache = tmp_path_factory.mktemp("source_contract_audio")
    specs = {
        "mp3": (cache / "tpl.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]),
        "m4b": (cache / "tpl.m4b", ["-c:a", "aac", "-f", "mp4"]),
        "ogg": (cache / "tpl.ogg", ["-c:a", "libvorbis"]),
    }
    for _ext, (target, codec_args) in specs.items():
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=22050:cl=mono",
                "-t",
                "1.0",
                "-threads",
                "1",
                *codec_args,
                str(target),
            ],
            capture_output=True,
            check=True,
        )
    return {ext: path for ext, (path, _) in specs.items()}


def _new_source(source_cls: type[Source]) -> Any:
    return source_cls(args.build_default_options())


def _fake_session_with_cover(cover_bytes: bytes = _SAMPLE_COVER):
    class _FakeSession:
        def get(self, _url: str, **_kwargs: Any):
            return SimpleNamespace(status_code=200, content=cover_bytes)

        def close(self) -> None:
            pass

    return _FakeSession()


def _build_archiveorg() -> tuple[Source, str]:
    src = _new_source(ArchiveOrgSource)
    src.get_json = lambda url, **_kw: (  # type: ignore[method-assign]
        {
            "response": {
                "docs": [
                    {
                        "identifier": "odyssey-demo",
                        "title": "The Odyssey",
                        "creator": ["Homer"],
                    }
                ]
            }
        }
        if "advancedsearch.php" in url
        else {
            "metadata": {
                "title": "The Odyssey; An Epic",
                "creator": ["Homer"],
                "description": "<p>Ancient epic</p>",
                "language": "eng",
                "date": "2020-04-15",
                "publisher": "Public Domain Press",
                "isbn": "9780000000001",
            },
            "files": [
                {"name": "chapter01.mp3", "format": "VBR MP3", "length": "12.5", "title": "Book I"},
                {"name": "cover.jpg", "format": "JPEG"},
            ],
        }
    )
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://archive.org/details/odyssey-demo"


def _build_audiobooksdotcom() -> tuple[Source, str]:
    src = _new_source(AudiobooksdotcomSource)
    src._session.cookies.set(
        "ci_session",
        json.dumps({"user_agent": "ContractTestAgent"}),
        domain=".audiobooks.com",
    )
    stream_html = (
        b"<html><head>"
        b'<script type="application/ld+json">'
        b'{"@type":"Audiobook","name":"Stream Book","author":[{"name":"Alice Author"}],'
        b'"readBy":[{"name":"Bob Narrator"}],"publisher":{"name":"Audio Pub"},'
        b'"isbn":"9780000000002","description":"Great listen."}'
        b"</script></head><body>"
        b'<img class="bookimage" src="https://www.audiobooks.com/cover.jpg" />'
        b'<script>var cfg = {mp3: "https://cdn.example/part1.mp3", '
        b'chapters: [{"title": "Opening", "start": 0}]};</script>'
        b"</body></html>"
    )
    library_html = (
        b'<html><body><div class="book">'
        b'<a class="no-decoration" href="/audiobook/413879">'
        b'<h3 class="book-title">Stream Book</h3></a>'
        b'<span class="book-author">Alice Author</span>'
        b"</div></body></html>"
    )
    src._get_page = lambda url, **_kw: (  # type: ignore[method-assign]
        library_html if "/browse/library" in url else stream_html
    )
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.audiobooks.com/book/stream/413879"


def _build_blinkist() -> tuple[Source, str]:
    src = _new_source(BlinkistSource)

    def _get_json(url: str, **_kw: Any):
        if "/api/library" in url or "/api/books/search" in url:
            return {
                "books": [
                    {
                        "id": "b1",
                        "slug": "thinking-fast",
                        "title": "Thinking Fast",
                        "language": "en",
                        "author": "Daniel",
                    }
                ]
            }
        return {
            "book": {
                "id": "b1",
                "title": "Thinking Fast",
                "authors": ["Daniel"],
                "narrators": ["Narrator"],
                "about_the_book": "<p>Short summary</p>",
                "categories": [{"label": "Psychology"}],
                "language": "en",
                "cover": {"default": {"src": "https://cdn.example/blink.jpg"}},
            },
            "chapters": [{"id": "c1", "title": "Intro", "duration": 30}],
        }

    src.get_json = _get_json  # type: ignore[method-assign]
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.blinkist.com/en/nc/reader/thinking-fast"


def _build_bookbeat() -> tuple[Source, str]:
    src = _new_source(BookBeatSource)
    src._saved_books_cache = {
        "_embedded": {
            "savedBooks": [
                {
                    "bookid": 12345,
                    "_embedded": {
                        "book": {
                            "bookid": 12345,
                            "title": "Nordic Noir",
                            "author": "A. Writer",
                            "_links": {"web": {"href": "https://www.bookbeat.com/book/12345"}},
                        }
                    },
                    "_links": {"book": {"href": "https://api.bookbeat.com/api/books/12345"}},
                }
            ]
        }
    }

    def _get_json(url: str, **_kw: Any):
        if "/api/books/12345" in url:
            return {
                "title": "Nordic Noir",
                "summary": "Mystery",
                "language": "sv",
                "cover": "https://cdn.example/bb.jpg",
                "series": {"name": "Stockholm", "partnumber": 2},
                "_links": {"web": {"href": "https://www.bookbeat.com/book/12345"}},
                "editions": [
                    {
                        "format": "audioBook",
                        "publisher": {"name": "Nordic Press"},
                        "isbn": "9780000000003",
                        "published": "2023-05-01",
                        "contributors": [
                            {"firstname": "A.", "lastname": "Writer", "role": ["author"]},
                            {"firstname": "B.", "lastname": "Voice", "role": ["narrator"]},
                        ],
                    }
                ],
            }
        if "/api/downloadinfo/" in url:
            return {
                "_embedded": {
                    "downloads": [
                        {
                            "format": "audioBook",
                            "_links": {"license": {"href": "https://api.bookbeat.com/api/license/12345"}},
                        }
                    ]
                }
            }
        if "/api/license/" in url:
            return {
                "_links": {"download": {"href": "https://cdn.example/book.mp4"}},
                "tracks": [{"title": "Chapter 1", "start": 0}],
            }
        if "/api/search" in url:
            return {
                "_embedded": {
                    "books": [
                        {
                            "bookid": 12345,
                            "title": "Nordic Noir",
                            "author": "A. Writer",
                            "_links": {"web": {"href": "https://www.bookbeat.com/book/12345"}},
                        }
                    ]
                }
            }
        raise AssertionError(f"Unexpected BookBeat URL: {url}")

    src.get_json = _get_json  # type: ignore[method-assign]
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.bookbeat.com/book/12345"


def _build_chirp() -> tuple[Source, str]:
    src = _new_source(ChirpSource)
    player_html = (
        b"<html><head><title>Chirp Mystery</title></head><body>"
        b'<div class="user-audiobook" data-audiobook-id="11435746" data-dk="0123456789abcdef"></div>'
        b'<script>window.__STATE__ = {"currentUser":{"id":42}};</script>'
        b'<div class="credit">Written by Jane Author</div>'
        b'<div class="credit">Narrated by John Voice</div>'
        b'<img class="cover-image" data-src="https://cdn.example/chirp.jpg" />'
        b"</body></html>"
    )
    src._get_page = lambda _url, **_kw: player_html  # type: ignore[method-assign]

    def _post_json(_url: str, **kw: Any):
        op = (kw.get("json") or {}).get("operationName")
        if op == "fetchUserLibrary":
            return {
                "data": {
                    "currentUser": {
                        "libraryAudiobooks": [
                            {"id": 11435746, "title": "Chirp Mystery", "authors": [{"name": "Jane Author"}]}
                        ]
                    }
                }
            }
        return {
            "data": {
                "audiobook": {
                    "tracks": [
                        {
                            "partNumber": 1,
                            "chapterNumber": 1,
                            "offsetFromBookStartMs": 0,
                            "durationMs": 1500,
                            "displayName": "Chapter 1",
                        }
                    ]
                }
            }
        }

    src.post_json = _post_json  # type: ignore[method-assign]
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.chirpbooks.com/player/11435746"


def _build_ereolen() -> tuple[Source, str]:
    src = _new_source(EreolenSource)
    page_html = (
        b'<html><body data-fbi-base-url="https://fbi-api.dbc.dk/opac/graphql"></body></html>'
    )
    src._get_page = lambda _url, **_kw: page_html  # type: ignore[method-assign]
    src.post_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "data": {
            "work": {
                "workId": "work-of:870970-basis:53978223",
                "titles": {"full": ["Dansk Lydbog"]},
                "creators": [{"display": "Karen Forfatter"}],
                "abstract": "En god historie.",
                "genreAndForm": ["Roman"],
                "mainLanguages": [{"isoCode": "dan", "display": "Dansk"}],
                "series": [
                    {
                        "title": "Nordserien",
                        "members": [
                            {"work": {"workId": "work-of:870970-basis:53978223"}, "numberInSeries": "1"}
                        ],
                    }
                ],
                "cover": {"detail": {"url": "https://cdn.example/ereolen.jpg"}},
                "manifestations": {
                    "all": [
                        {
                            "materialTypes": [{"materialTypeSpecific": {"display": "lydbog (online)"}}],
                            "identifiers": [{"value": "870970-basis:53978223", "type": {"display": "ISBN"}}],
                            "publisher": ["Gyldendal"],
                            "tableOfContents": {
                                "listOfContents": [{"title": "Kapitel 1", "playingTime": "PT10S"}]
                            },
                        }
                    ]
                },
            }
        }
    }
    src.get_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "loans": [
            {
                "orderId": "ord-1",
                "libraryBook": {
                    "identifier": "870970-basis:53978223",
                    "workId": "work-of:870970-basis:53978223",
                    "title": "Dansk Lydbog",
                    "author": "Karen Forfatter",
                },
            }
        ]
    }
    src.get_stream_files = lambda _url, **_kw: [  # type: ignore[method-assign]
        AudiobookFile(url="https://cdn.example/seg0.ts", ext="ts")
    ]
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://bibliotek.kk.dk/work/work-of:870970-basis:53978223"


def _build_everand() -> tuple[Source, str]:
    src = _new_source(EverandSource)
    show_payload = {
        "doc": {
            "title": "Odyssey, The",
            "author": {"name": "Homer"},
            "narrators": [{"name": "Ian"}],
            "description": "<p>Classic tale</p>",
            "series": {"title": "Classics", "position": 1},
            "isbn": "9780000000004",
            "publisher": {"name": "Penguin"},
            "language": "en",
            "publish_date": "2022-09-15",
            "cover_url": "https://cdn.example/everand.jpg",
        },
        "jwt_token": {"token": "jwt-123"},
        "share_opts": {"id": 579426746},
        "audiobook": {"chapters": [{"chapter_number": 1, "title": "Chapter 1", "duration": 2000}]},
    }
    html = f"<script>ReactDOM.render(Scribd.Audiobooks.Show, {json.dumps(show_payload)});</script>".encode()
    search_payload = json.dumps(
        {
            "results": [
                {
                    "id": 579426746,
                    "title": "The Odyssey",
                    "author": "Homer",
                    "url": "/audiobook/579426746/The-Odyssey",
                }
            ]
        }
    ).encode("utf-8")
    src._get_page = lambda _url, **_kw: html  # type: ignore[method-assign]
    src.post_json = lambda _url, **_kw: {"csrf_token": "ok"}  # type: ignore[method-assign]
    src.get_stream_files = lambda _url, **_kw: [  # type: ignore[method-assign]
        AudiobookFile(url="https://cdn.example/0.ts", ext="ts")
    ]
    src.get_json = lambda _url, **_kw: json.loads(search_payload)  # type: ignore[method-assign]
    src.get = lambda url, **_kw: (  # type: ignore[method-assign]
        search_payload if "search?" in url else _SAMPLE_COVER
    )
    return src, "https://www.everand.com/listen/579426746"


def _build_librivox() -> tuple[Source, str]:
    src = _new_source(LibrivoxSource)
    html = (
        b'<html><body><div class="content-wrap"><h1>LibriVox Tale</h1></div>'
        b'<div class="book-page-book-cover"><img src="https://cdn.example/lv.jpg"/></div>'
        b'<div class="chapter-download"><a class="chapter-name" href="https://cdn.example/ch1.mp3">Ch 1</a></div>'
        b"</body></html>"
    )
    src._get_page = lambda _url, **_kw: html  # type: ignore[method-assign]
    src.get_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "books": [
            {
                "title": "LibriVox Tale",
                "url_librivox": "https://librivox.org/librivox-tale-by-author/",
                "authors": [{"first_name": "Mary", "last_name": "Shelley"}],
                "genres": [{"name": "Fiction"}],
                "language": "English",
                "description": "<p>Public domain classic</p>",
                "copyright_year": "2021",
                "sections": [
                    {
                        "title": "Chapter 1",
                        "playtime": "01:30",
                        "readers": [{"display_name": "Volunteer Reader"}],
                    }
                ],
            }
        ]
    }
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://librivox.org/librivox-tale-by-author/"


def _build_librofm() -> tuple[Source, str]:
    src = _new_source(LibrofmSource)
    src._access_token = "tok"
    src.get_json = lambda url, **_kw: (  # type: ignore[method-assign]
        {
            "audiobooks": [
                {
                    "isbn": "9780123456789",
                    "title": "Libro Odyssey",
                    "authors": ["Indie Author"],
                    "narrators": ["Indie Voice"],
                    "publisher": {"name": "Libro Press"},
                    "description": "Great audiobook",
                    "cover_url": "https://cdn.example/libro.jpg",
                    "url": "https://libro.fm/audiobooks/9780123456789-libro-book",
                }
            ]
        }
        if "download-manifest" not in url
        else {
            "m4b_url": "https://cdn.example/libro.m4b",
            "cover_url": "https://cdn.example/libro.jpg",
            "title": "Libro Book",
        }
    )
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://libro.fm/audiobooks/9780123456789-libro-book"


def _build_nextory() -> tuple[Source, str]:
    src = _new_source(NextorySource)
    product = {
        "id": 99999999,
        "title": "Nextory Saga",
        "authors": [{"name": "S. Writer"}],
        "narrators": [{"name": "N. Reader"}],
        "description_full": "Epic story",
        "language": "sv",
        "series": {"name": "Northern Lights"},
        "volume": 3,
        "formats": [
            {
                "type": "hls",
                "identifier": "fmt-1",
                "img_url": "https://cdn.example/nextory.jpg",
                "publisher": {"name": "Nextory Pub"},
                "isbn": "9780000000005",
                "publication_date": "2023-01-15",
            }
        ],
    }

    def _get_json(url: str, **_kw: Any):
        if "/library/v1/products/99999999" in url:
            return product
        if "/packages/audio" in url:
            return {
                "files": [{"uri": "https://cdn.example/master.m3u8", "start_at": 0, "title": "Part 1"}],
                "chapters": [{"title": "Opening", "start_at": 0}],
            }
        if "/product_lists" in url and "want_to_read" not in url:
            return {"product_lists": [{"type": "want_to_read", "id": "wtr-1"}]}
        if "want_to_read/products" in url:
            return {"products": [product], "has_next": False}
        if "/discovery/v1/search" in url:
            return {"products": [product]}
        raise AssertionError(f"Unexpected Nextory URL: {url}")

    src.get_json = _get_json  # type: ignore[method-assign]
    src.get_stream_files = lambda _url, **_kw: [  # type: ignore[method-assign]
        AudiobookFile(url="https://cdn.example/0.aac", ext="aac")
    ]
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.nextory.com/book/99999999"


def _build_overdrive() -> tuple[Source, str]:
    src = _new_source(OverdriveSource)
    src._session.cookies.set("OverDrive", "valid", domain=".overdrive.com")
    bdata = {
        "title": {"main": "Libby Loan"},
        "description": {"short": "<p>Borrowed book</p>"},
        "publisher": {"name": "OverDrive Press"},
        "isbn": "9780000000006",
        "series": {"name": "Library Series", "position": "1"},
        "creator": [
            {"role": "author", "name": "Lib Author"},
            {"role": "narrator", "name": "Lib Reader"},
        ],
        "-odread-furbish-uri": "cover.jpg",
        "spine": [{"path": "Part1.mp3", "audio-duration": 60.0}],
        "nav": {"toc": [{"title": "Part 1", "path": "Part1.mp3#0"}]},
    }
    html = f"<html><script>window.bData = {json.dumps(bdata)};</script></html>".encode()
    src._get_page = lambda _url, **_kw: html  # type: ignore[method-assign]
    src.get_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "loans": [
            {
                "id": "loan-1",
                "title": "Libby Loan",
                "firstCreatorName": "Lib Author",
                "openUrl": "https://libbyapp.com/open/loan/loan-1",
            }
        ]
    }
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://ofs-demo.listen.overdrive.com/?d=1"


def _build_podimo() -> tuple[Source, str]:
    src = _new_source(PodimoSource)
    book_id = "11111111-2222-3333-4444-555555555555"

    def _post_json(_url: str, **kw: Any):
        op = (kw.get("json") or {}).get("operationName")
        if op == "AudiobookResultsQuery":
            return {
                "data": {
                    "audiobookById": {
                        "title": "Podimo Book",
                        "authors": [{"name": "P. Author"}],
                        "narrators": [{"name": "P. Voice"}],
                        "description": "Podimo description",
                        "publisherName": "Podimo Press",
                        "yearOfBookPublication": 2023,
                        "language": {"isoLanguage": "da"},
                        "coverImage": {"url": "https://cdn.example/podimo.jpg"},
                        "chapters": [{"title": "Chapter 1", "startTime": 0}],
                    }
                }
            }
        if op == "ShortLivedAudiobookMediaUrlQuery":
            return {"data": {"audiobookAudioById": {"url": "https://cdn.example/book.mp3"}}}
        if op == "SearchQuery":
            return {
                "data": {
                    "search": {
                        "audiobooks": [{"id": book_id, "title": "Podimo Book", "authors": [{"name": "P. Author"}]}],
                        "podcasts": [],
                    }
                }
            }
        if op == "LibraryQuery":
            return {
                "data": {
                    "userLibrary": {
                        "audiobooks": [{"id": book_id, "title": "Podimo Book", "authors": [{"name": "P. Author"}]}],
                        "podcasts": [],
                    }
                }
            }
        raise AssertionError(f"Unexpected Podimo op: {op}")

    src.post_json = _post_json  # type: ignore[method-assign]
    src.create_unauthenticated_session = _fake_session_with_cover  # type: ignore[method-assign]
    return src, f"https://open.podimo.com/audiobook/{book_id}"


def _build_rss() -> tuple[Source, str]:
    src = _new_source(RssSource)
    rss_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        b"<channel>"
        b"<title>Podcast Book; Vol 1</title>"
        b"<language>en-US</language>"
        b"<itunes:author>Feed Author</itunes:author>"
        b"<description>&lt;p&gt;Feed description&lt;/p&gt;</description>"
        b'<itunes:image href="https://example.com/cover.jpg" />'
        b"<item><title>Episode 1</title>"
        b'<enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" />'
        b"<itunes:duration>00:05:00</itunes:duration>"
        b"</item>"
        b"</channel></rss>"
    )
    src.get = lambda url, **_kw: rss_xml if url.endswith(".xml") else _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://example.com/podcast/feed.xml"


def _build_saxo() -> tuple[Source, str]:
    src = _new_source(SaxoSource)
    src.user_id = "user1"
    src.bearer_token = "tok"

    def _get_json(url: str, **_kw: Any):
        if "/api/v2/user/" in url or "/api/v2/search/" in url:
            return {
                "items": [
                    {
                        "bookId": "bx1",
                        "title": "Saxo Audiobook",
                        "isbn": "9788702222222",
                        "authors": ["S. Author"],
                    }
                ]
            }
        raise AssertionError(f"Unexpected Saxo GET URL: {url}")

    src.get_json = _get_json  # type: ignore[method-assign]
    src.post_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "items": [
            {
                "bookId": "bx1",
                "bookMetadata": {
                    "title": "Saxo Audiobook",
                    "authors": ["S. Author"],
                    "readBy": ["S. Reader"],
                    "seriesName": "Saxo Series",
                    "description": "<p>Danish thriller</p>",
                    "publisherName": "Saxo Press",
                    "language": "dan",
                    "publishingDate": "2023-04-12",
                    "image": {"highQualityImageUrl": "https://cdn.example/saxo.jpg"},
                },
                "techInfo": {
                    "chapters": [{"fileName": "ch1.mp3", "title": "Kapitel 1", "duration": 30}]
                },
            }
        ]
    }
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.saxo.com/dk/audiobook_9788702222222"


def _build_storytel() -> tuple[Source, str]:
    src = _new_source(StorytelSource)
    src._language = "en"
    details = {
        "consumableId": "9999999",
        "title": "Storytel Book",
        "shareUrl": "https://www.storytel.com/en/books/storytel-book-9999999",
        "authors": [{"name": "St Author"}],
        "narrators": [{"name": "St Voice"}],
        "description": "Good listen",
        "language": "en",
        "category": {"name": "Sci-Fi"},
        "seriesInfo": {"name": "Galaxy", "orderInSeries": 0},
        "cover": {"url": "https://cdn.example/st.jpg"},
        "formats": [
            {
                "type": "abook",
                "isReleased": True,
                "isbn": "9780000000007",
                "publisher": {"name": "Storytel Pub"},
                "releaseDate": "2023-03-01T00:00:00Z",
            }
        ],
    }

    def _request(method: str, url: str, **_kw: Any):
        if "/book-details/consumables/" in url:
            return SimpleNamespace(status_code=200, json=lambda: details, headers={}, text="")
        if "/assets/v2/" in url:
            return SimpleNamespace(
                status_code=302,
                headers={"Location": "https://cdn.example/book.mp3?isbn=9780000000007"},
                text="",
            )
        raise AssertionError(f"Unexpected Storytel request: {method} {url}")

    src.request = _request  # type: ignore[method-assign]
    src.get_json = lambda url, **_kw: (  # type: ignore[method-assign]
        {
            "books": [
                {
                    "id": "9999999",
                    "name": "Storytel Book",
                    "language": "en",
                    "authors": [{"name": "St Author"}],
                }
            ]
        }
        if "search/client/search" in url
        else {
            "formats": [
                {
                    "type": "abook",
                    "chapters": [{"number": 1, "title": "Chapter 1", "durationInMilliseconds": 1000}],
                }
            ]
        }
    )
    src.post_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "items": {
            "item1": {
                "model": {
                    "id": "9999999",
                    "name": "Storytel Book",
                    "language": "en",
                    "authors": [{"name": "St Author"}],
                }
            }
        }
    }
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://www.storytel.com/en/books/storytel-book-9999999"


def _build_yourcloudlibrary() -> tuple[Source, str]:
    src = _new_source(YourCloudLibrarySource)
    src._library_id = "lib1"
    listen_html = (
        b'<html><script>'
        b'"accountId":"acc1","fulfillmentId":"ful1","licenseId":"lic1","session_key":"sk1"'
        b"</script></html>"
    )
    shelf_json = json.dumps(
        {
            "items": [
                {"id": "item123", "title": "Cloud Book", "author": "Cloud Author"},
            ]
        }
    ).encode("utf-8")
    src._get_page = lambda url, **_kw: (  # type: ignore[method-assign]
        shelf_json if "/shelf" in url else listen_html
    )
    src.get_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "audiobook": {
            "title": "Cloud Book",
            "authors": ["Cloud Author"],
            "narrators": ["Cloud Reader"],
            "description": "<p>Cloud description</p>",
            "publisher": {"name": "Cloud Pub"},
            "isbn": "9780000000008",
            "language": "en",
            "series": ["Cloud Saga"],
            "publish_date": "2022-05-01",
            "cover_url": "https://cdn.example/cloud.jpg",
            "chapters": [{"chapter_number": 1, "title": "Part 1", "duration": 2000}],
        }
    }
    src.post_json = lambda _url, **_kw: {  # type: ignore[method-assign]
        "playlist": [{"url": "https://cdn.example/p1.mp3"}]
    }
    src.get = lambda _url, **_kw: _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://audio.yourcloudlibrary.com/listen/item123"


def _build_oreilly() -> tuple[Source, str]:
    src = _new_source(OReillySource)
    src._session.cookies.set("orm-jwt", "contract-jwt", domain=".oreilly.com")
    audio_info = {
        "identifier": "9781000000009",
        "ourn": "urn:orm:audiobook:9781000000009",
        "name": "Fluent Systems",
        "type": "audiobook",
        "talent": {
            "contributors": [
                {"name": "Ada Coder", "contributor_type": "author"},
                {"name": "Voice Reader", "contributor_type": "narrator"},
            ]
        },
        "publishers": [{"name": "O'Reilly Media"}],
        "publication_date": "2024-03-01",
        "language": "en",
        "description": {"text/html": "<p>Systems guide.</p>"},
    }
    toc_payload = {
        "toc": [
            {
                "title": "Chapter 1",
                "duration": 60,
                "reference_id": "9781000000009-clip1",
                "ourn": "urn:orm:audiobook:9781000000009:audio-clip:clip1",
            }
        ]
    }
    m3u8_bytes = b"#EXTM3U\n#EXTINF:60.0,\nhttps://cdn.example/seg0.aac\n#EXT-X-ENDLIST\n"

    def _request(_method: str, url: str, **_kw: Any):
        if "/api/v2/metadata/urn:orm:audiobook:9781000000009/" in url:
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(audio_info).encode("utf-8"),
                json=lambda: audio_info,
            )
        if "/api/v1/videotocs/9781000000009/" in url:
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(toc_payload).encode("utf-8"),
                json=lambda: toc_payload,
            )
        if "/api/v1/videoclips/9781000000009-clip1/" in url:
            clip_payload = {"kaltura_entry_id": "1_abc123"}
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(clip_payload).encode("utf-8"),
                json=lambda: clip_payload,
            )
        if "/api/v1/player/kaltura_session/" in url:
            ks_payload = {"session": "ks-token", "partner_id": "1926081"}
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(ks_payload).encode("utf-8"),
                json=lambda: ks_payload,
            )
        if url.endswith("/api/v3/collections/"):
            col_payload = [
                {
                    "id": "pl1",
                    "items": [
                        {
                            "ourn": "urn:orm:audiobook:9781000000009",
                            "title": "Fluent Systems",
                            "authors": [{"name": "Ada Coder"}],
                            "format": "audiobook",
                            "web_url": "https://learning.oreilly.com/audiobooks/-/9781000000009/",
                        }
                    ],
                }
            ]
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(col_payload).encode("utf-8"),
                json=lambda: col_payload,
            )
        if "/bookmarks/" in url:
            empty_bm = {"results": [], "next": None}
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(empty_bm).encode("utf-8"),
                json=lambda: empty_bm,
            )
        if "/api/v2/search/" in url:
            search_payload = {
                "results": [
                    {
                        "ourn": "urn:orm:audiobook:9781000000009",
                        "title": "Fluent Systems",
                        "authors": ["Ada Coder"],
                        "format": "audiobook",
                        "web_url": "https://learning.oreilly.com/audiobooks/-/9781000000009/",
                    }
                ]
            }
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(search_payload).encode("utf-8"),
                json=lambda: search_payload,
            )
        return SimpleNamespace(status_code=404, url=url, headers={}, content=b"")

    src.request = _request  # type: ignore[method-assign]
    src.get = lambda url, **_kw: m3u8_bytes if ".m3u8" in url else _SAMPLE_COVER  # type: ignore[method-assign]
    return src, "https://learning.oreilly.com/audiobooks/-/9781000000009/"


_SOURCE_CONTRACT_BUILDERS: dict[type[Source], Callable[[], tuple[Source, str]]] = {
    ArchiveOrgSource: _build_archiveorg,
    AudiobooksdotcomSource: _build_audiobooksdotcom,
    BlinkistSource: _build_blinkist,
    BookBeatSource: _build_bookbeat,
    ChirpSource: _build_chirp,
    EreolenSource: _build_ereolen,
    EverandSource: _build_everand,
    LibrivoxSource: _build_librivox,
    LibrofmSource: _build_librofm,
    NextorySource: _build_nextory,
    OReillySource: _build_oreilly,
    OverdriveSource: _build_overdrive,
    PodimoSource: _build_podimo,
    RssSource: _build_rss,
    SaxoSource: _build_saxo,
    StorytelSource: _build_storytel,
    YourCloudLibrarySource: _build_yourcloudlibrary,
}


def test_source_contract_matrix_covers_all_17_builtin_sources():
    assert len(_BUILTIN_SOURCES) == 17
    assert set(_SOURCE_CONTRACT_BUILDERS) == set(_BUILTIN_SOURCES)


@pytest.mark.parametrize("source_cls", _BUILTIN_SOURCES, ids=lambda cls: cls.__name__)
def test_source_to_tagging_round_trip_contract(
    source_cls: type[Source],
    tmp_path: Path,
    tag_templates: dict[str, Path] | None,
):
    builder = _SOURCE_CONTRACT_BUILDERS[source_cls]
    src, sample_url = builder()

    assert find_compatible_source(sample_url) is source_cls

    result = src.download(sample_url)
    if isinstance(result, Series):
        assert result.books
        first = result.books[0]
        ab = first if isinstance(first, Audiobook) else src.download_from_id(first.id)
    else:
        ab = result

    assert isinstance(ab, Audiobook)
    meta = ab.metadata
    assert isinstance(meta, AudiobookMetadata)
    assert isinstance(meta.title, str) and meta.title.strip()
    assert meta.language is None or (
        isinstance(meta.language, pycountry.db.Data) and hasattr(meta.language, "alpha_3")
    )
    assert meta.release_date is None or isinstance(meta.release_date, date)

    formatted_loc = gen_output_location(
        "{author}/{series}/{title}",
        meta,
        remove_chars='<>:"/\\|?*',
    )
    assert formatted_loc and "{" not in formatted_loc and "}" not in formatted_loc

    opf_path = tmp_path / "metadata.opf"
    sidecars.write_sidecar_opf(meta, str(opf_path), cover_filename="cover.jpg")
    parsed_opf = etree.fromstring(opf_path.read_bytes())
    assert parsed_opf is not None
    read_back_opf, _opf_cover, _opf_chapters = metadata_reader.read_sidecar_metadata(str(tmp_path))
    assert read_back_opf is not None
    assert read_back_opf.title == meta.title

    if tag_templates is not None:
        for ext, tpl_path in tag_templates.items():
            audio_target = tmp_path / f"{source_cls.__name__}.{ext}"
            shutil.copy2(tpl_path, audio_target)
            output_metadata.add_metadata(
                str(audio_target),
                meta,
                cover=ab.cover,
                chapters=ab.chapters,
            )
            extracted = metadata_reader.read_track_tags(str(audio_target))
            assert extracted is not None
            assert extracted.title == meta.title

    if getattr(source_cls, "supports_library", False) and source_cls is not LibrivoxSource:
        lib_items = src.list_library()
        assert len(lib_items) >= 1
        for item in lib_items:
            assert isinstance(item, CatalogBook)
            assert find_compatible_source(item.url) is source_cls

    if getattr(source_cls, "supports_search", False):
        search_items = src.search("odyssey")
        assert len(search_items) >= 1
        for item in search_items:
            assert isinstance(item, CatalogBook)
            assert find_compatible_source(item.url) is source_cls

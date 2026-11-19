import base64
from datetime import date
import json
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from audiobookdl import Audiobook, BookId, DownloadOptions, Series
from audiobookdl.sources.audiobooksdotcom import AudiobooksdotcomSource
from audiobookdl.sources.blinkist import BlinkistSource
from audiobookdl.sources.bookbeat import BookBeatSource
from audiobookdl.sources.chirp import ChirpSource
from audiobookdl.sources.ereolen import EreolenSource
from audiobookdl.sources.everand import EverandSource
from audiobookdl.sources.librivox import LibrivoxSource
from audiobookdl.sources.nextory import NextorySource
from audiobookdl.sources.overdrive import OverdriveSource
from audiobookdl.sources.podimo import PodimoSource
from audiobookdl.sources.saxo import SaxoSource
from audiobookdl.sources.yourcloudlibrary import YourCloudLibrarySource


def _opts(tmp_path) -> DownloadOptions:
    return DownloadOptions(database_directory=str(tmp_path), skip_downloaded=False)


def _json_resp(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = json.dumps(payload).encode("utf-8")
    resp.json.return_value = payload
    return resp


def test_librivox_source_parses_html_fixture(tmp_path):
    html = (
        b"<html><body>"
        b'<div class="content-wrap"><h1>The Odyssey</h1></div>'
        b'<div class="book-page-book-cover"><img src="https://librivox.org/cover.jpg"/></div>'
        b'<div class="chapter-download">'
        b'<a class="chapter-name" href="https://archive.org/ch1.mp3">Book 1</a>'
        b'<a class="chapter-name" href="https://archive.org/ch2.mp3">Book 2</a>'
        b"</div></body></html>"
    )
    src = LibrivoxSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            MagicMock(status_code=200, content=b"jpg-bytes"),
        ]
    )
    book = src.download("https://librivox.org/the-odyssey")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "The Odyssey"
    assert [f.url for f in book.files] == [
        "https://archive.org/ch1.mp3",
        "https://archive.org/ch2.mp3",
    ]
    assert [f.title for f in book.files] == ["Book 1", "Book 2"]
    assert book.cover is not None and book.cover.image == b"jpg-bytes"


def test_blinkist_source_parses_api_fixtures(tmp_path):
    book_info = {
        "book": {
            "id": "b1",
            "title": "Blink Book",
            "author": "Jane Doe",
            "language": "en",
            "cover": {"default": {"src": "https://cdn.blinkist.com/cover.jpg"}},
        },
        "chapters": [{"id": "c1"}, {"id": "c2"}],
    }
    src = BlinkistSource(_opts(tmp_path))

    def fake_blinkist_get(url, **_kwargs):
        if url.endswith("/chapters"):
            return _json_resp(book_info)
        if url.endswith("/chapters/c1"):
            return _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c1.m4a"})
        if url.endswith("/chapters/c2"):
            return _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c2.m4a"})
        if url.endswith("/cover.jpg"):
            return MagicMock(status_code=200, content=b"cover-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_blinkist_get)
    book = src.download("https://www.blinkist.com/en/nc/reader/blink-slug?lang=en")
    assert book.metadata.title == "Blink Book"
    assert book.metadata.authors == ["Jane Doe"]
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "eng"
    assert [f.url for f in book.files] == ["", ""]
    assert [f.url_resolver() for f in book.files if f.url_resolver is not None] == [
        "https://cdn.blinkist.com/c1.m4a",
        "https://cdn.blinkist.com/c2.m4a",
    ]
    assert all(f.ext == "m4a" for f in book.files)


def test_bookbeat_source_login_and_download(tmp_path):
    book_meta = {
        "title": "BookBeat Novel",
        "language": "sv",
        "cover": "https://cdn.bookbeat.com/42.jpg",
        "editions": [
            {
                "format": "audioBook",
                "contributors": [
                    {"firstname": "Ada", "lastname": "Lovelace", "role": ["author"]},
                    {"firstname": "Grace", "lastname": "Hopper", "role": ["narrator"]},
                ],
            }
        ],
    }
    dl_info = {
        "_embedded": {
            "downloads": [
                {
                    "format": "audioBook",
                    "_links": {"license": {"href": "https://api.bookbeat.com/api/lic/42"}},
                }
            ]
        }
    }
    lic = {
        "_links": {"download": {"href": "https://cdn.bookbeat.com/42.mp4"}},
        "tracks": [{"start": 0}, {"start": 15000}],
    }
    src = BookBeatSource(_opts(tmp_path))
    src._session.post = MagicMock(return_value=_json_resp({"token": "bb-jwt"}))
    src._session.get = MagicMock(
        side_effect=[
            _json_resp(book_meta),
            _json_resp(dl_info),
            _json_resp(lic),
            MagicMock(status_code=200, content=b"bb-cover"),
        ]
    )
    src.login("https://www.bookbeat.com/book/42", username="u", password="p")
    book = src.download("https://www.bookbeat.com/book/42")
    assert src._saved_books_cache is None
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "BookBeat Novel"
    assert book.metadata.authors == ["Ada Lovelace"]
    assert book.metadata.narrators == ["Grace Hopper"]
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "swe"
    assert [c.start for c in book.chapters] == [0, 15000]
    assert book.files[0].url == "https://cdn.bookbeat.com/42.mp4"


def test_ereolen_source_parses_graphql_and_hls_fixtures(tmp_path):
    work_html = b'<div data-fbi-base-url="https://fbi-api.dbc.dk/opac/graphql"></div>'
    graphql_resp = {
        "data": {
            "work": {
                "titles": {"full": ["Danish Audiobook"]},
                "creators": [{"display": "Author A"}],
                "mainLanguages": [{"isoCode": "dan", "display": "Dansk"}],
                "manifestations": {
                    "all": [
                        {
                            "materialTypes": [{"materialTypeSpecific": {"display": "lydbog (online)"}}],
                            "identifiers": [{"value": "9780000000001"}],
                        }
                    ]
                },
            }
        }
    }
    loans_resp = {
        "loans": [
            {"libraryBook": {"identifier": "9780000000001"}, "orderId": "ord-99"}
        ]
    }
    playlist = b"#EXTM3U\n#EXTINF:5.0,\npart0.ts\n#EXT-X-ENDLIST\n"

    src = EreolenSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=work_html),
            _json_resp(loans_resp),
            MagicMock(status_code=200, content=playlist),
        ]
    )
    src._session.post = MagicMock(return_value=_json_resp(graphql_resp))

    book = src.download("https://ereolen.dk/work/work-of:870970-basis:12345")
    assert book.metadata.title == "Danish Audiobook"
    assert book.metadata.authors == ["Author A"]
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "dan"
    assert len(book.files) == 1
    assert book.files[0].url == "https://audio.api.streaming.pubhub.dk/v1/stream/hls/ord-99/part0.ts"


def test_overdrive_source_parses_bdata_fixture(tmp_path):
    bdata = {
        "title": {"main": "Overdrive Title"},
        "creator": [
            {"role": "author", "name": "Writer X"},
            {"role": "narrator", "name": "Voice Y"},
        ],
        "-odread-furbish-uri": "cover.jpg",
        "spine": [
            {"path": "Part01.mp3", "audio-duration": 10.0},
            {"path": "Part02.mp3", "audio-duration": 20.0},
        ],
        "nav": {
            "toc": [
                {"title": "Intro", "path": "Part01.mp3#0"},
                {"title": "Chapter 2", "path": "Part02.mp3#2.5"},
            ]
        },
    }
    html = f"<script>window.bData = {json.dumps(bdata)};</script>".encode()
    src = OverdriveSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            MagicMock(status_code=200, content=b"od-cover"),
        ]
    )
    book = src.download("https://sample.listen.overdrive.com/")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "Overdrive Title"
    assert book.metadata.authors == ["Writer X"]
    assert book.metadata.narrators == ["Voice Y"]
    assert [c.start for c in book.chapters] == [0, 12500]
    assert [f.url for f in book.files] == [
        "https://sample.listen.overdrive.com/Part01.mp3",
        "https://sample.listen.overdrive.com/Part02.mp3",
    ]


def test_chirp_source_decrypts_track_urls_and_parses_credits(tmp_path):
    user_id = 12345
    key = b"0123456789abcdef"
    iv = base64.b64encode(b"xxxxxxx12345")
    # Chirp strips the trailing padding byte with [:-1] after AES-CBC decryption
    short_url = "https://cdn.chirp.test/trk1.mp3"  # len == 31 + 1 pad byte = 32 bytes
    assert len(short_url) == 31
    encrypted_b64 = base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv).encrypt((short_url + "\x01").encode("utf-8"))
    ).decode("ascii")

    html = (
        f'<html><head><title>Chirp Book</title></head><body>'
        f'<div class="user-audiobook" data-audiobook-id="77" data-dk="{key.decode()}"></div>'
        f'<span class="credit">Written by Alice</span>'
        f'<span class="credit">Narrated by Bob</span>'
        f'<img class="cover-image" src="https://cdn.chirp.test/cover.jpg"/>'
        f'<script>{{"id":{user_id}}}</script>'
        f"</body></html>"
    ).encode()

    tracks_resp = {
        "data": {
            "audiobook": {
                "tracks": [
                    {
                        "partNumber": 1,
                        "chapterNumber": 1,
                        "offsetFromBookStartMs": 0,
                        "durationMs": 5000,
                        "displayName": "Ch 1",
                    }
                ]
            }
        }
    }
    track_url_resp = {
        "data": {"audiobook": {"track": {"webPlayerMediaUrl": encrypted_b64}}}
    }

    src = ChirpSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            MagicMock(status_code=200, content=b"chirp-cover"),
        ]
    )
    src._session.post = MagicMock(
        side_effect=[_json_resp(tracks_resp), _json_resp(track_url_resp)]
    )

    book = src.download("https://www.chirpbooks.com/player/77")
    assert book.metadata.title == "Chirp Book"
    assert book.metadata.authors == ["Alice"]
    assert book.metadata.narrators == ["Bob"]
    assert book.files[0].url == ""
    assert book.files[0].url_resolver is not None
    assert book.files[0].url_resolver() == short_url
    assert book.chapters[0].title == "Ch 1"


def test_chirp_source_unpads_multi_byte_pkcs7_track_urls(tmp_path):
    key = b"0123456789abcdef"
    iv = base64.b64encode(b"xxxxxxx12345")
    src = ChirpSource(_opts(tmp_path))

    for url in (
        "https://cdn.chirp.test/t.mp3",        # pad length == 4
        "https://cdn.chirp.test/track01.mp3",  # len == 32, pad length == 16
    ):
        encrypted_b64 = base64.b64encode(
            AES.new(key, AES.MODE_CBC, iv).encrypt(pad(url.encode("utf-8"), AES.block_size))
        ).decode("ascii")
        src._session.post = MagicMock(
            return_value=_json_resp(
                {"data": {"audiobook": {"track": {"webPlayerMediaUrl": encrypted_b64}}}}
            )
        )
        decrypted = src.get_audio_url(77, key, iv, {"chapterNumber": 1, "partNumber": 1})
        assert decrypted == url


def test_saxo_source_login_search_and_download(tmp_path):
    book_data = {
        "items": [
            {
                "bookId": "saxo-1",
                "techInfo": {"chapters": [{"fileName": "ch01.mp3"}]},
                "bookMetadata": {
                    "title": "Saxo Book",
                    "authors": ["S Author"],
                    "readBy": ["S Narrator"],
                    "seriesName": "S Series",
                    "language": "Dansk",
                    "publishingDate": "2023-09-10T00:00:00Z",
                    "image": {"highQualityImageUrl": "https://saxo.com/c.jpg"},
                },
            }
        ]
    }
    src = SaxoSource(_opts(tmp_path))
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"access_token": "tok", "id": "u1"}),
            _json_resp(book_data),
        ]
    )
    src._session.get = MagicMock(
        side_effect=[
            _json_resp({"items": [{"bookId": "saxo-1"}]}),
            MagicMock(status_code=200, content=b"saxo-cover"),
            _json_resp({"link": "https://stream.saxo.com/ch01.mp3"}),
        ]
    )
    src.login("https://www.saxo.com/dk/book_9781234567890", username="u", password="p")
    book = src.download("https://www.saxo.com/dk/book_9781234567890")
    assert book.metadata.title == "Saxo Book"
    assert book.metadata.series == "S Series"
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "dan"
    assert book.metadata.release_date == date(2023, 9, 10)
    assert book.files[0].url == ""
    assert book.files[0].url_resolver is not None
    assert book.files[0].url_resolver() == "https://stream.saxo.com/ch01.mp3"
    assert book.files[0].encryption_method is not None


def test_yourcloudlibrary_source_parses_listen_page_and_findaway(tmp_path):
    listen_html = (
        b'<script>var cfg = {"accountId":"acc1","fulfillmentId":"ful1",'
        b'"licenseId":"lic1","session_key":"skey1"};</script>'
    )
    book_info = {
        "audiobook": {
            "title": "Cloud Book",
            "authors": ["Cloud Author"],
            "narrators": ["Cloud Narrator"],
            "series": ["Cloud Series"],
            "cover_url": "https://covers.example.com/c.jpg",
            "chapters": [
                {"chapter_number": 1, "duration": 1000},
                {"chapter_number": 2, "duration": 2500},
                {"chapter_number": 3, "duration": 1500},
            ],
        }
    }
    playlist = {"playlist": [{"url": "https://audio.example.com/p1.mp3"}]}

    src = YourCloudLibrarySource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=listen_html),
            _json_resp(book_info),
            MagicMock(status_code=200, content=b"ycl-cover"),
        ]
    )
    src._session.post = MagicMock(return_value=_json_resp(playlist))

    book = src.download("https://ebook.yourcloudlibrary.com/library/lib1/detail/bk99")
    assert book.metadata.title == "Cloud Book"
    assert book.metadata.series == "Cloud Series"
    assert book.files[0].url == "https://audio.example.com/p1.mp3"
    assert [(c.start, c.title) for c in book.chapters] == [
        (0, "Chapter 1"),
        (1000, "Chapter 2"),
        (3500, "Chapter 3"),
    ]


def test_audiobooksdotcom_source_book_and_library_series(tmp_path):
    src = AudiobooksdotcomSource(_opts(tmp_path))
    src._session.cookies.set(
        "ci_session",
        'a:1:{s:10:"user_agent";s:11:"TestAgent/1";}',
        domain="www.audiobooks.com",
    )
    book_html = (
        '<h2 id="bookTitle">AB Book</h2>'
        '<img class="bookimage" src="//cdn.audiobooks.com/c.jpg"/>'
        '<script>var cfg = {mp3: "https://cdn.audiobooks.com/book.mp3"};</script>'
    )
    page_resp = MagicMock(status_code=200, content=book_html.encode("utf-8"), text=book_html)
    src._session.get = MagicMock(
        side_effect=[
            page_resp,
            MagicMock(status_code=200, content=b"ab-cover"),
        ]
    )
    book = src.download("https://www.audiobooks.com/audiobook/title/12345")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "AB Book"
    assert book.files[0].url == "https://cdn.audiobooks.com/book.mp3"
    # Cached _get_page avoids refetching scrape_url in extract_file (page + cover = 2 GETs)
    assert src._session.get.call_count == 2

    lib_html = (
        b'<div class="book"><a class="no-decoration" href="/audiobook/a/111">A</a></div>'
        b'<div class="book"><a class="no-decoration" href="/audiobook/b/222">B</a></div>'
    )
    src_lib = AudiobooksdotcomSource(_opts(tmp_path))
    src_lib._session.cookies.set(
        "ci_session",
        'a:1:{s:10:"user_agent";s:11:"TestAgent/1";}',
        domain="www.audiobooks.com",
    )
    src_lib._session.get = MagicMock(return_value=MagicMock(status_code=200, content=lib_html))
    series = src_lib.download("https://www.audiobooks.com/browse/library")
    assert isinstance(series, Series)
    assert all(isinstance(b, BookId) for b in series.books)
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["111", "222"]


def test_everand_source_parses_external_audiobook_and_cleans_title(tmp_path):
    show_payload = {
        "doc": {
            "title": "Martian, The",
            "author": {"name": "Andy Weir"},
            "narrators": [{"name": "R.C. Bray"}],
            "language": "English",
            "publish_date": "May 4, 2021",
            "cover_url": "https://covers.everand.com/martian.jpg",
        },
        "jwt_token": {"token": None},
        "audiobook": {
            "external_id": "ext1",
            "account_id": "acc1",
            "session_key": "sess1",
        },
    }
    html = f"<script>ReactDOM.render(Scribd.Audiobooks.Show, {json.dumps(show_payload)});</script>".encode()
    src = EverandSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            _json_resp({"licenses": [{"id": "lic1"}]}),
            MagicMock(status_code=200, content=b"ev-cover"),
        ]
    )
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"csrf_token": "tok"}),
            _json_resp({"playlist": [{"chapter_number": 1, "url": "https://cdn.example.com/1.mp3"}]}),
        ]
    )
    book = src.download("https://www.everand.com/audiobook/999/the-martian")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "The Martian"
    assert book.metadata.authors == ["Andy Weir"]
    assert book.metadata.language is not None and book.metadata.language.alpha_3 == "eng"
    assert book.metadata.release_date == date(2021, 5, 4)
    assert book.files[0].url == "https://cdn.example.com/1.mp3"


def test_nextory_source_wantlist_and_book_metadata(tmp_path):
    wantlist = [
        {
            "id": 555,
            "title": "Nextory Book",
            "authors": [{"name": "N Author"}],
            "narrators": [{"name": "N Narrator"}],
            "description_full": "Desc",
            "language": "sv",
            "series": {"name": "N Series", "vol": 0},
            "volume": 2,
            "formats": [
                {
                    "type": "hls",
                    "identifier": "fmt-555",
                    "img_url": "https://img.nextory.com/555.jpg",
                    "publisher": {"name": "Nordic Pub"},
                    "isbn": "9789100000001",
                    "publication_date": "2024-05-01",
                }
            ],
        }
    ]
    audio_data = {
        "files": [
            {
                "uri": "https://cdn.nextory.com/stream/master.m3u8",
                "start_at": 0,
            }
        ]
    }
    playlist = b"#EXTM3U\n#EXTINF:10.0,\nseg0.ts\n#EXT-X-ENDLIST\n"

    src = NextorySource(_opts(tmp_path))

    def fake_nextory_get(url, **_kwargs):
        if url.endswith("/library/v1/products/555"):
            return _json_resp(wantlist[0])
        if url.endswith("/packages/audio"):
            return _json_resp(audio_data)
        if url.endswith("master.m3u8"):
            return MagicMock(status_code=200, content=playlist)
        if url.endswith("555.jpg"):
            return MagicMock(status_code=200, content=b"nextory-cover")
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_nextory_get)
    book = src.download("https://nextory.com/se/book/nextory-book-555")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "Nextory Book"
    assert book.metadata.series == "N Series"
    assert book.metadata.series_order == 2
    assert book.metadata.publisher == "Nordic Pub"
    assert book.metadata.release_date == date(2024, 5, 1)
    assert book.files[0].url == "https://cdn.nextory.com/stream/seg0.ts"


def test_podimo_source_share_link_and_audiobook_metadata(tmp_path):
    book_info_resp = {
        "data": {
            "audiobookById": {
                "title": "Podimo Book",
                "authors": [{"name": "P Author"}],
                "narrators": [{"name": "P Narrator"}],
                "description": "Podimo desc",
                "coverImage": {"url": "https://cdn.podimo.com/cover.png"},
            }
        }
    }
    files_resp = {
        "data": {"audiobookAudioById": {"url": "https://cdn.podimo.com/audio.mp3"}}
    }
    src = PodimoSource(_opts(tmp_path))
    src._session.post = MagicMock(
        side_effect=[_json_resp(book_info_resp), _json_resp(files_resp)]
    )
    unauth_session = MagicMock()
    unauth_session.get.return_value = MagicMock(status_code=200, content=b"pod-cover")
    with (
        patch("audiobookdl.utils.http.redirect_of", return_value="https://open.podimo.com/audiobook/abc-123"),
        patch.object(src, "create_unauthenticated_session", return_value=unauth_session),
    ):
        book = src.download("https://share.podimo.com/s/short")

    assert isinstance(book, Audiobook)
    assert book.metadata.title == "Podimo Book"
    assert book.metadata.authors == ["P Author"]
    assert book.metadata.narrators == ["P Narrator"]
    assert book.files[0].url == "https://cdn.podimo.com/audio.mp3"
    assert book.cover is not None and book.cover.extension == "png"


def test_overdrive_handles_short_toc_and_cumulative_offsets(tmp_path):
    bdata = {
        "title": {"main": "Short TOC Book"},
        "creator": [],
        "-odread-furbish-uri": "cover.jpg",
        "spine": [
            {"path": "Part01.mp3", "audio-duration": 10.0},
            {"path": "Part02.mp3", "audio-duration": 20.0},
            {"path": "Part03.mp3", "audio-duration": 30.0},
        ],
        "nav": {
            "toc": [
                {"title": "Opening", "path": "Part01.mp3#0"},
                {"title": "Finale", "path": "Part03.mp3#5.0"},
            ]
        },
    }
    html = f"<script>window.bData = {json.dumps(bdata)};</script>".encode()
    src = OverdriveSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            MagicMock(status_code=200, content=b"od-cover"),
        ]
    )
    book = src.download("https://sample.listen.overdrive.com/")
    assert isinstance(book, Audiobook)
    assert [f.title for f in book.files] == ["Opening", "Finale", "Part 3"]
    assert [(c.start, c.title) for c in book.chapters] == [
        (0, "Opening"),
        (35000, "Finale"),
    ]


def test_bookbeat_preserves_case_insensitive_headers_and_paginates(tmp_path):
    page_1 = {
        "_embedded": {"savedBooks": [{"bookid": i} for i in range(100)]},
        "_links": {"next": {"href": "https://api.bookbeat.com/api/my/books/saved?offset=100&limit=100"}},
    }
    page_2 = {
        "_embedded": {"savedBooks": [{"bookid": 100}]},
    }
    src = BookBeatSource(_opts(tmp_path))
    src._session.post = MagicMock(return_value=_json_resp({"token": "bb-jwt"}))
    src._session.get = MagicMock(side_effect=[_json_resp(page_1), _json_resp(page_2)])

    src.login("https://www.bookbeat.com/book/100", username="u", password="p")
    assert src._session.headers["Authorization"] == "Bearer bb-jwt"
    assert len(src.saved_books["_embedded"]["savedBooks"]) == 101


def test_ereolen_error_paths(tmp_path):
    import pytest
    from audiobookdl.exceptions import BookHasNoAudiobook, DataNotPresent, RequestError

    src = EreolenSource(_opts(tmp_path))
    page_html = b'<div data-fbi-base-url="https://fbi-api.dbc.dk/opac/graphql"></div>'

    # 1. GraphQL returns None -> RequestError
    src._session.get = MagicMock(return_value=MagicMock(status_code=200, content=page_html))
    src._session.post = MagicMock(return_value=MagicMock(status_code=500, content=b""))
    with pytest.raises(RequestError):
        src.download("https://ereolen.dk/work/work-of:123")

    # 2. No audiobook manifestation -> BookHasNoAudiobook
    no_audio_graphql = {
        "data": {
            "work": {
                "titles": {"full": ["Ebook Only"]},
                "creators": [],
                "manifestations": {
                    "all": [{"materialTypes": [{"materialTypeSpecific": {"display": "ebog"}}]}]
                },
            }
        }
    }
    src._session.get = MagicMock(return_value=MagicMock(status_code=200, content=page_html))
    src._session.post = MagicMock(return_value=_json_resp(no_audio_graphql))
    with pytest.raises(BookHasNoAudiobook):
        src.download("https://ereolen.dk/work/work-of:123")

    # 3. Audiobook exists but not in active user loans -> DataNotPresent
    audio_graphql = {
        "data": {
            "work": {
                "titles": {"full": ["Loaned Audio"]},
                "creators": [],
                "manifestations": {
                    "all": [
                        {
                            "materialTypes": [{"materialTypeSpecific": {"display": "lydbog (online)"}}],
                            "identifiers": [{"value": "id-999"}],
                        }
                    ]
                },
            }
        }
    }
    src3 = EreolenSource(_opts(tmp_path))
    src3._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=page_html),
            _json_resp({"loans": [{"libraryBook": {"identifier": "other"}, "orderId": "ord-1"}]}),
        ]
    )
    src3._session.post = MagicMock(return_value=_json_resp(audio_graphql))
    with pytest.raises(DataNotPresent):
        src3.download("https://ereolen.dk/work/work-of:123")


def test_saxo_missing_isbn_and_yourcloudlibrary_malformed_json(tmp_path):
    import pytest
    from audiobookdl.exceptions import DataNotPresent, NoSourceFound

    saxo = SaxoSource(_opts(tmp_path))
    with pytest.raises(NoSourceFound):
        saxo._extract_isbn("https://www.saxo.com/dk/book-without-trailing-digits")

    ycl = YourCloudLibrarySource(_opts(tmp_path))
    assert (
        ycl.get_listening_url("https://ebook.yourcloudlibrary.com/library/demo/detail/book-42")
        == "https://audio.yourcloudlibrary.com/listen/book-42"
    )
    ycl._session.get = MagicMock(return_value=MagicMock(status_code=200, content=b"<html>no json here</html>"))
    with pytest.raises(DataNotPresent):
        ycl.extract_json_string("https://audio.yourcloudlibrary.com/listen/book-42", "accountId")


def test_librivox_missing_chapters_and_catalog_list_library_edge_cases(tmp_path):
    lv = LibrivoxSource(_opts(tmp_path))
    lv._session.get = MagicMock(return_value=MagicMock(status_code=200, content=b"<html><body><h1>Empty</h1></body></html>"))
    assert lv.get_files("https://librivox.org/empty") == []
    assert lv._parse_feed_books({"books": [{"title": "", "url_librivox": ""}, "not-a-dict"]}) == []

    ab_src = AudiobooksdotcomSource(_opts(tmp_path))
    ab_src._session.get = MagicMock(
        return_value=MagicMock(
            status_code=200,
            content=b"<html><body><ul></ul></body></html>",
        )
    )
    assert ab_src.list_library() == []

    nxt = NextorySource(_opts(tmp_path))
    nxt.download_want_to_read_list = lambda: [  # type: ignore[method-assign]
        "not-a-dict",
        {"title": "No ID"},
        {"id": 10, "title": "Valid Nextory"},
    ]
    items = nxt.list_library()
    assert len(items) == 1
    assert items[0].title == "Valid Nextory"
    assert items[0].url == "https://www.nextory.com/book/10"


def test_everand_wires_chapters_selects_highest_bandwidth_and_escapes_url_regex(tmp_path):
    import pytest
    from audiobookdl.exceptions import NoSourceFound
    from audiobookdl.sources import find_compatible_source

    assert not hasattr(EverandSource, "download_book_id")
    assert find_compatible_source("https://www.everand.com/listen/12345") is EverandSource
    with pytest.raises(NoSourceFound):
        find_compatible_source("https://wwwXeverandXcom/listen/12345")

    show_payload = {
        "doc": {
            "title": "Internal Book",
            "author": {"name": "Author A"},
            "narrators": [{"name": "Narrator N"}],
            "cover_url": "https://covers.everand.com/internal.jpg",
        },
        "share_opts": {"id": "777"},
        "jwt_token": {"token": "jwt-token"},
        "chapters": [
            {"chapter_number": 0, "duration": 5000},
            {"chapter_number": 1, "duration": 10000},
        ],
    }
    html = f"<script>ReactDOM.render(Scribd.Audiobooks.Show, {json.dumps(show_payload)});</script>".encode()
    master_m3u8 = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=64000\n"
        b"64kbps.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=192000\n"
        b"192kbps.m3u8\n"
    )
    media_m3u8 = b"#EXTM3U\n#EXTINF:10.0,\nseg0.ts\n#EXT-X-ENDLIST\n"

    src = EverandSource(_opts(tmp_path))

    def fake_get(url, **_kwargs):
        if "everand.com/listen/777" in url:
            return MagicMock(status_code=200, content=html)
        if url.endswith("/777/master.m3u8"):
            return MagicMock(status_code=200, content=master_m3u8)
        if url.endswith("/777/192kbps.m3u8"):
            return MagicMock(status_code=200, content=media_m3u8)
        if url == "https://covers.everand.com/internal.jpg":
            return MagicMock(status_code=200, content=b"cover-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)
    src._session.post = MagicMock(return_value=_json_resp({"csrf_token": "tok"}))

    book = src.download("https://www.everand.com/listen/777")
    assert isinstance(book, Audiobook)
    assert [f.url for f in book.files] == [
        "https://audio.production.scribd.com/audiobooks/777/seg0.ts"
    ]
    assert [(c.start, c.title) for c in book.chapters] == [
        (0, "Introduction"),
        (5000, "Chapter 1"),
    ]


def test_yourcloudlibrary_login_raises_on_failed_auth_and_succeeds_on_valid_response(tmp_path):
    import pytest
    from audiobookdl.exceptions import UserNotAuthorized

    src = YourCloudLibrarySource(_opts(tmp_path))
    src._session.get = MagicMock(return_value=MagicMock(status_code=200, content=b"ok"))

    for bad_resp in (
        MagicMock(status_code=401, content=b""),
        MagicMock(status_code=403, content=b""),
        MagicMock(status_code=400, content=b'{"error": "Invalid"}'),
        MagicMock(status_code=200, content=b'{"error": "Invalid barcode"}'),
        MagicMock(status_code=200, content=b'{"success": false}'),
        MagicMock(status_code=200, content=b'{"patron": null}'),
    ):
        src._session.post = MagicMock(return_value=bad_resp)
        with pytest.raises(UserNotAuthorized):
            src.login("https://ebook.yourcloudlibrary.com/library/lib1/detail/bk1", username="u", password="p", library="lib1")

    src._session.post = MagicMock(return_value=MagicMock(status_code=200, content=b'{"ok": true, "error": null}'))
    src.login("https://ebook.yourcloudlibrary.com/library/lib1/detail/bk1", username="u", password="p", library="lib1")
    assert src.authenticated is True


def test_saxo_empty_search_results_raises_book_not_found(tmp_path):
    import pytest
    from audiobookdl.exceptions import BookNotFound

    src = SaxoSource(_opts(tmp_path))
    src._session.post = MagicMock(return_value=_json_resp({"access_token": "tok", "id": "u1"}))
    src._session.get = MagicMock(return_value=_json_resp({"items": []}))
    src.login("https://www.saxo.com/dk/book_9781234567890", username="u", password="p")

    with pytest.raises(BookNotFound):
        src.download("https://www.saxo.com/dk/book_9781234567890")


def test_section7_adapters_metadata_catalog_search_and_library(tmp_path):
    import pytest
    from audiobookdl.exceptions import UserNotAuthorized
    from audiobookdl.sources import find_compatible_source
    from audiobookdl.sources.storytel import StorytelSource

    # 1. audiobooks.com: full metadata + chapters + preserved order list_library()
    ab = AudiobooksdotcomSource(_opts(tmp_path))
    ab._session.cookies.set(
        "ci_session",
        'a:1:{s:10:"user_agent";s:11:"TestAgent/1";}',
        domain="www.audiobooks.com",
    )
    ab_html = (
        '<h2 id="bookTitle">Rich AB</h2>'
        '<span class="book-author"><a>Author One</a></span>'
        '<span class="book-narrator"><a>Reader One</a></span>'
        '<span class="book-series"><a>Galaxy Saga #3</a></span>'
        '<div id="book-description">Long summary here.</div>'
        '<img class="bookimage" src="https://cdn.audiobooks.com/c.jpg"/>'
        '<ul id="chapters"><li data-start="0">Prologue</li><li data-start="15">Part One</li></ul>'
        '<script>var cfg = {mp3: "https://cdn.audiobooks.com/ab.mp3"};</script>'
    )
    ab._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=ab_html.encode("utf-8")),
            MagicMock(status_code=200, content=b"jpg"),
        ]
    )
    book = ab.download("https://www.audiobooks.com/audiobook/rich/99")
    assert isinstance(book, Audiobook)
    assert book.metadata.authors == ["Author One"]
    assert book.metadata.narrators == ["Reader One"]
    assert book.metadata.series == "Galaxy Saga"
    assert book.metadata.series_order == 3
    assert book.metadata.description == "Long summary here."
    assert [(c.start, c.title) for c in book.chapters] == [(0, "Prologue"), (15000, "Part One")]

    lib_order_html = (
        b'<div class="book"><a class="no-decoration" href="/audiobook/second/20">'
        b'<span class="book-title">Zebra First</span></a><span class="book-author">Alice</span></div>'
        b'<div class="book"><a class="no-decoration" href="/audiobook/first/10">'
        b'<span class="book-title">Alpha Second</span></a><span class="book-author">Bob</span></div>'
    )
    ab._session.get = MagicMock(return_value=MagicMock(status_code=200, content=lib_order_html))
    items = ab.list_library()
    assert [(i.title, i.author) for i in items] == [
        ("Zebra First", "Alice"),
        ("Alpha Second", "Bob"),
    ]

    # 2. blinkist: non-/en/ /books/ URL, chapters/narrators/description, search & list_library
    assert find_compatible_source("https://www.blinkist.com/de/books/atomic-habits") is BlinkistSource
    bl = BlinkistSource(_opts(tmp_path))
    bl_info = {
        "book": {
            "id": "b99",
            "title": "Blink German",
            "author": "Author B",
            "narrators": [{"name": "Speaker B"}],
            "about_the_book": "<p>Clear <b>insights</b>.</p>",
            "language": "de",
            "cover": {"default": {"src": "https://cdn.blinkist.com/b99.jpg"}},
        },
        "chapters": [
            {"id": "c1", "title": "Intro", "duration": 10},
            {"id": "c2", "title": "Key Idea", "duration": 25},
        ],
    }
    bl._session.get = MagicMock(
        side_effect=[
            _json_resp(bl_info),
            _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c1.m4a"}),
            _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c2.m4a"}),
            MagicMock(status_code=200, content=b"jpg"),
        ]
    )
    bl_book = bl.download("https://www.blinkist.com/de/books/b99")
    assert bl_book.metadata.narrators == ["Speaker B"]
    assert bl_book.metadata.description == "Clear insights."
    assert [(c.start, c.title) for c in bl_book.chapters] == [(0, "Intro"), (10000, "Key Idea")]
    bl._session.get = MagicMock(return_value=_json_resp({"books": [{"slug": "s1", "title": "Hit 1", "author": "Auth"}]}))
    assert bl.search("hit")[0].title == "Hit 1"

    # 3. bookbeat: arbitrary catalog URL download without saved_books + search + persisted UUID4 device_id
    bb = BookBeatSource(_opts(tmp_path))
    bb._session.get = MagicMock(
        side_effect=[
            _json_resp({"title": "Catalog Book", "cover": "https://cdn.bookbeat.com/9.jpg", "editions": []}),
            _json_resp({"_embedded": {"downloads": [{"format": "audioBook", "_links": {"license": {"href": "https://api.bookbeat.com/lic/9"}}}]}}),
            _json_resp({"_links": {"download": {"href": "https://cdn.bookbeat.com/9.mp4"}}, "tracks": [{"start": 0}]}),
            MagicMock(status_code=200, content=b"cover"),
        ]
    )
    bb_book = bb.download("https://www.bookbeat.com/book/catalog-9?ref=share")
    assert isinstance(bb_book, Audiobook)
    assert bb_book.metadata.title == "Catalog Book"
    assert bb.get_or_create_device_id(
        "bookbeat_device_id.txt", bb.create_device_id
    ) == bb.get_or_create_device_id(
        "bookbeat_device_id.txt", bb.create_device_id
    )
    bb._session.get = MagicMock(return_value=_json_resp({"_embedded": {"books": [{"bookid": 5, "title": "Found", "author": "BB Auth"}]}}))
    assert bb.search("found")[0].title == "Found"

    # 4. chirp: /audiobooks/... URL support and list_library()
    assert find_compatible_source("https://www.chirpbooks.com/audiobooks/dune") is ChirpSource
    ch = ChirpSource(_opts(tmp_path))
    ch._session.post = MagicMock(
        return_value=_json_resp({"data": {"currentUser": {"libraryAudiobooks": [{"id": 77, "title": "Purchased", "authors": [{"name": "A"}]}]}}})
    )
    assert ch.list_library()[0].url == "https://www.chirpbooks.com/player/77"

    # 5. ereolen: cover, ISBN, publisher, series, description, chapters & clear error message
    eo = EreolenSource(_opts(tmp_path))
    work_html = b'<div data-fbi-base-url="https://fbi-api.dbc.dk/opac/graphql"></div><meta property="og:image" content="https://covers.dbc.dk/c.jpg">'
    graphql_rich = {
        "data": {
            "work": {
                "workId": "work-of:1",
                "titles": {"full": ["Rich Danish"]},
                "abstract": "Great mystery.",
                "creators": [{"display": "Author DK"}],
                "series": [{"title": "Nordic", "members": [{"numberInSeries": 2, "work": {"workId": "work-of:1"}}]}],
                "mainLanguages": [{"isoCode": "dan", "display": "Dansk"}],
                "genreAndForm": ["krimi"],
                "manifestations": {
                    "all": [
                        {
                            "materialTypes": [{"materialTypeSpecific": {"display": "lydbog (online)"}}],
                            "identifiers": [{"type": "ISBN", "value": "9788700000001"}],
                            "publisher": ["Gyldendal"],
                            "contents": {"entries": [{"title": {"display": "Kapitel 1"}, "playingTime": "PT10S"}, {"title": {"display": "Kapitel 2"}, "playingTime": "PT5S"}]},
                        }
                    ]
                },
            }
        }
    }
    eo._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=work_html),
            _json_resp({"loans": [{"libraryBook": {"identifier": "9788700000001"}, "orderId": "ord-1"}]}),
            MagicMock(status_code=200, content=b"#EXTM3U\n#EXTINF:5.0,\np0.ts\n#EXT-X-ENDLIST\n"),
            MagicMock(status_code=200, content=b"eo-cover"),
        ]
    )
    eo._session.post = MagicMock(return_value=_json_resp(graphql_rich))
    eo_book = eo.download("https://ereolen.dk/work/work-of:1")
    assert eo_book.metadata.isbn == "9788700000001"
    assert eo_book.metadata.publisher == "Gyldendal"
    assert eo_book.metadata.series == "Nordic"
    assert eo_book.metadata.series_order == 2
    assert eo_book.cover is not None and eo_book.cover.image == b"eo-cover"
    assert [(c.start, c.title) for c in eo_book.chapters] == [(0, "Kapitel 1"), (10000, "Kapitel 2")]

    # 6. everand: /book/ URL support, full metadata, search(), list_library()
    assert find_compatible_source("https://www.everand.com/book/12345/slug") is EverandSource
    ev = EverandSource(_opts(tmp_path))
    assert ev.create_listen_url("https://www.everand.com/book/12345/slug") == "https://www.everand.com/listen/12345"
    ev_meta = ev.format_metadata(
        {
            "title": "Dune, The",
            "author": {"name": "Frank Herbert"},
            "narrators": [],
            "isbn": "9780441172719",
            "series": {"title": "Dune Chronicles", "position": 1},
            "publisher": {"name": "Chilton"},
            "language": "en",
            "description": "<p>Spice.</p>",
        }
    )
    assert (ev_meta.title, ev_meta.isbn, ev_meta.series, ev_meta.series_order, ev_meta.publisher, ev_meta.description) == (
        "The Dune",
        "9780441172719",
        "Dune Chronicles",
        1,
        "Chilton",
        "Spice.",
    )
    ev._session.get = MagicMock(return_value=_json_resp({"documents": [{"id": 55, "title": "Result Book", "author": {"name": "Ev Auth"}}]}))
    assert ev.list_library()[0].title == "Result Book"

    # 7. librivox: RSS id triggers /api/feed/audiobooks metadata + chapters enrichment
    lv = LibrivoxSource(_opts(tmp_path))
    lv_html = (
        b'<html><body><a href="https://librivox.org/rss/44">RSS</a>'
        b'<div class="content-wrap"><h1>Moby Dick</h1></div>'
        b'<div class="book-page-book-cover"><img src="https://librivox.org/moby.jpg"/></div>'
        b'<div class="chapter-download"><a class="chapter-name" href="https://archive.org/1.mp3">Ch 1</a></div>'
        b"</body></html>"
    )
    lv_api = {
        "books": [
            {
                "title": "Moby Dick",
                "authors": [{"first_name": "Herman", "last_name": "Melville"}],
                "genres": [{"name": "Fiction"}],
                "language": "English",
                "description": "<p>Call me Ishmael.</p>",
                "sections": [
                    {"title": "Loomings", "playtime": "00:02:00", "readers": [{"display_name": "Stewart Wills"}]}
                ],
            }
        ]
    }
    lv._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=lv_html),
            _json_resp(lv_api),
            MagicMock(status_code=200, content=b"lv-cover"),
        ]
    )
    lv_book = lv.download("https://librivox.org/moby-dick")
    assert lv_book.metadata.authors == ["Herman Melville"]
    assert lv_book.metadata.narrators == ["Stewart Wills"]
    assert lv_book.metadata.description == "Call me Ishmael."
    assert [(c.start, c.title) for c in lv_book.chapters] == [(0, "Loomings")]
    assert LibrivoxSource.supports_library is False
    assert find_compatible_source("https://librivox.org/moby-dick") is LibrivoxSource
    assert find_compatible_source("https://www.librivox.org/moby-dick") is LibrivoxSource
    lv._session.get = MagicMock(
        side_effect=[
            _json_resp({"books": [{"title": "Title Hit", "url_librivox": "https://librivox.org/1", "authors": []}]}),
            _json_resp({
                "books": [
                    {"title": "Title Hit", "url_librivox": "https://librivox.org/1", "authors": []},
                    {"title": "Author Hit", "url_librivox": "https://librivox.org/2", "authors": [{"first_name": "Herman", "last_name": "Melville"}]},
                ]
            }),
        ]
    )
    lv_search = lv.search("Melville")
    assert [b.url for b in lv_search] == ["https://librivox.org/1", "https://librivox.org/2"]

    # 8. overdrive: Libby URLs + chip validation + rich metadata
    assert find_compatible_source("https://libbyapp.com/open/loan/999") is OverdriveSource
    od = OverdriveSource(_opts(tmp_path))
    with pytest.raises(UserNotAuthorized):
        od.download("https://libbyapp.com/open/loan/999")
    od._session.cookies.set("session", "valid", domain="libbyapp.com")
    od._session.get = MagicMock(
        return_value=_json_resp({"loans": [{"id": "999", "title": "Libby Book", "firstCreatorName": "L Auth", "openUrl": "https://demo.listen.overdrive.com/"}]})
    )
    assert od.list_library()[0].title == "Libby Book"

    # 9. saxo: enriched metadata + chapters + configurable app headers
    sx = SaxoSource(_opts(tmp_path))
    sx_meta = sx.get_metadata(
        {
            "bookMetadata": {
                "title": "Saxo Rich",
                "authors": ["A"],
                "readBy": ["N"],
                "description": "<p>Plot</p>",
                "publisherName": "PubHouse",
                "language": "en",
                "publishingDate": "2023-05-01T00:00:00Z",
            }
        },
        isbn="9781234567890",
    )
    assert sx_meta.isbn == "9781234567890"
    assert sx_meta.description == "Plot"
    assert sx_meta.publisher == "PubHouse"
    assert str(sx_meta.release_date) == "2023-05-01"

    # 10. storytel: externalized metadata_corrections + search()
    st = StorytelSource(_opts(tmp_path))
    st._session.get = MagicMock(
        return_value=_json_resp(
            {"books": [{"book": {"id": "500", "name": "Story Hit", "authors": [{"name": "St Auth"}]}}]}
        )
    )
    st_hits = st.search("Story")
    assert st_hits[0].title == "Story Hit"
    assert st_hits[0].url == "https://www.storytel.com/en/books/500"
    assert find_compatible_source(st_hits[0].url) is StorytelSource

    # 11. yourcloudlibrary: broader URL support + list_library()
    assert find_compatible_source("https://ebook.yourcloudlibrary.com/library/lib1/audiobook/bk-2") is YourCloudLibrarySource
    ycl = YourCloudLibrarySource(_opts(tmp_path))
    ycl._library_id = "lib1"
    ycl._session.get = MagicMock(
        return_value=_json_resp({"loans": [{"id": "bk-2", "title": "Cloud Book", "author": "YCL Auth"}]})
    )
    ycl_items = ycl.list_library()
    assert len(ycl_items) == 1
    assert ycl_items[0].title == "Cloud Book"
    assert ycl_items[0].url == "https://ebook.yourcloudlibrary.com/library/lib1/detail/bk-2"


def test_p1_adapter_fixes_overdrive_bookbeat_ycl_ereolen_saxo(tmp_path):
    from audiobookdl import Series

    # 1. Overdrive shelf returns Series of loans + nested TOC part titles are distinct
    od = OverdriveSource(_opts(tmp_path))
    od._session.cookies.set("session", "valid", domain="libbyapp.com")
    od._session.get = MagicMock(
        return_value=_json_resp(
            {
                "loans": [
                    {"id": "101", "title": "Loan One", "firstCreatorName": "A1"},
                    {"id": "102", "title": "Loan Two", "firstCreatorName": "A2"},
                ]
            }
        )
    )
    shelf_res = od.download("https://libbyapp.com/shelf/loans")
    assert isinstance(shelf_res, Series)
    assert [b.title for b in shelf_res.books] == ["Loan One", "Loan Two"]

    nested_bdata = {
        "spine": [{"path": "Part1.mp3"}, {"path": "Part2.mp3"}],
        "nav": {
            "toc": [
                {
                    "title": "Section 1",
                    "contents": [
                        {"title": "Chapter One", "path": "Part1.mp3#0"},
                        {"title": "Chapter Two", "path": "Part2.mp3#0"},
                    ],
                }
            ]
        },
    }
    od_files = od.get_files("https://demo.listen.overdrive.com", nested_bdata)
    assert [f.title for f in od_files] == ["Chapter One", "Chapter Two"]

    # 2. BookBeat: get_chapters works before get_files, track titles + extended metadata
    bb = BookBeatSource(_opts(tmp_path))
    bb_info = {
        "bookid": 77,
        "metadata": {
            "title": "BB Rich",
            "series": {"name": "Saga", "partnumber": 4},
            "editions": [
                {
                    "format": "audioBook",
                    "isbn": "9789100000077",
                    "publisher": {"name": "BB Pub"},
                    "published": "2024-02-01",
                    "contributors": [],
                }
            ],
        },
    }
    bb._session.get = MagicMock(
        side_effect=[
            _json_resp({"_embedded": {"downloads": [{"format": "audioBook", "_links": {"license": {"href": "https://api.bookbeat.com/lic/77"}}}]}}),
            _json_resp({"_links": {"download": {"href": "https://cdn.bookbeat.com/77.mp4"}}, "tracks": [{"start": 0, "title": "Prologue"}, {"start": 5000, "title": "Act I"}]}),
        ]
    )
    chapters = bb.get_chapters(bb_info)
    assert [(c.start, c.title) for c in chapters] == [(0, "Prologue"), (5000, "Act I")]
    bb_meta = bb.get_metadata(bb_info)
    assert (bb_meta.isbn, bb_meta.publisher, bb_meta.series, bb_meta.series_order) == ("9789100000077", "BB Pub", "Saga", 4)
    assert bb_meta.scrape_url == "https://www.bookbeat.com/book/77"

    # 3. YourCloudLibrary: real chapter titles, metadata fields, and JSON body
    ycl = YourCloudLibrarySource(_opts(tmp_path))
    ycl._session.post = MagicMock(return_value=_json_resp({"playlist": []}))
    ycl.download_playlist("ful1", "lic1")
    assert ycl._session.post.call_args.kwargs["json"] == {"license_id": "lic1"}
    ycl_chapters = ycl.create_chapters({"chapters": [{"chapter_number": 1, "title": "Opening", "duration": 1000}]})
    assert [(c.start, c.title) for c in ycl_chapters] == [(0, "Opening")]
    ycl_meta = ycl.get_metadata(
        {
            "title": "Cloud Rich",
            "authors": ["Auth"],
            "narrators": ["Narr"],
            "description": "<p>Summary</p>",
            "isbn": "9780001",
            "publisher": "YCL Pub",
            "language": "en",
        },
        url="https://audio.yourcloudlibrary.com/listen/bk99",
    )
    assert (ycl_meta.description, ycl_meta.isbn, ycl_meta.publisher, ycl_meta.scrape_url) == (
        "Summary",
        "9780001",
        "YCL Pub",
        "https://audio.yourcloudlibrary.com/listen/bk99",
    )
    assert ycl_meta.language is not None and ycl_meta.language.alpha_3 == "eng"

    # 4. eReolen & Saxo: list_library() & search()
    eo = EreolenSource(_opts(tmp_path))
    assert EreolenSource.supports_library is True
    eo._session.get = MagicMock(return_value=_json_resp({"loans": [{"libraryBook": {"workId": "work-of:42", "title": "Loaned DK", "author": "Auth DK"}}]}))
    eo_lib = eo.list_library()
    assert [(b.title, b.url) for b in eo_lib] == [("Loaned DK", "https://ereolen.dk/work/work-of:42")]

    sx = SaxoSource(_opts(tmp_path))
    sx.user_id = "u1"
    sx.bearer_token = "tok"
    assert SaxoSource.supports_search is True and SaxoSource.supports_library is True
    sx._session.get = MagicMock(return_value=_json_resp({"items": [{"bookId": "s1", "bookMetadata": {"title": "Saxo Search", "isbn13": "9781111111111", "authors": ["S Auth"]}}]}))
    sx_hits = sx.search("Saxo Search")
    assert sx_hits[0].title == "Saxo Search"
    assert sx_hits[0].author == "S Auth"


def test_source_metadata_language_and_release_date_serialize_to_all_output_formats(tmp_path):
    """Verify normalized source metadata feeds OPF, JSON, templates, and LibriVox/M4B tagging."""
    import json as _json
    from audiobookdl.output import output as out_mod
    from audiobookdl.output.metadata import sidecars

    lv = LibrivoxSource(_opts(tmp_path))
    lv._session.get = MagicMock(
        side_effect=[
            MagicMock(
                status_code=200,
                content=(
                    b'<html><body><div class="content-wrap"><h1>The Odyssey</h1></div>'
                    b'<div class="book-page-book-cover"><img src="https://librivox.org/cover.jpg"/></div>'
                    b'<a href="https://librivox.org/rss/42">RSS</a>'
                    b'<div class="chapter-download"><a class="chapter-name" href="https://archive.org/1.mp3">Ch 1</a></div>'
                    b"</body></html>"
                ),
            ),
            _json_resp(
                {
                    "books": [
                        {
                            "title": "The Odyssey",
                            "language": "English",
                            "copyright_year": "1914",
                            "authors": [{"first_name": "Homer", "last_name": ""}],
                        }
                    ]
                }
            ),
            MagicMock(status_code=200, content=b"jpg-bytes"),
        ]
    )
    lv_book = lv.download("https://librivox.org/the-odyssey")
    assert lv_book.metadata.language is not None and lv_book.metadata.language.alpha_3 == "eng"
    assert lv_book.metadata.release_date == date(1914, 1, 1)
    assert out_mod.gen_output_location("{title} [{language}] ({year})", lv_book.metadata, "") == "The Odyssey [eng] (1914)"
    opf_path = tmp_path / "lv.opf"
    sidecars.write_sidecar_opf(lv_book.metadata, str(opf_path))
    assert "<dc:language>en</dc:language>" in opf_path.read_text(encoding="utf-8")
    assert _json.loads(lv_book.metadata.as_json())["language"] == "eng"


def test_section23_adapter_scraping_routing_and_auth_fixes(tmp_path):
    from audiobookdl.sources import find_compatible_source

    # 1. OverDrive: semicolons inside bData, nested TOC chapters, and verify_auth false-positive fix
    od_bdata = {
        "title": {"main": "Tom &amp; Jerry; A Story"},
        "description": "Part 1; Part 2 &quot;quoted&quot;",
        "creator": [{"role": "author", "name": "A. Author"}],
        "-odread-furbish-uri": "cover.jpg",
        "spine": [
            {"path": "Part01.mp3", "audio-duration": 10.0},
            {"path": "Part02.mp3", "audio-duration": 20.0},
        ],
        "nav": {
            "toc": [
                {
                    "title": "Book I",
                    "path": "Part01.mp3#0",
                    "contents": [
                        {"title": "Nested Ch 1", "path": "Part01.mp3#2.5"},
                        {"title": "Nested Ch 2", "path": "Part02.mp3#5.0"},
                    ],
                }
            ]
        },
    }
    od_html = f'<script>window.bData = {json.dumps(od_bdata)}; var x = 1;</script>'.encode()
    od = OverdriveSource(_opts(tmp_path))
    od._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=od_html),
            MagicMock(status_code=200, content=b"od-cover"),
        ]
    )
    od_book = od.download("https://sample.listen.overdrive.com/")
    assert od_book.metadata.title == "Tom &amp; Jerry; A Story"
    assert [(c.start, c.title) for c in od_book.chapters] == [
        (0, "Book I"),
        (2500, "Nested Ch 1"),
        (15000, "Nested Ch 2"),
    ]
    # verify_auth must return False on 401/UserNotAuthorized even when cookies are loaded
    od._session.cookies.set("session", "expired", domain="libbyapp.com")
    od.load_cookie_file = MagicMock()
    od._Source__authenticated = True  # type: ignore[attr-defined]
    od._session.get = MagicMock(return_value=MagicMock(status_code=401, content=b"unauthorized"))
    assert od.verify_auth() is False

    # 2. Chirp: ignores earlier non-user "id" in HTML and extracts text inside nested <a> credits
    user_id = 98765
    key = b"0123456789abcdef"
    iv = base64.b64encode(b"xxxxxxx98765")
    track_url = "https://cdn.chirp.test/track01.mp3"
    encrypted_b64 = base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv).encrypt(pad(track_url.encode("utf-8"), AES.block_size))
    ).decode("ascii")
    chirp_html = (
        f'<html><head><title>Chirp Fixed</title></head><body>'
        f'<div class="user-audiobook" data-audiobook-id="77" data-dk="{key.decode()}"></div>'
        f'<span class="credit">Written by <a href="/a">Alice Smith</a></span>'
        f'<span class="credit">Narrated by <span>Bob Jones</span></span>'
        f'<img class="cover-image" src="https://cdn.chirp.test/cover.jpg"/>'
        f'<script>{{"id":999999,"__typename":"Audiobook","currentUser":{{"id":{user_id},"__typename":"User"}}}}</script>'
        f"</body></html>"
    ).encode()
    ch = ChirpSource(_opts(tmp_path))
    ch._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=chirp_html),
            MagicMock(status_code=200, content=b"chirp-cover"),
        ]
    )
    ch._session.post = MagicMock(
        side_effect=[
            _json_resp({"data": {"audiobook": {"tracks": [{"partNumber": 1, "chapterNumber": 1, "durationMs": 4000, "displayName": "Intro"}]}}}),
            _json_resp({"data": {"audiobook": {"track": {"webPlayerMediaUrl": encrypted_b64}}}}),
        ]
    )
    ch_book = ch.download("https://www.chirpbooks.com/player/77")
    assert ch_book.metadata.authors == ["Alice Smith"]
    assert ch_book.metadata.narrators == ["Bob Jones"]
    assert ch_book.files[0].url == ""
    assert ch_book.files[0].url_resolver is not None
    assert ch_book.files[0].url_resolver() == track_url

    # 3. Audiobooks.com: trailing slash, multi-part /book/stream/<id>/<n>, dotted domain & opaque ci_session
    ab = AudiobooksdotcomSource(_opts(tmp_path))
    ab._session.cookies.set("ci_session", "opaque-modern-token-value", domain=".audiobooks.com")
    part1_html = (
        '<h2 id="bookTitle">Multi AB</h2>'
        '<img class="bookimage" src="https://cdn.audiobooks.com/c.jpg"/>'
        '<a href="/book/stream/12345/2">Part 2</a>'
        '<script>var cfg = {mp3: "https://cdn.audiobooks.com/p1.mp3"};</script>'
    )
    part2_html = '<script>var cfg = {mp3: "https://cdn.audiobooks.com/p2.mp3"};</script>'

    def fake_ab_get(url, **_kwargs):
        if url.endswith("/book/stream/12345/1"):
            return MagicMock(status_code=200, content=part1_html.encode())
        if url.endswith("/book/stream/12345/2"):
            return MagicMock(status_code=200, content=part2_html.encode())
        if url == "https://cdn.audiobooks.com/c.jpg":
            return MagicMock(status_code=200, content=b"ab-cover")
        raise AssertionError(f"Unexpected GET {url}")

    ab._session.get = MagicMock(side_effect=fake_ab_get)
    ab_book = ab.download("https://www.audiobooks.com/audiobook/multi-ab/12345/")
    assert [f.url for f in ab_book.files] == [
        "https://cdn.audiobooks.com/p1.mp3",
        "https://cdn.audiobooks.com/p2.mp3",
    ]
    assert [f.title for f in ab_book.files] == ["Part 1", "Part 2"]
    assert find_compatible_source("https://www.audiobooks.com/book/stream/12345/12/") is AudiobooksdotcomSource

    # 4. eReolen: _login with empty url defaults to ereolen.dk & non-greedy user token regex
    eo = EreolenSource(_opts(tmp_path))
    login_html = (
        b'<form id="borchk-login-form" action="/borchk"></form>'
        b'<input id="libraryid-input" value="710100"/>'
        b'<input id="libraryname-input" value="DemoLib"/>'
    )
    tokens_js = b'window.setToken("user", "tok_abc"); window.setToken("other", "ignored");'
    eo_urls: list[str] = []

    def fake_eo_get(url, **_kwargs):
        eo_urls.append(url)
        if url == "https://ereolen.dk/login":
            return MagicMock(status_code=200, content=login_html)
        if url == "https://ereolen.dk/dpl-react/user-tokens":
            return MagicMock(status_code=200, content=tokens_js)
        if "pubhub-openplatform.dbc.dk/v1/user/loans" in url:
            return _json_resp({"loans": []})
        raise AssertionError(f"Unexpected GET {url}")

    eo._session.get = MagicMock(side_effect=fake_eo_get)
    eo._session.post = MagicMock(return_value=MagicMock(status_code=200, content=b"ok"))
    eo.login("", username="u", password="p")
    assert eo_urls[0] == "https://ereolen.dk/login"
    assert eo._session.headers["Authorization"] == "Bearer tok_abc"
    assert eo.verify_auth() is True
    eo._session.get = MagicMock(return_value=MagicMock(status_code=401, content=b""))
    assert eo.verify_auth() is False

    # 5. YourCloudLibrary: string series is not sliced to first character
    ycl = YourCloudLibrarySource(_opts(tmp_path))
    ycl_meta = ycl.get_metadata(
        {"title": "Final Empire", "authors": ["B. Sanderson"], "narrators": [], "series": "Mistborn"}
    )
    assert ycl_meta.series == "Mistborn"

    # 6. Everand: download_series extracts numeric id before slug
    ev = EverandSource(_opts(tmp_path))
    ev._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=b"<html><h1>My Series</h1></html>"),
            _json_resp({"compilation": {"modules": [{"documents": [{"id": 11}, {"id": 22}]}]}}),
        ]
    )
    ev_series = ev.download("https://www.everand.com/series/12345/Series-Title-Slug")
    assert isinstance(ev_series, Series)
    assert ev_series.title == "My Series"
    assert [b.id for b in ev_series.books] == [11, 22]
    assert "https://www.everand.com/series/12345/data" in ev._session.get.call_args_list[1].args[0]


def test_auth_sources_verify_auth_rejects_expired_and_unauthenticated(tmp_path):
    from audiobookdl.sources.audiobooksdotcom import AudiobooksdotcomSource
    from audiobookdl.sources.bookbeat import BookBeatSource
    from audiobookdl.sources.chirp import ChirpSource
    from audiobookdl.sources.everand import EverandSource
    from audiobookdl.sources.librofm import LibrofmSource
    from audiobookdl.sources.nextory import NextorySource
    from audiobookdl.sources.podimo import PodimoSource
    from audiobookdl.sources.saxo import SaxoSource
    from audiobookdl.sources.storytel import StorytelSource
    from audiobookdl.sources.yourcloudlibrary import YourCloudLibrarySource

    opts = _opts(tmp_path)

    # 1. Cookie sources with empty cookie jar return False cleanly
    for cls in (AudiobooksdotcomSource, ChirpSource, EverandSource, YourCloudLibrarySource):
        s = cls(opts)
        assert s.verify_auth() is False

    # 2. Rejected 401/302/error probe responses return False cleanly even when cookies/tokens exist
    ab = AudiobooksdotcomSource(opts)
    ab._session.cookies.set("ci_session", "stale", domain="www.audiobooks.com")
    ab._session.get = MagicMock(return_value=MagicMock(status_code=200, url="https://www.audiobooks.com/login", content=b"<form>Log In</form>"))
    assert ab.verify_auth() is False

    ch = ChirpSource(opts)
    ch._session.cookies.set("_session", "stale", domain="www.chirpbooks.com")
    ch._session.post = MagicMock(return_value=_json_resp({"data": {"currentUser": None}}))
    assert ch.verify_auth() is False

    ev = EverandSource(opts)
    ev._session.cookies.set("_scribd_session", "stale", domain="www.everand.com")
    ev._session.get = MagicMock(return_value=MagicMock(status_code=401, content=b""))
    assert ev.verify_auth() is False

    ycl = YourCloudLibrarySource(opts)
    ycl._session.cookies.set("auth", "stale", domain="ebook.yourcloudlibrary.com")
    ycl._session.get = MagicMock(return_value=MagicMock(status_code=401, content=b""))
    assert ycl.verify_auth() is False

    for cls, setup_fn in [
        (BookBeatSource, lambda s: s._session.headers.update({"Authorization": "Bearer expired"})),
        (StorytelSource, lambda s: s._session.headers.update({"Authorization": "Bearer expired"})),
        (NextorySource, lambda s: s._session.headers.update({"X-Profile-Token": "expired"})),
        (PodimoSource, lambda s: setattr(s, "_token", "expired")),
        (LibrofmSource, lambda s: setattr(s, "_access_token", "expired")),
        (SaxoSource, lambda s: s._session.headers.update({"Authorization": "Bearer expired"})),
    ]:
        s = cls(opts)
        assert s.verify_auth() is False
        setup_fn(s)
        s._session.get = MagicMock(return_value=MagicMock(status_code=401, content=b""))
        s._session.post = MagicMock(return_value=MagicMock(status_code=401, content=b""))
        assert s.verify_auth() is False


def test_everand_yourcloudlibrary_and_nextory_hardening(tmp_path):
    import pytest
    from audiobookdl.exceptions import GenericAudiobookDLException, UserNotAuthorized
    from audiobookdl.sources.everand import EverandSource
    from audiobookdl.sources.nextory import NextorySource
    from audiobookdl.sources.yourcloudlibrary import YourCloudLibrarySource

    # Everand missing authors does not raise IndexError
    ev = EverandSource(_opts(tmp_path))
    meta = ev.format_metadata({"title": "Solo Title", "authors": []}, url="https://www.everand.com/book/1")
    assert meta.title == "Solo Title"
    assert meta.authors == []

    # YourCloudLibrary invalid JSON on login raises UserNotAuthorized, missing library raises actionable error
    ycl = YourCloudLibrarySource(_opts(tmp_path))
    ycl._session.post = MagicMock(return_value=MagicMock(status_code=200, content=b"<html>Login error</html>"))
    with pytest.raises(UserNotAuthorized):
        ycl._login("https://ebook.yourcloudlibrary.com/library/demo/MyBooks", "user", "pw", "demo")
    ycl_no_lib = YourCloudLibrarySource(_opts(tmp_path))
    with pytest.raises(GenericAudiobookDLException, match="library"):
        ycl_no_lib.list_library()

    # Nextory respects configured locale over domain fallback
    nx = NextorySource(_opts(tmp_path))
    nx._configured_locale = "de_DE"
    assert nx._resolve_locale("https://www.nextory.com/book/1") == "de_DE"


def test_bookbeat_yourcloudlibrary_librofm_isolate_cdn_sessions_and_headers(tmp_path):
    from audiobookdl.sources.librofm import LibrofmSource

    # 1. BookBeat (audiobook + ebook) strips Authorization while keeping client headers & unauth session
    bb = BookBeatSource(_opts(tmp_path))
    bb._session.headers.update(
        {
            "accept": "application/hal+json",
            "bb-client": "BookBeatApp",
            "bb-device": "dev-1",
            "authorization": "Bearer bb-secret-jwt",
        }
    )
    bb._session.cookies.set("bb_sid", "bb-cookie", domain="bookbeat.com")
    bb_meta = {"title": "BB Safe", "cover": "https://cdn.bookbeat.com/cover.jpg", "editions": []}
    bb._session.get = MagicMock(
        side_effect=[
            _json_resp(bb_meta),
            _json_resp(
                {
                    "_embedded": {
                        "downloads": [
                            {"format": "audioBook", "_links": {"license": {"href": "https://api.bookbeat.com/lic/1"}}},
                            {"format": "eBook", "_links": {"license": {"href": "https://api.bookbeat.com/elic/1"}}},
                        ]
                    }
                }
            ),
            _json_resp({"_links": {"download": {"href": "https://cdn.bookbeat.com/1.mp4"}}, "tracks": [{"start": 0}]}),
            MagicMock(status_code=200, content=b"cover"),
        ]
    )
    bb_book = bb.download("https://www.bookbeat.com/book/1")
    assert isinstance(bb_book, Audiobook)
    assert bb_book.session is not bb._session
    assert "authorization" not in {k.lower() for k in bb_book.session.headers}
    assert bb_book.session.cookies.get("bb_sid") is None
    bb_hdrs = {k.lower(): v for k, v in bb_book.files[0].headers.items()}
    assert bb_hdrs.get("bb-client") == "BookBeatApp"
    assert bb_hdrs.get("bb-device") == "dev-1"
    assert "authorization" not in bb_hdrs

    # 2. YourCloudLibrary isolates Audiobook.session from Session-Key and cookies
    ycl = YourCloudLibrarySource(_opts(tmp_path))
    ycl._session.cookies.set("ycl_sid", "ycl-cookie", domain="yourcloudlibrary.com")
    listen_html = (
        b'"accountId":"acc1","fulfillmentId":"ful1","licenseId":"lic1","session_key":"sk-secret"'
    )
    ycl._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=listen_html),
            _json_resp({"audiobook": {"title": "YCL Safe", "authors": ["A"], "narrators": ["N"], "chapters": []}}),
        ]
    )
    ycl._session.post = MagicMock(
        return_value=_json_resp({"playlist": [{"url": "https://cdn.findawayworld.com/p1.mp3"}]})
    )
    ycl_book = ycl.download("https://audio.yourcloudlibrary.com/listen/bk1")
    assert ycl_book.session is not ycl._session
    assert "session-key" not in {k.lower() for k in ycl_book.session.headers}
    assert ycl_book.session.cookies.get("ycl_sid") is None

    # 3. Libro.fm isolates Audiobook.session from OAuth bearer token
    lf = LibrofmSource(_opts(tmp_path))
    lf._access_token = "libro-oauth-secret"
    lf._session.headers["Authorization"] = "Bearer libro-oauth-secret"
    lf._session.cookies.set("lf_sid", "lf-cookie", domain="libro.fm")
    lf.get_json = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            {"audiobooks": []},
            {"m4b_url": "https://cdn.libro.fm/book.m4b", "title": "Libro Safe"},
        ]
    )
    lf_book = lf.download("https://libro.fm/audiobooks/9780593152393")
    assert lf_book.session is not lf._session
    assert "authorization" not in {k.lower() for k in lf_book.session.headers}
    assert lf_book.session.cookies.get("lf_sid") is None


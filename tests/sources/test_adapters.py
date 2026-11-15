import base64
from datetime import date
import json
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES

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
            "cover": {"default": {"src": "https://cdn.blinkist.com/cover.jpg"}},
        },
        "chapters": [{"id": "c1"}, {"id": "c2"}],
    }
    src = BlinkistSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            _json_resp(book_info),
            _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c1.m4a"}),
            _json_resp({"signed_audio_url": "https://cdn.blinkist.com/c2.m4a"}),
            MagicMock(status_code=200, content=b"cover-bytes"),
        ]
    )
    book = src.download("https://www.blinkist.com/en/nc/reader/blink-slug?lang=en")
    assert book.metadata.title == "Blink Book"
    assert book.metadata.authors == ["Jane Doe"]
    assert [f.url for f in book.files] == [
        "https://cdn.blinkist.com/c1.m4a",
        "https://cdn.blinkist.com/c2.m4a",
    ]
    assert all(f.ext == "m4a" for f in book.files)


def test_bookbeat_source_login_and_download(tmp_path):
    saved_books = {
        "_embedded": {
            "savedBooks": [
                {
                    "bookid": 42,
                    "_links": {"book": {"href": "https://api.bookbeat.com/api/books/42"}},
                }
            ]
        }
    }
    book_meta = {
        "title": "BookBeat Novel",
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
            _json_resp(saved_books),
            _json_resp(book_meta),
            _json_resp(dl_info),
            _json_resp(lic),
            MagicMock(status_code=200, content=b"bb-cover"),
        ]
    )
    src.login("https://www.bookbeat.com/book/42", username="u", password="p")
    book = src.download("https://www.bookbeat.com/book/42")
    assert book.metadata.title == "BookBeat Novel"
    assert book.metadata.authors == ["Ada Lovelace"]
    assert book.metadata.narrators == ["Grace Hopper"]
    assert [c.start for c in book.chapters] == [0, 15000]
    assert book.files[0].url == "https://cdn.bookbeat.com/42.mp4"


def test_ereolen_source_parses_graphql_and_hls_fixtures(tmp_path):
    work_html = b'<div data-fbi-base-url="https://fbi-api.dbc.dk/opac/graphql"></div>'
    graphql_resp = {
        "data": {
            "work": {
                "titles": {"full": ["Danish Audiobook"]},
                "creators": [{"display": "Author A"}],
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
    html = f"<script>window.bData = {json.dumps(bdata)};</script>".encode("utf-8")
    src = OverdriveSource(_opts(tmp_path))
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=html),
            MagicMock(status_code=200, content=b"od-cover"),
        ]
    )
    book = src.download("https://sample.listen.overdrive.com/")
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
    ).encode("utf-8")

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
    assert book.files[0].url == short_url
    assert book.chapters[0].title == "Ch 1"


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
            _json_resp({"link": "https://stream.saxo.com/ch01.mp3"}),
            MagicMock(status_code=200, content=b"saxo-cover"),
        ]
    )
    src.login("https://www.saxo.com/dk/book_9781234567890", username="u", password="p")
    book = src.download("https://www.saxo.com/dk/book_9781234567890")
    assert book.metadata.title == "Saxo Book"
    assert book.metadata.series == "S Series"
    assert book.files[0].url == "https://stream.saxo.com/ch01.mp3"
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
            page_resp,
        ]
    )
    book = src.download("https://www.audiobooks.com/audiobook/title/12345")
    assert isinstance(book, Audiobook)
    assert book.metadata.title == "AB Book"
    assert book.files[0].url == "https://cdn.audiobooks.com/book.mp3"

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
            "cover_url": "https://covers.everand.com/martian.jpg",
        },
        "jwt_token": {"token": None},
        "audiobook": {
            "external_id": "ext1",
            "account_id": "acc1",
            "session_key": "sess1",
        },
    }
    html = f"<script>ReactDOM.render(Scribd.Audiobooks.Show, {json.dumps(show_payload)});</script>".encode(
        "utf-8"
    )
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
    src._session.get = MagicMock(
        side_effect=[
            _json_resp({"product_lists": [{"type": "want_to_read", "id": "wl-1"}]}),
            _json_resp({"products": wantlist}),
            _json_resp(audio_data),
            MagicMock(status_code=200, content=playlist),
            MagicMock(status_code=200, content=b"nextory-cover"),
        ]
    )
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
    with (
        patch("audiobookdl.utils.http.redirect_of", return_value="https://open.podimo.com/audiobook/abc-123"),
        patch("audiobookdl.sources.podimo.requests.get", return_value=MagicMock(content=b"pod-cover")),
    ):
        book = src.download("https://share.podimo.com/s/short")

    assert isinstance(book, Audiobook)
    assert book.metadata.title == "Podimo Book"
    assert book.metadata.authors == ["P Author"]
    assert book.metadata.narrators == ["P Narrator"]
    assert book.files[0].url == "https://cdn.podimo.com/audio.mp3"
    assert book.cover is not None and book.cover.extension == "png"

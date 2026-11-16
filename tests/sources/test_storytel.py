from datetime import date
import json
from pathlib import Path
from unittest.mock import MagicMock

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import pytest
import requests

from audiobookdl import Audiobook, AudiobookMetadata, BookId, Series
from audiobookdl.args import build_default_options
from audiobookdl.exceptions import (
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotFound,
    BookNotReleased,
    CloudflareBlocked,
    DataNotPresent,
    GenericAudiobookDLException,
    UserNotAuthorized,
)
from audiobookdl.sources import find_compatible_source
from audiobookdl.sources.storytel import StorytelSource, svg_headphone_path


def _make_source(tmp_path, **option_overrides) -> StorytelSource:
    opts = build_default_options(database_directory=str(tmp_path), **option_overrides)
    return StorytelSource(opts)


def _mock_resp(status_code: int = 200, *, json_data=None, content: bytes = b"", text: str = "", headers=None, url: str = "https://api.storytel.net"):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None and not content:
        content = json.dumps(json_data).encode("utf-8")
    resp.content = content
    resp.text = text
    resp.headers = headers or {}
    resp.url = url
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _sample_book_details(consumable_id: str = "1404854", **overrides) -> dict:
    payload = {
        "consumableId": consumable_id,
        "title": "Sample Book",
        "shareUrl": f"https://www.storytel.com/se/sv/books/sample-{consumable_id}?utm_source=share",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "isbn": "9780000000001",
        "description": "Book summary",
        "language": "sv",
        "category": {"name": "Fiction"},
        "seriesInfo": {"name": "Great Series", "orderInSeries": 3},
        "cover": {"url": "https://covers.storytel.com/cover.jpg"},
        "formats": [
            {
                "type": "abook",
                "isReleased": True,
                "publisher": {"name": "Audiobook Publisher"},
                "releaseDate": "2022-05-15T00:00:00Z",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_url():
    book_id = StorytelSource.get_id_from_url("https://www.storytel.com/se/sv/books/shantaram-1404854")
    assert book_id == "1404854"


def test_parse_url_with_params():
    book_id = StorytelSource.get_id_from_url("https://www.storytel.com/se/sv/books/shantaram-1404854?appRedirect=true")
    assert book_id == "1404854"


def test_get_id_from_url_raises_when_path_missing():
    with pytest.raises(DataNotPresent):
        StorytelSource.get_id_from_url("https://www.storytel.com")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.storytel.com/se/sv/books/shantaram-1404854",
        "https://www.mofibo.com/dk/da/series/some-series-99",
        "https://storytel.com/se/authors/author-1",
        "https://www.storytel.com/se/sv/want-to-read",
        "https://www.storytel.com/se/bookshelf/",
        "https://www.mofibo.com/dk/da/bookshelf",
    ],
)
def test_find_compatible_source_matches_storytel_and_mofibo_urls(url):
    assert find_compatible_source(url) is StorytelSource


def test_get_metadata_uses_abook_format_not_first_format(tmp_path):
    src = _make_source(tmp_path)
    book_details = {
        "title": "Sample Book",
        "shareUrl": "https://www.storytel.com/se/sv/books/sample-1",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "formats": [
            {
                "type": "ebook",
                "isReleased": False,
                "publisher": {"name": "Ebook Publisher"},
                "releaseDate": "2020-01-01T00:00:00Z",
            },
            {
                "type": "abook",
                "isReleased": True,
                "publisher": {"name": "Audiobook Publisher"},
                "releaseDate": "2022-05-15T00:00:00Z",
            },
        ],
    }

    metadata = src.get_metadata(book_details)
    assert metadata.publisher == "Audiobook Publisher"
    assert metadata.release_date == date(2022, 5, 15)


def test_get_metadata_checks_is_released_on_abook_format(tmp_path):
    src = _make_source(tmp_path)
    book_details = {
        "title": "Unreleased Audiobook",
        "shareUrl": "https://www.storytel.com/se/sv/books/unreleased-2",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "formats": [
            {
                "type": "ebook",
                "isReleased": True,
                "publisher": {"name": "Ebook Publisher"},
                "releaseDate": "2020-01-01T00:00:00Z",
            },
            {
                "type": "abook",
                "isReleased": False,
                "publisher": {"name": "Audiobook Publisher"},
                "releaseDate": "2030-01-01T00:00:00Z",
            },
        ],
    }

    with pytest.raises(BookNotReleased):
        src.get_metadata(book_details)


def test_get_metadata_full_fields_and_format_error_conditions(tmp_path):
    src = _make_source(tmp_path)
    details = _sample_book_details()
    meta = src.get_metadata(details)
    assert meta.title == "Sample Book"
    assert meta.scrape_url == "https://www.storytel.com/se/sv/books/sample-1404854"
    assert meta.authors == ["Author One"]
    assert meta.narrators == ["Narrator One"]
    assert meta.isbn == "9780000000001"
    assert meta.description == "Book summary"
    assert meta.language is not None and meta.language.alpha_3 == "swe"
    assert meta.genres == ["Audiobook", "Fiction"]
    assert meta.series == "Great Series"
    assert meta.series_order == 3
    assert meta.publisher == "Audiobook Publisher"
    assert meta.release_date == date(2022, 5, 15)

    # Missing formats -> DataNotPresent
    no_formats = _sample_book_details()
    del no_formats["formats"]
    with pytest.raises(DataNotPresent):
        src.get_metadata(no_formats)

    # Ebook only in default "auto" mode -> falls back to ebook metadata
    ebook_only = _sample_book_details(
        formats=[
            {
                "type": "ebook",
                "isReleased": True,
                "isbn": "9780000000002",
                "publisher": {"name": "Ebook Publisher"},
                "releaseDate": "2023-01-10T00:00:00Z",
            }
        ]
    )
    ebook_meta = src.get_metadata(ebook_only)
    assert ebook_meta.genres == ["Ebook", "Fiction"]
    assert ebook_meta.narrators == []
    assert ebook_meta.isbn == "9780000000002"
    assert ebook_meta.publisher == "Ebook Publisher"
    assert ebook_meta.release_date == date(2023, 1, 10)

    # Ebook only when media_type="audiobook" -> BookHasNoAudiobook
    src_audio = _make_source(tmp_path, media_type="audiobook")
    with pytest.raises(BookHasNoAudiobook):
        src_audio.get_metadata(ebook_only)

    # Audiobook only when media_type="ebook" -> BookHasNoEbook
    src_ebook = _make_source(tmp_path, media_type="ebook")
    with pytest.raises(BookHasNoEbook):
        src_ebook.get_metadata(_sample_book_details())

    # Multiple abook formats -> GenericAudiobookDLException
    multi_abook = _sample_book_details(
        formats=[{"type": "abook", "isReleased": True}, {"type": "abook", "isReleased": True}]
    )
    with pytest.raises(GenericAudiobookDLException):
        src.get_metadata(multi_abook)


def test_encrypt_password_and_login_flow_with_fresh_device_ids(tmp_path):
    encrypted_hex = StorytelSource.encrypt_password("s3cr3t-pass!")
    cipher = AES.new(b"VQZBJ6TD8M9WBUWT", AES.MODE_CBC, b"joiwef08u23j341a")
    decrypted = unpad(cipher.decrypt(bytes.fromhex(encrypted_hex)), AES.block_size)
    assert decrypted == b"s3cr3t-pass!"

    src = _make_source(tmp_path)
    seen_urls = []
    seen_data = []

    def fake_post(url, data=None, headers=None, **_kwargs):
        seen_urls.append(url)
        seen_data.append(data)
        return _mock_resp(
            200,
            json_data={"accountInfo": {"jwt": f"jwt-{len(seen_urls)}", "lang": "sv"}},
        )

    src._session.post = MagicMock(side_effect=fake_post)
    src.login("https://www.storytel.com/se/sv/books/sample-1", username="alice@example.com", password="s3cr3t-pass!")

    assert src.authenticated is True
    assert src._session.headers["authorization"] == "Bearer jwt-1"
    assert seen_data[0] == {"uid": "alice@example.com", "pwd": encrypted_hex}
    device_id_1 = seen_urls[0].split("deviceId=")[1].split("&")[0]

    # Periodic relogin triggers only on positive multiples of 10 downloads
    src._download_counter = 9
    src._relogin_check()
    assert len(seen_urls) == 1

    src._download_counter = 10
    src._relogin_check()
    assert len(seen_urls) == 2
    device_id_2 = seen_urls[1].split("deviceId=")[1].split("&")[0]
    assert device_id_1 != device_id_2
    assert src._session.headers["authorization"] == "Bearer jwt-2"


def test_login_raises_cloudflare_blocked_or_user_not_authorized(tmp_path):
    src = _make_source(tmp_path)

    src._session.post = MagicMock(
        return_value=_mock_resp(
            403,
            text="<html><title>Attention Required! | Cloudflare</title></html>",
        )
    )
    with pytest.raises(CloudflareBlocked):
        src.login("https://www.storytel.com/se/sv/books/sample-1", username="u", password="p")
    assert src.authenticated is False

    src._session.post = MagicMock(return_value=_mock_resp(403, text="Forbidden"))
    with pytest.raises(UserNotAuthorized):
        src.login("https://www.storytel.com/se/sv/books/sample-1", username="u", password="p")

    src._session.post = MagicMock(return_value=_mock_resp(401, text="Unauthorized"))
    with pytest.raises(UserNotAuthorized):
        src.login("https://www.storytel.com/se/sv/books/sample-1", username="u", password="p")


def test_download_from_id_refreshes_login_at_threshold_and_skip_downloaded_false(tmp_path):
    src = _make_source(tmp_path, skip_downloaded=False)
    book_marker = Path(src._get_book_path("500"))
    book_marker.parent.mkdir(parents=True, exist_ok=True)
    book_marker.write_text("{}", encoding="utf-8")

    details = _sample_book_details(consumableId="500", title="ID Book")
    redirect_url = "https://cdn.storytel.net/audio/500.mp3?token=xyz"

    def fake_get(url, allow_redirects=True, **_kwargs):
        if "book-details/consumables/500" in url:
            return _mock_resp(200, json_data=details)
        if "assets/v2/consumables/500/abook" in url:
            assert allow_redirects is False
            return _mock_resp(302, headers={"Location": redirect_url})
        if "covers.storytel.com/cover.jpg" in url:
            return _mock_resp(200, content=b"cover")
        if "playback-metadata/consumable/500" in url:
            return _mock_resp(200, json_data={"formats": [{"type": "abook"}]})
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)
    src._session.post = MagicMock(
        return_value=_mock_resp(200, json_data={"accountInfo": {"jwt": "refreshed-jwt", "lang": "sv"}})
    )
    src._username = "alice@example.com"
    src._password = StorytelSource.encrypt_password("pw")
    src._download_counter = 10

    book = src.download_from_id("500")
    assert isinstance(book, Audiobook)
    assert book.title == "ID Book"
    assert src._session.headers["authorization"] == "Bearer refreshed-jwt"
    src._session.post.assert_called_once()

    # With skip_downloaded=False, existing book-id JSON on disk does NOT filter out books
    bookshelf_payload = {
        "items": {
            "a": {"model": {"id": "500", "state": "WILL_CONSUME", "formats": [{"type": "abook", "isReleased": True}]}}
        }
    }
    src._session.post = MagicMock(return_value=_mock_resp(200, json_data=bookshelf_payload))
    series = src.download_bookshelf_wtr()
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["500"]


def test_download_book_end_to_end_chapters_isbn_and_corrections(tmp_path):
    src = _make_source(tmp_path)
    details = _sample_book_details(
        consumableId="1168061",
        title="Leo Da Vinci",
        isbn=None,
    )
    playback_payload = {
        "formats": [
            {"type": "ebook"},
            {
                "type": "abook",
                "chapters": [
                    {"number": 1, "title": "Leo Da Vinci - Kapitel 1", "durationInMilliseconds": 12000},
                    {"number": 2, "title": "Leo Da Vinci", "durationInMilliseconds": 5000},
                    {"number": 3, "title": None, "durationInMilliseconds": 3000},
                ],
            },
        ]
    }
    redirect_url = "https://cdn.storytel.net/audio/1168061.mp3?token=abc&isbn=9789111111119"

    def fake_get(url, allow_redirects=True, **_kwargs):
        if "book-details/consumables/1168061" in url:
            return _mock_resp(200, json_data=details)
        if "assets/v2/consumables/1168061/abook" in url:
            assert allow_redirects is False
            return _mock_resp(302, headers={"Location": redirect_url})
        if "covers.storytel.com/cover.jpg" in url:
            return _mock_resp(200, content=b"jpg-cover-bytes")
        if "playback-metadata/consumable/1168061" in url:
            return _mock_resp(200, json_data=playback_payload)
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)

    result = src.download("https://www.storytel.com/de/de/books/leo-da-vinci-1168061?appRedirect=true")
    assert isinstance(result, Audiobook)
    # ISBN updated from audio redirect query string; hardcoded correction applied for 1168061
    assert result.metadata.isbn == "9789111111119"
    assert result.metadata.title == "Leo Da Vinci: Folge 1"
    assert result.metadata.series == "Leo Da Vinci"
    assert result.metadata.series_order == 1
    assert len(result.files) == 1
    assert result.files[0].url == redirect_url
    assert result.files[0].ext == "mp3"
    assert result.files[0].expected_status_code == 200
    assert result.files[0].expected_content_type == "audio/mpeg"
    assert result.cover is not None and result.cover.image == b"jpg-cover-bytes"
    assert [(c.start, c.title) for c in result.chapters] == [
        (0, "Kapitel 1"),
        (12000, "Leo Da Vinci"),
        (17000, "Chapter 3"),
    ]
    assert src._download_counter == 1
    # Without debug mode, raw playback-metadata JSON dump is NOT persisted
    saved_playback = Path(src._get_playback_metadata_path("1168061"))
    assert not saved_playback.exists()

    # With debug=True, playback-metadata JSON dump IS written cleanly
    debug_src = _make_source(tmp_path / "dbg", debug=True)
    debug_src._session.get = MagicMock(side_effect=fake_get)
    debug_src.download("https://www.storytel.com/de/de/books/leo-da-vinci-1168061?appRedirect=true")
    saved_debug_playback = Path(debug_src._get_playback_metadata_path("1168061"))
    assert saved_debug_playback.exists()
    assert json.loads(saved_debug_playback.read_text(encoding="utf-8")) == playback_payload


def test_download_book_error_paths_and_missing_chapters(tmp_path):
    src = _make_source(tmp_path)

    src._session.get = MagicMock(return_value=_mock_resp(404))
    with pytest.raises(BookNotFound):
        src.download_book_details("999")

    src._session.get = MagicMock(return_value=_mock_resp(200, text="rate limited", url="https://api.storytel.net/assets"))
    with pytest.raises(GenericAudiobookDLException):
        src.get_audio_url("999")

    # Missing formats / missing abook in playback-metadata -> DataNotPresent
    src._session.get = MagicMock(return_value=_mock_resp(200, json_data={}))
    with pytest.raises(DataNotPresent):
        src.get_chapters({"consumableId": "10", "title": "T"})

    src._session.get = MagicMock(return_value=_mock_resp(200, json_data={"formats": [{"type": "ebook"}]}))
    with pytest.raises(DataNotPresent):
        src.get_chapters({"consumableId": "10", "title": "T"})

    # Present abook format without chapters list -> []
    src._session.get = MagicMock(return_value=_mock_resp(200, json_data={"formats": [{"type": "abook"}]}))
    assert src.get_chapters({"consumableId": "10", "title": "T"}) == []

    # Unrecognized URL passed directly to download -> BookNotFound
    with pytest.raises(BookNotFound):
        src.download("https://example.com/not-storytel")


def test_download_routes_bookshelf_want_to_read_and_avoids_slug_collision(tmp_path):
    src = _make_source(tmp_path, skip_downloaded=True, debug=True)
    # Mark book "104" as already downloaded
    Path(src._get_book_path("104")).write_text("{}", encoding="utf-8")

    bookshelf_payload = {
        "items": {
            "a": {"model": {"id": "101", "state": "WILL_CONSUME", "formats": [{"type": "abook", "isReleased": True}]}},
            "b": {"model": {"id": "102", "state": "CONSUMED", "formats": [{"type": "abook", "isReleased": True}]}},
            "c": {"model": {"id": "103", "state": "WILL_CONSUME", "formats": [{"type": "ebook", "isReleased": True}]}},
            "d": {"model": {"id": "104", "state": "WILL_CONSUME", "formats": [{"type": "abook", "isReleased": True}]}},
            "e": {"model": {"id": "105", "state": "WILL_CONSUME", "formats": [{"type": "abook", "isReleased": False}]}},
            "f": {"model": {"state": "WILL_CONSUME", "formats": [{"type": "abook", "isReleased": True}]}},
        }
    }
    src._session.post = MagicMock(return_value=_mock_resp(200, json_data=bookshelf_payload))

    # Default "auto" mode includes both audiobook (101) and ebook-only (103)
    series = src.download("https://www.storytel.com/se/sv/want-to-read?sort=recent#top")
    assert isinstance(series, Series)
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["101", "103"]
    bookshelf_json = Path(src.database_directory_lists) / "bookshelf.json"
    assert json.loads(bookshelf_json.read_text(encoding="utf-8")) == bookshelf_payload

    # "audiobook" mode skips ebook-only (103); with debug=False no bookshelf.json dump is written
    bookshelf_json.unlink()
    src_audio = _make_source(tmp_path, skip_downloaded=True, media_type="audiobook")
    src_audio._session.post = MagicMock(return_value=_mock_resp(200, json_data=bookshelf_payload))
    series_audio = src_audio.download("https://www.storytel.com/se/sv/want-to-read")
    assert isinstance(series_audio, Series)
    assert [b.id for b in series_audio.books if isinstance(b, BookId)] == ["101"]
    assert not bookshelf_json.exists()

    # "ebook" mode selects only ebook (103)
    src_ebook = _make_source(tmp_path, skip_downloaded=True, media_type="ebook")
    src_ebook._session.post = MagicMock(return_value=_mock_resp(200, json_data=bookshelf_payload))
    series_ebook = src_ebook.download("https://www.storytel.com/se/sv/want-to-read")
    assert isinstance(series_ebook, Series)
    assert [b.id for b in series_ebook.books if isinstance(b, BookId)] == ["103"]

    # Non-200 bookshelf response -> UserNotAuthorized
    src._session.post = MagicMock(return_value=_mock_resp(401))
    with pytest.raises(UserNotAuthorized):
        src.download("https://www.storytel.com/se/bookshelf")

    # A catalog book whose slug contains "want-to-read" must NOT route to bookshelf
    src.download_book_from_url = MagicMock(return_value="book-result")
    src.download_bookshelf_wtr = MagicMock(side_effect=AssertionError("misrouted to bookshelf"))
    assert src.download("https://www.storytel.com/se/sv/books/my-want-to-read-book-777") == "book-result"
    src.download_book_from_url.assert_called_once()


def test_download_lists_api_paginates_filters_and_persists_json(tmp_path):
    src = _make_source(tmp_path, skip_downloaded=True, debug=True)
    Path(src._get_book_path("202")).write_text("{}", encoding="utf-8")

    page_1 = {
        "id": "ser-42",
        "title": "My Series — ÅÄÖ",
        "nextPageToken": "page-2-token",
        "items": [
            {"id": "201", "formats": [{"type": "abook", "isReleased": True}]},
            {"id": "202", "formats": [{"type": "abook", "isReleased": True}]},  # skipped on disk
            {"id": "203", "formats": [{"type": "ebook", "isReleased": True}]},  # ebook only
        ],
    }
    page_2 = {
        "id": "ser-42",
        "title": "My Series — ÅÄÖ",
        "nextPageToken": None,
        "items": [
            {"id": "204", "formats": [{"type": "abook", "isReleased": False}]},  # unreleased
            {"id": "205", "formats": [{"type": "abook", "isReleased": True}]},
        ],
    }
    captured_params = []

    def fake_get(url, params=None, **_kwargs):
        assert url == "https://api.storytel.net/explore/lists/series/42"
        captured_params.append(dict(params or {}))
        return _mock_resp(200, json_data=page_1 if len(captured_params) == 1 else page_2)

    src._session.get = MagicMock(side_effect=fake_get)
    series = src.download("https://www.storytel.com/se/sv/series/cool-series-42")

    assert isinstance(series, Series)
    assert series.title == "My Series — ÅÄÖ"
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["201", "203", "205"]
    assert captured_params[0]["includeFormats"] == "abook,ebook"
    assert "nextPageToken" not in captured_params[0]
    assert captured_params[1]["nextPageToken"] == "page-2-token"
    saved_list = Path(src._get_lists_path("ser-42", "se", "abook,ebook"))
    assert len(json.loads(saved_list.read_text(encoding="utf-8"))["items"]) == 5

    # Explicit "audiobook" mode filters out ebook-only 203 and queries only "abook"
    captured_params_audio = []

    def fake_get_audio(url, params=None, **_kwargs):
        captured_params_audio.append(dict(params or {}))
        return _mock_resp(200, json_data=page_1 if len(captured_params_audio) == 1 else page_2)

    src_audio = _make_source(tmp_path, skip_downloaded=True, media_type="audiobook")
    src_audio._session.get = MagicMock(side_effect=fake_get_audio)
    series_audio = src_audio.download("https://www.storytel.com/se/sv/series/cool-series-42")
    assert isinstance(series_audio, Series)
    assert [b.id for b in series_audio.books if isinstance(b, BookId)] == ["201", "205"]
    assert captured_params_audio[0]["includeFormats"] == "abook"
    # debug=False does not dump lists/*.json
    assert not Path(src_audio._get_lists_path("ser-42", "se", "abook")).exists()


def test_download_books_from_website_filters_by_headphone_icon_and_skip(tmp_path):
    src = _make_source(tmp_path, skip_downloaded=True)
    Path(src._get_book_path("302")).write_text("{}", encoding="utf-8")

    html = (
        "<html><body>"
        "<h1>Site Header</h1>"
        "<h1>Sci-Fi Category</h1>"
        f'<a href="/se/sv/books/audio-one-301"><svg><path d="{svg_headphone_path}"/></svg></a>'
        f'<a href="/se/sv/books/audio-one-301">Duplicate Link</a>'
        f'<a href="/se/sv/books/audio-skip-302"><svg><path d="{svg_headphone_path}"/></svg></a>'
        '<a href="/se/sv/books/ebook-only-303"><svg><path d="M0 0h10"/></svg></a>'
        "</body></html>"
    ).encode()

    src._session.get = MagicMock(return_value=_mock_resp(200, content=html))
    series = src.download("https://www.storytel.com/se/sv/categories/sci-fi-10")

    assert isinstance(series, Series)
    assert series.title == "Sci-Fi Category"
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["301", "303"]

    # Explicit "audiobook" mode filters by headphone SVG icon
    src_audio = _make_source(tmp_path, skip_downloaded=True, media_type="audiobook")
    src_audio._session.get = MagicMock(return_value=_mock_resp(200, content=html))
    series_audio = src_audio.download("https://www.storytel.com/se/sv/categories/sci-fi-10")
    assert isinstance(series_audio, Series)
    assert [b.id for b in series_audio.books if isinstance(b, BookId)] == ["301"]


def test_on_download_complete_writes_utf8_json(tmp_path):
    unicode_title = "日本語・Книга — Storytel"
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title=unicode_title),
        files=[],
        source_data={"consumableId": "12345", "title": unicode_title},
    )

    no_dbg_src = _make_source(tmp_path / "no_dbg")
    no_dbg_src.on_download_complete(book)
    assert not Path(no_dbg_src._get_book_path("12345")).exists()

    src = _make_source(tmp_path / "dbg", debug=True)
    src.on_download_complete(book)

    saved = Path(src._get_book_path("12345")).read_text(encoding="utf-8")
    assert unicode_title in saved
    assert json.loads(saved)["title"] == unicode_title


def test_storytel_skips_playback_metadata_and_streams_on_print_output(tmp_path):
    src = _make_source(tmp_path, print_output=True)
    src._session.get = MagicMock(
        return_value=_mock_resp(
            200,
            json_data={
                "consumableId": "999",
                "title": "Print Only Book",
                "shareUrl": "https://www.storytel.com/se/sv/books/print-only-999",
                "formats": [{"type": "abook", "isReleased": True}],
                "authors": [{"name": "Author"}],
                "narrators": [],
                "cover": {"url": "https://example.com/cover.jpg"},
            },
        )
    )
    audiobook = src.download_book_from_book_id("999")
    assert isinstance(audiobook, Audiobook)
    assert audiobook.files == []
    assert audiobook.cover is None
    assert audiobook.chapters == []
    assert src._session.get.call_count == 1
    assert not Path(src._get_playback_metadata_path("999")).exists()


def test_download_ebook_only_book_in_auto_mode(tmp_path):
    src = _make_source(tmp_path)
    src._session.headers["authorization"] = "Bearer token-123"
    details = _sample_book_details(
        "555",
        formats=[
            {
                "type": "ebook",
                "isReleased": True,
                "isbn": "9781234567897",
                "publisher": {"name": "Ebook Press"},
                "releaseDate": "2024-03-01",
            }
        ],
    )

    def _fake_get(url, **kwargs):
        if "book-details/consumables/555" in url:
            return _mock_resp(200, json_data=details)
        if url.endswith("/assets/v2/consumables/555/ebook"):
            assert kwargs.get("allow_redirects") is False
            return _mock_resp(
                302,
                headers={"Location": "https://cdn.storytel.net/ebooks/sample.epub?sig=abc"},
            )
        if url == "https://covers.storytel.com/cover.jpg":
            return _mock_resp(200, content=b"cover-bytes")
        raise AssertionError(f"Unexpected URL requested: {url}")

    src._session.get = MagicMock(side_effect=_fake_get)
    book = src.download_book_from_book_id("555")

    assert isinstance(book, Audiobook)
    assert book.chapters == []
    assert len(book.files) == 1
    assert book.files[0].ext == "epub"
    assert book.files[0].expected_content_type is None
    assert book.files[0].url == "https://cdn.storytel.net/ebooks/sample.epub?sig=abc"
    assert book.files[0].headers["authorization"] == "Bearer token-123"
    assert book.metadata.genres == ["Ebook", "Fiction"]
    assert book.metadata.narrators == []
    assert book.metadata.isbn == "9781234567897"
    assert book.metadata.publisher == "Ebook Press"
    assert book.metadata.release_date == date(2024, 3, 1)
    assert not Path(src._get_playback_metadata_path("555")).exists()


def test_ebook_url_pdf_extension_and_error_handling(tmp_path):
    src = _make_source(tmp_path)
    src._session.get = MagicMock(
        return_value=_mock_resp(
            302,
            headers={"Location": "https://cdn.storytel.net/ebooks/doc.PDF?token=1"},
        )
    )
    files = src.get_files({"consumableId": "77"}, format_type="ebook")
    assert files[0].ext == "pdf"

    src._session.get = MagicMock(return_value=_mock_resp(404, text="missing"))
    with pytest.raises(GenericAudiobookDLException):
        src.get_ebook_url("77")


def test_dual_format_ebook_and_both_modes_with_skip_and_cache(tmp_path):
    details = _sample_book_details(
        "888",
        formats=[
            {
                "type": "abook",
                "isReleased": True,
                "publisher": {"name": "Audio Press"},
                "releaseDate": "2023-01-01",
            },
            {
                "type": "ebook",
                "isReleased": True,
                "isbn": "9789999999999",
                "publisher": {"name": "Ebook Press"},
                "releaseDate": "2023-02-02",
            },
        ],
    )

    def _make_get(details_payload):
        def _fake_get(url, **kwargs):
            if "book-details/consumables/888" in url:
                return _mock_resp(200, json_data=details_payload)
            if url.endswith("/assets/v2/consumables/888/abook"):
                return _mock_resp(302, headers={"Location": "https://cdn.storytel.net/audio.mp3"})
            if url.endswith("/assets/v2/consumables/888/ebook"):
                return _mock_resp(302, headers={"Location": "https://cdn.storytel.net/book.epub"})
            if "playback-metadata/consumable/888" in url:
                return _mock_resp(200, json_data={"formats": [{"type": "abook", "chapters": []}]})
            if url == "https://covers.storytel.com/cover.jpg":
                return _mock_resp(200, content=b"cover")
            raise AssertionError(f"Unexpected URL: {url}")

        return _fake_get

    # Explicit ebook mode selects only ebook
    ebook_src = _make_source(tmp_path, media_type="ebook")
    ebook_src._session.get = MagicMock(side_effect=_make_get(details))
    only_ebook = ebook_src.download_book_from_book_id("888")
    assert isinstance(only_ebook, Audiobook)
    assert only_ebook.files[0].ext == "epub"

    # Both mode returns Series with audiobook + ebook, and writes separate cache files
    both_src = _make_source(tmp_path, media_type="both", skip_downloaded=True)
    both_src._session.get = MagicMock(side_effect=_make_get(details))
    result = both_src.download_book_from_book_id("888")
    assert isinstance(result, Series)
    assert len(result.books) == 2
    b0, b1 = result.books
    assert isinstance(b0, Audiobook) and isinstance(b1, Audiobook)
    assert [b0.files[0].ext, b1.files[0].ext] == ["mp3", "epub"]

    both_src.on_download_complete(b0)
    both_src.on_download_complete(b1)
    assert Path(both_src._get_book_path("888", "abook")).exists()
    assert Path(both_src._get_book_path("888", "ebook")).exists()

    # If ebook already downloaded and skip_downloaded=True, only remaining abook is fetched
    Path(both_src._get_book_path("888", "abook")).unlink()
    both_src._session.get = MagicMock(side_effect=_make_get(details))
    remaining = both_src.download_book_from_book_id("888")
    assert isinstance(remaining, Audiobook)
    assert remaining.files[0].ext == "mp3"


def test_bookshelf_both_mode_skips_only_when_both_formats_exist(tmp_path):
    src = _make_source(tmp_path, media_type="both", skip_downloaded=True)
    shelf_payload = {
        "items": {
            "10": {
                "model": {
                    "id": "10",
                    "name": "Dual Book",
                    "state": "WILL_CONSUME",
                    "formats": [
                        {"type": "abook", "isReleased": True},
                        {"type": "ebook", "isReleased": True},
                    ],
                },
            },
            "20": {
                "model": {
                    "id": "20",
                    "name": "Already Both Done",
                    "state": "WILL_CONSUME",
                    "formats": [
                        {"type": "abook", "isReleased": True},
                        {"type": "ebook", "isReleased": True},
                    ],
                },
            },
        }
    }
    Path(src._get_book_path("10", "abook")).write_text("{}", encoding="utf-8")
    Path(src._get_book_path("20", "abook")).write_text("{}", encoding="utf-8")
    Path(src._get_book_path("20", "ebook")).write_text("{}", encoding="utf-8")

    src._session.post = MagicMock(return_value=_mock_resp(200, json_data=shelf_payload))
    series = src.download_bookshelf_wtr()
    assert len(series.books) == 1
    assert [b.id for b in series.books if isinstance(b, BookId)] == ["10"]


def test_storytel_download_counter_is_instance_attribute(tmp_path):
    assert not hasattr(StorytelSource, "_download_counter")
    src1 = _make_source(tmp_path)
    src2 = _make_source(tmp_path)
    assert src1._download_counter == 0
    assert src2._download_counter == 0
    src1._download_counter += 1
    assert src1._download_counter == 1
    assert src2._download_counter == 0


def test_storytel_library_and_search_urls_route_back_through_source(tmp_path):
    from audiobookdl.sources import find_compatible_source

    src = _make_source(tmp_path)
    src._language = "sv"
    src._session.post = MagicMock(
        return_value=_mock_resp(
            200,
            json_data={
                "items": {
                    "1": {"model": {"id": "777", "name": "Lib Book", "authors": [{"name": "A"}]}}
                }
            },
        )
    )
    lib_items = src.list_library()
    assert len(lib_items) == 1
    assert lib_items[0].url == "https://www.storytel.com/sv/books/777"
    assert find_compatible_source(lib_items[0].url) is StorytelSource
    assert StorytelSource.get_id_from_url(lib_items[0].url) == "777"

    # Legacy bare /books/{id} URLs without a language segment also route cleanly
    assert find_compatible_source("https://www.storytel.com/books/777") is StorytelSource
    assert StorytelSource.get_id_from_url("https://www.storytel.com/books/777") == "777"

    details = _sample_book_details(consumableId="777", title="Lib Book")
    src._session.get = MagicMock(
        side_effect=[
            _mock_resp(200, json_data=details),
            _mock_resp(200, content=b"cover"),
            _mock_resp(302, headers={"Location": "https://cdn.storytel.net/777.mp3"}),
            _mock_resp(200, json_data={"formats": [{"type": "abook", "chapters": []}]}),
        ]
    )
    downloaded = src.download(lib_items[0].url)
    assert isinstance(downloaded, Audiobook)
    assert downloaded.title == "Lib Book"





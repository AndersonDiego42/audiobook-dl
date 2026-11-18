import json
from unittest.mock import MagicMock

import pytest

from audiobookdl import Audiobook, BookId, Series
from audiobookdl.options_schema import build_default_options
from audiobookdl.exceptions import (
    AudiobookDLException,
    BookHasNoAudiobook,
    BookNotFound,
    DataNotPresent,
    GenericAudiobookDLException,
    UserNotAuthorized,
)
from audiobookdl.sources import find_compatible_source
from audiobookdl.sources.nextory import NextorySource


def _make_source(tmp_path) -> NextorySource:
    opts = build_default_options(database_directory=str(tmp_path), skip_downloaded=False)
    return NextorySource(opts)


def _json_resp(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = json.dumps(payload).encode("utf-8")
    resp.json.return_value = payload
    return resp


def _book_entry(book_id: int, *, with_hls: bool = True, **overrides) -> dict:
    formats = []
    if with_hls:
        formats.append(
            {
                "type": "hls",
                "identifier": f"fmt-{book_id}",
                "img_url": f"https://img.nextory.com/{book_id}.jpg",
                "publisher": {"name": "Nordic Pub"},
                "isbn": "9789100000001",
                "publication_date": "2024-05-01",
            }
        )
    else:
        formats.append({"type": "epub", "identifier": f"epub-{book_id}"})
    entry = {
        "id": book_id,
        "title": f"Book {book_id}",
        "authors": [{"name": "Author One"}],
        "narrators": [{"name": "Narrator One"}],
        "description_full": "Description",
        "language": "sv",
        "formats": formats,
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "url",
    [
        "https://nextory.com/se/want-to-read",
        "https://www.nextory.no/bok/sample-book-9999/",
        "https://catalog-se.nextory.com/book/sample-123",
    ],
)
def test_find_compatible_source_matches_nextory_urls(url):
    assert find_compatible_source(url) is NextorySource


def test_login_completes_session_and_profile_handshake(tmp_path):
    src = _make_source(tmp_path)
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"login_token": "tok-123", "country": "SE"}),
            _json_resp({"profile_token": "prof-456"}),
        ]
    )
    src._session.get = MagicMock(
        return_value=_json_resp({"profiles": [{"login_key": "key-abc"}]})
    )

    src.login(
        "https://www.nextory.se/bok/test-1",
        username="reader@example.com",
        password="secret",
    )

    import uuid

    assert src.authenticated is True
    assert src._session.headers["X-Application-Id"] == NextorySource.APP_ID
    assert src._session.headers["X-Os-Info"] == "ios"
    device_uuid = uuid.UUID(src._session.headers["X-Device-Id"])
    assert device_uuid.version == 4
    # Device ID is persisted per database_directory across source instances
    assert (
        _make_source(tmp_path).get_or_create_device_id(
            "nextory_device_id.txt", NextorySource.create_device_id
        )
        == src._session.headers["X-Device-Id"]
    )
    assert src._session.headers["token"] == "tok-123"
    assert src._session.headers["X-Login-Token"] == "tok-123"
    assert src._session.headers["X-Country-Code"] == "SE"
    assert src._session.headers["X-Profile-Token"] == "prof-456"
    assert src._session.post.call_args_list[0].args[0] == "https://api.nextory.com/user/v1/sessions"
    assert src._session.get.call_args.args[0] == "https://api.nextory.com/user/v1/me/profiles"
    assert src._session.post.call_args_list[1].args[0] == "https://api.nextory.com/user/v1/profile/authorize"


def test_login_maps_user_not_found_and_api_errors(tmp_path):
    src = _make_source(tmp_path)

    src._session.post = MagicMock(
        return_value=_json_resp(
            {"error": {"key": "UserNotFound", "description": "No such user"}}
        )
    )
    with pytest.raises(UserNotAuthorized):
        src.login("https://www.nextory.se/bok/test-1", username="bad", password="pw")
    assert src.authenticated is False

    src._session.post = MagicMock(
        return_value=_json_resp(
            {"error": {"key": "AppDeprecateError", "description": "Update app version"}}
        )
    )
    with pytest.raises(GenericAudiobookDLException) as exc_info:
        src.login("https://www.nextory.se/bok/test-1", username="u", password="pw")
    assert exc_info.value.data == {
        "heading": "AppDeprecateError",
        "body": "Update app version",
    }


def test_want_to_read_series_filters_ebooks_and_caches_wantlist(tmp_path):
    src = _make_source(tmp_path)
    wantlist = [
        _book_entry(101, with_hls=True),
        _book_entry(102, with_hls=False),  # ebook-only -> skipped
    ]
    audio_data = {
        "files": [
            {"uri": "https://cdn.nextory.com/101/master.m3u8", "start_at": 0},
            {"uri": "https://cdn.nextory.com/101/part2/master.m3u8", "start_at": 60000},
        ]
    }
    playlist = b"#EXTM3U\n#EXTINF:10.0,\nseg0.ts\n#EXT-X-ENDLIST\n"

    get_urls = []

    def fake_get(url, **_kwargs):
        get_urls.append(url)
        if url.endswith("/me/product_lists"):
            return _json_resp({"product_lists": [{"type": "want_to_read", "id": "wl-1"}]})
        if url.endswith("/want_to_read/products"):
            return _json_resp({"products": wantlist})
        if url.endswith("/packages/audio"):
            return _json_resp(audio_data)
        if url.endswith("master.m3u8"):
            master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nmedia.m3u8\n"
            return MagicMock(status_code=200, content=master)
        if url.endswith("media.m3u8"):
            return MagicMock(status_code=200, content=playlist)
        if url.endswith("101.jpg"):
            return MagicMock(status_code=200, content=b"cover-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)

    series = src.download("https://nextory.com/se/Want-To-Read")
    assert isinstance(series, Series)
    assert series.title == "Nextory want to read"
    assert [b.id for b in series.books if isinstance(b, BookId)] == [101]

    # Resolving BookId reuses cached wantlist without refetching product_lists/products
    book = src.download_from_id(series.books[0].id)  # type: ignore[union-attr]
    assert isinstance(book, Audiobook)
    assert [(c.start, c.title) for c in book.chapters] == [
        (0, "Chapter 1"),
        (60000, "Chapter 2"),
    ]
    assert [f.url for f in book.files] == [
        "https://cdn.nextory.com/101/seg0.ts",
        "https://cdn.nextory.com/101/part2/seg0.ts",
    ]
    assert sum(1 for u in get_urls if u.endswith("/want_to_read/products")) == 1


def test_error_paths_for_missing_wantlist_book_and_audio_files(tmp_path):
    src = _make_source(tmp_path)

    # No want_to_read list -> DataNotPresent
    src._session.get = MagicMock(
        return_value=_json_resp({"product_lists": [{"type": "reading", "id": "other"}]})
    )
    with pytest.raises(DataNotPresent):
        src.download_want_to_read_id()

    # Book missing from wantlist -> AudiobookDLException('nextory_want_to_read')
    src._wantlist_cache = [_book_entry(101)]
    with pytest.raises(AudiobookDLException) as exc_info:
        src.download("https://nextory.com/se/book/missing-999")
    assert exc_info.value.error_description == "nextory_want_to_read"

    # Audio payload missing 'files' -> BookHasNoAudiobook
    src._session.get = MagicMock(return_value=_json_resp({"status": "unavailable"}))
    with pytest.raises(BookHasNoAudiobook):
        src.download_from_id(101)


def test_metadata_and_series_edge_cases(tmp_path):
    src = _make_source(tmp_path)

    # Ebook-only (no hls format) and invalid/non-positive volume or bad date
    no_hls_entry = _book_entry(
        200,
        with_hls=False,
        series={"name": "Edge Series", "vol": 0},
        volume="not-an-int",
    )
    meta_no_hls = src.get_metadata(no_hls_entry)
    assert meta_no_hls.series == "Edge Series"
    assert meta_no_hls.series_order is None
    assert meta_no_hls.publisher is None
    assert meta_no_hls.release_date is None

    bad_date_entry = _book_entry(201, series=None, volume=0)
    bad_date_entry["formats"][0]["publication_date"] = "invalid-date"
    meta_bad_date = src.get_metadata(bad_date_entry)
    assert meta_bad_date.series is None
    assert meta_bad_date.series_order is None
    assert meta_bad_date.release_date is None
    assert meta_bad_date.isbn == "9789100000001"
    assert meta_bad_date.publisher == "Nordic Pub"
    assert meta_bad_date.release_date is None


def test_wantlist_pagination_and_secret_redaction(tmp_path, monkeypatch):
    from audiobookdl import logging as adl_logging

    src = _make_source(tmp_path)
    captured_debug = []
    monkeypatch.setattr(adl_logging, "debug", lambda msg, **_kw: captured_debug.append(msg))

    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"login_token": "super-secret-login-tok", "country": "SE"}),
            _json_resp({"profile_token": "super-secret-profile-tok"}),
        ]
    )
    src._session.get = MagicMock(
        return_value=_json_resp({"profiles": [{"login_key": "k1"}]})
    )
    src.login("https://www.nextory.se/bok/test-1", username="u", password="p")
    assert all("super-secret-profile-tok" not in m for m in captured_debug)

    page_0 = [_book_entry(i) for i in range(100)]
    page_1 = [_book_entry(100)]

    def fake_get(url, params=None, **_kwargs):
        if url.endswith("/me/product_lists"):
            return _json_resp({"product_lists": [{"type": "want_to_read", "id": "wl-1"}]})
        if url.endswith("/want_to_read/products"):
            if params["page"] == "0":
                return _json_resp({"products": page_0})
            if params["page"] == "1":
                return _json_resp({"products": page_1})
        raise AssertionError(f"Unexpected GET {url} {params}")

    src._session.get = MagicMock(side_effect=fake_get)
    items = src.download_want_to_read_list()
    assert len(items) == 101
    assert items[-1]["id"] == 100


def test_nextory_profile_selection_locale_search_and_chapters(tmp_path):
    from audiobookdl import DownloadOptions

    opts = DownloadOptions(database_directory=str(tmp_path), skip_downloaded=False, library="Kids")
    src = NextorySource(opts)
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"login_token": "tok", "country": "DE"}),
            _json_resp({"profile_token": "kids-prof-tok"}),
        ]
    )
    src._session.get = MagicMock(
        return_value=_json_resp(
            {
                "profiles": [
                    {"id": "p1", "name": "Adult", "login_key": "adult-key"},
                    {"id": "p2", "name": "Kids", "login_key": "kids-key"},
                ]
            }
        )
    )
    src.login("https://www.nextory.de/buch/demo-55", username="u", password="p")
    assert src._session.headers["X-Locale"] == "de_DE"
    assert src._session.post.call_args_list[1].kwargs["json"] == {"login_key": "kids-key"}

    chapters = NextorySource.get_chapters(
        {
            "files": [{"uri": "u", "start_at": 0, "title": "Opening Scene"}],
            "chapters": [
                {"title": "Prologue", "start_at": 0},
                {"title": "Chapter One", "start_at": 12000},
            ],
        }
    )
    assert [(c.start, c.title) for c in chapters] == [(0, "Prologue"), (12000, "Chapter One")]

    src._session.get = MagicMock(
        return_value=_json_resp(
            {"products": [{"id": 77, "title": "Search Hit", "authors": [{"name": "A. Writer"}]}]}
        )
    )
    hits = src.search("Search Hit")
    assert len(hits) == 1
    assert hits[0].title == "Search Hit"
    assert hits[0].author == "A. Writer"
    assert hits[0].url == "https://www.nextory.com/book/77"

    with pytest.raises(BookNotFound):
        src.download("https://www.nextory.com/book/no-numeric-id")
    meta = src.get_metadata(_book_entry(77))
    assert meta.source == "nextory"
    assert meta.scrape_url == "https://www.nextory.com/book/77"


def test_nextory_catalog_download_succeeds_without_want_to_read_list(tmp_path):
    src = _make_source(tmp_path)
    product = _book_entry(888, title="Catalog Only Book")
    audio_data = {"files": [{"uri": "https://cdn.nextory.com/888/master.m3u8", "start_at": 0}]}
    playlist = b"#EXTM3U\n#EXTINF:5.0,\nseg0.ts\n#EXT-X-ENDLIST\n"

    def fake_get(url, **_kwargs):
        if url.endswith("/library/v1/products/888"):
            return _json_resp(product)
        if url.endswith("/me/product_lists"):
            raise AssertionError("Should not query want_to_read when product API succeeds")
        if url.endswith("/packages/audio"):
            return _json_resp(audio_data)
        if url.endswith("master.m3u8"):
            return MagicMock(status_code=200, content=playlist)
        if url.endswith("888.jpg"):
            return MagicMock(status_code=200, content=b"cover-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)
    book = src.download("https://www.nextory.com/se/book/catalog-only-888")
    assert isinstance(book, Audiobook)
    assert book.title == "Catalog Only Book"

    # Also verify missing product + missing want_to_read list raises nextory_want_to_read instead of DataNotPresent
    def fake_get_no_wl(url, **_kwargs):
        if url.endswith("/library/v1/products/999"):
            return _json_resp({}, status_code=404)
        if url.endswith("/me/product_lists"):
            return _json_resp({"product_lists": [{"type": "reading", "id": "r-1"}]})
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get_no_wl)
    with pytest.raises(AudiobookDLException) as exc_info:
        src.download_from_id(999)
    assert exc_info.value.error_description == "nextory_want_to_read"


def test_nextory_parallel_playlists_and_hls_key_deduplication(tmp_path):
    src = _make_source(tmp_path)
    key_calls: list[str] = []
    playlist = (
        b"#EXTM3U\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        b"#EXTINF:5.0,\nseg0.ts\n"
        b"#EXTINF:5.0,\nseg1.ts\n"
        b"#EXTINF:5.0,\nseg2.ts\n"
        b"#EXT-X-ENDLIST\n"
    )

    def fake_get(url, **_kwargs):
        if url.endswith(".key") or url.endswith(".bin"):
            key_calls.append(url)
            return MagicMock(status_code=200, content=b"0123456789abcdef")
        if url.endswith(".m3u8"):
            return MagicMock(status_code=200, content=playlist)
        raise AssertionError(f"Unexpected GET {url}")

    src._session.get = MagicMock(side_effect=fake_get)
    files = src.get_files(
        {
            "files": [
                {"uri": "https://cdn.nextory.com/p1/media.m3u8"},
                {"uri": "https://cdn.nextory.com/p2/media.m3u8"},
            ]
        }
    )
    assert [f.url for f in files] == [
        "https://cdn.nextory.com/p1/seg0.ts",
        "https://cdn.nextory.com/p1/seg1.ts",
        "https://cdn.nextory.com/p1/seg2.ts",
        "https://cdn.nextory.com/p2/seg0.ts",
        "https://cdn.nextory.com/p2/seg1.ts",
        "https://cdn.nextory.com/p2/seg2.ts",
    ]
    # Each playlist's 3 segments share 1 distinct key URI -> 2 key fetches total instead of 6
    assert sorted(key_calls) == [
        "https://cdn.nextory.com/p1/key.bin",
        "https://cdn.nextory.com/p2/key.bin",
    ]


from datetime import date
import json
from unittest.mock import MagicMock, patch

import pytest

from audiobookdl import Audiobook, BookId, Series
from audiobookdl.args import build_default_options
from audiobookdl.exceptions import DataNotPresent, NoSourceFound, RequestError
from audiobookdl.sources import find_compatible_source
from audiobookdl.sources.podimo import PodimoSource


def _make_source(tmp_path) -> PodimoSource:
    opts = build_default_options(database_directory=str(tmp_path), skip_downloaded=False)
    return PodimoSource(opts)


def _json_resp(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = json.dumps(payload).encode("utf-8")
    resp.json.return_value = payload
    return resp


@pytest.mark.parametrize(
    "url",
    [
        "https://open.podimo.com/audiobook/abc-123",
        "https://open.podimo.com/podcast/pod-456",
        "https://share.podimo.com/s/short-code",
    ],
)
def test_find_compatible_source_matches_podimo_urls(url):
    assert find_compatible_source(url) is PodimoSource


def test_login_sends_graphql_credentials_and_sets_auth_header(tmp_path):
    src = _make_source(tmp_path)
    src._session.post = MagicMock(
        return_value=_json_resp(
            {"data": {"tokenWithCredentials": {"token": "podimo-jwt-token"}}}
        )
    )

    src.login(
        "https://open.podimo.com/audiobook/abc-123",
        username="reader@example.com",
        password="secret",
    )

    assert src.authenticated is True
    assert src._session.headers["authorization"] == "podimo-jwt-token"
    src._session.post.assert_called_once()
    call_args = src._session.post.call_args
    assert call_args.args[0] == "https://podimo.com/graphql"
    assert call_args.kwargs["headers"] == {"User-Agent": "JS GraphQL"}
    body = call_args.kwargs["json"]
    assert body["operationName"] == "web_logInUser"
    assert body["variables"] == {"email": "reader@example.com", "password": "secret"}
    assert "tokenWithCredentials" in body["query"]


def test_download_routes_direct_audiobook_and_rejects_unknown_url(tmp_path):
    src = _make_source(tmp_path)
    src._session.post = MagicMock(
        side_effect=[
            _json_resp(
                {
                    "data": {
                        "audiobookById": {
                            "title": "Direct Book",
                            "authors": [{"name": "A1"}],
                            "narrators": [{"name": "N1"}],
                            "description": "Summary",
                            "coverImage": {"url": "https://cdn.podimo.com/book.png"},
                        }
                    }
                }
            ),
            _json_resp(
                {"data": {"audiobookAudioById": {"url": "https://cdn.podimo.com/book.mp3"}}}
            ),
        ]
    )

    unauth_session = MagicMock()
    unauth_session.get.return_value = MagicMock(status_code=200, content=b"cover-png")
    with patch.object(src, "create_unauthenticated_session", return_value=unauth_session):
        result = src.download("https://open.podimo.com/audiobook/book-99")

    assert isinstance(result, Audiobook)
    assert result.metadata.title == "Direct Book"
    assert result.metadata.authors == ["A1"]
    assert result.metadata.narrators == ["N1"]
    assert result.metadata.description == "Summary"
    assert result.files[0].url == "https://cdn.podimo.com/book.mp3"
    assert result.cover is not None and result.cover.image == b"cover-png"

    with pytest.raises(NoSourceFound):
        src.download("https://example.com/unsupported")


def test_download_podcast_and_episodes_with_hls_and_mp3_streams(tmp_path):
    src = _make_source(tmp_path)
    ep1 = {
        "id": "ep-1",
        "podcastId": "pod-1",
        "title": "Episode One",
        "podcastName": "Tech Pod",
        "description": "First episode",
        "publishDatetime": "2024-07-10T14:30:00Z",
        "authorName": "Host One",
        "imageUrl": "https://cdn.podimo.com/ep1.png",
    }
    ep2 = {
        "id": "ep-2",
        "podcastId": "pod-1",
        "title": "Episode Two",
        "podcastName": "Tech Pod",
        "description": None,
        "publishDatetime": "2024-08-01T09:00:00+02:00",
        "authorName": "",
        "imageUrl": "https://cdn.podimo.com/ep2.png",
    }
    ep3 = {
        "id": "ep-3",
        "podcastId": "pod-1",
        "title": "Episode Three",
        "imageUrl": "https://cdn.podimo.com/ep3.png",
        "authorName": None,
    }

    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"data": {"podcastById": {"title": "Tech Pod"}}}),
            _json_resp({"data": {"podcastEpisodes": [ep1, ep2, ep3]}}),
            _json_resp(
                {
                    "data": {
                        "podcastEpisodeAudioById": {
                            "url": "https://cdn.podimo.com/ep1/main.m3u8"
                        }
                    }
                }
            ),
            _json_resp(
                {
                    "data": {
                        "podcastEpisodeAudioById": {
                            "url": "https://cdn.podimo.com/ep2/audio.mp3"
                        }
                    }
                }
            ),
        ]
    )

    master_bytes = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=64000\n"
        b"stream_audio_low/stream.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=192000\n"
        b"stream_audio_high/stream.m3u8\n"
    )
    playlist_bytes = b"#EXTM3U\n#EXTINF:10.0,\nseg0.ts\n#EXTINF:10.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=master_bytes),
            MagicMock(status_code=200, content=playlist_bytes),
        ]
    )

    series = src.download("https://open.podimo.com/podcast/pod-1")
    assert isinstance(series, Series)
    assert series.title == "Tech Pod"
    assert len(series.books) == 3
    assert all(isinstance(b, BookId) for b in series.books)

    unauth_session = MagicMock()
    unauth_session.get.return_value = MagicMock(status_code=200, content=b"ep-cover")
    with patch.object(src, "create_unauthenticated_session", return_value=unauth_session):
        book1 = src.download_from_id(series.books[0].id)  # type: ignore[union-attr]
        book2 = src.download_from_id(series.books[1].id)  # type: ignore[union-attr]

    # HLS episode resolves master playlist to highest-bandwidth variant via get_stream_files
    assert [c.args[0] for c in src._session.get.call_args_list] == [
        "https://cdn.podimo.com/ep1/main.m3u8",
        "https://cdn.podimo.com/ep1/stream_audio_high/stream.m3u8",
    ]
    assert [f.url for f in book1.files] == [
        "https://cdn.podimo.com/ep1/stream_audio_high/seg0.ts",
        "https://cdn.podimo.com/ep1/stream_audio_high/seg1.ts",
    ]
    assert book1.metadata.series == "Tech Pod"
    assert book1.metadata.authors == ["Host One"]
    assert book1.metadata.release_date == date(2024, 7, 10)
    assert book1.cover is not None and book1.cover.extension == "png"

    # MP3 episode with empty authorName and offset publishDatetime
    assert [f.url for f in book2.files] == ["https://cdn.podimo.com/ep2/audio.mp3"]
    assert book2.files[0].ext == "mp3"
    assert book2.metadata.authors == []
    assert book2.metadata.release_date == date(2024, 8, 1)

    # Episode without publishDatetime leaves release_date as None
    meta3 = src.format_podcast_metadata(ep3)
    assert meta3.release_date is None
    assert meta3.authors == []


def test_share_link_follows_two_redirects_to_podcast_and_handles_failures(tmp_path):
    src = _make_source(tmp_path)
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"data": {"podcastById": {"title": "Shared Pod"}}}),
            _json_resp({"data": {"podcastEpisodes": []}}),
        ]
    )

    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=302, headers={"location": "https://podimo.com/landing/xyz"}),
            MagicMock(status_code=301, headers={"location": "https://open.podimo.com/podcast/pod-77"}),
        ]
    )
    result = src.download("https://share.podimo.com/s/short")

    assert isinstance(result, Series)
    assert result.title == "Shared Pod"
    assert src._session.get.call_count == 2

    # Non-redirect response in redirect_of -> DataNotPresent
    src._session.get = MagicMock(return_value=MagicMock(status_code=200, headers={}))
    with pytest.raises(DataNotPresent):
        src.download("https://share.podimo.com/s/broken")

    # Redirect missing Location header -> DataNotPresent
    src._session.get = MagicMock(return_value=MagicMock(status_code=302, headers={}))
    with pytest.raises(DataNotPresent):
        src.download("https://share.podimo.com/s/no-loc")

    # Redirect to pathless host -> DataNotPresent
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=302, headers={"location": "https://open.podimo.com"}),
            MagicMock(status_code=302, headers={"location": "https://open.podimo.com"}),
        ]
    )
    with pytest.raises(DataNotPresent):
        src.download("https://share.podimo.com/s/no-path")

    # Redirect to unsupported kind (neither audiobook nor podcast) -> DataNotPresent
    src._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=302, headers={"location": "https://open.podimo.com/user/123"}),
            MagicMock(status_code=302, headers={"location": "https://open.podimo.com/user/123"}),
        ]
    )
    with pytest.raises(DataNotPresent):
        src.download("https://share.podimo.com/s/wrong-kind")


def test_podimo_pagination_and_redacted_token_logs(tmp_path, monkeypatch):
    from audiobookdl import logging as adl_logging

    src = _make_source(tmp_path)
    debug_msgs = []
    monkeypatch.setattr(adl_logging, "debug", lambda msg, **_kw: debug_msgs.append(msg))

    src._session.post = MagicMock(
        return_value=_json_resp(
            {"data": {"tokenWithCredentials": {"token": "podimo-secret-jwt-value"}}}
        )
    )
    src.login("https://open.podimo.com/audiobook/1", username="u", password="p")
    assert all("podimo-secret-jwt-value" not in msg for msg in debug_msgs)

    page_1 = [{"id": f"ep-{i}", "podcastId": "pod-1"} for i in range(100)]
    page_2 = [{"id": "ep-100", "podcastId": "pod-1"}]
    src._session.post = MagicMock(
        side_effect=[
            _json_resp({"data": {"podcastEpisodes": page_1}}),
            _json_resp({"data": {"podcastEpisodes": page_2}}),
        ]
    )
    episodes = src.download_podcast_episode_ids("pod-1")
    assert len(episodes) == 101
    assert episodes[-1].id["id"] == "ep-100"


def test_podimo_download_cover_returns_none_on_404_and_raises_on_500(tmp_path):
    src = _make_source(tmp_path)
    assert src.download_cover(None) is None

    unauth_404 = MagicMock()
    unauth_404.get.return_value = MagicMock(status_code=404, content=b"")
    with patch.object(src, "create_unauthenticated_session", return_value=unauth_404):
        assert src.download_cover("https://cdn.podimo.com/missing.png") is None
    unauth_404.close.assert_called_once()

    unauth_500 = MagicMock()
    unauth_500.get.return_value = MagicMock(status_code=500, content=b"")
    with patch.object(src, "create_unauthenticated_session", return_value=unauth_500):
        with pytest.raises(RequestError):
            src.download_cover("https://cdn.podimo.com/error.png")
    unauth_500.close.assert_called_once()


def test_podimo_main_m3u8_chapters_search_and_library(tmp_path):
    src = _make_source(tmp_path)
    src._session.post = MagicMock(
        return_value=_json_resp(
            {
                "data": {
                    "audiobookAudioById": {
                        "url": "https://cdn.podimo.com/ab-1/main.m3u8"
                    }
                }
            }
        )
    )
    src._session.get = MagicMock(
        return_value=MagicMock(
            status_code=200,
            content=b"#EXTM3U\n#EXTINF:5.0,\nseg0.ts\n#EXT-X-ENDLIST\n",
        )
    )
    files = src.get_audiobook_files("ab-1")
    assert [f.url for f in files] == ["https://cdn.podimo.com/ab-1/seg0.ts"]

    chapters = src.get_chapters(
        {"chapters": [{"title": "Intro", "startMs": 0}, {"title": "Ch 1", "startMs": 5000}]}
    )
    assert [(c.start, c.title) for c in chapters] == [(0, "Intro"), (5000, "Ch 1")]

    src._session.post = MagicMock(
        return_value=_json_resp(
            {
                "data": {
                    "search": {
                        "audiobooks": [{"id": "ab1", "title": "Pod Book", "authors": [{"name": "Auth"}]}],
                        "podcasts": [{"id": "pod1", "title": "Pod Show", "authorName": "Host"}],
                    }
                }
            }
        )
    )
    hits = src.search("Pod")
    assert [(h.title, h.url) for h in hits] == [
        ("Pod Book", "https://open.podimo.com/audiobook/ab1"),
        ("Pod Show", "https://open.podimo.com/podcast/pod1"),
    ]

    src._session.post = MagicMock(
        return_value=_json_resp(
            {
                "data": {
                    "userLibrary": {
                        "audiobooks": [{"id": "ab2", "title": "Saved Audio", "authors": []}],
                        "podcasts": [],
                    }
                }
            }
        )
    )
    lib = src.list_library()
    assert len(lib) == 1
    assert lib[0].url == "https://open.podimo.com/audiobook/ab2"

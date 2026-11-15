from unittest.mock import MagicMock, patch

import pytest

from audiobookdl import DownloadOptions
from audiobookdl.exceptions import RequestError, UserNotAuthorized
from audiobookdl.sources.source import Source
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT


class _DummySource(Source):
    names = ["dummy"]


def _make_source(tmp_path, responses=None):
    options = DownloadOptions(
        database_directory=str(tmp_path),
        skip_downloaded=False,
    )
    source = _DummySource(options)
    if responses is not None:
        source.get = MagicMock(side_effect=responses)
    return source


def test_source_instances_do_not_share_page_cache(tmp_path):
    url = "https://example.com/book"
    first = _make_source(tmp_path, [b"account-1"])
    second = _make_source(tmp_path, [b"account-2"])

    assert first._get_page(url) == b"account-1"
    assert first._get_page(url) == b"account-1"
    assert first.get.call_count == 1

    assert second._get_page(url) == b"account-2"
    assert second.get.call_count == 1


def test_get_page_with_use_cache_false_bypasses_cache(tmp_path):
    url = "https://example.com/dynamic"
    source = _make_source(tmp_path, [b"fresh-1", b"cached", b"fresh-2"])

    assert source._get_page(url, use_cache=False) == b"fresh-1"
    assert source._get_page(url, use_cache=True) == b"cached"
    assert source._get_page(url, use_cache=True) == b"cached"
    assert source._get_page(url, use_cache=False) == b"fresh-2"
    assert source._get_page(url, use_cache=True) == b"cached"
    assert source.get.call_count == 3


def test_source_get_and_post_use_default_timeout_and_allow_override(tmp_path):
    source = _make_source(tmp_path)
    ok_resp = MagicMock(status_code=200, content=b"ok")
    source._session.get = MagicMock(return_value=ok_resp)
    source._session.post = MagicMock(return_value=ok_resp)

    assert source.get("https://example.com/a") == b"ok"
    source._session.get.assert_called_with(
        "https://example.com/a",
        timeout=DEFAULT_HTTP_TIMEOUT,
    )

    assert source.get("https://example.com/b", timeout=5) == b"ok"
    source._session.get.assert_called_with(
        "https://example.com/b",
        timeout=5,
    )

    assert source.post("https://example.com/c", data={"k": "v"}) == b"ok"
    source._session.post.assert_called_with(
        "https://example.com/c",
        timeout=DEFAULT_HTTP_TIMEOUT,
        data={"k": "v"},
    )


def test_get_stream_files_loads_playlist_through_source_session(tmp_path):
    playlist_bytes = (
        b"#EXTM3U\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001\n'
        b"#EXTINF:10.0,\n"
        b"seg-0.ts?token=abc\n"
        b"#EXT-X-ENDLIST\n"
    )
    key_bytes = b"0123456789abcdef"

    source = _make_source(tmp_path)
    playlist_resp = MagicMock(status_code=200, content=playlist_bytes)
    key_resp = MagicMock(status_code=200, content=key_bytes)
    source._session.get = MagicMock(side_effect=[playlist_resp, key_resp])

    with patch("audiobookdl.sources.source.networking.m3u8.load", side_effect=AssertionError("must not call m3u8.load")):
        files = source.get_stream_files(
            "https://cdn.example.com/audio/playlist.m3u8",
            headers={"Authorization": "Bearer tok"},
        )

    assert len(files) == 1
    assert files[0].url == "https://cdn.example.com/audio/seg-0.ts?token=abc"
    assert files[0].ext == "ts"
    assert files[0].headers == {"Authorization": "Bearer tok"}
    assert files[0].encryption_method is not None
    assert files[0].encryption_method.key == key_bytes
    assert source._session.get.call_args_list[0].kwargs["timeout"] == DEFAULT_HTTP_TIMEOUT
    assert source._session.get.call_args_list[1].args[0] == "https://cdn.example.com/audio/key.bin"


def test_source_create_session_configures_20_connection_pool(tmp_path):
    source = _make_source(tmp_path)
    for scheme in ("https://", "http://"):
        adapter = source._session.get_adapter(f"{scheme}example.com")
        assert adapter._pool_connections == 20
        assert adapter._pool_maxsize == 20
        assert adapter.poolmanager.connection_pool_kw["maxsize"] == 20


def test_source_get_and_post_json_and_request_error(tmp_path):
    source = _make_source(tmp_path)
    source._session.get = MagicMock(
        return_value=MagicMock(status_code=200, content=b'{"ok": true}')
    )
    source._session.post = MagicMock(
        return_value=MagicMock(status_code=200, content=b'{"posted": 1}')
    )
    assert source.get_json("https://example.com/a.json") == {"ok": True}
    assert source.post_json("https://example.com/b.json") == {"posted": 1}

    source._session.get = MagicMock(
        return_value=MagicMock(status_code=500, content=b"server error")
    )
    with pytest.raises(RequestError):
        source.get("https://example.com/fail")

    source._session.post = MagicMock(
        return_value=MagicMock(status_code=401, content=b"unauthorized")
    )
    with pytest.raises(UserNotAuthorized):
        source.post("https://example.com/fail")

    source._session.get = MagicMock(
        return_value=MagicMock(status_code=403, content=b"forbidden")
    )
    with pytest.raises(UserNotAuthorized):
        source.get("https://example.com/forbidden")


def test_source_load_cookie_file_authenticates_session(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tFALSE\t2147483647\tsession_id\tabc123\n",
        encoding="utf-8",
    )
    source = _make_source(tmp_path)
    assert source.authenticated is False
    source.load_cookie_file(str(cookie_file))
    assert source.authenticated is True
    assert source._session.cookies.get("session_id") == "abc123"


def test_get_stream_files_handles_mixed_extensions_missing_iv_and_variants(tmp_path):
    master_m3u8 = (
        b"#EXTM3U\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=64000\n"
        b"low/stream.m3u8\n"
        b"#EXT-X-STREAM-INF:BANDWIDTH=128000\n"
        b"high/stream.m3u8\n"
    )
    media_m3u8 = (
        b"#EXTM3U\n"
        b"#EXT-X-MEDIA-SEQUENCE:10\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        b"#EXTINF:5.0,\n"
        b"init.mp4?token=1.2\n"
        b"#EXTINF:5.0,\n"
        b"seg1.aac?token=3.4\n"
        b"#EXT-X-ENDLIST\n"
    )
    key_bytes = b"0123456789abcdef"

    source = _make_source(tmp_path)
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=master_m3u8),
            MagicMock(status_code=200, content=media_m3u8),
            MagicMock(status_code=200, content=key_bytes),
        ]
    )

    files = source.get_stream_files("https://cdn.example.com/master.m3u8")
    assert [f.ext for f in files] == ["mp4", "aac"]
    assert [f.url for f in files] == [
        "https://cdn.example.com/high/init.mp4?token=1.2",
        "https://cdn.example.com/high/seg1.aac?token=3.4",
    ]
    assert files[0].encryption_method is not None
    assert files[1].encryption_method is not None
    assert files[0].encryption_method.iv == (10).to_bytes(16, byteorder="big")
    assert files[1].encryption_method.iv == (11).to_bytes(16, byteorder="big")
    # Distinct header dicts per segment
    assert files[0].headers is not files[1].headers


def test_create_unauthenticated_session_is_pooled_and_isolated(tmp_path):
    source = _make_source(tmp_path)
    source._session.headers["Authorization"] = "Bearer secret"
    source._session.cookies.set("sid", "cookie-val")

    unauth = source.create_unauthenticated_session()
    assert "Authorization" not in unauth.headers
    assert unauth.cookies.get("sid") is None
    adapter = unauth.get_adapter("https://cdn.example.com")
    assert adapter.poolmanager.connection_pool_kw["maxsize"] == 20


def test_source_retries_transient_429_and_connection_errors_with_proxy_and_timeout(tmp_path, monkeypatch):
    import threading
    import requests
    from audiobookdl import args, logging
    from audiobookdl.exceptions import DownloadCancelled

    monkeypatch.setattr("audiobookdl.sources.source.networking.time.sleep", lambda _: None)
    opts = args.build_default_options(
        database_directory=str(tmp_path),
        proxy="http://127.0.0.1:8888",
        timeout=42.0,
    )
    source = _DummySource(opts)
    assert source._session.proxies["https"] == "http://127.0.0.1:8888"

    resp_429 = MagicMock(status_code=429, headers={"Retry-After": "1"}, content=b"rate limited")
    resp_ok = MagicMock(status_code=200, content=b"success")
    captured_timeouts = []

    def fake_get(_url, **kwargs):
        captured_timeouts.append(kwargs.get("timeout"))
        if len(captured_timeouts) == 1:
            raise requests.exceptions.Timeout("read timed out")
        if len(captured_timeouts) == 2:
            return resp_429
        return resp_ok

    source._session.get = MagicMock(side_effect=fake_get)
    assert source.get("https://example.com/retry") == b"success"
    assert len(captured_timeouts) == 3
    assert captured_timeouts[0][1] == 42.0
    resp_429.close.assert_called_once()

    # Cancel event interrupts retry backoff immediately without sleeping full interval
    cancel = threading.Event()
    cancel.set()
    source._session.get = MagicMock(side_effect=requests.exceptions.ConnectionError("reset"))
    with logging.job_context(logging.JobContext(cancel_event=cancel)):
        with pytest.raises(DownloadCancelled):
            source.get("https://example.com/cancelled")


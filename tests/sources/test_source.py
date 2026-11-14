from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from audiobookdl.sources.source import Source
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT


class _DummySource(Source):
    names = ["dummy"]


def _make_source(tmp_path, responses=None):
    options = SimpleNamespace(
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


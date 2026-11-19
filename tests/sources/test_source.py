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
    from audiobookdl import logging
    from audiobookdl.exceptions import DownloadCancelled
    from audiobookdl.options_schema import build_default_options

    monkeypatch.setattr("audiobookdl.sources.source.networking.time.sleep", lambda _: None)
    opts = build_default_options(
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
    # pyrefly: ignore[unsupported-operation]
    assert captured_timeouts[0][1] == 42.0
    resp_429.close.assert_called_once()

    # Cancel event interrupts retry backoff immediately without sleeping full interval
    cancel = threading.Event()
    cancel.set()
    source._session.get = MagicMock(side_effect=requests.exceptions.ConnectionError("reset"))
    with logging.job_context(logging.JobContext(cancel_event=cancel)):
        with pytest.raises(DownloadCancelled):
            source.get("https://example.com/cancelled")


def test_source_respects_configurable_max_retries_and_exponential_backoff(tmp_path, monkeypatch):
    import requests
    from audiobookdl.options_schema import build_default_options

    delays: list[float] = []
    monkeypatch.setattr("audiobookdl.sources.source.networking.time.sleep", delays.append)
    monkeypatch.setattr("audiobookdl.utils.http.random.uniform", lambda _a, _b: 0.0)

    # max_retries=0 disables retries and makes a single attempt
    opts_zero = build_default_options(database_directory=str(tmp_path), max_retries=0)
    src_zero = _DummySource(opts_zero)
    src_zero._session.get = MagicMock(side_effect=requests.exceptions.Timeout("boom"))
    with pytest.raises(RequestError):
        src_zero.get("https://example.com/no-retry")
    assert src_zero._session.get.call_count == 1
    assert delays == []

    # max_retries=3 retries up to 3 times with exponential backoff (1s, 2s, 4s)
    opts_three = build_default_options(database_directory=str(tmp_path), max_retries=3)
    src_three = _DummySource(opts_three)
    calls = 0

    def fake_get(_url, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return MagicMock(status_code=503, headers={}, content=b"busy")
        return MagicMock(status_code=200, headers={}, content=b"ok")

    src_three._session.get = MagicMock(side_effect=fake_get)
    assert src_three.get("https://example.com/backoff") == b"ok"
    assert calls == 4
    assert delays == [1.0, 2.0, 4.0]


def test_source_close_closes_child_sessions_and_save_cookie_file_is_atomic(tmp_path):
    source = _make_source(tmp_path)
    primary_close = MagicMock()
    child_close = MagicMock()
    source._session.close = primary_close
    cdn_session = source.create_unauthenticated_session()
    cdn_session.close = child_close
    source._session.cookies.set("tok", "secret", domain="example.com")

    cookie_dest = tmp_path / "cookies" / "session.txt"
    written = source.save_cookie_file(str(cookie_dest))
    assert written == str(cookie_dest)
    assert "tok\tsecret" in cookie_dest.read_text(encoding="utf-8")

    source.close()
    primary_close.assert_called_once()
    child_close.assert_called_once()
    # Calling close() twice is idempotent
    source.close()
    assert child_close.call_count == 1


def test_get_source_classes_discovers_valid_plugin_entry_points(tmp_path, monkeypatch):
    from audiobookdl.sources import Source, _discover_plugin_sources, get_source_classes

    class _PluginSource(Source):
        names = ["CustomPlugin"]
        match = [r"https?://plugin\.example\.com/.+"]

    class _InvalidPlugin:
        pass

    class _FakeEP:
        def __init__(self, name, obj=None, error=None):
            self.name = name
            self._obj = obj
            self._error = error

        def load(self):
            if self._error:
                raise self._error
            return self._obj

    _discover_plugin_sources.cache_clear()
    monkeypatch.setattr(
        "audiobookdl.sources.importlib.metadata.entry_points",
        lambda group="": [
            _FakeEP("good", obj=_PluginSource),
            _FakeEP("bad_type", obj=_InvalidPlugin),
            _FakeEP("broken", error=ImportError("missing dep")),
        ] if group == "audiobookdl.sources" else [],
    )
    try:
        classes = get_source_classes()
        assert _PluginSource in classes
        assert _InvalidPlugin not in classes
    finally:
        _discover_plugin_sources.cache_clear()


def test_load_cookie_file_ignores_empty_and_expired_jars_but_accepts_session_cookies(tmp_path):
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    src_empty = _make_source(tmp_path / "e")
    src_empty.load_cookie_file(str(empty_file))
    assert src_empty.authenticated is False

    expired_file = tmp_path / "expired.txt"
    expired_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tFALSE\t1\told_tok\tdead\n",
        encoding="utf-8",
    )
    src_expired = _make_source(tmp_path / "x")
    src_expired.load_cookie_file(str(expired_file))
    assert src_expired.authenticated is False
    assert src_expired._session.cookies.get("old_tok") is None

    session_file = tmp_path / "session.txt"
    session_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tsess_tok\talive\n",
        encoding="utf-8",
    )
    src_sess = _make_source(tmp_path / "s")
    src_sess.load_cookie_file(str(session_file))
    assert src_sess.authenticated is True
    assert src_sess._session.cookies.get("sess_tok") == "alive"


def test_get_stream_files_supports_ext_x_map_and_ext_x_byterange(tmp_path):
    playlist = (
        b"#EXTM3U\n"
        b'#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"\n'
        b"#EXTINF:5.0,\n"
        b"#EXT-X-BYTERANGE:1024@720\n"
        b"book.m4s\n"
        b"#EXTINF:5.0,\n"
        b"#EXT-X-BYTERANGE:512\n"
        b"book.m4s\n"
        b"#EXT-X-ENDLIST\n"
    )
    source = _make_source(tmp_path)
    init_bytes = b"FTYP_MOOV_INIT"

    def fake_get(url, headers=None, **_kwargs):
        if url.endswith("prog.m3u8"):
            return MagicMock(status_code=200, content=playlist)
        if url.endswith("init.mp4"):
            assert headers and headers.get("Range") == "bytes=0-719"
            return MagicMock(status_code=206, content=init_bytes)
        raise AssertionError(f"Unexpected GET {url}")

    source._session.get = MagicMock(side_effect=fake_get)
    files = source.get_stream_files("https://cdn.example.com/hls/prog.m3u8")

    assert len(files) == 2
    assert files[0].headers["Range"] == "bytes=720-1743"
    assert files[1].headers["Range"] == "bytes=1744-2255"
    assert files[0].init_section == init_bytes
    assert files[1].init_section == init_bytes
    # Initialization section was fetched only once for both segments
    assert source._session.get.call_count == 2


def test_get_stream_files_selects_audio_rendition_decodes_data_key_and_rejects_unsupported_method(tmp_path):
    import base64
    from audiobookdl.exceptions import GenericAudiobookDLException

    master_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/index.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=2000000,AUDIO="aud"\n'
        b"video/index.m3u8\n"
    )
    raw_key = b"fedcba9876543210"
    b64_key = base64.b64encode(raw_key).decode("ascii")
    audio_m3u8 = (
        b"#EXTM3U\n"
        + f'#EXT-X-KEY:METHOD=AES-128,URI="data:application/octet-stream;base64,{b64_key}"\n'.encode("ascii")
        + b"#EXTINF:10.0,\nseg0.aac\n#EXT-X-ENDLIST\n"
    )

    source = _make_source(tmp_path)
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=master_m3u8),
            MagicMock(status_code=200, content=audio_m3u8),
        ]
    )
    files = source.get_stream_files("https://cdn.example.com/master.m3u8")
    assert len(files) == 1
    assert files[0].url == "https://cdn.example.com/audio/seg0.aac"
    assert files[0].encryption_method is not None
    assert files[0].encryption_method.key == raw_key

    unsupported_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://cdn.example.com/k.bin"\n'
        b"#EXTINF:10.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
    )
    source._session.get = MagicMock(return_value=MagicMock(status_code=200, content=unsupported_m3u8))
    with pytest.raises(GenericAudiobookDLException, match="SAMPLE-AES"):
        source.get_stream_files("https://cdn.example.com/sample.m3u8")


def test_download_file_invokes_lazy_encryption_resolver(tmp_path):
    import io
    import zipfile
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata
    from audiobookdl.output.download import download_file
    from audiobookdl.utils.audiobook import AESCBCEncryption

    key = b"0123456789abcdef"
    iv = b"abcdef9876543210"
    epub_buf = io.BytesIO()
    with zipfile.ZipFile(epub_buf, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", b"<container/>")
    plaintext = epub_buf.getvalue()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Length": str(len(ciphertext))}
    resp.iter_content.return_value = [ciphertext]
    session.get.return_value = resp

    resolved_calls = 0

    def _resolve_enc():
        nonlocal resolved_calls
        resolved_calls += 1
        return AESCBCEncryption(key=key, iv=iv)

    ab = Audiobook(
        session=session,
        files=[AudiobookFile(url="https://cdn.example.com/book.epub", ext="epub", encryption_resolver=_resolve_enc)],
        metadata=AudiobookMetadata(title="Enc Book"),
    )
    out_path = download_file(
        (ab, str(tmp_path / "Enc Book"), 0, lambda _d: None, None),
    )
    assert resolved_calls == 1
    with open(out_path, "rb") as fh:
        assert fh.read() == plaintext


def test_get_stream_files_prefer_video_selects_highest_bandwidth_variant(tmp_path):
    master_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/prog.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=600000,AUDIO="aud"\n'
        b"video/360p.m3u8\n"
        b'#EXT-X-STREAM-INF:BANDWIDTH=2400000,AUDIO="aud"\n'
        b"video/1080p.m3u8\n"
    )
    video_m3u8 = (
        b"#EXTM3U\n"
        b"#EXTINF:5.0,\n"
        b"seg0.ts\n"
        b"#EXT-X-ENDLIST\n"
    )
    source = _make_source(tmp_path)
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=master_m3u8),
            MagicMock(status_code=200, content=video_m3u8),
        ]
    )

    files = source.get_stream_files(
        "https://cdn.example.com/master.m3u8",
        prefer_video=True,
    )
    assert len(files) == 1
    assert files[0].ext == "ts"
    assert files[0].url == "https://cdn.example.com/video/1080p.m3u8".replace("1080p.m3u8", "seg0.ts")


def test_get_stream_files_handles_utf8_bom_and_locale_audio_rendition(tmp_path):
    from types import SimpleNamespace

    master_m3u8 = (
        "\ufeff#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio/en.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Swedish",LANGUAGE="sv",DEFAULT=NO,URI="audio/sv.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1200000,AUDIO="aud"\n'
        "video/main.m3u8\n"
    ).encode("utf-8")
    sv_m3u8 = (
        "\ufeff#EXTM3U\n"
        "#EXTINF:5.0,\n"
        "sv_seg0.ts\n"
        "#EXT-X-ENDLIST\n"
    ).encode("utf-8")
    source = _make_source(tmp_path)
    source._options = SimpleNamespace(locale="sv-SE")
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=200, content=master_m3u8),
            MagicMock(status_code=200, content=sv_m3u8),
        ]
    )

    files = source.get_stream_files("https://cdn.example.com/master.m3u8")
    assert len(files) == 1
    assert files[0].url == "https://cdn.example.com/audio/sv_seg0.ts"


def test_source_get_or_create_device_id_persists_and_recovers_empty(tmp_path):
    calls = 0

    def _factory() -> str:
        nonlocal calls
        calls += 1
        return f"dev-{calls}"

    src = _make_source(tmp_path)
    first = src.get_or_create_device_id("device.txt", _factory)
    second = src.get_or_create_device_id("device.txt", _factory)
    assert first == "dev-1"
    assert second == "dev-1"
    assert calls == 1

    # Blank file regenerates instead of returning empty device id
    device_file = tmp_path / "dummy" / "device.txt"
    device_file.write_text("   \n", encoding="utf-8")
    regenerated = src.get_or_create_device_id("device.txt", _factory)
    assert regenerated == "dev-2"
    assert calls == 2


def test_plugin_source_validation_rejects_api_mismatch_and_name_collisions(monkeypatch):
    from audiobookdl.sources import (
        Source,
        PLUGIN_API_VERSION,
        get_source_classes,
        reset_plugin_sources_cache,
    )

    class _ValidPlugin(Source):
        names = ["BrandNewPlugin"]
        match = [r"https?://brandnew\.example\.com/.+"]
        plugin_api_version = PLUGIN_API_VERSION

    class _WrongVersionPlugin(Source):
        names = ["FuturePlugin"]
        match = [r"https?://future\.example\.com/.+"]
        plugin_api_version = PLUGIN_API_VERSION + 1

    class _BuiltinCollisionPlugin(Source):
        names = ["storytel"]
        match = [r"https?://evil\.example\.com/.+"]

    class _DuplicatePlugin(Source):
        names = ["brandnewplugin"]
        match = [r"https?://dup\.example\.com/.+"]

    class _EP:
        def __init__(self, name, cls):
            self.name = name
            self._cls = cls

        def load(self):
            return self._cls

    reset_plugin_sources_cache()
    monkeypatch.setattr(
        "audiobookdl.sources.importlib.metadata.entry_points",
        lambda group="": [
            _EP("valid", _ValidPlugin),
            _EP("wrong_ver", _WrongVersionPlugin),
            _EP("collide_builtin", _BuiltinCollisionPlugin),
            _EP("collide_plugin", _DuplicatePlugin),
        ]
        if group == "audiobookdl.sources"
        else [],
    )
    try:
        classes = get_source_classes()
        assert _ValidPlugin in classes
        assert _WrongVersionPlugin not in classes
        assert _BuiltinCollisionPlugin not in classes
        assert _DuplicatePlugin not in classes
    finally:
        reset_plugin_sources_cache()


def test_ereolen_refresh_library_domains_is_thread_safe():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from audiobookdl.sources.ereolen import EreolenSource

    orig_domains = list(EreolenSource.library_domains)
    orig_match = list(EreolenSource.match)
    in_flight = 0
    max_concurrent = 0
    probe_lock = threading.Lock()

    class _FakeSession:
        def get(self, _url, timeout=10.0):
            nonlocal in_flight, max_concurrent
            import time

            with probe_lock:
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
            time.sleep(0.02)
            with probe_lock:
                in_flight -= 1
            return MagicMock(
                status_code=200,
                text='<a href="https://odensebib.dk/">Odense</a>',
            )

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _i: EreolenSource.refresh_library_domains(session=_FakeSession()), range(4))
            )
        assert max_concurrent == 1
        assert all("odensebib.dk" in r for r in results)
        assert "odensebib.dk" in EreolenSource.library_domains
        assert any("odensebib" in pat for pat in EreolenSource.match)
    finally:
        EreolenSource.library_domains = orig_domains
        EreolenSource.match = orig_match


def test_source_handles_cdn_522_retries_cloudflare_challenge_and_redirected_hls(tmp_path, monkeypatch):
    from audiobookdl.exceptions import CloudflareBlocked

    monkeypatch.setattr("audiobookdl.sources.source.networking.time.sleep", lambda _: None)
    monkeypatch.setattr("audiobookdl.utils.http.random.uniform", lambda _a, _b: 0.0)

    # 1. Cloudflare 522 & 408 transient CDN errors are retried with X-Retry-After
    source = _make_source(tmp_path)
    delays: list[float] = []
    monkeypatch.setattr("audiobookdl.sources.source.networking.time.sleep", delays.append)
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(status_code=522, headers={"x-retry-after": "2"}, content=b"origin timeout"),
            MagicMock(status_code=408, headers={}, content=b"req timeout"),
            MagicMock(status_code=200, headers={}, content=b"recovered"),
        ]
    )
    assert source.get("https://cdn.example.com/api") == b"recovered"
    assert delays == [2.0, 2.0]

    # 2. Cloudflare interactive challenge raises CloudflareBlocked immediately without retrying
    source._session.get = MagicMock(
        return_value=MagicMock(
            status_code=503,
            headers={"cf-mitigated": "challenge", "server": "cloudflare"},
            content=b"<html><title>Just a moment...</title></html>",
        )
    )
    with pytest.raises(CloudflareBlocked):
        source.get("https://cdn.example.com/blocked")
    assert source._session.get.call_count == 1

    # 3. Redirected HLS playlist resolves relative segment URIs against post-redirect resp.url,
    #    and slices init_section when CDN ignores Range on EXT-X-MAP and returns 200 OK.
    redirected_m3u8 = (
        b"#EXTM3U\n"
        b'#EXT-X-MAP:URI="media.mp4",BYTERANGE="4@2"\n'
        b"#EXTINF:5.0,\n"
        b"seg-0.m4s\n"
        b"#EXT-X-ENDLIST\n"
    )
    full_mp4 = b"XXINITrest-of-mp4"
    source._session.get = MagicMock(
        side_effect=[
            MagicMock(
                status_code=200,
                url="https://edge-eu.cdn.example.net/book42/stream.m3u8",
                headers={},
                content=redirected_m3u8,
            ),
            MagicMock(status_code=200, headers={}, content=full_mp4),
        ]
    )
    files = source.get_stream_files("https://api.example.com/play/master.m3u8")
    assert len(files) == 1
    assert files[0].url == "https://edge-eu.cdn.example.net/book42/seg-0.m4s"
    assert files[0].init_section == b"INIT"

    # 4. Non-M3U8 HTML soft-error playlist raises RequestError
    source._session.get = MagicMock(
        return_value=MagicMock(
            status_code=200,
            url="https://cdn.example.com/bad.m3u8",
            headers={"Content-Type": "text/html"},
            content=b"<html><body>Not Found</body></html>",
        )
    )
    with pytest.raises(RequestError, match="non-M3U8"):
        source.get_stream_files("https://cdn.example.com/bad.m3u8")








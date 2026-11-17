import sys
from unittest.mock import MagicMock

import requests

from audiobookdl.utils.updater import (
    UpdateInfo,
    check_for_updates,
    install_ffmpeg_via_winget,
    is_newer_version,
    parse_version_tuple,
    updates_disabled,
    winget_available,
)


def test_parse_version_and_is_newer():
    assert parse_version_tuple("v1.10.2") == (1, 10, 2)
    assert parse_version_tuple("0.9.0") == (0, 9, 0)
    assert is_newer_version("1.10.0", "1.9.9") is True
    assert is_newer_version("v1.9.0", "1.9.0") is False
    assert is_newer_version("1.8.5", "1.9.0") is False


def test_updates_disabled_env_var(monkeypatch):
    monkeypatch.delenv("AUDIOBOOK_DL_NO_UPDATE_CHECK", raising=False)
    assert updates_disabled() is False
    monkeypatch.setenv("AUDIOBOOK_DL_NO_UPDATE_CHECK", "1")
    assert updates_disabled() is True
    assert check_for_updates(current_version="0.1.0", respect_opt_out=True) is None


def test_check_for_updates_github_and_pypi_fallback():
    class _FakeResp:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _GitHubSession:
        def get(self, url, **_kwargs):
            assert "api.github.com" in url
            return _FakeResp(
                200,
                {"tag_name": "v9.9.9", "html_url": "https://github.com/example/releases/v9.9.9"},
            )

    info = check_for_updates(current_version="0.1.0", session=_GitHubSession(), respect_opt_out=False)
    assert info == UpdateInfo(
        current_version="0.1.0",
        latest_version="9.9.9",
        has_update=True,
        release_url="https://github.com/example/releases/v9.9.9",
    )

    class _FallbackSession:
        def get(self, url, **_kwargs):
            if "api.github.com" in url:
                raise requests.ConnectionError("github unreachable")
            assert "pypi.org" in url
            return _FakeResp(
                200,
                {"info": {"version": "2.5.0", "project_url": "https://pypi.org/project/audiobook-dl/"}},
            )

    fallback = check_for_updates(current_version="2.5.0", session=_FallbackSession(), respect_opt_out=False)
    assert fallback is not None
    assert fallback.has_update is False
    assert fallback.latest_version == "2.5.0"

    class _OfflineSession:
        def get(self, *_args, **_kwargs):
            raise requests.Timeout("offline")

    assert check_for_updates(current_version="1.0.0", session=_OfflineSession(), respect_opt_out=False) is None


def test_winget_helpers(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("audiobookdl.utils.updater.shutil.which", lambda _name: r"C:\Windows\System32\winget.exe")
    assert winget_available() is True

    fake_proc = MagicMock(returncode=0, stderr="", stdout="Successfully installed")
    monkeypatch.setattr("audiobookdl.utils.updater.subprocess.run", lambda *a, **k: fake_proc)
    ok, msg = install_ffmpeg_via_winget()
    assert ok is True
    assert "winget" in msg.lower()

    monkeypatch.setattr("audiobookdl.utils.updater.shutil.which", lambda _name: None)
    assert winget_available() is False
    ok_missing, _ = install_ffmpeg_via_winget()
    assert ok_missing is False


def test_check_for_updates_cache_ttl_etag_and_winget_subprocess_kwargs(tmp_path, monkeypatch):
    import subprocess

    cache_file = tmp_path / "update_check.json"
    calls: list[dict] = []

    class _Resp:
        def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}

        def json(self) -> dict:
            return self._payload

    class _CacheSession:
        def __init__(self) -> None:
            self._step = 0

        def get(self, url, **kwargs):
            calls.append({"url": url, "headers": dict(kwargs.get("headers") or {})})
            self._step += 1
            if self._step == 1:
                return _Resp(
                    200,
                    {"tag_name": "v2.0.0", "html_url": "https://github.com/example/releases/v2.0.0"},
                    headers={"ETag": '"etag-v2"'},
                )
            if self._step == 2:
                assert kwargs["headers"].get("If-None-Match") == '"etag-v2"'
                return _Resp(304)
            return _Resp(
                200,
                {"tag_name": "v3.0.0", "html_url": "https://github.com/example/releases/v3.0.0"},
                headers={"ETag": '"etag-v3"'},
            )

    sess = _CacheSession()
    info1 = check_for_updates(
        current_version="1.0.0",
        session=sess,  # type: ignore[arg-type]
        respect_opt_out=False,
        cache_path=str(cache_file),
        cache_ttl_seconds=3600.0,
        now=1000.0,
    )
    assert info1 is not None and info1.latest_version == "2.0.0" and info1.has_update is True
    assert len(calls) == 1

    # Within TTL and force=False -> returns cached result with 0 network calls
    info2 = check_for_updates(
        current_version="2.0.0",
        session=sess,  # type: ignore[arg-type]
        respect_opt_out=False,
        cache_path=str(cache_file),
        cache_ttl_seconds=3600.0,
        now=2000.0,
    )
    assert info2 is not None and info2.latest_version == "2.0.0" and info2.has_update is False
    assert len(calls) == 1

    # Expired TTL with HTTP 304 refreshes cache without failing
    info3 = check_for_updates(
        current_version="1.0.0",
        session=sess,  # type: ignore[arg-type]
        respect_opt_out=False,
        cache_path=str(cache_file),
        cache_ttl_seconds=3600.0,
        now=5000.0,
    )
    assert info3 is not None and info3.latest_version == "2.0.0" and info3.has_update is True
    assert len(calls) == 2

    # force=True bypasses cache and omits conditional If-None-Match
    info4 = check_for_updates(
        current_version="1.0.0",
        session=sess,  # type: ignore[arg-type]
        respect_opt_out=False,
        cache_path=str(cache_file),
        cache_ttl_seconds=3600.0,
        force=True,
        now=5100.0,
    )
    assert info4 is not None and info4.latest_version == "3.0.0"
    assert len(calls) == 3
    assert "If-None-Match" not in calls[-1]["headers"]

    # install_ffmpeg_via_winget passes creationflags via subprocess_kwargs when CREATE_NO_WINDOW exists
    captured_run_kwargs: list[dict] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("audiobookdl.utils.updater.shutil.which", lambda _n: "winget.exe")
    monkeypatch.setattr(
        "audiobookdl.utils.updater.subprocess_kwargs",
        lambda capture_output=True: {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)},
    )

    def _capture_run(*_a, **kw):
        captured_run_kwargs.append(kw)
        return MagicMock(returncode=0, stderr="", stdout="ok")

    monkeypatch.setattr("audiobookdl.utils.updater.subprocess.run", _capture_run)
    ok_winget, _ = install_ffmpeg_via_winget()
    assert ok_winget is True
    assert "creationflags" in captured_run_kwargs[0]

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

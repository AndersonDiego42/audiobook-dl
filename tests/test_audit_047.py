"""Regression tests for AUD-047 fixes (contracts, exceptions, source/util bugs)."""

from __future__ import annotations

from typing import Any

import pytest

from audiobookdl import logging
from audiobookdl.config import load_source_oauth_token
from audiobookdl.exceptions import (
    AudiobookDLException,
    DownloadCancelled,
    NoSourceFound,
    RequestError,
    UserNotAuthorized,
)
from audiobookdl.options_schema import build_default_options
from audiobookdl.output.metadata.sidecars import build_abs_metadata_dict
from audiobookdl.utils.audiobook import AudiobookMetadata, Chapter
from audiobookdl.utils.browser_cookies import default_domains_for_service
from audiobookdl.utils.updater import is_newer_version, parse_version_tuple


class _RecordingSink:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def on_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        pass

    def on_progress_advance(self, completed: float, total: float) -> None:
        pass

    def on_progress_metrics(self, *args: Any) -> None:
        pass

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        pass

    def on_download_estimate(self, *args: Any) -> None:
        pass

    def on_progress_finish(self) -> None:
        pass


# --- updater.py: pre-release versions must not compare as newer -----------------


def test_parse_version_tuple_ignores_pre_release_suffix():
    assert parse_version_tuple("0.7.3rc1") == (0, 7, 3)
    assert parse_version_tuple("1.2.3a4") == (1, 2, 3)
    assert parse_version_tuple("v2.0.0+local") == (2, 0, 0)


def test_is_newer_version_pre_release_not_newer():
    assert is_newer_version("0.7.3rc1", "0.7.3") is False
    assert is_newer_version("0.7.3", "0.7.3rc1") is False
    assert is_newer_version("0.8.0rc1", "0.7.3") is True


# --- browser_cookies.py: unknown service returns [] instead of raising -----------


def test_default_domains_for_service_unknown_service_returns_empty():
    assert default_domains_for_service("definitely-not-a-real-service-xyz") == []


# --- sidecars.py: ABS metadata chapters sorted by start --------------------------


def test_abs_metadata_dict_sorts_unsorted_chapters():
    metadata = AudiobookMetadata(title="Book", source="test")
    chapters = [
        Chapter(start=30000, title="Third"),
        Chapter(start=0, title="First"),
        Chapter(start=10000, title="Second"),
    ]
    payload = build_abs_metadata_dict(metadata, chapters, duration_seconds=40.0)
    abs_chapters = payload["chapters"]
    assert [c["title"] for c in abs_chapters] == ["First", "Second", "Third"]
    assert [c["start"] for c in abs_chapters] == [0.0, 10.0, 30.0]
    assert abs_chapters[0]["end"] == 10.0
    assert abs_chapters[2]["end"] == 40.0


# --- config.py: load_source_oauth_token performs a single vault lookup -----------


class _CountingCredentialStore:
    def __init__(self, token_json: str | None = None) -> None:
        self.token_json = token_json
        self.calls: list[str] = []

    def get_oauth_token(self, profile_key: str) -> str | None:
        self.calls.append(profile_key)
        return self.token_json


def test_load_source_oauth_token_single_lookup_for_default_profile(tmp_path):
    store = _CountingCredentialStore(token_json=None)
    result = load_source_oauth_token(
        "librivox",
        overwrite=str(tmp_path),
        credential_store=store,  # type: ignore[arg-type]
    )
    assert result is None
    assert store.calls == ["librivox"]


def test_load_source_oauth_token_profile_key_lookup(tmp_path):
    store = _CountingCredentialStore(token_json=None)
    load_source_oauth_token(
        "librivox",
        profile="work",
        overwrite=str(tmp_path),
        credential_store=store,  # type: ignore[arg-type]
    )
    assert store.calls == ["librivox:work"]


# --- exceptions.py: machine-readable error info ----------------------------------


def test_exception_to_error_info_fields():
    exc = RequestError(detail="boom")
    info = exc.to_error_info()
    assert info["code"] == "request_error"
    assert info["category"] == "network"
    assert info["retryable"] is True
    assert "boom" in info["message"]
    assert info["data"]["detail"] == "boom"


def test_exception_defaults_and_cancelled_category():
    base = AudiobookDLException()
    info = base.to_error_info()
    assert info["code"] == "generic"
    assert str(base).strip() != ""

    cancelled = DownloadCancelled()
    assert cancelled.to_error_info()["category"] == "cancelled"
    assert "cancelled" in str(cancelled).lower()


def test_user_not_authorized_has_action_hint():
    info = UserNotAuthorized().to_error_info()
    assert info["action_hint"]
    assert info["category"] == "auth"


# --- logging.py: progress adapter contract parity --------------------------------


def test_sink_progress_adapter_accepts_none_total():
    sink = _RecordingSink()
    adapter = logging._SinkProgressAdapter(sink)
    task_id = adapter.add_task("task", total=None)  # type: ignore[arg-type]
    adapter.advance(task_id, 1.0)
    adapter.__exit__(None, None, None)


def test_headless_progress_adapter_estimate_methods():
    adapter = logging._HeadlessProgressAdapter()
    with adapter:
        task_id = adapter.add_task("download", total=3)
        adapter.set_file_size_estimates(task_id, [100, 200, None])
        est = adapter.get_download_estimate(task_id)
        assert est is not None
        # 2 of 3 files known -> extrapolated estimate, not exact
        assert est[0] == 450 and est[1] is False
        adapter.report_file_size(task_id, 2, 300)
        est2 = adapter.get_download_estimate(task_id)
        assert est2 is not None and est2[1] is True and est2[0] == 600


# --- pipeline.py: service facades forward sink into JobContext -------------------


def test_verify_service_auth_forwards_sink(tmp_path, monkeypatch):
    from audiobookdl.config import Config
    from audiobookdl.pipeline import verify_service_auth
    from audiobookdl.sources.source import Source

    class _LoggingSource(Source):
        name = "sinkt"
        names = ["SinkTest"]
        _authentication_methods = ["cookies"]

        def verify_auth(self, url: str = "") -> bool:
            logging.log("sink-probe-message")
            return True

    monkeypatch.setattr("audiobookdl.pipeline.auth.find_source_by_name", lambda _n: _LoggingSource)
    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    sink = _RecordingSink()
    assert verify_service_auth("sinkt", build_default_options(), cfg, sink=sink) is True
    assert any("sink-probe-message" in msg for _lvl, msg in sink.logs)


def test_search_service_uses_ambient_sink_when_not_passed(tmp_path, monkeypatch):
    from audiobookdl.config import Config
    from audiobookdl.pipeline import search_service
    from audiobookdl.sources.source import Source

    class _AmbientSource(Source):
        name = "ambient"
        names = ["AmbientTest"]
        _authentication_methods = []

        def search(self, query: str):
            logging.log("ambient-search-message")
            return []

    monkeypatch.setattr("audiobookdl.pipeline.auth.find_source_by_name", lambda _n: _AmbientSource)
    cfg = Config(sources={}, output_template=None, database_directory=str(tmp_path), skip_downloaded=False)
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        assert search_service("ambient", "q", build_default_options(), cfg) == []
    assert any("ambient-search-message" in msg for _lvl, msg in sink.logs)


# --- sources/__init__.py: plugin diagnostics + URL matching ----------------------


def test_list_plugin_diagnostics_returns_list():
    from audiobookdl.sources import list_plugin_diagnostics

    diags = list_plugin_diagnostics()
    assert isinstance(diags, list)
    assert all(hasattr(d, "name") and hasattr(d, "ok") for d in diags)


def test_podimo_and_nextory_url_matching_escaped():
    from audiobookdl.sources import detect_service_key, find_compatible_source

    assert detect_service_key("https://open.podimo.com/audiobook/some-book") == "podimo"
    assert detect_service_key("https://share.podimo.com/s/abc123") == "podimo"
    # Unescaped dots previously matched arbitrary characters in the host.
    assert detect_service_key("https://openXpodimoYcom/audiobook/x") != "podimo"

    assert detect_service_key("https://www.nextory.se/books/123") == "nextory"
    assert detect_service_key("https://catalog-se.nextory.se/browse") == "nextory"
    assert detect_service_key("https://nextoryXevil.example.com/x") != "nextory"

    with pytest.raises(NoSourceFound):
        find_compatible_source("https://openXpodimoYcom/audiobook/x")

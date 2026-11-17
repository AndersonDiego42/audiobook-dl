"""Shared pytest fixtures, isolation guards, and failure diagnostics hooks for E2E tests."""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterator

import pytest

from audiobookdl import logging, utils
from tests.e2e.config import E2EConfig
from tests.e2e.diagnostics import E2EDiagnosticRecorder
from tests.e2e.fixtures import MediaFixtureFactory
from tests.e2e.harness import E2EWorkspace, GuiHarness, GuiSelfTestRunner, require_ffmpeg
from tests.e2e.server import LocalAudiobookServer


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Session-wide E2E configuration loaded from environment variables."""
    return E2EConfig.from_env()


@pytest.fixture(scope="session")
def media_factory(
    tmp_path_factory: pytest.TempPathFactory,
    e2e_config: E2EConfig,
) -> MediaFixtureFactory:
    """Session-cached generator for deterministic audio and cover fixtures."""
    ffmpeg_dir = str(e2e_config.ffmpeg_dir) if e2e_config.ffmpeg_dir is not None else None
    with logging.job_context(logging.JobContext(ffmpeg_dir=ffmpeg_dir)):
        ffmpeg = utils.find_executable("ffmpeg")
    cache_dir = tmp_path_factory.mktemp("e2e_media_cache")
    return MediaFixtureFactory(cache_dir=cache_dir, ffmpeg_path=ffmpeg)


@pytest.fixture(autouse=True)
def isolated_runtime_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Snapshot and restore environment variables across every E2E test."""
    prev_env = dict(os.environ)
    try:
        yield
    finally:
        for key in list(os.environ.keys()):
            if key not in prev_env:
                monkeypatch.delenv(key, raising=False)
        for key, val in prev_env.items():
            os.environ[key] = val


@pytest.fixture
def e2e_diagnostics(request: pytest.FixtureRequest) -> E2EDiagnosticRecorder:
    """Per-test diagnostic recorder attached to `request.node` for failure reporting."""
    recorder = E2EDiagnosticRecorder(request.node.nodeid)
    request.node._e2e_diagnostics = recorder  # type: ignore[attr-defined]
    return recorder


@pytest.fixture
def e2e_workspace(
    tmp_path: Path,
    e2e_config: E2EConfig,
    e2e_diagnostics: E2EDiagnosticRecorder,
) -> E2EWorkspace:
    """Isolated per-test filesystem workspace."""
    workspace = E2EWorkspace(tmp_path / "e2e_workspace", e2e_config)
    e2e_diagnostics.set_workspace_root(workspace.root)
    return workspace


@pytest.fixture
def e2e_server(
    e2e_config: E2EConfig,
    e2e_diagnostics: E2EDiagnosticRecorder,
) -> Iterator[LocalAudiobookServer]:
    """Start and readiness-check a per-test local HTTP origin/proxy server."""
    with LocalAudiobookServer(
        host=e2e_config.server_host,
        port=0,
        diagnostics=e2e_diagnostics,
    ) as server:
        yield server


@pytest.fixture
def gui_self_test_runner(
    e2e_config: E2EConfig,
    e2e_workspace: E2EWorkspace,
    e2e_diagnostics: E2EDiagnosticRecorder,
) -> GuiSelfTestRunner:
    """Subprocess runner for `--self-test` verification in an isolated workspace."""
    return GuiSelfTestRunner(
        config=e2e_config,
        workspace=e2e_workspace,
        diagnostics=e2e_diagnostics,
    )


@pytest.fixture
def gui_harness(
    e2e_config: E2EConfig,
    e2e_workspace: E2EWorkspace,
    e2e_diagnostics: E2EDiagnosticRecorder,
) -> Iterator[GuiHarness]:
    """Tkinter desktop GUI harness guaranteeing clean teardown."""
    harness = GuiHarness(
        config=e2e_config,
        workspace=e2e_workspace,
        diagnostics=e2e_diagnostics,
    )
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def ffmpeg_tools(e2e_config: E2EConfig):
    """Verify FFmpeg and FFprobe availability for media pipeline E2E tests."""
    return require_ffmpeg(e2e_config)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    """Attach captured E2E diagnostics to failed test reports and persist bundles if configured."""
    outcome = yield
    report = outcome.get_result()
    recorder = getattr(item, "_e2e_diagnostics", None)
    if recorder is not None and report.when in ("setup", "call") and report.failed:
        rendered = recorder.render_report()
        report.sections.append(("E2E Diagnostics", rendered))
        cfg = E2EConfig.from_env()
        if cfg.artifacts_dir is not None:
            bundle_dir = recorder.persist_bundle(cfg.artifacts_dir)
            report.sections.append(("E2E Artifact Bundle", str(bundle_dir)))

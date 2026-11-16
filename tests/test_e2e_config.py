"""Unit tests for E2E harness target configuration (`module` vs `executable`)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from tests.e2e.config import E2EConfig
from tests.e2e.diagnostics import E2EDiagnosticRecorder
from tests.e2e.harness import E2EWorkspace, GuiHarness


def test_e2e_config_commands_respect_executable_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIOBOOK_DL_E2E_TARGET", "executable")
    monkeypatch.delenv("AUDIOBOOK_DL_E2E_CLI_BIN", raising=False)
    monkeypatch.delenv("AUDIOBOOK_DL_E2E_GUI_BIN", raising=False)
    cfg = E2EConfig.from_env()
    assert cfg.cli_command() == ["audiobook-dl"]
    assert cfg.gui_self_test_command() == ["audiobook-dl-gui", "--self-test"]

    cli_bin = tmp_path / "audiobook-dl-cli.exe"
    gui_bin = tmp_path / "audiobook-dl.exe"
    monkeypatch.setenv("AUDIOBOOK_DL_E2E_CLI_BIN", str(cli_bin))
    monkeypatch.setenv("AUDIOBOOK_DL_E2E_GUI_BIN", str(gui_bin))
    cfg_bins = E2EConfig.from_env()
    assert cfg_bins.cli_command() == [str(cli_bin)]
    assert cfg_bins.gui_self_test_command() == [str(gui_bin), "--self-test"]


def test_e2e_config_commands_default_to_module_mode(monkeypatch):
    monkeypatch.delenv("AUDIOBOOK_DL_E2E_TARGET", raising=False)
    cfg = E2EConfig.from_env()
    assert cfg.cli_command() == [sys.executable, "-m", "audiobookdl"]
    assert cfg.gui_self_test_command() == [sys.executable, "-m", "audiobookdl.gui", "--self-test"]


def test_gui_harness_skips_in_executable_mode(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AUDIOBOOK_DL_E2E_TARGET", "executable")
    cfg = E2EConfig.from_env()
    rec = E2EDiagnosticRecorder("test_gui_harness_skips_in_executable_mode")
    ws = E2EWorkspace(tmp_path / "ws", cfg)
    harness = GuiHarness(cfg, ws, rec)
    with pytest.raises(pytest.skip.Exception, match="AUDIOBOOK_DL_E2E_TARGET=module"):
        harness.start()

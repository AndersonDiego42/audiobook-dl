import os
import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_EXE_PATH = REPO_ROOT / "scripts" / "build_exe.py"


def _load_build_exe():
    spec = importlib.util.spec_from_file_location("build_exe", BUILD_EXE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_exe_onedir_and_exclusions(tmp_path, monkeypatch):
    build_exe = _load_build_exe()
    monkeypatch.setattr(build_exe, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(build_exe, "BUILD_DIR", tmp_path / "build")

    captured_cmds = []

    def fake_run(cmd, cwd, check):
        captured_cmds.append(cmd)

    with patch.object(build_exe.subprocess, "run", side_effect=fake_run):
        exe_path = build_exe.build(onedir=True)

    assert exe_path.parent == tmp_path / "dist" / "audiobook-dl"
    cmd = captured_cmds[0]
    assert "--onedir" in cmd
    assert "--onefile" not in cmd
    for excluded in ("pytest", "unittest", "pydoc", "tkinter.test", "lib2to3", "setuptools", "_distutils_hack", "pkg_resources"):
        assert excluded in build_exe.EXCLUDED_MODULES
        idx = cmd.index(excluded)
        assert cmd[idx - 1] == "--exclude-module"
    if sys.version_info >= (3, 11):
        assert "tomli" not in build_exe.EXCLUDED_MODULES
        assert build_exe._collect_mypyc_binaries() == []
    if build_exe.ICON_PATH.is_file():
        assert "--icon" in cmd
    if os.name == "nt":
        assert "--version-file" in cmd
        version_file = Path(cmd[cmd.index("--version-file") + 1])
        assert version_file.is_file()
        assert "audiobook-dl.exe" in version_file.read_text(encoding="utf-8")


def test_verify_executable_invokes_self_test_in_temp_dir(tmp_path):
    build_exe = _load_build_exe()
    exe_path = tmp_path / "audiobook-dl.exe"
    exe_path.write_text("stub")

    calls = []

    def fake_run(cmd, cwd, check, timeout):
        calls.append((cmd, cwd, check, timeout))

    with patch.object(build_exe.subprocess, "run", side_effect=fake_run):
        build_exe.verify_executable(exe_path)

    assert len(calls) == 1
    cmd, cwd, check, _timeout = calls[0]
    assert cmd == [str(exe_path), "--self-test"]
    assert check is True
    assert Path(cwd) != tmp_path


def test_build_exe_copy_ffmpeg_targets_exe_directory(tmp_path, monkeypatch):
    build_exe = _load_build_exe()
    dist_dir = tmp_path / "dist"
    fake_bin_dir = tmp_path / "tools"
    fake_bin_dir.mkdir()
    (fake_bin_dir / "ffmpeg.exe").write_text("ff")
    (fake_bin_dir / "ffprobe.exe").write_text("fp")

    monkeypatch.setattr(build_exe, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_exe, "BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr(
        build_exe.shutil,
        "which",
        lambda name: str(fake_bin_dir / f"{name}.exe"),
    )

    with patch.object(build_exe.subprocess, "run"):
        exe_path = build_exe.build(copy_ffmpeg=True, onedir=True)

    assert (exe_path.parent / "ffmpeg.exe").is_file()
    assert (exe_path.parent / "ffprobe.exe").is_file()


def test_gui_launcher_parses_gui_args_and_launches_self_test():
    from audiobookdl.gui.app import main as gui_main, parse_gui_args

    parsed = parse_gui_args(["--self-test", "--config", "foo.toml"])
    assert parsed.self_test is True
    assert parsed.config_location == "foo.toml"

    with patch("audiobookdl.gui.app.run_self_test") as mock_self_test:
        gui_main(["--self-test"])
        mock_self_test.assert_called_once_with(check_gui=True)


def test_parse_gui_args_handles_windowed_none_streams_and_unknown_flags(monkeypatch):
    import pytest
    from audiobookdl.gui.app import parse_gui_args

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    parsed_unknown = parse_gui_args(["-psn_0_12345", "--unknown-flag"])
    assert parsed_unknown.self_test is False
    assert parsed_unknown.config_location is None

    with pytest.raises(SystemExit) as help_exc:
        parse_gui_args(["--help"])
    assert help_exc.value.code == 0

    with pytest.raises(SystemExit) as err_exc:
        parse_gui_args(["--config"])
    assert err_exc.value.code == 2


def test_build_exe_onedir_builds_single_gui_executable(tmp_path, monkeypatch):
    build_exe = _load_build_exe()
    dist_dir = tmp_path / "dist"
    monkeypatch.setattr(build_exe, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_exe, "BUILD_DIR", tmp_path / "build")

    captured_cmds = []

    def fake_run(cmd, cwd, check):
        captured_cmds.append(cmd)
        name = cmd[cmd.index("--name") + 1]
        out_dir = dist_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".exe" if os.name == "nt" else ""
        (out_dir / f"{name}{suffix}").write_text("stub")

    with patch.object(build_exe.subprocess, "run", side_effect=fake_run):
        exe_path = build_exe.build(onedir=True)

    assert len(captured_cmds) == 1
    assert "--windowed" in captured_cmds[0]
    assert str(build_exe.ENTRY_POINT) == captured_cmds[0][-1]
    assert [p.name for p in exe_path.parent.iterdir()] == [exe_path.name]

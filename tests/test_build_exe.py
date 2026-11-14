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

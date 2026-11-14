"""Build a portable Windows executable for audiobook-dl using PyInstaller."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "audiobookdl" / "gui" / "app.py"
ASSETS_DIR = REPO_ROOT / "audiobookdl" / "assets"
BUILD_DIR = REPO_ROOT / "build"
DIST_DIR = REPO_ROOT / "dist"


EXCLUDED_MODULES = (
    "PySide6",
    "shiboken6",
    "playwright",
    "pygame",
    "scipy",
    "matplotlib",
    "numpy",
    "pytest",
    "unittest",
    "pydoc",
    "pydoc_data",
    "doctest",
    "tkinter.test",
    "lib2to3",
    # On Python 3.12+, `distutils` is aliased from `setuptools._distutils` by PyInstaller's
    # pre-safe-import hook; excluding `distutils` directly triggers an alias conflict.
    *(("distutils",) if sys.version_info < (3, 12) else ()),
    "setuptools",
    "_distutils_hack",
    "pkg_resources",
)


def _collect_mypyc_binaries() -> list[Path]:
    """Return compiled mypyc runtime extensions referenced by `tomli`."""
    found: list[Path] = []
    try:
        dist_files = importlib.metadata.files("tomli") or []
    except importlib.metadata.PackageNotFoundError:
        return found
    for dist_file in dist_files:
        name = dist_file.name
        if "mypyc" in name and (name.endswith(".pyd") or name.endswith(".so")):
            located = Path(dist_file.locate())
            if located.is_file():
                found.append(located)
    return found


def build(
    bundle_ffmpeg: bool = False,
    copy_ffmpeg: bool = False,
    onedir: bool = False,
) -> Path:
    """Run PyInstaller to create a portable executable (`--onefile` or `--onedir`) and return its path."""
    sep = os.pathsep
    mode_flag = "--onedir" if onedir else "--onefile"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        mode_flag,
        "--windowed",
        "--name",
        "audiobook-dl",
        "--specpath",
        str(BUILD_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        "--distpath",
        str(DIST_DIR),
        "--paths",
        str(REPO_ROOT),
        "--add-data",
        f"{ASSETS_DIR}{sep}audiobookdl/assets",
        "--collect-data",
        "pycountry",
    ]
    for module in EXCLUDED_MODULES:
        cmd.extend(["--exclude-module", module])

    for mypyc_bin in _collect_mypyc_binaries():
        cmd.extend(["--add-binary", f"{mypyc_bin}{sep}."])

    binaries: list[Path] = []
    if bundle_ffmpeg or copy_ffmpeg:
        for tool in ("ffmpeg", "ffprobe"):
            found = shutil.which(tool)
            if not found:
                raise SystemExit(f"Error: '{tool}' was not found on PATH.")
            binaries.append(Path(found))

    if bundle_ffmpeg:
        for binary in binaries:
            cmd.extend(["--add-binary", f"{binary}{sep}."])

    cmd.append(str(ENTRY_POINT))
    subprocess.run(cmd, cwd=str(REPO_ROOT), check=True)

    exe_name = "audiobook-dl.exe" if os.name == "nt" else "audiobook-dl"
    exe_path = (DIST_DIR / "audiobook-dl" / exe_name) if onedir else (DIST_DIR / exe_name)
    if copy_ffmpeg and not bundle_ffmpeg:
        target_dir = exe_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        for binary in binaries:
            shutil.copy2(binary, target_dir / binary.name)
    return exe_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build portable audiobook-dl executable")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--onefile",
        action="store_true",
        help="Build a single-file portable executable (default)",
    )
    mode_group.add_argument(
        "--onedir",
        action="store_true",
        help="Build a zippable portable folder (dist/audiobook-dl/) for instant startup",
    )
    parser.add_argument(
        "--bundle-ffmpeg",
        action="store_true",
        help="Embed ffmpeg and ffprobe inside the executable bundle",
    )
    parser.add_argument(
        "--copy-ffmpeg",
        action="store_true",
        help="Copy ffmpeg and ffprobe beside the built executable",
    )
    args = parser.parse_args()
    exe_path = build(
        bundle_ffmpeg=args.bundle_ffmpeg,
        copy_ffmpeg=args.copy_ffmpeg,
        onedir=args.onedir,
    )
    print(f"Built portable executable: {exe_path}")


if __name__ == "__main__":
    main()

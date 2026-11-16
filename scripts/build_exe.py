"""Build a portable Windows executable for audiobook-dl using PyInstaller."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "audiobookdl" / "gui" / "app.py"
CLI_ENTRY_POINT = REPO_ROOT / "audiobookdl" / "__main__.py"
ASSETS_DIR = REPO_ROOT / "audiobookdl" / "assets"
ICON_PATH = ASSETS_DIR / "icon.ico"
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
    # Python 3.11+ uses stdlib `tomllib`, so `tomli` (and its mypyc binary) is unneeded.
    *(("tomli",) if sys.version_info >= (3, 11) else ()),
    "setuptools",
    "_distutils_hack",
    "pkg_resources",
)


def _read_version() -> str:
    init_py = REPO_ROOT / "audiobookdl" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_py.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "0.0.0"


def _write_windows_version_file(target_path: Path, version_str: str) -> Path:
    parts = [int(p) for p in re.findall(r"\d+", version_str)[:4]]
    while len(parts) < 4:
        parts.append(0)
    ver_tuple = tuple(parts)
    content = (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={ver_tuple},\n"
        f"    prodvers={ver_tuple},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable(\n"
        "        '040904B0',\n"
        "        [\n"
        "          StringStruct('CompanyName', 'jo1gi'),\n"
        "          StringStruct('FileDescription', 'audiobook-dl Desktop Downloader'),\n"
        f"          StringStruct('FileVersion', '{version_str}'),\n"
        "          StringStruct('InternalName', 'audiobook-dl'),\n"
        "          StringStruct('OriginalFilename', 'audiobook-dl.exe'),\n"
        "          StringStruct('ProductName', 'audiobook-dl'),\n"
        f"          StringStruct('ProductVersion', '{version_str}'),\n"
        "        ]\n"
        "      )\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    return target_path


def _collect_mypyc_binaries() -> list[Path]:
    """Return compiled mypyc runtime extensions referenced by `tomli` on Python < 3.11."""
    if sys.version_info >= (3, 11):
        return []
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

    if ICON_PATH.is_file():
        cmd.extend(["--icon", str(ICON_PATH)])

    if os.name == "nt":
        version_file = _write_windows_version_file(
            BUILD_DIR / "version_info.txt",
            _read_version(),
        )
        cmd.extend(["--version-file", str(version_file)])

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
    if onedir:
        cli_name = "audiobook-dl-cli"
        cli_exe_name = f"{cli_name}.exe" if os.name == "nt" else cli_name
        cli_cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--console",
            "--name",
            cli_name,
            "--specpath",
            str(BUILD_DIR),
            "--workpath",
            str(BUILD_DIR / "pyinstaller-cli"),
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
            cli_cmd.extend(["--exclude-module", module])
        for mypyc_bin in _collect_mypyc_binaries():
            cli_cmd.extend(["--add-binary", f"{mypyc_bin}{sep}."])
        if ICON_PATH.is_file():
            cli_cmd.extend(["--icon", str(ICON_PATH)])
        if os.name == "nt":
            cli_cmd.extend(["--version-file", str(version_file)])
        if bundle_ffmpeg:
            for binary in binaries:
                cli_cmd.extend(["--add-binary", f"{binary}{sep}."])
        cli_cmd.append(str(CLI_ENTRY_POINT))
        subprocess.run(cli_cmd, cwd=str(REPO_ROOT), check=True)
        cli_dist_dir = DIST_DIR / cli_name
        built_cli = cli_dist_dir / cli_exe_name
        if built_cli.is_file():
            exe_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built_cli, exe_path.parent / cli_exe_name)
        cli_internal_dir = cli_dist_dir / "_internal"
        if cli_internal_dir.is_dir():
            target_internal_dir = exe_path.parent / "_internal"
            target_internal_dir.mkdir(parents=True, exist_ok=True)
            for root_dir, _dirs, files in os.walk(cli_internal_dir):
                rel = Path(root_dir).relative_to(cli_internal_dir)
                dst_dir = target_internal_dir / rel
                dst_dir.mkdir(parents=True, exist_ok=True)
                for fname in files:
                    dst_file = dst_dir / fname
                    if not dst_file.exists():
                        shutil.copy2(Path(root_dir) / fname, dst_file)
        if cli_dist_dir.is_dir():
            shutil.rmtree(cli_dist_dir, ignore_errors=True)
    if copy_ffmpeg and not bundle_ffmpeg:
        target_dir = exe_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        for binary in binaries:
            shutil.copy2(binary, target_dir / binary.name)
    return exe_path


def verify_executable(exe_path: Path, timeout: int = 60) -> None:
    """Launch the built GUI executable with `--self-test` and companion CLI if present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            [str(exe_path), "--self-test"],
            cwd=tmpdir,
            check=True,
            timeout=timeout,
        )
        cli_name = "audiobook-dl-cli.exe" if os.name == "nt" else "audiobook-dl-cli"
        cli_path = exe_path.parent / cli_name
        if cli_path.is_file():
            subprocess.run(
                [str(cli_path), "serve", "--help"],
                cwd=tmpdir,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )


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
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run `--self-test` smoke verification on the built executable (default: enabled)",
    )
    args = parser.parse_args()
    exe_path = build(
        bundle_ffmpeg=args.bundle_ffmpeg,
        copy_ffmpeg=args.copy_ffmpeg,
        onedir=args.onedir,
    )
    print(f"Built portable executable: {exe_path}")
    if args.verify:
        verify_executable(exe_path)
        print(f"Verified executable self-test: {exe_path}")


if __name__ == "__main__":
    main()

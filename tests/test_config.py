import os
import sys

import pytest

from audiobookdl import args, utils
from audiobookdl.config import (
    config_dir,
    get_config_location,
    is_portable_mode,
    load_config,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.exceptions import ConfigNotFound


def test_load_config_missing_override_raises_config_not_found(tmp_path):
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigNotFound):
        load_config(str(missing))


def test_load_config_resolves_relative_cookie_file(tmp_path):
    cookie_path = tmp_path / "cookies" / "site.txt"
    cookie_path.parent.mkdir()
    cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        'output_template = "{author}/{title}"\n'
        'skip_downloaded = true\n\n'
        '[sources.everand]\n'
        'cookie_file = "cookies/site.txt"\n',
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    assert loaded.output_template == "{author}/{title}"
    assert loaded.skip_downloaded is True
    assert loaded.sources["everand"].cookie_file == os.path.normpath(str(cookie_path))


def test_save_output_preferences_preserves_sources(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.librivox]\nusername = "reader"\n',
        encoding="utf-8",
    )

    save_output_preferences("m4b", True, overwrite=str(cfg_path))
    loaded = load_config(str(cfg_path))
    assert loaded.output_format == "m4b"
    assert loaded.combine is True
    assert loaded.sources["librivox"].username == "reader"

    save_output_preferences(
        "m4b",
        True,
        overwrite=str(cfg_path),
        audio_bitrate="192k",
        resume=True,
    )
    loaded_with_opts = load_config(str(cfg_path))
    assert loaded_with_opts.audio_bitrate == "192k"
    assert loaded_with_opts.resume is True

    # Updating only format/combine without passing audio_bitrate/resume preserves them
    save_output_preferences("mp3", False, overwrite=str(cfg_path))
    preserved = load_config(str(cfg_path))
    assert preserved.output_format == "mp3"
    assert preserved.combine is False
    assert preserved.audio_bitrate == "192k"
    assert preserved.resume is True

    # Passing None clears audio_bitrate
    save_output_preferences(None, False, overwrite=str(cfg_path), audio_bitrate=None, resume=False)
    reloaded = load_config(str(cfg_path))
    assert reloaded.output_format is None
    assert reloaded.combine is False
    assert reloaded.audio_bitrate is None
    assert reloaded.resume is False


def test_portable_config_mode_stores_config_and_db_beside_frozen_exe(tmp_path, monkeypatch):
    exe_dir = tmp_path / "usb_drive"
    exe_dir.mkdir()
    fake_exe = exe_dir / "audiobook-dl.exe"
    fake_exe.write_text("stub")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    # Without .portable or audiobook-dl.toml beside exe, uses standard user config dir
    assert is_portable_mode() is False

    # With .portable marker beside exe, config, db, and toml stay in exe_dir
    marker = exe_dir / ".portable"
    marker.write_text("", encoding="utf-8")
    assert is_portable_mode() is True
    assert config_dir() == str(exe_dir)
    assert get_config_location(None) == os.path.join(str(exe_dir), "audiobook-dl.toml")
    assert args.default_database_directory() == os.path.join(str(exe_dir), "db")

    # Also activates if audiobook-dl.toml exists beside exe without .portable
    marker.unlink()
    (exe_dir / "audiobook-dl.toml").write_text('output_template = "{title}"\n', encoding="utf-8")
    assert is_portable_mode() is True
    assert config_dir() == str(exe_dir)


def test_save_gui_preferences_round_trip_and_preserves_other_sections(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    save_source_credentials(
        "librivox",
        username="reader",
        overwrite=str(cfg_path),
    )

    save_gui_preferences(
        window_geometry="900x720+40+30",
        output_dir=str(tmp_path / "Books"),
        output_template="{author}/{title}",
        overwrite=str(cfg_path),
    )

    # Updating only window_geometry preserves output_dir and output_template
    save_gui_preferences(
        window_geometry="960x740+50+40",
        overwrite=str(cfg_path),
    )

    # Saving output preferences preserves [gui] and [sources.*]
    save_output_preferences("m4b", True, overwrite=str(cfg_path))

    loaded = load_config(str(cfg_path))
    assert loaded.output_format == "m4b"
    assert loaded.combine is True
    assert loaded.sources["librivox"].username == "reader"
    assert loaded.gui.window_geometry == "960x740+50+40"
    assert loaded.gui.output_dir == str(tmp_path / "Books")
    assert loaded.gui.output_template == "{author}/{title}"


def test_save_ffmpeg_dir_and_resolve_executable(tmp_path):
    ffmpeg_root = tmp_path / "ffmpeg-release"
    bin_dir = ffmpeg_root / "bin"
    bin_dir.mkdir(parents=True)
    exe_suffix = ".exe" if os.name == "nt" else ""
    ffmpeg_bin = bin_dir / f"ffmpeg{exe_suffix}"
    ffprobe_bin = bin_dir / f"ffprobe{exe_suffix}"
    ffmpeg_bin.write_text("stub")
    ffprobe_bin.write_text("stub")

    # Resolves bin/ subfolder automatically when user selects archive root
    resolved = utils.resolve_ffmpeg_dir(str(ffmpeg_root))
    assert resolved == os.path.abspath(str(bin_dir))

    cfg_path = tmp_path / "audiobook-dl.toml"
    save_ffmpeg_dir(resolved, overwrite=str(cfg_path))
    loaded = load_config(str(cfg_path))
    assert loaded.ffmpeg_dir == resolved

    prev = utils.get_ffmpeg_dir()
    utils.set_ffmpeg_dir(loaded.ffmpeg_dir)
    try:
        assert utils.find_executable("ffmpeg") == os.path.join(str(resolved), f"ffmpeg{exe_suffix}")
        assert utils.find_executable("ffprobe") == os.path.join(str(resolved), f"ffprobe{exe_suffix}")
    finally:
        utils.set_ffmpeg_dir(prev)

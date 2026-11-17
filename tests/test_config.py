import os
from pathlib import Path
import sys

import pytest

from audiobookdl import logging, options_schema, utils
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

    save_output_preferences("m4b", True, config_location=str(cfg_path))
    loaded = load_config(str(cfg_path))
    assert loaded.output_format == "m4b"
    assert loaded.combine is True
    assert loaded.sources["librivox"].username == "reader"

    save_output_preferences(
        "m4b",
        True,
        config_location=str(cfg_path),
        audio_bitrate="192k",
        resume=True,
    )
    loaded_with_opts = load_config(str(cfg_path))
    assert loaded_with_opts.audio_bitrate == "192k"
    assert loaded_with_opts.resume is True

    # Updating only format/combine without passing audio_bitrate/resume preserves them
    save_output_preferences("mp3", False, config_location=str(cfg_path))
    preserved = load_config(str(cfg_path))
    assert preserved.output_format == "mp3"
    assert preserved.combine is False
    assert preserved.audio_bitrate == "192k"
    assert preserved.resume is True

    # Passing None clears audio_bitrate
    save_output_preferences(None, False, config_location=str(cfg_path), audio_bitrate=None, resume=False)
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
    assert options_schema.default_database_directory() == os.path.join(str(exe_dir), "db")

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
    save_output_preferences("m4b", True, config_location=str(cfg_path))

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

    assert utils.get_ffmpeg_dir() is None
    with logging.job_context(logging.JobContext(ffmpeg_dir=loaded.ffmpeg_dir)):
        assert utils.find_executable("ffmpeg") == os.path.join(str(resolved), f"ffmpeg{exe_suffix}")
        assert utils.find_executable("ffprobe") == os.path.join(str(resolved), f"ffprobe{exe_suffix}")
    assert utils.get_ffmpeg_dir() is None


class _RecordingSink:
    """In-memory `LogSink` helper for capturing config warnings during tests."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def on_log(self, level: str, message: str) -> None:
        self.messages.append(message)

    def on_progress_start(self, description: str, total: float) -> None:
        pass

    def on_progress_advance(self, completed: float, total: float) -> None:
        pass

    def on_progress_finish(self) -> None:
        pass


def test_config_normalizes_source_aliases_and_warns_on_unknown_keys(tmp_path):
    from audiobookdl import logging

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        'unknown_top_key = "ignored"\n'
        'output_format = "m4b"\n'
        'combine = true\n'
        'max_workers = "not-an-int"\n'
        '[gui]\n'
        'bogus_gui_key = 1\n'
        '[sources.Scribd]\n'
        'username = "alias-user"\n'
        'bad_field = "x"\n'
        '[sources.libby]\n'
        'library = "nypl"\n',
        encoding="utf-8",
    )
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        loaded = load_config(str(cfg_path))

    assert "everand" in loaded.sources
    assert loaded.sources["everand"].username == "alias-user"
    assert "overdrive" in loaded.sources
    assert loaded.sources["overdrive"].library == "nypl"
    assert loaded.max_workers is None
    joined = "\n".join(sink.messages)
    assert "unknown_top_key" in joined
    assert "bogus_gui_key" in joined
    assert "bad_field" in joined
    assert "normalized to 'everand'" in joined


def test_load_and_save_media_type_config(tmp_path):
    from audiobookdl import logging

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('media_type = "EBOOK"\n', encoding="utf-8")
    loaded = load_config(str(cfg_path))
    assert loaded.media_type == "ebook"

    # Invalid value emits a warning and clears media_type
    cfg_path.write_text('media_type = "comic"\n', encoding="utf-8")
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        invalid_loaded = load_config(str(cfg_path))
    assert invalid_loaded.media_type is None
    assert any("media_type" in msg for msg in sink.messages)

    # Round-trip save_output_preferences
    save_output_preferences(None, False, config_location=str(cfg_path), media_type="both")
    saved = load_config(str(cfg_path))
    assert saved.media_type == "both"


def test_save_output_and_gui_preferences_round_trip_all_toggles(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    custom_archive = str(tmp_path / "custom.sqlite")
    cfg_path.write_text(f'download_archive = "{custom_archive.replace(chr(92), "/")}"\n', encoding="utf-8")

    save_output_preferences(
        "m4b",
        True,
        config_location=str(cfg_path),
        skip_downloaded=True,
        write_json_metadata=True,
        enrich_metadata=True,
        no_chapters=True,
        debug=True,
        split_chapters=True,
        write_opf=True,
        write_cue=True,
        write_cover=True,
        remove_chars=":?",
        cover=True,
        overwrite=False,
        max_workers=6,
        book_delay=1.5,
        proxy="http://127.0.0.1:8888",
        timeout=45.0,
    )
    save_gui_preferences(overwrite=str(cfg_path), use_download_archive=False)

    loaded = load_config(str(cfg_path))
    assert loaded.skip_downloaded is True
    assert loaded.write_json_metadata is True
    assert loaded.enrich_metadata is True
    assert loaded.no_chapters is True
    assert loaded.debug is True
    assert loaded.split_chapters is True
    assert loaded.write_opf is True
    assert loaded.write_cue is True
    assert loaded.write_cover is True
    assert loaded.remove_chars == ":?"
    assert loaded.cover is True
    assert loaded.overwrite is False
    assert loaded.max_workers == 6
    assert loaded.book_delay == pytest.approx(1.5)
    assert loaded.proxy == "http://127.0.0.1:8888"
    assert loaded.timeout == pytest.approx(45.0)
    assert loaded.gui.use_download_archive is False
    assert loaded.download_archive is not None
    assert Path(loaded.download_archive) == Path(custom_archive)

    # Clearing optional fields with None/0/"" removes them cleanly without touching unrelated values
    save_output_preferences(
        config_location=str(cfg_path),
        remove_chars="",
        overwrite=None,
        max_workers=None,
        book_delay=0.0,
        proxy=None,
        timeout=None,
    )
    cleared = load_config(str(cfg_path))
    assert cleared.remove_chars is None
    assert cleared.overwrite is None
    assert cleared.max_workers is None
    assert cleared.book_delay is None
    assert cleared.proxy is None
    assert cleared.timeout is None
    assert cleared.skip_downloaded is True
    assert cleared.debug is True


def test_gui_theme_config_round_trip_and_validation(tmp_path):
    from audiobookdl import logging

    cfg_path = tmp_path / "audiobook-dl.toml"
    save_gui_preferences(overwrite=str(cfg_path), theme="dark")
    loaded = load_config(str(cfg_path))
    assert loaded.gui.theme == "dark"

    cfg_path.write_text('[gui]\ntheme = "neon"\n', encoding="utf-8")
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        invalid_loaded = load_config(str(cfg_path))
    assert invalid_loaded.gui.theme is None
    assert any("theme" in msg for msg in sink.messages)


def test_gui_notify_on_complete_config_round_trip_and_validation(tmp_path):
    from audiobookdl import logging

    cfg_path = tmp_path / "audiobook-dl.toml"
    save_gui_preferences(overwrite=str(cfg_path), notify_on_complete=False)
    loaded = load_config(str(cfg_path))
    assert loaded.gui.notify_on_complete is False

    save_gui_preferences(overwrite=str(cfg_path), notify_on_complete=True)
    loaded_true = load_config(str(cfg_path))
    assert loaded_true.gui.notify_on_complete is True

    cfg_path.write_text('[gui]\nnotify_on_complete = "yes"\n', encoding="utf-8")
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        invalid_loaded = load_config(str(cfg_path))
    assert invalid_loaded.gui.notify_on_complete is None
    assert any("notify_on_complete" in msg for msg in sink.messages)


def test_multi_profile_source_config_round_trip_and_migration(tmp_path):
    from audiobookdl.config import (
        get_source_config,
        list_configured_source_profiles,
        list_source_profiles,
    )
    from audiobookdl.credentials import PlaintextFallbackCredentialStore

    cfg_path = tmp_path / "audiobook-dl.toml"
    store = PlaintextFallbackCredentialStore(allow_plaintext=True)

    save_source_credentials(
        "storytel",
        username="default@example.com",
        password="pw1",
        overwrite=str(cfg_path),
        credential_store=store,
    )
    save_source_credentials(
        "storytel",
        username="family@example.com",
        password="pw2",
        library="se",
        overwrite=str(cfg_path),
        credential_store=store,
        profile="Family",
    )

    loaded = load_config(str(cfg_path), credential_store=store)
    assert list_source_profiles(loaded, "storytel") == ["default", "family"]
    default_cfg = get_source_config(loaded, "storytel")
    assert default_cfg is not None
    assert default_cfg.username == "default@example.com"
    family_cfg = get_source_config(loaded, "storytel", "family")
    assert family_cfg is not None
    assert family_cfg.username == "family@example.com"
    assert family_cfg.library == "se"
    assert get_source_config(loaded, "storytel", "missing") is None
    assert [(s, p) for s, p, _ in list_configured_source_profiles(loaded)] == [
        ("storytel", "default"),
        ("storytel", "family"),
    ]

    # Removing default profile preserves named profile
    save_source_credentials("storytel", overwrite=str(cfg_path), credential_store=store)
    reloaded = load_config(str(cfg_path), credential_store=store)
    assert list_source_profiles(reloaded, "storytel") == ["family"]
    assert get_source_config(reloaded, "storytel") is None
    reloaded_family = get_source_config(reloaded, "storytel", "family")
    assert reloaded_family is not None
    assert reloaded_family.username == "family@example.com"

    # Legacy plaintext password inside [sources.<svc>.profiles.<name>] migrates cleanly
    cfg_path.write_text(
        '[sources.ereolen.profiles.card2]\n'
        'username = "reader2"\n'
        'password = "legacy-profile-pw"\n'
        'library = "aalborg"\n',
        encoding="utf-8",
    )
    migrated = load_config(str(cfg_path))
    prof_cfg = get_source_config(migrated, "ereolen", "card2")
    assert prof_cfg is not None
    assert prof_cfg.username == "reader2"
    assert prof_cfg.password == "legacy-profile-pw"
    assert prof_cfg.library == "aalborg"
    assert "legacy-profile-pw" not in cfg_path.read_text(encoding="utf-8")


def test_env_config_var_overrides_config_dir_and_location(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("AUDIOBOOK_DL_CONFIG", raising=False)
    monkeypatch.delenv("AUDIOBOOKDL_CONFIG", raising=False)

    custom_dir = tmp_path / "docker-config"
    custom_cfg = custom_dir / "audiobook-dl.toml"
    monkeypatch.setenv("AUDIOBOOK_DL_CONFIG", str(custom_cfg))
    assert config_dir() == str(custom_dir)
    assert get_config_location(None) == str(custom_cfg)
    assert options_schema.default_database_directory() == os.path.join(str(custom_dir), "db")

    # Also supports legacy AUDIOBOOKDL_CONFIG spelling
    monkeypatch.delenv("AUDIOBOOK_DL_CONFIG", raising=False)
    legacy_cfg = tmp_path / "legacy-config" / "custom.toml"
    monkeypatch.setenv("AUDIOBOOKDL_CONFIG", str(legacy_cfg))
    assert config_dir() == str(legacy_cfg.parent)
    assert get_config_location(None) == str(legacy_cfg)


def test_path_locks_are_weakly_referenced_and_collected(tmp_path):
    import gc
    from audiobookdl.utils.fs import _PATH_LOCKS, file_lock

    target = str(tmp_path / "locked.toml")
    with file_lock(target):
        assert os.path.abspath(target) in _PATH_LOCKS
    gc.collect()
    assert os.path.abspath(target) not in _PATH_LOCKS


def test_toml_save_preserves_comments_inline_notes_and_custom_sections(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        "# Primary configuration\n"
        'output_template = "{title}" # keep inline note\n'
        "\n"
        "# Custom metadata notes\n"
        "[custom_tools]\n"
        'my_tool = "kept"\n',
        encoding="utf-8",
    )
    save_output_preferences("m4b", True, config_location=str(cfg_path))
    updated = cfg_path.read_text(encoding="utf-8")
    assert "# Primary configuration" in updated
    assert "# keep inline note" in updated
    assert "# Custom metadata notes" in updated
    assert "[custom_tools]" in updated
    assert 'my_tool = "kept"' in updated
    assert 'output_format = "m4b"' in updated
    assert "combine = true" in updated


def test_atomic_write_text_private_succeeds(tmp_path):
    from audiobookdl.utils.fs import atomic_write_text

    target = tmp_path / "secret.txt"
    atomic_write_text(str(target), "secret-token\n", private=True)
    assert target.read_text(encoding="utf-8") == "secret-token\n"


def test_file_lock_allows_reentrant_nesting_in_same_thread(tmp_path):
    from audiobookdl.utils.fs import file_lock

    target = tmp_path / "reentrant.toml"
    with file_lock(str(target), timeout=0.5):
        with file_lock(str(target), timeout=0.5):
            target.write_text("ok", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "ok"


def test_env_config_overrides_portable_mode_in_config_dir(tmp_path, monkeypatch):
    import sys

    exe_dir = tmp_path / "portable"
    exe_dir.mkdir()
    (exe_dir / ".portable").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "audiobook-dl.exe"))
    assert is_portable_mode() is True

    env_cfg = tmp_path / "override" / "custom.toml"
    monkeypatch.setenv("AUDIOBOOK_DL_CONFIG", str(env_cfg))
    assert config_dir() == str(env_cfg.parent)
    assert get_config_location(None) == str(env_cfg)


def test_debug_and_cover_keys_merge_from_config(tmp_path):
    from audiobookdl.config import structure_config
    from audiobookdl.options_schema import build_default_options
    from audiobookdl.pipeline import apply_runtime_options

    cfg = structure_config(
        str(tmp_path / "audiobook-dl.toml"),
        {"debug": True, "cover": True},
        credential_store=None,
    )
    opts = build_default_options()
    apply_runtime_options(opts, cfg)
    assert opts.debug is True
    assert opts.cover is True


def test_gui_clipboard_sync_schedule_and_check_updates_round_trip(tmp_path):
    from audiobookdl import logging
    from audiobookdl.config import save_check_updates

    cfg_path = tmp_path / "audiobook-dl.toml"
    save_gui_preferences(
        overwrite=str(cfg_path),
        clipboard_monitor=True,
        auto_sync_enabled=True,
        auto_sync_minutes=45,
        auto_sync_mode="scan_only",
        auto_sync_targets=["storytel", "everand:work"],
    )
    save_check_updates(False, overwrite=str(cfg_path))

    loaded = load_config(str(cfg_path))
    assert loaded.check_updates is False
    assert loaded.gui.clipboard_monitor is True
    assert loaded.gui.auto_sync_enabled is True
    assert loaded.gui.auto_sync_minutes == 45
    assert loaded.gui.auto_sync_mode == "scan_only"
    assert loaded.gui.auto_sync_targets == ["storytel", "everand:work"]

    # Empty targets list round-trips cleanly as []
    save_gui_preferences(overwrite=str(cfg_path), auto_sync_targets=[])
    loaded_empty = load_config(str(cfg_path))
    assert loaded_empty.gui.auto_sync_targets == []

    # Invalid values emit warnings and fall back to None
    cfg_path.write_text(
        "[gui]\n"
        'clipboard_monitor = "yes"\n'
        "auto_sync_minutes = 0\n"
        'auto_sync_mode = "bogus"\n'
        'auto_sync_targets = "not-a-list"\n',
        encoding="utf-8",
    )
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        invalid = load_config(str(cfg_path))
    assert invalid.gui.clipboard_monitor is None
    assert invalid.gui.auto_sync_minutes is None
    assert invalid.gui.auto_sync_mode is None
    assert invalid.gui.auto_sync_targets is None
    joined = "\n".join(sink.messages)
    assert "clipboard_monitor" in joined
    assert "auto_sync_minutes" in joined
    assert "auto_sync_mode" in joined
    assert "auto_sync_targets" in joined


def test_resolve_effective_source_profile(tmp_path):
    from audiobookdl.config import resolve_effective_source_profile, structure_config

    cfg = structure_config(
        str(tmp_path / "audiobook-dl.toml"),
        {
            "sources": {
                "everand": {"profiles": {"work": {"username": "bob"}}},
                "storytel": {
                    "username": "alice",
                    "profiles": {"kids": {"username": "kid"}},
                },
                "nextory": {
                    "profiles": {
                        "a": {"username": "u1"},
                        "b": {"username": "u2"},
                    }
                },
            }
        },
        credential_store=None,
    )
    # Sole named profile auto-selected when default is absent
    assert resolve_effective_source_profile(cfg, "everand") == "work"
    # Default takes precedence when present
    assert resolve_effective_source_profile(cfg, "storytel") == "default"
    # Explicit profile always wins
    assert resolve_effective_source_profile(cfg, "storytel", "kids") == "kids"
    # Multiple named profiles without default falls back to default
    assert resolve_effective_source_profile(cfg, "nextory") == "default"


def test_custom_metadata_providers_toml_migration_save_and_delete(tmp_path):
    from audiobookdl.config import (
        delete_custom_metadata_provider,
        save_custom_metadata_provider,
    )
    from audiobookdl.credentials import CredentialStore

    class _MemSecureStore(CredentialStore):
        is_secure = True

        def __init__(self) -> None:
            self.data: dict[tuple[str, str | None], str] = {}

        def get_password(self, service_key: str, username: str | None) -> str | None:
            return self.data.get((service_key, username))

        def set_password(self, service_key: str, username: str | None, password: str) -> None:
            self.data[(service_key, username)] = password

        def delete_password(self, service_key: str, username: str | None) -> None:
            self.data.pop((service_key, username), None)

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        "# Keep top comment\n"
        'enrich_providers = "hardcover,audnexus"\n\n'
        "[enrichment.custom.hardcover]\n"
        'url = "https://abs-hardcover.example.com/"\n'
        'api_key = "hc-plaintext-secret"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    store = _MemSecureStore()
    loaded = load_config(str(cfg_path), credential_store=store)
    assert loaded.enrich_providers == "custom:hardcover,audnexus"
    assert "hardcover" in loaded.custom_metadata_providers
    prov = loaded.custom_metadata_providers["hardcover"]
    assert prov.url == "https://abs-hardcover.example.com"
    assert prov.api_key == "hc-plaintext-secret"
    assert prov.enabled is True
    toml_after_load = cfg_path.read_text(encoding="utf-8")
    assert "hc-plaintext-secret" not in toml_after_load
    assert "# Keep top comment" in toml_after_load
    assert (
        store.get_password("metadata_provider:hardcover", "https://abs-hardcover.example.com")
        == "hc-plaintext-secret"
    )

    save_custom_metadata_provider(
        "hardcover",
        url="https://hardcover-v2.example.com/",
        enabled=False,
        overwrite=str(cfg_path),
        credential_store=store,
    )
    reloaded = load_config(str(cfg_path), credential_store=store)
    assert reloaded.custom_metadata_providers["hardcover"].url == "https://hardcover-v2.example.com"
    assert reloaded.custom_metadata_providers["hardcover"].enabled is False
    assert reloaded.custom_metadata_providers["hardcover"].api_key == "hc-plaintext-secret"

    delete_custom_metadata_provider("hardcover", overwrite=str(cfg_path), credential_store=store)
    after_delete = load_config(str(cfg_path), credential_store=store)
    assert after_delete.custom_metadata_providers == {}
    assert store.data == {}





"""Schema completeness and parity tests for `audiobookdl.options_schema`."""

from dataclasses import fields as dataclass_fields

from attrs import fields as attrs_fields
import pytest

from audiobookdl.config import (
    Config,
    CustomMetadataProviderConfig,
    GuiConfig,
    SourceConfig,
    save_output_preferences,
    structure_config,
)
from audiobookdl.gui.options_binding import (
    GUI_OPTION_BINDINGS,
    GUI_OPTION_SPECS,
    PERSISTED_GUI_OPTION_SPECS,
)
from audiobookdl.options_schema import (
    CONFIG_OPTION_SPECS,
    DOWNLOAD_OPTION_SPECS,
    GUI_CONFIG_KEYS,
    KNOWN_TOP_LEVEL_CONFIG_KEYS,
    MERGEABLE_OPTION_SPECS,
    OPTIONS_BY_NAME,
    OPTION_REGISTRY,
    SOURCE_CONFIG_KEYS,
    DownloadOptions,
    build_default_options,
    validate_config_value,
)
from audiobookdl.pipeline import resolve_runtime_options


def test_registry_names_are_unique():
    names = [spec.name for spec in OPTION_REGISTRY]
    assert len(names) == len(set(names))
    assert set(OPTIONS_BY_NAME.keys()) == set(names)


def test_download_options_parity_with_schema():
    dl_fields = {
        f.name for f in dataclass_fields(DownloadOptions) if not f.name.startswith("_")
    }
    schema_names = {spec.name for spec in DOWNLOAD_OPTION_SPECS}
    assert schema_names == dl_fields

    defaults = build_default_options()
    for spec in DOWNLOAD_OPTION_SPECS:
        expected = spec.resolve_default()
        assert getattr(defaults, spec.name) == expected, f"default mismatch for {spec.name}"


def test_config_fields_parity_with_schema():
    cfg_fields = {
        f.name
        for f in attrs_fields(Config)
        if f.name not in ("sources", "gui", "audiobookshelf", "custom_metadata_providers", "config_location")
    }
    schema_config_keys = {spec.config_key for spec in CONFIG_OPTION_SPECS if spec.config_key}
    assert schema_config_keys == cfg_fields
    assert KNOWN_TOP_LEVEL_CONFIG_KEYS == (
        {"sources", "gui", "audiobookshelf", "enrichment"} | schema_config_keys
    )

    assert {f.name for f in attrs_fields(GuiConfig)} == set(GUI_CONFIG_KEYS)
    assert {
        f.name for f in attrs_fields(SourceConfig) if f.name != "oauth_token_present"
    } == set(SOURCE_CONFIG_KEYS)


def test_config_clone_copies_all_fields_independently():
    original = Config(
        sources={
            "storytel": SourceConfig(
                username="u@example.com",
                password="secret",
                library="lib1",
                cookie_file="/tmp/cookies.txt",
            )
        },
        output_template="{author}/{title}",
        database_directory="/tmp/db",
        skip_downloaded=True,
        output_format="m4b",
        media_type="audiobook",
        combine=True,
        ffmpeg_dir="/usr/local/bin",
        resume=True,
        audio_bitrate="128k",
        sample_rate=22050,
        overwrite=False,
        max_workers=4,
        book_delay=1.5,
        proxy="http://127.0.0.1:8080",
        timeout=30.0,
        download_archive="/tmp/archive.sqlite",
        check_updates=True,
        gui=GuiConfig(window_geometry="900x700", output_dir="/tmp/out", output_template="{title}"),
        custom_metadata_providers={
            "hardcover": CustomMetadataProviderConfig(
                name="hardcover",
                url="https://hardcover.example.com",
                api_key="hc-secret",
                enabled=True,
            )
        },
    )

    cloned = original.clone()
    assert cloned is not original
    assert cloned.sources is not original.sources
    assert cloned.sources["storytel"] is not original.sources["storytel"]
    assert cloned.gui is not original.gui
    assert cloned.custom_metadata_providers is not original.custom_metadata_providers
    assert cloned.custom_metadata_providers["hardcover"] is not original.custom_metadata_providers["hardcover"]
    assert cloned.custom_metadata_providers["hardcover"].url == "https://hardcover.example.com"

    for spec in CONFIG_OPTION_SPECS:
        key = spec.config_key
        assert key is not None
        assert getattr(cloned, key) == getattr(original, key)

    cloned.sources["storytel"].username = "other"
    cloned.gui.output_dir = "/tmp/other"
    cloned.check_updates = False
    assert original.sources["storytel"].username == "u@example.com"
    assert original.gui.output_dir == "/tmp/out"
    assert original.check_updates is True


def test_persisted_gui_options_match_save_output_preferences(tmp_path):
    cfg_path = tmp_path / "prefs.toml"
    all_persisted = {}
    for spec in PERSISTED_GUI_OPTION_SPECS:
        assert spec.toml_serializer is not None
        assert spec.config_key is not None
        binding = GUI_OPTION_BINDINGS[spec.name]
        all_persisted[spec.name] = binding.read_option(binding.init_value(None))
    save_output_preferences(config_location=str(cfg_path), **all_persisted)
    assert cfg_path.is_file()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        save_output_preferences(config_location=str(cfg_path), unknown_pref_key=True)


def test_gui_option_bindings_roundtrip():
    from audiobookdl.gui.app import AudiobookDLApp

    assert len(GUI_OPTION_SPECS) >= 10
    annotations = AudiobookDLApp.__annotations__
    for spec in GUI_OPTION_SPECS:
        binding = GUI_OPTION_BINDINGS[spec.name]
        assert binding.var_attr in annotations, f"Missing type annotation for {binding.var_attr}"
        val = binding.init_value(None)
        option_val = binding.read_option(val)
        if binding.var_type == "bool":
            assert isinstance(val, bool)
            assert isinstance(option_val, bool)


def test_validate_config_value_variants():
    assert validate_config_value("timeout", "positive_float", 12) == (12.0, None)
    val, warn = validate_config_value("timeout", "positive_float", 0)
    assert val is None and warn is not None

    assert validate_config_value("book_delay", "non_negative_float", 0) == (0.0, None)
    val, warn = validate_config_value("book_delay", "non_negative_float", -1)
    assert val is None and warn is not None

    assert validate_config_value("max_workers", "positive_int", 3) == (3, None)
    val, warn = validate_config_value("max_workers", "positive_int", True)
    assert val is None and warn is not None

    assert validate_config_value("max_retries", "non_negative_int", 0) == (0, None)
    assert validate_config_value("max_retries", "non_negative_int", 6) == (6, None)
    val, warn = validate_config_value("max_retries", "non_negative_int", -1)
    assert val is None and warn is not None
    val, warn = validate_config_value("max_retries", "non_negative_int", True)
    assert val is None and warn is not None

    assert validate_config_value("media_type", "media_type", " EBOOK ") == ("ebook", None)
    val, warn = validate_config_value("media_type", "media_type", "vinyl")
    assert val is None and warn is not None

    assert validate_config_value("intro_chapter_mode", "intro_chapter_mode", " ALWAYS ") == ("always", None)
    assert validate_config_value("intro_chapter_mode", "intro_chapter_mode", "off") == ("off", None)
    val, warn = validate_config_value("intro_chapter_mode", "intro_chapter_mode", "invalid")
    assert val is None and warn is not None

    assert validate_config_value("sample_rate", "sample_rate", 44100) == (44100, None)
    assert validate_config_value("sample_rate", "sample_rate", "44.1kHz") == (44100, None)
    assert validate_config_value("sample_rate", "sample_rate", "auto") == (None, None)
    val, warn = validate_config_value("sample_rate", "sample_rate", True)
    assert val is None and warn is not None
    val, warn = validate_config_value("sample_rate", "sample_rate", 1000)
    assert val is None and warn is not None

    with pytest.raises(ValueError, match="Unknown config value kind"):
        validate_config_value("foo", "unknown_kind", "bar")


def test_schema_driven_merge_and_preset_precedence(tmp_path):
    cfg = structure_config(
        str(tmp_path / "audiobook-dl.toml"),
        {
            "output_template": "{title} - cfg",
            "combine": True,
            "skip_downloaded": True,
            "skip_unavailable": True,
            "resume": True,
            "output_format": "mp3",
            "media_type": "ebook",
            "audio_bitrate": "96k",
            "sample_rate": 22050,
            "overwrite": True,
            "max_workers": 3,
            "book_delay": 2,
            "limit_rate": "2M",
            "proxy": "http://127.0.0.1:8888",
            "timeout": 45,
            "max_retries": 7,
            "post_exec": "echo done",
            "loudnorm": True,
            "mono": True,
            "chapters_from_silence": True,
            "override_chapters": True,
            "chapter_offset_ms": -250,
            "intro_chapter_mode": "always",
            "square_cover": True,
            "max_cover_size": 1000,
            "download_archive": str(tmp_path / "arch.sqlite"),
            "database_directory": str(tmp_path / "db"),
            "remove_chars": ":?",
            "debug": True,
            "cover": True,
            "no_chapters": True,
            "split_chapters": True,
            "write_json_metadata": True,
            "write_cover": True,
            "write_opf": True,
            "write_abs_metadata": True,
            "upload_to_abs": True,
            "write_cue": True,
            "enrich_metadata": True,
            "enrich_providers": "itunes,google",
            "enrich_interactive": True,
            "silence_noise_db": -38.0,
            "silence_min_seconds": 2.2,
        },
        credential_store=None,
    )
    opts = build_default_options()
    resolved = resolve_runtime_options(opts, cfg)
    for spec in MERGEABLE_OPTION_SPECS:
        key = spec.config_key
        assert key is not None
        raw_val = getattr(cfg, key)
        expected = spec.merge_cast(raw_val) if spec.merge_cast else raw_val
        assert getattr(resolved, spec.name) == expected

    override_opts = build_default_options(output_template="{author}/{title}", combine=False)
    resolved_override = resolve_runtime_options(override_opts, cfg)
    assert resolved_override.combine is False
    assert resolved_override.output_template == "{author}/{title}"


def test_gui_tooltip_overrides_for_cli_jargon_options():
    from audiobookdl.gui.options_panel import _option_help

    for name in ("post_exec", "download_archive", "remove_chars"):
        binding = GUI_OPTION_BINDINGS[name]
        assert binding.tooltip
        gui_tip = _option_help(name)
        assert gui_tip == binding.tooltip
        assert gui_tip != OPTIONS_BY_NAME[name].get_help()

    assert _option_help("output_format") == OPTIONS_BY_NAME["output_format"].get_help()


def test_gui_option_bindings_converters():
    overwrite_binding = GUI_OPTION_BINDINGS["overwrite"]
    assert overwrite_binding.init_value(None) == "Ask"
    assert overwrite_binding.init_value(True) == "Overwrite"
    assert overwrite_binding.init_value(False) == "Keep existing"
    assert overwrite_binding.read_option("Ask") is None
    assert overwrite_binding.read_option("Overwrite") is True
    assert overwrite_binding.read_option("Keep existing") is False

    workers_binding = GUI_OPTION_BINDINGS["max_workers"]
    assert workers_binding.init_value(None) == ""
    assert workers_binding.init_value(8) == "8"
    assert workers_binding.read_option("") is None
    assert workers_binding.read_option("6") == 6
    with pytest.raises(ValueError, match="Max workers"):
        workers_binding.read_option("0")
    with pytest.raises(ValueError, match="Max workers"):
        workers_binding.read_option("abc")

    delay_binding = GUI_OPTION_BINDINGS["book_delay"]
    assert delay_binding.init_value(None) == "0"
    assert delay_binding.init_value(2.0) == "2"
    assert delay_binding.init_value(1.5) == "1.5"
    assert delay_binding.read_option("") == 0.0
    assert delay_binding.read_option("1.5") == 1.5
    with pytest.raises(ValueError, match="Book delay"):
        delay_binding.read_option("-1")

    timeout_binding = GUI_OPTION_BINDINGS["timeout"]
    assert timeout_binding.init_value(None) == ""
    assert timeout_binding.init_value(30.0) == "30"
    assert timeout_binding.read_option("") is None
    assert timeout_binding.read_option("45.5") == 45.5
    with pytest.raises(ValueError, match="Timeout"):
        timeout_binding.read_option("0")

    proxy_binding = GUI_OPTION_BINDINGS["proxy"]
    assert proxy_binding.init_value("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert proxy_binding.read_option("   ") is None
    assert proxy_binding.read_option(" http://proxy:8080 ") == "http://proxy:8080"

    remove_chars_binding = GUI_OPTION_BINDINGS["remove_chars"]
    assert remove_chars_binding.read_option(" :") == " :"

    archive_binding = GUI_OPTION_BINDINGS["download_archive"]
    assert archive_binding.init_value(None) is False
    assert archive_binding.init_value("/tmp/downloads.db") is True

    impersonate_binding = GUI_OPTION_BINDINGS["impersonate"]
    assert impersonate_binding.init_value(None) == ""
    assert impersonate_binding.init_value("chrome") == "chrome"
    assert impersonate_binding.read_option("  chrome  ") == "chrome"
    assert impersonate_binding.read_option("   ") is None


def test_persisted_gui_roundtrip_silence_offset_and_abs(tmp_path):
    from audiobookdl.config import load_config

    cfg_path = tmp_path / "persisted.toml"
    save_output_preferences(
        config_location=str(cfg_path),
        chapters_from_silence=True,
        override_chapters=True,
        chapter_offset_ms=-350,
        intro_chapter_mode="always",
        upload_to_abs=True,
    )
    loaded = load_config(str(cfg_path))
    assert loaded.chapters_from_silence is True
    assert loaded.override_chapters is True
    assert loaded.chapter_offset_ms == -350
    assert loaded.intro_chapter_mode == "always"
    assert loaded.upload_to_abs is True
    assert validate_config_value("chapter_offset_ms", "int", True)[0] is None

    intro_binding = GUI_OPTION_BINDINGS["intro_chapter_mode"]
    assert intro_binding.init_value(None) == "Auto (detect intro)"
    assert intro_binding.init_value("always") == "Always (first is 00)"
    assert intro_binding.init_value("off") == "Off (start at 01)"
    assert intro_binding.read_option("Auto (detect intro)") == "auto"
    assert intro_binding.read_option("Always (first is 00)") == "always"
    assert intro_binding.read_option("Off (start at 01)") == "off"
    assert validate_config_value("silence_noise_db", "silence_noise_db", -42.5) == (-42.5, None)
    assert validate_config_value("silence_noise_db", "silence_noise_db", 5.0)[0] is None
    assert validate_config_value("silence_min_seconds", "positive_float", 2.5) == (2.5, None)
    assert validate_config_value("silence_min_seconds", "positive_float", 0.0)[0] is None

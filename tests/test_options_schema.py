"""Schema completeness and parity tests for `audiobookdl.options_schema`."""

from dataclasses import fields as dataclass_fields
import inspect

from attrs import fields as attrs_fields
import pytest

from audiobookdl import args
from audiobookdl.config import Config, GuiConfig, SourceConfig, save_output_preferences, structure_config
from audiobookdl.options_schema import (
    CONFIG_OPTION_SPECS,
    DOWNLOAD_OPTION_SPECS,
    GUI_CONFIG_KEYS,
    GUI_OPTION_SPECS,
    KNOWN_TOP_LEVEL_CONFIG_KEYS,
    MERGEABLE_OPTION_SPECS,
    OPTIONS_BY_NAME,
    OPTION_REGISTRY,
    PERSISTED_GUI_OPTION_SPECS,
    SOURCE_CONFIG_KEYS,
    validate_config_value,
)
from audiobookdl.pipeline import apply_runtime_options


def test_registry_names_are_unique():
    names = [spec.name for spec in OPTION_REGISTRY]
    assert len(names) == len(set(names))
    assert set(OPTIONS_BY_NAME.keys()) == set(names)


def test_download_options_parity_with_schema():
    dl_fields = {
        f.name for f in dataclass_fields(args.DownloadOptions) if not f.name.startswith("_")
    }
    schema_names = {spec.name for spec in DOWNLOAD_OPTION_SPECS}
    assert schema_names == dl_fields

    defaults = args.build_default_options()
    parsed_defaults = args.parse_arguments([])
    for spec in DOWNLOAD_OPTION_SPECS:
        expected = spec.resolve_default()
        assert getattr(defaults, spec.name) == expected, f"default mismatch for {spec.name}"
        assert getattr(parsed_defaults, spec.name) == expected, f"CLI default mismatch for {spec.name}"


def test_config_fields_parity_with_schema():
    cfg_fields = {
        f.name for f in attrs_fields(Config) if f.name not in ("sources", "gui", "audiobookshelf")
    }
    schema_config_keys = {spec.config_key for spec in CONFIG_OPTION_SPECS if spec.config_key}
    assert schema_config_keys == cfg_fields
    assert KNOWN_TOP_LEVEL_CONFIG_KEYS == ({"sources", "gui", "audiobookshelf"} | schema_config_keys)

    assert {f.name for f in attrs_fields(GuiConfig)} == set(GUI_CONFIG_KEYS)
    assert {f.name for f in attrs_fields(SourceConfig)} == set(SOURCE_CONFIG_KEYS)


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
        overwrite=False,
        max_workers=4,
        book_delay=1.5,
        proxy="http://127.0.0.1:8080",
        timeout=30.0,
        download_archive="/tmp/archive.sqlite",
        check_updates=True,
        gui=GuiConfig(window_geometry="900x700", output_dir="/tmp/out", output_template="{title}"),
    )

    cloned = original.clone()
    assert cloned is not original
    assert cloned.sources is not original.sources
    assert cloned.sources["storytel"] is not original.sources["storytel"]
    assert cloned.gui is not original.gui

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


def test_persisted_gui_options_match_save_output_preferences():
    sig_params = set(inspect.signature(save_output_preferences).parameters.keys())
    for spec in PERSISTED_GUI_OPTION_SPECS:
        assert spec.toml_serializer is not None
        assert spec.config_key is not None
        assert spec.name in sig_params


def test_gui_option_bindings_roundtrip():
    from audiobookdl.gui.app import AudiobookDLApp

    assert len(GUI_OPTION_SPECS) >= 10
    annotations = AudiobookDLApp.__annotations__
    for spec in GUI_OPTION_SPECS:
        binding = spec.gui
        assert binding is not None
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

    assert validate_config_value("media_type", "media_type", " EBOOK ") == ("ebook", None)
    val, warn = validate_config_value("media_type", "media_type", "vinyl")
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
            "resume": True,
            "output_format": "mp3",
            "media_type": "ebook",
            "audio_bitrate": "96k",
            "overwrite": True,
            "max_workers": 3,
            "book_delay": 2,
            "limit_rate": "2M",
            "proxy": "http://127.0.0.1:8888",
            "timeout": 45,
            "post_exec": "echo done",
            "loudnorm": True,
            "mono": True,
            "square_cover": True,
            "max_cover_size": 1000,
            "download_archive": str(tmp_path / "arch.sqlite"),
            "database_directory": str(tmp_path / "db"),
            "remove_chars": ":?",
            "no_chapters": True,
            "split_chapters": True,
            "write_json_metadata": True,
            "write_cover": True,
            "write_opf": True,
            "write_abs_metadata": True,
            "write_cue": True,
            "enrich_metadata": True,
            "enrich_providers": "itunes,google",
            "enrich_interactive": True,
        },
        credential_store=None,
    )
    opts = args.parse_arguments([])
    apply_runtime_options(opts, cfg)
    for spec in MERGEABLE_OPTION_SPECS:
        key = spec.config_key
        assert key is not None
        raw_val = getattr(cfg, key)
        expected = spec.merge_cast(raw_val) if spec.merge_cast else raw_val
        assert getattr(opts, spec.name) == expected

    preset_opts = args.parse_arguments(["--output-preset", "plex", "--no-combine"])
    apply_runtime_options(preset_opts, cfg)
    assert preset_opts.combine is False
    assert preset_opts.output_template != "{title} - cfg"


def test_gui_cli_parity_bindings_converters():
    overwrite_binding = OPTIONS_BY_NAME["overwrite"].gui
    assert overwrite_binding is not None
    assert overwrite_binding.init_value(None) == "Ask"
    assert overwrite_binding.init_value(True) == "Overwrite"
    assert overwrite_binding.init_value(False) == "Keep existing"
    assert overwrite_binding.read_option("Ask") is None
    assert overwrite_binding.read_option("Overwrite") is True
    assert overwrite_binding.read_option("Keep existing") is False

    workers_binding = OPTIONS_BY_NAME["max_workers"].gui
    assert workers_binding is not None
    assert workers_binding.init_value(None) == ""
    assert workers_binding.init_value(8) == "8"
    assert workers_binding.read_option("") is None
    assert workers_binding.read_option("6") == 6
    with pytest.raises(ValueError, match="Max workers"):
        workers_binding.read_option("0")
    with pytest.raises(ValueError, match="Max workers"):
        workers_binding.read_option("abc")

    delay_binding = OPTIONS_BY_NAME["book_delay"].gui
    assert delay_binding is not None
    assert delay_binding.init_value(None) == "0"
    assert delay_binding.init_value(2.0) == "2"
    assert delay_binding.init_value(1.5) == "1.5"
    assert delay_binding.read_option("") == 0.0
    assert delay_binding.read_option("1.5") == 1.5
    with pytest.raises(ValueError, match="Book delay"):
        delay_binding.read_option("-1")

    timeout_binding = OPTIONS_BY_NAME["timeout"].gui
    assert timeout_binding is not None
    assert timeout_binding.init_value(None) == ""
    assert timeout_binding.init_value(30.0) == "30"
    assert timeout_binding.read_option("") is None
    assert timeout_binding.read_option("45.5") == 45.5
    with pytest.raises(ValueError, match="Timeout"):
        timeout_binding.read_option("0")

    proxy_binding = OPTIONS_BY_NAME["proxy"].gui
    assert proxy_binding is not None
    assert proxy_binding.init_value("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert proxy_binding.read_option("   ") is None
    assert proxy_binding.read_option(" http://proxy:8080 ") == "http://proxy:8080"

    remove_chars_binding = OPTIONS_BY_NAME["remove_chars"].gui
    assert remove_chars_binding is not None
    assert remove_chars_binding.read_option(" :") == " :"

    archive_binding = OPTIONS_BY_NAME["download_archive"].gui
    assert archive_binding is not None
    assert archive_binding.init_value(None) is False
    assert archive_binding.init_value("/tmp/downloads.db") is True

    impersonate_binding = OPTIONS_BY_NAME["impersonate"].gui
    assert impersonate_binding is not None
    assert impersonate_binding.init_value(None) == ""
    assert impersonate_binding.init_value("chrome") == "chrome"
    assert impersonate_binding.read_option("  chrome  ") == "chrome"
    assert impersonate_binding.read_option("   ") is None

    parsed = args.parse_arguments(["--impersonate", "chrome", "https://example.com/book"])
    assert parsed.impersonate == "chrome"

from dataclasses import fields as dataclass_fields

import audiobookdl.options_schema as options_schema
from audiobookdl.options_schema import (
    OPTIONS_BY_NAME,
    DownloadOptions,
    SyncOptions,
    build_default_options,
)


def test_build_default_options_tracks_explicit_fields():
    defaults = build_default_options()
    assert isinstance(defaults, DownloadOptions)
    assert defaults._explicit_fields == frozenset()
    assert defaults.media_type == "auto"

    opts = build_default_options(
        combine=True,
        output_format="m4b",
        audio_bitrate="192k",
    )
    assert opts.combine is True
    assert opts.output_format == "m4b"
    assert opts.audio_bitrate == "192k"
    assert opts._explicit_fields == frozenset({"combine", "output_format", "audio_bitrate"})


def test_pruned_cli_only_fields_absent_from_options_schema():
    dl_field_names = {f.name for f in dataclass_fields(DownloadOptions)}
    for removed in (
        "urls",
        "output_preset",
        "quiet",
        "no_download_archive",
        "series_items",
        "service",
        "no_enrich_cache",
    ):
        assert removed not in dl_field_names
        assert removed not in OPTIONS_BY_NAME

    sync_field_names = {f.name for f in dataclass_fields(SyncOptions)}
    assert "watch" not in sync_field_names
    assert "interval_seconds" not in sync_field_names
    assert not hasattr(options_schema, "OUTPUT_PRESET_CHOICES")
    assert not hasattr(options_schema, "parse_series_selection")


def test_sync_options_defaults():
    sync_opts = SyncOptions()
    assert sync_opts.services == []
    assert sync_opts.profile is None
    assert sync_opts.scan_only is False
    assert sync_opts.dry_run is False

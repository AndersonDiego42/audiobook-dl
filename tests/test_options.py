import pytest

from audiobookdl.options_schema import (
    DownloadOptions,
    SyncOptions,
    build_default_options,
    parse_series_selection,
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
        urls=["https://example.com/book"],
    )
    assert opts.combine is True
    assert opts.output_format == "m4b"
    assert opts.audio_bitrate == "192k"
    assert opts.urls == ["https://example.com/book"]
    assert opts._explicit_fields == frozenset({"combine", "output_format", "audio_bitrate", "urls"})


def test_parse_series_selection_valid_and_invalid():
    assert parse_series_selection("1, 3-5, 2, 4", 6) == [0, 2, 3, 4, 1]
    with pytest.raises(ValueError):
        parse_series_selection("0", 5)
    with pytest.raises(ValueError):
        parse_series_selection("2-8", 5)
    with pytest.raises(ValueError):
        parse_series_selection("abc", 5)
    with pytest.raises(ValueError):
        parse_series_selection("", 5)


def test_sync_options_defaults():
    sync_opts = SyncOptions()
    assert sync_opts.services == []
    assert sync_opts.profile is None
    assert sync_opts.scan_only is False
    assert sync_opts.dry_run is False
    assert sync_opts.interval_seconds == 3600

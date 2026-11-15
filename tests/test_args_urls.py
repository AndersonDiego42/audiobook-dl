import pytest

from audiobookdl import args


def test_missing_input_file_exits_cleanly():
    """A missing --input-file must exit with a message, not a traceback."""
    options = args.build_default_options(urls=[], input_file="does-not-exist-12345.txt")
    with pytest.raises(SystemExit) as exc_info:
        args.get_urls(options)
    assert exc_info.value.code != 0
    assert "does-not-exist-12345.txt" in str(exc_info.value.code)


def test_input_file_urls_are_combined(tmp_path):
    """Urls from args and a valid input file are combined."""
    input_file = tmp_path / "urls.txt"
    input_file.write_text("https://example.com/a\nhttps://example.com/b\n")
    options = args.build_default_options(
        urls=["https://example.com/zero"],
        input_file=str(input_file),
    )
    assert args.get_urls(options) == [
        "https://example.com/zero",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_parse_arguments_returns_download_options(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["audiobook-dl", "--combine", "-f", "m4b", "https://example.com/book"],
    )
    opts = args.parse_arguments()
    assert isinstance(opts, args.DownloadOptions)
    assert opts.combine is True
    assert opts.output_format == "m4b"
    assert opts.urls == ["https://example.com/book"]


def test_parse_arguments_resume_and_audio_bitrate():
    opts = args.parse_arguments([
        "--resume",
        "--audio-bitrate", "192K",
        "https://example.com/book",
    ])
    assert opts.resume is True
    assert opts.audio_bitrate == "192k"

    auto_opts = args.parse_arguments(["--m4b-bitrate", "auto"])
    assert auto_opts.audio_bitrate == "auto"

    with pytest.raises(SystemExit):
        args.parse_arguments(["--audio-bitrate", "invalid"])


def test_input_file_reads_utf8_with_or_without_bom(tmp_path):
    input_file = tmp_path / "utf8_urls.txt"
    input_file.write_bytes(
        "\ufeffhttps://example.com/日本語\nhttps://example.com/Книга\n".encode("utf-8")
    )
    options = args.build_default_options(urls=[], input_file=str(input_file))
    assert args.get_urls(options) == [
        "https://example.com/日本語",
        "https://example.com/Книга",
    ]


def test_parse_series_selection_and_new_cli_flags():
    assert args.parse_series_selection("1, 3-5, 2, 4", 6) == [0, 2, 3, 4, 1]
    with pytest.raises(ValueError):
        args.parse_series_selection("0", 5)
    with pytest.raises(ValueError):
        args.parse_series_selection("2-8", 5)
    with pytest.raises(ValueError):
        args.parse_series_selection("abc", 5)

    opts = args.parse_arguments([
        "--overwrite",
        "--list-sources",
        "--info",
        "--series-items", "1,3-4",
        "--max-workers", "4",
        "--rate-limit", "1.5",
        "--proxy", "http://127.0.0.1:8888",
        "--timeout", "45",
        "--no-combine",
        "--no-skip-downloaded",
        "--no-resume",
    ])
    assert opts.overwrite is True
    assert opts.list_sources is True
    assert opts.info_only is True
    assert opts.series_items == "1,3-4"
    assert opts.max_workers == 4
    assert opts.book_delay == 1.5
    assert opts.proxy == "http://127.0.0.1:8888"
    assert opts.timeout == 45.0
    assert opts.combine is False
    assert opts.skip_downloaded is False
    assert opts.resume is False
    assert "combine" in opts._explicit_fields
    assert "output_template" not in opts._explicit_fields


def test_parse_arguments_media_type_valid_and_invalid():
    default_opts = args.parse_arguments(["https://example.com/book"])
    assert default_opts.media_type == "auto"
    assert "media_type" not in default_opts._explicit_fields

    for choice in ("auto", "audiobook", "ebook", "both"):
        opts = args.parse_arguments(["--media-type", choice, "https://example.com/book"])
        assert opts.media_type == choice
        assert "media_type" in opts._explicit_fields

    with pytest.raises(SystemExit):
        args.parse_arguments(["--media-type", "podcast", "https://example.com/book"])


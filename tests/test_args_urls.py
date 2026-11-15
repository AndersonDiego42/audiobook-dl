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

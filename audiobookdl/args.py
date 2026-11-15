import argparse
from dataclasses import dataclass, field
import os
from typing import Any, List, Optional, Sequence

from audiobookdl import __version__
from audiobookdl.config import config_dir
from audiobookdl.utils import normalize_audio_bitrate


def default_database_directory() -> str:
    """Return the default directory for sources to store data."""
    return os.path.join(config_dir(), "db")


def _parse_audio_bitrate_arg(value: str) -> str:
    """Validate `--audio-bitrate` CLI argument, preserving explicit `'auto'` override."""
    if value.strip().lower() == "auto":
        return "auto"
    try:
        normalized = normalize_audio_bitrate(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return normalized or "auto"


@dataclass
class DownloadOptions:
    """Typed runtime options shared across CLI, GUI, pipeline, and sources."""

    urls: List[str] = field(default_factory=list)
    cookie_file: Optional[str] = None
    combine: bool = False
    output_template: str = "{title}"
    remove_chars: str = ""
    debug: bool = False
    quiet: bool = False
    print_output: bool = False
    cover: bool = False
    no_chapters: bool = False
    output_format: Optional[str] = None
    ffmpeg_output: bool = False
    input_file: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    library: Optional[str] = None
    skip_downloaded: bool = False
    database_directory: str = field(default_factory=default_database_directory)
    write_json_metadata: bool = False
    config_location: Optional[str] = None
    resume: bool = False
    audio_bitrate: Optional[str] = None


def build_default_options(**overrides: Any) -> DownloadOptions:
    """Build a `DownloadOptions` instance matching CLI defaults with optional overrides."""
    return DownloadOptions(**overrides)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> DownloadOptions:
    parser = argparse.ArgumentParser(
        prog="audiobook-dl",
        description="Download audiobooks from websites",
    )
    parser.add_argument(
        '-v',
        '--version',
        action = "version",
        version = f"audiobook-dl {__version__}"
    )
    parser.add_argument(
        'urls',
        help="Urls to download from",
        nargs='*',
    )
    parser.add_argument(
        '-c',
        '--cookies',
        dest='cookie_file',
        help="Path to Netscape cookie file",
    )
    parser.add_argument(
        '--combine',
        dest='combine',
        action='store_true',
        help="Combine output files into a single file",
    )
    parser.add_argument(
        '-o',
        '--output',
        dest="output_template",
        help="Output location",
        default="{title}"
    )
    parser.add_argument(
        '--remove-chars',
        dest="remove_chars",
        help = "List of characters that will be removed from output path",
        default="",
    )
    parser.add_argument(
        '--debug',
        '-d',
        dest="debug",
        help="Debug mode",
        action="store_true",
    )
    parser.add_argument(
        '--quiet',
        '-q',
        dest="quiet",
        help="Quiet mode",
        action="store_true",
    )
    parser.add_argument(
        '--print-output',
        dest="print_output",
        help="Prints the output locations instead of downloading",
        action='store_true',
    )
    parser.add_argument(
        '--cover',
        dest="cover",
        help="Download only cover",
        action='store_true',
    )
    parser.add_argument(
        '--no-chapters',
        dest="no_chapters",
        help="Don't include chapters in final file",
        action="store_true"
    )
    parser.add_argument(
        '-f',
        '--output-format',
        dest="output_format",
        help="Output file format",
    )
    parser.add_argument(
        '--verbose-ffmpeg',
        dest="ffmpeg_output",
        help="Show ffmpeg output in terminal",
        action="store_true",
    )
    parser.add_argument(
        '--input-file',
        dest="input_file",
        help="File with one url to download per line",
    )
    parser.add_argument(
        '--username',
        dest="username",
        help="Username for source",
    )
    parser.add_argument(
        '--password',
        dest="password",
        help="Password for source",
    )
    parser.add_argument(
        '--library',
        dest="library",
        help="Library for source",
    )
    parser.add_argument(
        '--skip-downloaded',
        dest="skip_downloaded",
        help="Skip downloading books if the output file or directory already exists",
        action="store_true",
    )
    parser.add_argument(
        '--database_directory',
        dest="database_directory",
        help="Directory for sources to store data (default: %(default)s)",
        default=default_database_directory()
    )
    parser.add_argument(
        '--write-json-metadata',
        dest = "write_json_metadata",
        help = "Write metadata in a separate json file",
        action="store_true",
    )
    parser.add_argument(
        '--config',
        dest = "config_location",
        help = "Alternative location of config file"
    )
    parser.add_argument(
        '--resume',
        dest="resume",
        help="Resume interrupted downloads using existing parts and HTTP Range requests",
        action="store_true",
    )
    parser.add_argument(
        '--audio-bitrate',
        '--m4b-bitrate',
        dest="audio_bitrate",
        type=_parse_audio_bitrate_arg,
        default=None,
        help="Target AAC bitrate when converting to .m4b/.m4a (auto, 64k, 128k, 192k, etc.)",
    )
    parsed = parser.parse_args(argv)
    return DownloadOptions(**vars(parsed))


def get_urls(options: DownloadOptions) -> List[str]:
    """
    Creates a list of all urls in cli options.
    Urls a found in `options.urls` and read from `options.input_file` if the
    file exists

    :param options: Cli options
    :returns: Combined list of all urls
    """
    urls = []
    # Args
    urls.extend(options.urls)
    # File
    if options.input_file:
        try:
            with open(options.input_file, "r") as f:
                urls.extend(f.read().split())
        except OSError as error:
            raise SystemExit(
                f"Error: cannot read input file '{options.input_file}': "
                f"{error.strerror or error}"
            )
    return urls

import argparse
from dataclasses import dataclass, field
import os
from typing import Any, FrozenSet, List, Optional, Sequence

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


def _parse_positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected integer, got '{value}'") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("Expected integer >= 1")
    return parsed


def _parse_positive_float_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected number, got '{value}'") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Expected number > 0")
    return parsed


def _parse_non_negative_float_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected number, got '{value}'") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Expected number >= 0")
    return parsed


def parse_series_selection(spec: str, total: int) -> List[int]:
    """
    Parse a 1-based comma/range selection string (e.g. `'1,3-5'`) into unique
    ordered 0-based indices bounded by `[0, total)`.
    """
    if total < 0:
        raise ValueError("total must be >= 0")
    cleaned = spec.strip()
    if not cleaned:
        raise ValueError("Series item selection cannot be empty")
    selected: List[int] = []
    seen = set()
    for token in cleaned.split(","):
        part = token.strip()
        if not part:
            raise ValueError(f"Empty token in selection '{spec}'")
        if "-" in part:
            bounds = part.split("-", 1)
            if not bounds[0].strip().isdigit() or not bounds[1].strip().isdigit():
                raise ValueError(f"Invalid range '{part}' in '{spec}'")
            start_1 = int(bounds[0].strip())
            end_1 = int(bounds[1].strip())
            if start_1 < 1 or end_1 < start_1 or end_1 > total:
                raise ValueError(f"Range '{part}' out of bounds for 1..{total}")
            for one_based in range(start_1, end_1 + 1):
                zero_based = one_based - 1
                if zero_based not in seen:
                    seen.add(zero_based)
                    selected.append(zero_based)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid index '{part}' in '{spec}'")
            one_based = int(part)
            if one_based < 1 or one_based > total:
                raise ValueError(f"Index '{part}' out of bounds for 1..{total}")
            zero_based = one_based - 1
            if zero_based not in seen:
                seen.add(zero_based)
                selected.append(zero_based)
    return selected


@dataclass
class DownloadOptions:
    """Typed runtime options shared across CLI, GUI, pipeline, and sources."""

    urls: List[str] = field(default_factory=list)
    cookie_file: Optional[str] = None
    combine: bool = False
    output_template: str = "{title}"
    output_preset: Optional[str] = None
    remove_chars: str = ""
    debug: bool = False
    quiet: bool = False
    print_output: bool = False
    cover: bool = False
    no_chapters: bool = False
    split_chapters: bool = False
    output_format: Optional[str] = None
    ffmpeg_output: bool = False
    input_file: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    library: Optional[str] = None
    skip_downloaded: bool = False
    download_archive: Optional[str] = None
    database_directory: str = field(default_factory=default_database_directory)
    write_json_metadata: bool = False
    write_cover: bool = False
    write_opf: bool = False
    write_cue: bool = False
    config_location: Optional[str] = None
    resume: bool = False
    audio_bitrate: Optional[str] = None
    overwrite: Optional[bool] = None
    list_sources: bool = False
    info_only: bool = False
    series_items: Optional[str] = None
    max_workers: Optional[int] = None
    book_delay: float = 0.0
    proxy: Optional[str] = None
    timeout: Optional[float] = None
    service: Optional[str] = None
    search_query: Optional[str] = None
    library_list: bool = False
    verify_auth: bool = False
    enrich_metadata: bool = False
    check_updates: bool = False
    _explicit_fields: FrozenSet[str] = field(
        default_factory=frozenset,
        init=False,
        repr=False,
        compare=False,
    )


def build_default_options(**overrides: Any) -> DownloadOptions:
    """Build a `DownloadOptions` instance matching CLI defaults with optional overrides."""
    opts = DownloadOptions(**overrides)
    opts._explicit_fields = frozenset(overrides.keys())
    return opts


def parse_arguments(argv: Optional[Sequence[str]] = None) -> DownloadOptions:
    parser = argparse.ArgumentParser(
        prog="audiobook-dl",
        description="Download audiobooks from websites",
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '-v',
        '--version',
        action="version",
        version=f"audiobook-dl {__version__}",
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
        '--no-combine',
        dest='combine',
        action='store_false',
        help="Keep multi-part files separate (overrides config)",
    )
    parser.add_argument(
        '-o',
        '--output',
        dest="output_template",
        help="Output location template (default: {title})",
    )
    parser.add_argument(
        '--output-preset',
        dest="output_preset",
        choices=("default", "audiobookshelf", "plex", "kavita"),
        help="Named output folder/filename template preset (overridden by -o/--output)",
    )
    parser.add_argument(
        '--remove-chars',
        dest="remove_chars",
        help="List of characters that will be removed from output path",
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
        action="store_true",
    )
    parser.add_argument(
        '--split-chapters',
        dest="split_chapters",
        help="Split downloaded audiobook into separate per-chapter files",
        action="store_true",
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
        '--no-skip-downloaded',
        dest="skip_downloaded",
        help="Do not skip already downloaded books (overrides config)",
        action="store_false",
    )
    parser.add_argument(
        '--database-directory',
        '--database_directory',
        dest="database_directory",
        help=f"Directory for sources to store data (default: {default_database_directory()})",
    )
    parser.add_argument(
        '--download-archive',
        dest="download_archive",
        help="Path to SQLite download archive file to record and skip previously downloaded books",
    )
    parser.add_argument(
        '--write-json-metadata',
        dest="write_json_metadata",
        help="Write metadata in a separate json file",
        action="store_true",
    )
    parser.add_argument(
        '--write-cover',
        dest="write_cover",
        help="Save cover artwork as a separate sidecar image file",
        action="store_true",
    )
    parser.add_argument(
        '--write-opf',
        dest="write_opf",
        help="Write metadata as an OPF XML sidecar file (Audiobookshelf/Calibre)",
        action="store_true",
    )
    parser.add_argument(
        '--write-cue',
        dest="write_cue",
        help="Write chapter timestamps as a CUE sheet sidecar file",
        action="store_true",
    )
    parser.add_argument(
        '--config',
        dest="config_location",
        help="Alternative location of config file",
    )
    parser.add_argument(
        '--resume',
        dest="resume",
        help="Resume interrupted downloads using existing parts and HTTP Range requests",
        action="store_true",
    )
    parser.add_argument(
        '--no-resume',
        dest="resume",
        help="Disable resume for this run (overrides config)",
        action="store_false",
    )
    parser.add_argument(
        '--audio-bitrate',
        '--m4b-bitrate',
        dest="audio_bitrate",
        type=_parse_audio_bitrate_arg,
        help="Target AAC bitrate when converting to .m4b/.m4a (auto, 64k, 128k, 192k, etc.)",
    )
    parser.add_argument(
        '-y',
        '--yes',
        '--overwrite',
        dest="overwrite",
        action="store_const",
        const=True,
        help="Overwrite existing files or directories without prompting",
    )
    parser.add_argument(
        '--no-overwrite',
        dest="overwrite",
        action="store_const",
        const=False,
        help="Refuse to overwrite existing files without prompting",
    )
    parser.add_argument(
        '--list-sources',
        dest="list_sources",
        action="store_true",
        help="List all supported audiobook services and exit",
    )
    parser.add_argument(
        '--service',
        dest="service",
        help="Target service name for --search, --library-list, or --verify-auth",
    )
    parser.add_argument(
        '--search',
        dest="search_query",
        help="Search a service catalog (e.g. librivox) and list matching audiobooks",
    )
    parser.add_argument(
        '--library-list',
        dest="library_list",
        action="store_true",
        help="List books from the configured service library/bookshelf",
    )
    parser.add_argument(
        '--verify-auth',
        dest="verify_auth",
        action="store_true",
        help="Verify credentials/cookies for --service and persist session cookies",
    )
    parser.add_argument(
        '--enrich-metadata',
        dest="enrich_metadata",
        action="store_true",
        help="Optionally fill missing metadata fields via OpenLibrary lookup",
    )
    parser.add_argument(
        '--check-updates',
        dest="check_updates",
        action="store_true",
        help="Check whether a newer release of audiobook-dl is available and exit",
    )
    parser.add_argument(
        '--info',
        '--dry-run',
        dest="info_only",
        action="store_true",
        help="Inspect metadata and output paths without downloading files",
    )
    parser.add_argument(
        '--series-items',
        dest="series_items",
        help="1-based comma/range selection of series books to download (e.g. 1,3-5)",
    )
    parser.add_argument(
        '--max-workers',
        dest="max_workers",
        type=_parse_positive_int_arg,
        help="Maximum concurrent download/processing worker threads",
    )
    parser.add_argument(
        '--rate-limit',
        '--book-delay',
        dest="book_delay",
        type=_parse_non_negative_float_arg,
        help="Seconds to pause between consecutive books in a batch or series",
    )
    parser.add_argument(
        '--proxy',
        dest="proxy",
        help="HTTP/HTTPS proxy URL for requests (e.g. http://127.0.0.1:8080)",
    )
    parser.add_argument(
        '--timeout',
        dest="timeout",
        type=_parse_positive_float_arg,
        help="HTTP read timeout in seconds",
    )
    provided = vars(parser.parse_args(argv))
    if "urls" in provided and not provided["urls"]:
        provided.pop("urls")
    return build_default_options(**provided)


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
            with open(options.input_file, "r", encoding="utf-8-sig") as f:
                urls.extend(f.read().split())
        except OSError as error:
            raise SystemExit(
                f"Error: cannot read input file '{options.input_file}': "
                f"{error.strerror or error}"
            )
    return urls

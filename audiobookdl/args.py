import argparse
from dataclasses import dataclass, field
from typing import Any, FrozenSet, List, Optional, Sequence

from audiobookdl import __version__
from audiobookdl.options_schema import (
    OPTION_REGISTRY,
    default_database_directory,
)


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
    media_type: str = "auto"
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
    enrich_providers: Optional[str] = None
    enrich_interactive: bool = False
    no_enrich_cache: bool = False
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
        epilog="Subcommands:\n  audiobook-dl pack <paths...>   Repackage local folders/tracks offline into .m4b",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument(
        '-v',
        '--version',
        action="version",
        version=f"audiobook-dl {__version__}",
    )
    for spec in OPTION_REGISTRY:
        spec.add_cli_arguments(parser)
    provided = vars(parser.parse_args(argv))
    if "urls" in provided and not provided["urls"]:
        provided.pop("urls")
    return build_default_options(**provided)


_PACK_SHARED_OPTION_NAMES: FrozenSet[str] = frozenset(
    {
        "output_template",
        "output_preset",
        "output_format",
        "remove_chars",
        "no_chapters",
        "audio_bitrate",
        "write_json_metadata",
        "write_cover",
        "write_opf",
        "write_cue",
        "overwrite",
        "enrich_metadata",
        "enrich_providers",
        "enrich_interactive",
        "no_enrich_cache",
        "database_directory",
        "proxy",
        "timeout",
        "config_location",
        "debug",
        "quiet",
        "ffmpeg_output",
    }
)


def _parse_series_order_arg(value: str) -> int:
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected integer for --series-order, got {value!r}") from exc
    if not parsed.is_integer():
        raise argparse.ArgumentTypeError(f"Expected integer for --series-order, got {value!r}")
    return int(parsed)


@dataclass
class PackOptions:
    """Pack-subcommand arguments for offline local audiobook repackaging."""

    paths: List[str] = field(default_factory=list)
    recursive: bool = False
    delete_source: bool = False
    dry_run: bool = False
    output_dir: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    narrators: List[str] = field(default_factory=list)
    series: Optional[str] = None
    series_order: Optional[int] = None
    isbn: Optional[str] = None
    asin: Optional[str] = None
    cover_path: Optional[str] = None


def build_pack_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for `audiobook-dl pack`."""
    parser = argparse.ArgumentParser(
        prog="audiobook-dl pack",
        description="Offline repackager to merge local audio tracks into chaptered .m4b audiobooks",
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Local folder(s) or audio track(s) to repackage",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively pack subdirectories that contain audio tracks",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete original source tracks after successful repackaging (kept by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview discovered books, chapters, metadata, and output paths without writing files",
    )
    parser.add_argument(
        "--output-dir",
        dest="pack_output_dir",
        help="Base directory for relative output templates",
    )
    parser.add_argument(
        "--output-template",
        dest="output_template",
        help="Alias for -o/--output location template",
    )
    parser.add_argument("--title", dest="pack_title", help="Override audiobook title")
    parser.add_argument(
        "--author",
        dest="pack_authors",
        action="append",
        help="Override author (repeatable)",
    )
    parser.add_argument(
        "--narrator",
        dest="pack_narrators",
        action="append",
        help="Override narrator (repeatable)",
    )
    parser.add_argument("--series", dest="pack_series", help="Override series name")
    parser.add_argument(
        "--series-order",
        dest="pack_series_order",
        type=_parse_series_order_arg,
        help="Override series index",
    )
    parser.add_argument("--isbn", dest="pack_isbn", help="Override ISBN")
    parser.add_argument("--asin", dest="pack_asin", help="Override ASIN")
    parser.add_argument("--cover", dest="pack_cover_path", help="Path to custom cover image")

    for spec in OPTION_REGISTRY:
        if spec.name in _PACK_SHARED_OPTION_NAMES:
            spec.add_cli_arguments(parser)
    return parser


def parse_pack_arguments(
    argv: Optional[Sequence[str]] = None,
) -> tuple[DownloadOptions, PackOptions]:
    """Parse arguments for `audiobook-dl pack` into `(DownloadOptions, PackOptions)`."""
    parser = build_pack_parser()
    raw = vars(parser.parse_args(argv))
    pack_opts = PackOptions(
        paths=list(raw.pop("paths", [])),
        recursive=bool(raw.pop("recursive", False)),
        delete_source=bool(raw.pop("delete_source", False)),
        dry_run=bool(raw.pop("dry_run", False)),
        output_dir=raw.pop("pack_output_dir", None),
        title=raw.pop("pack_title", None),
        authors=list(raw.pop("pack_authors", [])),
        narrators=list(raw.pop("pack_narrators", [])),
        series=raw.pop("pack_series", None),
        series_order=raw.pop("pack_series_order", None),
        isbn=raw.pop("pack_isbn", None),
        asin=raw.pop("pack_asin", None),
        cover_path=raw.pop("pack_cover_path", None),
    )
    return build_default_options(**raw), pack_opts


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

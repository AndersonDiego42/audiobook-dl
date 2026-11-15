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
    profile: Optional[str] = None
    skip_downloaded: bool = False
    download_archive: Optional[str] = None
    no_download_archive: bool = False
    database_directory: str = field(default_factory=default_database_directory)
    write_json_metadata: bool = False
    write_cover: bool = False
    write_opf: bool = False
    write_abs_metadata: bool = False
    upload_to_abs: bool = False
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
        epilog=(
            "Subcommands:\n"
            "  audiobook-dl pack <paths...>      Repackage local folders/tracks offline into .m4b\n"
            "  audiobook-dl library <action>     Manage unified library ledger, tags, and trash"
        ),
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
        "write_abs_metadata",
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


@dataclass
class LibraryOptions:
    """Arguments for `audiobook-dl library` subcommand."""

    action: str
    entry_ids: List[int] = field(default_factory=list)
    query: str = ""
    limit: int = 100
    include_trashed: bool = False
    include_hidden: bool = True
    tags: List[str] = field(default_factory=list)
    archive_path: Optional[str] = None
    config_location: Optional[str] = None


def parse_library_arguments(argv: Optional[Sequence[str]] = None) -> LibraryOptions:
    """Parse arguments for `audiobook-dl library`."""
    parser = argparse.ArgumentParser(
        prog="audiobook-dl library",
        description="Search, tag, hide, and manage library entries in downloads.db",
    )
    parser.add_argument(
        "--config",
        dest="config_location",
        default=None,
        help="Alternative location of config file",
    )
    parser.add_argument(
        "--download-archive",
        dest="archive_path",
        default=None,
        help="Path to SQLite library database",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    list_p = sub.add_parser("list", help="List or search library entries")
    list_p.add_argument("-q", "--query", default="", help="Field-aware search query (e.g. author:herbert tag:scifi)")
    list_p.add_argument("--limit", type=int, default=100, help="Maximum entries to print")
    list_p.add_argument("--include-trashed", action="store_true", help="Include trashed entries")
    list_p.add_argument("--no-hidden", action="store_true", help="Exclude entries tagged 'hidden'")

    tag_p = sub.add_parser("tag", help="Add or remove tags on library entries")
    tag_p.add_argument("entry_ids", nargs="+", type=int, help="Entry ID(s)")
    tag_p.add_argument("--add", nargs="*", default=[], help="Tags to add")
    tag_p.add_argument("--remove", nargs="*", default=[], help="Tags to remove")

    for act, help_txt in (
        ("hide", "Tag entries as 'hidden'"),
        ("unhide", "Remove 'hidden' tag from entries"),
        ("trash", "Move entries to trash"),
        ("restore", "Restore trashed entries"),
    ):
        act_p = sub.add_parser(act, help=help_txt)
        act_p.add_argument("entry_ids", nargs="+", type=int, help="Entry ID(s)")

    ns = parser.parse_args(argv)
    tags: List[str] = []
    if ns.action == "tag":
        tags = [*ns.add, *(f"-{t}" for t in ns.remove)]
    return LibraryOptions(
        action=ns.action,
        entry_ids=list(getattr(ns, "entry_ids", [])),
        query=getattr(ns, "query", ""),
        limit=getattr(ns, "limit", 100),
        include_trashed=bool(getattr(ns, "include_trashed", False)),
        include_hidden=not bool(getattr(ns, "no_hidden", False)),
        tags=tags,
        archive_path=ns.archive_path,
        config_location=ns.config_location,
    )


@dataclass
class ScanOptions:
    """CLI options parsed by `audiobook-dl scan`."""

    paths: List[str] = field(default_factory=list)
    verify_missing: bool = False
    dry_run: bool = False
    json_output: bool = False
    profile: str = "default"
    archive_path: Optional[str] = None
    config_location: Optional[str] = None


def parse_scan_arguments(argv: Optional[Sequence[str]] = None) -> ScanOptions:
    """Parse arguments for `audiobook-dl scan`."""
    parser = argparse.ArgumentParser(
        prog="audiobook-dl scan",
        description="Scan local folders for existing audiobooks and reconcile downloads.db",
    )
    parser.add_argument("paths", nargs="+", help="Local folder(s) or audio file(s) to scan")
    parser.add_argument("--config", dest="config_location", default=None, help="Alternative location of config file")
    parser.add_argument("--download-archive", dest="archive_path", default=None, help="Path to SQLite library database")
    parser.add_argument("--profile", default="default", help="Profile label to record for newly registered books")
    parser.add_argument(
        "--verify-missing",
        action="store_true",
        help="Mark previously downloaded entries whose output_path no longer exists back to pending",
    )
    parser.add_argument("--dry-run", action="store_true", help="Inspect without writing to downloads.db")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print scan summary as JSON")
    ns = parser.parse_args(argv)
    return ScanOptions(
        paths=list(ns.paths),
        verify_missing=bool(ns.verify_missing),
        dry_run=bool(ns.dry_run),
        json_output=bool(ns.json_output),
        profile=str(ns.profile or "default").strip().lower() or "default",
        archive_path=ns.archive_path,
        config_location=ns.config_location,
    )


@dataclass
class ExportOptions:
    """CLI options parsed by `audiobook-dl export`."""

    output_path: str
    fmt: Optional[str] = None
    query: str = ""
    limit: int = 100000
    include_trashed: bool = False
    include_hidden: bool = True
    archive_path: Optional[str] = None
    config_location: Optional[str] = None


def parse_export_arguments(argv: Optional[Sequence[str]] = None) -> ExportOptions:
    """Parse arguments for `audiobook-dl export`."""
    parser = argparse.ArgumentParser(
        prog="audiobook-dl export",
        description="Export library ledger entries to CSV, JSON, or Excel (.xlsx)",
    )
    parser.add_argument("output_path", help="Destination export path (.csv, .json, or .xlsx)")
    parser.add_argument(
        "-f",
        "--format",
        dest="fmt",
        choices=("csv", "json", "xlsx"),
        default=None,
        help="Override export format (defaults to file extension)",
    )
    parser.add_argument("-q", "--query", default="", help="Filter entries before exporting")
    parser.add_argument("--limit", type=int, default=100000, help="Maximum entries to export")
    parser.add_argument("--include-trashed", action="store_true", help="Include trashed entries")
    parser.add_argument("--no-hidden", action="store_true", help="Exclude entries tagged 'hidden'")
    parser.add_argument("--config", dest="config_location", default=None, help="Alternative location of config file")
    parser.add_argument("--download-archive", dest="archive_path", default=None, help="Path to SQLite library database")
    ns = parser.parse_args(argv)
    return ExportOptions(
        output_path=str(ns.output_path),
        fmt=ns.fmt,
        query=str(ns.query or ""),
        limit=max(1, int(ns.limit)),
        include_trashed=bool(ns.include_trashed),
        include_hidden=not bool(ns.no_hidden),
        archive_path=ns.archive_path,
        config_location=ns.config_location,
    )


@dataclass
class SyncOptions:
    """CLI options parsed by `audiobook-dl sync`."""

    services: List[str] = field(default_factory=list)
    profile: Optional[str] = None
    scan_only: bool = False
    dry_run: bool = False
    watch: bool = False
    interval_seconds: int = 3600
    json_output: bool = False
    output_template: Optional[str] = None
    output_format: Optional[str] = None
    archive_path: Optional[str] = None
    config_location: Optional[str] = None


def parse_sync_arguments(argv: Optional[Sequence[str]] = None) -> SyncOptions:
    """Parse arguments for `audiobook-dl sync`."""
    parser = argparse.ArgumentParser(
        prog="audiobook-dl sync",
        description="Poll configured account libraries and download new books automatically",
    )
    parser.add_argument(
        "--service",
        dest="services",
        action="append",
        default=[],
        help="Specific source service to sync (repeatable; defaults to all configured library services)",
    )
    parser.add_argument("--profile", default=None, help="Limit sync to a single named profile")
    parser.add_argument("--scan-only", action="store_true", help="Refresh downloads.db without downloading")
    parser.add_argument("--dry-run", action="store_true", help="Preview pending downloads without writing DB")
    parser.add_argument("--watch", action="store_true", help="Poll continuously at --interval seconds")
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Polling interval in seconds when --watch is active (default: 3600)",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print cycle results as JSON")
    parser.add_argument("-o", "--output", dest="output_template", default=None, help="Override output template")
    parser.add_argument("-f", "--output-format", dest="output_format", default=None, help="Override output format")
    parser.add_argument("--config", dest="config_location", default=None, help="Alternative location of config file")
    parser.add_argument("--download-archive", dest="archive_path", default=None, help="Path to SQLite library database")
    ns = parser.parse_args(argv)
    if ns.interval <= 0:
        parser.error("--interval must be a positive integer")
    return SyncOptions(
        services=list(ns.services or []),
        profile=ns.profile,
        scan_only=bool(ns.scan_only),
        dry_run=bool(ns.dry_run),
        watch=bool(ns.watch),
        interval_seconds=max(1, int(ns.interval)),
        json_output=bool(ns.json_output),
        output_template=ns.output_template,
        output_format=ns.output_format,
        archive_path=ns.archive_path,
        config_location=ns.config_location,
    )


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

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
    for spec in OPTION_REGISTRY:
        spec.add_cli_arguments(parser)
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

"""Declarative runtime option registry shared by CLI, Config, Pipeline, and GUI."""

import argparse
from dataclasses import dataclass
import math
import os
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple, Union

from audiobookdl.utils import normalize_audio_bitrate


MEDIA_TYPE_CHOICES: Tuple[str, ...] = ("auto", "audiobook", "ebook", "both")
OUTPUT_PRESET_CHOICES: Tuple[str, ...] = ("default", "audiobookshelf", "plex", "kavita")
GUI_OUTPUT_FORMATS: Tuple[str, ...] = ("Original", "m4b", "mp3", "m4a", "flac", "opus")
GUI_MEDIA_TYPE_OPTIONS: Tuple[str, ...] = ("Auto", "Audiobook", "Ebook", "Both")
GUI_OVERWRITE_OPTIONS: Tuple[str, ...] = ("Ask", "Overwrite", "Keep existing")

GUI_CONFIG_KEYS: FrozenSet[str] = frozenset(
    {
        "window_geometry",
        "output_dir",
        "output_template",
        "use_download_archive",
        "theme",
        "notify_on_complete",
    }
)
SOURCE_CONFIG_KEYS: FrozenSet[str] = frozenset({"username", "password", "library", "cookie_file"})

_MISSING: Any = object()


def default_database_directory() -> str:
    """Return the default directory for sources to store data."""
    from audiobookdl.config import config_dir

    return os.path.join(config_dir(), "db")


def parse_audio_bitrate_arg(value: str) -> str:
    """Validate `--audio-bitrate` CLI argument, preserving explicit `'auto'` override."""
    if value.strip().lower() == "auto":
        return "auto"
    try:
        normalized = normalize_audio_bitrate(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return normalized or "auto"


def parse_positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected integer, got '{value}'") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("Expected integer >= 1")
    return parsed


def parse_positive_float_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected number, got '{value}'") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Expected number > 0")
    return parsed


def parse_non_negative_float_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected number, got '{value}'") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Expected number >= 0")
    return parsed


def _validate_str(key: str, raw: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(raw, str):
        return None, f"Warning: invalid '{key}' in config (expected string); ignoring"
    return raw, None


def _validate_bool(key: str, raw: Any) -> Tuple[Optional[bool], Optional[str]]:
    if not isinstance(raw, bool):
        return None, f"Warning: invalid '{key}' in config (expected boolean); ignoring"
    return raw, None


def _validate_positive_int(key: str, raw: Any) -> Tuple[Optional[int], Optional[str]]:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw, None
    return None, f"Warning: invalid '{key}' in config (expected integer >= 1); ignoring"


def _validate_non_negative_float(key: str, raw: Any) -> Tuple[Optional[float], Optional[str]]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw), None
    return None, f"Warning: invalid '{key}' in config (expected number >= 0); ignoring"


def _validate_positive_float(key: str, raw: Any) -> Tuple[Optional[float], Optional[str]]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw), None
    return None, f"Warning: invalid '{key}' in config (expected number > 0); ignoring"


def _validate_audio_bitrate(key: str, raw: Any) -> Tuple[Optional[str], Optional[str]]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    try:
        return normalize_audio_bitrate(val), None
    except ValueError:
        return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def parse_enrich_providers_arg(value: str) -> str:
    """Validate and normalize `--enrich-providers` comma-separated provider list."""
    from audiobookdl.utils.enrichment import parse_provider_list

    try:
        return ",".join(parse_provider_list(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_limit_rate_arg(value: str) -> str:
    """Validate and normalize `--limit-rate` bandwidth specification."""
    from audiobookdl.utils.http import parse_byte_rate

    try:
        parsed = parse_byte_rate(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value.strip() if parsed else ""


def _validate_enrich_providers(key: str, raw: Any) -> Tuple[Optional[str], Optional[str]]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    if not val.strip():
        return None, None
    from audiobookdl.utils.enrichment import parse_provider_list

    try:
        return ",".join(parse_provider_list(val)), None
    except ValueError:
        return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_media_type(key: str, raw: Any) -> Tuple[Optional[str], Optional[str]]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    normalized = val.strip().lower()
    if normalized in MEDIA_TYPE_CHOICES:
        return normalized, None
    return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_limit_rate(key: str, raw: Any) -> Tuple[Optional[str], Optional[str]]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    cleaned = val.strip()
    if not cleaned:
        return None, None
    from audiobookdl.utils.http import parse_byte_rate

    try:
        parse_byte_rate(cleaned)
    except ValueError:
        return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"
    return cleaned, None


_CONFIG_VALIDATORS: Mapping[str, Callable[[str, Any], Tuple[Any, Optional[str]]]] = {
    "str": _validate_str,
    "bool": _validate_bool,
    "positive_int": _validate_positive_int,
    "non_negative_float": _validate_non_negative_float,
    "positive_float": _validate_positive_float,
    "audio_bitrate": _validate_audio_bitrate,
    "media_type": _validate_media_type,
    "enrich_providers": _validate_enrich_providers,
    "limit_rate": _validate_limit_rate,
}


def validate_config_value(key: str, value_kind: str, raw: Any) -> Tuple[Any, Optional[str]]:
    """Validate and normalize a raw TOML value according to `value_kind`."""
    if raw is None:
        return None, None
    validator = _CONFIG_VALIDATORS.get(value_kind)
    if validator is None:
        raise ValueError(f"Unknown config value kind: {value_kind!r}")
    return validator(key, raw)


def _serialize_bool(val: Any) -> Tuple[bool, Any]:
    return True, bool(val)


def _serialize_non_empty_str(val: Any) -> Tuple[bool, Any]:
    return (True, val) if val else (False, None)


def _serialize_audio_bitrate(val: Any) -> Tuple[bool, Any]:
    normalized = normalize_audio_bitrate(val)
    return (True, normalized) if normalized else (False, None)


def _serialize_media_type(val: Any) -> Tuple[bool, Any]:
    normalized = str(val).strip().lower() if val else ""
    if normalized and normalized != "auto":
        return True, normalized
    return False, None


def _serialize_optional_bool(val: Any) -> Tuple[bool, Any]:
    if val is None:
        return False, None
    return True, bool(val)


def _serialize_positive_int(val: Any) -> Tuple[bool, Any]:
    if val is None:
        return False, None
    return True, int(val)


def _serialize_positive_float(val: Any) -> Tuple[bool, Any]:
    if val is None or float(val) <= 0:
        return False, None
    return True, float(val)


def _gui_init_output_format(cfg_val: Any) -> str:
    val = cfg_val or "Original"
    return val if val in GUI_OUTPUT_FORMATS else "Original"


def _gui_read_output_format(raw: Any) -> Optional[str]:
    cleaned = str(raw).strip()
    return None if cleaned in ("", "Original") else cleaned


def _gui_init_media_type(cfg_val: Any) -> str:
    normalized = (cfg_val or "auto").strip().lower()
    return {
        "auto": "Auto",
        "audiobook": "Audiobook",
        "ebook": "Ebook",
        "both": "Both",
    }.get(normalized, "Auto")


def _gui_read_media_type(raw: Any) -> str:
    cleaned = str(raw).strip().lower()
    return cleaned if cleaned in MEDIA_TYPE_CHOICES else "auto"


def _gui_init_audio_bitrate(cfg_val: Any) -> str:
    cleaned = (cfg_val or "").strip()
    if not cleaned or cleaned.lower() == "auto":
        return "Auto"
    return cleaned


def _gui_read_audio_bitrate(raw: Any) -> Optional[str]:
    cleaned = str(raw).strip()
    return None if cleaned in ("", "Auto", "auto") else cleaned


def _gui_init_bool_default_true(cfg_val: Any) -> bool:
    return True if cfg_val is None else bool(cfg_val)


def _gui_init_bool_from_truthy(cfg_val: Any) -> bool:
    return bool(cfg_val)


def _gui_init_optional_str(cfg_val: Any) -> str:
    return "" if cfg_val is None else str(cfg_val)


def _gui_read_optional_str(raw: Any) -> Optional[str]:
    cleaned = str(raw).strip()
    return cleaned or None


def _gui_read_enrich_providers(raw: Any) -> Optional[str]:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    from audiobookdl.utils.enrichment import parse_provider_list

    return ",".join(parse_provider_list(cleaned))


def _gui_read_limit_rate(raw: Any) -> Optional[str]:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    from audiobookdl.utils.http import parse_byte_rate

    parse_byte_rate(cleaned)
    return cleaned


def _gui_init_raw_str(cfg_val: Any) -> str:
    return "" if cfg_val is None else str(cfg_val)


def _gui_read_raw_str(raw: Any) -> str:
    return str(raw)


def _gui_init_overwrite(cfg_val: Any) -> str:
    if cfg_val is True:
        return "Overwrite"
    if cfg_val is False:
        return "Keep existing"
    return "Ask"


def _gui_read_overwrite(raw: Any) -> Optional[bool]:
    cleaned = str(raw).strip().lower()
    if cleaned == "overwrite":
        return True
    if cleaned in ("keep existing", "no", "false"):
        return False
    return None


def _gui_init_max_workers(cfg_val: Any) -> str:
    return "" if cfg_val is None else str(cfg_val)


def _gui_read_max_workers(raw: Any) -> Optional[int]:
    cleaned = str(raw).strip()
    if not cleaned or cleaned.lower() in ("auto", "default"):
        return None
    try:
        parsed = int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Max workers must be a whole number >= 1, got '{cleaned}'") from exc
    if parsed < 1:
        raise ValueError("Max workers must be >= 1")
    return parsed


def _gui_format_number(cfg_val: Any, default_str: str = "") -> str:
    if cfg_val is None:
        return default_str
    num = float(cfg_val)
    return str(int(num)) if num.is_integer() else str(num)


def _gui_init_book_delay(cfg_val: Any) -> str:
    return _gui_format_number(cfg_val, default_str="0")


def _gui_read_book_delay(raw: Any) -> float:
    cleaned = str(raw).strip()
    if not cleaned:
        return 0.0
    try:
        parsed = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Book delay must be a number >= 0, got '{cleaned}'") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("Book delay must be >= 0")
    return parsed


def _gui_init_timeout(cfg_val: Any) -> str:
    return _gui_format_number(cfg_val, default_str="")


def _gui_read_timeout(raw: Any) -> Optional[float]:
    cleaned = str(raw).strip()
    if not cleaned or cleaned.lower() in ("auto", "default"):
        return None
    try:
        parsed = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Timeout must be a number > 0, got '{cleaned}'") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("Timeout must be > 0")
    return parsed


@dataclass(frozen=True)
class CliFlag:
    """Single argparse flag or positional argument definition bound to an `OptionSpec`."""

    flags: Tuple[str, ...]
    action: Optional[str] = None
    const: Any = _MISSING
    nargs: Optional[str] = None
    choices: Optional[Tuple[str, ...]] = None
    arg_type: Optional[Callable[[str], Any]] = None
    help: Union[str, Callable[[], str], None] = None


@dataclass(frozen=True)
class GuiBinding:
    """Declarative binding between a runtime option and a Tkinter variable."""

    var_attr: str
    var_type: str  # "bool" | "str"
    gui_default: Any
    from_config: Optional[Callable[[Any], Any]] = None
    to_option: Optional[Callable[[Any], Any]] = None
    persisted_in_output_prefs: bool = False

    def init_value(self, config_value: Any) -> Any:
        if self.from_config is not None:
            return self.from_config(config_value)
        if self.var_type == "bool":
            return bool(config_value) if config_value is not None else bool(self.gui_default)
        return self.gui_default if config_value is None else str(config_value)

    def read_option(self, raw_var_value: Any) -> Any:
        if self.to_option is not None:
            return self.to_option(raw_var_value)
        if self.var_type == "bool":
            return bool(raw_var_value)
        return raw_var_value


@dataclass(frozen=True)
class OptionSpec:
    """Single source of truth describing a runtime option across CLI, Config, Pipeline, and GUI."""

    name: str
    default: Any = None
    default_factory: Optional[Callable[[], Any]] = None
    in_download_options: bool = True
    cli_flags: Tuple[CliFlag, ...] = ()
    config_key: Optional[str] = None
    config_kind: Optional[str] = None
    toml_serializer: Optional[Callable[[Any], Tuple[bool, Any]]] = None
    merge_from_config: bool = False
    merge_only_if_default: bool = False
    merge_require_truthy: bool = False
    merge_cast: Optional[Callable[[Any], Any]] = None
    gui: Optional[GuiBinding] = None

    def resolve_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        return self.default

    def add_cli_arguments(self, parser: argparse.ArgumentParser) -> None:
        for cli in self.cli_flags:
            kwargs: Dict[str, Any] = {}
            is_positional = not any(f.startswith("-") for f in cli.flags)
            if not is_positional:
                kwargs["dest"] = self.name
            if cli.action is not None:
                kwargs["action"] = cli.action
            if cli.const is not _MISSING:
                kwargs["const"] = cli.const
            if cli.nargs is not None:
                kwargs["nargs"] = cli.nargs
            if cli.choices is not None:
                kwargs["choices"] = cli.choices
            if cli.arg_type is not None:
                kwargs["type"] = cli.arg_type
            if cli.help is not None:
                kwargs["help"] = cli.help() if callable(cli.help) else cli.help
            parser.add_argument(*cli.flags, **kwargs)


OPTION_REGISTRY: Tuple[OptionSpec, ...] = (
    OptionSpec(
        name="urls",
        default_factory=list,
        cli_flags=(CliFlag(("urls",), nargs="*", help="Urls to download from"),),
    ),
    OptionSpec(
        name="cookie_file",
        default=None,
        cli_flags=(CliFlag(("-c", "--cookies"), help="Path to Netscape cookie file"),),
    ),
    OptionSpec(
        name="combine",
        default=False,
        cli_flags=(
            CliFlag(("--combine",), action="store_true", help="Combine output files into a single file"),
            CliFlag(("--no-combine",), action="store_false", help="Keep multi-part files separate (overrides config)"),
        ),
        config_key="combine",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        gui=GuiBinding(
            var_attr="combine_var",
            var_type="bool",
            gui_default=True,
            from_config=_gui_init_bool_default_true,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="output_template",
        default="{title}",
        cli_flags=(CliFlag(("-o", "--output"), help="Output location template (default: {title})"),),
        config_key="output_template",
        config_kind="str",
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="output_preset",
        default=None,
        cli_flags=(
            CliFlag(
                ("--output-preset",),
                choices=OUTPUT_PRESET_CHOICES,
                help="Named output folder/filename template preset (overridden by -o/--output)",
            ),
        ),
    ),
    OptionSpec(
        name="remove_chars",
        default="",
        cli_flags=(
            CliFlag(("--remove-chars",), help="List of characters that will be removed from output path"),
        ),
        config_key="remove_chars",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="remove_chars_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_raw_str,
            to_option=_gui_read_raw_str,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="debug",
        default=False,
        cli_flags=(CliFlag(("--debug", "-d"), action="store_true", help="Debug mode"),),
        config_key="debug",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        gui=GuiBinding(
            var_attr="debug_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="quiet",
        default=False,
        cli_flags=(CliFlag(("--quiet", "-q"), action="store_true", help="Quiet mode"),),
    ),
    OptionSpec(
        name="print_output",
        default=False,
        cli_flags=(
            CliFlag(("--print-output",), action="store_true", help="Prints the output locations instead of downloading"),
        ),
    ),
    OptionSpec(
        name="cover",
        default=False,
        cli_flags=(CliFlag(("--cover",), action="store_true", help="Download only cover"),),
        config_key="cover",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        gui=GuiBinding(
            var_attr="cover_only_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="no_chapters",
        default=False,
        cli_flags=(
            CliFlag(("--no-chapters",), action="store_true", help="Don't include chapters in final file"),
        ),
        config_key="no_chapters",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="no_chapters_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="split_chapters",
        default=False,
        cli_flags=(
            CliFlag(
                ("--split-chapters",),
                action="store_true",
                help="Split downloaded audiobook into separate per-chapter files",
            ),
        ),
        config_key="split_chapters",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="split_chapters_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="output_format",
        default=None,
        cli_flags=(CliFlag(("-f", "--output-format"), help="Output file format"),),
        config_key="output_format",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="output_format_var",
            var_type="str",
            gui_default="Original",
            from_config=_gui_init_output_format,
            to_option=_gui_read_output_format,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="media_type",
        default="auto",
        cli_flags=(
            CliFlag(
                ("--media-type",),
                choices=MEDIA_TYPE_CHOICES,
                help="Media format to download when sources offer both audiobooks and ebooks (auto, audiobook, ebook, both)",
            ),
        ),
        config_key="media_type",
        config_kind="media_type",
        toml_serializer=_serialize_media_type,
        merge_from_config=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="media_type_var",
            var_type="str",
            gui_default="Auto",
            from_config=_gui_init_media_type,
            to_option=_gui_read_media_type,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="ffmpeg_output",
        default=False,
        cli_flags=(
            CliFlag(("--verbose-ffmpeg",), action="store_true", help="Show ffmpeg output in terminal"),
        ),
    ),
    OptionSpec(
        name="input_file",
        default=None,
        cli_flags=(CliFlag(("--input-file",), help="File with one url to download per line"),),
    ),
    OptionSpec(
        name="username",
        default=None,
        cli_flags=(CliFlag(("--username",), help="Username for source"),),
    ),
    OptionSpec(
        name="password",
        default=None,
        cli_flags=(CliFlag(("--password",), help="Password for source"),),
    ),
    OptionSpec(
        name="library",
        default=None,
        cli_flags=(CliFlag(("--library",), help="Library for source"),),
    ),
    OptionSpec(
        name="profile",
        default=None,
        cli_flags=(
            CliFlag(
                ("--profile",),
                help="Named service account profile from config (default: 'default')",
            ),
        ),
    ),
    OptionSpec(
        name="skip_downloaded",
        default=False,
        cli_flags=(
            CliFlag(
                ("--skip-downloaded",),
                action="store_true",
                help="Skip downloading books if the output file or directory already exists",
            ),
            CliFlag(
                ("--no-skip-downloaded",),
                action="store_false",
                help="Do not skip already downloaded books (overrides config)",
            ),
        ),
        config_key="skip_downloaded",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        gui=GuiBinding(
            var_attr="skip_downloaded_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="download_archive",
        default=None,
        cli_flags=(
            CliFlag(
                ("--download-archive",),
                help="Path to SQLite download archive file to record and skip previously downloaded books",
            ),
        ),
        config_key="download_archive",
        config_kind="str",
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="download_archive_var",
            var_type="bool",
            gui_default=False,
            from_config=_gui_init_bool_from_truthy,
        ),
    ),
    OptionSpec(
        name="no_download_archive",
        default=False,
        cli_flags=(
            CliFlag(
                ("--no-download-archive",),
                action="store_true",
                help="Disable reading and recording entries in the SQLite download archive (downloads.db)",
            ),
        ),
    ),
    OptionSpec(
        name="database_directory",
        default_factory=default_database_directory,
        cli_flags=(
            CliFlag(
                ("--database-directory", "--database_directory"),
                help=lambda: f"Directory for sources to store data (default: {default_database_directory()})",
            ),
        ),
        config_key="database_directory",
        config_kind="str",
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="write_json_metadata",
        default=False,
        cli_flags=(
            CliFlag(("--write-json-metadata",), action="store_true", help="Write metadata in a separate json file"),
        ),
        config_key="write_json_metadata",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="write_json_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="write_cover",
        default=False,
        cli_flags=(
            CliFlag(("--write-cover",), action="store_true", help="Save cover artwork as a separate sidecar image file"),
        ),
        config_key="write_cover",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="write_cover_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="write_opf",
        default=False,
        cli_flags=(
            CliFlag(
                ("--write-opf",),
                action="store_true",
                help="Write metadata as an OPF XML sidecar file (Audiobookshelf/Calibre)",
            ),
        ),
        config_key="write_opf",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="write_opf_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="write_abs_metadata",
        default=False,
        cli_flags=(
            CliFlag(
                ("--write-abs-metadata",),
                action="store_true",
                help="Write Audiobookshelf-compatible metadata.json sidecar alongside downloaded files",
            ),
        ),
        config_key="write_abs_metadata",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="write_abs_metadata_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="upload_to_abs",
        default=False,
        cli_flags=(
            CliFlag(
                ("--upload-to-abs",),
                action="store_true",
                help="Upload completed audiobooks to the configured Audiobookshelf server",
            ),
        ),
    ),
    OptionSpec(
        name="write_cue",
        default=False,
        cli_flags=(
            CliFlag(("--write-cue",), action="store_true", help="Write chapter timestamps as a CUE sheet sidecar file"),
        ),
        config_key="write_cue",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="write_cue_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="config_location",
        default=None,
        cli_flags=(CliFlag(("--config",), help="Alternative location of config file"),),
    ),
    OptionSpec(
        name="resume",
        default=False,
        cli_flags=(
            CliFlag(
                ("--resume",),
                action="store_true",
                help="Resume interrupted downloads using existing parts and HTTP Range requests",
            ),
            CliFlag(
                ("--no-resume",),
                action="store_false",
                help="Disable resume for this run (overrides config)",
            ),
        ),
        config_key="resume",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        gui=GuiBinding(
            var_attr="resume_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="audio_bitrate",
        default=None,
        cli_flags=(
            CliFlag(
                ("--audio-bitrate", "--m4b-bitrate"),
                arg_type=parse_audio_bitrate_arg,
                help="Target AAC bitrate when converting to .m4b/.m4a (auto, 64k, 128k, 192k, etc.)",
            ),
        ),
        config_key="audio_bitrate",
        config_kind="audio_bitrate",
        toml_serializer=_serialize_audio_bitrate,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="audio_bitrate_var",
            var_type="str",
            gui_default="Auto",
            from_config=_gui_init_audio_bitrate,
            to_option=_gui_read_audio_bitrate,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="overwrite",
        default=None,
        cli_flags=(
            CliFlag(
                ("-y", "--yes", "--overwrite"),
                action="store_const",
                const=True,
                help="Overwrite existing files or directories without prompting",
            ),
            CliFlag(
                ("--no-overwrite",),
                action="store_const",
                const=False,
                help="Refuse to overwrite existing files without prompting",
            ),
        ),
        config_key="overwrite",
        config_kind="bool",
        toml_serializer=_serialize_optional_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="overwrite_var",
            var_type="str",
            gui_default="Ask",
            from_config=_gui_init_overwrite,
            to_option=_gui_read_overwrite,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="list_sources",
        default=False,
        cli_flags=(
            CliFlag(("--list-sources",), action="store_true", help="List all supported audiobook services and exit"),
        ),
    ),
    OptionSpec(
        name="info_only",
        default=False,
        cli_flags=(
            CliFlag(
                ("--info", "--dry-run"),
                action="store_true",
                help="Inspect metadata and output paths without downloading files",
            ),
        ),
    ),
    OptionSpec(
        name="series_items",
        default=None,
        cli_flags=(
            CliFlag(
                ("--series-items",),
                help="1-based comma/range selection of series books to download (e.g. 1,3-5)",
            ),
        ),
    ),
    OptionSpec(
        name="max_workers",
        default=None,
        cli_flags=(
            CliFlag(
                ("--max-workers",),
                arg_type=parse_positive_int_arg,
                help="Maximum concurrent download/processing worker threads",
            ),
        ),
        config_key="max_workers",
        config_kind="positive_int",
        toml_serializer=_serialize_positive_int,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="max_workers_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_max_workers,
            to_option=_gui_read_max_workers,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="book_delay",
        default=0.0,
        cli_flags=(
            CliFlag(
                ("--rate-limit", "--book-delay"),
                arg_type=parse_non_negative_float_arg,
                help="Seconds to pause between consecutive books in a batch or series",
            ),
        ),
        config_key="book_delay",
        config_kind="non_negative_float",
        toml_serializer=_serialize_positive_float,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_cast=float,
        gui=GuiBinding(
            var_attr="book_delay_var",
            var_type="str",
            gui_default="0",
            from_config=_gui_init_book_delay,
            to_option=_gui_read_book_delay,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="limit_rate",
        default=None,
        cli_flags=(
            CliFlag(
                ("--limit-rate",),
                arg_type=parse_limit_rate_arg,
                help="Maximum aggregate download speed (e.g. 500K, 2M)",
            ),
        ),
        config_key="limit_rate",
        config_kind="limit_rate",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="limit_rate_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_limit_rate,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="json_output",
        default=False,
        cli_flags=(
            CliFlag(
                ("--json",),
                action="store_true",
                help="Emit machine-readable JSON summary on stdout",
            ),
        ),
    ),
    OptionSpec(
        name="post_exec",
        default=None,
        cli_flags=(
            CliFlag(
                ("--post-exec",),
                help="Shell command to execute after each completed audiobook (sets AUDIOBOOK_DL_* env vars)",
            ),
        ),
        config_key="post_exec",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="loudnorm",
        default=False,
        cli_flags=(
            CliFlag(
                ("--loudnorm",),
                action="store_true",
                help="Apply EBU R128 loudness normalization during audio processing",
            ),
        ),
        config_key="loudnorm",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
    ),
    OptionSpec(
        name="mono",
        default=False,
        cli_flags=(
            CliFlag(
                ("--mono",),
                action="store_true",
                help="Downmix audio to mono (1 channel) for voice-optimized size",
            ),
        ),
        config_key="mono",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
    ),
    OptionSpec(
        name="chapters_from_silence",
        default=False,
        cli_flags=(
            CliFlag(
                ("--chapters-from-silence",),
                action="store_true",
                help="Detect long pauses via FFmpeg silencedetect when no chapter markers exist",
            ),
        ),
    ),
    OptionSpec(
        name="chapter_offset_ms",
        default=0,
        cli_flags=(
            CliFlag(
                ("--chapter-offset",),
                arg_type=int,
                help="Shift non-initial chapter timestamps by N milliseconds",
            ),
        ),
    ),
    OptionSpec(
        name="square_cover",
        default=False,
        cli_flags=(
            CliFlag(
                ("--square-cover",),
                action="store_true",
                help="Pad cover artwork to a 1:1 square aspect ratio",
            ),
        ),
        config_key="square_cover",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
    ),
    OptionSpec(
        name="max_cover_size",
        default=None,
        cli_flags=(
            CliFlag(
                ("--max-cover-size",),
                arg_type=parse_positive_int_arg,
                help="Downscale oversized cover artwork so width and height do not exceed N pixels",
            ),
        ),
        config_key="max_cover_size",
        config_kind="positive_int",
        toml_serializer=_serialize_positive_int,
        merge_from_config=True,
        merge_only_if_default=True,
    ),
    OptionSpec(
        name="proxy",
        default=None,
        cli_flags=(
            CliFlag(("--proxy",), help="HTTP/HTTPS proxy URL for requests (e.g. http://127.0.0.1:8080)"),
        ),
        config_key="proxy",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="proxy_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_optional_str,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="timeout",
        default=None,
        cli_flags=(
            CliFlag(("--timeout",), arg_type=parse_positive_float_arg, help="HTTP read timeout in seconds"),
        ),
        config_key="timeout",
        config_kind="positive_float",
        toml_serializer=_serialize_positive_float,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_cast=float,
        gui=GuiBinding(
            var_attr="timeout_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_timeout,
            to_option=_gui_read_timeout,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="service",
        default=None,
        cli_flags=(
            CliFlag(("--service",), help="Target service name for --search, --library-list, or --verify-auth"),
        ),
    ),
    OptionSpec(
        name="search_query",
        default=None,
        cli_flags=(
            CliFlag(("--search",), help="Search a service catalog (e.g. librivox) and list matching audiobooks"),
        ),
    ),
    OptionSpec(
        name="library_list",
        default=False,
        cli_flags=(
            CliFlag(("--library-list",), action="store_true", help="List books from the configured service library/bookshelf"),
        ),
    ),
    OptionSpec(
        name="verify_auth",
        default=False,
        cli_flags=(
            CliFlag(
                ("--verify-auth",),
                action="store_true",
                help="Verify credentials/cookies for --service and persist session cookies",
            ),
        ),
    ),
    OptionSpec(
        name="enrich_metadata",
        default=False,
        cli_flags=(
            CliFlag(
                ("--enrich", "--enrich-metadata"),
                action="store_true",
                help="Fill missing metadata, cover art, and chapters via Audnexus, iTunes, Google Books, and OpenLibrary",
            ),
        ),
        config_key="enrich_metadata",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="enrich_metadata_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="enrich_providers",
        default=None,
        cli_flags=(
            CliFlag(
                ("--enrich-providers",),
                arg_type=parse_enrich_providers_arg,
                help="Comma-separated enrichment providers in priority order (audnexus,itunes,google,openlibrary)",
            ),
        ),
        config_key="enrich_providers",
        config_kind="enrich_providers",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="enrich_providers_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_enrich_providers,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="enrich_interactive",
        default=False,
        cli_flags=(
            CliFlag(
                ("--enrich-interactive",),
                action="store_true",
                help="Prompt interactively to select from ranked enrichment matches",
            ),
        ),
        config_key="enrich_interactive",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="enrich_interactive_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="no_enrich_cache",
        default=False,
        cli_flags=(
            CliFlag(
                ("--no-enrich-cache",),
                action="store_true",
                help="Bypass reading cached enrichment responses and fetch fresh metadata",
            ),
        ),
    ),
    OptionSpec(
        name="check_updates",
        default=False,
        cli_flags=(
            CliFlag(
                ("--check-updates",),
                action="store_true",
                help="Check whether a newer release of audiobook-dl is available and exit",
            ),
        ),
        config_key="check_updates",
        config_kind="bool",
    ),
    # Config-only top-level keys (not part of DownloadOptions)
    OptionSpec(
        name="ffmpeg_dir",
        default=None,
        in_download_options=False,
        config_key="ffmpeg_dir",
        config_kind="str",
    ),
)

OPTIONS_BY_NAME: Dict[str, OptionSpec] = {spec.name: spec for spec in OPTION_REGISTRY}
DOWNLOAD_OPTION_SPECS: Tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.in_download_options
)
CONFIG_OPTION_SPECS: Tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.config_key is not None
)
MERGEABLE_OPTION_SPECS: Tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.merge_from_config and spec.config_key is not None
)
GUI_OPTION_SPECS: Tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.gui is not None
)
PERSISTED_GUI_OPTION_SPECS: Tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.gui is not None and spec.gui.persisted_in_output_prefs
)
KNOWN_TOP_LEVEL_CONFIG_KEYS: FrozenSet[str] = frozenset(
    {"sources", "gui", "audiobookshelf"}
    | {spec.config_key for spec in CONFIG_OPTION_SPECS if spec.config_key is not None}
)

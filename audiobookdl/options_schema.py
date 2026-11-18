"""Declarative runtime option registry shared by Config, Pipeline, and Desktop GUI."""

from dataclasses import dataclass, field
import math
import os
import shlex
from typing import Any
from collections.abc import Callable, Mapping

from audiobookdl.utils import normalize_audio_bitrate
from audiobookdl.utils.http import DEFAULT_MAX_RETRIES


MEDIA_TYPE_CHOICES: tuple[str, ...] = ("auto", "audiobook", "ebook", "video", "both")
INTRO_CHAPTER_MODE_CHOICES: tuple[str, ...] = ("auto", "always", "off")
OUTPUT_PRESET_CHOICES: tuple[str, ...] = ("default", "audiobookshelf", "plex", "kavita")
GUI_OUTPUT_FORMATS: tuple[str, ...] = ("Original", "m4b", "mp3", "m4a", "mp4", "mkv", "flac", "opus")
GUI_MEDIA_TYPE_OPTIONS: tuple[str, ...] = ("Auto", "Audiobook", "Ebook", "Video", "Both")
GUI_INTRO_CHAPTER_MODES: tuple[str, ...] = (
    "Auto (detect intro)",
    "Always (first is 00)",
    "Off (start at 01)",
)
GUI_OVERWRITE_OPTIONS: tuple[str, ...] = ("Ask", "Overwrite", "Keep existing")

GUI_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "window_geometry",
        "window_maximized",
        "download_split_ratio",
        "active_page",
        "options_collapsed",
        "output_naming_collapsed",
        "output_dir",
        "output_template",
        "use_download_archive",
        "theme",
        "notify_on_complete",
        "clipboard_monitor",
        "auto_sync_enabled",
        "auto_sync_minutes",
        "auto_sync_mode",
        "auto_sync_targets",
        "queue_concurrency",
        "minimize_to_tray",
    }
)
SOURCE_CONFIG_KEYS: frozenset[str] = frozenset({"username", "password", "library", "cookie_file", "locale"})


def default_database_directory() -> str:
    """Return the default directory for sources to store data."""
    from audiobookdl.config import config_dir

    return os.path.join(config_dir(), "db")


def parse_series_selection(spec: str, total: int) -> list[int]:
    """
    Parse a 1-based comma/range selection string (e.g. `'1,3-5'`) into unique
    ordered 0-based indices bounded by `[0, total)`.
    """
    if total < 0:
        raise ValueError("total must be >= 0")
    cleaned = spec.strip()
    if not cleaned:
        raise ValueError("Series item selection cannot be empty")
    selected: list[int] = []
    seen: set[int] = set()
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


def _validate_str(key: str, raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, f"Warning: invalid '{key}' in config (expected string); ignoring"
    return raw, None


def _validate_bool(key: str, raw: Any) -> tuple[bool | None, str | None]:
    if not isinstance(raw, bool):
        return None, f"Warning: invalid '{key}' in config (expected boolean); ignoring"
    return raw, None


def _validate_int(key: str, raw: Any) -> tuple[int | None, str | None]:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw, None
    return None, f"Warning: invalid '{key}' in config (expected integer); ignoring"


def _validate_positive_int(key: str, raw: Any) -> tuple[int | None, str | None]:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw, None
    return None, f"Warning: invalid '{key}' in config (expected integer >= 1); ignoring"


def _validate_non_negative_int(key: str, raw: Any) -> tuple[int | None, str | None]:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw, None
    return None, f"Warning: invalid '{key}' in config (expected integer >= 0); ignoring"


def _validate_non_negative_float(key: str, raw: Any) -> tuple[float | None, str | None]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw), None
    return None, f"Warning: invalid '{key}' in config (expected number >= 0); ignoring"


def _validate_positive_float(key: str, raw: Any) -> tuple[float | None, str | None]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw), None
    return None, f"Warning: invalid '{key}' in config (expected number > 0); ignoring"


def _validate_silence_noise_db(key: str, raw: Any) -> tuple[float | None, str | None]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and -120.0 <= float(raw) <= 0.0:
        return float(raw), None
    return None, f"Warning: invalid '{key}' in config (expected dB between -120 and 0); ignoring"


def _validate_audio_bitrate(key: str, raw: Any) -> tuple[str | None, str | None]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    try:
        return normalize_audio_bitrate(val), None
    except ValueError:
        return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_enrich_providers(
    key: str,
    raw: Any,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    if not val.strip():
        return None, None
    from audiobookdl.utils.enrichment import parse_provider_list

    try:
        return ",".join(parse_provider_list(val, custom_providers=custom_providers)), None
    except ValueError:
        return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_media_type(key: str, raw: Any) -> tuple[str | None, str | None]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    normalized = val.strip().lower()
    if normalized in MEDIA_TYPE_CHOICES:
        return normalized, None
    return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_intro_chapter_mode(key: str, raw: Any) -> tuple[str | None, str | None]:
    val, warning = _validate_str(key, raw)
    if warning is not None or val is None:
        return None, warning
    normalized = val.strip().lower()
    if normalized in INTRO_CHAPTER_MODE_CHOICES:
        return normalized, None
    return None, f"Warning: invalid '{key}' in config ('{val}'); ignoring"


def _validate_limit_rate(key: str, raw: Any) -> tuple[str | None, str | None]:
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


def _validate_post_exec(key: str, raw: Any) -> tuple[str | list[str] | None, str | None]:
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, list) and raw and all(isinstance(part, str) and "\0" not in part for part in raw):
        cleaned = [part for part in raw if part]
        if cleaned:
            return cleaned, None
    return None, f"Warning: invalid '{key}' in config (expected command string or non-empty list of strings); ignoring"


_CONFIG_VALIDATORS: Mapping[str, Callable[[str, Any], tuple[Any, str | None]]] = {
    "str": _validate_str,
    "bool": _validate_bool,
    "int": _validate_int,
    "positive_int": _validate_positive_int,
    "non_negative_int": _validate_non_negative_int,
    "non_negative_float": _validate_non_negative_float,
    "positive_float": _validate_positive_float,
    "silence_noise_db": _validate_silence_noise_db,
    "audio_bitrate": _validate_audio_bitrate,
    "media_type": _validate_media_type,
    "intro_chapter_mode": _validate_intro_chapter_mode,
    "enrich_providers": _validate_enrich_providers,
    "limit_rate": _validate_limit_rate,
    "post_exec": _validate_post_exec,
}


def validate_config_value(
    key: str,
    value_kind: str,
    raw: Any,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[Any, str | None]:
    """Validate and normalize a raw TOML value according to `value_kind`."""
    if raw is None:
        return None, None
    if value_kind == "enrich_providers":
        return _validate_enrich_providers(key, raw, custom_providers=custom_providers)
    validator = _CONFIG_VALIDATORS.get(value_kind)
    if validator is None:
        raise ValueError(f"Unknown config value kind: {value_kind!r}")
    return validator(key, raw)


def _serialize_bool(val: Any) -> tuple[bool, Any]:
    return True, bool(val)


def _serialize_non_empty_str(val: Any) -> tuple[bool, Any]:
    return (True, val) if val else (False, None)


def _serialize_audio_bitrate(val: Any) -> tuple[bool, Any]:
    normalized = normalize_audio_bitrate(val)
    return (True, normalized) if normalized else (False, None)


def _serialize_media_type(val: Any) -> tuple[bool, Any]:
    normalized = str(val).strip().lower() if val else ""
    if normalized and normalized != "auto":
        return True, normalized
    return False, None


def _serialize_intro_chapter_mode(val: Any) -> tuple[bool, Any]:
    normalized = str(val).strip().lower() if val else ""
    if normalized in ("always", "off"):
        return True, normalized
    return False, None


def _serialize_optional_bool(val: Any) -> tuple[bool, Any]:
    if val is None:
        return False, None
    return True, bool(val)


def _serialize_int(val: Any) -> tuple[bool, Any]:
    if val is None:
        return False, None
    return True, int(val)


def _serialize_positive_int(val: Any) -> tuple[bool, Any]:
    if val is None:
        return False, None
    return True, int(val)


def _serialize_non_negative_int(val: Any) -> tuple[bool, Any]:
    if val is None or int(val) < 0:
        return False, None
    return True, int(val)


def _serialize_positive_float(val: Any) -> tuple[bool, Any]:
    if val is None or float(val) <= 0:
        return False, None
    return True, float(val)


def _serialize_float(val: Any) -> tuple[bool, Any]:
    if val is None:
        return False, None
    return True, float(val)


def _serialize_post_exec(val: Any) -> tuple[bool, Any]:
    if isinstance(val, list):
        cleaned = [str(part) for part in val if str(part)]
        return (True, cleaned) if cleaned else (False, None)
    return (True, str(val)) if val else (False, None)


def _gui_init_output_format(cfg_val: Any) -> str:
    val = cfg_val or "Original"
    return val if val in GUI_OUTPUT_FORMATS else "Original"


def _gui_read_output_format(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    return None if cleaned in ("", "Original") else cleaned


def _gui_init_media_type(cfg_val: Any) -> str:
    normalized = (cfg_val or "auto").strip().lower()
    return {
        "auto": "Auto",
        "audiobook": "Audiobook",
        "ebook": "Ebook",
        "video": "Video",
        "both": "Both",
    }.get(normalized, "Auto")


def _gui_read_media_type(raw: Any) -> str:
    cleaned = str(raw).strip().lower()
    return cleaned if cleaned in MEDIA_TYPE_CHOICES else "auto"


def _gui_init_intro_chapter_mode(cfg_val: Any) -> str:
    normalized = (cfg_val or "auto").strip().lower()
    return {
        "auto": "Auto (detect intro)",
        "always": "Always (first is 00)",
        "off": "Off (start at 01)",
    }.get(normalized, "Auto (detect intro)")


def _gui_read_intro_chapter_mode(raw: Any) -> str:
    cleaned = str(raw).strip().lower()
    if cleaned in ("always", "always (first is 00)", "force"):
        return "always"
    if cleaned in ("off", "off (start at 01)", "never"):
        return "off"
    return "auto"


def _gui_init_audio_bitrate(cfg_val: Any) -> str:
    cleaned = (cfg_val or "").strip()
    if not cleaned or cleaned.lower() == "auto":
        return "Auto"
    return cleaned


def _gui_read_audio_bitrate(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    return None if cleaned in ("", "Auto", "auto") else cleaned


def _gui_init_bool_default_true(cfg_val: Any) -> bool:
    return True if cfg_val is None else bool(cfg_val)


def _gui_init_bool_from_truthy(cfg_val: Any) -> bool:
    return bool(cfg_val)


def _gui_init_optional_str(cfg_val: Any) -> str:
    return "" if cfg_val is None else str(cfg_val)


def _gui_init_post_exec(cfg_val: Any) -> str:
    if isinstance(cfg_val, list):
        return shlex.join(str(part) for part in cfg_val if str(part))
    return "" if cfg_val is None else str(cfg_val)


def _gui_read_optional_str(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    return cleaned or None


def _gui_read_enrich_providers(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    from audiobookdl.utils.enrichment import parse_provider_list

    return ",".join(parse_provider_list(cleaned))


def _gui_read_limit_rate(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    from audiobookdl.utils.http import parse_byte_rate

    parse_byte_rate(cleaned)
    return cleaned


def _gui_read_decryption_key(raw: Any) -> str | None:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    from audiobookdl.utils.audiobook import parse_decryption_key_spec

    parse_decryption_key_spec(cleaned)
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


def _gui_read_overwrite(raw: Any) -> bool | None:
    cleaned = str(raw).strip().lower()
    if cleaned == "overwrite":
        return True
    if cleaned in ("keep existing", "no", "false"):
        return False
    return None


def _gui_init_max_workers(cfg_val: Any) -> str:
    return "" if cfg_val is None else str(cfg_val)


def _gui_init_int_str(cfg_val: Any) -> str:
    return "0" if cfg_val is None else str(int(cfg_val))


def _gui_read_int(raw: Any) -> int:
    cleaned = str(raw).strip()
    if not cleaned:
        return 0
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Expected whole number, got '{cleaned}'") from exc


def _gui_read_optional_positive_int(raw: Any) -> int | None:
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    try:
        parsed = int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Expected positive integer, got '{cleaned}'") from exc
    if parsed < 1:
        raise ValueError("Expected integer >= 1")
    return parsed


def _gui_read_max_workers(raw: Any) -> int | None:
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


def _gui_read_timeout(raw: Any) -> float | None:
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


def _gui_init_max_retries(cfg_val: Any) -> str:
    return str(DEFAULT_MAX_RETRIES) if cfg_val is None else str(int(cfg_val))


def _gui_read_max_retries(raw: Any) -> int:
    cleaned = str(raw).strip()
    if not cleaned:
        return DEFAULT_MAX_RETRIES
    try:
        parsed = int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Max retries must be a whole number >= 0, got '{cleaned}'") from exc
    if parsed < 0:
        raise ValueError("Max retries must be >= 0")
    return parsed


@dataclass(frozen=True)
class GuiBinding:
    """Declarative binding between a runtime option and a Tkinter variable."""

    var_attr: str
    var_type: str  # "bool" | "str"
    gui_default: Any
    from_config: Callable[[Any], Any] | None = None
    to_option: Callable[[Any], Any] | None = None
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
    """Single source of truth describing a runtime option across Config, Pipeline, and Desktop GUI."""

    name: str
    default: Any = None
    default_factory: Callable[[], Any] | None = None
    in_download_options: bool = True
    help: str | Callable[[], str] | None = None
    config_key: str | None = None
    config_kind: str | None = None
    toml_serializer: Callable[[Any], tuple[bool, Any]] | None = None
    merge_from_config: bool = False
    merge_only_if_default: bool = False
    merge_require_truthy: bool = False
    merge_cast: Callable[[Any], Any] | None = None
    gui: GuiBinding | None = None

    def resolve_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        return self.default

    def get_help(self) -> str:
        """Return resolved help/tooltip description for this option."""
        if self.help is None:
            return ""
        return self.help() if callable(self.help) else str(self.help)


OPTION_REGISTRY: tuple[OptionSpec, ...] = (
    OptionSpec(
        name="urls",
        default_factory=list,
        help="Urls to download from",
    ),
    OptionSpec(
        name="cookie_file",
        default=None,
        help="Path to Netscape cookie file",
    ),
    OptionSpec(
        name="combine",
        default=False,
        help="Combine output files into a single file",
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
        help="Output location template (default: {title})",
        config_key="output_template",
        config_kind="str",
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="output_preset",
        default=None,
        help="Named output folder/filename template preset",
    ),
    OptionSpec(
        name="remove_chars",
        default="",
        help="List of characters that will be removed from output path",
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
        help="Debug mode",
        config_key="debug",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
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
        help="Quiet mode",
    ),
    OptionSpec(
        name="cover",
        default=False,
        help="Download only cover",
        config_key="cover",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
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
        help="Don't include chapters in final file",
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
        help="Split downloaded audiobook into separate per-chapter files",
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
        help="Output file format",
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
        help="Media format to download when sources offer multiple formats (auto, audiobook, ebook, video, both)",
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
        help="Include verbose ffmpeg output in logs",
    ),
    OptionSpec(
        name="username",
        default=None,
        help="Username for source",
    ),
    OptionSpec(
        name="password",
        default=None,
        help="Password for source",
    ),
    OptionSpec(
        name="library",
        default=None,
        help="Library for source",
    ),
    OptionSpec(
        name="profile",
        default=None,
        help="Named service account profile from config (default: 'default')",
    ),
    OptionSpec(
        name="skip_downloaded",
        default=False,
        help="Skip downloading books if the output file or directory already exists",
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
        name="skip_unavailable",
        default=False,
        help="Skip unavailable or inaccessible books and continue bulk downloads",
        config_key="skip_unavailable",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="skip_unavailable_var",
            var_type="bool",
            gui_default=True,
            from_config=_gui_init_bool_default_true,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="download_archive",
        default=None,
        help="Path to SQLite download archive file to record and skip previously downloaded books",
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
        help="Disable reading and recording entries in the SQLite download archive (downloads.db)",
    ),
    OptionSpec(
        name="database_directory",
        default_factory=default_database_directory,
        help=lambda: f"Directory for sources to store data (default: {default_database_directory()})",
        config_key="database_directory",
        config_kind="str",
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="write_json_metadata",
        default=False,
        help="Write metadata in a separate json file",
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
        help="Save cover artwork as a separate sidecar image file",
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
        help="Write metadata as an OPF XML sidecar file (Audiobookshelf/Calibre)",
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
        help="Write Audiobookshelf-compatible metadata.json sidecar alongside downloaded files",
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
        help="Upload completed audiobooks to the configured Audiobookshelf server",
        config_key="upload_to_abs",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="upload_to_abs_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="write_cue",
        default=False,
        help="Write chapter timestamps as a CUE sheet sidecar file",
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
        help="Alternative location of config file",
    ),
    OptionSpec(
        name="resume",
        default=False,
        help="Resume interrupted downloads using existing parts and HTTP Range requests",
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
        help="Target AAC bitrate when converting to .m4b/.m4a (auto, 64k, 128k, 192k, etc.)",
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
        help="Overwrite policy for existing files or directories",
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
        name="series_items",
        default=None,
        help="1-based comma/range selection of series books to download (e.g. 1,3-5)",
    ),
    OptionSpec(
        name="max_workers",
        default=None,
        help="Maximum concurrent download/processing worker threads",
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
        help="Seconds to pause between consecutive books in a batch or series",
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
        help="Maximum aggregate download speed (e.g. 500K, 2M)",
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
        name="post_exec",
        default=None,
        help=(
            "Command (string or argv list) to execute after each completed audiobook (300s timeout; "
            "read AUDIOBOOK_DL_PATH/TITLE/AUTHOR/SOURCE/FORMAT env vars rather than interpolating titles)"
        ),
        config_key="post_exec",
        config_kind="post_exec",
        toml_serializer=_serialize_post_exec,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="post_exec_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_post_exec,
            to_option=_gui_read_optional_str,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="loudnorm",
        default=False,
        help="Apply EBU R128 loudness normalization during audio processing",
        config_key="loudnorm",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="loudnorm_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="mono",
        default=False,
        help="Downmix audio to mono (1 channel) for voice-optimized size",
        config_key="mono",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="mono_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="chapters_from_silence",
        default=False,
        help="Detect long pauses via FFmpeg silencedetect when no chapter markers exist",
        config_key="chapters_from_silence",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="chapters_from_silence_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="override_chapters",
        default=False,
        help="Replace existing source chapters when using metadata enrichment or silence detection",
        config_key="override_chapters",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="override_chapters_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="silence_noise_db",
        default=-35.0,
        help="Noise floor threshold in dB for FFmpeg silencedetect (e.g. -35.0)",
        config_key="silence_noise_db",
        config_kind="silence_noise_db",
        toml_serializer=_serialize_float,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_cast=float,
    ),
    OptionSpec(
        name="silence_min_seconds",
        default=1.8,
        help="Minimum silence duration in seconds for FFmpeg silencedetect",
        config_key="silence_min_seconds",
        config_kind="positive_float",
        toml_serializer=_serialize_positive_float,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_cast=float,
    ),
    OptionSpec(
        name="chapter_offset_ms",
        default=0,
        help="Shift non-initial chapter timestamps by N milliseconds",
        config_key="chapter_offset_ms",
        config_kind="int",
        toml_serializer=_serialize_int,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="chapter_offset_ms_var",
            var_type="str",
            gui_default="0",
            from_config=_gui_init_int_str,
            to_option=_gui_read_int,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="intro_chapter_mode",
        default="auto",
        help="Numbering mode when splitting chapters with an introduction/prologue first (auto, always, off)",
        config_key="intro_chapter_mode",
        config_kind="intro_chapter_mode",
        toml_serializer=_serialize_intro_chapter_mode,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="intro_chapter_mode_var",
            var_type="str",
            gui_default="Auto (detect intro)",
            from_config=_gui_init_intro_chapter_mode,
            to_option=_gui_read_intro_chapter_mode,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="square_cover",
        default=False,
        help="Pad cover artwork to a 1:1 square aspect ratio",
        config_key="square_cover",
        config_kind="bool",
        toml_serializer=_serialize_bool,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="square_cover_var",
            var_type="bool",
            gui_default=False,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="max_cover_size",
        default=None,
        help="Downscale oversized cover artwork so width and height do not exceed N pixels",
        config_key="max_cover_size",
        config_kind="positive_int",
        toml_serializer=_serialize_positive_int,
        merge_from_config=True,
        merge_only_if_default=True,
        gui=GuiBinding(
            var_attr="max_cover_size_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_optional_positive_int,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="proxy",
        default=None,
        help="HTTP/HTTPS proxy URL for requests (e.g. http://127.0.0.1:8080)",
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
        help="HTTP read timeout in seconds",
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
        name="max_retries",
        default=DEFAULT_MAX_RETRIES,
        help="Maximum automatic retry attempts with exponential backoff for failed requests (0 disables retries)",
        config_key="max_retries",
        config_kind="non_negative_int",
        toml_serializer=_serialize_non_negative_int,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_cast=int,
        gui=GuiBinding(
            var_attr="max_retries_var",
            var_type="str",
            gui_default=str(DEFAULT_MAX_RETRIES),
            from_config=_gui_init_max_retries,
            to_option=_gui_read_max_retries,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="impersonate",
        default=None,
        help="TLS/HTTP2 browser fingerprint to impersonate via optional curl_cffi (e.g. chrome, edge, safari)",
        config_key="impersonate",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
        gui=GuiBinding(
            var_attr="impersonate_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_optional_str,
            persisted_in_output_prefs=True,
        ),
    ),
    OptionSpec(
        name="decryption_key",
        default=None,
        help="Optional session-only clear decryption key (32/64-char hex or KID:KEY pairs) for CENC/HLS overrides",
        gui=GuiBinding(
            var_attr="decryption_key_var",
            var_type="str",
            gui_default="",
            from_config=_gui_init_optional_str,
            to_option=_gui_read_decryption_key,
            persisted_in_output_prefs=False,
        ),
    ),
    OptionSpec(
        name="locale",
        default=None,
        help="Optional regional locale code for sources such as Nextory (e.g. sv_SE, en_GB)",
        config_key="locale",
        config_kind="str",
        toml_serializer=_serialize_non_empty_str,
        merge_from_config=True,
        merge_only_if_default=True,
        merge_require_truthy=True,
    ),
    OptionSpec(
        name="service",
        default=None,
        help="Target service key",
    ),
    OptionSpec(
        name="enrich_metadata",
        default=False,
        help="Fill missing metadata, cover art, and chapters via Audnexus, iTunes, Google Books, and OpenLibrary",
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
        help="Comma-separated enrichment providers in priority order (audnexus,itunes,google,openlibrary, or custom:<name>)",
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
        help="Prompt interactively to select from ranked enrichment matches",
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
        help="Bypass reading cached enrichment responses and fetch fresh metadata",
    ),
    # Config-only top-level keys (not part of DownloadOptions)
    OptionSpec(
        name="check_updates",
        default=False,
        in_download_options=False,
        help="Check whether a newer release of audiobook-dl is available at startup",
        config_key="check_updates",
        config_kind="bool",
        toml_serializer=_serialize_bool,
    ),
    OptionSpec(
        name="ffmpeg_dir",
        default=None,
        in_download_options=False,
        config_key="ffmpeg_dir",
        config_kind="str",
    ),
)

OPTIONS_BY_NAME: dict[str, OptionSpec] = {spec.name: spec for spec in OPTION_REGISTRY}
DOWNLOAD_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.in_download_options
)
CONFIG_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.config_key is not None
)
MERGEABLE_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.merge_from_config and spec.config_key is not None
)
GUI_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.gui is not None
)
PERSISTED_GUI_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.gui is not None and spec.gui.persisted_in_output_prefs
)
KNOWN_TOP_LEVEL_CONFIG_KEYS: frozenset[str] = frozenset(
    {"sources", "gui", "audiobookshelf", "enrichment"}
    | {spec.config_key for spec in CONFIG_OPTION_SPECS if spec.config_key is not None}
)


@dataclass
class DownloadOptions:
    """Typed runtime options shared across Desktop GUI, pipeline, and sources."""

    urls: list[str] = field(default_factory=list)
    cookie_file: str | None = None
    combine: bool = False
    output_template: str = "{title}"
    output_preset: str | None = None
    remove_chars: str = ""
    debug: bool = False
    quiet: bool = False
    cover: bool = False
    no_chapters: bool = False
    split_chapters: bool = False
    output_format: str | None = None
    media_type: str = "auto"
    ffmpeg_output: bool = False
    username: str | None = None
    password: str | None = None
    library: str | None = None
    profile: str | None = None
    skip_downloaded: bool = False
    skip_unavailable: bool = False
    download_archive: str | None = None
    no_download_archive: bool = False
    database_directory: str = field(default_factory=default_database_directory)
    write_json_metadata: bool = False
    write_cover: bool = False
    write_opf: bool = False
    write_abs_metadata: bool = False
    upload_to_abs: bool = False
    write_cue: bool = False
    config_location: str | None = None
    resume: bool = False
    audio_bitrate: str | None = None
    overwrite: bool | None = None
    series_items: str | None = None
    max_workers: int | None = None
    book_delay: float = 0.0
    limit_rate: str | None = None
    post_exec: str | list[str] | None = None
    loudnorm: bool = False
    mono: bool = False
    chapters_from_silence: bool = False
    override_chapters: bool = False
    silence_noise_db: float = -35.0
    silence_min_seconds: float = 1.8
    chapter_offset_ms: int = 0
    intro_chapter_mode: str = "auto"
    square_cover: bool = False
    max_cover_size: int | None = None
    proxy: str | None = None
    timeout: float | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    impersonate: str | None = None
    decryption_key: str | None = None
    locale: str | None = None
    service: str | None = None
    enrich_metadata: bool = False
    enrich_providers: str | None = None
    enrich_interactive: bool = False
    no_enrich_cache: bool = False
    _explicit_fields: frozenset[str] = field(
        default_factory=frozenset,
        init=False,
        repr=False,
        compare=False,
    )


def build_default_options(**overrides: Any) -> DownloadOptions:
    """Build a `DownloadOptions` instance with defaults and optional explicit overrides."""
    opts = DownloadOptions(**overrides)
    opts._explicit_fields = frozenset(overrides.keys())
    return opts


@dataclass
class SyncOptions:
    """Options controlling library synchronization runs."""

    services: list[str] = field(default_factory=list)
    profile: str | None = None
    scan_only: bool = False
    dry_run: bool = False
    watch: bool = False
    interval_seconds: int = 3600
    output_template: str | None = None
    output_format: str | None = None
    archive_path: str | None = None
    config_location: str | None = None

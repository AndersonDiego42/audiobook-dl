"""GUI-side bindings between runtime options and Tkinter variables.

This module is the only place where `OptionSpec` entries are mapped to Tkinter
variables; core `options_schema.py` stays free of GUI concerns. Bindings are
keyed by `OptionSpec.name`.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import shlex
from typing import Any
from collections.abc import Callable

from audiobookdl.options_schema import (
    DEFAULT_MAX_RETRIES,
    MEDIA_TYPE_CHOICES,
    OPTION_REGISTRY,
    OptionSpec,
)
from audiobookdl.utils import normalize_audio_bitrate, normalize_sample_rate

GUI_OUTPUT_FORMATS: tuple[str, ...] = ("Original", "m4b", "mp3", "m4a", "mp4", "mkv", "flac", "opus")
GUI_MEDIA_TYPE_OPTIONS: tuple[str, ...] = ("Auto", "Audiobook", "Ebook", "Video", "Both")
GUI_INTRO_CHAPTER_MODES: tuple[str, ...] = (
    "Auto (detect intro)",
    "Always (first is 00)",
    "Off (start at 01)",
)
GUI_OVERWRITE_OPTIONS: tuple[str, ...] = ("Ask", "Overwrite", "Keep existing")


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
    if not cleaned or cleaned.lower() == "auto":
        return None
    return normalize_audio_bitrate(cleaned)


def _gui_init_sample_rate(cfg_val: Any) -> str:
    if cfg_val is None:
        return "Auto"
    try:
        normalized = normalize_sample_rate(cfg_val)
    except ValueError:
        return "Auto"
    return str(normalized) if normalized is not None else "Auto"


def _gui_read_sample_rate(raw: Any) -> int | None:
    if raw is None:
        return None
    return normalize_sample_rate(str(raw))


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
    tooltip: str | None = None

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

GUI_OPTION_BINDINGS: dict[str, GuiBinding] = {
    "audio_bitrate": GuiBinding(
        var_attr="audio_bitrate_var",
        var_type="str",
        gui_default="Auto",
        from_config=_gui_init_audio_bitrate,
        to_option=_gui_read_audio_bitrate,
    ),
    "book_delay": GuiBinding(
        var_attr="book_delay_var",
        var_type="str",
        gui_default="0",
        from_config=_gui_init_book_delay,
        to_option=_gui_read_book_delay,
    ),
    "chapter_offset_ms": GuiBinding(
        var_attr="chapter_offset_ms_var",
        var_type="str",
        gui_default="0",
        from_config=_gui_init_int_str,
        to_option=_gui_read_int,
    ),
    "chapters_from_silence": GuiBinding(
        var_attr="chapters_from_silence_var",
        var_type="bool",
        gui_default=False,
    ),
    "combine": GuiBinding(
        var_attr="combine_var",
        var_type="bool",
        gui_default=True,
        from_config=_gui_init_bool_default_true,
    ),
    "cover": GuiBinding(
        var_attr="cover_only_var",
        var_type="bool",
        gui_default=False,
    ),
    "debug": GuiBinding(
        var_attr="debug_var",
        var_type="bool",
        gui_default=False,
    ),
    "decryption_key": GuiBinding(
        var_attr="decryption_key_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_decryption_key,
    ),
    "download_archive": GuiBinding(
        var_attr="download_archive_var",
        var_type="bool",
        gui_default=False,
        from_config=_gui_init_bool_from_truthy,
        tooltip="Record completed downloads in the SQLite archive ledger (downloads.db) and skip already-archived books when 'Skip downloaded' is enabled",
    ),
    "enrich_interactive": GuiBinding(
        var_attr="enrich_interactive_var",
        var_type="bool",
        gui_default=False,
    ),
    "enrich_metadata": GuiBinding(
        var_attr="enrich_metadata_var",
        var_type="bool",
        gui_default=False,
    ),
    "enrich_providers": GuiBinding(
        var_attr="enrich_providers_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_enrich_providers,
    ),
    "impersonate": GuiBinding(
        var_attr="impersonate_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_optional_str,
    ),
    "intro_chapter_mode": GuiBinding(
        var_attr="intro_chapter_mode_var",
        var_type="str",
        gui_default="Auto (detect intro)",
        from_config=_gui_init_intro_chapter_mode,
        to_option=_gui_read_intro_chapter_mode,
    ),
    "limit_rate": GuiBinding(
        var_attr="limit_rate_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_limit_rate,
    ),
    "loudnorm": GuiBinding(
        var_attr="loudnorm_var",
        var_type="bool",
        gui_default=False,
    ),
    "max_cover_size": GuiBinding(
        var_attr="max_cover_size_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_optional_positive_int,
    ),
    "max_retries": GuiBinding(
        var_attr="max_retries_var",
        var_type="str",
        gui_default=str(DEFAULT_MAX_RETRIES),
        from_config=_gui_init_max_retries,
        to_option=_gui_read_max_retries,
    ),
    "max_workers": GuiBinding(
        var_attr="max_workers_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_max_workers,
        to_option=_gui_read_max_workers,
    ),
    "media_type": GuiBinding(
        var_attr="media_type_var",
        var_type="str",
        gui_default="Auto",
        from_config=_gui_init_media_type,
        to_option=_gui_read_media_type,
    ),
    "mono": GuiBinding(
        var_attr="mono_var",
        var_type="bool",
        gui_default=False,
    ),
    "no_chapters": GuiBinding(
        var_attr="no_chapters_var",
        var_type="bool",
        gui_default=False,
    ),
    "output_format": GuiBinding(
        var_attr="output_format_var",
        var_type="str",
        gui_default="Original",
        from_config=_gui_init_output_format,
        to_option=_gui_read_output_format,
    ),
    "override_chapters": GuiBinding(
        var_attr="override_chapters_var",
        var_type="bool",
        gui_default=False,
    ),
    "overwrite": GuiBinding(
        var_attr="overwrite_var",
        var_type="str",
        gui_default="Ask",
        from_config=_gui_init_overwrite,
        to_option=_gui_read_overwrite,
    ),
    "post_exec": GuiBinding(
        var_attr="post_exec_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_post_exec,
        to_option=_gui_read_optional_str,
        tooltip=(
            "Command to run after each completed audiobook (300s timeout; reads "
            "AUDIOBOOK_DL_PATH/TITLE/AUTHOR/SOURCE/FORMAT env vars; prefix with "
            "[argv, shell=False] or configure a TOML list to avoid shell expansion)"
        ),
    ),
    "proxy": GuiBinding(
        var_attr="proxy_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_optional_str,
        to_option=_gui_read_optional_str,
    ),
    "remove_chars": GuiBinding(
        var_attr="remove_chars_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_raw_str,
        to_option=_gui_read_raw_str,
        tooltip="Characters to strip from generated output folder and file names (e.g. :?*)",
    ),
    "resume": GuiBinding(
        var_attr="resume_var",
        var_type="bool",
        gui_default=False,
    ),
    "sample_rate": GuiBinding(
        var_attr="sample_rate_var",
        var_type="str",
        gui_default="Auto",
        from_config=_gui_init_sample_rate,
        to_option=_gui_read_sample_rate,
    ),
    "skip_downloaded": GuiBinding(
        var_attr="skip_downloaded_var",
        var_type="bool",
        gui_default=False,
    ),
    "skip_unavailable": GuiBinding(
        var_attr="skip_unavailable_var",
        var_type="bool",
        gui_default=True,
        from_config=_gui_init_bool_default_true,
    ),
    "split_chapters": GuiBinding(
        var_attr="split_chapters_var",
        var_type="bool",
        gui_default=False,
    ),
    "square_cover": GuiBinding(
        var_attr="square_cover_var",
        var_type="bool",
        gui_default=False,
    ),
    "timeout": GuiBinding(
        var_attr="timeout_var",
        var_type="str",
        gui_default="",
        from_config=_gui_init_timeout,
        to_option=_gui_read_timeout,
    ),
    "upload_to_abs": GuiBinding(
        var_attr="upload_to_abs_var",
        var_type="bool",
        gui_default=False,
    ),
    "write_abs_metadata": GuiBinding(
        var_attr="write_abs_metadata_var",
        var_type="bool",
        gui_default=False,
    ),
    "write_cover": GuiBinding(
        var_attr="write_cover_var",
        var_type="bool",
        gui_default=False,
    ),
    "write_cue": GuiBinding(
        var_attr="write_cue_var",
        var_type="bool",
        gui_default=False,
    ),
    "write_json_metadata": GuiBinding(
        var_attr="write_json_var",
        var_type="bool",
        gui_default=False,
    ),
    "write_opf": GuiBinding(
        var_attr="write_opf_var",
        var_type="bool",
        gui_default=False,
    ),
}

GUI_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in OPTION_REGISTRY if spec.name in GUI_OPTION_BINDINGS
)
PERSISTED_GUI_OPTION_SPECS: tuple[OptionSpec, ...] = tuple(
    spec for spec in GUI_OPTION_SPECS if spec.persist_output_pref
)

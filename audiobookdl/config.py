from audiobookdl import logging
from audiobookdl.credentials import (
    CredentialStore,
    PlaintextFallbackCredentialStore,
    get_credential_store,
)
from audiobookdl.exceptions import ConfigNotFound
from audiobookdl.options_schema import (
    CONFIG_OPTION_SPECS,
    GUI_CONFIG_KEYS,
    KNOWN_TOP_LEVEL_CONFIG_KEYS,
    OPTIONS_BY_NAME,
    SOURCE_CONFIG_KEYS,
    validate_config_value,
)
from audiobookdl.utils.fs import atomic_write_text, file_lock
from attrs import Factory, define

from typing import Any, Dict, Optional

import os
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

import appdirs


@define
class SourceConfig:
    """Stores configuration for source"""
    username: Optional[str]
    password: Optional[str]
    library: Optional[str]
    cookie_file: Optional[str]


@define
class GuiConfig:
    """Stores desktop GUI layout and output preferences"""
    window_geometry: Optional[str] = None
    output_dir: Optional[str] = None
    output_template: Optional[str] = None
    use_download_archive: Optional[bool] = None
    theme: Optional[str] = None
    notify_on_complete: Optional[bool] = None


@define
class Config:
    """audiobook-dl config"""
    sources: Dict[str, SourceConfig]
    output_template: Optional[str]
    database_directory: Optional[str]
    skip_downloaded: Optional[bool]
    output_format: Optional[str] = None
    media_type: Optional[str] = None
    combine: Optional[bool] = None
    ffmpeg_dir: Optional[str] = None
    resume: Optional[bool] = None
    audio_bitrate: Optional[str] = None
    overwrite: Optional[bool] = None
    max_workers: Optional[int] = None
    book_delay: Optional[float] = None
    proxy: Optional[str] = None
    timeout: Optional[float] = None
    download_archive: Optional[str] = None
    check_updates: Optional[bool] = None
    remove_chars: Optional[str] = None
    debug: Optional[bool] = None
    cover: Optional[bool] = None
    no_chapters: Optional[bool] = None
    split_chapters: Optional[bool] = None
    write_json_metadata: Optional[bool] = None
    write_cover: Optional[bool] = None
    write_opf: Optional[bool] = None
    write_cue: Optional[bool] = None
    enrich_metadata: Optional[bool] = None
    enrich_providers: Optional[str] = None
    enrich_interactive: Optional[bool] = None
    gui: GuiConfig = Factory(GuiConfig)

    def clone(self) -> "Config":
        """Return an isolated copy of this configuration for background jobs."""
        sources_copy = {
            name: SourceConfig(
                username=src.username,
                password=src.password,
                library=src.library,
                cookie_file=src.cookie_file,
            )
            for name, src in self.sources.items()
        }
        gui_copy = GuiConfig(
            window_geometry=self.gui.window_geometry,
            output_dir=self.gui.output_dir,
            output_template=self.gui.output_template,
            use_download_archive=self.gui.use_download_archive,
            theme=self.gui.theme,
            notify_on_complete=self.gui.notify_on_complete,
        )
        kwargs: Dict[str, Any] = {"sources": sources_copy, "gui": gui_copy}
        for spec in CONFIG_OPTION_SPECS:
            if spec.config_key is not None:
                kwargs[spec.config_key] = getattr(self, spec.config_key)
        return Config(**kwargs)


_STATIC_SOURCE_ALIASES = {
    "audiobooksdotcom": "audiobooks.com",
    "libby": "overdrive",
    "mofibo": "storytel",
    "scribd": "everand",
    "cloudlibrary": "yourcloudlibrary",
}


def normalize_source_key(raw_key: str) -> str:
    """Normalize a configured service key or alias (e.g. `scribd` -> `everand`)."""
    lowered = raw_key.strip().lower()
    if not lowered:
        return lowered
    if lowered in _STATIC_SOURCE_ALIASES:
        return _STATIC_SOURCE_ALIASES[lowered]
    try:
        from audiobookdl.sources import get_source_classes

        for cls in get_source_classes():
            if not cls.names:
                continue
            canonical = cls.names[0].lower()
            for alias in cls.names:
                if alias.lower() == lowered:
                    return canonical
    except Exception:
        pass
    return lowered


def load_config(
    overwrite: Optional[str],
    credential_store: Optional[CredentialStore] = None,
) -> Config:
    """
    Load config file from disk

    :param overwrite: Optional alternative location of config file
    :param credential_store: Optional credential store override
    :returns: Content of config file
    :raises: ConfigNotFound if location does not exists
    """
    config_location = get_config_location(overwrite)
    config_dict = read_config(config_location)
    store = credential_store or get_credential_store(config_location)
    _migrate_legacy_passwords(config_location, config_dict, store)
    return structure_config(config_location, config_dict, credential_store=store)


def _frozen_executable_dir() -> Optional[str]:
    """Return the directory containing the frozen executable, or `None`."""
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.abspath(sys.executable))


def is_portable_mode() -> bool:
    """
    Return `True` when running as a frozen executable in USB-portable mode.

    Portable mode activates when a `.portable` marker file or `audiobook-dl.toml`
    exists beside the frozen executable so config, credentials, and `db/` stay
    with the executable rather than writing to the host user's AppData folder.
    """
    exe_dir = _frozen_executable_dir()
    if exe_dir is None:
        return False
    return any(
        os.path.exists(os.path.join(exe_dir, marker))
        for marker in (".portable", "audiobook-dl.toml")
    )


def config_dir() -> str:
    """
    Get path of configuration directory

    :returns: Path of configuration directory
    """
    if is_portable_mode():
        exe_dir = _frozen_executable_dir()
        if exe_dir is not None:
            return exe_dir
    return appdirs.user_config_dir("audiobook-dl", "jo1gi")


def get_config_location(overwrite: Optional[str]) -> str:
    """
    Get path of configuration file

    :param overwrite: Overwrite of default configuration file location
    :returns: Path of configuration file
    """
    if overwrite:
        if not os.path.exists(overwrite):
            raise ConfigNotFound
        return overwrite
    return os.path.join(config_dir(), "audiobook-dl.toml")


def read_config(config_file: str) -> dict:
    """
    Read config from disk as dictionary

    :param location: Location of configuration file
    :returns: Content of config file as dictionary
    :raises: ConfigNotFound if location does not exists
    """
    if os.path.exists(config_file):
        with open(config_file, "rb") as f:
            config_dict = tomllib.load(f)
    else:
        config_dict = {}
    return config_dict


def _migrate_legacy_passwords(
    config_location: str,
    config_dict: Dict[str, Any],
    store: CredentialStore,
) -> None:
    """Move any plaintext passwords in `config_dict` into `store` and scrub TOML."""
    if not store.is_secure:
        return
    sources = config_dict.get("sources")
    if not isinstance(sources, dict):
        return
    migrated = False
    for source_name, values in list(sources.items()):
        if not isinstance(values, dict) or "password" not in values:
            continue
        legacy_password = values.pop("password")
        if legacy_password:
            try:
                store.set_password(
                    source_name,
                    values.get("username"),
                    str(legacy_password),
                )
            except Exception as exc:
                values["password"] = legacy_password
                logging.debug(
                    f"Failed to migrate password for '{source_name}' to OS keystore: {exc}"
                )
                continue
        if not any(v is not None and v != "" for v in values.values()):
            sources.pop(source_name, None)
        migrated = True
    if migrated:
        if not sources:
            config_dict.pop("sources", None)
        if os.path.exists(config_location):
            try:
                with file_lock(config_location):
                    _write_config_dict(config_location, config_dict)
            except OSError as exc:
                logging.debug(
                    f"Could not scrub migrated password from '{config_location}': {exc}"
                )


def structure_config(
    config_location: str,
    config_dict: dict,
    credential_store: Optional[CredentialStore] = None,
) -> Config:
    """
    Structure configuration file content as `Config`

    :param config_dict: Configuration file as a dictionary
    :param credential_store: Optional credential store for password lookup
    :returns: Configuration file `Config`
    """
    # Warn on unknown top-level keys
    for key in config_dict:
        if key not in KNOWN_TOP_LEVEL_CONFIG_KEYS:
            logging.log(f"Warning: unknown config key '{key}' ignored")

    # Add sources with normalized canonical keys and validation warnings
    sources: Dict[str, SourceConfig] = {}
    config_dir_path = os.path.dirname(os.path.abspath(config_location))
    raw_sources = config_dict.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, dict):
        logging.log("Warning: config '[sources]' must be a table; ignoring")
        raw_sources = {}
    if isinstance(raw_sources, dict):
        for raw_source_name, values in raw_sources.items():
            if not isinstance(values, dict):
                logging.log(f"Warning: config '[sources.{raw_source_name}]' must be a table; ignoring")
                continue
            source_name = normalize_source_key(raw_source_name)
            if source_name != raw_source_name.lower():
                logging.log(
                    f"Warning: config source alias '{raw_source_name}' normalized to '{source_name}'"
                )
            for key in values:
                if key not in SOURCE_CONFIG_KEYS:
                    logging.log(
                        f"Warning: unknown key '{key}' in '[sources.{raw_source_name}]' ignored"
                    )
            username = values.get("username")
            password = values.get("password")
            if password is None and credential_store is not None:
                password = credential_store.get_password(source_name, username)
            if password is None and credential_store is not None and raw_source_name != source_name:
                password = credential_store.get_password(raw_source_name, username)
            cookie_file = values.get("cookie_file")
            if cookie_file is not None and not isinstance(cookie_file, str):
                logging.log(f"Warning: invalid 'cookie_file' in '[sources.{raw_source_name}]'; ignoring")
                cookie_file = None
            if cookie_file and not os.path.isabs(cookie_file):
                cookie_file = os.path.normpath(os.path.join(config_dir_path, cookie_file))
            candidate_cfg = SourceConfig(
                username=username if isinstance(username, str) or username is None else str(username),
                password=password if isinstance(password, str) or password is None else str(password),
                library=values.get("library"),
                cookie_file=cookie_file,
            )
            if source_name in sources and raw_source_name.lower() != source_name:
                logging.log(
                    f"Warning: duplicate config for '{source_name}' from alias '{raw_source_name}'; keeping canonical entry"
                )
                continue
            sources[source_name] = candidate_cfg
    if credential_store is not None:
        for raw_service_key, stored_user in credential_store.stored_services().items():
            service_key = normalize_source_key(raw_service_key)
            if service_key not in sources:
                stored_pw = credential_store.get_password(raw_service_key, stored_user)
                if stored_pw is not None:
                    sources[service_key] = SourceConfig(
                        username=stored_user,
                        password=stored_pw,
                        library=None,
                        cookie_file=None,
                    )

    gui_section = config_dict.get("gui")
    gui_config = GuiConfig()
    if gui_section is not None and not isinstance(gui_section, dict):
        logging.log("Warning: config '[gui]' must be a table; ignoring")
    elif isinstance(gui_section, dict):
        for key in gui_section:
            if key not in GUI_CONFIG_KEYS:
                logging.log(f"Warning: unknown key '{key}' in '[gui]' ignored")
        use_archive_raw = gui_section.get("use_download_archive")
        if use_archive_raw is not None and not isinstance(use_archive_raw, bool):
            logging.log("Warning: config '[gui].use_download_archive' must be a boolean; ignoring")
            use_archive_val = None
        else:
            use_archive_val = use_archive_raw
        theme_raw = gui_section.get("theme")
        theme_val: Optional[str] = None
        if theme_raw is not None:
            if isinstance(theme_raw, str) and theme_raw.strip().lower() in ("system", "light", "dark"):
                theme_val = theme_raw.strip().lower()
            else:
                logging.log(
                    "Warning: config '[gui].theme' must be 'system', 'light', or 'dark'; ignoring"
                )
        notify_raw = gui_section.get("notify_on_complete")
        if notify_raw is not None and not isinstance(notify_raw, bool):
            logging.log("Warning: config '[gui].notify_on_complete' must be a boolean; ignoring")
            notify_val = None
        else:
            notify_val = notify_raw
        gui_config = GuiConfig(
            window_geometry=gui_section.get("window_geometry") if isinstance(gui_section.get("window_geometry"), str) else None,
            output_dir=gui_section.get("output_dir") if isinstance(gui_section.get("output_dir"), str) else None,
            output_template=gui_section.get("output_template") if isinstance(gui_section.get("output_template"), str) else None,
            use_download_archive=use_archive_val,
            theme=theme_val,
            notify_on_complete=notify_val,
        )

    parsed_options: Dict[str, Any] = {}
    for spec in CONFIG_OPTION_SPECS:
        key = spec.config_key
        if key is None or spec.config_kind is None:
            continue
        value, warning = validate_config_value(key, spec.config_kind, config_dict.get(key))
        if warning is not None:
            logging.log(warning)
        parsed_options[key] = value

    # Create config object
    return Config(
        sources=sources,
        gui=gui_config,
        **parsed_options,
    )


def _format_toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return _format_toml_value(key)


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = []
    for ch in text:
        if ch == "\\":
            escaped.append("\\\\")
        elif ch == '"':
            escaped.append('\\"')
        elif ch == "\b":
            escaped.append("\\b")
        elif ch == "\f":
            escaped.append("\\f")
        elif ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif ch == "\t":
            escaped.append("\\t")
        elif ord(ch) < 0x20:
            escaped.append(f"\\u{ord(ch):04x}")
        else:
            escaped.append(ch)
    return f'"{"".join(escaped)}"'


def _dump_toml(data: Dict[str, Any]) -> str:
    """Serialize config dictionary (top-level scalars + [gui] + [sources.<name>] tables) to TOML."""
    lines = []
    for key, value in data.items():
        if isinstance(value, dict) or value is None:
            continue
        lines.append(f"{_format_toml_key(key)} = {_format_toml_value(value)}")

    gui = data.get("gui")
    if isinstance(gui, dict):
        entries = [(k, v) for k, v in gui.items() if v is not None and v != ""]
        if entries:
            if lines:
                lines.append("")
            lines.append("[gui]")
            for k, v in entries:
                lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")

    sources = data.get("sources")
    if isinstance(sources, dict):
        for source_name, values in sources.items():
            if not isinstance(values, dict):
                continue
            entries = [(k, v) for k, v in values.items() if v is not None and v != ""]
            if not entries:
                continue
            if lines:
                lines.append("")
            lines.append(f"[sources.{_format_toml_key(source_name)}]")
            for k, v in entries:
                lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _has_plaintext_password(config_dict: Dict[str, Any]) -> bool:
    sources = config_dict.get("sources")
    if not isinstance(sources, dict):
        return False
    return any(
        isinstance(values, dict) and bool(values.get("password"))
        for values in sources.values()
    )


def _write_config_dict(config_location: str, config_dict: Dict[str, Any]) -> str:
    return atomic_write_text(
        config_location,
        _dump_toml(config_dict),
        private=_has_plaintext_password(config_dict),
    )


def save_source_credentials(
    service_key: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    library: Optional[str] = None,
    cookie_file: Optional[str] = None,
    overwrite: Optional[str] = None,
    credential_store: Optional[CredentialStore] = None,
) -> str:
    """
    Save or clear credentials for `service_key` in the configuration file.

    Non-secret fields (`username`, `library`, `cookie_file`) are stored in
    `audiobook-dl.toml` while `password` is persisted in the OS credential store.
    Preserves existing global settings and credentials for other services.
    Returns the path to the configuration file written.
    """
    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    store = credential_store or get_credential_store(config_location)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        sources_dict = dict(config_dict.get("sources") or {})
        prev_entry = sources_dict.get(canonical_key)
        prev_username = prev_entry.get("username") if isinstance(prev_entry, dict) else None

        entry: Dict[str, Any] = {
            k: v
            for k, v in (
                ("username", username),
                ("library", library),
                ("cookie_file", cookie_file),
            )
            if v is not None and v != ""
        }

        if prev_username != username:
            store.delete_password(canonical_key, prev_username)

        if password is not None and password != "":
            store.set_password(canonical_key, username, password)
            if (
                not store.is_secure
                and isinstance(store, PlaintextFallbackCredentialStore)
                and store.allow_plaintext
            ):
                entry["password"] = password
        else:
            store.delete_password(canonical_key, username)

        if entry:
            sources_dict[canonical_key] = entry
        else:
            sources_dict.pop(canonical_key, None)

        if sources_dict:
            config_dict["sources"] = sources_dict
        else:
            config_dict.pop("sources", None)

        return _write_config_dict(config_location, config_dict)


_UNSET: Any = object()


def save_output_preferences(
    output_format: Any = _UNSET,
    combine: Any = _UNSET,
    config_location: Optional[str] = None,
    *,
    audio_bitrate: Any = _UNSET,
    resume: Any = _UNSET,
    media_type: Any = _UNSET,
    skip_downloaded: Any = _UNSET,
    write_json_metadata: Any = _UNSET,
    enrich_metadata: Any = _UNSET,
    enrich_providers: Any = _UNSET,
    enrich_interactive: Any = _UNSET,
    no_chapters: Any = _UNSET,
    debug: Any = _UNSET,
    split_chapters: Any = _UNSET,
    write_opf: Any = _UNSET,
    write_cue: Any = _UNSET,
    write_cover: Any = _UNSET,
    remove_chars: Any = _UNSET,
    cover: Any = _UNSET,
    overwrite: Any = _UNSET,
    max_workers: Any = _UNSET,
    book_delay: Any = _UNSET,
    proxy: Any = _UNSET,
    timeout: Any = _UNSET,
) -> str:
    """Persist GUI output and toggle preferences in `audiobook-dl.toml`."""
    resolved_location = config_location or get_config_location(None)
    updates = {
        "output_format": output_format,
        "combine": combine,
        "audio_bitrate": audio_bitrate,
        "resume": resume,
        "media_type": media_type,
        "skip_downloaded": skip_downloaded,
        "write_json_metadata": write_json_metadata,
        "enrich_metadata": enrich_metadata,
        "enrich_providers": enrich_providers,
        "enrich_interactive": enrich_interactive,
        "no_chapters": no_chapters,
        "debug": debug,
        "split_chapters": split_chapters,
        "write_opf": write_opf,
        "write_cue": write_cue,
        "write_cover": write_cover,
        "remove_chars": remove_chars,
        "cover": cover,
        "overwrite": overwrite,
        "max_workers": max_workers,
        "book_delay": book_delay,
        "proxy": proxy,
        "timeout": timeout,
    }
    with file_lock(resolved_location):
        config_dict = read_config(resolved_location)
        for key, raw_value in updates.items():
            if raw_value is _UNSET:
                continue
            spec = OPTIONS_BY_NAME[key]
            if spec.toml_serializer is None:
                continue
            should_set, serialized = spec.toml_serializer(raw_value)
            if should_set:
                config_dict[key] = serialized
            else:
                config_dict.pop(key, None)
        return _write_config_dict(resolved_location, config_dict)


def save_gui_preferences(
    window_geometry: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_template: Optional[str] = None,
    overwrite: Optional[str] = None,
    *,
    use_download_archive: Optional[bool] = None,
    theme: Optional[str] = None,
    notify_on_complete: Optional[bool] = None,
) -> str:
    """Persist GUI window geometry, output prefs, archive toggle, theme, and notifications in `audiobook-dl.toml`."""
    config_location = overwrite or get_config_location(None)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        gui_dict = dict(config_dict.get("gui") or {})
        updates = {
            "window_geometry": window_geometry,
            "output_dir": output_dir,
            "output_template": output_template,
            "theme": theme.strip().lower() if isinstance(theme, str) else theme,
        }
        for key, val in updates.items():
            if val is None:
                continue
            cleaned = val.strip()
            if cleaned:
                gui_dict[key] = cleaned
            else:
                gui_dict.pop(key, None)
        if use_download_archive is not None:
            gui_dict["use_download_archive"] = bool(use_download_archive)
        if notify_on_complete is not None:
            gui_dict["notify_on_complete"] = bool(notify_on_complete)
        if gui_dict:
            config_dict["gui"] = gui_dict
        else:
            config_dict.pop("gui", None)
        return _write_config_dict(config_location, config_dict)


def save_ffmpeg_dir(
    ffmpeg_dir: Optional[str],
    overwrite: Optional[str] = None,
) -> str:
    """Persist or clear custom `ffmpeg_dir` in `audiobook-dl.toml`."""
    config_location = overwrite or get_config_location(None)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        cleaned = ffmpeg_dir.strip() if ffmpeg_dir else ""
        if cleaned:
            config_dict["ffmpeg_dir"] = cleaned
        else:
            config_dict.pop("ffmpeg_dir", None)
        return _write_config_dict(config_location, config_dict)

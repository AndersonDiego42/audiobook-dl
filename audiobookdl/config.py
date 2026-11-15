from audiobookdl import logging
from audiobookdl.credentials import (
    CredentialStore,
    PlaintextFallbackCredentialStore,
    get_credential_store,
)
from audiobookdl.exceptions import ConfigNotFound
from audiobookdl.utils import normalize_audio_bitrate
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


@define
class Config:
    """audiobook-dl config"""
    sources: Dict[str, SourceConfig]
    output_template: Optional[str]
    database_directory: Optional[str]
    skip_downloaded: Optional[bool]
    output_format: Optional[str] = None
    combine: Optional[bool] = None
    ffmpeg_dir: Optional[str] = None
    resume: Optional[bool] = None
    audio_bitrate: Optional[str] = None
    gui: GuiConfig = Factory(GuiConfig)


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
    # Add sources
    sources: Dict[str, SourceConfig] = {}
    config_dir_path = os.path.dirname(os.path.abspath(config_location))
    if "sources" in config_dict:
        for source_name, values in config_dict["sources"].items():
            username = values.get("username")
            password = values.get("password")
            if password is None and credential_store is not None:
                password = credential_store.get_password(source_name, username)
            cookie_file = values.get("cookie_file")
            if cookie_file and not os.path.isabs(cookie_file):
                cookie_file = os.path.normpath(os.path.join(config_dir_path, cookie_file))
            sources[source_name] = SourceConfig(
                username = username,
                password = password,
                library = values.get("library"),
                cookie_file = cookie_file
            )
    if credential_store is not None:
        for service_key, stored_user in credential_store.stored_services().items():
            if service_key not in sources:
                stored_pw = credential_store.get_password(service_key, stored_user)
                if stored_pw is not None:
                    sources[service_key] = SourceConfig(
                        username=stored_user,
                        password=stored_pw,
                        library=None,
                        cookie_file=None,
                    )
    gui_section = config_dict.get("gui")
    gui_config = GuiConfig()
    if isinstance(gui_section, dict):
        gui_config = GuiConfig(
            window_geometry=gui_section.get("window_geometry"),
            output_dir=gui_section.get("output_dir"),
            output_template=gui_section.get("output_template"),
        )
    raw_resume = config_dict.get("resume")
    resume_val = raw_resume if isinstance(raw_resume, bool) else None
    raw_bitrate = config_dict.get("audio_bitrate")
    audio_bitrate = normalize_audio_bitrate(raw_bitrate) if isinstance(raw_bitrate, str) else None
    # Create config object
    return Config(
        sources = sources,
        output_template = config_dict.get("output_template"),
        database_directory = config_dict.get("database_directory"),
        skip_downloaded = config_dict.get("skip_downloaded"),
        output_format = config_dict.get("output_format"),
        combine = config_dict.get("combine"),
        ffmpeg_dir = config_dict.get("ffmpeg_dir"),
        resume = resume_val,
        audio_bitrate = audio_bitrate,
        gui = gui_config,
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


def _write_config_dict(config_location: str, config_dict: Dict[str, Any]) -> str:
    parent = os.path.dirname(os.path.abspath(config_location))
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{config_location}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(_dump_toml(config_dict))
    os.replace(tmp_path, config_location)
    return config_location


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
    config_dict = read_config(config_location)
    sources_dict = dict(config_dict.get("sources") or {})
    prev_entry = sources_dict.get(service_key)
    prev_username = prev_entry.get("username") if isinstance(prev_entry, dict) else None
    store = credential_store or get_credential_store(config_location)

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
        store.delete_password(service_key, prev_username)

    if password is not None and password != "":
        store.set_password(service_key, username, password)
        if (
            not store.is_secure
            and isinstance(store, PlaintextFallbackCredentialStore)
            and store.allow_plaintext
        ):
            entry["password"] = password
    else:
        store.delete_password(service_key, username)

    if entry:
        sources_dict[service_key] = entry
    else:
        sources_dict.pop(service_key, None)

    if sources_dict:
        config_dict["sources"] = sources_dict
    else:
        config_dict.pop("sources", None)

    return _write_config_dict(config_location, config_dict)


_UNSET: Any = object()


def save_output_preferences(
    output_format: Optional[str],
    combine: bool,
    overwrite: Optional[str] = None,
    *,
    audio_bitrate: Any = _UNSET,
    resume: Any = _UNSET,
) -> str:
    """Persist GUI output format, combine, bitrate, and resume preferences in `audiobook-dl.toml`."""
    config_location = overwrite or get_config_location(None)
    config_dict = read_config(config_location)
    if output_format:
        config_dict["output_format"] = output_format
    else:
        config_dict.pop("output_format", None)
    config_dict["combine"] = bool(combine)
    if audio_bitrate is not _UNSET:
        normalized_bitrate = normalize_audio_bitrate(audio_bitrate)
        if normalized_bitrate:
            config_dict["audio_bitrate"] = normalized_bitrate
        else:
            config_dict.pop("audio_bitrate", None)
    if resume is not _UNSET:
        config_dict["resume"] = bool(resume)
    return _write_config_dict(config_location, config_dict)


def save_gui_preferences(
    window_geometry: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_template: Optional[str] = None,
    overwrite: Optional[str] = None,
) -> str:
    """Persist GUI window geometry, last output folder, and filename template in `audiobook-dl.toml`."""
    config_location = overwrite or get_config_location(None)
    config_dict = read_config(config_location)
    gui_dict = dict(config_dict.get("gui") or {})
    updates = {
        "window_geometry": window_geometry,
        "output_dir": output_dir,
        "output_template": output_template,
    }
    for key, val in updates.items():
        if val is None:
            continue
        cleaned = val.strip()
        if cleaned:
            gui_dict[key] = cleaned
        else:
            gui_dict.pop(key, None)
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
    config_dict = read_config(config_location)
    cleaned = ffmpeg_dir.strip() if ffmpeg_dir else ""
    if cleaned:
        config_dict["ffmpeg_dir"] = cleaned
    else:
        config_dict.pop("ffmpeg_dir", None)
    return _write_config_dict(config_location, config_dict)

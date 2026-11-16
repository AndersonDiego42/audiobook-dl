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
    PERSISTED_GUI_OPTION_SPECS,
    SOURCE_CONFIG_KEYS,
    validate_config_value,
)
from audiobookdl.utils.fs import atomic_write_text, file_lock
from attrs import Factory, define

from typing import Any
from collections.abc import Sequence

import os
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

import platformdirs


@define
class SourceConfig:
    """Stores configuration for source"""
    username: str | None
    password: str | None
    library: str | None
    cookie_file: str | None


@define
class GuiConfig:
    """Stores desktop GUI layout and output preferences"""
    window_geometry: str | None = None
    output_dir: str | None = None
    output_template: str | None = None
    use_download_archive: bool | None = None
    theme: str | None = None
    notify_on_complete: bool | None = None


@define
class AudiobookshelfConfig:
    """Audiobookshelf server connection and sync configuration"""
    url: str | None = None
    api_key: str | None = None
    library_id: str | None = None
    auto_upload: bool = False
    auto_scan: bool = True


@define
class Config:
    """audiobook-dl config"""
    sources: dict[str, SourceConfig]
    output_template: str | None
    database_directory: str | None
    skip_downloaded: bool | None
    output_format: str | None = None
    media_type: str | None = None
    combine: bool | None = None
    ffmpeg_dir: str | None = None
    resume: bool | None = None
    audio_bitrate: str | None = None
    overwrite: bool | None = None
    max_workers: int | None = None
    book_delay: float | None = None
    limit_rate: str | None = None
    proxy: str | None = None
    timeout: float | None = None
    impersonate: str | None = None
    post_exec: str | None = None
    loudnorm: bool | None = None
    mono: bool | None = None
    chapters_from_silence: bool | None = None
    chapter_offset_ms: int | None = None
    square_cover: bool | None = None
    max_cover_size: int | None = None
    download_archive: str | None = None
    check_updates: bool | None = None
    remove_chars: str | None = None
    debug: bool | None = None
    cover: bool | None = None
    no_chapters: bool | None = None
    split_chapters: bool | None = None
    write_json_metadata: bool | None = None
    write_cover: bool | None = None
    write_opf: bool | None = None
    write_abs_metadata: bool | None = None
    upload_to_abs: bool | None = None
    write_cue: bool | None = None
    enrich_metadata: bool | None = None
    enrich_providers: str | None = None
    enrich_interactive: bool | None = None
    gui: GuiConfig = Factory(GuiConfig)
    audiobookshelf: AudiobookshelfConfig = Factory(AudiobookshelfConfig)

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
        abs_copy = AudiobookshelfConfig(
            url=self.audiobookshelf.url,
            api_key=self.audiobookshelf.api_key,
            library_id=self.audiobookshelf.library_id,
            auto_upload=self.audiobookshelf.auto_upload,
            auto_scan=self.audiobookshelf.auto_scan,
        )
        kwargs: dict[str, Any] = {
            "sources": sources_copy,
            "gui": gui_copy,
            "audiobookshelf": abs_copy,
        }
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


DEFAULT_PROFILE = "default"


def normalize_profile_name(profile: str | None) -> str:
    """Normalize a source profile identifier, defaulting empty/None to `'default'`."""
    if profile is None:
        return DEFAULT_PROFILE
    cleaned = profile.strip().lower()
    return cleaned or DEFAULT_PROFILE


def make_source_profile_key(service_key: str, profile: str | None = None) -> str:
    """Build canonical `Config.sources` / `CredentialStore` key for `(service_key, profile)`."""
    canonical = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    if norm_profile == DEFAULT_PROFILE:
        return canonical
    return f"{canonical}:{norm_profile}"


def split_source_profile_key(raw_key: str) -> tuple[str, str]:
    """Split a `Config.sources` or vault key into `(canonical_service, profile)`."""
    if ":" in raw_key:
        service_part, profile_part = raw_key.split(":", 1)
        return normalize_source_key(service_part), normalize_profile_name(profile_part)
    return normalize_source_key(raw_key), DEFAULT_PROFILE


def get_source_config(
    config: Config,
    service_key: str,
    profile: str | None = None,
    aliases: Sequence[str] = (),
) -> SourceConfig | None:
    """Look up `SourceConfig` for `(service_key, profile)` checking canonical and alias names."""
    norm_profile = normalize_profile_name(profile)
    candidates = [service_key, *(a for a in aliases if a)]
    for candidate in candidates:
        key = make_source_profile_key(candidate, norm_profile)
        found = config.sources.get(key)
        if found is not None:
            return found
    return None


def list_source_profiles(config: Config, service_key: str) -> list[str]:
    """Return configured profile names for `service_key`, with `'default'` first if present."""
    canonical = normalize_source_key(service_key)
    profiles: list[str] = []
    for raw_key in config.sources:
        svc, prof = split_source_profile_key(raw_key)
        if svc == canonical and prof not in profiles:
            profiles.append(prof)
    profiles.sort(key=lambda p: (0 if p == DEFAULT_PROFILE else 1, p))
    return profiles


def list_configured_source_profiles(config: Config) -> list[tuple[str, str, SourceConfig]]:
    """Return all configured `(service_key, profile, SourceConfig)` entries sorted deterministically."""
    out: list[tuple[str, str, SourceConfig]] = []
    for raw_key, src_cfg in config.sources.items():
        svc, prof = split_source_profile_key(raw_key)
        out.append((svc, prof, src_cfg))
    out.sort(key=lambda item: (item[0], 0 if item[1] == DEFAULT_PROFILE else 1, item[1]))
    return out


def load_config(
    overwrite: str | None,
    credential_store: CredentialStore | None = None,
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


def _frozen_executable_dir() -> str | None:
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


def _env_config_path() -> str | None:
    """Return configured path override from `AUDIOBOOK_DL_CONFIG` (or `AUDIOBOOKDL_CONFIG`), if set."""
    raw = os.environ.get("AUDIOBOOK_DL_CONFIG") or os.environ.get("AUDIOBOOKDL_CONFIG")
    if not raw:
        return None
    cleaned = raw.strip()
    return os.path.abspath(os.path.expanduser(cleaned)) if cleaned else None


def config_dir() -> str:
    """
    Get path of configuration directory

    :returns: Path of configuration directory
    """
    env_cfg = _env_config_path()
    if env_cfg is not None:
        return os.path.dirname(env_cfg)
    if is_portable_mode():
        exe_dir = _frozen_executable_dir()
        if exe_dir is not None:
            return exe_dir
    return platformdirs.user_config_dir("audiobook-dl", "jo1gi")


def get_config_location(overwrite: str | None) -> str:
    """
    Get path of configuration file

    :param overwrite: Overwrite of default configuration file location
    :returns: Path of configuration file
    """
    if overwrite:
        if not os.path.exists(overwrite):
            raise ConfigNotFound
        return overwrite
    env_cfg = _env_config_path()
    if env_cfg is not None:
        return env_cfg
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


def _migrate_profile_password(
    profile_key: str,
    values: dict[str, Any],
    store: CredentialStore,
) -> bool:
    """Move plaintext `password` out of `values` into `store`; return True if modified."""
    if "password" not in values:
        return False
    legacy_password = values.pop("password")
    if legacy_password:
        try:
            store.set_password(
                profile_key,
                values.get("username"),
                str(legacy_password),
            )
        except Exception as exc:
            values["password"] = legacy_password
            logging.debug(
                f"Failed to migrate password for '{profile_key}' to OS keystore: {exc}"
            )
            return False
    return True


def _migrate_legacy_passwords(
    config_location: str,
    config_dict: dict[str, Any],
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
        if not isinstance(values, dict):
            continue
        base_svc, base_prof = split_source_profile_key(source_name)
        if _migrate_profile_password(
            make_source_profile_key(base_svc, base_prof),
            values,
            store,
        ):
            migrated = True
        profiles = values.get("profiles")
        if isinstance(profiles, dict):
            for prof_name, prof_values in list(profiles.items()):
                if not isinstance(prof_values, dict):
                    continue
                prof_key = make_source_profile_key(base_svc, str(prof_name))
                if _migrate_profile_password(prof_key, prof_values, store):
                    migrated = True
                if not any(v is not None and v != "" for v in prof_values.values()):
                    profiles.pop(prof_name, None)
            if not profiles:
                values.pop("profiles", None)
        if not any(
            k != "profiles" and v is not None and v != ""
            for k, v in values.items()
        ) and not values.get("profiles"):
            sources.pop(source_name, None)
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


def _build_source_config_from_dict(
    *,
    section_label: str,
    raw_source_name: str,
    profile_key: str,
    values: dict[str, Any],
    config_dir_path: str,
    credential_store: CredentialStore | None,
    allowed_keys: Sequence[str],
) -> SourceConfig:
    """Validate and construct a `SourceConfig` from a parsed TOML source/profile table."""
    for key in values:
        if key not in allowed_keys:
            logging.log(
                f"Warning: unknown key '{key}' in '{section_label}' ignored"
            )
    username = values.get("username")
    password = values.get("password")
    if password is None and credential_store is not None:
        password = credential_store.get_password(profile_key, username)
    if password is None and credential_store is not None and raw_source_name != profile_key:
        password = credential_store.get_password(raw_source_name, username)
    cookie_file = values.get("cookie_file")
    if cookie_file is not None and not isinstance(cookie_file, str):
        logging.log(f"Warning: invalid 'cookie_file' in '{section_label}'; ignoring")
        cookie_file = None
    if cookie_file and not os.path.isabs(cookie_file):
        cookie_file = os.path.normpath(os.path.join(config_dir_path, cookie_file))
    return SourceConfig(
        username=username if isinstance(username, str) or username is None else str(username),
        password=password if isinstance(password, str) or password is None else str(password),
        library=values.get("library"),
        cookie_file=cookie_file,
    )


def structure_config(
    config_location: str,
    config_dict: dict,
    credential_store: CredentialStore | None = None,
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
    sources: dict[str, SourceConfig] = {}
    config_dir_path = os.path.dirname(os.path.abspath(config_location))
    raw_sources = config_dict.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, dict):
        logging.log("Warning: config '[sources]' must be a table; ignoring")
        raw_sources = {}
    if isinstance(raw_sources, dict):
        allowed_with_profiles = tuple(SOURCE_CONFIG_KEYS) + ("profiles",)
        for raw_source_name, values in raw_sources.items():
            if not isinstance(values, dict):
                logging.log(f"Warning: config '[sources.{raw_source_name}]' must be a table; ignoring")
                continue
            base_service, base_profile = split_source_profile_key(raw_source_name)
            if base_service != raw_source_name.lower() and base_profile == DEFAULT_PROFILE:
                logging.log(
                    f"Warning: config source alias '{raw_source_name}' normalized to '{base_service}'"
                )
            has_scalar_fields = any(
                k in values for k in values if k != "profiles"
            )
            if has_scalar_fields:
                profile_key = make_source_profile_key(base_service, base_profile)
                candidate_cfg = _build_source_config_from_dict(
                    section_label=f"[sources.{raw_source_name}]",
                    raw_source_name=raw_source_name,
                    profile_key=profile_key,
                    values=values,
                    config_dir_path=config_dir_path,
                    credential_store=credential_store,
                    allowed_keys=allowed_with_profiles,
                )
                if profile_key in sources and raw_source_name.lower() != profile_key:
                    logging.log(
                        f"Warning: duplicate config for '{profile_key}' from alias '{raw_source_name}'; keeping canonical entry"
                    )
                else:
                    sources[profile_key] = candidate_cfg
            else:
                for key in values:
                    if key not in allowed_with_profiles:
                        logging.log(
                            f"Warning: unknown key '{key}' in '[sources.{raw_source_name}]' ignored"
                        )

            raw_profiles = values.get("profiles")
            if raw_profiles is not None and not isinstance(raw_profiles, dict):
                logging.log(
                    f"Warning: config '[sources.{raw_source_name}.profiles]' must be a table; ignoring"
                )
            elif isinstance(raw_profiles, dict):
                for raw_prof_name, prof_values in raw_profiles.items():
                    section_label = f"[sources.{raw_source_name}.profiles.{raw_prof_name}]"
                    if not isinstance(prof_values, dict):
                        logging.log(f"Warning: config '{section_label}' must be a table; ignoring")
                        continue
                    prof_key = make_source_profile_key(base_service, str(raw_prof_name))
                    prof_cfg = _build_source_config_from_dict(
                        section_label=section_label,
                        raw_source_name=f"{raw_source_name}:{raw_prof_name}",
                        profile_key=prof_key,
                        values=prof_values,
                        config_dir_path=config_dir_path,
                        credential_store=credential_store,
                        allowed_keys=tuple(SOURCE_CONFIG_KEYS),
                    )
                    if prof_key in sources and raw_source_name.lower() != base_service:
                        logging.log(
                            f"Warning: duplicate config for '{prof_key}' from alias '{raw_source_name}'; keeping canonical entry"
                        )
                        continue
                    sources[prof_key] = prof_cfg
    if credential_store is not None:
        for raw_service_key, stored_user in credential_store.stored_services().items():
            if raw_service_key.startswith("integration:"):
                continue
            svc, prof = split_source_profile_key(raw_service_key)
            profile_key = make_source_profile_key(svc, prof)
            if profile_key not in sources:
                stored_pw = credential_store.get_password(raw_service_key, stored_user)
                if stored_pw is not None:
                    sources[profile_key] = SourceConfig(
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
        theme_val: str | None = None
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

    abs_section = config_dict.get("audiobookshelf")
    abs_config = AudiobookshelfConfig()
    if abs_section is not None and not isinstance(abs_section, dict):
        logging.log("Warning: config '[audiobookshelf]' must be a table; ignoring")
    else:
        abs_dict = abs_section if isinstance(abs_section, dict) else {}
        for key in abs_dict:
            if key not in {"url", "api_key", "library_id", "auto_upload", "auto_scan"}:
                logging.log(f"Warning: unknown key '{key}' in '[audiobookshelf]' ignored")
        abs_url = abs_dict.get("url").strip().rstrip("/") if isinstance(abs_dict.get("url"), str) else None
        if abs_url == "":
            abs_url = None
        abs_lib = abs_dict.get("library_id").strip() if isinstance(abs_dict.get("library_id"), str) else None
        if abs_lib == "":
            abs_lib = None
        abs_auto_upload = abs_dict.get("auto_upload") if isinstance(abs_dict.get("auto_upload"), bool) else False
        abs_auto_scan = abs_dict.get("auto_scan") if isinstance(abs_dict.get("auto_scan"), bool) else True
        abs_key_raw = abs_dict.get("api_key")
        abs_key: str | None = abs_key_raw.strip() if isinstance(abs_key_raw, str) and abs_key_raw.strip() else None
        abs_account = abs_url or "api"
        if credential_store is not None:
            if abs_key is not None and credential_store.is_secure:
                try:
                    credential_store.set_password("integration:audiobookshelf", abs_account, abs_key)
                except Exception as exc:
                    logging.log(f"Warning: could not store Audiobookshelf token in OS vault: {exc}")
            elif abs_key is None:
                abs_key = credential_store.get_password("integration:audiobookshelf", abs_account)
                if abs_key is None and abs_account != "api":
                    abs_key = credential_store.get_password("integration:audiobookshelf", "api")
        abs_config = AudiobookshelfConfig(
            url=abs_url,
            api_key=abs_key,
            library_id=abs_lib,
            auto_upload=abs_auto_upload,
            auto_scan=abs_auto_scan,
        )

    parsed_options: dict[str, Any] = {}
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
        audiobookshelf=abs_config,
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


def _split_toml_section_header(header: str) -> tuple[str, ...] | None:
    """Parse a dotted TOML `[section.sub]` header into key segments."""
    text = header.strip()
    if not text:
        return None
    parts: list[str] = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        if text[idx] in ('"', "'"):
            quote_ch = text[idx]
            idx += 1
            buf = []
            while idx < len(text) and text[idx] != quote_ch:
                if text[idx] == "\\" and quote_ch == '"' and idx + 1 < len(text):
                    idx += 1
                buf.append(text[idx])
                idx += 1
            if idx >= len(text):
                return None
            idx += 1
            parts.append("".join(buf))
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx < len(text):
                if text[idx] != ".":
                    return None
                idx += 1
        else:
            start = idx
            while idx < len(text) and text[idx] != ".":
                idx += 1
            seg = text[start:idx].strip()
            if not seg:
                return None
            parts.append(seg)
            if idx < len(text) and text[idx] == ".":
                idx += 1
    return tuple(parts) if parts else None


def _strip_toml_assignment_key(line: str) -> tuple[str, str] | None:
    """Return `(key_name, trailing_comment)` if `line` is a single-line TOML assignment."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    eq_idx = stripped.find("=")
    if eq_idx <= 0:
        return None
    raw_key = stripped[:eq_idx].strip()
    if len(raw_key) >= 2 and raw_key[0] in ('"', "'") and raw_key[-1] == raw_key[0]:
        key_name = raw_key[1:-1]
    elif re.fullmatch(r"[A-Za-z0-9_-]+", raw_key):
        key_name = raw_key
    else:
        return None
    rhs = stripped[eq_idx + 1 :]
    in_quote: str | None = None
    escaped = False
    trailing = ""
    for pos, ch in enumerate(rhs):
        if escaped:
            escaped = False
            continue
        if in_quote == '"' and ch == "\\":
            escaped = True
            continue
        if ch in ('"', "'"):
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
        elif ch == "#" and in_quote is None:
            trailing = "  " + rhs[pos:].rstrip()
            break
    if in_quote is not None:
        return None
    return key_name, trailing


def _iter_toml_sections(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], list[tuple[str, Any]]]]:
    """Collect `(section_path, scalar_items)` pairs in deterministic order for all nested dicts."""
    scalars: list[tuple[str, Any]] = []
    sub_sections: list[tuple[tuple[str, ...], list[tuple[str, Any]]]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            sub_sections.extend(_iter_toml_sections(v, prefix + (str(k),)))
        elif v is not None and v != "":
            scalars.append((str(k), v))
    sections: list[tuple[tuple[str, ...], list[tuple[str, Any]]]] = []
    if prefix:
        if scalars:
            sections.append((prefix, scalars))
    elif scalars:
        sections.append(((), scalars))
    sections.extend(sub_sections)
    return sections


def _merge_toml_preserving_comments(existing_text: str, data: dict[str, Any]) -> str:
    """
    Update TOML `existing_text` with `data` while preserving `#` comments, blank lines,
    key order, inline comments, and unknown user sections.
    """
    target_sections = dict(_iter_toml_sections(data))
    emitted_keys: dict[tuple[str, ...], set[str]] = {sec: set() for sec in target_sections}
    out_lines: list[str] = []
    current_section: tuple[str, ...] = ()

    def _flush_missing_for_section(sec: tuple[str, ...]) -> None:
        if sec not in target_sections:
            return
        seen = emitted_keys.setdefault(sec, set())
        missing = [(k, v) for k, v in target_sections[sec] if k not in seen]
        if not missing:
            return
        # Insert new keys before any trailing blank lines at the end of the section.
        trailing_blanks: list[str] = []
        while out_lines and out_lines[-1].strip() == "":
            trailing_blanks.append(out_lines.pop())
        for k, v in missing:
            out_lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
            seen.add(k)
        out_lines.extend(reversed(trailing_blanks))

    for raw_line in existing_text.splitlines():
        sec_match = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", raw_line)
        if sec_match:
            _flush_missing_for_section(current_section)
            parsed_sec = _split_toml_section_header(sec_match.group(1))
            if parsed_sec is not None:
                current_section = parsed_sec
            out_lines.append(raw_line)
            continue

        parsed_assign = _strip_toml_assignment_key(raw_line)
        if parsed_assign is not None:
            key_name, trailing_comment = parsed_assign
            sec_entries = dict(target_sections.get(current_section, []))
            if key_name in sec_entries:
                val = sec_entries[key_name]
                indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
                out_lines.append(
                    f"{indent}{_format_toml_key(key_name)} = {_format_toml_value(val)}{trailing_comment}"
                )
                emitted_keys.setdefault(current_section, set()).add(key_name)
            # Keys removed from target_sections are intentionally dropped cleanly.
            continue

        out_lines.append(raw_line)

    _flush_missing_for_section(current_section)

    for sec, entries in _iter_toml_sections(data):
        if sec == ():
            continue
        seen = emitted_keys.get(sec, set())
        remaining = [(k, v) for k, v in entries if k not in seen]
        if not remaining:
            continue
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.append(f"[{'.'.join(_format_toml_key(part) for part in sec)}]")
        for k, v in remaining:
            out_lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")

    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize config dictionary (top-level scalars + all nested tables) to TOML."""
    lines: list[str] = []
    for sec, entries in _iter_toml_sections(data):
        if not entries:
            continue
        if sec:
            if lines:
                lines.append("")
            lines.append(f"[{'.'.join(_format_toml_key(part) for part in sec)}]")
        for k, v in entries:
            lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _has_plaintext_password(config_dict: dict[str, Any]) -> bool:
    sources = config_dict.get("sources")
    if not isinstance(sources, dict):
        return False
    for values in sources.values():
        if not isinstance(values, dict):
            continue
        if values.get("password"):
            return True
        profiles = values.get("profiles")
        if isinstance(profiles, dict) and any(
            isinstance(pv, dict) and bool(pv.get("password"))
            for pv in profiles.values()
        ):
            return True
    return False


def _write_config_dict(config_location: str, config_dict: dict[str, Any]) -> str:
    if os.path.isfile(config_location):
        try:
            with open(config_location, encoding="utf-8") as fh:
                existing_text = fh.read()
            rendered = _merge_toml_preserving_comments(existing_text, config_dict)
        except OSError:
            rendered = _dump_toml(config_dict)
    else:
        rendered = _dump_toml(config_dict)
    return atomic_write_text(
        config_location,
        rendered,
        private=_has_plaintext_password(config_dict),
    )


def save_source_credentials(
    service_key: str,
    username: str | None = None,
    password: str | None = None,
    library: str | None = None,
    cookie_file: str | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
    *,
    profile: str | None = None,
) -> str:
    """
    Save or clear credentials for `(service_key, profile)` in the configuration file.

    Non-secret fields (`username`, `library`, `cookie_file`) are stored in
    `audiobook-dl.toml` while `password` is persisted in the OS credential store.
    Preserves existing global settings and credentials for other services/profiles.
    Returns the path to the configuration file written.
    """
    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    vault_key = make_source_profile_key(canonical_key, norm_profile)
    store = credential_store or get_credential_store(config_location)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        sources_dict = dict(config_dict.get("sources") or {})
        service_table = dict(sources_dict.get(canonical_key) or {})

        if norm_profile == DEFAULT_PROFILE:
            prev_entry = service_table
        else:
            profiles_dict = dict(service_table.get("profiles") or {})
            prev_entry = profiles_dict.get(norm_profile) if isinstance(profiles_dict.get(norm_profile), dict) else {}
        prev_username = prev_entry.get("username") if isinstance(prev_entry, dict) else None

        entry: dict[str, Any] = {
            k: v
            for k, v in (
                ("username", username),
                ("library", library),
                ("cookie_file", cookie_file),
            )
            if v is not None and v != ""
        }

        if prev_username != username:
            store.delete_password(vault_key, prev_username)

        if password is not None and password != "":
            store.set_password(vault_key, username, password)
            if (
                not store.is_secure
                and isinstance(store, PlaintextFallbackCredentialStore)
                and store.allow_plaintext
            ):
                entry["password"] = password
        else:
            store.delete_password(vault_key, username)

        if norm_profile == DEFAULT_PROFILE:
            existing_profiles = service_table.get("profiles")
            new_service_table = dict(entry)
            if isinstance(existing_profiles, dict) and existing_profiles:
                new_service_table["profiles"] = existing_profiles
            if new_service_table:
                sources_dict[canonical_key] = new_service_table
            else:
                sources_dict.pop(canonical_key, None)
        else:
            profiles_dict = dict(service_table.get("profiles") or {})
            if entry:
                profiles_dict[norm_profile] = entry
            else:
                profiles_dict.pop(norm_profile, None)
            if profiles_dict:
                service_table["profiles"] = profiles_dict
            else:
                service_table.pop("profiles", None)
            if service_table:
                sources_dict[canonical_key] = service_table
            else:
                sources_dict.pop(canonical_key, None)

        if sources_dict:
            config_dict["sources"] = sources_dict
        else:
            config_dict.pop("sources", None)

        return _write_config_dict(config_location, config_dict)


_UNSET: Any = object()
_PERSISTED_GUI_SPEC_BY_NAME = {spec.name: spec for spec in PERSISTED_GUI_OPTION_SPECS}


def save_output_preferences(
    output_format: Any = _UNSET,
    combine: Any = _UNSET,
    config_location: str | None = None,
    **preferences: Any,
) -> str:
    """Persist GUI output and toggle preferences in `audiobook-dl.toml`."""
    updates: dict[str, Any] = {}
    if output_format is not _UNSET:
        updates["output_format"] = output_format
    if combine is not _UNSET:
        updates["combine"] = combine
    for key, raw_value in preferences.items():
        if key not in _PERSISTED_GUI_SPEC_BY_NAME:
            raise TypeError(f"save_output_preferences() got an unexpected keyword argument {key!r}")
        if key in updates:
            raise TypeError(f"save_output_preferences() got multiple values for argument {key!r}")
        updates[key] = raw_value

    resolved_location = config_location or get_config_location(None)
    with file_lock(resolved_location):
        config_dict = read_config(resolved_location)
        for key, raw_value in updates.items():
            if raw_value is _UNSET:
                continue
            spec = _PERSISTED_GUI_SPEC_BY_NAME[key]
            if spec.toml_serializer is None:
                continue
            should_set, serialized = spec.toml_serializer(raw_value)
            if should_set:
                config_dict[key] = serialized
            else:
                config_dict.pop(key, None)
        return _write_config_dict(resolved_location, config_dict)


def save_gui_preferences(
    window_geometry: str | None = None,
    output_dir: str | None = None,
    output_template: str | None = None,
    overwrite: str | None = None,
    *,
    use_download_archive: bool | None = None,
    theme: str | None = None,
    notify_on_complete: bool | None = None,
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
    ffmpeg_dir: str | None,
    overwrite: str | None = None,
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


def save_audiobookshelf_config(
    *,
    url: str | None = None,
    library_id: str | None = None,
    api_key: str | None = None,
    auto_upload: bool | None = None,
    auto_scan: bool | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> str:
    """Persist Audiobookshelf settings in `audiobook-dl.toml` and store the API key in the OS vault."""
    config_location = overwrite or get_config_location(None)
    store = credential_store or get_credential_store(config_location)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        prev_section = config_dict.get("audiobookshelf")
        prev_url = (
            prev_section.get("url").strip().rstrip("/")
            if isinstance(prev_section, dict) and isinstance(prev_section.get("url"), str)
            else None
        )
        abs_dict = dict(prev_section) if isinstance(prev_section, dict) else {}

        if url is not None:
            cleaned_url = url.strip().rstrip("/")
            if cleaned_url:
                abs_dict["url"] = cleaned_url
            else:
                abs_dict.pop("url", None)
        if library_id is not None:
            cleaned_lib = library_id.strip()
            if cleaned_lib:
                abs_dict["library_id"] = cleaned_lib
            else:
                abs_dict.pop("library_id", None)
        if auto_upload is not None:
            abs_dict["auto_upload"] = bool(auto_upload)
        if auto_scan is not None:
            abs_dict["auto_scan"] = bool(auto_scan)

        target_account = abs_dict.get("url") or "api"
        if api_key is not None:
            cleaned_key = api_key.strip()
            if prev_url and prev_url != target_account:
                store.delete_password("integration:audiobookshelf", prev_url)
            if cleaned_key:
                store.set_password("integration:audiobookshelf", target_account, cleaned_key)
                if (
                    not store.is_secure
                    and isinstance(store, PlaintextFallbackCredentialStore)
                    and store.allow_plaintext
                ):
                    abs_dict["api_key"] = cleaned_key
                else:
                    abs_dict.pop("api_key", None)
            else:
                store.delete_password("integration:audiobookshelf", target_account)
                store.delete_password("integration:audiobookshelf", "api")
                abs_dict.pop("api_key", None)

        if abs_dict:
            config_dict["audiobookshelf"] = abs_dict
        else:
            config_dict.pop("audiobookshelf", None)
        return _write_config_dict(config_location, config_dict)

"""Config file location resolution, TOML parsing/serialization, and `structure_config`."""

from audiobookdl import logging
from audiobookdl.credentials import CredentialStore, get_credential_store
from audiobookdl.exceptions import ConfigNotFound
from audiobookdl.options_schema import (
    CONFIG_OPTION_SPECS,
    GUI_CONFIG_KEYS,
    KNOWN_TOP_LEVEL_CONFIG_KEYS,
    SOURCE_CONFIG_KEYS,
    validate_config_value,
)
from audiobookdl.utils.fs import atomic_write_text, file_lock
import os
import sys
from typing import Any
from collections.abc import Sequence

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

import platformdirs
import tomlkit
from tomlkit.exceptions import TOMLKitError

from audiobookdl.config.migration import (
    _custom_provider_vault_service,
    _is_non_source_vault_key,
    _migrate_legacy_passwords,
)
from audiobookdl.config.models import (
    DEFAULT_PROFILE,
    AudiobookshelfConfig,
    Config,
    CustomMetadataProviderConfig,
    GuiConfig,
    SourceConfig,
    make_source_profile_key,
    split_source_profile_key,
)


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
    store = credential_store or get_credential_store(config_location)
    if os.path.exists(config_location):
        # Serialize the read-migrate-write window so a concurrent GUI save cannot be
        # clobbered by a stale `config_dict` write-back (the inner lock in
        # `_migrate_legacy_passwords` is reentrant on the same thread).
        with file_lock(config_location):
            config_dict = read_config(config_location)
            _migrate_legacy_passwords(config_location, config_dict, store)
    else:
        config_dict = read_config(config_location)
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

def _parse_oauth_scopes(raw: Any, section_label: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        scopes = tuple(part.strip() for part in raw.replace(",", " ").split() if part.strip())
        return scopes or None
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        scopes = tuple(item.strip() for item in raw if item.strip())
        return scopes or None
    logging.warning(f"Warning: invalid 'oauth_scopes' in '{section_label}'; ignoring")
    return None


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
            logging.warning(
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
        logging.warning(f"Warning: invalid 'cookie_file' in '{section_label}'; ignoring")
        cookie_file = None
    if cookie_file and not os.path.isabs(cookie_file):
        cookie_file = os.path.normpath(os.path.join(config_dir_path, cookie_file))
    raw_locale = values.get("locale")
    if raw_locale is not None and not isinstance(raw_locale, str):
        logging.warning(f"Warning: invalid 'locale' in '{section_label}'; ignoring")
        raw_locale = None
    raw_client_id = values.get("oauth_client_id")
    if raw_client_id is not None and not isinstance(raw_client_id, str):
        logging.warning(f"Warning: invalid 'oauth_client_id' in '{section_label}'; ignoring")
        raw_client_id = None
    oauth_scopes = _parse_oauth_scopes(values.get("oauth_scopes"), section_label)
    oauth_token_present = False
    if credential_store is not None:
        oauth_token_present = credential_store.get_oauth_token(profile_key) is not None
        if not oauth_token_present and raw_source_name != profile_key:
            oauth_token_present = credential_store.get_oauth_token(raw_source_name) is not None
    return SourceConfig(
        username=username if isinstance(username, str) or username is None else str(username),
        password=password if isinstance(password, str) or password is None else str(password),
        library=values.get("library"),
        cookie_file=cookie_file,
        locale=raw_locale.strip() or None if isinstance(raw_locale, str) else None,
        oauth_client_id=raw_client_id.strip() or None if isinstance(raw_client_id, str) else None,
        oauth_scopes=oauth_scopes,
        oauth_token_present=oauth_token_present,
    )


def _read_gui_bool(gui_section: dict[str, Any], key: str) -> bool | None:
    """Return a boolean `[gui]` value, logging and ignoring non-boolean entries."""
    raw = gui_section.get(key)
    if raw is None:
        return None
    if not isinstance(raw, bool):
        logging.warning(f"Warning: config '[gui].{key}' must be a boolean; ignoring")
        return None
    return raw


def _read_gui_split_ratio(gui_section: dict[str, Any]) -> float | None:
    """Return `[gui].download_split_ratio` when it is a number strictly between 0 and 1."""
    raw = gui_section.get("download_split_ratio")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0.0 < float(raw) < 1.0:
        logging.warning("Warning: config '[gui].download_split_ratio' must be a number between 0 and 1; ignoring")
        return None
    return float(raw)


def _read_gui_positive_int(gui_section: dict[str, Any], key: str) -> int | None:
    """Return a positive integer `[gui]` value, logging and ignoring invalid entries."""
    raw = gui_section.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        logging.warning(f"Warning: config '[gui].{key}' must be an integer >= 1; ignoring")
        return None
    return raw


DEFAULT_AUTO_SYNC_MODE = "enqueue"
VALID_AUTO_SYNC_MODES = ("enqueue", "scan_only", "dry_run")


def _read_gui_sync_mode(gui_section: dict[str, Any]) -> str | None:
    """Return `[gui].auto_sync_mode` when it is `'enqueue'`, `'scan_only'`, or `'dry_run'`."""
    raw = gui_section.get("auto_sync_mode")
    if raw is None:
        return None
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in VALID_AUTO_SYNC_MODES:
            return normalized
    logging.warning(
        "Warning: config '[gui].auto_sync_mode' must be 'enqueue', 'scan_only', or 'dry_run'; ignoring"
    )
    return None


def _read_gui_sync_targets(gui_section: dict[str, Any]) -> list[str] | None:
    """Return `[gui].auto_sync_targets` when it is a list of non-empty strings."""
    raw = gui_section.get("auto_sync_targets")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        logging.warning("Warning: config '[gui].auto_sync_targets' must be a list of strings; ignoring")
        return None
    return [item.strip() for item in raw if item.strip()]


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
            logging.warning(f"Warning: unknown config key '{key}' ignored")

    # Add sources with normalized canonical keys and validation warnings
    sources: dict[str, SourceConfig] = {}
    config_dir_path = os.path.dirname(os.path.abspath(config_location))
    raw_sources = config_dict.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, dict):
        logging.warning("Warning: config '[sources]' must be a table; ignoring")
        raw_sources = {}
    if isinstance(raw_sources, dict):
        allowed_with_profiles = tuple(SOURCE_CONFIG_KEYS) + ("profiles",)
        for raw_source_name, values in raw_sources.items():
            if not isinstance(values, dict):
                logging.warning(f"Warning: config '[sources.{raw_source_name}]' must be a table; ignoring")
                continue
            base_service, base_profile = split_source_profile_key(raw_source_name)
            if base_service != raw_source_name.lower() and base_profile == DEFAULT_PROFILE:
                logging.warning(
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
                    logging.warning(
                        f"Warning: duplicate config for '{profile_key}' from alias '{raw_source_name}'; keeping canonical entry"
                    )
                else:
                    sources[profile_key] = candidate_cfg
            else:
                for key in values:
                    if key not in allowed_with_profiles:
                        logging.warning(
                            f"Warning: unknown key '{key}' in '[sources.{raw_source_name}]' ignored"
                        )

            raw_profiles = values.get("profiles")
            if raw_profiles is not None and not isinstance(raw_profiles, dict):
                logging.warning(
                    f"Warning: config '[sources.{raw_source_name}.profiles]' must be a table; ignoring"
                )
            elif isinstance(raw_profiles, dict):
                for raw_prof_name, prof_values in raw_profiles.items():
                    section_label = f"[sources.{raw_source_name}.profiles.{raw_prof_name}]"
                    if not isinstance(prof_values, dict):
                        logging.warning(f"Warning: config '{section_label}' must be a table; ignoring")
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
                        logging.warning(
                            f"Warning: duplicate config for '{prof_key}' from alias '{raw_source_name}'; keeping canonical entry"
                        )
                        continue
                    sources[prof_key] = prof_cfg
    if credential_store is not None:
        for raw_service_key, stored_user in credential_store.stored_services().items():
            if _is_non_source_vault_key(raw_service_key):
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
                        oauth_token_present=credential_store.get_oauth_token(profile_key) is not None,
                    )
        for raw_oauth_key in credential_store.stored_oauth_profiles():
            if _is_non_source_vault_key(raw_oauth_key):
                continue
            svc, prof = split_source_profile_key(raw_oauth_key)
            profile_key = make_source_profile_key(svc, prof)
            if profile_key in sources:
                sources[profile_key].oauth_token_present = True
            else:
                sources[profile_key] = SourceConfig(
                    username=None,
                    password=None,
                    library=None,
                    cookie_file=None,
                    oauth_token_present=True,
                )

    gui_section = config_dict.get("gui")
    gui_config = GuiConfig()
    if gui_section is not None and not isinstance(gui_section, dict):
        logging.warning("Warning: config '[gui]' must be a table; ignoring")
    elif isinstance(gui_section, dict):
        for key in gui_section:
            if key not in GUI_CONFIG_KEYS:
                logging.warning(f"Warning: unknown key '{key}' in '[gui]' ignored")
        use_archive_val = _read_gui_bool(gui_section, "use_download_archive")
        theme_raw = gui_section.get("theme")
        theme_val: str | None = None
        if theme_raw is not None:
            if isinstance(theme_raw, str) and theme_raw.strip().lower() in ("system", "light", "dark"):
                theme_val = theme_raw.strip().lower()
            else:
                logging.warning(
                    "Warning: config '[gui].theme' must be 'system', 'light', or 'dark'; ignoring"
                )
        notify_val = _read_gui_bool(gui_section, "notify_on_complete")
        gui_config = GuiConfig(
            window_geometry=gui_section.get("window_geometry") if isinstance(gui_section.get("window_geometry"), str) else None,
            window_maximized=_read_gui_bool(gui_section, "window_maximized"),
            download_split_ratio=_read_gui_split_ratio(gui_section),
            active_page=gui_section.get("active_page") if isinstance(gui_section.get("active_page"), str) else None,
            options_collapsed=_read_gui_bool(gui_section, "options_collapsed"),
            output_naming_collapsed=_read_gui_bool(gui_section, "output_naming_collapsed"),
            output_dir=gui_section.get("output_dir") if isinstance(gui_section.get("output_dir"), str) else None,
            output_template=gui_section.get("output_template") if isinstance(gui_section.get("output_template"), str) else None,
            use_download_archive=use_archive_val,
            theme=theme_val,
            notify_on_complete=notify_val,
            clipboard_monitor=_read_gui_bool(gui_section, "clipboard_monitor"),
            auto_sync_enabled=_read_gui_bool(gui_section, "auto_sync_enabled"),
            auto_sync_minutes=_read_gui_positive_int(gui_section, "auto_sync_minutes"),
            auto_sync_mode=_read_gui_sync_mode(gui_section),
            auto_sync_targets=_read_gui_sync_targets(gui_section),
            queue_concurrency=(
                max(1, min(4, qc))
                if (qc := _read_gui_positive_int(gui_section, "queue_concurrency")) is not None
                else None
            ),
            minimize_to_tray=_read_gui_bool(gui_section, "minimize_to_tray"),
        )

    abs_section = config_dict.get("audiobookshelf")
    abs_config = AudiobookshelfConfig()
    if abs_section is not None and not isinstance(abs_section, dict):
        logging.warning("Warning: config '[audiobookshelf]' must be a table; ignoring")
    else:
        abs_dict = abs_section if isinstance(abs_section, dict) else {}
        for key in abs_dict:
            if key not in {"url", "api_key", "library_id", "auto_upload", "auto_scan"}:
                logging.warning(f"Warning: unknown key '{key}' in '[audiobookshelf]' ignored")
        raw_url = abs_dict.get("url")
        abs_url = raw_url.strip().rstrip("/") if isinstance(raw_url, str) else None
        if abs_url == "":
            abs_url = None
        raw_lib = abs_dict.get("library_id")
        abs_lib = raw_lib.strip() if isinstance(raw_lib, str) else None
        if abs_lib == "":
            abs_lib = None
        raw_upload = abs_dict.get("auto_upload")
        abs_auto_upload = raw_upload if isinstance(raw_upload, bool) else False
        raw_scan = abs_dict.get("auto_scan")
        abs_auto_scan = raw_scan if isinstance(raw_scan, bool) else True
        abs_key_raw = abs_dict.get("api_key")
        abs_key: str | None = abs_key_raw.strip() if isinstance(abs_key_raw, str) and abs_key_raw.strip() else None
        abs_account = abs_url or "api"
        if credential_store is not None:
            if abs_key is not None and credential_store.is_secure:
                try:
                    credential_store.set_password("integration:audiobookshelf", abs_account, abs_key)
                except Exception as exc:
                    logging.warning(f"Warning: could not store Audiobookshelf token in OS vault: {exc}")
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

    custom_providers: dict[str, CustomMetadataProviderConfig] = {}
    enrichment_section = config_dict.get("enrichment")
    if enrichment_section is not None and not isinstance(enrichment_section, dict):
        logging.warning("Warning: config '[enrichment]' must be a table; ignoring")
    elif isinstance(enrichment_section, dict):
        for key in enrichment_section:
            if key != "custom":
                logging.warning(f"Warning: unknown key '{key}' in '[enrichment]' ignored")
        raw_custom = enrichment_section.get("custom")
        if raw_custom is not None and not isinstance(raw_custom, dict):
            logging.warning("Warning: config '[enrichment.custom]' must be a table; ignoring")
        elif isinstance(raw_custom, dict):
            from audiobookdl.utils.enrichment import (
                normalize_custom_provider_name,
                validate_custom_provider_url,
            )

            for raw_name, raw_entry in raw_custom.items():
                section_label = f"[enrichment.custom.{raw_name}]"
                if not isinstance(raw_entry, dict):
                    logging.warning(f"Warning: config '{section_label}' must be a table; ignoring")
                    continue
                try:
                    slug = normalize_custom_provider_name(str(raw_name))
                except ValueError as exc:
                    logging.warning(f"Warning: invalid custom provider '{raw_name}': {exc}; ignoring")
                    continue
                for entry_key in raw_entry:
                    if entry_key not in {"url", "api_key", "enabled"}:
                        logging.warning(f"Warning: unknown key '{entry_key}' in '{section_label}' ignored")
                raw_url = raw_entry.get("url")
                if not isinstance(raw_url, str) or not raw_url.strip():
                    logging.warning(f"Warning: missing 'url' in '{section_label}'; ignoring")
                    continue
                try:
                    norm_url = validate_custom_provider_url(raw_url)
                except ValueError as exc:
                    logging.warning(f"Warning: invalid 'url' in '{section_label}': {exc}; ignoring")
                    continue
                raw_enabled = raw_entry.get("enabled")
                enabled = raw_enabled if isinstance(raw_enabled, bool) else True
                if raw_enabled is not None and not isinstance(raw_enabled, bool):
                    logging.warning(f"Warning: invalid 'enabled' in '{section_label}'; defaulting to true")
                raw_key = raw_entry.get("api_key")
                prov_key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
                vault_service = _custom_provider_vault_service(slug)
                if credential_store is not None:
                    if prov_key is not None and credential_store.is_secure:
                        try:
                            credential_store.set_password(vault_service, norm_url, prov_key)
                        except Exception as exc:
                            logging.warning(
                                f"Warning: could not store API key for custom provider '{slug}': {exc}"
                            )
                    elif prov_key is None:
                        prov_key = credential_store.get_password(vault_service, norm_url)
                        if prov_key is None:
                            prov_key = credential_store.get_password(vault_service, "api")
                custom_providers[slug] = CustomMetadataProviderConfig(
                    name=slug,
                    url=norm_url,
                    api_key=prov_key,
                    enabled=enabled,
                )

    parsed_options: dict[str, Any] = {}
    for spec in CONFIG_OPTION_SPECS:
        key = spec.config_key
        if key is None or spec.config_kind is None:
            continue
        value, warning = validate_config_value(
            key,
            spec.config_kind,
            config_dict.get(key),
            custom_providers=custom_providers if spec.config_kind == "enrich_providers" else None,
        )
        if warning is not None:
            logging.warning(warning)
        parsed_options[key] = value

    # Create config object
    return Config(
        sources=sources,
        gui=gui_config,
        audiobookshelf=abs_config,
        custom_metadata_providers=custom_providers,
        config_location=config_location,
        **parsed_options,
    )


def _has_nonempty_toml_entries(mapping: dict[str, Any]) -> bool:
    """Return True if `mapping` contains at least one non-empty leaf value."""
    for val in mapping.values():
        if isinstance(val, dict):
            if _has_nonempty_toml_entries(val):
                return True
        elif val is not None and val != "":
            return True
    return False


def _sync_toml_mapping(container: Any, target: dict[str, Any]) -> None:
    """Reconcile a `tomlkit` document/table in-place against `target`, preserving comments."""
    for existing_key in list(container.keys()):
        if existing_key not in target:
            del container[existing_key]

    for key, val in target.items():
        if val is None or val == "":
            if key in container:
                del container[key]
            continue
        if isinstance(val, dict):
            if not _has_nonempty_toml_entries(val):
                if key in container:
                    del container[key]
                continue
            existing = container.get(key)
            if isinstance(existing, dict):
                _sync_toml_mapping(existing, val)
            else:
                sub_table = tomlkit.table()
                _sync_toml_mapping(sub_table, val)
                container[key] = sub_table
            continue

        normalized = list(val) if isinstance(val, tuple) else val
        if key in container:
            existing_val = container[key]
            unwrapped = existing_val.unwrap() if hasattr(existing_val, "unwrap") else existing_val
            if type(unwrapped) is type(normalized) and unwrapped == normalized:
                continue
        container[key] = normalized


def _merge_toml_preserving_comments(existing_text: str, data: dict[str, Any]) -> str:
    """
    Update TOML `existing_text` with `data` using `tomlkit`, preserving `#` comments,
    blank lines, key order, inline comments, multiline structures, and custom sections.
    """
    try:
        doc = tomlkit.parse(existing_text)
    except TOMLKitError:
        return _dump_toml(data)
    _sync_toml_mapping(doc, data)
    rendered = tomlkit.dumps(doc)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize config dictionary (top-level scalars + all nested tables) to TOML."""
    doc = tomlkit.document()
    _sync_toml_mapping(doc, data)
    rendered = tomlkit.dumps(doc)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _has_plaintext_password(config_dict: dict[str, Any]) -> bool:
    sources = config_dict.get("sources")
    if isinstance(sources, dict):
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
    abs_section = config_dict.get("audiobookshelf")
    if isinstance(abs_section, dict) and abs_section.get("api_key"):
        return True
    enrichment = config_dict.get("enrichment")
    if isinstance(enrichment, dict) and isinstance(enrichment.get("custom"), dict):
        for entry in enrichment["custom"].values():
            if isinstance(entry, dict) and entry.get("api_key"):
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

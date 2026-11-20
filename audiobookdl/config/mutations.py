"""Config mutation APIs: credential/profile persistence and GUI preference writers."""

import os
import shutil
from typing import TYPE_CHECKING, Any
from collections.abc import Sequence

from audiobookdl.credentials import (
    CredentialStore,
    PlaintextFallbackCredentialStore,
    get_credential_store,
)
from audiobookdl.options_schema import PERSISTED_OUTPUT_PREF_SPECS
from audiobookdl.utils.fs import file_lock

from audiobookdl.config.io import (
    VALID_AUTO_SYNC_MODES,
    _write_config_dict,
    get_config_location,
    read_config,
)
from audiobookdl.config.migration import _custom_provider_vault_service
from audiobookdl.config.models import (
    DEFAULT_PROFILE,
    make_source_profile_key,
    normalize_profile_name,
    normalize_source_key,
    profile_scoped_filename,
    sanitize_profile_filename_part,
)

if TYPE_CHECKING:
    from audiobookdl.utils.oauth import OAuthTokenBundle


_UNSET: Any = object()
_PERSISTED_OUTPUT_PREF_SPEC_BY_NAME = {spec.name: spec for spec in PERSISTED_OUTPUT_PREF_SPECS}


def _read_profile_entry(
    sources_dict: dict[str, Any],
    canonical_key: str,
    norm_profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    service_table = dict(sources_dict.get(canonical_key) or {})
    if norm_profile == DEFAULT_PROFILE:
        prev_entry = {k: v for k, v in service_table.items() if k != "profiles"}
    else:
        profiles_dict = dict(service_table.get("profiles") or {})
        raw_profile = profiles_dict.get(norm_profile)
        prev_entry = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    return service_table, prev_entry


def _write_profile_entry(
    config_dict: dict[str, Any],
    sources_dict: dict[str, Any],
    canonical_key: str,
    norm_profile: str,
    entry: dict[str, Any] | None,
) -> None:
    service_table = dict(sources_dict.get(canonical_key) or {})
    if norm_profile == DEFAULT_PROFILE:
        existing_profiles = service_table.get("profiles")
        new_service_table = dict(entry) if entry else {}
        if isinstance(existing_profiles, dict) and existing_profiles:
            new_service_table["profiles"] = existing_profiles
        if new_service_table:
            sources_dict[canonical_key] = new_service_table
        else:
            sources_dict.pop(canonical_key, None)
    else:
        profiles_dict = dict(service_table.get("profiles") or {})
        if entry:
            profiles_dict[norm_profile] = dict(entry)
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


def _profile_runtime_files(
    database_directory: str | None,
    canonical_key: str,
    profile: str,
) -> list[str]:
    if not database_directory:
        return []
    service_dir = os.path.join(database_directory, canonical_key)
    if not os.path.isdir(service_dir):
        return []
    norm_profile = normalize_profile_name(profile)
    safe_suffix = sanitize_profile_filename_part(norm_profile)
    matches: list[str] = []
    cookie_path = os.path.join(service_dir, profile_scoped_filename("session_cookies.txt", norm_profile))
    if os.path.isfile(cookie_path):
        matches.append(cookie_path)
    try:
        entries = os.listdir(service_dir)
    except OSError:
        return matches
    for name in entries:
        if safe_suffix is None:
            if name.endswith("_device_id.txt"):
                matches.append(os.path.join(service_dir, name))
        else:
            target_suffix = f"_device_id_{safe_suffix}.txt"
            if name.endswith(target_suffix):
                matches.append(os.path.join(service_dir, name))
    return matches


def _delete_profile_runtime_files(
    database_directory: str | None,
    canonical_key: str,
    profile: str,
) -> None:
    for path in _profile_runtime_files(database_directory, canonical_key, profile):
        try:
            os.remove(path)
        except OSError:
            pass


def _rename_profile_runtime_files(
    database_directory: str | None,
    canonical_key: str,
    old_profile: str,
    new_profile: str,
) -> None:
    if not database_directory:
        return
    old_norm = normalize_profile_name(old_profile)
    new_norm = normalize_profile_name(new_profile)
    if old_norm == new_norm:
        return
    service_dir = os.path.join(database_directory, canonical_key)
    if not os.path.isdir(service_dir):
        return
    old_suffix = sanitize_profile_filename_part(old_norm)
    for src in _profile_runtime_files(database_directory, canonical_key, old_norm):
        base = os.path.basename(src)
        if base.startswith("session_cookies"):
            dst_name = profile_scoped_filename("session_cookies.txt", new_norm)
        elif old_suffix is None and base.endswith("_device_id.txt"):
            dst_name = profile_scoped_filename(base, new_norm)
        elif old_suffix is not None and base.endswith(f"_device_id_{old_suffix}.txt"):
            prefix = base[: -len(f"_device_id_{old_suffix}.txt")]
            dst_name = profile_scoped_filename(f"{prefix}_device_id.txt", new_norm)
        else:
            continue
        dst = os.path.join(service_dir, dst_name)
        try:
            os.makedirs(service_dir, exist_ok=True)
            shutil.move(src, dst)
        except OSError:
            pass


def load_source_oauth_token(
    service_key: str,
    *,
    profile: str | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> "OAuthTokenBundle | None":
    """Load persisted OAuth token bundle for `(service_key, profile)` from `CredentialStore`."""
    from audiobookdl.utils.oauth import OAuthTokenBundle

    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    profile_key = make_source_profile_key(canonical_key, norm_profile)
    store = credential_store or get_credential_store(config_location)
    raw = store.get_oauth_token(profile_key)
    return OAuthTokenBundle.from_json(raw) if raw else None


def save_source_oauth_token(
    service_key: str,
    token: "OAuthTokenBundle",
    *,
    profile: str | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    """Persist serialized `OAuthTokenBundle` for `(service_key, profile)` in `CredentialStore`."""
    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    profile_key = make_source_profile_key(canonical_key, norm_profile)
    store = credential_store or get_credential_store(config_location)
    store.set_oauth_token(profile_key, token.to_json())


def delete_source_oauth_token(
    service_key: str,
    *,
    profile: str | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    """Delete any persisted OAuth token bundle for `(service_key, profile)` from `CredentialStore`."""
    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    profile_key = make_source_profile_key(canonical_key, norm_profile)
    store = credential_store or get_credential_store(config_location)
    store.delete_oauth_token(profile_key)


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
    locale: Any = _UNSET,
    oauth_client_id: Any = _UNSET,
    oauth_scopes: Any = _UNSET,
) -> str:
    """
    Save or clear credentials for `(service_key, profile)` in the configuration file.

    Non-secret fields (`username`, `library`, `cookie_file`, `locale`, `oauth_client_id`,
    `oauth_scopes`) are stored in `audiobook-dl.toml` while `password` and OAuth tokens are
    persisted in the OS credential store. Preserves existing settings for other fields/profiles.
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
        _, prev_entry = _read_profile_entry(sources_dict, canonical_key, norm_profile)
        prev_username = prev_entry.get("username") if isinstance(prev_entry, dict) else None

        clearing_credentials = (
            not (username or "")
            and not (password or "")
            and not (library or "")
            and not (cookie_file or "")
            and locale is _UNSET
            and oauth_client_id is _UNSET
            and oauth_scopes is _UNSET
        )

        if clearing_credentials:
            entry: dict[str, Any] = {}
        else:
            entry = {
                k: v
                for k, v in (
                    ("username", username),
                    ("library", library),
                    ("cookie_file", cookie_file),
                )
                if v is not None and v != ""
            }
            for optional_key, raw_override in (
                ("locale", locale),
                ("oauth_client_id", oauth_client_id),
                ("oauth_scopes", oauth_scopes),
            ):
                if raw_override is _UNSET:
                    prev_val = prev_entry.get(optional_key)
                    if prev_val is not None and prev_val != "" and prev_val != []:
                        entry[optional_key] = prev_val
                elif raw_override is not None and raw_override != "" and raw_override != [] and raw_override != ():
                    if optional_key == "oauth_scopes" and isinstance(raw_override, str):
                        parsed_scopes = [p.strip() for p in raw_override.replace(",", " ").split() if p.strip()]
                        if parsed_scopes:
                            entry[optional_key] = parsed_scopes
                    elif optional_key == "oauth_scopes" and isinstance(raw_override, tuple):
                        entry[optional_key] = list(raw_override)
                    elif isinstance(raw_override, str):
                        cleaned = raw_override.strip()
                        if cleaned:
                            entry[optional_key] = cleaned
                    else:
                        entry[optional_key] = raw_override

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

        _write_profile_entry(config_dict, sources_dict, canonical_key, norm_profile, entry)
        return _write_config_dict(config_location, config_dict)


def delete_source_profile(
    service_key: str,
    *,
    profile: str | None = None,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
    database_directory: str | None = None,
) -> str:
    """Remove a `(service_key, profile)` configuration, vault secrets, OAuth tokens, and runtime caches."""
    config_location = overwrite or get_config_location(None)
    canonical_key = normalize_source_key(service_key)
    norm_profile = normalize_profile_name(profile)
    vault_key = make_source_profile_key(canonical_key, norm_profile)
    store = credential_store or get_credential_store(config_location)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        sources_dict = dict(config_dict.get("sources") or {})
        _, prev_entry = _read_profile_entry(sources_dict, canonical_key, norm_profile)
        prev_username = prev_entry.get("username") if isinstance(prev_entry, dict) else None
        store.delete_password(vault_key, prev_username)
        store.delete_password(vault_key, None)
        store.delete_oauth_token(vault_key)
        _write_profile_entry(config_dict, sources_dict, canonical_key, norm_profile, None)
        written = _write_config_dict(config_location, config_dict)
    _delete_profile_runtime_files(database_directory, canonical_key, norm_profile)
    return written


def rename_source_profile(
    service_key: str,
    old_profile: str,
    new_profile: str,
    *,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
    database_directory: str | None = None,
) -> str:
    """Rename `(service_key, old_profile)` to `new_profile`, migrating TOML, vault secrets, OAuth tokens, and caches."""
    canonical_key = normalize_source_key(service_key)
    old_norm = normalize_profile_name(old_profile)
    new_norm = normalize_profile_name(new_profile)
    if not new_profile or not new_profile.strip():
        raise ValueError("New profile name cannot be empty")
    if old_norm == new_norm:
        return overwrite or get_config_location(None)

    config_location = overwrite or get_config_location(None)
    old_vault_key = make_source_profile_key(canonical_key, old_norm)
    new_vault_key = make_source_profile_key(canonical_key, new_norm)
    store = credential_store or get_credential_store(config_location)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        sources_dict = dict(config_dict.get("sources") or {})
        _, old_entry = _read_profile_entry(sources_dict, canonical_key, old_norm)
        _, existing_target = _read_profile_entry(sources_dict, canonical_key, new_norm)
        old_username = old_entry.get("username") if isinstance(old_entry, dict) else None
        old_pw = store.get_password(old_vault_key, old_username)
        old_oauth = store.get_oauth_token(old_vault_key)

        if (
            existing_target
            or store.get_password(new_vault_key, None) is not None
            or store.get_oauth_token(new_vault_key) is not None
        ):
            raise ValueError(f"Profile '{new_norm}' already exists for {canonical_key}")
        if not old_entry and old_pw is None and old_oauth is None:
            raise ValueError(f"Profile '{old_norm}' does not exist for {canonical_key}")

        if old_pw is not None:
            store.set_password(new_vault_key, old_username, old_pw)
            store.delete_password(old_vault_key, old_username)
        if old_oauth is not None:
            store.set_oauth_token(new_vault_key, old_oauth)
            store.delete_oauth_token(old_vault_key)

        _write_profile_entry(config_dict, sources_dict, canonical_key, old_norm, None)
        if old_entry:
            _write_profile_entry(config_dict, sources_dict, canonical_key, new_norm, old_entry)
        written = _write_config_dict(config_location, config_dict)
    _rename_profile_runtime_files(database_directory, canonical_key, old_norm, new_norm)
    return written


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
        if key not in _PERSISTED_OUTPUT_PREF_SPEC_BY_NAME:
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
            spec = _PERSISTED_OUTPUT_PREF_SPEC_BY_NAME[key]
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
    window_maximized: bool | None = None,
    download_split_ratio: float | None = None,
    active_page: str | None = None,
    options_collapsed: bool | None = None,
    output_naming_collapsed: bool | None = None,
    use_download_archive: bool | None = None,
    theme: str | None = None,
    notify_on_complete: bool | None = None,
    clipboard_monitor: bool | None = None,
    auto_sync_enabled: bool | None = None,
    auto_sync_minutes: int | None = None,
    auto_sync_mode: str | None = None,
    auto_sync_targets: Sequence[str] | None = None,
    queue_concurrency: int | None = None,
    minimize_to_tray: bool | None = None,
) -> str:
    """Persist GUI window state, output prefs, archive toggle, theme, notifications, clipboard, and sync schedule."""
    config_location = overwrite or get_config_location(None)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        gui_dict = dict(config_dict.get("gui") or {})
        str_updates = {
            "window_geometry": window_geometry,
            "active_page": active_page.strip().lower() if isinstance(active_page, str) else active_page,
            "output_dir": output_dir,
            "output_template": output_template,
            "theme": theme.strip().lower() if isinstance(theme, str) else theme,
        }
        for key, val in str_updates.items():
            if val is None:
                continue
            cleaned = val.strip()
            if cleaned:
                gui_dict[key] = cleaned
            else:
                gui_dict.pop(key, None)
        bool_updates = {
            "window_maximized": window_maximized,
            "options_collapsed": options_collapsed,
            "output_naming_collapsed": output_naming_collapsed,
            "use_download_archive": use_download_archive,
            "notify_on_complete": notify_on_complete,
            "clipboard_monitor": clipboard_monitor,
            "auto_sync_enabled": auto_sync_enabled,
            "minimize_to_tray": minimize_to_tray,
        }
        for bkey, bval in bool_updates.items():
            if bval is not None:
                gui_dict[bkey] = bool(bval)
        if auto_sync_minutes is not None and int(auto_sync_minutes) >= 1:
            gui_dict["auto_sync_minutes"] = int(auto_sync_minutes)
        if queue_concurrency is not None:
            gui_dict["queue_concurrency"] = max(1, min(4, int(queue_concurrency)))
        if auto_sync_mode is not None:
            norm_mode = auto_sync_mode.strip().lower()
            if norm_mode not in VALID_AUTO_SYNC_MODES:
                raise ValueError(
                    f"Invalid auto_sync_mode {auto_sync_mode!r}; expected one of {VALID_AUTO_SYNC_MODES}"
                )
            gui_dict["auto_sync_mode"] = norm_mode
        if auto_sync_targets is not None:
            gui_dict["auto_sync_targets"] = [
                str(t).strip() for t in auto_sync_targets if str(t).strip()
            ]
        if download_split_ratio is not None and 0.0 < float(download_split_ratio) < 1.0:
            gui_dict["download_split_ratio"] = round(float(download_split_ratio), 4)
        if gui_dict:
            config_dict["gui"] = gui_dict
        else:
            config_dict.pop("gui", None)
        return _write_config_dict(config_location, config_dict)


def save_check_updates(
    enabled: bool,
    overwrite: str | None = None,
) -> str:
    """Persist top-level `check_updates` preference in `audiobook-dl.toml`."""
    config_location = overwrite or get_config_location(None)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        config_dict["check_updates"] = bool(enabled)
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
        raw_prev_url = prev_section.get("url") if isinstance(prev_section, dict) else None
        prev_url = raw_prev_url.strip().rstrip("/") if isinstance(raw_prev_url, str) else None
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


def save_custom_metadata_provider(
    name: str,
    *,
    url: str,
    api_key: str | None = None,
    enabled: bool = True,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> str:
    """Persist or update an ABS-spec custom metadata provider in TOML and OS credential store."""
    from audiobookdl.utils.enrichment import (
        normalize_custom_provider_name,
        validate_custom_provider_url,
    )

    slug = normalize_custom_provider_name(name)
    cleaned_url = validate_custom_provider_url(url)
    config_location = overwrite or get_config_location(None)
    store = credential_store or get_credential_store(config_location)
    vault_service = _custom_provider_vault_service(slug)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        enrichment_dict = dict(config_dict.get("enrichment") or {})
        custom_dict = dict(enrichment_dict.get("custom") or {})
        prev_entry = custom_dict.get(slug)
        raw_prev_url = prev_entry.get("url") if isinstance(prev_entry, dict) else None
        prev_url = raw_prev_url.strip().rstrip("/") if isinstance(raw_prev_url, str) else None
        entry_dict: dict[str, Any] = {
            "url": cleaned_url,
            "enabled": bool(enabled),
        }
        if api_key is not None:
            cleaned_key = api_key.strip()
            if prev_url and prev_url != cleaned_url:
                store.delete_password(vault_service, prev_url)
            if cleaned_key:
                store.set_password(vault_service, cleaned_url, cleaned_key)
                if (
                    not store.is_secure
                    and isinstance(store, PlaintextFallbackCredentialStore)
                    and store.allow_plaintext
                ):
                    entry_dict["api_key"] = cleaned_key
            else:
                store.delete_password(vault_service, cleaned_url)
                store.delete_password(vault_service, "api")
        elif prev_url and prev_url != cleaned_url:
            existing = store.get_password(vault_service, prev_url)
            if existing:
                store.set_password(vault_service, cleaned_url, existing)
                store.delete_password(vault_service, prev_url)
            elif isinstance(prev_entry, dict) and prev_entry.get("api_key"):
                entry_dict["api_key"] = prev_entry["api_key"]
        elif isinstance(prev_entry, dict) and prev_entry.get("api_key"):
            entry_dict["api_key"] = prev_entry["api_key"]

        custom_dict[slug] = entry_dict
        enrichment_dict["custom"] = custom_dict
        config_dict["enrichment"] = enrichment_dict
        return _write_config_dict(config_location, config_dict)


def delete_custom_metadata_provider(
    name: str,
    *,
    overwrite: str | None = None,
    credential_store: CredentialStore | None = None,
) -> str:
    """Remove a configured custom metadata provider from TOML and delete any vaulted key."""
    from audiobookdl.utils.enrichment import normalize_custom_provider_name

    slug = normalize_custom_provider_name(name)
    config_location = overwrite or get_config_location(None)
    store = credential_store or get_credential_store(config_location)
    vault_service = _custom_provider_vault_service(slug)
    with file_lock(config_location):
        config_dict = read_config(config_location)
        enrichment_dict = dict(config_dict.get("enrichment") or {})
        custom_dict = dict(enrichment_dict.get("custom") or {})
        removed = custom_dict.pop(slug, None)
        if isinstance(removed, dict) and isinstance(removed.get("url"), str) and removed["url"].strip():
            store.delete_password(vault_service, removed["url"].strip().rstrip("/"))
        store.delete_password(vault_service, "api")
        if custom_dict:
            enrichment_dict["custom"] = custom_dict
        else:
            enrichment_dict.pop("custom", None)
        if enrichment_dict:
            config_dict["enrichment"] = enrichment_dict
        else:
            config_dict.pop("enrichment", None)
        return _write_config_dict(config_location, config_dict)

"""One-time migration of legacy plaintext secrets out of `audiobook-dl.toml`."""

import os
from typing import Any

from audiobookdl import logging
from audiobookdl.credentials import CredentialStore
from audiobookdl.utils.fs import file_lock

from audiobookdl.config.models import make_source_profile_key, split_source_profile_key


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


_CUSTOM_PROVIDER_VAULT_PREFIX = "metadata_provider:"


def _custom_provider_vault_service(slug: str) -> str:
    return f"{_CUSTOM_PROVIDER_VAULT_PREFIX}{slug}"


def _is_non_source_vault_key(key: str) -> bool:
    """Return True for vault service keys that must not hydrate `Config.sources`."""
    return key.startswith("integration:") or key.startswith(_CUSTOM_PROVIDER_VAULT_PREFIX)


def _migrate_legacy_passwords(
    config_location: str,
    config_dict: dict[str, Any],
    store: CredentialStore,
) -> None:
    """Move any plaintext passwords or custom-provider keys in `config_dict` into `store` and scrub TOML."""
    if not store.is_secure:
        return
    migrated = False
    sources = config_dict.get("sources")
    if isinstance(sources, dict):
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
        if not sources:
            config_dict.pop("sources", None)

    enrichment = config_dict.get("enrichment")
    if isinstance(enrichment, dict) and isinstance(enrichment.get("custom"), dict):
        from audiobookdl.utils.enrichment import normalize_custom_provider_name

        for raw_name, raw_entry in list(enrichment["custom"].items()):
            if not isinstance(raw_entry, dict) or "api_key" not in raw_entry:
                continue
            legacy_key = raw_entry.pop("api_key")
            cleaned_key = str(legacy_key).strip() if isinstance(legacy_key, str) else ""
            if not cleaned_key:
                migrated = True
                continue
            try:
                slug = normalize_custom_provider_name(str(raw_name))
            except ValueError:
                raw_entry["api_key"] = legacy_key
                continue
            raw_url = raw_entry.get("url")
            account = raw_url.strip().rstrip("/") if isinstance(raw_url, str) and raw_url.strip() else "api"
            try:
                store.set_password(_custom_provider_vault_service(slug), account, cleaned_key)
                migrated = True
            except Exception as exc:
                raw_entry["api_key"] = legacy_key
                logging.debug(
                    f"Failed to migrate API key for custom provider '{slug}' to OS keystore: {exc}"
                )

    abs_section = config_dict.get("audiobookshelf")
    if isinstance(abs_section, dict) and "api_key" in abs_section:
        legacy_abs_key = abs_section.pop("api_key")
        cleaned_abs_key = (
            legacy_abs_key.strip()
            if isinstance(legacy_abs_key, str) and legacy_abs_key.strip()
            else ""
        )
        if cleaned_abs_key:
            raw_abs_url = abs_section.get("url")
            abs_account = (
                raw_abs_url.strip().rstrip("/")
                if isinstance(raw_abs_url, str) and raw_abs_url.strip()
                else "api"
            )
            try:
                store.set_password(
                    "integration:audiobookshelf", abs_account, cleaned_abs_key
                )
                migrated = True
            except Exception as exc:
                abs_section["api_key"] = legacy_abs_key
                logging.debug(
                    f"Failed to migrate Audiobookshelf API key to OS keystore: {exc}"
                )
        else:
            migrated = True

    if migrated:
        if os.path.exists(config_location):
            try:
                from audiobookdl.config.io import _write_config_dict

                with file_lock(config_location):
                    _write_config_dict(config_location, config_dict)
            except OSError as exc:
                logging.debug(
                    f"Could not scrub migrated password from '{config_location}': {exc}"
                )



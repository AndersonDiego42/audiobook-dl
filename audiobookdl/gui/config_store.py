"""Persistent GUI configuration state and save/load helpers for `GuiController`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import threading
from typing import TYPE_CHECKING, Any

from audiobookdl.config import (
    Config,
    delete_custom_metadata_provider,
    delete_source_profile,
    load_config,
    rename_source_profile,
    save_audiobookshelf_config,
    save_check_updates,
    save_custom_metadata_provider,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.options_schema import PERSISTED_OUTPUT_PREF_SPECS
from audiobookdl.utils.archive import resolve_database_directory

if TYPE_CHECKING:
    from audiobookdl.options_schema import DownloadOptions


class GuiConfigStore:
    """Owns the cached `Config` and all GUI-facing configuration persistence."""

    def __init__(self, config_location: str | None = None) -> None:
        self.config_location = config_location
        self._config_lock = threading.Lock()
        self._config = self.load_saved()

    @property
    def config(self) -> Config:
        with self._config_lock:
            return self._config

    @config.setter
    def config(self, value: Config) -> None:
        with self._config_lock:
            self._config = value

    def load_saved(self) -> Config:
        """Load configuration from `config_location` (or default)."""
        if self.config_location and not os.path.exists(self.config_location):
            return Config(
                sources={},
                output_template=None,
                database_directory=None,
                skip_downloaded=None,
            )
        return load_config(self.config_location)

    def reload(self) -> Config:
        """Reload configuration from disk and replace the cached `Config`."""
        loaded = self.load_saved()
        with self._config_lock:
            self._config = loaded
        return loaded

    def snapshot(self) -> Config:
        """Return a thread-safe isolated copy of the current cached configuration."""
        with self._config_lock:
            return self._config.clone()

    def save_ffmpeg_dir(self, resolved_dir: str | None) -> Config:
        save_ffmpeg_dir(resolved_dir, overwrite=self.config_location)
        return self.reload()

    def save_custom_metadata_provider(
        self,
        name: str,
        *,
        url: str,
        api_key: str | None = None,
        enabled: bool = True,
    ) -> Config:
        """Persist or update a custom ABS-spec metadata provider and reload `self.config`."""
        save_custom_metadata_provider(
            name,
            url=url,
            api_key=api_key,
            enabled=enabled,
            overwrite=self.config_location,
        )
        return self.reload()

    def delete_custom_metadata_provider(self, name: str) -> Config:
        """Delete a custom ABS-spec metadata provider and reload `self.config`."""
        delete_custom_metadata_provider(name, overwrite=self.config_location)
        return self.reload()

    def save_source_credentials(
        self,
        service_key: str,
        *,
        username: str | None = None,
        password: str | None = None,
        library: str | None = None,
        cookie_file: str | None = None,
        profile: str | None = None,
        locale: str | None = None,
        oauth_client_id: str | None = None,
        oauth_scopes: Any = None,
        preserve_optional: bool = True,
    ) -> Config:
        extra_kwargs: dict[str, Any] = {}
        if not preserve_optional or locale is not None:
            extra_kwargs["locale"] = locale
        if not preserve_optional or oauth_client_id is not None:
            extra_kwargs["oauth_client_id"] = oauth_client_id
        if not preserve_optional or oauth_scopes is not None:
            extra_kwargs["oauth_scopes"] = oauth_scopes
        save_source_credentials(
            service_key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            overwrite=self.config_location,
            profile=profile,
            **extra_kwargs,
        )
        return self.reload()

    def delete_source_profile(
        self,
        service_key: str,
        *,
        profile: str | None = None,
    ) -> Config:
        """Delete a profile's config, vault secrets, OAuth tokens, and cached session/device files."""
        cfg = self.snapshot()
        db_dir = resolve_database_directory(cfg.database_directory)
        delete_source_profile(
            service_key,
            profile=profile,
            overwrite=self.config_location,
            database_directory=db_dir,
        )
        return self.reload()

    def rename_source_profile(
        self,
        service_key: str,
        old_profile: str,
        new_profile: str,
    ) -> Config:
        """Rename a configured service profile and migrate its secrets, OAuth tokens, and caches."""
        cfg = self.snapshot()
        db_dir = resolve_database_directory(cfg.database_directory)
        rename_source_profile(
            service_key,
            old_profile,
            new_profile,
            overwrite=self.config_location,
            database_directory=db_dir,
        )
        return self.reload()

    def save_run_preferences(
        self,
        options: DownloadOptions,
        *,
        output_dir: str,
        output_template: str,
        use_download_archive: bool | None = None,
    ) -> Config:
        prefs = {spec.name: getattr(options, spec.name) for spec in PERSISTED_OUTPUT_PREF_SPECS}
        save_output_preferences(config_location=self.config_location, **prefs)
        save_gui_preferences(
            output_dir=output_dir,
            output_template=output_template,
            overwrite=self.config_location,
            use_download_archive=use_download_archive,
        )
        return self.reload()

    def save_window_preferences(
        self,
        *,
        window_geometry: str | None,
        window_maximized: bool | None = None,
        download_split_ratio: float | None = None,
        active_page: str | None = None,
        options_collapsed: bool | None = None,
        output_naming_collapsed: bool | None = None,
        output_dir: str | None,
        output_template: str | None,
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
        output_prefs: Mapping[str, Any] | None = None,
    ) -> None:
        if output_prefs:
            save_output_preferences(config_location=self.config_location, **output_prefs)
        save_gui_preferences(
            window_geometry=window_geometry,
            window_maximized=window_maximized,
            download_split_ratio=download_split_ratio,
            active_page=active_page,
            options_collapsed=options_collapsed,
            output_naming_collapsed=output_naming_collapsed,
            output_dir=output_dir,
            output_template=output_template,
            overwrite=self.config_location,
            use_download_archive=use_download_archive,
            theme=theme,
            notify_on_complete=notify_on_complete,
            clipboard_monitor=clipboard_monitor,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
            queue_concurrency=queue_concurrency,
            minimize_to_tray=minimize_to_tray,
        )

    def save_gui_preferences(
        self,
        *,
        notify_on_complete: bool | None = None,
        clipboard_monitor: bool | None = None,
        auto_sync_enabled: bool | None = None,
        auto_sync_minutes: int | None = None,
        auto_sync_mode: str | None = None,
        auto_sync_targets: Sequence[str] | None = None,
        queue_concurrency: int | None = None,
        minimize_to_tray: bool | None = None,
    ) -> Config:
        """Persist incremental `[gui]` preferences and reload `self.config`."""
        save_gui_preferences(
            overwrite=self.config_location,
            notify_on_complete=notify_on_complete,
            clipboard_monitor=clipboard_monitor,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
            queue_concurrency=queue_concurrency,
            minimize_to_tray=minimize_to_tray,
        )
        return self.reload()

    def save_audiobookshelf_config(
        self,
        *,
        url: str,
        library_id: str,
        api_key: str,
        auto_upload: bool,
        auto_scan: bool,
    ) -> Config:
        """Persist `[audiobookshelf]` settings cleanly and reload `self.config` under lock."""
        save_audiobookshelf_config(
            url=url,
            library_id=library_id,
            api_key=api_key,
            auto_upload=auto_upload,
            auto_scan=auto_scan,
            overwrite=self.config_location,
        )
        return self.reload()

    def set_check_updates(self, enabled: bool) -> Config:
        """Persist `check_updates` in `audiobook-dl.toml` and reload `self.config`."""
        save_check_updates(enabled, overwrite=self.config_location)
        return self.reload()

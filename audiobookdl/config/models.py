"""Configuration data models and source-profile helpers."""

from attrs import Factory, define
from collections.abc import Sequence
import os
import re
from typing import Any

from audiobookdl.options_schema import CONFIG_OPTION_SPECS


@define
class SourceConfig:
    """Stores configuration for source"""
    username: str | None
    password: str | None
    library: str | None
    cookie_file: str | None
    locale: str | None = None
    oauth_client_id: str | None = None
    oauth_scopes: tuple[str, ...] | None = None
    oauth_token_present: bool = False


@define
class GuiConfig:
    """Stores desktop GUI layout and output preferences"""
    window_geometry: str | None = None
    window_maximized: bool | None = None
    download_split_ratio: float | None = None
    active_page: str | None = None
    options_collapsed: bool | None = None
    output_naming_collapsed: bool | None = None
    output_dir: str | None = None
    output_template: str | None = None
    use_download_archive: bool | None = None
    theme: str | None = None
    notify_on_complete: bool | None = None
    clipboard_monitor: bool | None = None
    auto_sync_enabled: bool | None = None
    auto_sync_minutes: int | None = None
    auto_sync_mode: str | None = None
    auto_sync_targets: list[str] | None = None
    queue_concurrency: int | None = None
    minimize_to_tray: bool | None = None


@define
class AudiobookshelfConfig:
    """Audiobookshelf server connection and sync configuration"""
    url: str | None = None
    api_key: str | None = None
    library_id: str | None = None
    auto_upload: bool = False
    auto_scan: bool = True


@define
class CustomMetadataProviderConfig:
    """Configuration for an Audiobookshelf-compatible custom metadata provider."""
    name: str
    url: str
    api_key: str | None = None
    enabled: bool = True


@define
class Config:
    """audiobook-dl config"""
    sources: dict[str, SourceConfig]
    output_template: str | None
    database_directory: str | None
    skip_downloaded: bool | None
    skip_unavailable: bool | None = None
    output_format: str | None = None
    media_type: str | None = None
    combine: bool | None = None
    ffmpeg_dir: str | None = None
    resume: bool | None = None
    audio_bitrate: str | None = None
    sample_rate: int | None = None
    overwrite: bool | None = None
    max_workers: int | None = None
    book_delay: float | None = None
    limit_rate: str | None = None
    proxy: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    impersonate: str | None = None
    locale: str | None = None
    post_exec: str | list[str] | None = None
    loudnorm: bool | None = None
    mono: bool | None = None
    chapters_from_silence: bool | None = None
    override_chapters: bool | None = None
    silence_noise_db: float | None = None
    silence_min_seconds: float | None = None
    chapter_offset_ms: int | None = None
    intro_chapter_mode: str | None = None
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
    custom_metadata_providers: dict[str, CustomMetadataProviderConfig] = Factory(dict)
    config_location: str | None = None

    def clone(self) -> "Config":
        """Return an isolated copy of this configuration for background jobs."""
        sources_copy = {
            name: SourceConfig(
                username=src.username,
                password=src.password,
                library=src.library,
                cookie_file=src.cookie_file,
                locale=src.locale,
                oauth_client_id=src.oauth_client_id,
                oauth_scopes=src.oauth_scopes,
                oauth_token_present=src.oauth_token_present,
            )
            for name, src in self.sources.items()
        }
        gui_copy = GuiConfig(
            window_geometry=self.gui.window_geometry,
            window_maximized=self.gui.window_maximized,
            download_split_ratio=self.gui.download_split_ratio,
            active_page=self.gui.active_page,
            options_collapsed=self.gui.options_collapsed,
            output_naming_collapsed=self.gui.output_naming_collapsed,
            output_dir=self.gui.output_dir,
            output_template=self.gui.output_template,
            use_download_archive=self.gui.use_download_archive,
            theme=self.gui.theme,
            notify_on_complete=self.gui.notify_on_complete,
            clipboard_monitor=self.gui.clipboard_monitor,
            auto_sync_enabled=self.gui.auto_sync_enabled,
            auto_sync_minutes=self.gui.auto_sync_minutes,
            auto_sync_mode=self.gui.auto_sync_mode,
            auto_sync_targets=(
                list(self.gui.auto_sync_targets)
                if self.gui.auto_sync_targets is not None
                else None
            ),
            queue_concurrency=self.gui.queue_concurrency,
            minimize_to_tray=self.gui.minimize_to_tray,
        )
        abs_copy = AudiobookshelfConfig(
            url=self.audiobookshelf.url,
            api_key=self.audiobookshelf.api_key,
            library_id=self.audiobookshelf.library_id,
            auto_upload=self.audiobookshelf.auto_upload,
            auto_scan=self.audiobookshelf.auto_scan,
        )
        custom_providers_copy = {
            name: CustomMetadataProviderConfig(
                name=prov.name,
                url=prov.url,
                api_key=prov.api_key,
                enabled=prov.enabled,
            )
            for name, prov in self.custom_metadata_providers.items()
        }
        kwargs: dict[str, Any] = {
            "sources": sources_copy,
            "gui": gui_copy,
            "audiobookshelf": abs_copy,
            "custom_metadata_providers": custom_providers_copy,
            "config_location": self.config_location,
        }
        for spec in CONFIG_OPTION_SPECS:
            if spec.config_key is not None:
                val = getattr(self, spec.config_key)
                kwargs[spec.config_key] = list(val) if isinstance(val, list) else val
        return Config(**kwargs)


_STATIC_SOURCE_ALIASES = {
    "audiobooksdotcom": "audiobooks.com",
    "libby": "overdrive",
    "mofibo": "storytel",
    "o'reilly": "oreilly",
    "oreillylearning": "oreilly",
    "scribd": "everand",
    "cloudlibrary": "yourcloudlibrary",
    "ytmusic": "youtubemusic",
    "youtube-music": "youtubemusic",
}


def normalize_source_key(raw_key: str) -> str:
    """Normalize a configured service key or alias (e.g. `scribd` -> `everand`)."""
    lowered = raw_key.strip().lower()
    if not lowered:
        return lowered
    if lowered in _STATIC_SOURCE_ALIASES:
        return _STATIC_SOURCE_ALIASES[lowered]
    try:
        from audiobookdl.sources import normalize_descriptor_source_key

        return normalize_descriptor_source_key(lowered)
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


def sanitize_profile_filename_part(profile: str | None) -> str | None:
    """Return a filesystem-safe profile suffix for non-default profiles, or `None`."""
    norm = normalize_profile_name(profile)
    if norm == DEFAULT_PROFILE:
        return None
    return re.sub(r"[^a-z0-9_-]", "_", norm)


def profile_scoped_filename(filename: str, profile: str | None) -> str:
    """Inject `_<profile>` before the extension of `filename` for non-default profiles."""
    safe_profile = sanitize_profile_filename_part(profile)
    if not safe_profile:
        return filename
    stem, ext = os.path.splitext(filename)
    return f"{stem}_{safe_profile}{ext}"


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


def resolve_effective_source_profile(
    config: Config,
    service_key: str,
    requested_profile: str | None = None,
    *,
    aliases: Sequence[str] = (),
) -> str:
    """Resolve the active profile for `service_key`, auto-selecting a sole named profile when `default` is absent."""
    if requested_profile is not None and requested_profile.strip():
        norm_req = normalize_profile_name(requested_profile)
        if norm_req != DEFAULT_PROFILE:
            return norm_req
    if get_source_config(config, service_key, DEFAULT_PROFILE, aliases=aliases) is not None:
        return DEFAULT_PROFILE
    profiles = list_source_profiles(config, service_key)
    if DEFAULT_PROFILE not in profiles and len(profiles) == 1:
        return profiles[0]
    return DEFAULT_PROFILE


def list_configured_source_profiles(config: Config) -> list[tuple[str, str, SourceConfig]]:
    """Return all configured `(service_key, profile, SourceConfig)` entries sorted deterministically."""
    out: list[tuple[str, str, SourceConfig]] = []
    for raw_key, src_cfg in config.sources.items():
        svc, prof = split_source_profile_key(raw_key)
        out.append((svc, prof, src_cfg))
    out.sort(key=lambda item: (item[0], 0 if item[1] == DEFAULT_PROFILE else 1, item[1]))
    return out

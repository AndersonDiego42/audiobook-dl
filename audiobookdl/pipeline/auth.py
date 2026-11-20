import os
import threading
from typing import Any


from .. import logging
from ..config import (
    DEFAULT_PROFILE,
    Config,
    SourceConfig,
    delete_source_oauth_token,
    get_source_config,
    load_source_oauth_token,
    normalize_profile_name,
    resolve_effective_source_profile,
    save_source_oauth_token,
)
from ..options_schema import (
    DownloadOptions,
)
from ..exceptions import (
    DownloadCancelled,
    UserNotAuthorized,
)
from ..sources import find_source_by_name
from ..sources.source import CatalogBook, Source


from audiobookdl.pipeline.common import (
    apply_runtime_options as apply_runtime_options,
    resolve_runtime_options as resolve_runtime_options,
)

def get_cookie_path(options: DownloadOptions, config: SourceConfig | None) -> str | None:
    """
    Find path to cookie file from `options.cookie_file` or source config.
    """
    if options.cookie_file is not None and os.path.exists(options.cookie_file):
        return options.cookie_file
    if config is not None and config.cookie_file is not None and os.path.exists(config.cookie_file):
        return config.cookie_file
    return None


def get_or_ask(
    attr: str,
    hidden: bool,
    options: DownloadOptions,
    config: SourceConfig | None,
    interaction: logging.InteractionHandler | None = None,
) -> str:
    """
    Check for `attr` in `options` and source config options.
    Ask the user for the value if it is not found.
    """
    config_value = getattr(config, attr, None)
    value: str | None = getattr(options, attr, None) or config_value
    if value is None:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        return handler.ask_input(attr.capitalize(), hidden=hidden)
    return value


def login(
    url: str,
    source: Source,
    options: DownloadOptions,
    config: SourceConfig | None,
    interaction: logging.InteractionHandler | None = None,
) -> None:
    """Login to `source`."""
    login_data = {}
    for name in source.login_data:
        hidden = name == "password"
        login_data[name] = get_or_ask(name, hidden, options, config, interaction=interaction)
    source.login(url, **login_data)


def _explicit_inline_credentials(options: DownloadOptions) -> bool:
    return bool(
        getattr(options, "username", None)
        or getattr(options, "password", None)
        or getattr(options, "cookie_file", None)
    )


def _resolve_request_profile(
    options: DownloadOptions,
    config: Config,
    source: Source,
) -> str:
    requested_profile = getattr(options, "profile", None)
    if _explicit_inline_credentials(options) and not (requested_profile and str(requested_profile).strip()):
        return DEFAULT_PROFILE
    return resolve_effective_source_profile(
        config,
        source.name,
        requested_profile,
        aliases=getattr(source, "names", ()),
    )


def _resolve_vault_config_location(
    options: DownloadOptions,
    config: Config,
    source: Any = None,
) -> str | None:
    raw = getattr(options, "config_location", None) or getattr(config, "config_location", None)
    if raw:
        return str(raw)
    if source is not None and getattr(source, "supports_oauth", False):
        from .config import get_config_location
        return get_config_location(None)
    return None


def _apply_source_config_overrides(source: Source, source_config: SourceConfig | None) -> None:
    if source_config is None:
        return
    if getattr(source_config, "locale", None):
        source._configured_locale = source_config.locale  # type: ignore[attr-defined]
    if getattr(source_config, "oauth_client_id", None):
        source._oauth_client_id_override = source_config.oauth_client_id
    if getattr(source_config, "oauth_scopes", None):
        source._oauth_scopes_override = source_config.oauth_scopes


def authenticate(
    url: str,
    source: Source,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    *,
    use_cached_cookies: bool = True,
    use_cached_oauth: bool = True,
) -> None:
    """Authenticate `source` using cookie file, persisted OAuth tokens, OAuth flow, or login credentials."""
    explicit_inline_creds = _explicit_inline_credentials(options)
    req_profile = _resolve_request_profile(options, config, source)
    profile_label = f" ({req_profile})" if req_profile != DEFAULT_PROFILE else ""
    logging.log(f"Authenticating with {source.name}{profile_label}")
    source_config = get_source_config(
        config,
        source.name,
        req_profile,
        aliases=getattr(source, "names", ()),
    )
    _apply_source_config_overrides(source, source_config)
    if (
        req_profile != DEFAULT_PROFILE
        and source_config is None
        and not explicit_inline_creds
    ):
        logging.warning(
            f"Warning: profile '{req_profile}' not found in config for {source.name}"
        )
    source._loaded_cached_cookies = False
    source._loaded_cached_oauth = False

    cookie_path = get_cookie_path(options, source_config)
    if cookie_path is not None:
        source.load_cookie_file(cookie_path)
    elif (
        use_cached_cookies
        and getattr(source, "supports_cookies", False)
        and hasattr(source, "cached_cookie_path")
        and os.path.exists(source.cached_cookie_path())
    ):
        source.load_cookie_file(source.cached_cookie_path())
        source._loaded_cached_cookies = bool(source.authenticated)

    vault_loc = _resolve_vault_config_location(options, config, source)
    can_use_vault_oauth = bool(
        use_cached_oauth
        and not explicit_inline_creds
        and not source.authenticated
        and (
            vault_loc is not None
            or (source_config is not None and getattr(source_config, "oauth_token_present", False))
            or getattr(source, "supports_oauth", False)
        )
    )
    if can_use_vault_oauth and hasattr(source, "apply_oauth_token"):
        cached_token = load_source_oauth_token(
            source.name,
            profile=req_profile,
            overwrite=vault_loc,
        )
        if cached_token is not None:
            try:
                source.apply_oauth_token(cached_token)
                if cached_token.is_expired():
                    refreshed = source.refresh_oauth_token_if_needed(force=True)
                    if refreshed is not None:
                        save_source_oauth_token(
                            source.name,
                            refreshed,
                            profile=req_profile,
                            overwrite=vault_loc,
                        )
                        source._loaded_cached_oauth = True
                    else:
                        delete_source_oauth_token(
                            source.name,
                            profile=req_profile,
                            overwrite=vault_loc,
                        )
                        source.reset_authentication(clear_cookies=False)
                else:
                    source._loaded_cached_oauth = True
            except UserNotAuthorized:
                delete_source_oauth_token(
                    source.name,
                    profile=req_profile,
                    overwrite=vault_loc,
                )
                source.reset_authentication(clear_cookies=False)

    prefer_inline_login = bool(
        explicit_inline_creds
        and getattr(options, "username", None)
        and getattr(source, "supports_login", False)
    )
    if getattr(source, "supports_oauth", False) and not source.authenticated and not prefer_inline_login:
        try:
            new_token = source.oauth_authorize(url, interaction=interaction)
        except UserNotAuthorized:
            if not getattr(source, "supports_login", False):
                raise
        else:
            if vault_loc is not None or getattr(source, "supports_oauth", False):
                save_source_oauth_token(
                    source.name,
                    new_token,
                    profile=req_profile,
                    overwrite=vault_loc,
                )

    if getattr(source, "supports_login", False) and not source.authenticated:
        login(url, source, options, source_config, interaction=interaction)
        source._loaded_cached_cookies = False
        issued_token = getattr(source, "oauth_token", None)
        if issued_token is not None and vault_loc is not None and not explicit_inline_creds:
            try:
                save_source_oauth_token(
                    source.name,
                    issued_token,
                    profile=req_profile,
                    overwrite=vault_loc,
                )
            except Exception as exc:
                logging.debug(f"Could not persist OAuth token for {source.name}: {exc}")
        if (
            getattr(source, "supports_cookies", False)
            and hasattr(source, "save_cookie_file")
            and getattr(options, "database_directory", None)
            and len(source._session.cookies) > 0
        ):
            try:
                source.save_cookie_file()
            except OSError as exc:
                logging.debug(f"Could not persist session cookies for {source.name}: {exc}")


def authorize_service_oauth(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    profile: str | None = None,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    open_browser: bool = True,
) -> Any:
    """Run OAuth 2.0 interactive authorization for `(service_name, profile)` and persist tokens."""
    from dataclasses import replace as _dc_replace

    source_class = find_source_by_name(service_name)
    norm_profile = normalize_profile_name(profile if profile is not None else getattr(options, "profile", None))
    run_options = resolve_runtime_options(
        _dc_replace(options, profile=None if norm_profile == DEFAULT_PROFILE else norm_profile),
        config,
    )
    with logging.job_context(apply_runtime_options(run_options, config)):
        source = source_class(run_options)
        try:
            source_config = get_source_config(
                config,
                source.name,
                norm_profile,
                aliases=getattr(source, "names", ()),
            )
            _apply_source_config_overrides(source, source_config)
            token = source.oauth_authorize(
                interaction=interaction,
                cancel_event=cancel_event,
                open_browser=open_browser,
            )
            vault_loc = _resolve_vault_config_location(run_options, config, source)
            save_source_oauth_token(
                source.name,
                token,
                profile=norm_profile,
                overwrite=vault_loc,
            )
            return token
        finally:
            _close_source(source)


def disconnect_service_oauth(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    profile: str | None = None,
) -> bool:
    """Revoke (best-effort) and remove any persisted OAuth token bundle for `(service_name, profile)`."""
    from audiobookdl.utils.oauth import revoke_oauth_token

    source_class = find_source_by_name(service_name)
    norm_profile = normalize_profile_name(profile if profile is not None else getattr(options, "profile", None))
    vault_loc = _resolve_vault_config_location(options, config)
    token = load_source_oauth_token(
        source_class.names[0].lower() if source_class.names else service_name,
        profile=norm_profile,
        overwrite=vault_loc,
    )
    revoked = False
    if token is not None:
        source = source_class(options)
        try:
            source_config = get_source_config(
                config,
                source.name,
                norm_profile,
                aliases=getattr(source, "names", ()),
            )
            _apply_source_config_overrides(source, source_config)
            provider = source.get_oauth_provider_config()
            if provider is not None:
                revoked = revoke_oauth_token(provider, token, session=getattr(source, "_session", None))
        finally:
            _close_source(source)
        delete_source_oauth_token(
            source_class.names[0].lower() if source_class.names else service_name,
            profile=norm_profile,
            overwrite=vault_loc,
        )
    return revoked


def _init_service_source(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    url: str = "",
) -> Source:
    """Instantiate `service_name` and authenticate when required."""
    source_class = find_source_by_name(service_name)
    source = source_class(options)
    handler = interaction or logging.NonInteractiveInteractionHandler()
    if source.requires_authentication and not source.authenticated:
        authenticate(url, source, options, config, interaction=handler)
    return source


def _close_source(source: Any) -> None:
    close_fn = getattr(source, "close", None)
    if callable(close_fn):
        close_fn()


def verify_service_auth(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    persist_cookies: bool = True,
    cancel_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
) -> bool:
    """Authenticate against `service_name` and optionally cache session cookies."""
    resolved_options = resolve_runtime_options(options, config)
    ctx = apply_runtime_options(resolved_options, config)
    ctx.cancel_event = cancel_event
    ctx.sink = sink if sink is not None else logging.get_ui_sink()
    with logging.job_context(ctx):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Verification cancelled")
        source = _init_service_source(service_name, resolved_options, config, interaction=interaction)
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Verification cancelled")
            ok = source.verify_auth()
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Verification cancelled")
            if ok and persist_cookies and source.supports_cookies and len(source._session.cookies) > 0:
                source.save_cookie_file()
            return ok
        finally:
            _close_source(source)


def search_service(
    service_name: str,
    query: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
) -> list[CatalogBook]:
    """Search `service_name` catalog for `query`."""
    resolved_options = resolve_runtime_options(options, config)
    ctx = apply_runtime_options(resolved_options, config)
    ctx.cancel_event = cancel_event
    ctx.sink = sink if sink is not None else logging.get_ui_sink()
    with logging.job_context(ctx):
        source = _init_service_source(service_name, resolved_options, config, interaction=interaction)
        try:
            return source.search(query)
        finally:
            _close_source(source)


def list_service_library(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
) -> list[CatalogBook]:
    """List books from `service_name`'s user library/bookshelf."""
    resolved_options = resolve_runtime_options(options, config)
    ctx = apply_runtime_options(resolved_options, config)
    ctx.cancel_event = cancel_event
    ctx.sink = sink if sink is not None else logging.get_ui_sink()
    with logging.job_context(ctx):
        source = _init_service_source(service_name, resolved_options, config, interaction=interaction)
        try:
            return source.list_library()
        finally:
            _close_source(source)



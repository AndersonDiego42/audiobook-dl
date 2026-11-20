"""Provider registry: custom/plugin provider discovery, provider-list parsing, and shared HTTP helpers."""

from contextlib import nullcontext
import functools
import importlib.metadata
import threading
from typing import Any
from urllib.parse import urlparse
from collections.abc import Mapping

import requests

from audiobookdl import logging
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
    make_cache_key,
)
from audiobookdl.utils.http import (
    DEFAULT_USER_AGENT,
    create_configured_http_session,
)
from audiobookdl.utils.enrichment.models import (
    CUSTOM_PROVIDER_PREFIX,
    DEFAULT_PROVIDERS,
    ENRICHMENT_ENTRY_POINT_GROUP,
    PLUGIN_PROVIDER_PREFIX,
    PROVIDER_ALIASES,
    _CUSTOM_PROVIDER_NAME_RE,
    _raise_if_cancelled,
)


def normalize_custom_provider_name(raw: str) -> str:
    """Validate and normalize a custom metadata provider slug."""
    cleaned = str(raw or "").strip().lower()
    if cleaned.startswith(CUSTOM_PROVIDER_PREFIX):
        cleaned = cleaned[len(CUSTOM_PROVIDER_PREFIX) :].strip()
    if not cleaned or not _CUSTOM_PROVIDER_NAME_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid custom provider name {raw!r}; use lowercase letters, digits, '-' or '_'"
        )
    if cleaned in PROVIDER_ALIASES:
        raise ValueError(
            f"Custom provider name '{cleaned}' conflicts with built-in provider name"
        )
    return cleaned


def validate_custom_provider_url(raw: str) -> str:
    """Validate and normalize a base HTTP(S) URL for an ABS custom metadata provider."""
    cleaned = str(raw or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Invalid custom provider URL {raw!r}; expected http:// or https:// URL"
        )
    return cleaned


def _lookup_custom_provider(
    slug: str,
    custom_providers: Mapping[str, Any] | None,
) -> Any | None:
    if not custom_providers:
        return None
    if slug in custom_providers:
        return custom_providers[slug]
    lowered = slug.lower()
    for key, val in custom_providers.items():
        if str(key).strip().lower() == lowered:
            return val
    return None


@functools.lru_cache(maxsize=1)
def _discover_plugin_enrichment_providers() -> dict[str, Any]:
    """Discover third-party enrichment plugins from the `audiobookdl.enrichment` entry-point group."""
    plugins: dict[str, Any] = {}
    try:
        eps = importlib.metadata.entry_points(group=ENRICHMENT_ENTRY_POINT_GROUP)
    except Exception as exc:
        logging.debug(f"Failed querying enrichment entry points: {exc}")
        return plugins
    for ep in eps:
        raw_name = str(getattr(ep, "name", "") or "").strip().lower()
        if not raw_name or raw_name in PROVIDER_ALIASES:
            continue
        try:
            loaded = ep.load()
        except Exception as exc:
            logging.debug(f"Failed loading enrichment plugin '{raw_name}': {exc}")
            continue
        if callable(loaded) or hasattr(loaded, "query"):
            plugins[raw_name] = loaded
    return plugins


def reset_plugin_enrichment_providers_cache() -> None:
    """Clear the cached `audiobookdl.enrichment` entry-point lookup (for tests/reloads)."""
    _discover_plugin_enrichment_providers.cache_clear()


def parse_provider_list(
    raw: str | None,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Parse a comma-separated provider list into canonical deduplicated provider identifiers."""
    if raw is None or not raw.strip():
        return DEFAULT_PROVIDERS
    plugin_providers = _discover_plugin_enrichment_providers()
    resolved: list[str] = []
    seen = set()
    for token in raw.split(","):
        cleaned = token.strip().lower()
        if not cleaned:
            continue
        canonical = PROVIDER_ALIASES.get(cleaned)
        if canonical is None:
            if cleaned.startswith(CUSTOM_PROVIDER_PREFIX):
                slug = normalize_custom_provider_name(cleaned)
                if custom_providers is not None and _lookup_custom_provider(slug, custom_providers) is None:
                    raise ValueError(
                        f"Unknown custom enrichment provider '{token.strip()}'. "
                        f"Configured custom providers: {', '.join(custom_providers.keys()) or 'none'}"
                    )
                canonical = f"{CUSTOM_PROVIDER_PREFIX}{slug}"
            elif cleaned.startswith(PLUGIN_PROVIDER_PREFIX):
                pslug = cleaned[len(PLUGIN_PROVIDER_PREFIX) :].strip()
                if pslug not in plugin_providers:
                    raise ValueError(
                        f"Unknown enrichment plugin '{token.strip()}'. "
                        f"Discovered plugins: {', '.join(sorted(plugin_providers)) or 'none'}"
                    )
                canonical = f"{PLUGIN_PROVIDER_PREFIX}{pslug}"
            elif custom_providers is not None and _lookup_custom_provider(cleaned, custom_providers) is not None:
                canonical = f"{CUSTOM_PROVIDER_PREFIX}{cleaned}"
            elif cleaned in plugin_providers:
                canonical = f"{PLUGIN_PROVIDER_PREFIX}{cleaned}"
        if canonical is None:
            valid = (
                list(DEFAULT_PROVIDERS)
                + [f"{CUSTOM_PROVIDER_PREFIX}{k}" for k in (custom_providers or {})]
                + [f"{PLUGIN_PROVIDER_PREFIX}{k}" for k in sorted(plugin_providers)]
            )
            raise ValueError(
                f"Unknown enrichment provider '{token.strip()}'. Valid providers: {', '.join(valid)}"
            )
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    if not resolved:
        raise ValueError("At least one enrichment provider must be specified")
    return tuple(resolved)


def resolve_provider_list(
    raw: str | None = None,
    *,
    custom_providers: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return explicit `raw` providers, or `DEFAULT_PROVIDERS` plus enabled custom/plugin providers."""
    if raw is not None and raw.strip():
        return parse_provider_list(raw, custom_providers=custom_providers)
    resolved: list[str] = list(DEFAULT_PROVIDERS)
    if custom_providers:
        for key, cfg in custom_providers.items():
            if not getattr(cfg, "enabled", True):
                continue
            name = getattr(cfg, "name", None) or key
            try:
                slug = normalize_custom_provider_name(str(name))
            except ValueError:
                continue
            token = f"{CUSTOM_PROVIDER_PREFIX}{slug}"
            if token not in resolved:
                resolved.append(token)
    for pslug in sorted(_discover_plugin_enrichment_providers()):
        token = f"{PLUGIN_PROVIDER_PREFIX}{pslug}"
        if token not in resolved:
            resolved.append(token)
    return tuple(resolved)


def resolve_redirect_url(
    url: str,
    *,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    cancel_event: threading.Event | None = None,
) -> str:
    """Follow HTTP redirects on short/share links to obtain the canonical target URL."""
    _raise_if_cancelled(cancel_event)
    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        return url
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    session_cm = (
        nullcontext(session)
        if session is not None
        else create_configured_http_session(proxy=proxy)
    )
    try:
        with session_cm as sess:
            resp = sess.head(
                cleaned,
                allow_redirects=True,
                timeout=timeout,
                proxies=proxies,
                headers=headers,
            )
            if resp.status_code in (403, 405) or resp.url == cleaned:
                resp = sess.get(
                    cleaned,
                    allow_redirects=True,
                    stream=True,
                    timeout=timeout,
                    proxies=proxies,
                    headers=headers,
                )
                try:
                    final_url = resp.url or cleaned
                finally:
                    resp.close()
                return final_url
            return resp.url or cleaned
    except requests.RequestException:
        return url


def _cached_get_json(
    session: requests.Session,
    provider: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    cancel_event: threading.Event | None = None,
) -> Any | None:
    _raise_if_cancelled(cancel_event)
    cache_key = make_cache_key(provider, url, params, headers=headers)
    if cache is not None and use_cache:
        cached = cache.get(cache_key, ttl_seconds=cache_ttl_seconds)
        if cached is not None:
            return cached

    proxies = {"http": proxy, "https": proxy} if proxy else None
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        req_headers.update(headers)
    try:
        resp = session.get(
            url,
            params=params,
            timeout=timeout,
            proxies=proxies,
            headers=req_headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logging.debug(f"Enrichment request failed ({provider} {url}): {exc}")
        return None

    if cache is not None and isinstance(payload, (dict, list)):
        cache.set(cache_key, provider, payload)
    return payload

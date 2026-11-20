"""Provider dispatch: route a canonical provider identifier to its query implementation."""

import threading
from typing import Any
from collections.abc import Mapping

import requests

from audiobookdl import logging
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
)
from audiobookdl.utils.enrichment.matching import score_candidate_match
from audiobookdl.utils.enrichment.models import (
    CUSTOM_PROVIDER_PREFIX,
    DEFAULT_CONFIDENCE_THRESHOLD,
    PLUGIN_PROVIDER_PREFIX,
    EnrichmentCandidate,
    _raise_if_cancelled,
)
from audiobookdl.utils.enrichment.providers.abs_custom import (
    _parse_abs_custom_match,
    _query_abs_custom_provider,
)
from audiobookdl.utils.enrichment.providers.audnexus import _query_audnexus
from audiobookdl.utils.enrichment.providers.google import _query_google
from audiobookdl.utils.enrichment.providers.itunes import _query_itunes
from audiobookdl.utils.enrichment.providers.openlibrary import _query_openlibrary
from audiobookdl.utils.enrichment.providers.registry import (
    _discover_plugin_enrichment_providers,
    _lookup_custom_provider,
)


def _query_provider(
    provider: str,
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    custom_providers: Mapping[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[EnrichmentCandidate]:
    _raise_if_cancelled(cancel_event)
    if provider == "audnexus":
        return _query_audnexus(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            min_confidence=min_confidence,
            cancel_event=cancel_event,
        )
    if provider == "itunes":
        return _query_itunes(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            cancel_event=cancel_event,
        )
    if provider == "google":
        return _query_google(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            cancel_event=cancel_event,
        )
    if provider == "openlibrary":
        return _query_openlibrary(
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            cancel_event=cancel_event,
        )
    if provider.startswith(CUSTOM_PROVIDER_PREFIX):
        slug = provider[len(CUSTOM_PROVIDER_PREFIX) :].strip().lower()
        provider_cfg = _lookup_custom_provider(slug, custom_providers)
        if provider_cfg is None:
            raise ValueError(f"Custom enrichment provider '{slug}' is not configured")
        return _query_abs_custom_provider(
            provider_cfg,
            metadata,
            session=session,
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            cancel_event=cancel_event,
        )
    if provider.startswith(PLUGIN_PROVIDER_PREFIX):
        _raise_if_cancelled(cancel_event)
        pslug = provider[len(PLUGIN_PROVIDER_PREFIX) :].strip().lower()
        plugin = _discover_plugin_enrichment_providers().get(pslug)
        if plugin is None:
            raise ValueError(f"Enrichment plugin '{pslug}' is not installed")
        query_fn = getattr(plugin, "query", plugin)
        try:
            raw_items = query_fn(
                metadata,
                session=session,
                proxy=proxy,
                timeout=timeout,
            )
        except DownloadCancelled:
            raise
        except Exception as exc:
            logging.debug(f"Enrichment plugin '{pslug}' query failed: {exc}")
            return []
        _raise_if_cancelled(cancel_event)
        candidates: list[EnrichmentCandidate] = []
        for item in raw_items or ():
            if isinstance(item, EnrichmentCandidate):
                cand = item
            elif isinstance(item, Mapping):
                parsed = _parse_abs_custom_match(provider, item)
                if parsed is None:
                    continue
                cand = parsed
            else:
                continue
            if cand.score <= 0.0:
                cand.score = score_candidate_match(metadata, cand)
            candidates.append(cand)
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates
    raise ValueError(f"Unknown enrichment provider: {provider!r}")

"""Enrichment engine: candidate application, cover download, and top-level search/enrich orchestration."""

from contextlib import nullcontext
from io import BytesIO
import threading
from typing import Any
from collections.abc import Mapping, Sequence

from PIL import Image
import requests

from audiobookdl import logging
from audiobookdl.utils.concurrency import run_cancellable_pool
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookMetadata,
    Cover,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
    default_enrichment_cache_path,
)
from audiobookdl.utils.http import (
    DEFAULT_USER_AGENT,
    create_configured_http_session,
)
from audiobookdl.utils.enrichment.models import (
    CUSTOM_PROVIDER_PREFIX,
    DEFAULT_CONFIDENCE_THRESHOLD,
    PLUGIN_PROVIDER_PREFIX,
    CandidateSelector,
    EnrichmentCandidate,
    EnrichmentResult,
    _normalize_title,
    _raise_if_cancelled,
)
from audiobookdl.utils.enrichment.providers import dispatch, registry
from audiobookdl.utils.enrichment.providers.audnexus import _fetch_audnexus_chapters


MAX_COVER_BYTES = 25 * 1024 * 1024


def _download_cover(
    session: requests.Session,
    cover_url: str,
    *,
    proxy: str | None = None,
    timeout: float = 10.0,
    cancel_event: threading.Event | None = None,
) -> Cover | None:
    _raise_if_cancelled(cancel_event)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = None
    try:
        resp = session.get(
            cover_url,
            timeout=timeout,
            proxies=proxies,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            stream=True,
        )
        resp.raise_for_status()
        iter_content = getattr(resp, "iter_content", None)
        if callable(iter_content):
            chunks = iter_content(chunk_size=64 * 1024)
        else:
            content = getattr(resp, "content", b"") or b""
            chunks = (
                content[off : off + 64 * 1024]
                for off in range(0, len(content), 64 * 1024)
            )
        buf = bytearray()
        for chunk in chunks:
            _raise_if_cancelled(cancel_event)
            if not chunk:
                continue
            remaining = MAX_COVER_BYTES - len(buf)
            if remaining <= 0:
                logging.debug(f"Enrichment cover exceeds {MAX_COVER_BYTES} bytes, skipping ({cover_url})")
                return None
            buf.extend(chunk[:remaining])
        raw_bytes = bytes(buf)
    except requests.RequestException as exc:
        logging.debug(f"Enrichment cover download skipped ({cover_url}): {exc}")
        return None
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    if not raw_bytes:
        return None
    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img.verify()
            fmt = (img.format or "").lower()
    except Exception as exc:
        logging.debug(f"Enrichment cover image invalid ({cover_url}): {exc}")
        return None
    ext = "jpg" if fmt == "jpeg" else (fmt or "jpg")
    return Cover(image=raw_bytes, extension=ext)


_PROVIDER_CAPABILITIES: dict[str, frozenset[str]] = {
    "audnexus": frozenset({
        "title",
        "authors",
        "narrators",
        "series",
        "series_order",
        "genres",
        "language",
        "description",
        "asin",
        "publisher",
        "release_date",
        "cover",
        "chapters",
    }),
    "itunes": frozenset({
        "title",
        "authors",
        "genres",
        "description",
        "release_date",
        "cover",
    }),
    "google": frozenset({
        "title",
        "authors",
        "genres",
        "language",
        "description",
        "isbn",
        "publisher",
        "release_date",
        "cover",
    }),
    "openlibrary": frozenset({
        "title",
        "authors",
        "genres",
        "description",
        "isbn",
        "publisher",
        "release_date",
        "cover",
    }),
}


_ABS_CUSTOM_CAPABILITIES: frozenset[str] = frozenset({
    "title",
    "authors",
    "narrators",
    "series",
    "series_order",
    "genres",
    "language",
    "description",
    "isbn",
    "asin",
    "publisher",
    "release_date",
    "cover",
})


def _provider_capabilities_for(provider: str) -> frozenset[str] | None:
    if provider.startswith(CUSTOM_PROVIDER_PREFIX):
        return _ABS_CUSTOM_CAPABILITIES
    if provider.startswith(PLUGIN_PROVIDER_PREFIX):
        pslug = provider[len(PLUGIN_PROVIDER_PREFIX) :].strip().lower()
        plugin = registry._discover_plugin_enrichment_providers().get(pslug)
        caps = getattr(plugin, "capabilities", None) if plugin is not None else None
        return frozenset(caps) if caps else None
    return _PROVIDER_CAPABILITIES.get(provider)


def _missing_enrichment_fields(
    audiobook: Audiobook,
    *,
    include_chapters: bool = True,
    override_chapters: bool = False,
) -> set[str]:
    meta = audiobook.metadata
    missing: set[str] = set()
    if not meta.title:
        missing.add("title")
    if not meta.authors:
        missing.add("authors")
    if not meta.narrators:
        missing.add("narrators")
    if not meta.series:
        missing.add("series")
    if meta.series_order is None:
        missing.add("series_order")
    if not meta.genres:
        missing.add("genres")
    if meta.language is None:
        missing.add("language")
    if not meta.description:
        missing.add("description")
    if not meta.isbn:
        missing.add("isbn")
    if not meta.asin:
        missing.add("asin")
    if not meta.publisher:
        missing.add("publisher")
    if meta.release_date is None:
        missing.add("release_date")
    if audiobook.cover is None:
        missing.add("cover")
    if include_chapters and (override_chapters or not audiobook.chapters):
        missing.add("chapters")
    return missing


def _has_missing_enrichment_fields(
    audiobook: Audiobook,
    *,
    include_chapters: bool = True,
    override_chapters: bool = False,
) -> bool:
    return bool(
        _missing_enrichment_fields(
            audiobook,
            include_chapters=include_chapters,
            override_chapters=override_chapters,
        )
    )


def apply_candidate_to_audiobook(
    audiobook: Audiobook,
    candidate: EnrichmentCandidate,
    *,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    include_chapters: bool = True,
    override_chapters: bool = False,
    cancel_event: threading.Event | None = None,
) -> tuple[list[str], bool, bool]:
    """
    Apply missing metadata fields, cover, and chapters from `candidate` onto
    `audiobook` without overwriting existing non-empty fields unless
    `override_chapters` is enabled for chapters.
    """
    meta = audiobook.metadata
    updated_fields: list[str] = []

    if not meta.title and candidate.title:
        meta.title = candidate.title
        updated_fields.append("title")
    if not meta.authors and candidate.authors:
        meta.add_authors(candidate.authors)
        updated_fields.append("authors")
    if not meta.narrators and candidate.narrators:
        meta.add_narrators(candidate.narrators)
        updated_fields.append("narrators")
    if not meta.series and candidate.series:
        meta.series = candidate.series
        updated_fields.append("series")
        if meta.series_order is None and candidate.series_order is not None:
            meta.series_order = candidate.series_order
            updated_fields.append("series_order")
    elif (
        meta.series
        and meta.series_order is None
        and candidate.series
        and candidate.series_order is not None
        and _normalize_title(meta.series) == _normalize_title(candidate.series)
    ):
        meta.series_order = candidate.series_order
        updated_fields.append("series_order")
    if not meta.genres and candidate.genres:
        meta.add_genres(candidate.genres)
        updated_fields.append("genres")
    if meta.language is None and candidate.language is not None:
        meta.language = candidate.language
        updated_fields.append("language")
    if not meta.description and candidate.description:
        meta.description = candidate.description
        updated_fields.append("description")
    if not meta.isbn and candidate.isbn:
        meta.isbn = candidate.isbn
        updated_fields.append("isbn")
    if not meta.asin and candidate.asin:
        meta.asin = candidate.asin
        updated_fields.append("asin")
    if not meta.publisher and candidate.publisher:
        meta.publisher = candidate.publisher
        updated_fields.append("publisher")
    if meta.release_date is None and candidate.release_date is not None:
        meta.release_date = candidate.release_date
        updated_fields.append("release_date")

    cover_updated = False
    chapters_updated = False
    need_cover = audiobook.cover is None and bool(candidate.cover_url)
    need_chapters = (
        include_chapters
        and (override_chapters or not audiobook.chapters)
        and (bool(candidate.chapters) or (candidate.provider == "audnexus" and bool(candidate.asin)))
    )
    if need_cover or need_chapters:
        session_cm: Any = (
            nullcontext(session)
            if session is not None
            else create_configured_http_session(proxy=proxy)
        )
        with session_cm as sess:
            if need_cover and candidate.cover_url:
                cover = _download_cover(
                    sess,
                    candidate.cover_url,
                    proxy=proxy,
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
                if cover is not None:
                    audiobook.cover = cover
                    cover_updated = True
            _raise_if_cancelled(cancel_event)
            if need_chapters:
                chapters = list(candidate.chapters)
                if not chapters and candidate.provider == "audnexus" and candidate.asin:
                    chapters = _fetch_audnexus_chapters(
                        sess,
                        candidate.asin,
                        proxy=proxy,
                        timeout=timeout,
                        cache=cache,
                        use_cache=use_cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                        cancel_event=cancel_event,
                    )
                if chapters:
                    audiobook.chapters = chapters
                    chapters_updated = True

    return updated_fields, cover_updated, chapters_updated


def search_enrichment_candidates(
    metadata: AudiobookMetadata,
    *,
    providers: Sequence[str] | None = None,
    custom_providers: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    database_directory: str | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    cancel_event: threading.Event | None = None,
) -> list[EnrichmentCandidate]:
    """Query configured providers and return scored `EnrichmentCandidate` entries."""
    _raise_if_cancelled(cancel_event)
    provider_list = (
        tuple(providers)
        if providers is not None
        else registry.resolve_provider_list(None, custom_providers=custom_providers)
    )
    cache_cm: Any = (
        EnrichmentCache(
            default_enrichment_cache_path(database_directory),
            ttl_seconds=cache_ttl_seconds,
            persistent=True,
        )
        if database_directory
        else nullcontext(None)
    )
    collected: list[EnrichmentCandidate] = []
    with cache_cm as cache:
        if session is None and len(provider_list) > 1:
            def _query_worker(prov: str) -> list[EnrichmentCandidate]:
                with create_configured_http_session(proxy=proxy) as worker_sess:
                    return dispatch._query_provider(
                        prov,
                        metadata,
                        session=worker_sess,
                        proxy=proxy,
                        timeout=timeout,
                        cache=cache,
                        use_cache=use_cache,
                        cache_ttl_seconds=cache_ttl_seconds,
                        min_confidence=min_confidence,
                        custom_providers=custom_providers,
                        cancel_event=cancel_event,
                    )

            for batch in run_cancellable_pool(
                _query_worker,
                provider_list,
                max_workers=min(4, len(provider_list)),
                cancel_event=cancel_event,
            ):
                collected.extend(batch)
        else:
            session_cm: Any = (
                nullcontext(session)
                if session is not None
                else create_configured_http_session(proxy=proxy)
            )
            with session_cm as sess:
                for provider in provider_list:
                    _raise_if_cancelled(cancel_event)
                    collected.extend(
                        dispatch._query_provider(
                            provider,
                            metadata,
                            session=sess,
                            proxy=proxy,
                            timeout=timeout,
                            cache=cache,
                            use_cache=use_cache,
                            cache_ttl_seconds=cache_ttl_seconds,
                            min_confidence=min_confidence,
                            custom_providers=custom_providers,
                            cancel_event=cancel_event,
                        )
                    )
    collected.sort(key=lambda c: c.score, reverse=True)
    return collected


def enrich_audiobook_metadata(
    audiobook: Audiobook,
    *,
    providers: Sequence[str] | None = None,
    custom_providers: Mapping[str, Any] | None = None,
    session: requests.Session | None = None,
    proxy: str | None = None,
    timeout: float = 10.0,
    database_directory: str | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    interactive: bool = False,
    selector: CandidateSelector | None = None,
    include_chapters: bool = True,
    override_chapters: bool = False,
    cancel_event: threading.Event | None = None,
) -> EnrichmentResult:
    """
    Enrich `audiobook` across `providers` in priority order, filling missing
    metadata, cover art, and chapters without overwriting existing source data
    (unless `override_chapters` replaces existing chapters).
    """
    provider_list = (
        tuple(providers)
        if providers is not None
        else registry.resolve_provider_list(None, custom_providers=custom_providers)
    )
    result = EnrichmentResult()
    if not _has_missing_enrichment_fields(
        audiobook,
        include_chapters=include_chapters,
        override_chapters=override_chapters,
    ):
        return result

    cache_cm: Any = (
        EnrichmentCache(
            default_enrichment_cache_path(database_directory),
            ttl_seconds=cache_ttl_seconds,
            persistent=True,
        )
        if database_directory
        else nullcontext(None)
    )
    session_cm: Any = (
        nullcontext(session)
        if session is not None
        else create_configured_http_session(proxy=proxy)
    )
    chapters_overridden = False

    with cache_cm as cache, session_cm as sess:
        for provider in provider_list:
            _raise_if_cancelled(cancel_event)
            effective_override = override_chapters and not chapters_overridden
            missing = _missing_enrichment_fields(
                audiobook,
                include_chapters=include_chapters,
                override_chapters=effective_override,
            )
            if not missing:
                break
            supported = _provider_capabilities_for(provider)
            if supported is not None and missing.isdisjoint(supported):
                continue
            candidates = dispatch._query_provider(
                provider,
                audiobook.metadata,
                session=sess,
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                min_confidence=min_confidence,
                custom_providers=custom_providers,
                cancel_event=cancel_event,
            )
            if not candidates:
                continue

            chosen: EnrichmentCandidate | None = None
            if interactive and selector is not None:
                chosen_idx = selector(provider, candidates)
                if chosen_idx is not None and 0 <= chosen_idx < len(candidates):
                    chosen = candidates[chosen_idx]
            else:
                top = candidates[0]
                if top.score >= min_confidence:
                    chosen = top

            if chosen is None:
                continue

            fields, cover_updated, chapters_updated = apply_candidate_to_audiobook(
                audiobook,
                chosen,
                session=sess,
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                include_chapters=include_chapters,
                override_chapters=effective_override,
                cancel_event=cancel_event,
            )
            result.providers_used.append(provider)
            result.matched_candidates.append(chosen)
            if fields or cover_updated or chapters_updated:
                result.updated = True
                result.fields_updated.extend(f for f in fields if f not in result.fields_updated)
                result.cover_updated = result.cover_updated or cover_updated
                result.chapters_updated = result.chapters_updated or chapters_updated
            if chapters_updated:
                chapters_overridden = True
            if not _has_missing_enrichment_fields(
                audiobook,
                include_chapters=include_chapters,
                override_chapters=override_chapters and not chapters_overridden,
            ):
                break

    return result

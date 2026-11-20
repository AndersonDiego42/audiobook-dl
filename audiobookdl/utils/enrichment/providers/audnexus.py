"""Audible/Audnex.us enrichment provider."""

from typing import Any
from collections.abc import Mapping

import threading

import requests

from audiobookdl.utils.audiobook import (
    AudiobookMetadata,
    Chapter,
    clean_html_description,
    parse_language,
    parse_release_date,
    parse_series_order,
)
from audiobookdl.utils.enrichment_cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    EnrichmentCache,
)
from audiobookdl.utils.enrichment.matching import score_candidate_match
from audiobookdl.utils.enrichment.models import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    EnrichmentCandidate,
    _clean_asin,
    _clean_isbn,
    _extract_names,
    _raise_if_cancelled,
)
from audiobookdl.utils.enrichment.providers.registry import _cached_get_json


def _parse_audnexus_book(payload: Mapping[str, Any]) -> EnrichmentCandidate | None:
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(payload.get("asin"))
    authors = _extract_names(payload.get("authors"))
    narrators = _extract_names(payload.get("narrators"))

    genres: list[str] = []
    raw_genres = payload.get("genres")
    if isinstance(raw_genres, list):
        for g in raw_genres:
            if isinstance(g, dict):
                g_name = str(g.get("name") or "").strip()
            else:
                g_name = str(g).strip()
            if g_name and g_name not in genres:
                genres.append(g_name)

    series_name: str | None = None
    series_order: int | None = None
    series_primary = payload.get("seriesPrimary")
    if isinstance(series_primary, dict):
        raw_series = str(series_primary.get("name") or "").strip()
        series_name = raw_series or None
        series_order = parse_series_order(series_primary.get("position"))

    description = clean_html_description(
        payload.get("summary") or payload.get("description"),
        preserve_newlines=True,
    )
    cover_url = str(payload.get("image") or "").strip() or None
    publisher = str(payload.get("publisherName") or "").strip() or None
    release_date = parse_release_date(payload.get("releaseDate"))
    language = parse_language(payload.get("language"))
    isbn = _clean_isbn(payload.get("isbn"))

    return EnrichmentCandidate(
        provider="audnexus",
        score=0.0,
        title=title,
        authors=authors,
        narrators=narrators,
        series=series_name,
        series_order=series_order,
        genres=genres[:5],
        language=language,
        description=description,
        isbn=isbn,
        asin=asin,
        publisher=publisher,
        release_date=release_date,
        cover_url=cover_url,
        raw_id=asin,
    )


def _parse_audible_product(product: Mapping[str, Any]) -> EnrichmentCandidate | None:
    title = str(product.get("title") or "").strip()
    if not title:
        return None
    asin = _clean_asin(product.get("asin"))
    authors = _extract_names(product.get("authors"))
    narrators = _extract_names(product.get("narrators"))

    series_name: str | None = None
    series_order: int | None = None
    series_list = product.get("series")
    if isinstance(series_list, list) and series_list and isinstance(series_list[0], dict):
        series_name = str(series_list[0].get("title") or "").strip() or None
        series_order = parse_series_order(series_list[0].get("sequence"))

    cover_url: str | None = None
    images = product.get("product_images")
    if isinstance(images, dict) and images:
        for size_key in ("1024", "900", "500"):
            if images.get(size_key):
                cover_url = str(images[size_key]).strip()
                break
        if cover_url is None:
            first_val = next(iter(images.values()))
            if first_val:
                cover_url = str(first_val).strip()

    description = clean_html_description(
        product.get("publisher_summary") or product.get("merchandising_summary"),
        preserve_newlines=True,
    )
    publisher = str(product.get("publisher_name") or "").strip() or None
    release_date = parse_release_date(product.get("release_date"))
    language = parse_language(product.get("language"))

    return EnrichmentCandidate(
        provider="audnexus",
        score=0.0,
        title=title,
        authors=authors,
        narrators=narrators,
        series=series_name,
        series_order=series_order,
        language=language,
        description=description,
        asin=asin,
        publisher=publisher,
        release_date=release_date,
        cover_url=cover_url,
        raw_id=asin,
    )


def _fetch_audnexus_chapters(
    session: requests.Session,
    asin: str,
    *,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    cancel_event: threading.Event | None = None,
) -> list[Chapter]:
    payload = _cached_get_json(
        session,
        "audnexus",
        f"https://api.audnex.us/books/{asin}/chapters",
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
        cancel_event=cancel_event,
    )
    if not isinstance(payload, dict):
        return []
    raw_chapters = payload.get("chapters")
    if not isinstance(raw_chapters, list):
        return []
    chapters: list[Chapter] = []
    for item in raw_chapters:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        start_ms = item.get("startOffsetMs")
        if start_ms is None and item.get("startOffsetSec") is not None:
            try:
                start_ms = int(float(item["startOffsetSec"]) * 1000)
            except (TypeError, ValueError):
                start_ms = None
        if title and start_ms is not None:
            try:
                chapters.append(Chapter(start=max(0, int(start_ms)), title=title))
            except (TypeError, ValueError):
                continue
    return chapters


def _query_audnexus(
    metadata: AudiobookMetadata,
    *,
    session: requests.Session,
    proxy: str | None = None,
    timeout: float = 10.0,
    cache: EnrichmentCache | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    cancel_event: threading.Event | None = None,
) -> list[EnrichmentCandidate]:
    _raise_if_cancelled(cancel_event)
    direct_asin = _clean_asin(metadata.asin)
    if direct_asin:
        book_payload = _cached_get_json(
            session,
            "audnexus",
            f"https://api.audnex.us/books/{direct_asin}",
            proxy=proxy,
            timeout=timeout,
            cache=cache,
            use_cache=use_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            cancel_event=cancel_event,
        )
        if isinstance(book_payload, dict):
            cand = _parse_audnexus_book(book_payload)
            if cand is not None:
                cand.score = score_candidate_match(metadata, cand)
                return [cand]
        return []

    if not metadata.title:
        return []

    search_params: dict[str, Any] = {
        "title": metadata.title,
        "num_results": 5,
        "products_sort_by": "Relevance",
        "response_groups": "contributors,media,product_attrs,product_desc,series",
    }
    if metadata.authors:
        search_params["author"] = metadata.authors[0]

    catalog_payload = _cached_get_json(
        session,
        "audnexus",
        "https://api.audible.com/1.0/catalog/products",
        params=search_params,
        proxy=proxy,
        timeout=timeout,
        cache=cache,
        use_cache=use_cache,
        cache_ttl_seconds=cache_ttl_seconds,
        cancel_event=cancel_event,
    )
    if not isinstance(catalog_payload, dict):
        return []
    products = catalog_payload.get("products")
    if not isinstance(products, list):
        return []

    preliminary: list[EnrichmentCandidate] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        cand = _parse_audible_product(product)
        if cand is not None:
            cand.score = score_candidate_match(metadata, cand)
            preliminary.append(cand)

    preliminary.sort(key=lambda c: c.score, reverse=True)
    enriched: list[EnrichmentCandidate] = []
    for idx, cand in enumerate(preliminary):
        _raise_if_cancelled(cancel_event)
        if cand.asin and (idx < 3 and cand.score >= min_confidence * 0.7):
            book_payload = _cached_get_json(
                session,
                "audnexus",
                f"https://api.audnex.us/books/{cand.asin}",
                proxy=proxy,
                timeout=timeout,
                cache=cache,
                use_cache=use_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                cancel_event=cancel_event,
            )
            if isinstance(book_payload, dict):
                full_cand = _parse_audnexus_book(book_payload)
                if full_cand is not None:
                    full_cand.score = score_candidate_match(metadata, full_cand)
                    enriched.append(full_cand)
                    continue
        enriched.append(cand)
    enriched.sort(key=lambda c: c.score, reverse=True)
    return enriched

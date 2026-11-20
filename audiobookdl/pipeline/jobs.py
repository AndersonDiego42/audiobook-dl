import os
import threading
from typing import Any
from collections.abc import Sequence

import requests

from .. import logging, output
from ..config import (
    Config,
    delete_source_oauth_token,
    load_config,
    normalize_profile_name,
    save_source_oauth_token,
)
from ..utils.fs import atomic_write_bytes
from ..options_schema import (
    DownloadOptions,
)
from ..exceptions import (
    AudiobookDLException,
    DownloadCancelled,
    GenericAudiobookDLException,
    NoSourceFound,
    UserNotAuthorized,
)
from ..output.download import download
from ..sources import find_compatible_source
from ..sources.source import Source, bind_pooled_session
from ..utils.http import create_configured_http_session, resolve_http_timeout
from ..utils.archive import DownloadArchive, default_archive_path, make_book_key
from ..utils.audiobook import (
    Audiobook,
    BookId,
    CENCDecryption,
    Result,
    Series,
    parse_decryption_key_spec,
    reconcile_cenc_decryption,
    resolve_audiobook_output_format,
)
from ..utils.pssh import extract_cenc_kids_safe
from ..utils.enrichment import (
    EnrichmentCandidate,
    EnrichmentResult,
    enrich_audiobook_metadata,
    resolve_provider_list,
    resolve_redirect_url,
)


from audiobookdl.pipeline.common import (
    UNAVAILABLE_BOOK_EXCEPTIONS as UNAVAILABLE_BOOK_EXCEPTIONS,
    _describe_unavailable_reason as _describe_unavailable_reason,
    DownloadSummary as DownloadSummary,
    DEFAULT_POST_EXEC_TIMEOUT as DEFAULT_POST_EXEC_TIMEOUT,
    run_post_exec_hook as run_post_exec_hook,
    DownloadEvent as DownloadEvent,
    _EventLogSink as _EventLogSink,
    resolve_runtime_options as resolve_runtime_options,
    apply_runtime_options as apply_runtime_options,
    _pause_between_books as _pause_between_books,
    session_pool_scope as session_pool_scope,
    close_session_pool as close_session_pool,
    _close_downloader_sessions as _close_downloader_sessions,
    _resolve_pool_key as _resolve_pool_key,
    SessionPool as SessionPool,
    SessionPoolKey as SessionPoolKey,
    _ACTIVE_SESSION_POOL as _ACTIVE_SESSION_POOL,
)

from audiobookdl.pipeline.auth import (
    _close_source as _close_source,
    _resolve_request_profile as _resolve_request_profile,
    _resolve_vault_config_location as _resolve_vault_config_location,
    authenticate as authenticate,
)

def prepare_source_result(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    session: requests.Session | None = None,
    session_pool: SessionPool | None = None,
) -> tuple[Source, Result]:
    """
    Resolve source for `url`, authenticate if required, and fetch `Result`
    without downloading audio files.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"
    logging.log("Finding compatible source")
    try:
        source_class = find_compatible_source(url)
    except NoSourceFound:
        resolved_url = resolve_redirect_url(
            url,
            session=session,
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
            cancel_event=cancel_event,
        )
        if resolved_url == url:
            raise
        logging.debug(f"Resolved short URL '{url}' -> '{resolved_url}'")
        url = resolved_url
        source_class = find_compatible_source(url)
    active_pool = session_pool if session_pool is not None else _ACTIVE_SESSION_POOL.get()
    pool_key = _resolve_pool_key(source_class, options, config) if active_pool is not None else None
    pooled_session = active_pool.get(pool_key) if (active_pool is not None and pool_key is not None) else None
    with bind_pooled_session(pooled_session):
        source = source_class(options)
    try:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        if source.requires_authentication and not source.authenticated:
            authenticate(url, source, options, config, interaction=handler)
        if (
            active_pool is not None
            and pool_key is not None
            and pooled_session is None
            and hasattr(source, "_session")
            and (not source.requires_authentication or source.authenticated)
        ):
            active_pool[pool_key] = source._session
            release_fn = getattr(source, "release_session_ownership", None)
            if callable(release_fn):
                release_fn()
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        logging.debug(f"Downloading result of {url}")
        try:
            result = source.download(url)
        except UserNotAuthorized:
            loaded_cookies = bool(getattr(source, "_loaded_cached_cookies", False))
            loaded_oauth = bool(getattr(source, "_loaded_cached_oauth", False))
            if not (loaded_oauth or (loaded_cookies and getattr(source, "supports_login", False))):
                raise

            vault_loc = _resolve_vault_config_location(options, config, source)
            req_profile = _resolve_request_profile(options, config, source)

            if loaded_oauth:
                oauth_tok = getattr(source, "oauth_token", None)
                refresh_fn = getattr(source, "refresh_oauth_token_if_needed", None)
                if oauth_tok is not None and getattr(oauth_tok, "can_refresh", False) and callable(refresh_fn):
                    try:
                        refreshed = refresh_fn(force=True)
                    except UserNotAuthorized:
                        refreshed = None
                    if refreshed is not None:
                        if vault_loc is not None or getattr(source, "supports_oauth", False):
                            save_source_oauth_token(
                                source.name,
                                refreshed,
                                profile=req_profile,
                                overwrite=vault_loc,
                            )
                        return source, source.download(url)
                if vault_loc is not None or getattr(source, "supports_oauth", False):
                    delete_source_oauth_token(source.name, profile=req_profile, overwrite=vault_loc)

            if not (getattr(source, "supports_login", False) or getattr(source, "supports_oauth", False)):
                raise

            logging.debug(
                f"Cached session credentials for {source.name} were rejected; clearing stale cache and re-authenticating"
            )
            cached_path_fn = getattr(source, "cached_cookie_path", None)
            if loaded_cookies and callable(cached_path_fn):
                try:
                    os.remove(cached_path_fn())
                except OSError:
                    pass
            reset_fn = getattr(source, "reset_authentication", None)
            if callable(reset_fn):
                reset_fn(clear_cookies=True)
            else:
                source._loaded_cached_cookies = False
                source._loaded_cached_oauth = False
            authenticate(
                url,
                source,
                options,
                config,
                interaction=handler,
                use_cached_cookies=False,
                use_cached_oauth=False,
            )
            if (
                active_pool is not None
                and pool_key is not None
                and hasattr(source, "_session")
                and (not source.requires_authentication or source.authenticated)
            ):
                active_pool[pool_key] = source._session
                release_fn = getattr(source, "release_session_ownership", None)
                if callable(release_fn):
                    release_fn()
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled from None
            result = source.download(url)
        return source, result
    except BaseException:
        _close_source(source)
        raise


def fetch_metadata(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
) -> Result:
    """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
    source, result = prepare_source_result(
        url,
        options,
        config,
        interaction=interaction,
        cancel_event=cancel_event,
    )
    _close_source(source)
    return result


def _audiobook_output_entry(audiobook: Audiobook, options: DownloadOptions) -> dict[str, Any]:
    location = output.gen_output_location(
        options.output_template,
        audiobook.metadata,
        options.remove_chars,
    )
    fmt = resolve_audiobook_output_format(audiobook, options.output_format)
    candidate_file = f"{location}.{fmt}" if fmt else ""
    resolved_path = candidate_file if (candidate_file and os.path.isfile(candidate_file)) else location
    return {
        "title": audiobook.title,
        "authors": list(audiobook.metadata.authors),
        "source": audiobook.metadata.source,
        "format": fmt,
        "output_path": resolved_path,
        "parts": len(audiobook.files),
        "chapters": len(audiobook.chapters),
    }


def download_result(
    source: Source,
    result: Result,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    selected_indices: Sequence[int] | None = None,
    summary: DownloadSummary | None = None,
    config: Config | None = None,
) -> None:
    """Download a resolved `Audiobook` or `Series` `Result`."""
    handler = interaction or logging.NonInteractiveInteractionHandler()

    logging.log("")  # Empty line
    if isinstance(result, Audiobook):
        logging.emit_item_progress(1, 1, result.title)
        logging.log(f"Downloading {result.title} from {source.name}")
        processed = process_audiobook(
            source,
            result,
            options,
            interaction=handler,
            cancel_event=cancel_event,
            config=config,
        )
        if summary is not None:
            if processed is False:
                summary.books_skipped += 1
            else:
                summary.books_completed += 1
                summary.outputs.append(_audiobook_output_entry(result, options))
    elif isinstance(result, Series):
        if selected_indices is None:
            books_to_process: list[BookId[Any] | Audiobook] = list(result.books)
        else:
            seen_idx = set()
            books_to_process = []
            for idx in selected_indices:
                if 0 <= idx < len(result.books) and idx not in seen_idx:
                    seen_idx.add(idx)
                    books_to_process.append(result.books[idx])
        count = len(books_to_process)
        if count == 0:
            logging.log(f"No books selected from {result.title}")
            return
        logging.emit_item_progress(0, count, result.title)
        logging.log(
            f"Downloading {count} books in {result.title} from {source.name}"
        )
        done_count = 0
        skip_count = 0
        fail_count = 0
        errors: list[tuple[str, str]] = []
        for index, book in enumerate(books_to_process, start=1):
            logging.wait_if_paused(cancel_event=cancel_event)
            if index > 1 and getattr(options, "book_delay", 0.0) > 0:
                _pause_between_books(options.book_delay, cancel_event)
            logging.emit_item_progress(index, count, getattr(book, "title", None) or result.title)
            try:
                resolved = book if isinstance(book, Audiobook) else source.download_from_id(book.id)
                audiobooks = _flatten_resolved_books(source, resolved)
                for audiobook in audiobooks:
                    logging.emit_item_progress(index, count, audiobook.title)
                    processed = process_audiobook(
                        source,
                        audiobook,
                        options,
                        interaction=handler,
                        cancel_event=cancel_event,
                        config=config,
                    )
                    if processed is False:
                        skip_count += 1
                    else:
                        done_count += 1
                        if summary is not None:
                            summary.outputs.append(_audiobook_output_entry(audiobook, options))
            except UNAVAILABLE_BOOK_EXCEPTIONS as e:
                skip_count += 1
                logging.log(f"Skipped {book} ({_describe_unavailable_reason(e)})")
                continue
            except AudiobookDLException as e:
                if isinstance(e, DownloadCancelled):
                    raise
                # Don't let a single broken book take down a 200+ book run.
                # Surface the underlying error and move on.
                fail_count += 1
                detail = str(e).strip() or e.error_description
                errors.append((str(book), detail))
                logging.log(f"Skipped {book} ({detail})")
                if logging.is_debug_mode():
                    logging.log_traceback()
                continue
            except Exception as e:
                # Same isolation for unexpected per-book errors (ValueError,
                # KeyError, LoadError, ...) — DownloadCancelled still propagates
                # via the AudiobookDLException handler above.
                fail_count += 1
                detail = str(e).strip() or repr(e)
                errors.append((str(book), detail))
                logging.log(f"Skipped {book} ({detail})")
                if logging.is_debug_mode():
                    logging.log_traceback()
                continue
        if summary is not None:
            summary.books_completed += done_count
            summary.books_skipped += skip_count
            summary.books_failed += fail_count
            summary.book_errors.extend(errors)
        if fail_count > 0 or skip_count > 0:
            logging.log(
                f"Series summary: {done_count} completed, {skip_count} skipped, {fail_count} failed"
            )


def process_url(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    selected_indices: Sequence[int] | None = None,
    prefetched: tuple[Source, Result] | None = None,
    summary: DownloadSummary | None = None,
    session: requests.Session | None = None,
    session_pool: SessionPool | None = None,
) -> None:
    """
    Process `url` based on `options` and `config`.
    Will by default download the audiobook the URL is pointing to.
    """
    owns_source = prefetched is None
    if prefetched is not None:
        source, result = prefetched
    else:
        source, result = prepare_source_result(
            url,
            options,
            config,
            interaction=interaction,
            cancel_event=cancel_event,
            session=session,
            session_pool=session_pool,
        )
    try:
        download_result(
            source,
            result,
            options,
            interaction=interaction,
            cancel_event=cancel_event,
            selected_indices=selected_indices,
            summary=summary,
            config=config,
        )
    finally:
        if owns_source:
            _close_source(source)



def _flatten_resolved_books(source: Source, resolved: Result) -> list[Audiobook]:
    """Expand a `Result` (`Audiobook` or `Series`) into concrete `Audiobook` items."""
    if isinstance(resolved, Audiobook):
        return [resolved]
    items: list[Audiobook] = []
    for entry in resolved.books:
        if isinstance(entry, Audiobook):
            items.append(entry)
        else:
            nested = source.download_from_id(entry.id)
            items.extend(_flatten_resolved_books(source, nested))
    return items


def _format_candidate_choice(candidate: EnrichmentCandidate) -> str:
    """Format an `EnrichmentCandidate` as a concise single-line selection option."""
    pct = round(candidate.score * 100)
    parts = [f"[{candidate.provider} {pct}%] {candidate.title}"]
    if candidate.authors:
        parts.append(f"by {', '.join(candidate.authors)}")
    if candidate.narrators:
        parts.append(f"(narr. {', '.join(candidate.narrators)})")
    if candidate.release_date:
        parts.append(f"[{candidate.release_date.year}]")
    extras = []
    if candidate.asin:
        extras.append(f"ASIN:{candidate.asin}")
    if candidate.isbn:
        extras.append(f"ISBN:{candidate.isbn}")
    if candidate.cover_url:
        extras.append("cover")
    if extras:
        parts.append("{" + ", ".join(extras) + "}")
    return " ".join(parts)


def enrich_audiobook_with_options(
    audiobook: Audiobook,
    options: DownloadOptions,
    *,
    source: Source | None = None,
    interaction: logging.InteractionHandler | None = None,
    config: Config | None = None,
    cancel_event: threading.Event | None = None,
) -> EnrichmentResult | None:
    """Run multi-provider metadata enrichment for `audiobook` according to `options`."""
    resolved_cfg = config
    if resolved_cfg is None and getattr(options, "config_location", None):
        try:
            resolved_cfg = load_config(options.config_location)
        except Exception:
            resolved_cfg = None
    custom_providers = (
        resolved_cfg.custom_metadata_providers if resolved_cfg is not None else None
    )
    raw_providers = getattr(options, "enrich_providers", None)
    try:
        providers = resolve_provider_list(raw_providers, custom_providers=custom_providers)
    except ValueError as exc:
        raise GenericAudiobookDLException(str(exc)) from exc

    interactive = bool(getattr(options, "enrich_interactive", False))
    selector = None
    if interactive:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        choose_fn = getattr(handler, "choose_index", None)
        if choose_fn is not None:
            def _select(provider_name: str, candidates: Sequence[EnrichmentCandidate]) -> int | None:
                labels = [_format_candidate_choice(c) for c in candidates]
                return choose_fn(
                    f"Select {provider_name} match for '{audiobook.title}'",
                    labels,
                    default=0 if candidates[0].score >= 0.65 else None,
                )
            selector = _select

    resolved_proxy = getattr(options, "proxy", None) or (
        resolved_cfg.proxy if resolved_cfg is not None else None
    )
    with create_configured_http_session(options, proxy=resolved_proxy) as enrich_session:
        result = enrich_audiobook_metadata(
            audiobook,
            providers=providers,
            custom_providers=custom_providers,
            session=enrich_session,
            proxy=resolved_proxy,
            timeout=getattr(options, "timeout", None) or 10.0,
            database_directory=getattr(options, "database_directory", None),
            use_cache=True,
            interactive=interactive,
            selector=selector,
            include_chapters=not bool(getattr(options, "no_chapters", False)),
            override_chapters=bool(getattr(options, "override_chapters", False)),
            cancel_event=cancel_event,
        )
    if result.updated:
        details = list(result.fields_updated)
        if result.cover_updated:
            details.append("cover")
        if result.chapters_updated:
            details.append(f"chapters({len(audiobook.chapters)})")
        logging.book_update(
            f"Enriched metadata ({', '.join(result.providers_used)}): {', '.join(details)}"
        )
    return result


_CENC_CONTAINER_EXTENSIONS = frozenset({"mp4", "m4a", "m4b", "m4s", "m4v", "mov"})


def _apply_user_decryption_key(audiobook: Audiobook, options: DownloadOptions) -> None:
    """Apply user-supplied `options.decryption_key` to CENC files as fallback/override."""
    raw_key = getattr(options, "decryption_key", None)
    if not isinstance(raw_key, str) or not raw_key.strip():
        return
    default_key, key_map = parse_decryption_key_spec(raw_key)
    for file in audiobook.files:
        if isinstance(file.encryption_method, CENCDecryption):
            if default_key is not None:
                file.encryption_method.key = default_key
            if key_map:
                file.encryption_method.key_map.update(key_map)
        elif (
            file.encryption_method is None
            and file.encryption_resolver is None
            and file.ext.lstrip(".").lower() in _CENC_CONTAINER_EXTENSIONS
        ):
            file.encryption_method = CENCDecryption(key=default_key, key_map=dict(key_map))
        if isinstance(file.encryption_method, CENCDecryption) and file.init_section:
            known_kids = extract_cenc_kids_safe(file.init_section)
            reconcile_cenc_decryption(file.encryption_method, known_kids)


def process_audiobook(
    source: Source,
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    config: Config | None = None,
) -> bool:
    """Operate on `audiobook` based on `options`. Return False if skipped via archive."""
    if not audiobook.metadata.source and source is not None:
        audiobook.metadata.source = source.name
    if options.cover:
        download_cover(audiobook, options)
        return True

    source_name = (
        source.name
        if source is not None
        else (audiobook.metadata.source or "unknown")
    )
    book_key = make_book_key(source_name, audiobook)
    explicit_fields: frozenset[str] = getattr(options, "_explicit_fields", frozenset())
    explicit_archive = getattr(options, "download_archive", None)
    use_archive = not ("download_archive" in explicit_fields and not explicit_archive)
    ledger_path = (
        explicit_archive
        or (
            default_archive_path(options.database_directory)
            if use_archive and getattr(options, "database_directory", None)
            else None
        )
    )
    archive: DownloadArchive | None = None
    check_archive = bool(use_archive and (explicit_archive or (options.skip_downloaded and ledger_path)))
    try:
        if check_archive and ledger_path:
            archive = DownloadArchive(ledger_path, persistent=True)
            if archive.contains(source_name, book_key):
                logging.log(
                    f"Skipping {audiobook.title}, already recorded in download archive."
                )
                return False
            dup = archive.find_cross_source_duplicate(
                source_name,
                book_key,
                title=audiobook.title,
                author=audiobook.metadata.author,
                isbn=audiobook.metadata.isbn,
                asin=audiobook.metadata.asin,
            )
            if dup is not None:
                dup_label = dup.get("output_path") or dup.get("title") or dup.get("source")
                if options.skip_downloaded:
                    logging.log(
                        f"Skipping {audiobook.title}, already archived from {dup['source']} ({dup_label})."
                    )
                    return False
                logging.warning(
                    f"Warning: {audiobook.title} matches existing archive entry from {dup['source']} ({dup_label})."
                )

        enrich_result = None
        if getattr(options, "enrich_metadata", False):
            enrich_result = enrich_audiobook_with_options(
                audiobook,
                options,
                source=source,
                interaction=interaction,
                config=config,
                cancel_event=cancel_event,
            )

        logging.wait_if_paused(cancel_event=cancel_event)
        _apply_user_decryption_key(audiobook, options)
        if enrich_result is not None and enrich_result.chapters_updated:
            outcome = download(
                audiobook,
                options,
                interaction=interaction,
                cancel_event=cancel_event,
                chapters_already_overridden=True,
            )
        else:
            outcome = download(audiobook, options, interaction=interaction, cancel_event=cancel_event)
        if outcome is not None and getattr(outcome, "status", "downloaded") == "skipped":
            return False
        if source is not None:
            source.on_download_complete(audiobook)

        location = output.gen_output_location(
            options.output_template,
            audiobook.metadata,
            options.remove_chars,
        )
        fmt = resolve_audiobook_output_format(audiobook, options.output_format)
        candidate_file = f"{location}.{fmt}" if fmt else ""
        resolved_output = candidate_file if (candidate_file and os.path.isfile(candidate_file)) else location

        if ledger_path:
            if archive is None:
                archive = DownloadArchive(ledger_path, persistent=True)
            active_profile = normalize_profile_name(getattr(options, "profile", None))
            archive.record(
                source_name,
                book_key,
                title=audiobook.title,
                author=audiobook.metadata.author,
                url=audiobook.metadata.scrape_url,
                output_path=resolved_output,
                audio_format=fmt,
                profile=active_profile,
                status="downloaded",
                series=audiobook.metadata.series,
                series_order=audiobook.metadata.series_order,
                narrator=audiobook.metadata.narrator,
                isbn=audiobook.metadata.isbn,
                asin=audiobook.metadata.asin,
            )
    finally:
        if archive is not None:
            archive.close()

    force_abs = bool(getattr(options, "upload_to_abs", False))
    abs_cfg = config.audiobookshelf if config is not None else None
    if abs_cfg is None and force_abs:
        abs_cfg = load_config(getattr(options, "config_location", None)).audiobookshelf
    if abs_cfg is not None:
        try:
            from audiobookdl.integrations.audiobookshelf import sync_book_to_audiobookshelf

            resolved_proxy = getattr(options, "proxy", None) or (
                config.proxy if config is not None else None
            )
            resolved_timeout = resolve_http_timeout(options)
            if sync_book_to_audiobookshelf(
                resolved_output,
                audiobook.metadata,
                abs_cfg,
                force_upload=force_abs,
                proxy=resolved_proxy,
                timeout=resolved_timeout,
                cancel_event=cancel_event,
            ):
                logging.log(f"Synced {audiobook.title} to Audiobookshelf")
        except DownloadCancelled:
            raise
        except Exception as exc:
            logging.error(f"Audiobookshelf sync failed for '{audiobook.title}': {exc}")
    run_post_exec_hook(
        getattr(options, "post_exec", None),
        output_path=resolved_output,
        metadata=audiobook.metadata,
        audio_format=fmt,
        cancel_event=cancel_event,
    )
    if source is not None:
        clear_fn = getattr(source, "clear_page_cache", None)
        if callable(clear_fn):
            clear_fn()
    return True


def download_cover(audiobook: Audiobook, options: DownloadOptions) -> None:
    """Download audiobook cover to the resolved output location."""
    cover = audiobook.cover
    if not cover:
        return
    location = output.gen_output_location(
        options.output_template,
        audiobook.metadata,
        options.remove_chars,
    )
    ext = cover.extension.lstrip(".")
    if not location:
        cover_path = f"cover.{ext}"
    elif location.endswith(("/", "\\")) or os.path.isdir(location):
        cover_path = os.path.join(location, f"cover.{ext}")
    else:
        cover_path = f"{location}.{ext}"
    parent = os.path.dirname(cover_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    atomic_write_bytes(cover_path, cover.image)

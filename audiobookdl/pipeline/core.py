import sys
import threading
import weakref
from typing import Any
from collections.abc import Sequence


from .. import __version__, logging
from ..config import (
    Config,
    load_config,
)
from ..options_schema import (
    DownloadOptions,
    build_default_options,
)
from ..exceptions import (
    AudiobookDLException,
    DownloadCancelled,
)
from ..sources.source import Source
from ..utils.http import create_configured_http_session
from ..utils.audiobook import (
    Result,
)


from audiobookdl.pipeline.common import (
    UNAVAILABLE_BOOK_EXCEPTIONS as UNAVAILABLE_BOOK_EXCEPTIONS,
    _describe_unavailable_reason as _describe_unavailable_reason,
    DownloadSummary as DownloadSummary,
    DEFAULT_POST_EXEC_TIMEOUT as DEFAULT_POST_EXEC_TIMEOUT,
    run_post_exec_hook as run_post_exec_hook,
    DownloadEvent as DownloadEvent,
    EventCallback as EventCallback,
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
    get_cookie_path as get_cookie_path,
    get_or_ask as get_or_ask,
    login as login,
    _explicit_inline_credentials as _explicit_inline_credentials,
    _resolve_request_profile as _resolve_request_profile,
    _resolve_vault_config_location as _resolve_vault_config_location,
    _apply_source_config_overrides as _apply_source_config_overrides,
    authenticate as authenticate,
    authorize_service_oauth as authorize_service_oauth,
    disconnect_service_oauth as disconnect_service_oauth,
    _init_service_source as _init_service_source,
    _close_source as _close_source,
    verify_service_auth as verify_service_auth,
    search_service as search_service,
    list_service_library as list_service_library,
)

from audiobookdl.pipeline.jobs import (
    prepare_source_result as prepare_source_result,
    fetch_metadata as fetch_metadata,
    _audiobook_output_entry as _audiobook_output_entry,
    download_result as download_result,
    process_url as process_url,
    _flatten_resolved_books as _flatten_resolved_books,
    _format_candidate_choice as _format_candidate_choice,
    enrich_audiobook_with_options as enrich_audiobook_with_options,
    _CENC_CONTAINER_EXTENSIONS as _CENC_CONTAINER_EXTENSIONS,
    _apply_user_decryption_key as _apply_user_decryption_key,
    process_audiobook as process_audiobook,
    download_cover as download_cover,
)

class Downloader:
    """
    Programmatic client for authenticating, fetching metadata, and downloading audiobooks.

    All operations run inside an isolated `logging.JobContext`, leaving global
    `audiobookdl.logging` flags and sinks untouched.
    """

    def __init__(
        self,
        config: Config | None = None,
        options: DownloadOptions | None = None,
        *,
        on_event: EventCallback | None = None,
        sink: logging.LogSink | None = None,
        interaction: logging.InteractionHandler | None = None,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
    ) -> None:
        if sink is not None and on_event is not None:
            raise ValueError("Specify either 'sink' or 'on_event', not both")
        self.options: DownloadOptions = (
            options if options is not None else build_default_options()
        )
        self.config: Config = (
            config if config is not None else load_config(self.options.config_location)
        )
        self.interaction: logging.InteractionHandler = (
            interaction if interaction is not None else logging.NonInteractiveInteractionHandler()
        )
        self.cancel_event: threading.Event | None = cancel_event
        self.pause_event: threading.Event | None = pause_event
        if sink is not None:
            self.sink: logging.LogSink | None = sink
        elif on_event is not None:
            self.sink = _EventLogSink(on_event)
        else:
            self.sink = None
        proxy = getattr(self.options, "proxy", None) or getattr(self.config, "proxy", None)
        self._http_session = create_configured_http_session(self.options, proxy=proxy)
        self._owned_session_pool: SessionPool = {}
        self._http_session_finalizer = weakref.finalize(
            self,
            _close_downloader_sessions,
            self._http_session,
            self._owned_session_pool,
        )

    def _effective_session_pool(self) -> SessionPool:
        active = _ACTIVE_SESSION_POOL.get()
        return active if active is not None else self._owned_session_pool

    def close(self) -> None:
        """Close the shared HTTP session and any pooled source sessions held by this `Downloader`."""
        self._http_session_finalizer()

    def __enter__(self) -> "Downloader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _resolve_options(self) -> DownloadOptions:
        """Return `self.options` merged with config defaults (input is not mutated)."""
        return resolve_runtime_options(self.options, self.config)

    def _build_context(self, options: DownloadOptions | None = None) -> logging.JobContext:
        resolved = options if options is not None else self._resolve_options()
        ctx = apply_runtime_options(resolved, self.config)
        ctx.interaction = self.interaction
        ctx.cancel_event = self.cancel_event
        ctx.pause_event = self.pause_event
        ctx.sink = self.sink if self.sink is not None else logging.get_ui_sink()
        return ctx

    def prepare(self, url: str) -> tuple[Source, Result]:
        """Resolve source for `url`, authenticate if needed, and fetch `Result`."""
        options = self._resolve_options()
        with logging.job_context(self._build_context(options)):
            return prepare_source_result(
                url,
                options,
                self.config,
                interaction=self.interaction,
                cancel_event=self.cancel_event,
                session=self._http_session,
                session_pool=self._effective_session_pool(),
            )

    def fetch_metadata(self, url: str) -> Result:
        """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
        source, result = self.prepare(url)
        close_fn = getattr(source, "close", None)
        if callable(close_fn):
            close_fn()
        return result

    def download_result(
        self,
        source: Source,
        result: Result,
        *,
        selected_indices: Sequence[int] | None = None,
    ) -> None:
        """Download an already-resolved `Audiobook` or `Series` `Result`."""
        options = self._resolve_options()
        with logging.job_context(self._build_context(options)):
            download_result(
                source,
                result,
                options,
                interaction=self.interaction,
                cancel_event=self.cancel_event,
                selected_indices=selected_indices,
                config=self.config,
            )

    def download(
        self,
        urls: str | Sequence[str],
        *,
        selected_indices: Sequence[int] | None = None,
        prefetched: tuple[Source, Result] | None = None,
    ) -> DownloadSummary:
        """Download one URL or a sequence of URLs and return a `DownloadSummary`."""
        url_list: Sequence[str] = [urls] if isinstance(urls, str) else urls
        options = self._resolve_options()
        should_skip_unavailable = (
            bool(getattr(options, "skip_unavailable", False)) or len(url_list) > 1
        )
        ctx = self._build_context(options)
        summary = DownloadSummary()
        with logging.job_context(ctx):
            logging.debug(f"audiobook-dl {__version__}")
            logging.debug(f"python {sys.version}")
            for index, url in enumerate(url_list):
                try:
                    logging.wait_if_paused(
                        cancel_event=self.cancel_event,
                        pause_event=self.pause_event,
                    )
                except DownloadCancelled:
                    summary.cancelled = True
                    logging.log("Download cancelled")
                    break
                if index > 0 and options.book_delay > 0:
                    try:
                        _pause_between_books(options.book_delay, self.cancel_event)
                    except DownloadCancelled:
                        summary.cancelled = True
                        logging.log("Download cancelled")
                        break
                try:
                    extra_kwargs: dict[str, Any] = {}
                    if index == 0:
                        if selected_indices is not None:
                            extra_kwargs["selected_indices"] = selected_indices
                        if prefetched is not None:
                            extra_kwargs["prefetched"] = prefetched
                    before_failed = summary.books_failed
                    before_completed = summary.books_completed
                    before_skipped = summary.books_skipped
                    process_url(
                        url,
                        options,
                        self.config,
                        interaction=self.interaction,
                        cancel_event=self.cancel_event,
                        summary=summary,
                        session=self._http_session,
                        session_pool=self._effective_session_pool(),
                        **extra_kwargs,
                    )
                    if (
                        summary.books_failed > before_failed
                        and summary.books_completed == before_completed
                        and summary.books_skipped == before_skipped
                    ):
                        summary.failed += 1
                    else:
                        summary.completed += 1
                except (DownloadCancelled, KeyboardInterrupt):
                    summary.cancelled = True
                    logging.log("Download cancelled")
                    break
                except UNAVAILABLE_BOOK_EXCEPTIONS as e:
                    if should_skip_unavailable:
                        summary.books_skipped += 1
                        summary.completed += 1
                        reason = _describe_unavailable_reason(e)
                        logging.log(f"Skipped {url} ({reason})")
                        if logging.is_debug_mode():
                            logging.log_traceback()
                        continue
                    summary.failed += 1
                    summary.books_failed += 1
                    detail = str(e).strip() or e.error_description
                    summary.book_errors.append((url, detail))
                    logging.error(detail)
                    if logging.is_debug_mode():
                        logging.log_traceback()
                except AudiobookDLException as e:
                    summary.failed += 1
                    summary.books_failed += 1
                    detail = str(e).strip() or e.error_description
                    summary.book_errors.append((url, detail))
                    logging.error(detail)
                    if logging.is_debug_mode():
                        logging.log_traceback()
                except Exception as e:
                    # Don't let an unexpected per-book error (ValueError, KeyError,
                    # LoadError, ...) abort the rest of the batch.
                    summary.failed += 1
                    summary.books_failed += 1
                    detail = str(e).strip() or repr(e)
                    summary.book_errors.append((url, detail))
                    logging.error(detail)
                    if logging.is_debug_mode():
                        logging.log_traceback()
        return summary


def run_downloads(
    urls: Sequence[str],
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
    selected_indices: Sequence[int] | None = None,
    prefetched: tuple[Source, Result] | None = None,
) -> DownloadSummary:
    """Run audiobook downloads for `urls` with isolated per-job context."""
    with Downloader(
        config=config,
        options=options,
        sink=sink,
        interaction=interaction,
        cancel_event=cancel_event,
        pause_event=pause_event,
    ) as downloader:
        return downloader.download(
            urls,
            selected_indices=selected_indices,
            prefetched=prefetched,
        )



"""Auxiliary cancellable background services for `GuiController`."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
import os
from collections.abc import Sequence
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from audiobookdl import logging
from audiobookdl.config import DEFAULT_PROFILE
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.event_bus import GuiInteractionHandler, _PrefixedInteractionHandler
from audiobookdl.gui.events import (
    AbsUploadDoneEvent,
    AccountsVerificationDoneEvent,
    AccountsVerificationItemEvent,
    AuthVerifiedEvent,
    CatalogLoadedEvent,
    FfmpegInstallDoneEvent,
    OAuthConnectedEvent,
    OAuthDisconnectedEvent,
    PreviewDoneEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressStartEvent,
    SyncDoneEvent,
    SyncEnqueueReadyEvent,
    UpdateCheckedEvent,
)
from audiobookdl.options_schema import build_default_options
from audiobookdl.pack import discover_pack_groups, inspect_local_book, scope_overrides_for_batch
from audiobookdl.pipeline import (
    authorize_service_oauth,
    disconnect_service_oauth,
    list_service_library,
    prepare_source_result,
    search_service,
    verify_service_auth,
)
from audiobookdl.utils.archive import (
    DownloadArchive,
    default_archive_path,
    make_book_key,
    resolve_archive_path,
    resolve_database_directory,
)
from audiobookdl.utils.audiobook import Audiobook, Series
from audiobookdl.utils.enrichment import (
    apply_candidate_to_audiobook,
    resolve_provider_list,
    search_enrichment_candidates,
)
from audiobookdl.utils.enrichment_cache import EnrichmentCache, default_enrichment_cache_path
from audiobookdl.utils.http import resolve_http_timeout
from audiobookdl.utils.updater import check_for_updates, install_ffmpeg_via_winget

if TYPE_CHECKING:
    from audiobookdl.config import Config
    from audiobookdl.gui.config_store import GuiConfigStore
    from audiobookdl.gui.event_bus import GuiEventBus
    from audiobookdl.gui.http_pool import GuiHttpPool
    from audiobookdl.gui.task_runner import GuiTaskRunner
    from audiobookdl.options_schema import DownloadOptions, SyncOptions
    from audiobookdl.pack import PackBookSpec, PackOverrides
    from audiobookdl.utils.enrichment import EnrichmentCandidate


class GuiServiceTasks:
    """Owns auxiliary cancellable background operations (preview, auth, sync, ABS, etc.)."""

    def __init__(
        self,
        *,
        events: GuiEventBus,
        config: GuiConfigStore,
        http: GuiHttpPool,
        tasks: GuiTaskRunner,
    ) -> None:
        self._events = events
        self._config = config
        self._http = http
        self._tasks = tasks
        self._preview_lock = threading.Lock()
        self._preview_request_id = 0
        self.preview_cancel_event = threading.Event()
        self.preview_interaction: GuiInteractionHandler | None = None
        self.catalog_request_id = 0
        self._catalog_lock = threading.Lock()
        self.catalog_cancel_event: threading.Event | None = None
        self.catalog_interaction: GuiInteractionHandler | None = None
        self._sync_lock = threading.Lock()
        self.sync_cancel_event: threading.Event | None = None
        self.sync_interaction: GuiInteractionHandler | None = None
        self._sync_cancel_events: set[threading.Event] = set()
        self._sync_interactions: set[GuiInteractionHandler] = set()
        self._accounts_test_lock = threading.Lock()
        self._accounts_test_run_id = 0
        self._accounts_test_cancel_events: list[threading.Event] = []
        self._accounts_test_interactions: list[GuiInteractionHandler] = []
        self._abs_upload_lock = threading.Lock()
        self._active_abs_uploads = 0

    @property
    def preview_request_id(self) -> int:
        return self._preview_request_id

    @preview_request_id.setter
    def preview_request_id(self, value: int) -> None:
        self._preview_request_id = value

    # -- Cross-source duplicate lookup --------------------------------------

    def find_cross_source_duplicate(
        self,
        metadata: Any,
        *,
        source_name: str = "",
        archive_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an active archive row from another service matching `metadata`, if any."""
        cfg = self._config.snapshot()
        db_dir = resolve_database_directory(cfg)
        ledger = archive_path or default_archive_path(db_dir)
        if not ledger or not os.path.exists(ledger):
            return None
        book_key = (
            getattr(metadata, "scrape_url", None)
            or getattr(metadata, "title", None)
            or make_book_key(source_name, type("_B", (), {"metadata": metadata, "title": getattr(metadata, "title", "")})())
        )
        try:
            with DownloadArchive(ledger, persistent=True) as archive:
                return archive.find_cross_source_duplicate(
                    source_name,
                    str(book_key or ""),
                    title=str(getattr(metadata, "title", "") or ""),
                    author=str(getattr(metadata, "author", "") or ""),
                    isbn=getattr(metadata, "isbn", None),
                    asin=getattr(metadata, "asin", None),
                )
        except Exception:
            return None

    # -- Local pack inspection ----------------------------------------------

    def inspect_local_pack_preview(
        self,
        paths: Sequence[str],
        options: DownloadOptions,
        *,
        recursive: bool = False,
        output_dir: str | None = None,
        overrides: PackOverrides | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[PackBookSpec]:
        """Discover and inspect local audiobook specs cleanly without modifying files."""
        groups = discover_pack_groups(paths, recursive=recursive, cancel_event=cancel_event)
        total = len(groups)
        specs: list[PackBookSpec] = []
        probe_cache: dict[str, Any] = {}
        session = self._http.get_http_session()
        book_overrides = scope_overrides_for_batch(overrides, total)
        for source_root, raw_tracks in groups:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            specs.append(
                inspect_local_book(
                    source_root,
                    raw_tracks,
                    options,
                    output_dir=output_dir,
                    overrides=book_overrides,
                    probe_cache=probe_cache,
                    session=session,
                    cancel_event=cancel_event,
                )
            )
        return specs

    # -- Metadata preview ----------------------------------------------------

    def cancel_preview(self) -> None:
        """Cancel any active metadata preview and unblock pending interaction prompts."""
        with self._preview_lock:
            self._preview_request_id += 1
            self.preview_cancel_event.set()
            interaction = self.preview_interaction
        if interaction is not None:
            interaction.release_all()

    def start_preview(
        self,
        url: str,
        options: DownloadOptions,
        *,
        check_duplicate: bool = False,
    ) -> int:
        """Fetch metadata and resolved `Result` for `url` on a background thread."""
        with self._preview_lock:
            self._preview_request_id += 1
            request_id = self._preview_request_id
            self.preview_cancel_event.clear()
            interaction = GuiInteractionHandler(self._events.queue, self.preview_cancel_event)
            self.preview_interaction = interaction

        def _worker() -> None:
            try:
                config = self._config.snapshot()
                ffmpeg_dir = config.ffmpeg_dir.strip() or None if config.ffmpeg_dir else None
                ctx = logging.JobContext(
                    debug=options.debug,
                    ffmpeg_dir=ffmpeg_dir,
                    sink=self._events.sink,
                    interaction=interaction,
                    cancel_event=self.preview_cancel_event,
                )
                with logging.job_context(ctx):
                    source, result = prepare_source_result(
                        url,
                        options,
                        config,
                        interaction=interaction,
                        cancel_event=self.preview_cancel_event,
                        session=self._http.get_http_session(),
                    )
                with self._preview_lock:
                    stale = self.preview_cancel_event.is_set() or request_id != self._preview_request_id
                if stale:
                    close_fn = getattr(source, "close", None)
                    if callable(close_fn):
                        close_fn()
                    raise DownloadCancelled()

                duplicate_warning: str | None = None
                existing_series_indices: frozenset[int] | None = None
                if isinstance(result, Audiobook) and check_duplicate:
                    archive_path = resolve_archive_path(config)
                    dup = self.find_cross_source_duplicate(
                        result.metadata,
                        source_name=source.name,
                        archive_path=archive_path,
                    )
                    if dup is not None:
                        dup_where = dup.get("output_path") or dup.get("title") or ""
                        duplicate_warning = (
                            f"Duplicate Alert: Already in library from {dup['source']}"
                            + (f" ({dup_where})" if dup_where else "")
                        )
                elif isinstance(result, Series):
                    from audiobookdl.gui.coordinators import series_book_exists

                    dir_stems_cache: dict[str, set[str]] = {}
                    archive_titles_cache: set[str] | None = None
                    if options.download_archive and os.path.exists(options.download_archive):
                        try:
                            with DownloadArchive(options.download_archive, persistent=True) as archive:
                                archive_titles_cache = {
                                    e.title.strip().casefold()
                                    for e in archive.list_entries(limit=5000, include_hidden=True)
                                    if e.status == "downloaded" and e.title and e.title.strip()
                                }
                        except sqlite3.Error:
                            archive_titles_cache = None
                    existing_series_indices = frozenset(
                        idx
                        for idx, book in enumerate(result.books)
                        if series_book_exists(
                            book,
                            options=options,
                            dir_stems_cache=dir_stems_cache,
                            archive_titles_cache=archive_titles_cache,
                            series_title=result.title,
                        )
                    )

                with self._preview_lock:
                    stale = self.preview_cancel_event.is_set() or request_id != self._preview_request_id
                if stale:
                    close_fn = getattr(source, "close", None)
                    if callable(close_fn):
                        close_fn()
                    raise DownloadCancelled()

                self._events.queue.put(
                    PreviewDoneEvent(
                        url=url,
                        prepared=(source, result),
                        error=None,
                        duplicate_checked=check_duplicate,
                        duplicate_warning=duplicate_warning,
                        existing_series_indices=existing_series_indices,
                    )
                )
            except Exception as exc:
                with self._preview_lock:
                    is_current = request_id == self._preview_request_id
                if is_current:
                    self._events.queue.put(PreviewDoneEvent(url=url, prepared=None, error=exc))

        self._tasks.submit_bg_task(_worker)
        return request_id

    # -- Auth / OAuth / accounts ---------------------------------------------

    def start_auth_verification(
        self,
        service_key: str,
        service_label: str,
        options: DownloadOptions,
    ) -> None:
        """Verify credentials for `service_key` on a background thread."""
        cfg = self._config.snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._events.queue, cancel_ev)
        self._tasks.track_interaction(interaction, cancel_ev)

        def _worker() -> None:
            try:
                ok = verify_service_auth(
                    service_key, options, cfg, interaction=interaction,
                    cancel_event=cancel_ev, sink=self._events.sink,
                )
                self._events.queue.put(
                    AuthVerifiedEvent(service_label=service_label, ok=ok, error=None)
                )
            except Exception as exc:
                self._events.queue.put(
                    AuthVerifiedEvent(service_label=service_label, ok=False, error=exc)
                )
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._tasks.untrack_interaction(interaction, cancel_ev)

        self._tasks.submit_bg_task(_worker)

    def start_oauth_connect(
        self,
        service_key: str,
        service_label: str,
        profile: str,
        options: DownloadOptions,
    ) -> threading.Event:
        """Run interactive OAuth 2.0 flow for `(service_key, profile)` in a background thread."""
        cfg = self._config.snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._events.queue, cancel_ev)
        self._tasks.track_interaction(interaction, cancel_ev)

        def _worker() -> None:
            try:
                authorize_service_oauth(
                    service_key,
                    options,
                    cfg,
                    profile=profile,
                    interaction=interaction,
                    cancel_event=cancel_ev,
                )
                self._events.queue.put(
                    OAuthConnectedEvent(
                        service_key=service_key,
                        service_label=service_label,
                        profile=profile,
                        ok=True,
                        error=None,
                    )
                )
            except Exception as exc:
                self._events.queue.put(
                    OAuthConnectedEvent(
                        service_key=service_key,
                        service_label=service_label,
                        profile=profile,
                        ok=False,
                        error=exc,
                    )
                )
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._tasks.untrack_interaction(interaction, cancel_ev)

        self._tasks.submit_bg_task(_worker)
        return cancel_ev

    def disconnect_oauth(
        self,
        service_key: str,
        options: DownloadOptions,
        *,
        profile: str | None = None,
    ) -> tuple[bool, Config]:
        """Revoke and delete stored OAuth tokens for `(service_key, profile)` and reload config."""
        cfg = self._config.snapshot()
        revoked = disconnect_service_oauth(service_key, options, cfg, profile=profile)
        return revoked, self._config.reload()

    def start_oauth_disconnect(
        self,
        service_key: str,
        service_label: str,
        profile: str,
        options: DownloadOptions,
    ) -> None:
        """Revoke OAuth tokens for `(service_key, profile)` on a background thread and emit `OAuthDisconnectedEvent`."""

        def _worker() -> None:
            try:
                revoked, _cfg = self.disconnect_oauth(service_key, options, profile=profile)
                self._events.queue.put(
                    OAuthDisconnectedEvent(
                        service_key=service_key,
                        service_label=service_label,
                        profile=profile,
                        revoked=revoked,
                        error=None,
                    )
                )
            except Exception as exc:
                self._events.queue.put(
                    OAuthDisconnectedEvent(
                        service_key=service_key,
                        service_label=service_label,
                        profile=profile,
                        revoked=False,
                        error=exc,
                    )
                )

        self._tasks.submit_bg_task(_worker)

    def start_accounts_verification(
        self,
        targets: Sequence[tuple[str, str, str]],
    ) -> int:
        """Verify multiple `(service_key, service_label, profile)` accounts with per-target cancel tokens."""
        cfg = self._config.snapshot()
        target_list = list(targets)
        per_target_cancel: dict[tuple[str, str], threading.Event] = {}
        interactions: list[GuiInteractionHandler] = []
        cancel_events: list[threading.Event] = []
        for svc_key, _svc_label, prof in target_list:
            ev = threading.Event()
            handler = GuiInteractionHandler(self._events.queue, ev)
            per_target_cancel[(svc_key, prof)] = ev
            cancel_events.append(ev)
            interactions.append(handler)
            self._tasks.track_interaction(handler, ev)

        with self._accounts_test_lock:
            self._accounts_test_run_id += 1
            run_id = self._accounts_test_run_id
            prev_cancels = list(self._accounts_test_cancel_events)
            prev_interactions = list(self._accounts_test_interactions)
            self._accounts_test_cancel_events = cancel_events
            self._accounts_test_interactions = interactions

        for ev in prev_cancels:
            ev.set()
        for handler in prev_interactions:
            handler.release_all()

        def _orchestrate() -> None:
            valid = 0
            checked = 0
            was_cancelled = False
            counter_lock = threading.Lock()
            handler_map = {
                (svc_key, prof): handler
                for (svc_key, _svc_label, prof), handler in zip(target_list, interactions, strict=True)
            }

            def _check_one(svc_key: str, svc_label: str, prof: str) -> None:
                nonlocal valid, checked, was_cancelled
                cancel_ev = per_target_cancel[(svc_key, prof)]
                if cancel_ev.is_set():
                    return
                handler = handler_map[(svc_key, prof)]
                prefix = f"{svc_label} ({prof})"
                wrapped_handler = _PrefixedInteractionHandler(handler, prefix)
                opts = build_default_options(
                    config_location=self._config.config_location,
                    profile=None if prof == DEFAULT_PROFILE else prof,
                )
                ok = False
                err: str | None = None
                try:
                    ok = verify_service_auth(
                        svc_key,
                        opts,
                        cfg,
                        interaction=wrapped_handler,
                        cancel_event=cancel_ev,
                        sink=self._events.sink,
                    )
                except DownloadCancelled:
                    with counter_lock:
                        was_cancelled = True
                    return
                except Exception as exc:
                    err = str(exc) or type(exc).__name__
                if cancel_ev.is_set():
                    with counter_lock:
                        was_cancelled = True
                    return
                with counter_lock:
                    checked += 1
                    if ok:
                        valid += 1
                self._events.queue.put(
                    AccountsVerificationItemEvent(
                        run_id=run_id,
                        service_key=svc_key,
                        profile=prof,
                        ok=ok,
                        error=err,
                    )
                )

            max_workers = max(1, min(4, len(target_list)))
            pool: ThreadPoolExecutor | None = None
            try:
                if max_workers <= 1:
                    for svc_key, svc_label, prof in target_list:
                        ev = per_target_cancel[(svc_key, prof)]
                        with self._accounts_test_lock:
                            superseded = self._accounts_test_run_id != run_id
                        if ev.is_set() or superseded:
                            was_cancelled = True
                            break
                        _check_one(svc_key, svc_label, prof)
                else:
                    pool = ThreadPoolExecutor(
                        max_workers=max_workers,
                        thread_name_prefix="abdl-acct-verify",
                    )
                    pending = {
                        pool.submit(_check_one, svc_key, svc_label, prof)
                        for svc_key, svc_label, prof in target_list
                    }
                    while pending:
                        with self._accounts_test_lock:
                            superseded = self._accounts_test_run_id != run_id
                        if superseded or all(ev.is_set() for ev in cancel_events):
                            was_cancelled = True
                            for fut in pending:
                                fut.cancel()
                            break
                        done_set, pending = wait_futures(
                            pending, timeout=0.05, return_when=FIRST_COMPLETED
                        )
                        for fut in done_set:
                            try:
                                fut.result()
                            except Exception:
                                pass
            finally:
                if pool is not None:
                    pool.shutdown(wait=not was_cancelled, cancel_futures=was_cancelled)
                for ev in cancel_events:
                    ev.set()
                for handler in interactions:
                    handler.release_all()
                for handler, ev in zip(interactions, cancel_events, strict=True):
                    self._tasks.untrack_interaction(handler, ev)
                with self._accounts_test_lock:
                    if self._accounts_test_run_id == run_id:
                        self._accounts_test_cancel_events = []
                        self._accounts_test_interactions = []
                self._events.queue.put(
                    AccountsVerificationDoneEvent(
                        run_id=run_id,
                        valid_count=valid,
                        checked_count=checked,
                        total_count=len(target_list),
                        cancelled=was_cancelled,
                    )
                )

        self._tasks.submit_bg_task(_orchestrate)
        return run_id

    def cancel_accounts_verification(self) -> None:
        """Cancel any active multi-account login verification sweep immediately."""
        with self._accounts_test_lock:
            cancel_events = list(self._accounts_test_cancel_events)
            interactions = list(self._accounts_test_interactions)
        for ev in cancel_events:
            ev.set()
        for handler in interactions:
            handler.release_all()

    # -- Catalog --------------------------------------------------------------

    def cancel_catalog_fetch(self) -> bool:
        """Cancel the currently in-flight catalog search/list request, if any."""
        with self._catalog_lock:
            cancel_ev = self.catalog_cancel_event
            interaction = self.catalog_interaction
            if cancel_ev is None and interaction is None:
                return False
            self.catalog_request_id += 1
            self.catalog_cancel_event = None
            self.catalog_interaction = None
        if cancel_ev is not None:
            cancel_ev.set()
        if interaction is not None:
            interaction.release_all()
        return True

    def start_catalog_fetch(
        self,
        mode: str,
        service_key: str,
        query_text: str,
        options: DownloadOptions,
        *,
        archive_path: str | None = None,
    ) -> int:
        """Search a service catalog or list its library on a background thread."""
        self.cancel_catalog_fetch()
        cfg = self._config.snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._events.queue, cancel_ev)
        with self._catalog_lock:
            self.catalog_request_id += 1
            request_id = self.catalog_request_id
            self.catalog_cancel_event = cancel_ev
            self.catalog_interaction = interaction
        self._tasks.track_interaction(interaction, cancel_ev)

        def _worker() -> None:
            try:
                if mode == "search":
                    books = search_service(
                        service_key,
                        query_text,
                        options,
                        cfg,
                        interaction=interaction,
                        cancel_event=cancel_ev,
                        sink=self._events.sink,
                    )
                else:
                    books = list_service_library(
                        service_key,
                        options,
                        cfg,
                        interaction=interaction,
                        cancel_event=cancel_ev,
                        sink=self._events.sink,
                    )
                if cancel_ev.is_set():
                    return
                statuses: list[str] | None = None
                if books and archive_path and os.path.exists(archive_path):
                    from audiobookdl.utils.archive import DownloadArchive

                    with DownloadArchive(archive_path, persistent=True) as archive:
                        statuses = archive.lookup_catalog_statuses(service_key, books)
                if not cancel_ev.is_set():
                    self._events.queue.put(
                        CatalogLoadedEvent(
                            request_id=request_id,
                            payload=(books, statuses),
                            error=None,
                        )
                    )
            except Exception as exc:
                if not cancel_ev.is_set():
                    self._events.queue.put(
                        CatalogLoadedEvent(
                            request_id=request_id,
                            payload=([], None),
                            error=exc,
                        )
                    )
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._tasks.untrack_interaction(interaction, cancel_ev)
                with self._catalog_lock:
                    if self.catalog_cancel_event is cancel_ev:
                        self.catalog_cancel_event = None
                    if self.catalog_interaction is interaction:
                        self.catalog_interaction = None

        self._tasks.submit_bg_task(_worker)
        return request_id

    # -- Library sync ---------------------------------------------------------

    def cancel_library_sync(self) -> None:
        """Signal any active library synchronization pass to cancel and unblock prompts."""
        with self._sync_lock:
            cancel_events = list(self._sync_cancel_events)
            interactions = list(self._sync_interactions)
            if self.sync_cancel_event is not None:
                cancel_events.append(self.sync_cancel_event)
            if self.sync_interaction is not None:
                interactions.append(self.sync_interaction)
        for ev in cancel_events:
            ev.set()
        for inter in interactions:
            inter.release_all()

    def start_library_sync(self, sync_opts: SyncOptions) -> None:
        """Run one library synchronization cycle (`audiobookdl.sync.run_sync_once`) on a worker thread."""
        from audiobookdl.sync import run_sync_once

        cfg = self._config.snapshot()
        ffmpeg_dir = cfg.ffmpeg_dir.strip() or None if cfg.ffmpeg_dir else None
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._events.queue, cancel_ev)
        with self._sync_lock:
            self.sync_cancel_event = cancel_ev
            self.sync_interaction = interaction
            self._sync_cancel_events.add(cancel_ev)
            self._sync_interactions.add(interaction)
        self._tasks.track_interaction(interaction, cancel_ev)
        ctx = logging.JobContext(
            ffmpeg_dir=ffmpeg_dir,
            sink=self._events.sink,
            interaction=interaction,
            cancel_event=cancel_ev,
        )

        def _worker() -> None:
            try:
                with logging.job_context(ctx):
                    cycle = run_sync_once(
                        sync_opts,
                        config=cfg,
                        cancel_event=cancel_ev,
                        interaction=interaction,
                        sink=self._events.sink,
                    )
                self._events.queue.put(SyncDoneEvent(cycle=cycle, error=None))
            except Exception as exc:
                self._events.queue.put(SyncDoneEvent(cycle=None, error=exc))
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._tasks.untrack_interaction(interaction, cancel_ev)
                with self._sync_lock:
                    self._sync_cancel_events.discard(cancel_ev)
                    self._sync_interactions.discard(interaction)
                    if self.sync_cancel_event is cancel_ev:
                        self.sync_cancel_event = None
                    if self.sync_interaction is interaction:
                        self.sync_interaction = None

        self._tasks.submit_bg_task(_worker)

    def start_sync_enqueue_discovery(
        self,
        targets: Sequence[tuple[str, str]],
        sync_opts: SyncOptions,
    ) -> None:
        """Discover pending library URLs across `targets` via `sync.py` and emit `sync_enqueue_ready`."""
        from audiobookdl.sync import discover_pending_sync_books

        cfg = self._config.snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self._events.queue, cancel_ev)
        with self._sync_lock:
            self.sync_cancel_event = cancel_ev
            self.sync_interaction = interaction
            self._sync_cancel_events.add(cancel_ev)
            self._sync_interactions.add(interaction)
        self._tasks.track_interaction(interaction, cancel_ev)

        def _worker() -> None:
            try:
                discovery = discover_pending_sync_books(
                    sync_opts,
                    targets=targets,
                    config=cfg,
                    cancel_event=cancel_ev,
                    interaction=interaction,
                )
                cycle = discovery.cycle
                self._events.queue.put(
                    SyncEnqueueReadyEvent(
                        pending_groups=discovery.pending_groups,
                        stats={
                            "discovered": cycle.books_discovered,
                            "newly_added": cycle.newly_added,
                            "skipped": cycle.skipped,
                            "errors": list(cycle.errors),
                            "cancelled": cycle.cancelled or cancel_ev.is_set(),
                        },
                        error=None,
                    )
                )
            except Exception as exc:
                self._events.queue.put(SyncEnqueueReadyEvent(pending_groups=[], stats={}, error=exc))
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._tasks.untrack_interaction(interaction, cancel_ev)
                with self._sync_lock:
                    self._sync_cancel_events.discard(cancel_ev)
                    self._sync_interactions.discard(interaction)
                    if self.sync_cancel_event is cancel_ev:
                        self.sync_cancel_event = None
                    if self.sync_interaction is interaction:
                        self.sync_interaction = None

        self._tasks.submit_bg_task(_worker)

    # -- Audiobookshelf upload -------------------------------------------------

    @property
    def is_uploading_abs(self) -> bool:
        """Return whether one or more Audiobookshelf uploads are currently in flight."""
        with self._abs_upload_lock:
            return self._active_abs_uploads > 0

    def start_abs_upload(
        self,
        local_path: str,
        *,
        title: str,
        authors: Sequence[str] = (),
        series: str | None = None,
    ) -> None:
        """Upload a local archive entry to the configured Audiobookshelf server."""
        from audiobookdl.integrations.audiobookshelf import AudiobookshelfClient
        from audiobookdl.utils.audiobook import AudiobookMetadata

        cfg = self._config.snapshot()
        abs_cfg = cfg.audiobookshelf
        timeout = resolve_http_timeout(cfg)

        with self._abs_upload_lock:
            self._active_abs_uploads += 1

        def _worker() -> None:
            try:
                if not abs_cfg.url or not abs_cfg.api_key or not abs_cfg.library_id:
                    self._events.queue.put(
                        AbsUploadDoneEvent(
                            title=title,
                            ok=False,
                            error_message="Configure Audiobookshelf URL, token, and library ID first.",
                        )
                    )
                    return
                client = AudiobookshelfClient(
                    abs_cfg.url,
                    abs_cfg.api_key,
                    session=self._http.get_http_session(),
                    timeout=timeout,
                )
                try:
                    meta = AudiobookMetadata(
                        title=title,
                        authors=[a for a in authors if a],
                        series=series,
                    )
                    started_progress = False

                    def _on_upload_progress(sent: int, total: int) -> None:
                        nonlocal started_progress
                        if total <= 0:
                            return
                        if not started_progress:
                            started_progress = True
                            self._events.queue.put(
                                ProgressStartEvent(
                                    description=f"Uploading {title}",
                                    total=float(total),
                                )
                            )
                        self._events.queue.put(
                            ProgressAdvanceEvent(completed=float(sent), total=float(total))
                        )

                    try:
                        client.upload_book(
                            local_path,
                            library_id=abs_cfg.library_id,
                            metadata=meta,
                            on_progress=_on_upload_progress,
                        )
                    finally:
                        if started_progress:
                            self._events.queue.put(ProgressFinishEvent())
                    if abs_cfg.auto_scan:
                        client.scan_library(abs_cfg.library_id)
                    self._events.queue.put(AbsUploadDoneEvent(title=title, ok=True, error_message=None))
                except Exception as exc:
                    self._events.queue.put(AbsUploadDoneEvent(title=title, ok=False, error_message=str(exc)))
            finally:
                with self._abs_upload_lock:
                    self._active_abs_uploads = max(0, self._active_abs_uploads - 1)

        self._tasks.submit_bg_task(_worker)

    # -- Updates / FFmpeg -------------------------------------------------------

    def start_update_check(self, *, interactive: bool, respect_opt_out: bool) -> None:
        cfg = self._config.snapshot()
        proxy = cfg.proxy
        db_dir = resolve_database_directory(cfg, config_location=self._config.config_location)
        cache_path = os.path.join(db_dir, "update_check.json")

        def _worker() -> None:
            session = self._http.get_http_session()
            info = check_for_updates(
                proxy=proxy,
                session=session,
                respect_opt_out=respect_opt_out,
                cache_path=cache_path,
                force=interactive,
            )
            if interactive or (info is not None and info.has_update):
                self._events.queue.put(UpdateCheckedEvent(info=info, interactive=interactive))

        self._tasks.submit_bg_task(_worker)

    def start_winget_ffmpeg_install(self) -> None:
        def _worker() -> None:
            ok, detail = install_ffmpeg_via_winget()
            self._events.queue.put(FfmpegInstallDoneEvent(ok=ok, detail=detail))

        self._tasks.submit_bg_task(_worker)

    # -- Enrichment -------------------------------------------------------------

    def search_enrichment_for_audiobook(
        self,
        audiobook: Audiobook,
        options: DownloadOptions,
    ) -> list[EnrichmentCandidate]:
        """Query configured metadata enrichment providers for `audiobook`."""
        cfg = self._config.snapshot()
        custom_providers = cfg.custom_metadata_providers
        raw_providers = getattr(options, "enrich_providers", None)
        providers = resolve_provider_list(raw_providers, custom_providers=custom_providers)
        return search_enrichment_candidates(
            audiobook.metadata,
            providers=providers,
            custom_providers=custom_providers,
            session=self._http.get_http_session(),
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
            database_directory=getattr(options, "database_directory", None),
            use_cache=True,
        )

    def apply_enrichment_candidate(
        self,
        audiobook: Audiobook,
        candidate: EnrichmentCandidate,
        options: DownloadOptions,
    ) -> tuple[list[str], bool, bool]:
        """Apply `candidate` onto `audiobook` using controller session and cache."""
        db_dir = getattr(options, "database_directory", None)
        use_cache = True
        include_chapters = not bool(getattr(options, "no_chapters", False))
        override_chapters = bool(getattr(options, "override_chapters", False))
        if db_dir:
            with EnrichmentCache(default_enrichment_cache_path(db_dir), persistent=True) as cache:
                return apply_candidate_to_audiobook(
                    audiobook,
                    candidate,
                    session=self._http.get_http_session(),
                    proxy=getattr(options, "proxy", None),
                    timeout=getattr(options, "timeout", None) or 10.0,
                    cache=cache,
                    use_cache=use_cache,
                    include_chapters=include_chapters,
                    override_chapters=override_chapters,
                )
        return apply_candidate_to_audiobook(
            audiobook,
            candidate,
            session=self._http.get_http_session(),
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
            use_cache=use_cache,
            include_chapters=include_chapters,
            override_chapters=override_chapters,
        )

    # -- Shutdown helpers -------------------------------------------------------

    def cancel_all(self) -> None:
        """Cancel every auxiliary service task (preview, sync, catalog, accounts)."""
        self.cancel_preview()
        self.cancel_library_sync()
        self.cancel_catalog_fetch()
        self.cancel_accounts_verification()

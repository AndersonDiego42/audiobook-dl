"""Compatibility facade composing headless GUI services for `GuiController`.

`GuiController` remains the single object pages/coordinators/tests talk to, but the
implementation is delegated to focused modules:

- `event_bus`: typed event queue, log sink, prompt bridge, drain/coalescing
- `config_store`: cached `Config` and all GUI preference persistence
- `http_pool`: per-thread pooled HTTP sessions and cover fetching
- `queue_store`: queue records, pending deque, and `gui_queue.json` persistence
- `task_runner`: bounded background/DB executors and activity tracking
- `job_runner`: download/pack worker orchestration and run state
- `service_tasks`: auxiliary cancellable operations (preview, auth, sync, ABS, etc.)
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import threading
from typing import TYPE_CHECKING, Any

from audiobookdl.gui.config_store import GuiConfigStore
from audiobookdl.gui.event_bus import (
    ControllerEventQueue,
    GuiEventBus,
    GuiInteractionHandler,
    GuiLogSink,
)
from audiobookdl.gui.http_pool import GuiHttpPool
from audiobookdl.gui.job_runner import GuiJobRunner
from audiobookdl.gui.queue_store import GuiQueueStore, PackJobRequest, QueuedJob
from audiobookdl.gui.service_tasks import GuiServiceTasks
from audiobookdl.gui.task_runner import GuiTaskRunner

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from audiobookdl.config import Config
    from audiobookdl.gui.events import ControllerEvent
    from audiobookdl.options_schema import DownloadOptions, SyncOptions
    from audiobookdl.pack import PackBookSpec, PackOverrides
    from audiobookdl.sources.source import Source
    from audiobookdl.utils.audiobook import Audiobook, Result
    from audiobookdl.utils.enrichment import EnrichmentCandidate

__all__ = [
    "GuiController",
    "PackJobRequest",
    "QueuedJob",
    "GuiInteractionHandler",
    "ControllerEventQueue",
    "GuiLogSink",
]


class GuiController:
    """Facade owning configuration, job queue, worker thread, and async task dispatch for the GUI."""

    MAX_QUEUE_BATCH = 200
    _EDITABLE_OVERRIDE_KEYS = GuiQueueStore.EDITABLE_OVERRIDE_KEYS
    _EDITABLE_JOB_STATUSES = GuiQueueStore.EDITABLE_JOB_STATUSES
    _UNSET_VALUE = GuiQueueStore.UNSET_VALUE

    def __init__(self, config_location: str | None = None) -> None:
        self._config_store = GuiConfigStore(config_location)
        self._event_bus = GuiEventBus()
        self._http_pool = GuiHttpPool(self._config_store.snapshot)
        self._queue_store = GuiQueueStore(
            config_snapshot=self._config_store.snapshot,
            config_location=config_location,
        )
        self._task_runner = GuiTaskRunner()
        self._job_runner = GuiJobRunner(
            events=self._event_bus,
            queue_store=self._queue_store,
            config=self._config_store,
            tasks=self._task_runner,
        )
        self._service_tasks = GuiServiceTasks(
            events=self._event_bus,
            config=self._config_store,
            http=self._http_pool,
            tasks=self._task_runner,
        )
        # Preserve private attributes that tests/pages reach into directly.
        self._thread_local = self._http_pool._thread_local
        self._thread_sessions = self._http_pool._thread_sessions
        self._sessions_lock = self._http_pool._sessions_lock

    # -- Core state ---------------------------------------------------------

    @property
    def config_location(self) -> str | None:
        return self._config_store.config_location

    @config_location.setter
    def config_location(self, value: str | None) -> None:
        self._config_store.config_location = value
        self._queue_store.config_location = value

    @property
    def config(self) -> Config:
        return self._config_store.config

    @config.setter
    def config(self, value: Config) -> None:
        self._config_store.config = value

    @property
    def event_queue(self) -> ControllerEventQueue:
        return self._event_bus.queue

    @property
    def sink(self) -> GuiLogSink:
        return self._event_bus.sink

    @property
    def pending_jobs(self) -> deque[QueuedJob | None]:
        return self._queue_store.pending_jobs

    @property
    def queue_lock(self) -> threading.RLock:
        return self._queue_store.queue_lock

    @property
    def queue_cond(self) -> threading.Condition:
        return self._queue_store.queue_cond

    @property
    def queue_records(self) -> dict[str, dict[str, Any]]:
        return self._queue_store.queue_records

    @property
    def worker(self) -> threading.Thread | None:
        return self._job_runner.worker

    @worker.setter
    def worker(self, value: threading.Thread | None) -> None:
        self._job_runner.worker = value

    @property
    def worker_has_active_run(self) -> bool:
        return self._job_runner.worker_has_active_run

    @worker_has_active_run.setter
    def worker_has_active_run(self, value: bool) -> None:
        self._job_runner.worker_has_active_run = value

    @property
    def worker_run_id(self) -> int:
        return self._job_runner.worker_run_id

    @worker_run_id.setter
    def worker_run_id(self, value: int) -> None:
        self._job_runner.worker_run_id = value

    @property
    def batch_total(self) -> int:
        return self._job_runner.batch_total

    @batch_total.setter
    def batch_total(self, value: int) -> None:
        self._job_runner.batch_total = value

    @property
    def run_seen_urls(self) -> set[str]:
        return self._job_runner.run_seen_urls

    @run_seen_urls.setter
    def run_seen_urls(self, value: set[str]) -> None:
        self._job_runner.run_seen_urls = value

    @property
    def active_job_id(self) -> str | None:
        return self._job_runner.active_job_id

    @active_job_id.setter
    def active_job_id(self, value: str | None) -> None:
        self._job_runner.active_job_id = value

    @property
    def active_job_ids(self) -> set[str]:
        return self._job_runner.active_job_ids

    @active_job_ids.setter
    def active_job_ids(self, value: set[str]) -> None:
        self._job_runner.active_job_ids = value

    @property
    def running_job_ids(self) -> set[str]:
        return self._job_runner.running_job_ids

    @running_job_ids.setter
    def running_job_ids(self, value: set[str]) -> None:
        self._job_runner.running_job_ids = value

    @property
    def active_job_running(self) -> bool:
        return self._job_runner.active_job_running

    @active_job_running.setter
    def active_job_running(self, value: bool) -> None:
        self._job_runner.active_job_running = value

    @property
    def queue_concurrency(self) -> int:
        return self._job_runner.queue_concurrency

    @queue_concurrency.setter
    def queue_concurrency(self, value: int) -> None:
        self._job_runner.queue_concurrency = value

    @property
    def cancelled_job_ids(self) -> set[str]:
        return self._job_runner.cancelled_job_ids

    @cancelled_job_ids.setter
    def cancelled_job_ids(self, value: set[str]) -> None:
        self._job_runner.cancelled_job_ids = value

    @property
    def interaction(self) -> GuiInteractionHandler | None:
        return self._job_runner.interaction

    @interaction.setter
    def interaction(self, value: GuiInteractionHandler | None) -> None:
        self._job_runner.interaction = value

    @property
    def cancel_event(self) -> threading.Event:
        return self._job_runner.cancel_event

    @cancel_event.setter
    def cancel_event(self, value: threading.Event) -> None:
        self._job_runner.cancel_event = value

    @property
    def pause_event(self) -> threading.Event:
        return self._job_runner.pause_event

    @pause_event.setter
    def pause_event(self, value: threading.Event) -> None:
        self._job_runner.pause_event = value

    @property
    def preview_cancel_event(self) -> threading.Event:
        return self._service_tasks.preview_cancel_event

    @preview_cancel_event.setter
    def preview_cancel_event(self, value: threading.Event) -> None:
        self._service_tasks.preview_cancel_event = value

    @property
    def preview_request_id(self) -> int:
        return self._service_tasks.preview_request_id

    @preview_request_id.setter
    def preview_request_id(self, value: int) -> None:
        self._service_tasks.preview_request_id = value

    @property
    def preview_interaction(self) -> GuiInteractionHandler | None:
        return self._service_tasks.preview_interaction

    @preview_interaction.setter
    def preview_interaction(self, value: GuiInteractionHandler | None) -> None:
        self._service_tasks.preview_interaction = value

    @property
    def catalog_request_id(self) -> int:
        return self._service_tasks.catalog_request_id

    @catalog_request_id.setter
    def catalog_request_id(self, value: int) -> None:
        self._service_tasks.catalog_request_id = value

    @property
    def catalog_cancel_event(self) -> threading.Event | None:
        return self._service_tasks.catalog_cancel_event

    @catalog_cancel_event.setter
    def catalog_cancel_event(self, value: threading.Event | None) -> None:
        self._service_tasks.catalog_cancel_event = value

    @property
    def catalog_interaction(self) -> GuiInteractionHandler | None:
        return self._service_tasks.catalog_interaction

    @catalog_interaction.setter
    def catalog_interaction(self, value: GuiInteractionHandler | None) -> None:
        self._service_tasks.catalog_interaction = value

    @property
    def sync_cancel_event(self) -> threading.Event | None:
        return self._service_tasks.sync_cancel_event

    @sync_cancel_event.setter
    def sync_cancel_event(self, value: threading.Event | None) -> None:
        self._service_tasks.sync_cancel_event = value

    @property
    def sync_interaction(self) -> GuiInteractionHandler | None:
        return self._service_tasks.sync_interaction

    @sync_interaction.setter
    def sync_interaction(self, value: GuiInteractionHandler | None) -> None:
        self._service_tasks.sync_interaction = value

    @property
    def is_uploading_abs(self) -> bool:
        """Return whether one or more Audiobookshelf uploads are currently in flight."""
        return self._service_tasks.is_uploading_abs

    @property
    def db_executor(self) -> ThreadPoolExecutor:
        """Return the shared single-worker serialized SQLite executor."""
        return self._task_runner.db_executor

    # -- Configuration -------------------------------------------------------

    @staticmethod
    def make_job_id() -> str:
        return GuiQueueStore.make_job_id()

    def load_saved_config(self) -> Config:
        """Load configuration from `config_location` (or default)."""
        return self._config_store.load_saved()

    def reload_config(self) -> Config:
        return self._config_store.reload()

    def config_snapshot(self) -> Config:
        """Return a thread-safe isolated copy of the current cached configuration."""
        return self._config_store.snapshot()

    def save_ffmpeg_dir(self, resolved_dir: str | None) -> Config:
        return self._config_store.save_ffmpeg_dir(resolved_dir)

    def save_custom_metadata_provider(
        self,
        name: str,
        *,
        url: str,
        api_key: str | None = None,
        enabled: bool = True,
    ) -> Config:
        """Persist or update a custom ABS-spec metadata provider and reload `self.config`."""
        return self._config_store.save_custom_metadata_provider(
            name, url=url, api_key=api_key, enabled=enabled
        )

    def delete_custom_metadata_provider(self, name: str) -> Config:
        """Delete a custom ABS-spec metadata provider and reload `self.config`."""
        return self._config_store.delete_custom_metadata_provider(name)

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
        return self._config_store.save_source_credentials(
            service_key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            profile=profile,
            locale=locale,
            oauth_client_id=oauth_client_id,
            oauth_scopes=oauth_scopes,
            preserve_optional=preserve_optional,
        )

    def delete_source_profile(
        self,
        service_key: str,
        *,
        profile: str | None = None,
    ) -> Config:
        """Delete a profile's config, vault secrets, OAuth tokens, and cached session/device files."""
        return self._config_store.delete_source_profile(service_key, profile=profile)

    def rename_source_profile(
        self,
        service_key: str,
        old_profile: str,
        new_profile: str,
    ) -> Config:
        """Rename a configured service profile and migrate its secrets, OAuth tokens, and caches."""
        return self._config_store.rename_source_profile(service_key, old_profile, new_profile)

    def save_run_preferences(
        self,
        options: DownloadOptions,
        *,
        output_dir: str,
        output_template: str,
        use_download_archive: bool | None = None,
    ) -> Config:
        return self._config_store.save_run_preferences(
            options,
            output_dir=output_dir,
            output_template=output_template,
            use_download_archive=use_download_archive,
        )

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
        self._config_store.save_window_preferences(
            window_geometry=window_geometry,
            window_maximized=window_maximized,
            download_split_ratio=download_split_ratio,
            active_page=active_page,
            options_collapsed=options_collapsed,
            output_naming_collapsed=output_naming_collapsed,
            output_dir=output_dir,
            output_template=output_template,
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
            output_prefs=output_prefs,
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
        return self._config_store.save_gui_preferences(
            notify_on_complete=notify_on_complete,
            clipboard_monitor=clipboard_monitor,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
            queue_concurrency=queue_concurrency,
            minimize_to_tray=minimize_to_tray,
        )

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
        return self._config_store.save_audiobookshelf_config(
            url=url,
            library_id=library_id,
            api_key=api_key,
            auto_upload=auto_upload,
            auto_scan=auto_scan,
        )

    def set_check_updates(self, enabled: bool) -> Config:
        """Persist `check_updates` in `audiobook-dl.toml` and reload `self.config`."""
        return self._config_store.set_check_updates(enabled)

    # -- HTTP pool -------------------------------------------------------------

    def get_http_session(self) -> Any:
        """Return a per-thread pooled `requests.Session` configured with user proxy settings."""
        return self._http_pool.get_http_session()

    def fetch_cover_bytes(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout: float | None = None,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> bytes | None:
        """Download cover image bytes via pooled thread-local session, respecting proxy, timeout, and size cap."""
        return self._http_pool.fetch_cover_bytes(url, proxy=proxy, timeout=timeout, max_bytes=max_bytes)

    # -- Queue store -----------------------------------------------------------

    def queue_state_path(self) -> str:
        """Return path to `gui_queue.json` inside the effective database directory."""
        return self._queue_store.queue_state_path()

    def save_queue_state(self) -> None:
        """Persist non-completed download and pack queue entries to `gui_queue.json` atomically."""
        self._queue_store.save_queue_state()

    def load_saved_queue_records(self) -> list[dict[str, Any]]:
        """Load persisted unresolved download and pack queue items from `gui_queue.json` once."""
        return self._queue_store.load_saved_queue_records()

    def _request_save_queue_state(self) -> None:
        """Persist immediately, or coalesce until the active `drain_events` batch finishes."""
        self._queue_store.request_save_queue_state()

    def register_queue_record(
        self,
        job: QueuedJob,
        service_label: str,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        return self._queue_store.register_queue_record(job, service_label, persist=persist)

    def get_queue_record(self, job_id: str) -> dict[str, Any] | None:
        """Return a shallow copy of the queue record for `job_id`, if present."""
        return self._queue_store.get_queue_record(job_id)

    def update_queue_record(self, job_id: str, status: str, detail: str = "") -> dict[str, Any] | None:
        return self._queue_store.update_queue_record(job_id, status, detail)

    def update_queue_metadata(
        self,
        job_id: str,
        *,
        title: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Update resolved title and/or output path for `job_id` and persist if changed."""
        return self._queue_store.update_queue_metadata(
            job_id, title=title, output_path=output_path
        )

    def update_queue_job_config(
        self,
        job_id: str,
        *,
        profile: Any = _UNSET_VALUE,
        job_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update editable `profile` and/or `job_overrides` on a non-running queue job."""
        return self._queue_store.update_queue_job_config(
            job_id, profile=profile, job_overrides=job_overrides
        )

    def retryable_urls(self, ordered_ids: Sequence[str]) -> list[str]:
        return self._queue_store.retryable_urls(ordered_ids)

    def retryable_jobs(self, ordered_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return shallow copies of retryable download and pack queue records in order."""
        return self._queue_store.retryable_jobs(ordered_ids)

    def clear_completed_records(
        self,
        ordered_ids: Sequence[str],
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[str]:
        return self._queue_store.clear_completed_records(ordered_ids, statuses=statuses)

    def move_pending_job(self, job_id: str, direction: int) -> str | None:
        """Swap `job_id` with its neighbor in `pending_jobs`; returns swapped neighbor `job_id`."""
        return self._queue_store.move_pending_job(job_id, direction)

    def move_pending_job_to_edge(self, job_id: str, *, to_top: bool) -> list[str] | None:
        """Move `job_id` to the front or back of `pending_jobs`; return new pending job ID order."""
        return self._queue_store.move_pending_job_to_edge(job_id, to_top=to_top)

    def move_pending_job_to_target(self, job_id: str, target_job_id: str) -> list[str] | None:
        """Move pending `job_id` to the position currently occupied by `target_job_id`."""
        return self._queue_store.move_pending_job_to_target(job_id, target_job_id)

    # -- Job runner -------------------------------------------------------------

    def is_paused(self) -> bool:
        """Return `True` when the queue worker is paused between jobs."""
        return self._job_runner.is_paused()

    def pause_queue(self) -> None:
        """Pause the queue cooperatively (mid-job and before upcoming jobs)."""
        self._job_runner.pause_queue()

    def resume_queue(self) -> None:
        """Resume queue processing after a pause."""
        self._job_runner.resume_queue()

    def toggle_pause(self) -> bool:
        """Toggle queue pause state and return the new `is_paused()` state."""
        return self._job_runner.toggle_pause()

    def latest_run_id(self) -> int:
        """Return the latest download batch `worker_run_id` under lock."""
        return self._job_runner.latest_run_id()

    def set_queue_concurrency(self, value: int) -> int:
        """Set maximum parallel queued jobs (`1..4`) and return the clamped value."""
        return self._job_runner.set_queue_concurrency(value)

    def clear_seen_urls(self) -> None:
        self._job_runner.clear_seen_urls()

    def start_new_run(
        self,
        job_specs: Sequence[tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]],
        *,
        service_label_resolver: Callable[[str], str] | None = None,
    ) -> tuple[int, list[QueuedJob]]:
        """Initialize queue state for a fresh download run and ensure the worker is running."""
        return self._job_runner.start_new_run(job_specs, service_label_resolver=service_label_resolver)

    def enqueue_additional_urls(
        self,
        urls: Sequence[str],
        build_job_options: Callable[[str], DownloadOptions],
        *,
        allow_existing: bool = False,
        selected_indices: list[int] | None = None,
        service_label_resolver: Callable[[str], str] | None = None,
    ) -> tuple[bool, list[QueuedJob], int]:
        """Append unseen URLs (or retried URLs when `allow_existing=True`) to the active queue."""
        return self._job_runner.enqueue_additional_urls(
            urls,
            build_job_options,
            allow_existing=allow_existing,
            selected_indices=selected_indices,
            service_label_resolver=service_label_resolver,
        )

    def enqueue_from_preview(
        self,
        url: str,
        options: DownloadOptions,
        prefetched: tuple[Source, Result],
        selected_indices: list[int] | None,
        *,
        service_label: str = "",
    ) -> tuple[QueuedJob, int] | None:
        """Append a previewed item to the active run if one exists; otherwise return `None`."""
        return self._job_runner.enqueue_from_preview(
            url, options, prefetched, selected_indices, service_label=service_label
        )

    def start_or_enqueue_jobs(
        self,
        *,
        download_specs: Sequence[
            tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ] = (),
        pack_requests: Sequence[tuple[PackJobRequest, DownloadOptions]] = (),
        service_label_resolver: Callable[[str], str] | None = None,
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue download and/or pack jobs onto the active run, or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        return self._job_runner.start_or_enqueue_jobs(
            download_specs=download_specs,
            pack_requests=pack_requests,
            service_label_resolver=service_label_resolver,
        )

    def start_or_enqueue_pack_jobs(
        self,
        requests_with_options: Sequence[tuple[PackJobRequest, DownloadOptions]],
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue local pack jobs on the active run or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        return self._job_runner.start_or_enqueue_pack_jobs(requests_with_options)

    def remove_pending_job(self, job_id: str) -> tuple[bool, int | None, bool]:
        """Remove a job from pending queue/records; returns `(removed, new_batch_total, is_active_running)`."""
        return self._job_runner.remove_pending_job(job_id)

    def cancel_active_run(self) -> None:
        self._job_runner.cancel_active_run()

    def ensure_worker_started(self) -> None:
        self._job_runner.ensure_worker_started()

    # -- Service tasks ---------------------------------------------------------

    def find_cross_source_duplicate(
        self,
        metadata: Any,
        *,
        source_name: str = "",
        archive_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an active archive row from another service matching `metadata`, if any."""
        return self._service_tasks.find_cross_source_duplicate(
            metadata, source_name=source_name, archive_path=archive_path
        )

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
        return self._service_tasks.inspect_local_pack_preview(
            paths,
            options,
            recursive=recursive,
            output_dir=output_dir,
            overrides=overrides,
            cancel_event=cancel_event,
        )

    def cancel_preview(self) -> None:
        """Cancel any active metadata preview and unblock pending interaction prompts."""
        self._service_tasks.cancel_preview()

    def start_preview(
        self,
        url: str,
        options: DownloadOptions,
        *,
        check_duplicate: bool = False,
    ) -> int:
        """Fetch metadata and resolved `Result` for `url` on a background thread."""
        return self._service_tasks.start_preview(url, options, check_duplicate=check_duplicate)

    def start_auth_verification(
        self,
        service_key: str,
        service_label: str,
        options: DownloadOptions,
    ) -> None:
        """Verify credentials for `service_key` on a background thread."""
        self._service_tasks.start_auth_verification(service_key, service_label, options)

    def start_oauth_connect(
        self,
        service_key: str,
        service_label: str,
        profile: str,
        options: DownloadOptions,
    ) -> threading.Event:
        """Run interactive OAuth 2.0 flow for `(service_key, profile)` in a background thread."""
        return self._service_tasks.start_oauth_connect(service_key, service_label, profile, options)

    def disconnect_oauth(
        self,
        service_key: str,
        options: DownloadOptions,
        *,
        profile: str | None = None,
    ) -> tuple[bool, Config]:
        """Revoke and delete stored OAuth tokens for `(service_key, profile)` and reload config."""
        return self._service_tasks.disconnect_oauth(service_key, options, profile=profile)

    def start_oauth_disconnect(
        self,
        service_key: str,
        service_label: str,
        profile: str,
        options: DownloadOptions,
    ) -> None:
        """Revoke OAuth tokens for `(service_key, profile)` on a background thread and emit `OAuthDisconnectedEvent`."""
        self._service_tasks.start_oauth_disconnect(service_key, service_label, profile, options)

    def start_accounts_verification(
        self,
        targets: Sequence[tuple[str, str, str]],
    ) -> int:
        """Verify multiple `(service_key, service_label, profile)` accounts with per-target cancel tokens."""
        return self._service_tasks.start_accounts_verification(targets)

    def cancel_accounts_verification(self) -> None:
        """Cancel any active multi-account login verification sweep immediately."""
        self._service_tasks.cancel_accounts_verification()

    def cancel_catalog_fetch(self) -> bool:
        """Cancel the currently in-flight catalog search/list request, if any."""
        return self._service_tasks.cancel_catalog_fetch()

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
        return self._service_tasks.start_catalog_fetch(
            mode, service_key, query_text, options, archive_path=archive_path
        )

    def cancel_library_sync(self) -> None:
        """Signal any active library synchronization pass to cancel and unblock prompts."""
        self._service_tasks.cancel_library_sync()

    def start_library_sync(self, sync_opts: SyncOptions) -> None:
        """Run one library synchronization cycle (`audiobookdl.sync.run_sync_once`) on a worker thread."""
        self._service_tasks.start_library_sync(sync_opts)

    def start_sync_enqueue_discovery(
        self,
        targets: Sequence[tuple[str, str]],
        sync_opts: SyncOptions,
    ) -> None:
        """Discover pending library URLs across `targets` via `sync.py` and emit `sync_enqueue_ready`."""
        self._service_tasks.start_sync_enqueue_discovery(targets, sync_opts)

    def start_abs_upload(
        self,
        local_path: str,
        *,
        title: str,
        authors: Sequence[str] = (),
        series: str | None = None,
    ) -> None:
        """Upload a local archive entry to the configured Audiobookshelf server."""
        self._service_tasks.start_abs_upload(
            local_path, title=title, authors=authors, series=series
        )

    def start_update_check(self, *, interactive: bool, respect_opt_out: bool) -> None:
        self._service_tasks.start_update_check(interactive=interactive, respect_opt_out=respect_opt_out)

    def start_winget_ffmpeg_install(self) -> None:
        self._service_tasks.start_winget_ffmpeg_install()

    def search_enrichment_for_audiobook(
        self,
        audiobook: Audiobook,
        options: DownloadOptions,
    ) -> list[EnrichmentCandidate]:
        """Query configured metadata enrichment providers for `audiobook`."""
        return self._service_tasks.search_enrichment_for_audiobook(audiobook, options)

    def apply_enrichment_candidate(
        self,
        audiobook: Audiobook,
        candidate: EnrichmentCandidate,
        options: DownloadOptions,
    ) -> tuple[list[str], bool, bool]:
        """Apply `candidate` onto `audiobook` using controller session and cache."""
        return self._service_tasks.apply_enrichment_candidate(audiobook, candidate, options)

    # -- Task runner ---------------------------------------------------------

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Register a main-thread wakeup callback invoked when background tasks start."""
        self._task_runner.set_activity_callback(callback)

    def _notify_activity(self) -> None:
        self._task_runner.notify_activity()

    def has_active_background_tasks(self) -> bool:
        """Return True while queue worker jobs or bounded pool tasks are active."""
        return self._job_runner.has_active_run() or self._task_runner.has_active_bg_tasks()

    def _submit_bg_task(self, fn: Callable[[], None]) -> None:
        """Execute `fn` on the shared bounded GUI background executor."""
        self._task_runner.submit_bg_task(fn)

    def _register_aux_interaction(
        self,
        interaction: GuiInteractionHandler,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._task_runner.track_interaction(interaction, cancel_event)

    def _unregister_aux_interaction(
        self,
        interaction: GuiInteractionHandler,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._task_runner.untrack_interaction(interaction, cancel_event)

    def release_auxiliary_interactions(self) -> None:
        """Signal and unblock any pending auxiliary interaction prompts and cancel events."""
        self._task_runner.release_tracked_interactions()

    # -- Event drain / shutdown ------------------------------------------------

    def drain_events(
        self,
        handler: Callable[[ControllerEvent], None],
        max_batch: int = MAX_QUEUE_BATCH,
    ) -> int:
        """Drain up to `max_batch` events, coalescing consecutive logs, progress, and queue saves."""
        with self._queue_store.defer_queue_saves():
            return self._event_bus.drain_events(handler, max_batch=max_batch)

    def shutdown(self, *, is_running: bool, is_previewing: bool) -> None:
        """Cancel all active operations, persist unresolved queue state, stop worker, and close HTTP."""
        del is_running, is_previewing
        self._queue_store.save_queue_state()
        self._job_runner.cancel_active_run()
        self._service_tasks.cancel_all()
        self._task_runner.release_tracked_interactions()
        self._job_runner.shutdown()
        self._task_runner.shutdown()
        self._http_pool.close_all()

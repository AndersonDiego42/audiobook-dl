"""Download/pack queue worker orchestration for `GuiController`."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.event_bus import GuiInteractionHandler
from audiobookdl.gui.events import (
    BatchItemEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogEvent,
    PreviewDoneEvent,
    RunDoneEvent,
)
from audiobookdl.gui.formatting import format_failed_job_detail
from audiobookdl.gui.queue_store import PackJobRequest, QueuedJob
from audiobookdl.pipeline import (
    DownloadSummary,
    close_session_pool,
    run_downloads,
    session_pool_scope,
)
from audiobookdl.pack import run_pack

if TYPE_CHECKING:
    from audiobookdl.gui.config_store import GuiConfigStore
    from audiobookdl.gui.event_bus import GuiEventBus
    from audiobookdl.gui.queue_store import GuiQueueStore
    from audiobookdl.gui.task_runner import GuiTaskRunner
    from audiobookdl.options_schema import DownloadOptions
    from audiobookdl.pipeline import SessionPool
    from audiobookdl.sources.source import Source
    from audiobookdl.utils.audiobook import Result


class GuiJobRunner:
    """Owns run/queue execution state and the persistent download/pack worker."""

    def __init__(
        self,
        *,
        events: GuiEventBus,
        queue_store: GuiQueueStore,
        config: GuiConfigStore,
        tasks: GuiTaskRunner,
    ) -> None:
        self._events = events
        self._queue = queue_store
        self._config = config
        self._tasks = tasks
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.interaction: GuiInteractionHandler | None = None
        self.worker: threading.Thread | None = None
        self.worker_has_active_run = False
        self.worker_run_id = 0
        self.batch_total = 0
        self.run_seen_urls: set[str] = set()
        self.active_job_id: str | None = None
        self.active_job_ids: set[str] = set()
        self.running_job_ids: set[str] = set()
        self.active_job_running = False
        self.queue_concurrency: int = max(
            1, min(4, int(self._config.snapshot().gui.queue_concurrency or 1))
        )
        self.cancelled_job_ids: set[str] = set()

    # -- Queue pause/resume -------------------------------------------------

    def is_paused(self) -> bool:
        """Return `True` when the queue worker is paused between jobs."""
        return not self.pause_event.is_set()

    def pause_queue(self) -> None:
        """Pause the queue cooperatively (mid-job and before upcoming jobs)."""
        self.pause_event.clear()
        with self._queue.queue_cond:
            running_ids = list(self.running_job_ids)
        for job_id in running_ids:
            self._events.queue.put(JobStatusEvent(job_id=job_id, status="Paused", detail="Paused"))

    def resume_queue(self) -> None:
        """Resume queue processing after a pause."""
        self.pause_event.set()
        with self._queue.queue_cond:
            running_ids = list(self.running_job_ids)
            self._queue.queue_cond.notify_all()
        for job_id in running_ids:
            self._events.queue.put(JobStatusEvent(job_id=job_id, status="Running", detail=""))

    def toggle_pause(self) -> bool:
        """Toggle queue pause state and return the new `is_paused()` state."""
        if self.is_paused():
            self.resume_queue()
            return False
        self.pause_queue()
        return True

    def latest_run_id(self) -> int:
        """Return the latest download batch `worker_run_id` under lock."""
        with self._queue.queue_cond:
            return self.worker_run_id

    def set_queue_concurrency(self, value: int) -> int:
        """Set maximum parallel queued jobs (`1..4`) and return the clamped value."""
        clamped = max(1, min(4, int(value)))
        with self._queue.queue_cond:
            self.queue_concurrency = clamped
        return clamped

    def clear_seen_urls(self) -> None:
        with self._queue.queue_lock:
            self.run_seen_urls.clear()

    def has_active_run(self) -> bool:
        """Return True while the queue worker reports an active download/pack run."""
        with self._queue.queue_cond:
            return bool(
                self.worker_has_active_run
                or self.running_job_ids
                or self.active_job_ids
                or self.active_job_running
            )

    # -- Job submission -----------------------------------------------------

    def _begin_run_state(self, job_count: int, seen_urls: Iterable[str]) -> int:
        """(Re)initialize run state for a fresh batch; caller must hold `queue_cond`."""
        self.cancel_event.clear()
        self.pause_event.set()
        self.interaction = GuiInteractionHandler(self._events.queue, self.cancel_event)
        self.worker_run_id += 1
        self.worker_has_active_run = True
        self.batch_total = job_count
        self.run_seen_urls = set(seen_urls)
        self._events.sink.set_active_run_id(self.worker_run_id)
        return self.worker_run_id

    def _extend_run_state(self, added_count: int, seen_urls: Iterable[str]) -> None:
        """Bump batch totals and mark URLs seen for the active run; caller holds `queue_cond`."""
        if self.interaction is None:
            self.interaction = GuiInteractionHandler(self._events.queue, self.cancel_event)
        self.batch_total += added_count
        self.run_seen_urls.update(seen_urls)

    def _append_queued_job(
        self,
        job: QueuedJob,
        service_label: str,
        queued_jobs: list[QueuedJob] | None = None,
    ) -> None:
        """Register a job's queue record and append it to `pending_jobs`."""
        self._queue.register_queue_record(job, service_label, persist=False)
        self._queue.pending_jobs.append(job)
        if queued_jobs is not None:
            queued_jobs.append(job)

    def start_new_run(
        self,
        job_specs: Sequence[tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]],
        *,
        service_label_resolver: Callable[[str], str] | None = None,
    ) -> tuple[int, list[QueuedJob]]:
        """Initialize queue state for a fresh download run and ensure the worker is running."""
        queued_jobs: list[QueuedJob] = []
        with self._queue.queue_cond:
            run_id = self._begin_run_state(len(job_specs), (url for url, _, _, _ in job_specs))
            interaction = self.interaction
            for url, job_opts, selected_indices, prefetched in job_specs:
                job = QueuedJob(
                    job_id=self._queue.make_job_id(),
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                )
                service_label = service_label_resolver(url) if service_label_resolver is not None else ""
                self._append_queued_job(job, service_label, queued_jobs)
            self._queue.queue_cond.notify_all()

        self.ensure_worker_started()
        return run_id, queued_jobs

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
        unique_urls = list(dict.fromkeys(urls))
        added_jobs: list[QueuedJob] = []
        with self._queue.queue_cond:
            if not self.worker_has_active_run or self.cancel_event.is_set():
                return False, [], self.batch_total
            new_urls = (
                list(unique_urls)
                if allow_existing
                else [u for u in unique_urls if u not in self.run_seen_urls]
            )
            if not new_urls:
                return True, [], self.batch_total
            self._extend_run_state(len(new_urls), new_urls)
            interaction = self.interaction
            run_id = self.worker_run_id
            for u in new_urls:
                job = QueuedJob(
                    job_id=self._queue.make_job_id(),
                    run_id=run_id,
                    url=u,
                    options=build_job_options(u),
                    interaction=interaction,
                    selected_indices=list(selected_indices) if selected_indices is not None and len(new_urls) == 1 else None,
                )
                service_label = service_label_resolver(u) if service_label_resolver is not None else ""
                self._append_queued_job(job, service_label, added_jobs)
            new_total = self.batch_total
            self._queue.queue_cond.notify_all()
        return True, added_jobs, new_total

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
        with self._queue.queue_cond:
            if not self.worker_has_active_run or self.cancel_event.is_set():
                return None
            self._extend_run_state(1, (url,))
            added_job = QueuedJob(
                job_id=self._queue.make_job_id(),
                run_id=self.worker_run_id,
                url=url,
                options=options,
                interaction=self.interaction,
                selected_indices=selected_indices,
                prefetched=prefetched,
            )
            self._append_queued_job(added_job, service_label)
            new_total = self.batch_total
            self._queue.queue_cond.notify_all()
            return added_job, new_total

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
        total_incoming = len(download_specs) + len(pack_requests)
        queued_jobs: list[QueuedJob] = []
        with self._queue.queue_cond:
            if self.worker_has_active_run and self.cancel_event.is_set():
                return False, self.worker_run_id, [], self.batch_total
            was_appended = self.worker_has_active_run
            seen_urls = (url for url, _, _, _ in download_specs)
            if not was_appended:
                run_id = self._begin_run_state(total_incoming, seen_urls)
            else:
                self._extend_run_state(total_incoming, seen_urls)
                run_id = self.worker_run_id
            interaction = self.interaction
            for url, job_opts, selected_indices, prefetched in download_specs:
                job = QueuedJob(
                    job_id=self._queue.make_job_id(),
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                )
                service_label = service_label_resolver(url) if service_label_resolver is not None else ""
                self._append_queued_job(job, service_label, queued_jobs)
            for req, opts in pack_requests:
                display = req.label or (req.paths[0] if req.paths else "Local folder")
                job = QueuedJob(
                    job_id=self._queue.make_job_id(),
                    run_id=run_id,
                    url=display,
                    options=opts,
                    interaction=interaction,
                    pack_request=req,
                )
                self._append_queued_job(job, "Local Pack", queued_jobs)
            new_total = self.batch_total
            self._queue.queue_cond.notify_all()

        self.ensure_worker_started()
        return was_appended, run_id, queued_jobs, new_total

    def start_or_enqueue_pack_jobs(
        self,
        requests_with_options: Sequence[tuple[PackJobRequest, DownloadOptions]],
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue local pack jobs on the active run or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        return self.start_or_enqueue_jobs(pack_requests=requests_with_options)

    # -- Queue mutation -----------------------------------------------------

    @staticmethod
    def _close_job_prefetched(job: QueuedJob | None) -> None:
        if job is None or job.prefetched is None:
            return
        source = job.prefetched[0]
        job.prefetched = None
        close_fn = getattr(source, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def remove_pending_job(self, job_id: str) -> tuple[bool, int | None, bool]:
        """Remove a job from pending queue/records; returns `(removed, new_batch_total, is_active_running)`."""
        removed_job: QueuedJob | None = None
        with self._queue.queue_cond:
            rec = self._queue.queue_records.get(job_id)
            if rec is None:
                return False, None, False
            pending_idx: int | None = None
            for idx, job in enumerate(self._queue.pending_jobs):
                if job is not None and job.job_id == job_id:
                    pending_idx = idx
                    break
            if pending_idx is not None:
                removed_job = self._queue.pending_jobs[pending_idx]
                del self._queue.pending_jobs[pending_idx]
                self.batch_total = max(0, self.batch_total - 1)
                if not any(j is not None and j.url == rec["url"] for j in self._queue.pending_jobs):
                    self.run_seen_urls.discard(rec["url"])
                new_total: int | None = self.batch_total
            elif job_id in self.active_job_ids:
                if job_id in self.running_job_ids or rec["status"] == "Running":
                    return False, None, True
                self.cancelled_job_ids.add(job_id)
                self.batch_total = max(0, self.batch_total - 1)
                if not any(j is not None and j.url == rec["url"] for j in self._queue.pending_jobs):
                    self.run_seen_urls.discard(rec["url"])
                new_total = self.batch_total
                self._queue.queue_records.pop(job_id, None)
                self._queue.queue_cond.notify_all()
                self._events.queue.put(JobStatusEvent(job_id=job_id, status="Cancelled", detail=""))
                self._queue.save_queue_state()
                return True, new_total, False
            elif rec["status"] == "Running":
                return False, None, True
            else:
                new_total = None
            self._queue.queue_records.pop(job_id, None)
        self._close_job_prefetched(removed_job)
        self._queue.save_queue_state()
        return True, new_total, False

    def cancel_active_run(self) -> None:
        self.cancel_event.set()
        self.pause_event.set()
        if self.interaction is not None:
            self.interaction.release_all()

    # -- Worker lifecycle ---------------------------------------------------

    def ensure_worker_started(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
            )
            self.worker.start()
        self._tasks.notify_activity()

    def _drain_pending_jobs(self, cancel_status: str = "Cancelled") -> bool:
        """Remove queued jobs after cancellation/error; return True if close sentinel seen."""
        saw_sentinel = False
        cancelled_jobs: list[QueuedJob] = []
        with self._queue.queue_cond:
            while self._queue.pending_jobs:
                pending = self._queue.pending_jobs.popleft()
                if pending is None:
                    saw_sentinel = True
                    break
                cancelled_jobs.append(pending)
        for job in cancelled_jobs:
            self._close_job_prefetched(job)
            self._events.queue.put(JobStatusEvent(job_id=job.job_id, status=cancel_status, detail=""))
        return saw_sentinel

    def _pop_next_job_locked(self) -> tuple[QueuedJob | None, bool]:
        """Pop the next job from `pending_jobs` while holding `self._queue.queue_cond`."""
        if not self._queue.pending_jobs:
            if not self.active_job_ids:
                self.worker_has_active_run = False
                self.active_job_id = None
                self.running_job_ids.clear()
                self.active_job_running = False
            return None, False
        next_job = self._queue.pending_jobs.popleft()
        if next_job is None:
            if not self.active_job_ids:
                self.worker_has_active_run = False
                self.active_job_id = None
                self.running_job_ids.clear()
                self.active_job_running = False
            return None, True
        self.active_job_id = next_job.job_id
        return next_job, False

    def _discard_seen_url_if_unpending(self, url: str) -> None:
        with self._queue.queue_cond:
            if not any(j is not None and j.url == url for j in self._queue.pending_jobs):
                self.run_seen_urls.discard(url)

    def _execute_single_queued_job(
        self,
        active_job: QueuedJob,
        *,
        run_id: int,
        item_index: int,
        item_total: int,
        batch_summary: DownloadSummary,
        summary_lock: threading.Lock,
    ) -> bool:
        """Run one claimed `QueuedJob`, emit status events, and return True if batch was cancelled."""
        self._events.queue.put(
            BatchItemEvent(
                run_id=run_id,
                item_index=item_index,
                item_total=item_total,
                target_url=active_job.url,
            )
        )
        self._events.queue.put(
            JobStatusEvent(job_id=active_job.job_id, status="Running", detail="")
        )
        self._events.sink.set_active_job_id(active_job.job_id)

        should_break = False
        try:
            config = self._config.snapshot()
            resolved_title = ""
            resolved_output_path = ""
            if active_job.pack_request is not None:
                req = active_job.pack_request
                pack_summary = run_pack(
                    req.paths,
                    active_job.options,
                    config,
                    output_dir=req.output_dir,
                    recursive=req.recursive,
                    delete_source=req.delete_source,
                    overrides=req.overrides,
                    interaction=active_job.interaction,
                    cancel_event=self.cancel_event,
                    pause_event=self.pause_event,
                    sink=self._events.sink,
                )
                summary = DownloadSummary(
                    completed=1 if pack_summary.books_completed > 0 else 0,
                    failed=1 if pack_summary.books_failed > 0 else 0,
                    cancelled=pack_summary.cancelled,
                    books_completed=pack_summary.books_completed,
                    books_skipped=pack_summary.books_skipped,
                    books_failed=pack_summary.books_failed,
                    book_errors=list(pack_summary.book_errors),
                )
                if pack_summary.output_paths:
                    if len(pack_summary.output_paths) == 1:
                        resolved_output_path = pack_summary.output_paths[0]
                    else:
                        first_out = pack_summary.output_paths[0]
                        resolved_output_path = os.path.dirname(first_out) or first_out
                        base_label = req.label or os.path.basename(resolved_output_path) or "Local Pack"
                        resolved_title = f"{base_label} ({len(pack_summary.output_paths)} books)"
            else:
                run_kwargs: dict[str, Any] = {
                    "interaction": active_job.interaction,
                    "cancel_event": self.cancel_event,
                    "pause_event": self.pause_event,
                    "sink": self._events.sink,
                }
                if active_job.selected_indices is not None:
                    run_kwargs["selected_indices"] = active_job.selected_indices
                if active_job.prefetched is not None:
                    run_kwargs["prefetched"] = active_job.prefetched
                summary = run_downloads(
                    [active_job.url],
                    active_job.options,
                    config,
                    **run_kwargs,
                )
                if summary.outputs:
                    if len(summary.outputs) == 1:
                        resolved_title = str(summary.outputs[0].get("title") or "").strip()
                        resolved_output_path = str(summary.outputs[0].get("output_path") or "").strip()
                    else:
                        first_out = str(summary.outputs[0].get("output_path") or "").strip()
                        resolved_output_path = os.path.dirname(first_out) or first_out
            if resolved_title or resolved_output_path:
                self._events.queue.put(
                    JobMetaEvent(
                        job_id=active_job.job_id,
                        title=resolved_title,
                        output_path=resolved_output_path,
                    )
                )
            with summary_lock:
                batch_summary.completed += summary.completed
                batch_summary.failed += summary.failed
                batch_summary.books_completed += summary.books_completed
                batch_summary.books_skipped += summary.books_skipped
                batch_summary.books_failed += summary.books_failed
                batch_summary.book_errors.extend(summary.book_errors)
            if summary.cancelled:
                with summary_lock:
                    batch_summary.cancelled = True
                self._discard_seen_url_if_unpending(active_job.url)
                self._events.queue.put(
                    JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
                )
                should_break = True
            elif summary.failed > 0 or summary.books_failed > 0:
                detail = format_failed_job_detail(summary.book_errors)
                self._discard_seen_url_if_unpending(active_job.url)
                self._events.queue.put(
                    JobStatusEvent(job_id=active_job.job_id, status="Failed", detail=detail)
                )
            else:
                if summary.books_completed:
                    out_name = os.path.basename(resolved_output_path) if resolved_output_path else ""
                    detail = (
                        f"{summary.books_completed} ok — {out_name}"
                        if out_name
                        else f"{summary.books_completed} ok"
                    )
                else:
                    detail = "Skipped" if summary.books_skipped else "Done"
                self._events.queue.put(
                    JobStatusEvent(job_id=active_job.job_id, status="Completed", detail=detail)
                )
        except DownloadCancelled:
            with summary_lock:
                batch_summary.cancelled = True
            self._discard_seen_url_if_unpending(active_job.url)
            self._events.queue.put(
                JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
            )
            should_break = True
        except Exception as exc:
            if self.cancel_event.is_set():
                with summary_lock:
                    batch_summary.cancelled = True
                self._discard_seen_url_if_unpending(active_job.url)
                self._events.queue.put(
                    JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
                )
                should_break = True
            else:
                err_text = str(exc).strip() or type(exc).__name__
                with summary_lock:
                    batch_summary.failed += 1
                    batch_summary.books_failed += 1
                    batch_summary.book_errors.append((active_job.url, err_text))
                self._discard_seen_url_if_unpending(active_job.url)
                self._events.queue.put(
                    LogEvent(level="ERROR", message=f"{active_job.url}: {err_text}")
                )
                self._events.queue.put(
                    JobStatusEvent(
                        job_id=active_job.job_id,
                        status="Failed",
                        detail=err_text[:120],
                    )
                )
        finally:
            self._events.sink.set_active_job_id(None)
            self._close_job_prefetched(active_job)
            with self._queue.queue_cond:
                self.cancelled_job_ids.discard(active_job.job_id)
                self.running_job_ids.discard(active_job.job_id)
                self.active_job_ids.discard(active_job.job_id)
                if self.active_job_id == active_job.job_id:
                    self.active_job_id = next(iter(self.active_job_ids), None)
                self.active_job_running = bool(self.running_job_ids)
        return should_break

    def _run_batch_worker(
        self,
        *,
        run_id: int,
        initial_job: list[QueuedJob | None],
        batch_summary: DownloadSummary,
        summary_lock: threading.Lock,
        item_counter: list[int],
    ) -> bool:
        """Process queued jobs in a thread-isolated session pool; return True if sentinel seen."""
        saw_sentinel = False
        worker_session_pool: SessionPool = {}
        with session_pool_scope(worker_session_pool):
            try:
                while True:
                    with self._queue.queue_cond:
                        if initial_job and initial_job[0] is not None:
                            current_job: QueuedJob | None = initial_job[0]
                            initial_job[0] = None
                        else:
                            current_job, popped_sentinel = self._pop_next_job_locked()
                            if popped_sentinel:
                                saw_sentinel = True
                        if current_job is None:
                            break
                        self.active_job_ids.add(current_job.job_id)

                    if self.is_paused() and not self.cancel_event.is_set():
                        self._events.queue.put(
                            JobStatusEvent(job_id=current_job.job_id, status="Paused", detail="Queue paused")
                        )
                        while not self.pause_event.wait(0.2):
                            if self.cancel_event.is_set():
                                break
                            with self._queue.queue_cond:
                                if current_job.job_id in self.cancelled_job_ids:
                                    break
                    if self.cancel_event.is_set():
                        with summary_lock:
                            batch_summary.cancelled = True
                        self._close_job_prefetched(current_job)
                        with self._queue.queue_cond:
                            self.cancelled_job_ids.discard(current_job.job_id)
                            self.running_job_ids.discard(current_job.job_id)
                            self.active_job_ids.discard(current_job.job_id)
                            if self.active_job_id == current_job.job_id:
                                self.active_job_id = next(iter(self.active_job_ids), None)
                            self.active_job_running = bool(self.running_job_ids)
                        self._events.queue.put(
                            JobStatusEvent(job_id=current_job.job_id, status="Cancelled", detail="")
                        )
                        if self._drain_pending_jobs("Cancelled"):
                            saw_sentinel = True
                        break

                    with self._queue.queue_cond:
                        if current_job.job_id in self.cancelled_job_ids:
                            self.cancelled_job_ids.discard(current_job.job_id)
                            self.running_job_ids.discard(current_job.job_id)
                            self.active_job_ids.discard(current_job.job_id)
                            if self.active_job_id == current_job.job_id:
                                self.active_job_id = next(iter(self.active_job_ids), None)
                            self.active_job_running = bool(self.running_job_ids)
                            skipped_job: QueuedJob | None = current_job
                            active_job: QueuedJob | None = None
                        else:
                            self.running_job_ids.add(current_job.job_id)
                            self.active_job_running = True
                            item_total = self.batch_total
                            skipped_job = None
                            active_job = current_job

                    if active_job is None:
                        self._close_job_prefetched(skipped_job)
                        continue

                    with summary_lock:
                        item_counter[0] += 1
                        item_idx = item_counter[0]

                    should_break = self._execute_single_queued_job(
                        active_job,
                        run_id=run_id,
                        item_index=item_idx,
                        item_total=item_total,
                        batch_summary=batch_summary,
                        summary_lock=summary_lock,
                    )
                    if should_break:
                        if self._drain_pending_jobs("Cancelled"):
                            saw_sentinel = True
                        break
            finally:
                close_session_pool(worker_session_pool)
        return saw_sentinel

    def _worker_loop(self) -> None:
        while True:
            with self._queue.queue_cond:
                while not self._queue.pending_jobs:
                    self._queue.queue_cond.wait()
                first_job = self._queue.pending_jobs.popleft()
                if first_job is not None:
                    self.active_job_id = first_job.job_id
                    self.active_job_ids = {first_job.job_id}
                    self.running_job_ids = set()
                    self.active_job_running = False
                concurrency = max(1, min(4, int(self.queue_concurrency)))
            if first_job is None:
                break

            run_id = first_job.run_id
            batch_summary = DownloadSummary()
            summary_lock = threading.Lock()
            item_counter = [0]
            fatal_exc: Exception | None = None
            saw_sentinel = False
            initial_holder: list[QueuedJob | None] = [first_job]
            try:
                if concurrency <= 1:
                    saw_sentinel = self._run_batch_worker(
                        run_id=run_id,
                        initial_job=initial_holder,
                        batch_summary=batch_summary,
                        summary_lock=summary_lock,
                        item_counter=item_counter,
                    )
                else:
                    with ThreadPoolExecutor(
                        max_workers=concurrency,
                        thread_name_prefix="abdl-dl-job",
                    ) as job_pool:
                        futures = [
                            job_pool.submit(
                                self._run_batch_worker,
                                run_id=run_id,
                                initial_job=initial_holder if idx == 0 else [],
                                batch_summary=batch_summary,
                                summary_lock=summary_lock,
                                item_counter=item_counter,
                            )
                            for idx in range(concurrency)
                        ]
                        for fut in futures:
                            if fut.result():
                                saw_sentinel = True
            except Exception as exc:
                fatal_exc = exc
            finally:
                with self._queue.queue_cond:
                    if self.worker_run_id == run_id:
                        self.worker_has_active_run = False
                        self.active_job_ids.clear()
                        self.running_job_ids.clear()
                        self.active_job_id = None
                        self.active_job_running = False

            self._events.queue.put(
                RunDoneEvent(run_id=run_id, summary=batch_summary, error=fatal_exc)
            )
            if saw_sentinel:
                break

    def shutdown(self) -> None:
        """Drain pending jobs, append the close sentinel, and join the worker thread."""
        drained_jobs: list[QueuedJob] = []
        with self._queue.queue_cond:
            for item in self._queue.pending_jobs:
                if item is not None:
                    drained_jobs.append(item)
            self._queue.pending_jobs.clear()
            self._queue.pending_jobs.append(None)
            self._queue.queue_cond.notify_all()
            worker = self.worker
        for job in drained_jobs:
            self._close_job_prefetched(job)
        while True:
            try:
                ev = self._events.queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(ev, PreviewDoneEvent) and ev.prepared is not None:
                close_fn = getattr(ev.prepared[0], "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=0.5)

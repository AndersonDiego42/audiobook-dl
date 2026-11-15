"""State, queue, background worker, and event-bridge controller for the desktop GUI."""

from collections import deque
from dataclasses import dataclass
import os
import queue
import threading
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import uuid

from audiobookdl import args, logging
from audiobookdl.config import (
    Config,
    load_config,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.formatting import format_failed_job_detail
from audiobookdl.options_schema import PERSISTED_GUI_OPTION_SPECS
from audiobookdl.pipeline import (
    DownloadSummary,
    list_service_library,
    prepare_source_result,
    run_downloads,
    search_service,
    verify_service_auth,
)
from audiobookdl.sources.source import Source
from audiobookdl.utils import set_ffmpeg_dir
from audiobookdl.utils.audiobook import Audiobook, Result
from audiobookdl.utils.enrichment import (
    EnrichmentCandidate,
    apply_candidate_to_audiobook,
    parse_provider_list,
    search_enrichment_candidates,
)
from audiobookdl.utils.enrichment_cache import EnrichmentCache, default_enrichment_cache_path
from audiobookdl.utils.updater import check_for_updates, install_ffmpeg_via_winget


@dataclass
class QueuedJob:
    """Single queued URL download job consumed by the persistent GUI worker."""

    job_id: str
    run_id: int
    url: str
    options: args.DownloadOptions
    interaction: "GuiInteractionHandler"
    selected_indices: Optional[List[int]] = None
    prefetched: Optional[Tuple[Source, Result]] = None


class GuiLogSink:
    """Forwards core log and progress events onto the Tkinter event queue."""

    def __init__(self, event_queue: "queue.Queue[tuple]") -> None:
        self._queue = event_queue
        self._active_run_id = 0

    def set_active_run_id(self, run_id: int) -> None:
        self._active_run_id = run_id

    def on_log(self, level: str, message: str) -> None:
        self._queue.put(("log", level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        self._queue.put(("progress_start", description, total))

    def on_progress_advance(self, completed: float, total: float) -> None:
        self._queue.put(("progress_advance", completed, total))

    def on_progress_metrics(
        self,
        completed: float,
        total: float,
        bytes_downloaded: int,
        speed_bps: float,
        elapsed_seconds: float,
        eta_seconds: Optional[float],
    ) -> None:
        self._queue.put((
            "progress_metrics",
            completed,
            total,
            bytes_downloaded,
            speed_bps,
            elapsed_seconds,
            eta_seconds,
        ))

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        self._queue.put(("item_progress", self._active_run_id, current, total, title))

    def on_progress_finish(self) -> None:
        self._queue.put(("progress_finish",))


class GuiInteractionHandler:
    """Bridges worker-thread prompts to Tkinter modal dialogs on the main thread."""

    def __init__(
        self,
        event_queue: "queue.Queue[tuple]",
        cancel_event: threading.Event,
    ) -> None:
        self._queue = event_queue
        self._cancel_event = cancel_event
        self._pending_events: List[threading.Event] = []
        self._lock = threading.Lock()

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        if self._cancel_event.is_set():
            raise DownloadCancelled
        done = threading.Event()
        holder: dict = {"value": None}
        with self._lock:
            self._pending_events.append(done)
        self._queue.put(("prompt_input", prompt, hidden, holder, done))
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        if self._cancel_event.is_set() or holder["value"] is None:
            raise DownloadCancelled
        return str(holder["value"])

    def confirm(self, message: str) -> bool:
        if self._cancel_event.is_set():
            return False
        done = threading.Event()
        holder: dict = {"value": False}
        with self._lock:
            self._pending_events.append(done)
        self._queue.put(("prompt_confirm", logging.strip_markup(message), holder, done))
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        return bool(holder["value"])

    def choose_index(
        self,
        title: str,
        choices: Sequence[str],
        default: Optional[int] = None,
    ) -> Optional[int]:
        if self._cancel_event.is_set() or not choices:
            return None
        done = threading.Event()
        holder: dict = {"value": None}
        with self._lock:
            self._pending_events.append(done)
        self._queue.put(("prompt_choice", title, list(choices), default, holder, done))
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        val = holder["value"]
        return int(val) if isinstance(val, int) and 0 <= val < len(choices) else None

    def release_all(self) -> None:
        """Unblock any waiting prompt threads when the window closes or cancels."""
        with self._lock:
            for ev in self._pending_events:
                ev.set()


class GuiController:
    """Owns configuration, job queue, worker thread, and async task dispatch for the GUI."""

    MAX_QUEUE_BATCH = 200

    def __init__(self, config_location: Optional[str] = None) -> None:
        self.config_location = config_location
        self.config = self.load_saved_config()

        self.event_queue: "queue.Queue[tuple]" = queue.Queue()
        self.sink = GuiLogSink(self.event_queue)
        self.cancel_event = threading.Event()
        self.preview_cancel_event = threading.Event()
        self.interaction: Optional[GuiInteractionHandler] = None
        self.preview_interaction: Optional[GuiInteractionHandler] = None

        self.pending_jobs: Deque[Optional[QueuedJob]] = deque()
        self.queue_lock = threading.Lock()
        self.queue_cond = threading.Condition(self.queue_lock)
        self.worker: Optional[threading.Thread] = None
        self.worker_has_active_run = False
        self.worker_run_id = 0
        self.batch_total = 0
        self.run_seen_urls: Set[str] = set()
        self.queue_records: Dict[str, Dict[str, Any]] = {}
        self.catalog_request_id = 0
        self._http_session: Optional[Any] = None

    @staticmethod
    def make_job_id() -> str:
        return uuid.uuid4().hex[:12]

    def load_saved_config(self) -> Config:
        """Load configuration from `config_location` (or default) and apply `ffmpeg_dir`."""
        if self.config_location and not os.path.exists(self.config_location):
            cfg = Config(
                sources={},
                output_template=None,
                database_directory=None,
                skip_downloaded=None,
            )
        else:
            cfg = load_config(self.config_location)
        set_ffmpeg_dir(cfg.ffmpeg_dir)
        return cfg

    def reload_config(self) -> Config:
        loaded = self.load_saved_config()
        with self.queue_lock:
            self.config = loaded
        return loaded

    def config_snapshot(self) -> Config:
        """Return a thread-safe isolated copy of the current cached configuration."""
        with self.queue_lock:
            return self.config.clone()

    def save_ffmpeg_dir(self, resolved_dir: str) -> Config:
        set_ffmpeg_dir(resolved_dir)
        save_ffmpeg_dir(resolved_dir, overwrite=self.config_location)
        return self.reload_config()

    def save_source_credentials(
        self,
        service_key: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        library: Optional[str] = None,
        cookie_file: Optional[str] = None,
    ) -> Config:
        save_source_credentials(
            service_key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            overwrite=self.config_location,
        )
        return self.reload_config()

    def save_run_preferences(
        self,
        options: args.DownloadOptions,
        *,
        output_dir: str,
        output_template: str,
        use_download_archive: Optional[bool] = None,
    ) -> Config:
        prefs = {spec.name: getattr(options, spec.name) for spec in PERSISTED_GUI_OPTION_SPECS}
        save_output_preferences(config_location=self.config_location, **prefs)
        save_gui_preferences(
            output_dir=output_dir,
            output_template=output_template,
            overwrite=self.config_location,
            use_download_archive=use_download_archive,
        )
        return self.reload_config()

    def save_window_preferences(
        self,
        *,
        window_geometry: Optional[str],
        output_dir: Optional[str],
        output_template: Optional[str],
        use_download_archive: Optional[bool] = None,
        theme: Optional[str] = None,
        notify_on_complete: Optional[bool] = None,
        output_prefs: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if output_prefs:
            save_output_preferences(config_location=self.config_location, **output_prefs)
        save_gui_preferences(
            window_geometry=window_geometry,
            output_dir=output_dir,
            output_template=output_template,
            overwrite=self.config_location,
            use_download_archive=use_download_archive,
            theme=theme,
            notify_on_complete=notify_on_complete,
        )

    def get_http_session(self) -> Any:
        if self._http_session is None:
            import requests

            self._http_session = requests.Session()
        return self._http_session

    def register_queue_record(self, job: QueuedJob, service_label: str) -> Dict[str, Any]:
        rec = {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "url": job.url,
            "status": "Queued",
            "detail": "",
            "service": service_label,
        }
        self.queue_records[job.job_id] = rec
        return rec

    def update_queue_record(self, job_id: str, status: str, detail: str = "") -> Optional[Dict[str, Any]]:
        rec = self.queue_records.get(job_id)
        if rec is None:
            return None
        rec["status"] = status
        rec["detail"] = detail
        return rec

    def clear_seen_urls(self) -> None:
        with self.queue_lock:
            self.run_seen_urls.clear()

    def start_new_run(
        self,
        job_specs: Sequence[Tuple[str, args.DownloadOptions, Optional[List[int]], Optional[Tuple[Source, Result]]]],
    ) -> Tuple[int, List[QueuedJob]]:
        """Initialize queue state for a fresh download run and ensure the worker is running."""
        self.cancel_event.clear()
        self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)

        queued_jobs: List[QueuedJob] = []
        with self.queue_cond:
            self.worker_run_id += 1
            run_id = self.worker_run_id
            self.sink.set_active_run_id(run_id)
            self.worker_has_active_run = True
            self.batch_total = len(job_specs)
            self.run_seen_urls = {url for url, _, _, _ in job_specs}
            for url, job_opts, selected_indices, prefetched in job_specs:
                job = QueuedJob(
                    job_id=self.make_job_id(),
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=self.interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                )
                self.pending_jobs.append(job)
                queued_jobs.append(job)
            self.queue_cond.notify_all()

        self.ensure_worker_started()
        return run_id, queued_jobs

    def enqueue_additional_urls(
        self,
        urls: Sequence[str],
        build_job_options: Callable[[str], args.DownloadOptions],
    ) -> Tuple[bool, List[QueuedJob], int]:
        """Append unseen URLs to the active queue; returns `(has_active_run, added_jobs, batch_total)`."""
        unique_urls = list(dict.fromkeys(urls))
        added_jobs: List[QueuedJob] = []
        with self.queue_cond:
            if not self.worker_has_active_run:
                return False, [], self.batch_total
            new_urls = [u for u in unique_urls if u not in self.run_seen_urls]
            if not new_urls:
                return True, [], self.batch_total
            if self.interaction is None:
                self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
            interaction = self.interaction
            run_id = self.worker_run_id
            self.batch_total += len(new_urls)
            self.run_seen_urls.update(new_urls)
            for u in new_urls:
                job = QueuedJob(
                    job_id=self.make_job_id(),
                    run_id=run_id,
                    url=u,
                    options=build_job_options(u),
                    interaction=interaction,
                )
                self.pending_jobs.append(job)
                added_jobs.append(job)
            new_total = self.batch_total
            self.queue_cond.notify_all()
        return True, added_jobs, new_total

    def enqueue_from_preview(
        self,
        url: str,
        options: args.DownloadOptions,
        prefetched: Tuple[Source, Result],
        selected_indices: Optional[List[int]],
    ) -> Optional[Tuple[QueuedJob, int]]:
        """Append a previewed item to the active run if one exists; otherwise return `None`."""
        with self.queue_cond:
            if not self.worker_has_active_run:
                return None
            if self.interaction is None:
                self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
            self.batch_total += 1
            self.run_seen_urls.add(url)
            added_job = QueuedJob(
                job_id=self.make_job_id(),
                run_id=self.worker_run_id,
                url=url,
                options=options,
                interaction=self.interaction,
                selected_indices=selected_indices,
                prefetched=prefetched,
            )
            self.pending_jobs.append(added_job)
            new_total = self.batch_total
            self.queue_cond.notify_all()
            return added_job, new_total

    def remove_pending_job(self, job_id: str) -> Tuple[bool, Optional[int], bool]:
        """Remove a job from pending queue/records; returns `(removed, new_batch_total, is_active_running)`."""
        rec = self.queue_records.get(job_id)
        if rec is None:
            return False, None, False
        with self.queue_cond:
            pending_idx: Optional[int] = None
            for idx, job in enumerate(self.pending_jobs):
                if job is not None and job.job_id == job_id:
                    pending_idx = idx
                    break
            if pending_idx is not None:
                del self.pending_jobs[pending_idx]
                self.batch_total = max(0, self.batch_total - 1)
                self.run_seen_urls.discard(rec["url"])
                new_total: Optional[int] = self.batch_total
            elif rec["status"] == "Running":
                return False, None, True
            else:
                new_total = None
        self.queue_records.pop(job_id, None)
        return True, new_total, False

    def move_pending_job(self, job_id: str, direction: int) -> Optional[str]:
        """Swap `job_id` with its neighbor in `pending_jobs`; returns swapped neighbor `job_id`."""
        if direction not in (-1, 1):
            return None
        with self.queue_cond:
            pending_list = list(self.pending_jobs)
            pos = next(
                (i for i, job in enumerate(pending_list) if job is not None and job.job_id == job_id),
                None,
            )
            if pos is None:
                return None
            target = pos + direction
            if target < 0 or target >= len(pending_list) or pending_list[target] is None:
                return None
            neighbor = pending_list[target]
            assert neighbor is not None
            neighbor_id = neighbor.job_id
            pending_list[pos], pending_list[target] = pending_list[target], pending_list[pos]
            self.pending_jobs = deque(pending_list)
            return neighbor_id

    def retryable_urls(self, ordered_ids: Sequence[str]) -> List[str]:
        return [
            rec["url"]
            for iid in ordered_ids
            for rec in [self.queue_records.get(iid)]
            if rec is not None and rec["status"] in ("Failed", "Cancelled")
        ]

    def clear_completed_records(self, ordered_ids: Sequence[str]) -> List[str]:
        removable = [
            iid
            for iid in ordered_ids
            if self.queue_records.get(iid, {}).get("status") in ("Completed", "Failed", "Cancelled")
        ]
        for iid in removable:
            self.queue_records.pop(iid, None)
        return removable

    def cancel_active_run(self) -> None:
        self.cancel_event.set()
        if self.interaction is not None:
            self.interaction.release_all()

    def ensure_worker_started(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
            )
            self.worker.start()

    def _drain_pending_jobs(self, cancel_status: str = "Cancelled") -> bool:
        """Remove queued jobs after cancellation/error; return True if close sentinel seen."""
        saw_sentinel = False
        cancelled_ids: List[str] = []
        with self.queue_cond:
            while self.pending_jobs:
                pending = self.pending_jobs.popleft()
                if pending is None:
                    saw_sentinel = True
                    break
                cancelled_ids.append(pending.job_id)
            self.worker_has_active_run = False
        for job_id in cancelled_ids:
            self.event_queue.put(("job_status", job_id, cancel_status, ""))
        return saw_sentinel

    def _worker_loop(self) -> None:
        while True:
            with self.queue_cond:
                while not self.pending_jobs:
                    self.queue_cond.wait()
                first_job = self.pending_jobs.popleft()
            if first_job is None:
                break

            run_id = first_job.run_id
            batch_summary = DownloadSummary()
            fatal_exc: Optional[Exception] = None
            item_index = 0
            saw_sentinel = False
            current_job: Optional[QueuedJob] = first_job

            while current_job is not None:
                item_index += 1
                if self.cancel_event.is_set():
                    batch_summary.cancelled = True
                    self.event_queue.put(("job_status", current_job.job_id, "Cancelled", ""))
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    break

                with self.queue_lock:
                    item_total = self.batch_total

                self.event_queue.put(("batch_item", run_id, item_index, item_total, current_job.url))
                self.event_queue.put(("job_status", current_job.job_id, "Running", ""))

                try:
                    config = self.config_snapshot()
                    run_kwargs: Dict[str, Any] = {
                        "interaction": current_job.interaction,
                        "cancel_event": self.cancel_event,
                        "sink": self.sink,
                    }
                    if current_job.selected_indices is not None:
                        run_kwargs["selected_indices"] = current_job.selected_indices
                    if current_job.prefetched is not None:
                        run_kwargs["prefetched"] = current_job.prefetched
                    summary = run_downloads(
                        [current_job.url],
                        current_job.options,
                        config,
                        **run_kwargs,
                    )
                    batch_summary.completed += summary.completed
                    batch_summary.failed += summary.failed
                    batch_summary.books_completed += summary.books_completed
                    batch_summary.books_skipped += summary.books_skipped
                    batch_summary.books_failed += summary.books_failed
                    batch_summary.book_errors.extend(summary.book_errors)
                    if summary.cancelled:
                        batch_summary.cancelled = True
                        self.event_queue.put(("job_status", current_job.job_id, "Cancelled", ""))
                        saw_sentinel = self._drain_pending_jobs("Cancelled")
                        break
                    if summary.failed > 0 or summary.books_failed > 0:
                        detail = format_failed_job_detail(summary.book_errors)
                        self.event_queue.put(("job_status", current_job.job_id, "Failed", detail))
                    else:
                        detail = (
                            f"{summary.books_completed} ok"
                            if summary.books_completed
                            else ("Skipped" if summary.books_skipped else "Done")
                        )
                        self.event_queue.put(("job_status", current_job.job_id, "Completed", detail))
                except Exception as exc:
                    fatal_exc = exc
                    self.event_queue.put(("job_status", current_job.job_id, "Failed", str(exc)[:120]))
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    break

                with self.queue_cond:
                    if not self.pending_jobs:
                        self.worker_has_active_run = False
                        current_job = None
                    else:
                        next_job = self.pending_jobs.popleft()
                        if next_job is None:
                            self.worker_has_active_run = False
                            saw_sentinel = True
                            current_job = None
                        else:
                            current_job = next_job

            self.event_queue.put(("done", run_id, batch_summary, fatal_exc))
            if saw_sentinel:
                break

    def start_preview(self, url: str, options: args.DownloadOptions) -> None:
        """Fetch metadata and resolved `Result` for `url` on a background thread."""
        self.preview_cancel_event.clear()
        interaction = GuiInteractionHandler(self.event_queue, self.preview_cancel_event)
        self.preview_interaction = interaction

        def _worker() -> None:
            try:
                config = self.config_snapshot()
                ctx = logging.JobContext(
                    debug=options.debug,
                    quiet=options.quiet,
                    ffmpeg_output=options.ffmpeg_output or options.debug,
                    sink=self.sink,
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
                    )
                self.event_queue.put(("preview_done", url, (source, result), None))
            except Exception as exc:
                self.event_queue.put(("preview_done", url, None, exc))

        threading.Thread(target=_worker, daemon=True).start()

    def start_auth_verification(
        self,
        service_key: str,
        service_label: str,
        options: args.DownloadOptions,
    ) -> None:
        """Verify credentials for `service_key` on a background thread."""
        cfg = self.config_snapshot()
        interaction = GuiInteractionHandler(self.event_queue, threading.Event())

        def _worker() -> None:
            try:
                ok = verify_service_auth(service_key, options, cfg, interaction=interaction)
                self.event_queue.put(("auth_verified", service_label, ok, None))
            except Exception as exc:
                self.event_queue.put(("auth_verified", service_label, False, exc))

        threading.Thread(target=_worker, daemon=True).start()

    def start_catalog_fetch(
        self,
        mode: str,
        service_key: str,
        query_text: str,
        options: args.DownloadOptions,
        dialog: Any,
    ) -> int:
        """Search a service catalog or list its library on a background thread."""
        self.catalog_request_id += 1
        request_id = self.catalog_request_id
        cfg = self.config_snapshot()
        interaction = GuiInteractionHandler(self.event_queue, threading.Event())

        def _worker() -> None:
            try:
                if mode == "search":
                    books = search_service(
                        service_key,
                        query_text,
                        options,
                        cfg,
                        interaction=interaction,
                    )
                else:
                    books = list_service_library(
                        service_key,
                        options,
                        cfg,
                        interaction=interaction,
                    )
                self.event_queue.put(("catalog_loaded", request_id, dialog, books, None))
            except Exception as exc:
                self.event_queue.put(("catalog_loaded", request_id, dialog, [], exc))

        threading.Thread(target=_worker, daemon=True).start()
        return request_id

    def start_update_check(self, *, interactive: bool, respect_opt_out: bool) -> None:
        proxy = self.config.proxy
        session = self.get_http_session()

        def _worker() -> None:
            info = check_for_updates(proxy=proxy, session=session, respect_opt_out=respect_opt_out)
            if interactive or (info is not None and info.has_update):
                self.event_queue.put(("update_checked", info, interactive))

        threading.Thread(target=_worker, daemon=True).start()

    def start_winget_ffmpeg_install(self) -> None:
        def _worker() -> None:
            ok, detail = install_ffmpeg_via_winget()
            self.event_queue.put(("ffmpeg_install_done", ok, detail))

        threading.Thread(target=_worker, daemon=True).start()

    def search_enrichment_for_audiobook(
        self,
        audiobook: Audiobook,
        options: args.DownloadOptions,
    ) -> List[EnrichmentCandidate]:
        """Query configured metadata enrichment providers for `audiobook`."""
        raw_providers = getattr(options, "enrich_providers", None)
        providers = parse_provider_list(raw_providers) if raw_providers else None
        return search_enrichment_candidates(
            audiobook.metadata,
            providers=providers,
            session=self.get_http_session(),
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
            database_directory=getattr(options, "database_directory", None),
            use_cache=not bool(getattr(options, "no_enrich_cache", False)),
        )

    def apply_enrichment_candidate(
        self,
        audiobook: Audiobook,
        candidate: EnrichmentCandidate,
        options: args.DownloadOptions,
    ) -> Tuple[List[str], bool, bool]:
        """Apply `candidate` onto `audiobook` using controller session and cache."""
        db_dir = getattr(options, "database_directory", None)
        use_cache = not bool(getattr(options, "no_enrich_cache", False))
        include_chapters = not bool(getattr(options, "no_chapters", False))
        if db_dir:
            with EnrichmentCache(default_enrichment_cache_path(db_dir), persistent=True) as cache:
                return apply_candidate_to_audiobook(
                    audiobook,
                    candidate,
                    session=self.get_http_session(),
                    proxy=getattr(options, "proxy", None),
                    timeout=getattr(options, "timeout", None) or 10.0,
                    cache=cache,
                    use_cache=use_cache,
                    include_chapters=include_chapters,
                )
        return apply_candidate_to_audiobook(
            audiobook,
            candidate,
            session=self.get_http_session(),
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
            use_cache=use_cache,
            include_chapters=include_chapters,
        )

    def drain_events(
        self,
        handler: Callable[[tuple], None],
        max_batch: int = MAX_QUEUE_BATCH,
    ) -> int:
        """Drain up to `max_batch` events, coalescing consecutive progress updates."""
        pending_progress: Optional[tuple] = None
        pending_metrics: Optional[tuple] = None
        processed = 0
        while processed < max_batch:
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if item[0] == "progress_advance":
                pending_progress = item
                continue
            if item[0] == "progress_metrics":
                pending_metrics = item
                continue
            if pending_progress is not None:
                handler(pending_progress)
                pending_progress = None
            if pending_metrics is not None:
                handler(pending_metrics)
                pending_metrics = None
            handler(item)
        if pending_progress is not None:
            handler(pending_progress)
        if pending_metrics is not None:
            handler(pending_metrics)
        return processed

    def shutdown(self, *, is_running: bool, is_previewing: bool) -> None:
        """Cancel active operations, stop the worker thread, and close HTTP sessions."""
        if is_running:
            self.cancel_active_run()
        if is_previewing:
            self.preview_cancel_event.set()
            if self.preview_interaction is not None:
                self.preview_interaction.release_all()
        with self.queue_cond:
            self.pending_jobs.append(None)
            self.queue_cond.notify_all()
        if self._http_session is not None:
            try:
                self._http_session.close()
            except Exception:
                pass

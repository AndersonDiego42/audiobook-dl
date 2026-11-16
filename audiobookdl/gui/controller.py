"""State, queue, background worker, and event-bridge controller for the desktop GUI."""

from collections import deque
from dataclasses import dataclass
import json
import os
import queue
import threading
from typing import Any
from collections.abc import Callable, Mapping, Sequence
import uuid

from audiobookdl import args, logging
from audiobookdl.config import (
    Config,
    config_dir,
    load_config,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.utils.fs import atomic_write_text
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.formatting import format_failed_job_detail
from audiobookdl.options_schema import PERSISTED_GUI_OPTION_SPECS
from audiobookdl.pack import (
    PackBookSpec,
    PackOverrides,
    discover_pack_groups,
    inspect_local_book,
    run_pack,
    scope_overrides_for_batch,
)
from audiobookdl.pipeline import (
    DownloadSummary,
    list_service_library,
    prepare_source_result,
    run_downloads,
    search_service,
    verify_service_auth,
)
from audiobookdl.sources.source import Source
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
class PackJobRequest:
    """Payload describing a local audiobook repackaging job."""

    paths: list[str]
    recursive: bool = False
    delete_source: bool = False
    output_dir: str | None = None
    overrides: PackOverrides | None = None
    label: str = ""


@dataclass
class QueuedJob:
    """Single queued URL download or local pack job consumed by the persistent GUI worker."""

    job_id: str
    run_id: int
    url: str
    options: args.DownloadOptions
    interaction: "GuiInteractionHandler"
    selected_indices: list[int] | None = None
    prefetched: tuple[Source, Result] | None = None
    pack_request: PackJobRequest | None = None


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
        eta_seconds: float | None,
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
        self._pending_events: list[threading.Event] = []
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
        default: int | None = None,
    ) -> int | None:
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

    def __init__(self, config_location: str | None = None) -> None:
        self.config_location = config_location
        self.config = self.load_saved_config()

        self.event_queue: queue.Queue[tuple] = queue.Queue()
        self.sink = GuiLogSink(self.event_queue)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.preview_cancel_event = threading.Event()
        self.interaction: GuiInteractionHandler | None = None
        self.preview_interaction: GuiInteractionHandler | None = None

        self.pending_jobs: deque[QueuedJob | None] = deque()
        self.queue_lock = threading.Lock()
        self.queue_cond = threading.Condition(self.queue_lock)
        self.worker: threading.Thread | None = None
        self.worker_has_active_run = False
        self.worker_run_id = 0
        self.batch_total = 0
        self.run_seen_urls: set[str] = set()
        self.queue_records: dict[str, dict[str, Any]] = {}
        self.catalog_request_id = 0
        self.preview_request_id = 0
        self._http_session: Any | None = None

    @staticmethod
    def make_job_id() -> str:
        return uuid.uuid4().hex[:12]

    def load_saved_config(self) -> Config:
        """Load configuration from `config_location` (or default)."""
        if self.config_location and not os.path.exists(self.config_location):
            return Config(
                sources={},
                output_template=None,
                database_directory=None,
                skip_downloaded=None,
            )
        return load_config(self.config_location)

    def reload_config(self) -> Config:
        loaded = self.load_saved_config()
        with self.queue_lock:
            self.config = loaded
        return loaded

    def config_snapshot(self) -> Config:
        """Return a thread-safe isolated copy of the current cached configuration."""
        with self.queue_lock:
            return self.config.clone()

    def save_ffmpeg_dir(self, resolved_dir: str | None) -> Config:
        save_ffmpeg_dir(resolved_dir, overwrite=self.config_location)
        return self.reload_config()

    def save_source_credentials(
        self,
        service_key: str,
        *,
        username: str | None = None,
        password: str | None = None,
        library: str | None = None,
        cookie_file: str | None = None,
        profile: str | None = None,
    ) -> Config:
        save_source_credentials(
            service_key,
            username=username,
            password=password,
            library=library,
            cookie_file=cookie_file,
            overwrite=self.config_location,
            profile=profile,
        )
        return self.reload_config()

    def save_run_preferences(
        self,
        options: args.DownloadOptions,
        *,
        output_dir: str,
        output_template: str,
        use_download_archive: bool | None = None,
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
        window_geometry: str | None,
        output_dir: str | None,
        output_template: str | None,
        use_download_archive: bool | None = None,
        theme: str | None = None,
        notify_on_complete: bool | None = None,
        output_prefs: Mapping[str, Any] | None = None,
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

    def queue_state_path(self) -> str:
        """Return path to `gui_queue.json` beside `config_location` (or default config dir)."""
        base_dir = (
            os.path.dirname(os.path.abspath(self.config_location))
            if self.config_location
            else config_dir()
        )
        return os.path.join(base_dir, "db", "gui_queue.json")

    def save_queue_state(self) -> None:
        """Persist non-completed download queue entries to `gui_queue.json` atomically."""
        persistable: list[dict[str, str]] = []
        with self.queue_lock:
            records = list(self.queue_records.values())
        for rec in records:
            if rec.get("job_kind", "download") != "download":
                continue
            status = str(rec.get("status") or "")
            if status == "Completed":
                continue
            saved_status = "Interrupted" if status in ("Queued", "Running", "Paused") else status
            detail = (
                "Interrupted when closed"
                if saved_status == "Interrupted" and not rec.get("detail")
                else str(rec.get("detail") or "")
            )
            persistable.append(
                {
                    "url": str(rec.get("url") or ""),
                    "service": str(rec.get("service") or "Unknown"),
                    "status": saved_status,
                    "detail": detail,
                }
            )
        target = self.queue_state_path()
        try:
            if not persistable:
                if os.path.exists(target):
                    os.remove(target)
                return
            atomic_write_text(
                target,
                json.dumps(persistable, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def load_saved_queue_records(self) -> list[dict[str, Any]]:
        """Load persisted unresolved queue items from `gui_queue.json`."""
        target = self.queue_state_path()
        if not os.path.isfile(target):
            return []
        try:
            with open(target, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        restored: list[dict[str, Any]] = []
        with self.queue_lock:
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                job_id = self.make_job_id()
                rec = {
                    "job_id": job_id,
                    "run_id": 0,
                    "url": url,
                    "status": str(item.get("status") or "Interrupted"),
                    "detail": str(item.get("detail") or ""),
                    "service": str(item.get("service") or "Unknown"),
                    "job_kind": "download",
                }
                self.queue_records[job_id] = rec
                restored.append(rec)
        return restored

    def is_paused(self) -> bool:
        """Return `True` when the queue worker is paused between jobs."""
        return not self.pause_event.is_set()

    def pause_queue(self) -> None:
        """Pause the queue before starting the next queued job."""
        self.pause_event.clear()

    def resume_queue(self) -> None:
        """Resume queue processing after a pause."""
        self.pause_event.set()
        with self.queue_cond:
            self.queue_cond.notify_all()

    def toggle_pause(self) -> bool:
        """Toggle queue pause state and return the new `is_paused()` state."""
        if self.is_paused():
            self.resume_queue()
            return False
        self.pause_queue()
        return True

    def register_queue_record(self, job: QueuedJob, service_label: str) -> dict[str, Any]:
        rec = {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "url": job.url,
            "status": "Queued",
            "detail": "",
            "service": service_label,
            "job_kind": "pack" if job.pack_request is not None else "download",
        }
        with self.queue_lock:
            self.queue_records[job.job_id] = rec
        self.save_queue_state()
        return rec

    def update_queue_record(self, job_id: str, status: str, detail: str = "") -> dict[str, Any] | None:
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            rec["status"] = status
            rec["detail"] = detail
        self.save_queue_state()
        return rec

    def clear_seen_urls(self) -> None:
        with self.queue_lock:
            self.run_seen_urls.clear()

    def start_new_run(
        self,
        job_specs: Sequence[tuple[str, args.DownloadOptions, list[int] | None, tuple[Source, Result] | None]],
    ) -> tuple[int, list[QueuedJob]]:
        """Initialize queue state for a fresh download run and ensure the worker is running."""
        self.cancel_event.clear()
        self.pause_event.set()
        self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)

        queued_jobs: list[QueuedJob] = []
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
    ) -> tuple[bool, list[QueuedJob], int]:
        """Append unseen URLs to the active queue; returns `(has_active_run, added_jobs, batch_total)`."""
        unique_urls = list(dict.fromkeys(urls))
        added_jobs: list[QueuedJob] = []
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
        prefetched: tuple[Source, Result],
        selected_indices: list[int] | None,
    ) -> tuple[QueuedJob, int] | None:
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

    def start_or_enqueue_pack_jobs(
        self,
        requests_with_options: Sequence[tuple[PackJobRequest, args.DownloadOptions]],
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue local pack jobs on the active run or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        queued_jobs: list[QueuedJob] = []
        with self.queue_cond:
            was_appended = self.worker_has_active_run
            if not was_appended:
                self.cancel_event.clear()
                self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
                self.worker_run_id += 1
                self.worker_has_active_run = True
                self.batch_total = len(requests_with_options)
                self.run_seen_urls.clear()
                self.sink.set_active_run_id(self.worker_run_id)
            else:
                if self.interaction is None:
                    self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
                self.batch_total += len(requests_with_options)
            run_id = self.worker_run_id
            interaction = self.interaction
            for req, opts in requests_with_options:
                display = req.label or (req.paths[0] if req.paths else "Local folder")
                job = QueuedJob(
                    job_id=self.make_job_id(),
                    run_id=run_id,
                    url=display,
                    options=opts,
                    interaction=interaction,
                    pack_request=req,
                )
                self.pending_jobs.append(job)
                queued_jobs.append(job)
            new_total = self.batch_total
            self.queue_cond.notify_all()

        self.ensure_worker_started()
        return was_appended, run_id, queued_jobs, new_total

    def inspect_local_pack_preview(
        self,
        paths: Sequence[str],
        options: args.DownloadOptions,
        *,
        recursive: bool = False,
        output_dir: str | None = None,
        overrides: PackOverrides | None = None,
    ) -> list[PackBookSpec]:
        """Discover and inspect local audiobook specs cleanly without modifying files."""
        groups = discover_pack_groups(paths, recursive=recursive)
        total = len(groups)
        specs: list[PackBookSpec] = []
        probe_cache: dict[str, Any] = {}
        session = self.get_http_session()
        book_overrides = scope_overrides_for_batch(overrides, total)
        for source_root, raw_tracks in groups:
            specs.append(
                inspect_local_book(
                    source_root,
                    raw_tracks,
                    options,
                    output_dir=output_dir,
                    overrides=book_overrides,
                    probe_cache=probe_cache,
                    session=session,
                )
            )
        return specs

    def remove_pending_job(self, job_id: str) -> tuple[bool, int | None, bool]:
        """Remove a job from pending queue/records; returns `(removed, new_batch_total, is_active_running)`."""
        rec = self.queue_records.get(job_id)
        if rec is None:
            return False, None, False
        with self.queue_cond:
            pending_idx: int | None = None
            for idx, job in enumerate(self.pending_jobs):
                if job is not None and job.job_id == job_id:
                    pending_idx = idx
                    break
            if pending_idx is not None:
                del self.pending_jobs[pending_idx]
                self.batch_total = max(0, self.batch_total - 1)
                self.run_seen_urls.discard(rec["url"])
                new_total: int | None = self.batch_total
            elif rec["status"] == "Running":
                return False, None, True
            else:
                new_total = None
        with self.queue_lock:
            self.queue_records.pop(job_id, None)
        self.save_queue_state()
        return True, new_total, False

    def move_pending_job(self, job_id: str, direction: int) -> str | None:
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

    def retryable_urls(self, ordered_ids: Sequence[str]) -> list[str]:
        return [
            rec["url"]
            for iid in ordered_ids
            for rec in [self.queue_records.get(iid)]
            if rec is not None
            and rec.get("job_kind", "download") == "download"
            and rec["status"] in ("Failed", "Cancelled", "Interrupted")
        ]

    def clear_completed_records(self, ordered_ids: Sequence[str]) -> list[str]:
        with self.queue_lock:
            removable = [
                iid
                for iid in ordered_ids
                if self.queue_records.get(iid, {}).get("status")
                in ("Completed", "Failed", "Cancelled", "Interrupted")
            ]
            for iid in removable:
                self.queue_records.pop(iid, None)
        self.save_queue_state()
        return removable

    def cancel_active_run(self) -> None:
        self.cancel_event.set()
        self.pause_event.set()
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
        cancelled_ids: list[str] = []
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
            fatal_exc: Exception | None = None
            item_index = 0
            saw_sentinel = False
            current_job: QueuedJob | None = first_job

            while current_job is not None:
                if self.is_paused() and not self.cancel_event.is_set():
                    self.event_queue.put(("job_status", current_job.job_id, "Paused", "Queue paused"))
                    while not self.pause_event.wait(0.2):
                        if self.cancel_event.is_set():
                            break
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
                    if current_job.pack_request is not None:
                        req = current_job.pack_request
                        pack_summary = run_pack(
                            req.paths,
                            current_job.options,
                            config,
                            output_dir=req.output_dir,
                            recursive=req.recursive,
                            delete_source=req.delete_source,
                            overrides=req.overrides,
                            interaction=current_job.interaction,
                            cancel_event=self.cancel_event,
                            sink=self.sink,
                        )
                        summary = DownloadSummary(
                            completed=1 if pack_summary.books_completed and pack_summary.books_failed == 0 else 0,
                            failed=1 if pack_summary.books_failed > 0 else 0,
                            cancelled=pack_summary.cancelled,
                            books_completed=pack_summary.books_completed,
                            books_skipped=pack_summary.books_skipped,
                            books_failed=pack_summary.books_failed,
                            book_errors=list(pack_summary.book_errors),
                        )
                    else:
                        run_kwargs: dict[str, Any] = {
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

    def cancel_preview(self) -> None:
        """Cancel any active metadata preview and unblock pending interaction prompts."""
        self.preview_cancel_event.set()
        if self.preview_interaction is not None:
            self.preview_interaction.release_all()

    def start_preview(self, url: str, options: args.DownloadOptions) -> int:
        """Fetch metadata and resolved `Result` for `url` on a background thread."""
        self.preview_request_id += 1
        request_id = self.preview_request_id
        self.preview_cancel_event.clear()
        interaction = GuiInteractionHandler(self.event_queue, self.preview_cancel_event)
        self.preview_interaction = interaction

        def _worker() -> None:
            try:
                config = self.config_snapshot()
                ffmpeg_dir = config.ffmpeg_dir.strip() or None if config.ffmpeg_dir else None
                ctx = logging.JobContext(
                    debug=options.debug,
                    quiet=options.quiet,
                    ffmpeg_output=options.ffmpeg_output or options.debug,
                    ffmpeg_dir=ffmpeg_dir,
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
                        session=self.get_http_session(),
                    )
                if self.preview_cancel_event.is_set() or request_id != self.preview_request_id:
                    close_fn = getattr(source, "close", None)
                    if callable(close_fn):
                        close_fn()
                    raise DownloadCancelled()
                self.event_queue.put(("preview_done", url, (source, result), None))
            except Exception as exc:
                if request_id == self.preview_request_id:
                    self.event_queue.put(("preview_done", url, None, exc))

        threading.Thread(target=_worker, daemon=True).start()
        return request_id

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
    ) -> list[EnrichmentCandidate]:
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
    ) -> tuple[list[str], bool, bool]:
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
        pending_progress: tuple | None = None
        pending_metrics: tuple | None = None
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
        """Cancel active operations, persist unresolved queue state, stop worker, and close HTTP."""
        self.save_queue_state()
        self.pause_event.set()
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

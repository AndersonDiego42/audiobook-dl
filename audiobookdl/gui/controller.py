"""State, queue, background worker, and event-bridge controller for the desktop GUI."""

from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import queue
import threading
from typing import Any
import uuid

from audiobookdl import logging
from audiobookdl.options_schema import PERSISTED_GUI_OPTION_SPECS, DownloadOptions, SyncOptions
from audiobookdl.config import (
    Config,
    config_dir,
    load_config,
    save_check_updates,
    save_ffmpeg_dir,
    save_gui_preferences,
    save_output_preferences,
    save_source_credentials,
)
from audiobookdl.utils.fs import atomic_write_text
from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.events import (
    AbsUploadDoneEvent,
    AuthVerifiedEvent,
    BatchItemEvent,
    CatalogLoadedEvent,
    ControllerEvent,
    FfmpegInstallDoneEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogBatchEvent,
    LogEvent,
    PreviewDoneEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
    PromptChoiceEvent,
    PromptConfirmEvent,
    PromptInputEvent,
    RunDoneEvent,
    SyncDoneEvent,
    SyncEnqueueReadyEvent,
    UpdateCheckedEvent,
)
from audiobookdl.gui.formatting import format_failed_job_detail
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
from audiobookdl.utils.audiobook import Audiobook, Chapter, Result
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
    output_format: str | None = None

    def to_dict(self, *, output_format: str | None = None) -> dict[str, Any]:
        """Serialize this pack request into a JSON-compatible dictionary."""
        ov_dict: dict[str, Any] | None = None
        if self.overrides is not None:
            ov = self.overrides
            ov_dict = {
                "title": ov.title,
                "authors": list(ov.authors),
                "narrators": list(ov.narrators),
                "series": ov.series,
                "series_order": ov.series_order,
                "isbn": ov.isbn,
                "asin": ov.asin,
                "cover_path": ov.cover_path,
                "genres": list(ov.genres),
                "publisher": ov.publisher,
                "release_date": ov.release_date,
                "language": ov.language,
                "description": ov.description,
                "chapters": (
                    [{"start": ch.start, "title": ch.title} for ch in ov.chapters]
                    if ov.chapters is not None
                    else None
                ),
            }
        fmt = output_format if output_format is not None else self.output_format
        return {
            "paths": list(self.paths),
            "recursive": self.recursive,
            "delete_source": self.delete_source,
            "output_dir": self.output_dir,
            "label": self.label,
            "output_format": fmt,
            "overrides": ov_dict,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PackJobRequest | None":
        """Reconstruct a `PackJobRequest` from a persisted dictionary, or return `None`."""
        if not isinstance(raw, Mapping):
            return None
        raw_paths = raw.get("paths")
        if not isinstance(raw_paths, list):
            return None
        paths = [p.strip() for p in raw_paths if isinstance(p, str) and p.strip()]
        if not paths:
            return None
        overrides: PackOverrides | None = None
        raw_ov = raw.get("overrides")
        if isinstance(raw_ov, Mapping):
            authors = [str(a).strip() for a in (raw_ov.get("authors") or []) if str(a).strip()]
            narrators = [str(n).strip() for n in (raw_ov.get("narrators") or []) if str(n).strip()]
            genres = [str(g).strip() for g in (raw_ov.get("genres") or []) if str(g).strip()]
            raw_order = raw_ov.get("series_order")
            series_order = raw_order if isinstance(raw_order, int) and not isinstance(raw_order, bool) else None
            raw_ch = raw_ov.get("chapters")
            parsed_chapters: list[Chapter] | None = None
            if isinstance(raw_ch, list):
                parsed_chapters = [
                    Chapter(start=int(c["start"]), title=str(c["title"]))
                    for c in raw_ch
                    if isinstance(c, Mapping) and "start" in c and "title" in c
                ]
            cand = PackOverrides(
                title=str(raw_ov["title"]).strip() if raw_ov.get("title") else None,
                authors=authors,
                narrators=narrators,
                series=str(raw_ov["series"]).strip() if raw_ov.get("series") else None,
                series_order=series_order,
                isbn=str(raw_ov["isbn"]).strip() if raw_ov.get("isbn") else None,
                asin=str(raw_ov["asin"]).strip() if raw_ov.get("asin") else None,
                cover_path=str(raw_ov["cover_path"]).strip() if raw_ov.get("cover_path") else None,
                genres=genres,
                publisher=str(raw_ov["publisher"]).strip() if raw_ov.get("publisher") else None,
                release_date=str(raw_ov["release_date"]).strip() if raw_ov.get("release_date") else None,
                language=str(raw_ov["language"]).strip() if raw_ov.get("language") else None,
                description=str(raw_ov["description"]).strip() if raw_ov.get("description") else None,
                chapters=parsed_chapters,
            )
            if any((
                cand.title,
                cand.authors,
                cand.narrators,
                cand.series is not None,
                cand.series_order is not None,
                cand.isbn is not None,
                cand.asin is not None,
                cand.cover_path,
                cand.genres,
                cand.publisher,
                cand.release_date,
                cand.language,
                cand.description,
                cand.chapters is not None,
            )):
                overrides = cand
        raw_out_dir = raw.get("output_dir")
        output_dir = raw_out_dir.strip() if isinstance(raw_out_dir, str) and raw_out_dir.strip() else None
        raw_fmt = raw.get("output_format")
        output_format = raw_fmt.strip().lower() if isinstance(raw_fmt, str) and raw_fmt.strip() else None
        return cls(
            paths=paths,
            recursive=bool(raw.get("recursive", False)),
            delete_source=bool(raw.get("delete_source", False)),
            output_dir=output_dir,
            overrides=overrides,
            label=str(raw.get("label") or ""),
            output_format=output_format,
        )


@dataclass
class QueuedJob:
    """Single queued URL download or local pack job consumed by the persistent GUI worker."""

    job_id: str
    run_id: int
    url: str
    options: DownloadOptions
    interaction: "GuiInteractionHandler"
    selected_indices: list[int] | None = None
    prefetched: tuple[Source, Result] | None = None
    pack_request: PackJobRequest | None = None


class GuiLogSink:
    """Forwards core log and progress events onto the Tkinter event queue."""

    def __init__(self, event_queue: "queue.Queue[ControllerEvent]") -> None:
        self._queue = event_queue
        self._active_run_id = 0
        self._active_job_id: str | None = None
        self._active_series_title: str = ""
        self._last_emitted_title: str = ""

    def set_active_run_id(self, run_id: int) -> None:
        self._active_run_id = run_id

    def set_active_job_id(self, job_id: str | None) -> None:
        """Bind `job_id` so `item_progress` updates also resolve the active queue row title."""
        self._active_job_id = job_id
        self._active_series_title = ""
        self._last_emitted_title = ""

    def on_log(self, level: str, message: str) -> None:
        self._queue.put(LogEvent(level=level, message=message))

    def on_progress_start(self, description: str, total: float) -> None:
        self._queue.put(ProgressStartEvent(description=description, total=total))

    def on_progress_advance(self, completed: float, total: float) -> None:
        self._queue.put(ProgressAdvanceEvent(completed=completed, total=total))

    def on_progress_metrics(
        self,
        completed: float,
        total: float,
        bytes_downloaded: int,
        speed_bps: float,
        elapsed_seconds: float,
        eta_seconds: float | None,
    ) -> None:
        self._queue.put(
            ProgressMetricsEvent(
                completed=completed,
                total=total,
                bytes_downloaded=bytes_downloaded,
                speed_bps=speed_bps,
                elapsed_seconds=elapsed_seconds,
                eta_seconds=eta_seconds,
            )
        )

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        self._queue.put(
            ItemProgressEvent(
                run_id=self._active_run_id,
                current=current,
                total=total,
                title=title,
            )
        )
        cleaned = (title or "").strip()
        if not self._active_job_id or not cleaned:
            return
        if total > 1 and current == 0:
            self._active_series_title = cleaned
            display_title = cleaned
        elif total > 1 and current > 0:
            if self._active_series_title and self._active_series_title != cleaned:
                display_title = f"{self._active_series_title} ({current}/{total})"
            else:
                display_title = f"{cleaned} ({current}/{total})"
        else:
            display_title = cleaned
        if display_title != self._last_emitted_title:
            self._last_emitted_title = display_title
            self._queue.put(JobMetaEvent(job_id=self._active_job_id, title=display_title))

    def on_progress_finish(self) -> None:
        self._queue.put(ProgressFinishEvent())


class GuiInteractionHandler:
    """Bridges worker-thread prompts to Tkinter modal dialogs on the main thread."""

    def __init__(
        self,
        event_queue: "queue.Queue[ControllerEvent]",
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
        self._queue.put(PromptInputEvent(prompt=prompt, hidden=hidden, holder=holder, done=done))
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
        self._queue.put(
            PromptConfirmEvent(message=logging.strip_markup(message), holder=holder, done=done)
        )
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
        self._queue.put(
            PromptChoiceEvent(
                title=title,
                choices=tuple(choices),
                default_index=default,
                holder=holder,
                done=done,
            )
        )
        done.wait()
        with self._lock:
            if done in self._pending_events:
                self._pending_events.remove(done)
        val = holder["value"]
        return val if isinstance(val, int) and not isinstance(val, bool) and 0 <= val < len(choices) else None

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

        self.event_queue: queue.Queue[ControllerEvent] = queue.Queue()
        self.sink = GuiLogSink(self.event_queue)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.preview_cancel_event = threading.Event()
        self.interaction: GuiInteractionHandler | None = None
        self.preview_interaction: GuiInteractionHandler | None = None
        self.sync_cancel_event: threading.Event | None = None
        self.sync_interaction: GuiInteractionHandler | None = None
        self.catalog_cancel_event: threading.Event | None = None
        self.catalog_interaction: GuiInteractionHandler | None = None
        self._aux_interactions_lock = threading.Lock()
        self._aux_interactions: set[GuiInteractionHandler] = set()

        self.pending_jobs: deque[QueuedJob | None] = deque()
        self.queue_lock = threading.Lock()
        self.queue_cond = threading.Condition(self.queue_lock)
        self.worker: threading.Thread | None = None
        self.worker_has_active_run = False
        self.worker_run_id = 0
        self.batch_total = 0
        self.run_seen_urls: set[str] = set()
        self.queue_records: dict[str, dict[str, Any]] = {}
        self._saved_queue_loaded = False
        self.catalog_request_id = 0
        self.preview_request_id = 0
        self.active_job_id: str | None = None
        self.active_job_running = False
        self.cancelled_job_ids: set[str] = set()
        self._save_defer_depth = 0
        self._save_deferred = False
        self._thread_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._thread_sessions: list[Any] = []

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
        options: DownloadOptions,
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
        output_prefs: Mapping[str, Any] | None = None,
    ) -> None:
        if output_prefs:
            save_output_preferences(config_location=self.config_location, **output_prefs)
        save_gui_preferences(
            window_geometry=window_geometry,
            window_maximized=window_maximized,
            download_split_ratio=download_split_ratio,
            active_page=active_page,
            options_collapsed=options_collapsed,
            output_naming_collapsed=output_naming_collapsed,
            output_dir=output_dir,
            output_template=output_template,
            overwrite=self.config_location,
            use_download_archive=use_download_archive,
            theme=theme,
            notify_on_complete=notify_on_complete,
            clipboard_monitor=clipboard_monitor,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
        )

    def save_gui_preferences(
        self,
        *,
        clipboard_monitor: bool | None = None,
        auto_sync_enabled: bool | None = None,
        auto_sync_minutes: int | None = None,
        auto_sync_mode: str | None = None,
        auto_sync_targets: Sequence[str] | None = None,
    ) -> Config:
        """Persist incremental `[gui]` preferences and reload `self.config`."""
        save_gui_preferences(
            overwrite=self.config_location,
            clipboard_monitor=clipboard_monitor,
            auto_sync_enabled=auto_sync_enabled,
            auto_sync_minutes=auto_sync_minutes,
            auto_sync_mode=auto_sync_mode,
            auto_sync_targets=auto_sync_targets,
        )
        return self.reload_config()

    def set_check_updates(self, enabled: bool) -> Config:
        """Persist `check_updates` in `audiobook-dl.toml` and reload `self.config`."""
        save_check_updates(enabled, overwrite=self.config_location)
        return self.reload_config()

    def get_http_session(self) -> Any:
        """Return a per-thread pooled `requests.Session` closed on controller shutdown."""
        session = getattr(self._thread_local, "http_session", None)
        if session is None:
            import requests

            session = requests.Session()
            self._thread_local.http_session = session
            with self._sessions_lock:
                self._thread_sessions.append(session)
        return session

    def fetch_cover_bytes(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout: float | None = None,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> bytes | None:
        """Download cover image bytes via pooled thread-local session, respecting proxy, timeout, and size cap."""
        if not url or not url.lower().startswith(("http://", "https://")):
            return None
        cfg = self.config_snapshot()
        eff_proxy = (proxy.strip() if isinstance(proxy, str) else "") or (cfg.proxy or "").strip() or None
        proxies = {"http": eff_proxy, "https": eff_proxy} if eff_proxy else None
        eff_timeout = (
            float(timeout)
            if timeout is not None and float(timeout) > 0
            else (float(cfg.timeout) if cfg.timeout and cfg.timeout > 0 else 10.0)
        )
        session = self.get_http_session()
        try:
            with session.get(
                url,
                timeout=eff_timeout,
                proxies=proxies,
                headers={"User-Agent": "audiobook-dl"},
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                raw_len = resp.headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        if int(str(raw_len).strip()) > max_bytes:
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data if data else None
        except Exception:
            return None

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

    def queue_state_path(self) -> str:
        """Return path to `gui_queue.json` beside `config_location` (or default config dir)."""
        base_dir = (
            os.path.dirname(os.path.abspath(self.config_location))
            if self.config_location
            else config_dir()
        )
        return os.path.join(base_dir, "db", "gui_queue.json")

    def save_queue_state(self) -> None:
        """Persist non-completed download and pack queue entries to `gui_queue.json` atomically."""
        persistable: list[dict[str, Any]] = []
        with self.queue_lock:
            records = list(self.queue_records.values())
        for rec in records:
            job_kind = str(rec.get("job_kind") or "download")
            if job_kind not in ("download", "pack"):
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
            entry: dict[str, Any] = {
                "job_kind": job_kind,
                "url": str(rec.get("url") or ""),
                "service": str(rec.get("service") or "Unknown"),
                "status": saved_status,
                "detail": detail,
            }
            title = str(rec.get("title") or "").strip()
            if title:
                entry["title"] = title
            output_path = str(rec.get("output_path") or "").strip()
            if output_path:
                entry["output_path"] = output_path
            profile = str(rec.get("profile") or "").strip()
            if profile:
                entry["profile"] = profile
            selected_indices = rec.get("selected_indices")
            if isinstance(selected_indices, list):
                entry["selected_indices"] = [i for i in selected_indices if isinstance(i, int)]
            job_overrides = rec.get("job_overrides")
            if isinstance(job_overrides, dict) and job_overrides:
                entry["job_overrides"] = dict(job_overrides)
            if job_kind == "pack":
                pack_payload = rec.get("pack_request")
                if not isinstance(pack_payload, dict):
                    continue
                entry["pack_request"] = pack_payload
            persistable.append(entry)
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
        """Load persisted unresolved download and pack queue items from `gui_queue.json` once."""
        with self.queue_lock:
            if self._saved_queue_loaded:
                return [
                    rec
                    for rec in self.queue_records.values()
                    if rec.get("run_id") == 0
                ]
        target = self.queue_state_path()
        raw: Any = None
        if os.path.isfile(target):
            try:
                with open(target, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = None
        restored: list[dict[str, Any]] = []
        with self.queue_lock:
            if self._saved_queue_loaded:
                return [
                    rec
                    for rec in self.queue_records.values()
                    if rec.get("run_id") == 0
                ]
            self._saved_queue_loaded = True
            if not isinstance(raw, list):
                return []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                job_kind = str(item.get("job_kind") or "download").strip().lower()
                if job_kind not in ("download", "pack"):
                    continue
                pack_dict: dict[str, Any] | None = None
                if job_kind == "pack":
                    parsed_pack = PackJobRequest.from_dict(item.get("pack_request"))
                    if parsed_pack is None:
                        continue
                    pack_dict = parsed_pack.to_dict()
                    url = str(item.get("url") or "").strip() or parsed_pack.label or parsed_pack.paths[0]
                    service = str(item.get("service") or "Local Pack")
                    title = str(item.get("title") or parsed_pack.label or "").strip()
                else:
                    url = str(item.get("url") or "").strip()
                    if not url:
                        continue
                    service = str(item.get("service") or "Unknown")
                    title = str(item.get("title") or "").strip()
                job_id = self.make_job_id()
                rec: dict[str, Any] = {
                    "job_id": job_id,
                    "run_id": 0,
                    "url": url,
                    "title": title,
                    "output_path": str(item.get("output_path") or "").strip(),
                    "status": str(item.get("status") or "Interrupted"),
                    "detail": str(item.get("detail") or ""),
                    "service": service,
                    "job_kind": job_kind,
                }
                profile = str(item.get("profile") or "").strip()
                if profile:
                    rec["profile"] = profile
                raw_indices = item.get("selected_indices")
                if isinstance(raw_indices, list):
                    rec["selected_indices"] = [
                        i for i in raw_indices if isinstance(i, int) and not isinstance(i, bool)
                    ]
                raw_overrides = item.get("job_overrides")
                if isinstance(raw_overrides, dict) and raw_overrides:
                    rec["job_overrides"] = dict(raw_overrides)
                if pack_dict is not None:
                    rec["pack_request"] = pack_dict
                self.queue_records[job_id] = rec
                restored.append(rec)
        return restored

    def is_paused(self) -> bool:
        """Return `True` when the queue worker is paused between jobs."""
        return not self.pause_event.is_set()

    def pause_queue(self) -> None:
        """Pause the queue cooperatively (mid-job and before upcoming jobs)."""
        self.pause_event.clear()
        with self.queue_cond:
            active_id = self.active_job_id if self.active_job_running else None
        if active_id is not None:
            self.event_queue.put(JobStatusEvent(job_id=active_id, status="Paused", detail="Paused"))

    def resume_queue(self) -> None:
        """Resume queue processing after a pause."""
        self.pause_event.set()
        with self.queue_cond:
            active_id = self.active_job_id if self.active_job_running else None
            self.queue_cond.notify_all()
        if active_id is not None:
            self.event_queue.put(JobStatusEvent(job_id=active_id, status="Running", detail=""))

    def toggle_pause(self) -> bool:
        """Toggle queue pause state and return the new `is_paused()` state."""
        if self.is_paused():
            self.resume_queue()
            return False
        self.pause_queue()
        return True

    def _request_save_queue_state(self) -> None:
        """Persist immediately, or coalesce until the active `drain_events` batch finishes."""
        if self._save_defer_depth > 0:
            self._save_deferred = True
            return
        self.save_queue_state()

    def register_queue_record(
        self,
        job: QueuedJob,
        service_label: str,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        is_pack = job.pack_request is not None
        initial_title = ""
        if job.prefetched is not None:
            initial_title = str(getattr(job.prefetched[1], "title", "") or "").strip()
        elif job.pack_request is not None:
            initial_title = (job.pack_request.label or "").strip()
        rec: dict[str, Any] = {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "url": job.url,
            "title": initial_title,
            "output_path": "",
            "status": "Queued",
            "detail": "",
            "service": service_label,
            "job_kind": "pack" if is_pack else "download",
        }
        profile = getattr(job.options, "profile", None)
        if isinstance(profile, str) and profile.strip():
            rec["profile"] = profile.strip()
        if job.selected_indices is not None:
            rec["selected_indices"] = list(job.selected_indices)
        job_overrides: dict[str, Any] = {}
        for attr in ("output_format", "combine", "output_template", "split_chapters", "media_type"):
            val = getattr(job.options, attr, None)
            if val is not None:
                job_overrides[attr] = val
        if job_overrides:
            rec["job_overrides"] = job_overrides
        if job.pack_request is not None:
            rec["pack_request"] = job.pack_request.to_dict(
                output_format=getattr(job.options, "output_format", None) or job.pack_request.output_format
            )
        with self.queue_lock:
            self.queue_records[job.job_id] = rec
        if persist:
            self._request_save_queue_state()
        return rec

    def get_queue_record(self, job_id: str) -> dict[str, Any] | None:
        """Return a shallow copy of the queue record for `job_id`, if present."""
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            return dict(rec) if rec is not None else None

    def update_queue_record(self, job_id: str, status: str, detail: str = "") -> dict[str, Any] | None:
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            rec["status"] = status
            rec["detail"] = detail
        self._request_save_queue_state()
        return rec

    def update_queue_metadata(
        self,
        job_id: str,
        *,
        title: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Update resolved title and/or output path for `job_id` and persist if changed."""
        changed = False
        snapshot: dict[str, Any] | None = None
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            cleaned_title = (title or "").strip()
            cleaned_path = (output_path or "").strip()
            if cleaned_title and rec.get("title") != cleaned_title:
                rec["title"] = cleaned_title
                changed = True
            if cleaned_path and rec.get("output_path") != cleaned_path:
                rec["output_path"] = cleaned_path
                changed = True
            snapshot = dict(rec)
        if changed:
            self._request_save_queue_state()
        return snapshot

    _UNSET_VALUE: Any = object()
    _EDITABLE_JOB_STATUSES = frozenset({"Queued", "Paused", "Failed", "Cancelled", "Interrupted"})
    _EDITABLE_OVERRIDE_KEYS = ("output_format", "combine", "split_chapters", "media_type", "output_template")

    def update_queue_job_config(
        self,
        job_id: str,
        *,
        profile: Any = _UNSET_VALUE,
        job_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update editable `profile` and/or `job_overrides` on a non-running queue job."""
        snapshot: dict[str, Any] | None = None
        with self.queue_cond:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            if str(rec.get("status") or "") not in self._EDITABLE_JOB_STATUSES:
                return None

            norm_prof: str | None = None
            has_profile_update = profile is not self._UNSET_VALUE
            if has_profile_update:
                cleaned_prof = str(profile or "").strip().lower()
                if not cleaned_prof or cleaned_prof == "default":
                    rec.pop("profile", None)
                    norm_prof = None
                else:
                    rec["profile"] = cleaned_prof
                    norm_prof = cleaned_prof

            updated_overrides: dict[str, Any] | None = None
            if job_overrides is not None:
                current_ov = dict(rec.get("job_overrides") or {})
                for key in self._EDITABLE_OVERRIDE_KEYS:
                    if key not in job_overrides:
                        continue
                    val = job_overrides[key]
                    if val is None or val == "":
                        current_ov.pop(key, None)
                    else:
                        current_ov[key] = val
                if current_ov:
                    rec["job_overrides"] = current_ov
                else:
                    rec.pop("job_overrides", None)
                updated_overrides = current_ov

            for pending in self.pending_jobs:
                if pending is None or pending.job_id != job_id:
                    continue
                if has_profile_update:
                    pending.options.profile = norm_prof
                if updated_overrides is not None:
                    explicit = set(getattr(pending.options, "_explicit_fields", set()))
                    for key in self._EDITABLE_OVERRIDE_KEYS:
                        if key in updated_overrides:
                            setattr(pending.options, key, updated_overrides[key])
                            explicit.add(key)
                    pending.options._explicit_fields = frozenset(explicit)
            snapshot = dict(rec)

        self._request_save_queue_state()
        return snapshot

    def clear_seen_urls(self) -> None:
        with self.queue_lock:
            self.run_seen_urls.clear()

    def start_new_run(
        self,
        job_specs: Sequence[tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]],
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
        build_job_options: Callable[[str], DownloadOptions],
        *,
        allow_existing: bool = False,
        selected_indices: list[int] | None = None,
    ) -> tuple[bool, list[QueuedJob], int]:
        """Append unseen URLs (or retried URLs when `allow_existing=True`) to the active queue."""
        unique_urls = list(dict.fromkeys(urls))
        added_jobs: list[QueuedJob] = []
        with self.queue_cond:
            if not self.worker_has_active_run:
                return False, [], self.batch_total
            new_urls = (
                list(unique_urls)
                if allow_existing
                else [u for u in unique_urls if u not in self.run_seen_urls]
            )
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
                    selected_indices=list(selected_indices) if selected_indices is not None and len(new_urls) == 1 else None,
                )
                self.pending_jobs.append(job)
                added_jobs.append(job)
            new_total = self.batch_total
            self.queue_cond.notify_all()
        return True, added_jobs, new_total

    def enqueue_from_preview(
        self,
        url: str,
        options: DownloadOptions,
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

    def start_or_enqueue_jobs(
        self,
        *,
        download_specs: Sequence[
            tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ] = (),
        pack_requests: Sequence[tuple[PackJobRequest, DownloadOptions]] = (),
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue download and/or pack jobs onto the active run, or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        total_incoming = len(download_specs) + len(pack_requests)
        queued_jobs: list[QueuedJob] = []
        with self.queue_cond:
            was_appended = self.worker_has_active_run
            if not was_appended:
                self.cancel_event.clear()
                self.pause_event.set()
                self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
                self.worker_run_id += 1
                self.worker_has_active_run = True
                self.batch_total = total_incoming
                self.run_seen_urls = {url for url, _, _, _ in download_specs}
                self.sink.set_active_run_id(self.worker_run_id)
            else:
                if self.interaction is None:
                    self.interaction = GuiInteractionHandler(self.event_queue, self.cancel_event)
                self.batch_total += total_incoming
                self.run_seen_urls.update(url for url, _, _, _ in download_specs)
            run_id = self.worker_run_id
            interaction = self.interaction
            for url, job_opts, selected_indices, prefetched in download_specs:
                job = QueuedJob(
                    job_id=self.make_job_id(),
                    run_id=run_id,
                    url=url,
                    options=job_opts,
                    interaction=interaction,
                    selected_indices=selected_indices,
                    prefetched=prefetched,
                )
                self.pending_jobs.append(job)
                queued_jobs.append(job)
            for req, opts in pack_requests:
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

    def start_or_enqueue_pack_jobs(
        self,
        requests_with_options: Sequence[tuple[PackJobRequest, DownloadOptions]],
    ) -> tuple[bool, int, list[QueuedJob], int]:
        """Queue local pack jobs on the active run or start a new run when idle.

        Returns `(was_appended, run_id, queued_jobs, batch_total)`.
        """
        return self.start_or_enqueue_jobs(pack_requests=requests_with_options)

    def inspect_local_pack_preview(
        self,
        paths: Sequence[str],
        options: DownloadOptions,
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
        removed_job: QueuedJob | None = None
        with self.queue_cond:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return False, None, False
            pending_idx: int | None = None
            for idx, job in enumerate(self.pending_jobs):
                if job is not None and job.job_id == job_id:
                    pending_idx = idx
                    break
            if pending_idx is not None:
                removed_job = self.pending_jobs[pending_idx]
                del self.pending_jobs[pending_idx]
                self.batch_total = max(0, self.batch_total - 1)
                if not any(j is not None and j.url == rec["url"] for j in self.pending_jobs):
                    self.run_seen_urls.discard(rec["url"])
                new_total: int | None = self.batch_total
            elif self.active_job_id == job_id:
                if self.active_job_running or rec["status"] == "Running":
                    return False, None, True
                self.cancelled_job_ids.add(job_id)
                self.batch_total = max(0, self.batch_total - 1)
                if not any(j is not None and j.url == rec["url"] for j in self.pending_jobs):
                    self.run_seen_urls.discard(rec["url"])
                new_total = self.batch_total
            elif rec["status"] == "Running":
                return False, None, True
            else:
                new_total = None
            self.queue_records.pop(job_id, None)
        self._close_job_prefetched(removed_job)
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

    def move_pending_job_to_edge(self, job_id: str, *, to_top: bool) -> list[str] | None:
        """Move `job_id` to the front or back of `pending_jobs`; return new pending job ID order."""
        with self.queue_cond:
            pending_list = list(self.pending_jobs)
            pos = next(
                (i for i, job in enumerate(pending_list) if job is not None and job.job_id == job_id),
                None,
            )
            if pos is None:
                return None
            job = pending_list.pop(pos)
            assert job is not None
            non_null = [j for j in pending_list if j is not None]
            has_sentinel = any(j is None for j in pending_list)
            if to_top:
                reordered = [job, *non_null]
            else:
                reordered = [*non_null, job]
            self.pending_jobs = deque(reordered + ([None] if has_sentinel else []))
            return [j.job_id for j in reordered]

    def move_pending_job_to_target(self, job_id: str, target_job_id: str) -> list[str] | None:
        """Move pending `job_id` to the position currently occupied by `target_job_id`."""
        if job_id == target_job_id:
            return None
        with self.queue_cond:
            pending_list = list(self.pending_jobs)
            pos_src = next(
                (i for i, job in enumerate(pending_list) if job is not None and job.job_id == job_id),
                None,
            )
            pos_dst = next(
                (i for i, job in enumerate(pending_list) if job is not None and job.job_id == target_job_id),
                None,
            )
            if pos_src is None or pos_dst is None:
                return None
            job = pending_list.pop(pos_src)
            assert job is not None
            pending_list.insert(pos_dst, job)
            self.pending_jobs = deque(pending_list)
            return [j.job_id for j in pending_list if j is not None]

    def retryable_urls(self, ordered_ids: Sequence[str]) -> list[str]:
        with self.queue_lock:
            return [
                rec["url"]
                for iid in ordered_ids
                for rec in [self.queue_records.get(iid)]
                if rec is not None
                and rec.get("job_kind", "download") == "download"
                and rec["status"] in ("Failed", "Cancelled", "Interrupted")
            ]

    def retryable_jobs(self, ordered_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return shallow copies of retryable download and pack queue records in order."""
        out: list[dict[str, Any]] = []
        with self.queue_lock:
            for iid in ordered_ids:
                rec = self.queue_records.get(iid)
                if rec is None:
                    continue
                if rec.get("status") not in ("Failed", "Cancelled", "Interrupted"):
                    continue
                job_kind = rec.get("job_kind", "download")
                if job_kind == "download" and rec.get("url"):
                    out.append(dict(rec))
                elif job_kind == "pack" and isinstance(rec.get("pack_request"), dict):
                    out.append(dict(rec))
        return out

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
        cancelled_jobs: list[QueuedJob] = []
        with self.queue_cond:
            while self.pending_jobs:
                pending = self.pending_jobs.popleft()
                if pending is None:
                    saw_sentinel = True
                    break
                cancelled_jobs.append(pending)
            self.worker_has_active_run = False
        for job in cancelled_jobs:
            self._close_job_prefetched(job)
            self.event_queue.put(JobStatusEvent(job_id=job.job_id, status=cancel_status, detail=""))
        return saw_sentinel

    def _pop_next_job_locked(self) -> tuple[QueuedJob | None, bool]:
        """Pop the next job from `pending_jobs` while holding `self.queue_cond`."""
        if not self.pending_jobs:
            self.worker_has_active_run = False
            self.active_job_id = None
            self.active_job_running = False
            return None, False
        next_job = self.pending_jobs.popleft()
        if next_job is None:
            self.worker_has_active_run = False
            self.active_job_id = None
            self.active_job_running = False
            return None, True
        self.active_job_id = next_job.job_id
        self.active_job_running = False
        return next_job, False

    def _discard_seen_url_if_unpending(self, url: str) -> None:
        with self.queue_cond:
            if not any(j is not None and j.url == url for j in self.pending_jobs):
                self.run_seen_urls.discard(url)

    def _worker_loop(self) -> None:
        while True:
            with self.queue_cond:
                while not self.pending_jobs:
                    self.queue_cond.wait()
                first_job = self.pending_jobs.popleft()
                if first_job is not None:
                    self.active_job_id = first_job.job_id
                    self.active_job_running = False
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
                    self.event_queue.put(
                        JobStatusEvent(job_id=current_job.job_id, status="Paused", detail="Queue paused")
                    )
                    while not self.pause_event.wait(0.2):
                        if self.cancel_event.is_set():
                            break
                        with self.queue_cond:
                            if current_job.job_id in self.cancelled_job_ids:
                                break
                if self.cancel_event.is_set():
                    batch_summary.cancelled = True
                    self._close_job_prefetched(current_job)
                    with self.queue_cond:
                        self.cancelled_job_ids.discard(current_job.job_id)
                        self.active_job_id = None
                        self.active_job_running = False
                    self.event_queue.put(
                        JobStatusEvent(job_id=current_job.job_id, status="Cancelled", detail="")
                    )
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    break

                active_job: QueuedJob | None = current_job
                next_job: QueuedJob | None = None
                skipped_job: QueuedJob | None = None
                item_total = 0
                with self.queue_cond:
                    if current_job.job_id in self.cancelled_job_ids:
                        self.cancelled_job_ids.discard(current_job.job_id)
                        self.active_job_id = None
                        self.active_job_running = False
                        skipped_job = current_job
                        next_job, saw_sentinel = self._pop_next_job_locked()
                        active_job = None
                    else:
                        self.active_job_running = True
                        item_total = self.batch_total

                if active_job is None:
                    self._close_job_prefetched(skipped_job)
                    current_job = next_job
                    continue

                item_index += 1
                self.event_queue.put(
                    BatchItemEvent(
                        run_id=run_id,
                        item_index=item_index,
                        item_total=item_total,
                        target_url=active_job.url,
                    )
                )
                self.event_queue.put(
                    JobStatusEvent(job_id=active_job.job_id, status="Running", detail="")
                )
                self.sink.set_active_job_id(active_job.job_id)

                should_break = False
                try:
                    config = self.config_snapshot()
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
                            "sink": self.sink,
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
                        self.event_queue.put(
                            JobMetaEvent(
                                job_id=active_job.job_id,
                                title=resolved_title,
                                output_path=resolved_output_path,
                            )
                        )
                    batch_summary.completed += summary.completed
                    batch_summary.failed += summary.failed
                    batch_summary.books_completed += summary.books_completed
                    batch_summary.books_skipped += summary.books_skipped
                    batch_summary.books_failed += summary.books_failed
                    batch_summary.book_errors.extend(summary.book_errors)
                    if summary.cancelled:
                        batch_summary.cancelled = True
                        self._discard_seen_url_if_unpending(active_job.url)
                        self.event_queue.put(
                            JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
                        )
                        saw_sentinel = self._drain_pending_jobs("Cancelled")
                        should_break = True
                    elif summary.failed > 0 or summary.books_failed > 0:
                        detail = format_failed_job_detail(summary.book_errors)
                        self._discard_seen_url_if_unpending(active_job.url)
                        self.event_queue.put(
                            JobStatusEvent(job_id=active_job.job_id, status="Failed", detail=detail)
                        )
                    else:
                        if summary.books_completed:
                            out_name = os.path.basename(resolved_output_path) if resolved_output_path else ""
                            detail = (
                                f"{summary.books_completed} ok \u2014 {out_name}"
                                if out_name
                                else f"{summary.books_completed} ok"
                            )
                        else:
                            detail = "Skipped" if summary.books_skipped else "Done"
                        self.event_queue.put(
                            JobStatusEvent(job_id=active_job.job_id, status="Completed", detail=detail)
                        )
                except DownloadCancelled:
                    batch_summary.cancelled = True
                    self._discard_seen_url_if_unpending(active_job.url)
                    self.event_queue.put(
                        JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
                    )
                    saw_sentinel = self._drain_pending_jobs("Cancelled")
                    should_break = True
                except Exception as exc:
                    if self.cancel_event.is_set():
                        batch_summary.cancelled = True
                        self._discard_seen_url_if_unpending(active_job.url)
                        self.event_queue.put(
                            JobStatusEvent(job_id=active_job.job_id, status="Cancelled", detail="")
                        )
                        saw_sentinel = self._drain_pending_jobs("Cancelled")
                        should_break = True
                    else:
                        err_text = str(exc).strip() or type(exc).__name__
                        batch_summary.failed += 1
                        batch_summary.books_failed += 1
                        batch_summary.book_errors.append((active_job.url, err_text))
                        self._discard_seen_url_if_unpending(active_job.url)
                        self.event_queue.put(
                            LogEvent(level="ERROR", message=f"{active_job.url}: {err_text}")
                        )
                        self.event_queue.put(
                            JobStatusEvent(
                                job_id=active_job.job_id,
                                status="Failed",
                                detail=err_text[:120],
                            )
                        )
                finally:
                    self.sink.set_active_job_id(None)
                    self._close_job_prefetched(active_job)
                    with self.queue_cond:
                        self.cancelled_job_ids.discard(active_job.job_id)
                        self.active_job_id = None
                        self.active_job_running = False

                if should_break:
                    break

                with self.queue_cond:
                    current_job, saw_sentinel = self._pop_next_job_locked()

            self.event_queue.put(
                RunDoneEvent(run_id=run_id, summary=batch_summary, error=fatal_exc)
            )
            if saw_sentinel:
                break

    def cancel_preview(self) -> None:
        """Cancel any active metadata preview and unblock pending interaction prompts."""
        self.preview_cancel_event.set()
        if self.preview_interaction is not None:
            self.preview_interaction.release_all()

    def start_preview(self, url: str, options: DownloadOptions) -> int:
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
                self.event_queue.put(
                    PreviewDoneEvent(url=url, prepared=(source, result), error=None)
                )
            except Exception as exc:
                if request_id == self.preview_request_id:
                    self.event_queue.put(PreviewDoneEvent(url=url, prepared=None, error=exc))

        threading.Thread(target=_worker, daemon=True).start()
        return request_id

    def _register_aux_interaction(self, interaction: GuiInteractionHandler) -> None:
        with self._aux_interactions_lock:
            self._aux_interactions.add(interaction)

    def _unregister_aux_interaction(self, interaction: GuiInteractionHandler) -> None:
        with self._aux_interactions_lock:
            self._aux_interactions.discard(interaction)

    def release_auxiliary_interactions(self) -> None:
        """Unblock any pending auxiliary interaction prompts (auth test, catalog fetch)."""
        with self._aux_interactions_lock:
            handlers = list(self._aux_interactions)
        for handler in handlers:
            handler.release_all()

    @contextmanager
    def auxiliary_interaction(self) -> Generator[tuple[GuiInteractionHandler, threading.Event]]:
        """Yield a registered `(GuiInteractionHandler, cancel_event)` pair for an auxiliary worker."""
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self.event_queue, cancel_ev)
        self._register_aux_interaction(interaction)
        try:
            yield interaction, cancel_ev
        finally:
            cancel_ev.set()
            interaction.release_all()
            self._unregister_aux_interaction(interaction)

    def start_auth_verification(
        self,
        service_key: str,
        service_label: str,
        options: DownloadOptions,
    ) -> None:
        """Verify credentials for `service_key` on a background thread."""
        cfg = self.config_snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self.event_queue, cancel_ev)
        self._register_aux_interaction(interaction)

        def _worker() -> None:
            try:
                ok = verify_service_auth(service_key, options, cfg, interaction=interaction)
                self.event_queue.put(
                    AuthVerifiedEvent(service_label=service_label, ok=ok, error=None)
                )
            except Exception as exc:
                self.event_queue.put(
                    AuthVerifiedEvent(service_label=service_label, ok=False, error=exc)
                )
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._unregister_aux_interaction(interaction)

        threading.Thread(target=_worker, daemon=True).start()

    def cancel_catalog_fetch(self) -> bool:
        """Cancel the currently in-flight catalog search/list request, if any."""
        cancel_ev = self.catalog_cancel_event
        interaction = self.catalog_interaction
        if cancel_ev is None and interaction is None:
            return False
        self.catalog_request_id += 1
        if cancel_ev is not None:
            cancel_ev.set()
        if interaction is not None:
            interaction.release_all()
        self.catalog_cancel_event = None
        self.catalog_interaction = None
        return True

    def start_catalog_fetch(
        self,
        mode: str,
        service_key: str,
        query_text: str,
        options: DownloadOptions,
        dialog: Any,
        *,
        archive_path: str | None = None,
    ) -> int:
        """Search a service catalog or list its library on a background thread."""
        self.cancel_catalog_fetch()
        self.catalog_request_id += 1
        request_id = self.catalog_request_id
        cfg = self.config_snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self.event_queue, cancel_ev)
        self.catalog_cancel_event = cancel_ev
        self.catalog_interaction = interaction
        self._register_aux_interaction(interaction)

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
                if cancel_ev.is_set():
                    return
                statuses: list[str] | None = None
                if books and archive_path and os.path.exists(archive_path):
                    from audiobookdl.utils.archive import DownloadArchive

                    with DownloadArchive(archive_path, persistent=True) as archive:
                        statuses = archive.lookup_catalog_statuses(service_key, books)
                if not cancel_ev.is_set():
                    self.event_queue.put(
                        CatalogLoadedEvent(
                            request_id=request_id,
                            target_tab=dialog,
                            payload=(books, statuses),
                            error=None,
                        )
                    )
            except Exception as exc:
                if not cancel_ev.is_set():
                    self.event_queue.put(
                        CatalogLoadedEvent(
                            request_id=request_id,
                            target_tab=dialog,
                            payload=([], None),
                            error=exc,
                        )
                    )
            finally:
                cancel_ev.set()
                interaction.release_all()
                self._unregister_aux_interaction(interaction)
                if self.catalog_cancel_event is cancel_ev:
                    self.catalog_cancel_event = None
                if self.catalog_interaction is interaction:
                    self.catalog_interaction = None

        threading.Thread(target=_worker, daemon=True).start()
        return request_id

    def cancel_library_sync(self) -> None:
        """Signal any active library synchronization pass to cancel and unblock prompts."""
        if self.sync_cancel_event is not None:
            self.sync_cancel_event.set()
        if self.sync_interaction is not None:
            self.sync_interaction.release_all()

    def start_library_sync(self, sync_opts: SyncOptions) -> None:
        """Run one library synchronization cycle (`audiobookdl.sync.run_sync_once`) on a worker thread."""
        from audiobookdl.sync import run_sync_once

        cfg = self.config_snapshot()
        ffmpeg_dir = cfg.ffmpeg_dir.strip() or None if cfg.ffmpeg_dir else None
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self.event_queue, cancel_ev)
        self.sync_cancel_event = cancel_ev
        self.sync_interaction = interaction
        ctx = logging.JobContext(
            ffmpeg_dir=ffmpeg_dir,
            sink=self.sink,
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
                        sink=self.sink,
                    )
                self.event_queue.put(SyncDoneEvent(cycle=cycle, error=None))
            except Exception as exc:
                self.event_queue.put(SyncDoneEvent(cycle=None, error=exc))
            finally:
                if self.sync_cancel_event is cancel_ev:
                    self.sync_cancel_event = None
                if self.sync_interaction is interaction:
                    self.sync_interaction = None
                interaction.release_all()

        threading.Thread(target=_worker, daemon=True).start()

    def start_sync_enqueue_discovery(
        self,
        targets: Sequence[tuple[str, str]],
        sync_opts: SyncOptions,
    ) -> None:
        """Discover pending library URLs across `targets` and emit `sync_enqueue_ready`."""
        from audiobookdl.config import DEFAULT_PROFILE
        from audiobookdl.sync import _build_sync_download_options
        from audiobookdl.utils.archive import DownloadArchive, make_catalog_book_key, resolve_archive_path

        cfg = self.config_snapshot()
        cancel_ev = threading.Event()
        interaction = GuiInteractionHandler(self.event_queue, cancel_ev)
        self.sync_cancel_event = cancel_ev
        self.sync_interaction = interaction

        def _worker() -> None:
            discovered = 0
            newly_added = 0
            skipped = 0
            pending_by_profile: list[tuple[str, str, list[str]]] = []
            errors: list[str] = []
            try:
                archive_path = resolve_archive_path(cfg, sync_opts.archive_path)
                archive = DownloadArchive(archive_path)
                for svc, prof in targets:
                    if cancel_ev.is_set():
                        break
                    try:
                        dl_opts = _build_sync_download_options(sync_opts, cfg, archive_path, prof)
                        books = list_service_library(svc, dl_opts, cfg, interaction=interaction)
                        discovered += len(books)
                        if books:
                            ins, _upd = archive.upsert_catalog_items(svc, books, profile=prof)
                            newly_added += ins
                        urls: list[str] = []
                        for book in books:
                            if not book.url:
                                continue
                            item_source = (book.source or svc).strip().lower() or svc
                            book_key = make_catalog_book_key(item_source, book)
                            entry = archive.get_entry_by_key(item_source, book_key)
                            if entry is not None and (
                                entry.status in ("downloaded", "trashed") or entry.is_hidden
                            ):
                                skipped += 1
                                continue
                            if book.url not in urls:
                                urls.append(book.url)
                        if urls:
                            pending_by_profile.append(
                                (svc, "" if prof == DEFAULT_PROFILE else prof, urls)
                            )
                    except Exception as exc:
                        errors.append(f"{svc} ({prof}): {exc}")
                self.event_queue.put(
                    SyncEnqueueReadyEvent(
                        pending_groups=pending_by_profile,
                        stats={
                            "discovered": discovered,
                            "newly_added": newly_added,
                            "skipped": skipped,
                            "errors": errors,
                            "cancelled": cancel_ev.is_set(),
                        },
                        error=None,
                    )
                )
            except Exception as exc:
                self.event_queue.put(SyncEnqueueReadyEvent(pending_groups=[], stats={}, error=exc))
            finally:
                if self.sync_cancel_event is cancel_ev:
                    self.sync_cancel_event = None
                if self.sync_interaction is interaction:
                    self.sync_interaction = None
                interaction.release_all()

        threading.Thread(target=_worker, daemon=True).start()

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

        cfg = self.config_snapshot()
        abs_cfg = cfg.audiobookshelf

        def _worker() -> None:
            if not abs_cfg.url or not abs_cfg.api_key or not abs_cfg.library_id:
                self.event_queue.put(
                    AbsUploadDoneEvent(
                        title=title,
                        ok=False,
                        error_message="Configure Audiobookshelf URL, token, and library ID first.",
                    )
                )
                return
            client = AudiobookshelfClient(abs_cfg.url, abs_cfg.api_key, session=self.get_http_session())
            try:
                meta = AudiobookMetadata(
                    title=title,
                    authors=[a for a in authors if a],
                    series=series,
                )
                client.upload_book(local_path, library_id=abs_cfg.library_id, metadata=meta)
                if abs_cfg.auto_scan:
                    client.scan_library(abs_cfg.library_id)
                self.event_queue.put(AbsUploadDoneEvent(title=title, ok=True, error_message=None))
            except Exception as exc:
                self.event_queue.put(AbsUploadDoneEvent(title=title, ok=False, error_message=str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def start_update_check(self, *, interactive: bool, respect_opt_out: bool) -> None:
        proxy = self.config.proxy

        def _worker() -> None:
            session = self.get_http_session()
            info = check_for_updates(proxy=proxy, session=session, respect_opt_out=respect_opt_out)
            if interactive or (info is not None and info.has_update):
                self.event_queue.put(UpdateCheckedEvent(info=info, interactive=interactive))

        threading.Thread(target=_worker, daemon=True).start()

    def start_winget_ffmpeg_install(self) -> None:
        def _worker() -> None:
            ok, detail = install_ffmpeg_via_winget()
            self.event_queue.put(FfmpegInstallDoneEvent(ok=ok, detail=detail))

        threading.Thread(target=_worker, daemon=True).start()

    def search_enrichment_for_audiobook(
        self,
        audiobook: Audiobook,
        options: DownloadOptions,
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
        options: DownloadOptions,
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
        handler: Callable[[ControllerEvent], None],
        max_batch: int = MAX_QUEUE_BATCH,
    ) -> int:
        """Drain up to `max_batch` events, coalescing consecutive logs, progress, and queue saves."""
        pending_progress: ProgressAdvanceEvent | None = None
        pending_metrics: ProgressMetricsEvent | None = None
        pending_logs: list[tuple[str, str]] = []
        processed = 0
        self._save_defer_depth += 1

        def _flush_logs() -> None:
            if not pending_logs:
                return
            if len(pending_logs) == 1:
                lvl, msg = pending_logs[0]
                pending_logs.clear()
                handler(LogEvent(level=lvl, message=msg))
                return
            batch = tuple(pending_logs)
            pending_logs.clear()
            handler(LogBatchEvent(entries=batch))

        def _flush_progress() -> None:
            nonlocal pending_progress, pending_metrics
            if pending_progress is not None:
                handler(pending_progress)
                pending_progress = None
            if pending_metrics is not None:
                handler(pending_metrics)
                pending_metrics = None

        try:
            while processed < max_batch:
                try:
                    item = self.event_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                if isinstance(item, LogEvent):
                    _flush_progress()
                    pending_logs.append((item.level, item.message))
                    continue
                if isinstance(item, ProgressAdvanceEvent):
                    _flush_logs()
                    pending_progress = item
                    continue
                if isinstance(item, ProgressMetricsEvent):
                    _flush_logs()
                    pending_metrics = item
                    continue
                _flush_logs()
                _flush_progress()
                handler(item)
            _flush_logs()
            _flush_progress()
        finally:
            self._save_defer_depth -= 1
            if self._save_defer_depth == 0 and self._save_deferred:
                self._save_deferred = False
                self.save_queue_state()
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
        self.cancel_library_sync()
        self.cancel_catalog_fetch()
        self.release_auxiliary_interactions()
        drained_jobs: list[QueuedJob] = []
        with self.queue_cond:
            for item in self.pending_jobs:
                if item is not None:
                    drained_jobs.append(item)
            self.pending_jobs.append(None)
            self.queue_cond.notify_all()
        for job in drained_jobs:
            self._close_job_prefetched(job)
        while True:
            try:
                ev = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(ev, PreviewDoneEvent) and ev.prepared is not None:
                close_fn = getattr(ev.prepared[0], "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
        with self._sessions_lock:
            sessions = list(self._thread_sessions)
            self._thread_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

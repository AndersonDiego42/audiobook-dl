"""Lightweight headless HTTP Web UI dashboard & JSON API powered by `GuiController`."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import PurePosixPath
import queue
import threading
from typing import Any
from collections.abc import Sequence
from urllib.parse import parse_qs, urlparse
import uuid

from audiobookdl import __version__, args, logging, output
from audiobookdl.config import DEFAULT_PROFILE, list_source_profiles
from audiobookdl.gui.controller import GuiController, GuiInteractionHandler, PackJobRequest
from audiobookdl.pipeline import list_service_library, search_service
from audiobookdl.sources import get_available_services, resolve_service_label
from audiobookdl.sync import run_sync_once
from audiobookdl.utils.archive import DownloadArchive, resolve_archive_path

MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_WEB_LOG_LINES = 400


class _PayloadTooLargeError(ValueError):
    """Raised when an HTTP request body exceeds `MAX_REQUEST_BODY_BYTES`."""


class WebDashboardState:
    """Thread-safe headless state bridge around `GuiController` for HTTP/JSON handlers."""

    def __init__(self, serve_options: args.ServeOptions) -> None:
        self.options = serve_options
        self.controller = GuiController(serve_options.config_location)
        self.token: str | None = serve_options.token
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self.is_running = False
        self.is_syncing = False
        self.sync_cancel_event: threading.Event | None = None
        self.last_sync: dict[str, Any] | None = None
        self.last_sync_error: str | None = None
        self.status_text = "Ready"
        self.stage_text = ""
        self.progress_pct = 0.0
        self.speed_bps = 0.0
        self.eta_seconds: float | None = None
        self.logs: deque[str] = deque(maxlen=MAX_WEB_LOG_LINES)
        self._services = get_available_services()
        self._services_by_key = {s.key: s for s in self._services}
        self._sub_lock = threading.Lock()
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]]]] = set()
        self._active_prompts: dict[str, tuple[str, dict[str, Any], threading.Event]] = {}
        self.pending_prompt: dict[str, Any] | None = None

        # Restore persisted unresolved queue entries so status reflects prior state.
        self.controller.load_saved_queue_records()
        self._pump_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._pump_thread.start()

    def subscribe_events(self) -> queue.Queue[tuple[str, dict[str, Any]]]:
        """Register an SSE client queue and enqueue an initial state snapshot."""
        sub_q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=256)
        with self._sub_lock:
            self._subscribers.add(sub_q)
        sub_q.put_nowait(("snapshot", self.status_snapshot()))
        return sub_q

    def unsubscribe_events(self, sub_q: queue.Queue[tuple[str, dict[str, Any]]]) -> None:
        with self._sub_lock:
            self._subscribers.discard(sub_q)

    def has_subscribers(self) -> bool:
        with self._sub_lock:
            return bool(self._subscribers)

    def _broadcast_event(self, event_name: str, payload: dict[str, Any]) -> None:
        with self._sub_lock:
            targets = list(self._subscribers)
        for sub_q in targets:
            try:
                sub_q.put_nowait((event_name, payload))
            except queue.Full:
                try:
                    sub_q.get_nowait()
                    sub_q.put_nowait((event_name, payload))
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    def _snapshot_locked(self) -> dict[str, Any]:
        with self.controller.queue_lock:
            records = [dict(rec) for rec in self.controller.queue_records.values()]
        return {
            "version": __version__,
            "running": self.is_running,
            "syncing": self.is_syncing,
            "paused": self.controller.is_paused(),
            "status": self.status_text,
            "stage": self.stage_text,
            "progress_pct": round(self.progress_pct, 1),
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "last_sync": self.last_sync,
            "last_sync_error": self.last_sync_error,
            "pending_prompt": dict(self.pending_prompt) if self.pending_prompt else None,
            "queue": records,
            "logs": list(self.logs),
        }

    def broadcast_status(self) -> None:
        """Broadcast the current snapshot to active SSE clients."""
        with self._lock:
            snap = self._snapshot_locked()
        self._broadcast_event("status", snap)

    def _clear_active_prompts_locked(self) -> dict[str, Any] | None:
        cleared = bool(self._active_prompts or self.pending_prompt)
        for prompt_kind, holder, done in list(self._active_prompts.values()):
            holder["value"] = False if prompt_kind == "prompt_confirm" else None
            done.set()
        self._active_prompts.clear()
        self.pending_prompt = None
        return {"prompt": None} if cleared else None

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self.controller.drain_events(self._handle_event, max_batch=200)
            self._stop_event.wait(0.02 if processed >= 200 else 0.1)

    def _handle_event(self, item: tuple) -> None:
        kind = item[0]
        events_to_emit: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            if kind == "log":
                _, level, message = item
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                new_lines = [line for line in f"{prefix}{message}".splitlines()]
                self.logs.extend(new_lines)
                if level == "INFO" and message.strip():
                    self.status_text = message.strip().splitlines()[0][:100]
                if new_lines:
                    events_to_emit.append(("log", {"lines": new_lines, "status": self.status_text}))
            elif kind == "log_batch":
                _, entries = item
                new_lines = []
                for level, message in entries:
                    prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                    for line in f"{prefix}{message}".splitlines():
                        self.logs.append(line)
                        new_lines.append(line)
                    if level == "INFO" and message.strip():
                        self.status_text = message.strip().splitlines()[0][:100]
                if new_lines:
                    events_to_emit.append(("log", {"lines": new_lines, "status": self.status_text}))
            elif kind == "batch_item":
                self.stage_text = "Fetching metadata"
                events_to_emit.append(("progress", {
                    "status": self.status_text,
                    "stage": self.stage_text,
                    "progress_pct": round(self.progress_pct, 1),
                    "speed_bps": self.speed_bps,
                    "eta_seconds": self.eta_seconds,
                }))
            elif kind == "job_status":
                _, job_id, status_val, detail = item
                self.controller.update_queue_record(job_id, status_val, detail)
                with self.controller.queue_lock:
                    records = [dict(rec) for rec in self.controller.queue_records.values()]
                events_to_emit.append(("queue", {"queue": records}))
            elif kind == "job_meta":
                _, job_id, title_val, output_path_val = item
                self.controller.update_queue_metadata(
                    job_id,
                    title=title_val or None,
                    output_path=output_path_val or None,
                )
                with self.controller.queue_lock:
                    records = [dict(rec) for rec in self.controller.queue_records.values()]
                events_to_emit.append(("queue", {"queue": records}))
            elif kind == "progress_start":
                _, description, _total = item
                self.progress_pct = 0.0
                self.speed_bps = 0.0
                self.eta_seconds = None
                self.stage_text = "Downloading"
                self.status_text = str(description)[:100]
                events_to_emit.append(("progress", {
                    "status": self.status_text,
                    "stage": self.stage_text,
                    "progress_pct": 0.0,
                    "speed_bps": 0.0,
                    "eta_seconds": None,
                }))
            elif kind == "progress_advance":
                _, completed, total = item
                pct = (completed / total * 100.0) if total > 0 else 0.0
                self.progress_pct = min(100.0, max(0.0, pct))
                events_to_emit.append(("progress", {
                    "status": self.status_text,
                    "stage": self.stage_text,
                    "progress_pct": round(self.progress_pct, 1),
                    "speed_bps": self.speed_bps,
                    "eta_seconds": self.eta_seconds,
                }))
            elif kind == "progress_metrics":
                _, completed, total, _bytes_dl, speed_bps, _elapsed, eta = item
                pct = (completed / total * 100.0) if total > 0 else 0.0
                self.progress_pct = min(100.0, max(0.0, pct))
                self.speed_bps = float(speed_bps or 0.0)
                self.eta_seconds = eta
                events_to_emit.append(("progress", {
                    "status": self.status_text,
                    "stage": self.stage_text,
                    "progress_pct": round(self.progress_pct, 1),
                    "speed_bps": self.speed_bps,
                    "eta_seconds": self.eta_seconds,
                }))
            elif kind == "progress_finish":
                self.progress_pct = 100.0
                self.eta_seconds = 0.0
                events_to_emit.append(("progress", {
                    "status": self.status_text,
                    "stage": self.stage_text,
                    "progress_pct": 100.0,
                    "speed_bps": self.speed_bps,
                    "eta_seconds": 0.0,
                }))
            elif kind in ("prompt_input", "prompt_confirm", "prompt_choice"):
                holder, done = item[-2], item[-1]
                if not self.has_subscribers():
                    # Unattended headless run without web UI listeners: unblock immediately.
                    holder["value"] = False if kind == "prompt_confirm" else None
                    done.set()
                else:
                    prompt_id = uuid.uuid4().hex[:10]
                    if kind == "prompt_input":
                        prompt_meta: dict[str, Any] = {
                            "id": prompt_id,
                            "kind": "input",
                            "message": str(item[1]),
                            "hidden": bool(item[2]),
                        }
                    elif kind == "prompt_confirm":
                        prompt_meta = {
                            "id": prompt_id,
                            "kind": "confirm",
                            "message": str(item[1]),
                        }
                    else:
                        prompt_meta = {
                            "id": prompt_id,
                            "kind": "choice",
                            "message": str(item[1]),
                            "choices": [str(c) for c in item[2]],
                            "default": item[3],
                        }
                    self._active_prompts[prompt_id] = (kind, holder, done)
                    self.pending_prompt = prompt_meta
                    events_to_emit.append(("prompt", {"prompt": dict(prompt_meta)}))
            elif kind == "done":
                _, _run_id, summary, exc = item
                self.is_running = False
                self.stage_text = ""
                self.controller.clear_seen_urls()
                prompt_clear = self._clear_active_prompts_locked()
                if prompt_clear is not None:
                    events_to_emit.append(("prompt", prompt_clear))
                if exc is not None:
                    self.progress_pct = 0.0
                    self.status_text = f"Error: {exc}"
                elif summary is None or summary.cancelled:
                    self.progress_pct = 0.0
                    self.status_text = "Cancelled"
                elif summary.failed > 0 and summary.completed == 0:
                    self.progress_pct = 0.0
                    self.status_text = "Failed"
                elif summary.failed > 0:
                    self.progress_pct = 100.0
                    self.status_text = f"Done ({summary.completed} ok, {summary.failed} failed)"
                else:
                    self.progress_pct = 100.0
                    self.status_text = "Completed"
                events_to_emit.append(("status", self._snapshot_locked()))
        for ev_name, payload in events_to_emit:
            self._broadcast_event(ev_name, payload)

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._clear_active_prompts_locked()
        self.cancel_sync()
        self.controller.shutdown(is_running=self.is_running, is_previewing=False)
        if self._pump_thread.is_alive():
            self._pump_thread.join(timeout=2.0)

    def cancel_sync(self) -> None:
        ev = self.sync_cancel_event
        if ev is not None:
            ev.set()

    def respond_prompt(
        self,
        prompt_id: str,
        value: Any = None,
        *,
        cancelled: bool = False,
    ) -> bool:
        """Resolve a pending interactive prompt triggered by a running download or sync worker."""
        prompt_payload: dict[str, Any] | None = None
        with self._lock:
            entry = self._active_prompts.pop(prompt_id, None)
            if entry is None:
                return False
            prompt_kind, holder, done = entry
            if cancelled:
                holder["value"] = False if prompt_kind == "prompt_confirm" else None
            elif prompt_kind == "prompt_confirm":
                holder["value"] = bool(value)
            elif prompt_kind == "prompt_choice":
                if isinstance(value, int) and not isinstance(value, bool):
                    holder["value"] = value
                elif isinstance(value, str) and value.strip().isdigit():
                    holder["value"] = int(value.strip())
                else:
                    holder["value"] = None
            else:
                holder["value"] = "" if value is None else str(value)
            done.set()
            if self.pending_prompt and self.pending_prompt.get("id") == prompt_id:
                self.pending_prompt = None
            prompt_payload = {"prompt": dict(self.pending_prompt) if self.pending_prompt else None}
        if prompt_payload is not None:
            self._broadcast_event("prompt", prompt_payload)
        return True

    def start_sync(self, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self.is_running or self.is_syncing:
                raise RuntimeError("A download or library sync is already in progress")
            self.is_syncing = True
            self.last_sync_error = None
            self.status_text = "Syncing libraries..."
            self.stage_text = "Syncing library"
            cancel_ev = threading.Event()
            self.sync_cancel_event = cancel_ev
        self.broadcast_status()

        cfg = self.controller.config_snapshot()
        raw = payload or {}
        raw_services = raw.get("services") or []
        services = [str(s).strip() for s in raw_services if str(s).strip()] if isinstance(raw_services, list) else []
        sync_opts = args.SyncOptions(
            services=services,
            scan_only=bool(raw.get("scan_only", False)),
            dry_run=bool(raw.get("dry_run", False)),
            output_template=self.options.output_template,
            output_format=self.options.output_format,
            config_location=self.options.config_location,
        )
        interaction = GuiInteractionHandler(self.controller.event_queue, cancel_ev)
        ffmpeg_dir = cfg.ffmpeg_dir.strip() or None if cfg.ffmpeg_dir else None
        ctx = logging.JobContext(
            ffmpeg_dir=ffmpeg_dir,
            sink=self.controller.sink,
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
                        sink=self.controller.sink,
                    )
                summary = cycle.to_dict()
                with self._lock:
                    self.last_sync = summary
                    self.last_sync_error = None
                    if cycle.cancelled:
                        self.status_text = "Library sync cancelled"
                    else:
                        self.status_text = (
                            f"Sync complete: {cycle.books_discovered} discovered, "
                            f"{cycle.downloaded} downloaded"
                        )
            except Exception as exc:
                err_line = f"[ERROR] Library sync failed: {exc}"
                with self._lock:
                    self.last_sync_error = str(exc)
                    self.status_text = f"Sync error: {exc}"
                    self.logs.append(err_line)
                self._broadcast_event("log", {"lines": [err_line], "status": self.status_text})
            finally:
                interaction.release_all()
                with self._lock:
                    self.is_syncing = False
                    if self.sync_cancel_event is cancel_ev:
                        self.sync_cancel_event = None
                    self.stage_text = ""
                    snap = self._snapshot_locked()
                self._broadcast_event("status", snap)

        threading.Thread(target=_worker, daemon=True).start()

    def archive_path(self) -> str:
        return resolve_archive_path(self.controller.config_snapshot())

    def build_download_options(
        self,
        urls: Sequence[str],
        overrides: dict[str, Any] | None = None,
    ) -> args.DownloadOptions:
        cfg = self.controller.config_snapshot()
        raw_ov = overrides or {}
        configured_base = os.path.realpath(
            self.options.output_dir or cfg.gui.output_dir or os.getcwd()
        )
        raw_dir = raw_ov.get("output_dir")
        if raw_dir is not None and str(raw_dir).strip():
            requested_dir = str(raw_dir).strip()
            candidate_dir = os.path.realpath(
                requested_dir
                if os.path.isabs(requested_dir)
                else os.path.join(configured_base, requested_dir)
            )
            try:
                inside = os.path.commonpath([configured_base, candidate_dir]) == configured_base
            except ValueError:
                inside = False
            if not inside:
                raise ValueError("output_dir must stay within the configured output directory")
            base_dir = candidate_dir
        else:
            base_dir = configured_base

        raw_preset = str(raw_ov.get("output_preset") or "").strip().lower()
        preset_key = raw_preset if raw_preset in output.OUTPUT_TEMPLATE_PRESETS else None
        raw_tpl = raw_ov.get("output_template")
        if raw_tpl is not None and str(raw_tpl).strip():
            tpl = str(raw_tpl).strip()
            if (
                os.path.isabs(tpl)
                or tpl.startswith(("/", "\\"))
                or (len(tpl) >= 2 and tpl[1] == ":")
            ):
                raise ValueError("output_template override must be relative")
            if ".." in PurePosixPath(tpl.replace("\\", "/")).parts:
                raise ValueError("output_template must not contain '..' path segments")
        elif preset_key is not None:
            tpl = output.OUTPUT_TEMPLATE_PRESETS[preset_key]
        else:
            tpl = str(
                self.options.output_template
                or cfg.gui.output_template
                or cfg.output_template
                or "{title}"
            ).strip()
        output_template = os.path.join(base_dir, tpl) if base_dir else tpl
        fmt = raw_ov.get("output_format") or self.options.output_format or cfg.output_format
        combine = (
            bool(raw_ov["combine"])
            if "combine" in raw_ov and raw_ov["combine"] is not None
            else (cfg.combine if cfg.combine is not None else (fmt == "m4b"))
        )
        enrich_metadata = (
            bool(raw_ov["enrich_metadata"])
            if "enrich_metadata" in raw_ov and raw_ov["enrich_metadata"] is not None
            else bool(cfg.enrich_metadata)
        )
        raw_profile = str(raw_ov.get("profile") or "").strip()
        profile = None if not raw_profile or raw_profile == DEFAULT_PROFILE else raw_profile
        return args.build_default_options(
            urls=list(urls),
            output_template=output_template,
            output_preset=preset_key,
            output_format=fmt,
            combine=combine,
            enrich_metadata=enrich_metadata,
            download_archive=self.archive_path(),
            config_location=self.options.config_location,
            profile=profile,
        )

    def services_payload(self) -> list[dict[str, Any]]:
        cfg = self.controller.config_snapshot()
        return [
            {**asdict(s), "profiles": list_source_profiles(cfg, s.key)}
            for s in get_available_services()
        ]

    def service_label_for_url(self, url: str) -> str:
        return resolve_service_label(url, self._services_by_key)

    def queue_urls(self, urls: Sequence[str], payload: dict[str, Any]) -> tuple[int, int]:
        unique_urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        unique_urls = list(dict.fromkeys(unique_urls))
        if not unique_urls:
            raise ValueError("At least one valid URL is required")

        # Hold `_lock` while starting/enqueuing and registering records so `_event_loop`
        # cannot process `job_status`/`done` events before the records exist in `queue_records`.
        with self._lock:
            if self.is_running:
                has_run, added, new_total = self.controller.enqueue_additional_urls(
                    unique_urls,
                    lambda u: self.build_download_options([u], payload),
                )
                if has_run:
                    for job in added:
                        self.controller.register_queue_record(
                            job,
                            self.service_label_for_url(job.url),
                            persist=False,
                        )
                    if added:
                        self.controller.save_queue_state()
                    snap = self._snapshot_locked()
                    self._broadcast_event("status", snap)
                    return len(added), new_total

            job_specs = [
                (u, self.build_download_options([u], payload), None, None)
                for u in unique_urls
            ]
            _run_id, queued_jobs = self.controller.start_new_run(job_specs)
            self.is_running = True
            self.status_text = "Starting..."
            self.stage_text = "Preparing"
            self.progress_pct = 0.0
            for job in queued_jobs:
                self.controller.register_queue_record(
                    job,
                    self.service_label_for_url(job.url),
                    persist=False,
                )
            if queued_jobs:
                self.controller.save_queue_state()
            snap = self._snapshot_locked()
        self._broadcast_event("status", snap)
        return len(queued_jobs), len(queued_jobs)

    def _sync_queue_record_order(self) -> None:
        """Align `queue_records` key order for pending jobs with `controller.pending_jobs`."""
        with self.controller.queue_cond:
            pending_ids = [j.job_id for j in self.controller.pending_jobs if j is not None]
            pending_set = set(pending_ids)
            non_pending = [
                (jid, rec)
                for jid, rec in self.controller.queue_records.items()
                if jid not in pending_set
            ]
            pending_items = [
                (jid, self.controller.queue_records[jid])
                for jid in pending_ids
                if jid in self.controller.queue_records
            ]
            self.controller.queue_records = dict([*non_pending, *pending_items])

    def remove_job(self, job_id: str) -> tuple[bool, int | None, bool]:
        removed, new_total, is_active = self.controller.remove_pending_job(job_id)
        if removed:
            self.broadcast_status()
        return removed, new_total, is_active

    def move_job(self, job_id: str, direction: str) -> bool:
        norm = direction.strip().lower()
        moved = False
        if norm == "up":
            moved = self.controller.move_pending_job(job_id, -1) is not None
        elif norm == "down":
            moved = self.controller.move_pending_job(job_id, 1) is not None
        elif norm == "top":
            moved = self.controller.move_pending_job_to_edge(job_id, to_top=True) is not None
        elif norm == "bottom":
            moved = self.controller.move_pending_job_to_edge(job_id, to_top=False) is not None
        if moved:
            self._sync_queue_record_order()
            self.controller.save_queue_state()
            self.broadcast_status()
        return moved

    def retry_jobs(self, job_ids: Sequence[str] | None = None) -> tuple[int, int]:
        with self.controller.queue_lock:
            target_ids = list(job_ids) if job_ids else list(self.controller.queue_records.keys())
        retry_records = self.controller.retryable_jobs(target_ids)
        if not retry_records:
            return 0, 0
        dl_specs: list[tuple[str, args.DownloadOptions, list[int] | None, None]] = []
        pack_specs: list[tuple[PackJobRequest, args.DownloadOptions]] = []
        for rec in retry_records:
            raw_ov = dict(rec.get("job_overrides") or {})
            saved_tpl = raw_ov.pop("output_template", None)
            if rec.get("profile"):
                raw_ov["profile"] = rec["profile"]
            opts = self.build_download_options([], raw_ov)
            if isinstance(saved_tpl, str) and saved_tpl.strip():
                opts.output_template = saved_tpl.strip()
            if rec.get("job_kind", "download") == "pack":
                req = PackJobRequest.from_dict(rec.get("pack_request"))
                if req is not None:
                    pack_specs.append((req, opts))
            else:
                url = str(rec.get("url") or "").strip()
                if url:
                    opts.urls = [url]
                    raw_sel = rec.get("selected_indices")
                    sel = [int(i) for i in raw_sel] if isinstance(raw_sel, list) else None
                    dl_specs.append((url, opts, sel, None))
        if not dl_specs and not pack_specs:
            return 0, 0
        with self._lock:
            was_appended, _run_id, queued_jobs, new_total = self.controller.start_or_enqueue_jobs(
                download_specs=dl_specs,
                pack_requests=pack_specs,
            )
            if not was_appended:
                self.is_running = True
                self.status_text = "Starting..."
                self.stage_text = "Preparing"
                self.progress_pct = 0.0
            for job in queued_jobs:
                svc = (
                    "Local Pack"
                    if job.pack_request is not None
                    else self.service_label_for_url(job.url)
                )
                self.controller.register_queue_record(job, svc, persist=False)
            if queued_jobs:
                self.controller.save_queue_state()
            snap = self._snapshot_locked()
        self._broadcast_event("status", snap)
        return len(queued_jobs), new_total

    def clear_queue(self) -> int:
        with self.controller.queue_lock:
            ids = list(self.controller.queue_records.keys())
        cleared = self.controller.clear_completed_records(ids)
        if cleared:
            self.broadcast_status()
        return len(cleared)

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


def _render_dashboard_html(requires_token: bool) -> str:
    token_banner = (
        '<div class="card"><label for="api-token">API Token:</label> '
        '<input id="api-token" type="password" placeholder="Enter bearer token">'
        '<button type="button" onclick="saveToken()">Unlock</button></div>'
        if requires_token
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>audiobook-dl Web Dashboard v{__version__}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f172a; --card:#1e293b; --border:#334155; --fg:#f8fafc; --muted:#94a3b8; --accent:#38bdf8; --ok:#22c55e; --err:#ef4444; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--fg); padding:20px; line-height:1.4; }}
    .wrap {{ max-width:1040px; margin:0 auto; display:grid; gap:16px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
    h1,h2 {{ margin:0 0 10px 0; font-weight:600; }}
    h1 {{ font-size:1.35rem; display:flex; justify-content:space-between; align-items:center; }}
    h2 {{ font-size:1.05rem; }}
    textarea,input,select {{ background:#0b1220; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:8px 10px; font:inherit; }}
    textarea {{ width:100%; min-height:76px; resize:vertical; }}
    .row {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px; }}
    button {{ background:var(--accent); color:#04111d; border:none; border-radius:6px; padding:8px 14px; font-weight:600; cursor:pointer; }}
    button.secondary {{ background:#334155; color:var(--fg); }}
    button.danger {{ background:var(--err); color:#fff; }}
    progress {{ width:100%; height:14px; margin-top:8px; }}
    .muted {{ color:var(--muted); font-size:0.9rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.9rem; margin-top:8px; }}
    th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }}
    pre {{ background:#0b1220; border:1px solid var(--border); border-radius:6px; padding:10px; max-height:200px; overflow:auto; font-size:0.82rem; margin:8px 0 0; }}
    .msg {{ margin-top:8px; font-size:0.9rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1><span>audiobook-dl Web UI <small class="muted">v{__version__}</small> <small id="sse-badge" class="muted"></small></span>
      <button type="button" class="secondary" onclick="triggerSync()">Sync Libraries</button></h1>
    {token_banner}
    <div id="prompt-banner" class="card" hidden>
      <h2>Action Required</h2>
      <div id="prompt-message"></div>
      <div class="row" id="prompt-controls"></div>
    </div>
    <div class="card">
      <h2>Queue Download</h2>
      <label for="urls-input" class="muted">Audiobook, series, archive.org, or RSS feed URLs (one per line):</label>
      <textarea id="urls-input" placeholder="https://librivox.org/..."></textarea>
      <div class="row">
        <label for="fmt-select">Format:</label>
        <select id="fmt-select"><option value="">Original</option><option value="m4b" selected>.m4b</option><option value="mp3">.mp3</option></select>
        <label for="preset-select">Preset:</label>
        <select id="preset-select"><option value="">Custom</option><option value="default">Default</option><option value="audiobookshelf">Audiobookshelf</option><option value="plex">Plex</option><option value="kavita">Kavita</option></select>
        <label for="profile-select">Profile:</label>
        <select id="profile-select"><option value="default">default</option></select>
        <label><input type="checkbox" id="combine-cb" checked> Combine parts</label>
        <label><input type="checkbox" id="enrich-cb"> Enrich metadata</label>
        <button type="button" onclick="submitUrls()">Download / Queue</button>
        <button type="button" class="secondary" id="pause-btn" onclick="togglePause()">Pause</button>
        <button type="button" class="danger" onclick="cancelRun()">Cancel</button>
      </div>
      <div id="submit-msg" class="msg muted" aria-live="polite"></div>
    </div>
    <div class="card">
      <h2>Status &amp; Progress
        <button type="button" class="secondary" onclick="retryAllFailed()">Retry Failed</button>
        <button type="button" class="secondary" onclick="clearCompleted()">Clear Completed</button>
      </h2>
      <div id="status-line">Ready</div>
      <div id="stage-line" class="muted"></div>
      <progress id="prog-bar" max="100" value="0"></progress>
      <table><thead><tr><th>Service</th><th>URL / Book</th><th>Status</th><th>Detail</th><th>Actions</th></tr></thead><tbody id="queue-body"></tbody></table>
      <pre id="logs-box"></pre>
    </div>
    <div class="card">
      <h2>Catalog Search &amp; Library</h2>
      <div class="row">
        <select id="svc-select" aria-label="Service"></select>
        <input id="cat-query" type="text" placeholder="Search query (optional for library)">
        <button type="button" onclick="browseCatalog('search')">Search</button>
        <button type="button" class="secondary" onclick="browseCatalog('library')">List Library</button>
      </div>
      <table><thead><tr><th>Title</th><th>Author</th><th>Action</th></tr></thead><tbody id="cat-body"></tbody></table>
    </div>
    <div class="card">
      <h2>Downloaded Archive <button type="button" class="secondary" onclick="refreshLibrary()">Refresh</button></h2>
      <table><thead><tr><th>Service</th><th>Title</th><th>Author</th><th>Status</th></tr></thead><tbody id="lib-body"></tbody></table>
    </div>
  </div>
  <script>
    let _servicesCache = [];
    let _pollTimer = null;
    let _sseAbort = null;
    function authHeaders() {{
      const tok = sessionStorage.getItem('abdl_token') || '';
      return tok ? {{ 'Authorization': 'Bearer ' + tok }} : {{}};
    }}
    function saveToken() {{
      const el = document.getElementById('api-token');
      if (el) sessionStorage.setItem('abdl_token', el.value.trim());
      refreshAll();
      connectSse();
    }}
    async function apiFetch(url, opts = {{}}) {{
      opts.headers = Object.assign({{'Content-Type': 'application/json'}}, authHeaders(), opts.headers || {{}});
      const res = await fetch(url, opts);
      const data = await res.json().catch(() => ({{}}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }}
    function currentSubmissionOptions() {{
      return {{
        output_format: document.getElementById('fmt-select').value || null,
        output_preset: document.getElementById('preset-select').value || null,
        profile: document.getElementById('profile-select').value || null,
        combine: document.getElementById('combine-cb').checked,
        enrich_metadata: document.getElementById('enrich-cb').checked,
      }};
    }}
    async function submitUrls() {{
      const raw = document.getElementById('urls-input').value;
      const urls = raw.split(/\\s+/).map(s => s.trim()).filter(Boolean);
      const msg = document.getElementById('submit-msg');
      if (!urls.length) {{ msg.textContent = 'Please enter at least one URL.'; return; }}
      try {{
        const data = await apiFetch('/api/download', {{
          method: 'POST',
          body: JSON.stringify(Object.assign({{urls}}, currentSubmissionOptions())),
        }});
        msg.textContent = 'Queued ' + data.queued + ' job(s) (total ' + data.total + ').';
      }} catch (e) {{ msg.textContent = 'Error: ' + e.message; }}
    }}
    async function togglePause() {{ await apiFetch('/api/pause', {{method: 'POST', body: '{{}}'}}); }}
    async function cancelRun() {{ await apiFetch('/api/cancel', {{method: 'POST', body: '{{}}'}}); }}
    async function triggerSync() {{
      const msg = document.getElementById('submit-msg');
      msg.textContent = 'Starting library sync...';
      try {{
        await apiFetch('/api/sync', {{method: 'POST', body: '{{}}'}});
        msg.textContent = 'Library sync started in background.';
      }} catch (e) {{ msg.textContent = 'Sync failed: ' + e.message; }}
    }}
    function syncProfilesForSelectedService() {{
      const svcKey = document.getElementById('svc-select').value;
      const svc = _servicesCache.find(s => s.key === svcKey);
      const profSel = document.getElementById('profile-select');
      const profiles = (svc && Array.isArray(svc.profiles) && svc.profiles.length) ? svc.profiles : ['default'];
      const cur = profSel.value;
      profSel.innerHTML = profiles.map(p => `<option value="${{escapeAttr(p)}}">${{escapeHtml(p)}}</option>`).join('');
      if (profiles.includes(cur)) profSel.value = cur;
    }}
    async function loadServices() {{
      try {{
        const svcs = await apiFetch('/api/services');
        _servicesCache = Array.isArray(svcs) ? svcs : [];
        const sel = document.getElementById('svc-select');
        sel.innerHTML = _servicesCache.map(s => `<option value="${{escapeAttr(s.key)}}">${{escapeHtml(s.label)}}</option>`).join('');
        syncProfilesForSelectedService();
      }} catch (_) {{}}
    }}
    document.getElementById('svc-select').addEventListener('change', syncProfilesForSelectedService);
    async function browseCatalog(mode) {{
      const svc = document.getElementById('svc-select').value;
      const profile = document.getElementById('profile-select').value || null;
      const query = document.getElementById('cat-query').value;
      const body = document.getElementById('cat-body');
      body.innerHTML = '<tr><td colspan="3" class="muted">Loading...</td></tr>';
      try {{
        const items = await apiFetch('/api/catalog', {{method: 'POST', body: JSON.stringify({{service: svc, profile, mode, query}})}});
        if (!items.length) {{ body.innerHTML = '<tr><td colspan="3" class="muted">No items found.</td></tr>'; return; }}
        body.innerHTML = items.map(b => `<tr><td>${{escapeHtml(b.title)}}</td><td>${{escapeHtml(b.author || '')}}</td><td><button type="button" class="cat-queue-btn" data-url="${{escapeAttr(b.url)}}">Queue</button></td></tr>`).join('');
      }} catch (e) {{ body.innerHTML = `<tr><td colspan="3" class="muted">Error: ${{escapeHtml(e.message)}}</td></tr>`; }}
    }}
    document.getElementById('cat-body').addEventListener('click', (ev) => {{
      const btn = ev.target.closest('.cat-queue-btn');
      if (btn && btn.dataset.url) queueSingle(btn.dataset.url);
    }});
    async function queueSingle(url) {{
      await apiFetch('/api/download', {{method: 'POST', body: JSON.stringify(Object.assign({{urls: [url]}}, currentSubmissionOptions()))}});
    }}
    async function queueAction(action, jobId) {{
      try {{
        if (action === 'up' || action === 'down') {{
          await apiFetch('/api/queue/move', {{method: 'POST', body: JSON.stringify({{job_id: jobId, direction: action}})}});
        }} else if (action === 'remove') {{
          await apiFetch('/api/queue/remove', {{method: 'POST', body: JSON.stringify({{job_id: jobId}})}});
        }} else if (action === 'retry') {{
          await apiFetch('/api/queue/retry', {{method: 'POST', body: JSON.stringify({{job_ids: [jobId]}})}});
        }}
      }} catch (e) {{ document.getElementById('submit-msg').textContent = 'Queue error: ' + e.message; }}
    }}
    async function retryAllFailed() {{
      try {{ await apiFetch('/api/queue/retry', {{method: 'POST', body: '{{}}'}}); }}
      catch (e) {{ document.getElementById('submit-msg').textContent = 'Retry error: ' + e.message; }}
    }}
    async function clearCompleted() {{
      try {{ await apiFetch('/api/queue/clear', {{method: 'POST', body: '{{}}'}}); }}
      catch (_) {{}}
    }}
    document.getElementById('queue-body').addEventListener('click', (ev) => {{
      const btn = ev.target.closest('.q-act-btn');
      if (btn && btn.dataset.action && btn.dataset.jobId) queueAction(btn.dataset.action, btn.dataset.jobId);
    }});
    function renderQueue(queueList) {{
      const qBody = document.getElementById('queue-body');
      qBody.innerHTML = (queueList && queueList.length)
        ? queueList.map(q => {{
            const jid = escapeAttr(q.job_id || '');
            const st = q.status || '';
            const label = q.title ? `${{q.title}} (${{q.url}})` : q.url;
            let btns = '';
            if (st === 'Queued') {{
              btns += `<button type="button" class="secondary q-act-btn" data-action="up" data-job-id="${{jid}}">↑</button> `;
              btns += `<button type="button" class="secondary q-act-btn" data-action="down" data-job-id="${{jid}}">↓</button> `;
            }}
            if (['Failed','Cancelled','Interrupted'].includes(st)) {{
              btns += `<button type="button" class="secondary q-act-btn" data-action="retry" data-job-id="${{jid}}">Retry</button> `;
            }}
            if (st !== 'Running') {{
              btns += `<button type="button" class="danger q-act-btn" data-action="remove" data-job-id="${{jid}}">Remove</button>`;
            }}
            return `<tr><td>${{escapeHtml(q.service)}}</td><td>${{escapeHtml(label)}}</td><td>${{escapeHtml(st)}}</td><td>${{escapeHtml(q.detail || '')}}</td><td>${{btns}}</td></tr>`;
          }}).join('')
        : '<tr><td colspan="5" class="muted">Queue is empty.</td></tr>';
    }}
    function renderPrompt(prompt) {{
      const banner = document.getElementById('prompt-banner');
      const msg = document.getElementById('prompt-message');
      const ctrls = document.getElementById('prompt-controls');
      if (!prompt) {{ banner.hidden = true; msg.textContent = ''; ctrls.innerHTML = ''; return; }}
      banner.hidden = false;
      msg.textContent = prompt.message || 'Input required';
      ctrls.innerHTML = '';
      const pid = prompt.id;
      if (prompt.kind === 'confirm') {{
        const yes = document.createElement('button'); yes.textContent = 'Confirm';
        yes.onclick = () => sendPromptResponse(pid, true, false);
        const no = document.createElement('button'); no.className = 'secondary'; no.textContent = 'Decline';
        no.onclick = () => sendPromptResponse(pid, false, false);
        ctrls.append(yes, no);
      }} else if (prompt.kind === 'choice') {{
        const sel = document.createElement('select');
        (prompt.choices || []).forEach((c, idx) => {{
          const opt = document.createElement('option'); opt.value = String(idx); opt.textContent = c;
          if (prompt.default === idx) opt.selected = true;
          sel.appendChild(opt);
        }});
        const ok = document.createElement('button'); ok.textContent = 'Select';
        ok.onclick = () => sendPromptResponse(pid, Number(sel.value), false);
        const cancel = document.createElement('button'); cancel.className = 'danger'; cancel.textContent = 'Cancel';
        cancel.onclick = () => sendPromptResponse(pid, null, true);
        ctrls.append(sel, ok, cancel);
      }} else {{
        const inp = document.createElement('input');
        inp.type = prompt.hidden ? 'password' : 'text';
        const ok = document.createElement('button'); ok.textContent = 'Submit';
        ok.onclick = () => sendPromptResponse(pid, inp.value, false);
        const cancel = document.createElement('button'); cancel.className = 'danger'; cancel.textContent = 'Cancel';
        cancel.onclick = () => sendPromptResponse(pid, null, true);
        ctrls.append(inp, ok, cancel);
      }}
    }}
    async function sendPromptResponse(id, value, cancelled) {{
      await apiFetch('/api/prompt/respond', {{method: 'POST', body: JSON.stringify({{id, value, cancelled}})}});
      renderPrompt(null);
    }}
    function applyProgress(p) {{
      const pct = p.progress_pct || 0;
      document.getElementById('status-line').textContent = (p.status || 'Ready') + (pct > 0 && p.stage ? ` (${{pct}}%)` : '');
      document.getElementById('stage-line').textContent = p.stage ? ('Stage: ' + p.stage) : '';
      document.getElementById('prog-bar').value = pct;
    }}
    function applySnapshot(s) {{
      applyProgress(s);
      document.getElementById('pause-btn').textContent = s.paused ? 'Resume' : 'Pause';
      renderQueue(s.queue);
      renderPrompt(s.pending_prompt || null);
      const box = document.getElementById('logs-box');
      box.textContent = (s.logs || []).join('\\n');
      box.scrollTop = box.scrollHeight;
    }}
    async function refreshStatus() {{
      try {{ applySnapshot(await apiFetch('/api/status')); }} catch (_) {{}}
    }}
    function startFallbackPolling() {{
      if (_pollTimer === null) _pollTimer = setInterval(refreshStatus, 2500);
    }}
    function stopFallbackPolling() {{
      if (_pollTimer !== null) {{ clearInterval(_pollTimer); _pollTimer = null; }}
    }}
    async function connectSse() {{
      if (_sseAbort) _sseAbort.abort();
      _sseAbort = new AbortController();
      const badge = document.getElementById('sse-badge');
      try {{
        const res = await fetch('/api/events', {{headers: authHeaders(), signal: _sseAbort.signal}});
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        stopFallbackPolling();
        if (badge) badge.textContent = '(live)';
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {{
          const {{value, done}} = await reader.read();
          if (done) break;
          buf += dec.decode(value, {{stream: true}});
          const parts = buf.split('\\n\\n');
          buf = parts.pop() || '';
          for (const frame of parts) {{
            let evName = 'message';
            let dataStr = '';
            for (const line of frame.split('\\n')) {{
              if (line.startsWith('event:')) evName = line.slice(6).trim();
              else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
            }}
            if (!dataStr) continue;
            const payload = JSON.parse(dataStr);
            if (evName === 'snapshot' || evName === 'status') applySnapshot(payload);
            else if (evName === 'progress') applyProgress(payload);
            else if (evName === 'queue') renderQueue(payload.queue);
            else if (evName === 'prompt') renderPrompt(payload.prompt || null);
            else if (evName === 'log') {{
              const box = document.getElementById('logs-box');
              const lines = (payload.lines || []).join('\\n');
              if (lines) box.textContent = (box.textContent ? box.textContent + '\\n' : '') + lines;
              box.scrollTop = box.scrollHeight;
              if (payload.status) document.getElementById('status-line').textContent = payload.status;
            }}
          }}
        }}
      }} catch (err) {{
        if (err && err.name === 'AbortError') return;
        if (badge) badge.textContent = '(polling)';
        startFallbackPolling();
        setTimeout(connectSse, 3000);
      }}
    }}
    async function refreshLibrary() {{
      try {{
        const items = await apiFetch('/api/library?limit=50');
        const body = document.getElementById('lib-body');
        body.innerHTML = items.length
          ? items.map(e => `<tr><td>${{escapeHtml(e.source)}}</td><td>${{escapeHtml(e.title)}}</td><td>${{escapeHtml(e.author || '')}}</td><td>${{escapeHtml(e.status)}}</td></tr>`).join('')
          : '<tr><td colspan="4" class="muted">No archive entries recorded.</td></tr>';
      }} catch (_) {{}}
    }}
    function escapeHtml(s) {{ return String(s).replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c])); }}
    function escapeAttr(s) {{ return escapeHtml(s); }}
    function refreshAll() {{ loadServices(); refreshStatus(); refreshLibrary(); }}
    refreshAll();
    connectSse();
  </script>
</body>
</html>"""


class _WebRequestHandler(BaseHTTPRequestHandler):
    """HTTP request router for the headless Web UI & JSON endpoints."""

    server_version = f"audiobook-dl/{__version__}"

    @property
    def state(self) -> WebDashboardState:
        return self.server.dashboard_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy per-poll stdout/stderr HTTP logs on headless terminals.
        return

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        sub_q = self.state.subscribe_events()
        try:
            while not self.state._stop_event.is_set():
                try:
                    event_name, payload = sub_q.get(timeout=5.0)
                    chunk = (
                        f"event: {event_name}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    ).encode()
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    break
        finally:
            self.state.unsubscribe_events(sub_q)

    def _authorized(self) -> bool:
        expected = self.state.token
        if not expected:
            return True
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
            if hmac.compare_digest(provided, expected):
                return True
        alt = (self.headers.get("X-API-Token") or "").strip()
        return bool(alt and hmac.compare_digest(alt, expected))

    def _validate_csrf(self) -> tuple[HTTPStatus, str] | None:
        host_hdr = (self.headers.get("Host") or "").strip().lower()
        raw_origin = self.headers.get("Origin")
        if raw_origin is not None:
            origin = raw_origin.strip()
            if not origin or origin.lower() == "null":
                return (HTTPStatus.FORBIDDEN, "Cross-origin request rejected")
            origin_netloc = urlparse(origin).netloc.lower()
            if not host_hdr or origin_netloc != host_hdr:
                return (HTTPStatus.FORBIDDEN, "Cross-origin request rejected")
        else:
            raw_referer = self.headers.get("Referer")
            if raw_referer is not None:
                ref_netloc = urlparse(raw_referer.strip()).netloc.lower()
                if not host_hdr or ref_netloc != host_hdr:
                    return (HTTPStatus.FORBIDDEN, "Cross-origin request rejected")
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return (HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        return None

    def _drain_small_body(self) -> None:
        raw_len = (self.headers.get("Content-Length") or "").strip()
        if raw_len.isdigit():
            length = int(raw_len)
            if 0 < length <= 65536:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
                return
            if length > 65536:
                self.close_connection = True

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length") or "0"
        if not raw_len.strip().isdigit():
            raise ValueError("Invalid Content-Length header")
        length = int(raw_len.strip())
        if length > MAX_REQUEST_BODY_BYTES:
            raise _PayloadTooLargeError("Request payload too large")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(HTTPStatus.OK, _render_dashboard_html(bool(self.state.token)))
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "version": __version__})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        if path == "/api/status":
            self._send_json(HTTPStatus.OK, self.state.status_snapshot())
            return
        if path == "/api/services":
            self._send_json(HTTPStatus.OK, self.state.services_payload())
            return
        if path == "/api/events":
            self._serve_sse()
            return
        if path == "/api/library":
            qs = parse_qs(parsed.query)
            query_text = (qs.get("q") or [""])[0]
            raw_limit = (qs.get("limit") or ["100"])[0]
            limit = int(raw_limit) if raw_limit.isdigit() else 100
            archive_path = self.state.archive_path()
            if not os.path.exists(archive_path):
                self._send_json(HTTPStatus.OK, [])
                return
            entries = DownloadArchive(archive_path).list_entries(
                query=query_text,
                limit=max(1, min(500, limit)),
            )
            self._send_json(HTTPStatus.OK, [asdict(e) for e in entries])
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authorized():
            self._drain_small_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        csrf_err = self._validate_csrf()
        if csrf_err is not None:
            self._drain_small_body()
            status_code, message = csrf_err
            self._send_json(status_code, {"error": message})
            return
        try:
            body = self._read_json_body()
        except _PayloadTooLargeError as exc:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/download":
            try:
                urls = body.get("urls")
                if isinstance(urls, str):
                    urls = [urls]
                if not isinstance(urls, list):
                    raise ValueError("'urls' must be a list of URL strings")
                queued, total = self.state.queue_urls(urls, body)
                self._send_json(HTTPStatus.ACCEPTED, {"queued": queued, "total": total})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/pause":
            paused = self.state.controller.toggle_pause()
            self.state.broadcast_status()
            self._send_json(HTTPStatus.OK, {"paused": paused})
            return

        if path == "/api/cancel":
            self.state.controller.cancel_active_run()
            self.state.cancel_sync()
            with self.state._lock:
                prompt_clear = self.state._clear_active_prompts_locked()
            if prompt_clear is not None:
                self.state._broadcast_event("prompt", prompt_clear)
            self.state.broadcast_status()
            self._send_json(HTTPStatus.OK, {"cancelling": True})
            return

        if path == "/api/queue/clear":
            cleared_count = self.state.clear_queue()
            self._send_json(HTTPStatus.OK, {"cleared": cleared_count})
            return

        if path == "/api/queue/remove":
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'job_id' is required"})
                return
            removed, new_total, is_active = self.state.remove_job(job_id)
            if is_active:
                self._send_json(HTTPStatus.CONFLICT, {"error": "Cannot remove an actively running job"})
                return
            if not removed:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job not found: {job_id}"})
                return
            self._send_json(HTTPStatus.OK, {"removed": True, "total": new_total})
            return

        if path == "/api/queue/retry":
            raw_ids = body.get("job_ids")
            if raw_ids is None and body.get("job_id"):
                raw_ids = [body.get("job_id")]
            if raw_ids is not None and not isinstance(raw_ids, list):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'job_ids' must be a list"})
                return
            job_ids = [str(i).strip() for i in raw_ids if str(i).strip()] if isinstance(raw_ids, list) else None
            try:
                queued, total = self.state.retry_jobs(job_ids)
                self._send_json(HTTPStatus.ACCEPTED, {"queued": queued, "total": total})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/queue/move":
            job_id = str(body.get("job_id") or "").strip()
            direction = str(body.get("direction") or "").strip().lower()
            if not job_id or direction not in ("up", "down", "top", "bottom"):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "'job_id' and direction ('up'|'down'|'top'|'bottom') are required"},
                )
                return
            moved = self.state.move_job(job_id, direction)
            self._send_json(HTTPStatus.OK, {"moved": moved})
            return

        if path == "/api/prompt/respond":
            prompt_id = str(body.get("id") or "").strip()
            if not prompt_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'id' is required"})
                return
            resolved = self.state.respond_prompt(
                prompt_id,
                body.get("value"),
                cancelled=bool(body.get("cancelled", False)),
            )
            if not resolved:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Prompt not found or already resolved"})
                return
            self._send_json(HTTPStatus.OK, {"resolved": True})
            return

        if path == "/api/catalog":
            service_key = str(body.get("service") or "").strip()
            mode = str(body.get("mode") or "search").strip().lower()
            query_text = str(body.get("query") or "")
            if not service_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'service' is required"})
                return
            cfg = self.state.controller.config_snapshot()
            opts = self.state.build_download_options([], body)
            if body.get("profile") == DEFAULT_PROFILE:
                opts.profile = None
            try:
                if mode == "library":
                    books = list_service_library(service_key, opts, cfg)
                else:
                    books = search_service(service_key, query_text, opts, cfg)
                self._send_json(HTTPStatus.OK, [asdict(b) for b in books])
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/sync":
            try:
                self.state.start_sync(body)
                self._send_json(HTTPStatus.ACCEPTED, {"syncing": True})
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})


def create_web_server(serve_options: args.ServeOptions) -> ThreadingHTTPServer:
    """Create and bind a headless Web UI `ThreadingHTTPServer`."""
    state = WebDashboardState(serve_options)
    server = ThreadingHTTPServer((serve_options.host, serve_options.port), _WebRequestHandler)
    server.dashboard_state = state  # type: ignore[attr-defined]
    return server


def serve_web_ui(serve_options: args.ServeOptions) -> int:
    """Run the headless Web UI HTTP server until interrupted."""
    server = create_web_server(serve_options)
    bound_host, bound_port = server.server_address[:2]
    host_str = bound_host.decode("utf-8", errors="replace") if isinstance(bound_host, bytes) else str(bound_host)
    print(f"audiobook-dl Web UI listening on http://{host_str}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.dashboard_state.shutdown()  # type: ignore[attr-defined]
        server.server_close()
    return 0

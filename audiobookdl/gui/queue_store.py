"""Persistent download/pack queue records and `gui_queue.json` state for the GUI."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import json
import os
import threading
from typing import TYPE_CHECKING, Any
import uuid

from audiobookdl.config import Config
from audiobookdl.gui.event_bus import GuiInteractionHandler
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.pack import PackOverrides
from audiobookdl.utils.archive import resolve_database_directory
from audiobookdl.utils.audiobook import Chapter
from audiobookdl.utils.fs import atomic_write_text

if TYPE_CHECKING:
    from audiobookdl.sources.source import Source
    from audiobookdl.utils.audiobook import Result


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
    def from_dict(cls, raw: Any) -> PackJobRequest | None:
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
    interaction: GuiInteractionHandler
    selected_indices: list[int] | None = None
    prefetched: tuple[Source, Result] | None = None
    pack_request: PackJobRequest | None = None
    baseline_options: DownloadOptions | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.baseline_options is None:
            copied = replace(self.options)
            copied._explicit_fields = frozenset(getattr(self.options, "_explicit_fields", set()))
            self.baseline_options = copied


EDITABLE_OVERRIDE_KEYS = (
    "output_format",
    "combine",
    "split_chapters",
    "intro_chapter_mode",
    "media_type",
    "output_template",
    "audio_bitrate",
    "sample_rate",
    "overwrite",
    "skip_downloaded",
    "write_abs_metadata",
    "upload_to_abs",
)


class GuiQueueStore:
    """Owns queue records, pending job deque, and `gui_queue.json` persistence."""

    UNSET_VALUE: Any = object()
    EDITABLE_JOB_STATUSES = frozenset({"Queued", "Paused", "Failed", "Cancelled", "Interrupted"})
    EDITABLE_OVERRIDE_KEYS = EDITABLE_OVERRIDE_KEYS

    def __init__(
        self,
        *,
        config_snapshot: Callable[[], Config],
        config_location: str | None = None,
    ) -> None:
        self._config_snapshot = config_snapshot
        self.config_location = config_location
        self.queue_lock = threading.RLock()
        self.queue_cond = threading.Condition(self.queue_lock)
        self.pending_jobs: deque[QueuedJob | None] = deque()
        self.queue_records: dict[str, dict[str, Any]] = {}
        self._saved_queue_loaded = False
        self._save_defer_lock = threading.Lock()
        self._save_defer_depth = 0
        self._save_deferred = False

    @staticmethod
    def make_job_id() -> str:
        return uuid.uuid4().hex[:12]

    def queue_state_path(self) -> str:
        """Return path to `gui_queue.json` inside the effective database directory."""
        db_dir = resolve_database_directory(
            self._config_snapshot(),
            config_location=self.config_location,
        )
        return os.path.join(db_dir, "gui_queue.json")

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
                entry["selected_indices"] = [
                    i for i in selected_indices if isinstance(i, int) and not isinstance(i, bool)
                ]
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

    def request_save_queue_state(self) -> None:
        """Persist immediately, or coalesce until the active `drain_events` batch finishes."""
        with self._save_defer_lock:
            if self._save_defer_depth > 0:
                self._save_deferred = True
                return
        self.save_queue_state()

    @contextmanager
    def defer_queue_saves(self) -> Iterator[None]:
        """Defer `save_queue_state` calls until the wrapped block exits, then flush once."""
        with self._save_defer_lock:
            self._save_defer_depth += 1
        should_flush = False
        try:
            yield
        finally:
            with self._save_defer_lock:
                self._save_defer_depth -= 1
                if self._save_defer_depth == 0 and self._save_deferred:
                    self._save_deferred = False
                    should_flush = True
            if should_flush:
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
        explicit_fields: frozenset[str] = getattr(job.options, "_explicit_fields", frozenset())
        job_overrides: dict[str, Any] = {}
        for attr in self.EDITABLE_OVERRIDE_KEYS:
            if is_pack and attr not in explicit_fields:
                continue
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
            existing = self.queue_records.get(job.job_id)
            if existing is None:
                self.queue_records[job.job_id] = rec
            else:
                if service_label and not existing.get("service"):
                    existing["service"] = service_label
                rec = dict(existing)
        if persist:
            self.request_save_queue_state()
        return rec

    def get_queue_record(self, job_id: str) -> dict[str, Any] | None:
        """Return a shallow copy of the queue record for `job_id`, if present."""
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            return dict(rec) if rec is not None else None

    def update_queue_record(self, job_id: str, status: str, detail: str = "") -> dict[str, Any] | None:
        snapshot: dict[str, Any] | None = None
        with self.queue_lock:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            rec["status"] = status
            rec["detail"] = detail
            snapshot = dict(rec)
        self.request_save_queue_state()
        return snapshot

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
            self.request_save_queue_state()
        return snapshot

    def update_queue_job_config(
        self,
        job_id: str,
        *,
        profile: Any = UNSET_VALUE,
        job_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update editable `profile` and/or `job_overrides` on a non-running queue job."""
        snapshot: dict[str, Any] | None = None
        with self.queue_cond:
            rec = self.queue_records.get(job_id)
            if rec is None:
                return None
            if str(rec.get("status") or "") not in self.EDITABLE_JOB_STATUSES:
                return None

            norm_prof: str | None = None
            has_profile_update = profile is not self.UNSET_VALUE
            if has_profile_update:
                cleaned_prof = str(profile or "").strip().lower()
                if not cleaned_prof or cleaned_prof == "default":
                    rec.pop("profile", None)
                    norm_prof = None
                else:
                    rec["profile"] = cleaned_prof
                    norm_prof = cleaned_prof

            updated_overrides: dict[str, Any] | None = None
            cleared_keys: set[str] = set()
            if job_overrides is not None:
                current_ov = dict(rec.get("job_overrides") or {})
                for key in self.EDITABLE_OVERRIDE_KEYS:
                    if key not in job_overrides:
                        continue
                    val = job_overrides[key]
                    if val is None or val == "":
                        current_ov.pop(key, None)
                        cleared_keys.add(key)
                    else:
                        current_ov[key] = val
                if current_ov:
                    rec["job_overrides"] = current_ov
                else:
                    rec.pop("job_overrides", None)
                updated_overrides = current_ov
                if isinstance(rec.get("pack_request"), dict):
                    if "output_format" in updated_overrides:
                        rec["pack_request"]["output_format"] = updated_overrides["output_format"]
                    elif "output_format" in cleared_keys:
                        rec["pack_request"]["output_format"] = None

            default_options = DownloadOptions() if cleared_keys else None
            for pending in self.pending_jobs:
                if pending is None or pending.job_id != job_id:
                    continue
                if has_profile_update:
                    pending.options.profile = norm_prof
                if updated_overrides is not None:
                    baseline = pending.baseline_options
                    baseline_explicit: frozenset[str] = (
                        getattr(baseline, "_explicit_fields", frozenset())
                        if baseline is not None
                        else frozenset()
                    )
                    explicit = set(getattr(pending.options, "_explicit_fields", set()))
                    for key in self.EDITABLE_OVERRIDE_KEYS:
                        if key in updated_overrides:
                            setattr(pending.options, key, updated_overrides[key])
                            explicit.add(key)
                        elif key in cleared_keys:
                            if baseline is not None and key not in baseline_explicit:
                                setattr(pending.options, key, getattr(baseline, key))
                            elif default_options is not None:
                                setattr(pending.options, key, getattr(default_options, key))
                            explicit.discard(key)
                    pending.options._explicit_fields = frozenset(explicit)
                    if pending.pack_request is not None:
                        if "output_format" in updated_overrides:
                            pending.pack_request = replace(
                                pending.pack_request,
                                output_format=str(updated_overrides["output_format"]),
                            )
                        elif "output_format" in cleared_keys:
                            pending.pack_request = replace(
                                pending.pack_request,
                                output_format=None,
                            )
            snapshot = dict(rec)

        self.request_save_queue_state()
        return snapshot

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

    def clear_completed_records(
        self,
        ordered_ids: Sequence[str],
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[str]:
        target_statuses = (
            set(statuses)
            if statuses is not None
            else {"Completed", "Failed", "Cancelled", "Interrupted"}
        )
        with self.queue_lock:
            removable = [
                iid
                for iid in ordered_ids
                if self.queue_records.get(iid, {}).get("status") in target_statuses
            ]
            for iid in removable:
                self.queue_records.pop(iid, None)
        self.save_queue_state()
        return removable

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

"""Single-pass library synchronization used by the desktop GUI."""

from dataclasses import dataclass, field
import threading
from typing import Any
from collections.abc import Sequence

from audiobookdl import logging
from audiobookdl.config import (
    DEFAULT_PROFILE,
    Config,
    list_configured_source_profiles,
    load_config,
    normalize_profile_name,
    normalize_source_key,
)
from audiobookdl.exceptions import NoSourceFound
from audiobookdl.options_schema import DownloadOptions, SyncOptions, build_default_options
from audiobookdl.pipeline import (
    Downloader,
    apply_runtime_options,
    list_service_library,
)
from audiobookdl.sources import find_source_by_name
from audiobookdl.utils.archive import DownloadArchive, make_catalog_book_key, resolve_archive_path


@dataclass
class ProfileSyncStats:
    """Sync statistics for one `(service, profile)` target."""

    service: str
    profile: str
    discovered: int = 0
    newly_added: int = 0
    pending_count: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "profile": self.profile,
            "discovered": self.discovered,
            "newly_added": self.newly_added,
            "pending_count": self.pending_count,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "error": self.error,
        }


@dataclass
class SyncCycleResult:
    """Aggregated outcome of a single sync pass across all selected profiles."""

    profiles_checked: int = 0
    books_discovered: int = 0
    newly_added: int = 0
    pending_count: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    details: list[ProfileSyncStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles_checked": self.profiles_checked,
            "books_discovered": self.books_discovered,
            "newly_added": self.newly_added,
            "pending_count": self.pending_count,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "errors": list(self.errors),
            "details": [d.to_dict() for d in self.details],
        }


def _lookup_source_class(service_name: str):
    try:
        return find_source_by_name(service_name)
    except NoSourceFound:
        return None


def _resolve_sync_targets(
    config: Config,
    requested_services: Sequence[str],
    requested_profile: str | None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve `(service_key, profile_name)` targets to synchronize."""
    norm_req_services = [normalize_source_key(s) for s in requested_services if s.strip()]
    norm_req_profile = normalize_profile_name(requested_profile) if requested_profile else None

    configured = [(svc, prof) for svc, prof, _cfg in list_configured_source_profiles(config)]
    targets: list[tuple[str, str]] = []
    errors: list[str] = []
    seen = set()

    if norm_req_services:
        for req_svc in norm_req_services:
            cls = _lookup_source_class(req_svc)
            if cls is None:
                errors.append(f"Unknown service: {req_svc}")
                continue
            if not getattr(cls, "supports_library", False):
                errors.append(f"Service '{req_svc}' does not support library listing")
                continue
            matched = [
                (svc, prof)
                for svc, prof in configured
                if svc == req_svc and (norm_req_profile is None or prof == norm_req_profile)
            ]
            if not matched:
                matched = [(req_svc, norm_req_profile or DEFAULT_PROFILE)]
            for pair in matched:
                if pair not in seen:
                    seen.add(pair)
                    targets.append(pair)
    else:
        for svc, prof in configured:
            if norm_req_profile is not None and prof != norm_req_profile:
                continue
            cls = _lookup_source_class(svc)
            if cls is None or not getattr(cls, "supports_library", False):
                continue
            if (svc, prof) not in seen:
                seen.add((svc, prof))
                targets.append((svc, prof))

    return targets, errors


def _build_sync_download_options(
    sync_opts: SyncOptions,
    config: Config,
    archive_path: str,
    profile: str,
) -> DownloadOptions:
    base_opts = build_default_options(
        config_location=sync_opts.config_location,
        download_archive=archive_path,
        skip_downloaded=True,
        profile=None if profile == DEFAULT_PROFILE else profile,
        output_template=sync_opts.output_template,
        output_format=sync_opts.output_format,
    )
    apply_runtime_options(base_opts, config)
    return base_opts


def run_sync_once(
    sync_opts: SyncOptions,
    *,
    config: Config | None = None,
    cancel_event: threading.Event | None = None,
    interaction: logging.InteractionHandler | None = None,
    sink: logging.LogSink | None = None,
) -> SyncCycleResult:
    """Perform one sync cycle: poll account libraries, update ledger, download new pending items."""
    resolved_config = config if config is not None else load_config(sync_opts.config_location)
    archive_path = resolve_archive_path(resolved_config, sync_opts.archive_path)

    targets, target_errors = _resolve_sync_targets(
        resolved_config,
        sync_opts.services,
        sync_opts.profile,
    )
    cycle = SyncCycleResult()
    cycle.errors.extend(target_errors)
    archive = DownloadArchive(archive_path)

    for svc, prof in targets:
        if cancel_event is not None and cancel_event.is_set():
            cycle.cancelled = True
            break
        cycle.profiles_checked += 1
        stats = ProfileSyncStats(service=svc, profile=prof)
        try:
            dl_opts = _build_sync_download_options(sync_opts, resolved_config, archive_path, prof)
            if interaction is not None:
                books = list_service_library(svc, dl_opts, resolved_config, interaction=interaction)
            else:
                books = list_service_library(svc, dl_opts, resolved_config)
            stats.discovered = len(books)
            cycle.books_discovered += len(books)

            if not sync_opts.dry_run and books:
                inserted, _updated = archive.upsert_catalog_items(svc, books, profile=prof)
                stats.newly_added = inserted
                cycle.newly_added += inserted

            pending_urls: list[str] = []
            for book in books:
                if not book.url:
                    continue
                item_source = (book.source or svc).strip().lower() or svc
                book_key = make_catalog_book_key(item_source, book)
                entry = archive.get_entry_by_key(item_source, book_key)
                if entry is not None and (entry.status in ("downloaded", "trashed") or entry.is_hidden):
                    stats.skipped += 1
                    cycle.skipped += 1
                    continue
                if book.url not in pending_urls:
                    pending_urls.append(book.url)

            stats.pending_count = len(pending_urls)
            cycle.pending_count += len(pending_urls)

            if pending_urls and not sync_opts.scan_only and not sync_opts.dry_run:
                dl_kwargs: dict[str, Any] = {
                    "config": resolved_config,
                    "options": dl_opts,
                    "cancel_event": cancel_event,
                }
                if sink is not None:
                    dl_kwargs["sink"] = sink
                if interaction is not None:
                    dl_kwargs["interaction"] = interaction
                dl_summary = Downloader(**dl_kwargs).download(pending_urls)
                stats.downloaded = dl_summary.books_completed
                stats.failed = dl_summary.failed
                cycle.downloaded += dl_summary.books_completed
                cycle.failed += dl_summary.failed
                if dl_summary.cancelled:
                    cycle.cancelled = True
        except Exception as exc:
            msg = f"{svc} ({prof}): {exc}"
            stats.error = str(exc)
            stats.failed += 1
            cycle.failed += 1
            cycle.errors.append(msg)
        cycle.details.append(stats)
        if cycle.cancelled:
            break

    return cycle

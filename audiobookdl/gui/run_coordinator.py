"""Download, pack, retry, pause, and cancellation orchestration for `AudiobookDLApp`."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from audiobookdl.gui.controller import GuiController, PackJobRequest, QueuedJob
from audiobookdl.gui.formatting import parse_url_text
from audiobookdl.gui.queue_store import EDITABLE_OVERRIDE_KEYS
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources.source import Source
from audiobookdl.utils.audiobook import Result

if TYPE_CHECKING:
    from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
    from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
    from audiobookdl.gui.options_state import OptionsCoordinator
    from audiobookdl.gui.pages import DownloadPage
    from audiobookdl.gui.shell import SidebarShell


class RunCoordinator:
    """Coordinates active download/pack runs, queue submission, retries, and run completion."""

    def __init__(
        self,
        root: tk.Tk,
        controller: GuiController,
        *,
        auth: ServiceAuthCoordinator,
        ffmpeg_assistant: FFmpegAssistant,
        options: OptionsCoordinator,
        shell: SidebarShell,
        status_var: tk.StringVar,
        queue_concurrency_var: tk.IntVar,
        notify_on_complete_var: tk.BooleanVar,
        is_previewing_provider: Callable[[], bool],
        is_syncing_provider: Callable[[], bool],
        schedule_poll_soon: Callable[[], None],
        show_view_in_download_action: Callable[[], None],
    ) -> None:
        self._root = root
        self._controller = controller
        self._auth = auth
        self._ffmpeg_assistant = ffmpeg_assistant
        self._options = options
        self._shell = shell
        self._status_var = status_var
        self._queue_concurrency_var = queue_concurrency_var
        self._notify_on_complete_var = notify_on_complete_var
        self._is_previewing_provider = is_previewing_provider
        self._is_syncing_provider = is_syncing_provider
        self._schedule_poll_soon = schedule_poll_soon
        self._show_view_in_download_action = show_view_in_download_action

        self.is_running = False

    def _download_page(self) -> DownloadPage:
        return self._shell.ensure_page("download")

    def apply_download_page_running_state(self, dl_page: DownloadPage, running: bool) -> None:
        for widget in dl_page.output_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state="disabled" if running else "normal")
        dl_page.set_options_panel_enabled(not running)

    def set_running_state(self, running: bool) -> None:
        self.is_running = running
        dl_page = self._shell.get_page("download")
        if dl_page is not None:
            self.apply_download_page_running_state(dl_page, running)
            dl_page.queue_panel.set_running_state(
                running,
                is_previewing=self._is_previewing_provider(),
                is_syncing=self._is_syncing_provider(),
            )
        self._auth.set_running_state(running)
        self._shell.set_footer_running_state(
            running=running,
            paused=self._controller.is_paused(),
            previewing=self._is_previewing_provider(),
            syncing=self._is_syncing_provider(),
        )
        if running:
            self._schedule_poll_soon()

    def on_queue_concurrency_changed(self, value: int) -> None:
        clamped = self._controller.set_queue_concurrency(value)
        if self._queue_concurrency_var.get() != clamped:
            self._queue_concurrency_var.set(clamped)
        try:
            self._controller.save_gui_preferences(queue_concurrency=clamped)
        except OSError as exc:
            self._status_var.set(f"Failed to save concurrency preference: {exc}")

    def toggle_pause_queue(self) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        self._download_page().queue_panel.toggle_pause(is_running=self.is_running)
        self._shell.set_footer_running_state(
            running=self.is_running,
            paused=self._controller.is_paused(),
            previewing=self._is_previewing_provider(),
            syncing=self._is_syncing_provider(),
        )

    def cancel_download(self) -> None:
        dl_page = self._download_page()
        queue_panel = dl_page.queue_panel
        if self._is_previewing_provider() and not self.is_running:
            self._status_var.set("Cancelling preview...")
            queue_panel.cancel_btn.configure(state="disabled")
            self._shell.footer_cancel_btn.configure(state="disabled")
            queue_panel.append_log("Cancelling metadata preview...")
            self._controller.cancel_preview()
            return
        if self._is_syncing_provider() and not self.is_running:
            self._status_var.set("Cancelling library sync...")
            queue_panel.cancel_btn.configure(state="disabled")
            self._shell.footer_cancel_btn.configure(state="disabled")
            queue_panel.append_log("Cancelling library sync...")
            self._controller.cancel_library_sync()
            return
        if not self.is_running:
            return
        self._status_var.set("Cancelling...")
        queue_panel.cancel_btn.configure(state="disabled")
        queue_panel.download_btn.configure(state="disabled")
        self._shell.footer_cancel_btn.configure(state="disabled")
        self._controller.cancel_active_run()

    def start_new_run(
        self,
        job_specs: Sequence[
            tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ],
    ) -> None:
        """Initialize UI and queue state for a fresh download run."""
        queue_panel = self._download_page().queue_panel
        run_id, queued_jobs = self._controller.start_new_run(
            job_specs,
            service_label_resolver=queue_panel.service_label_for_url,
        )
        self.set_running_state(True)
        queue_panel.prepare_run_ui_state(
            run_id,
            len(job_specs),
            f"--- Starting download ({len(job_specs)} URL{'s' if len(job_specs) != 1 else ''}) ---",
        )
        self._register_queued_jobs(queued_jobs)

    def enqueue_additional_urls(
        self,
        urls: list[str],
        *,
        allow_existing: bool = False,
        profile_override: str | None = None,
    ) -> bool:
        """Append new URLs (or retried URLs when `allow_existing=True`) to the active queue."""
        if not self._auth.validate_cookie_file():
            return True
        if self._options.try_build_options(urls_override=[]) is None:
            return True

        def _build_for_url(u: str) -> DownloadOptions:
            opts = self._options.build_options(urls_override=[u])
            if profile_override:
                opts.profile = profile_override
            return opts

        queue_panel = self._download_page().queue_panel
        has_active_run, added_jobs, new_total = self._controller.enqueue_additional_urls(
            urls,
            _build_for_url,
            allow_existing=allow_existing,
            service_label_resolver=queue_panel.service_label_for_url,
        )
        if not has_active_run:
            return False
        if not added_jobs:
            self._status_var.set("URL(s) already in active queue")
            queue_panel.append_log("All entered URL(s) are already in the active queue.")
            return True
        self._register_queued_jobs(added_jobs)
        queue_panel.ui_item_total = new_total
        queue_panel.refresh_batch_label()
        self._status_var.set(f"Queued +{len(added_jobs)} (total {new_total})")
        queue_panel.append_log(
            f"--- Added {len(added_jobs)} URL{'s' if len(added_jobs) != 1 else ''} to queue (total {new_total}) ---"
        )
        return True

    def register_enqueued_batch(
        self,
        *,
        was_appended: bool,
        run_id: int,
        queued_jobs: Sequence[QueuedJob],
        new_total: int,
        start_header: str,
        append_status: str,
        append_log_msg: str,
    ) -> None:
        if not was_appended and not queued_jobs:
            return
        queue_panel = self._download_page().queue_panel
        if was_appended:
            self._register_queued_jobs(queued_jobs)
            queue_panel.ui_item_total = new_total
            queue_panel.refresh_batch_label()
            self._status_var.set(append_status)
            queue_panel.append_log(append_log_msg)
            return

        self.set_running_state(True)
        queue_panel.prepare_run_ui_state(
            run_id,
            len(queued_jobs),
            start_header,
        )
        self._register_queued_jobs(queued_jobs)

    def _register_queued_jobs(
        self,
        queued_jobs: Sequence[QueuedJob],
        *,
        persist: bool = False,
    ) -> None:
        """Register queued jobs in the queue panel and persist queue state once."""
        if not queued_jobs:
            return
        queue_panel = self._download_page().queue_panel
        for job in queued_jobs:
            queue_panel.register_queue_job(job, persist=persist)
        self._controller.save_queue_state()

    @staticmethod
    def apply_job_overrides_to_options(
        opts: DownloadOptions,
        rec: Mapping[str, Any] | None,
    ) -> DownloadOptions:
        if not isinstance(rec, Mapping):
            return opts
        job_overrides = rec.get("job_overrides")
        if not isinstance(job_overrides, Mapping):
            return opts
        return opts.with_overrides(job_overrides, allowed_keys=EDITABLE_OVERRIDE_KEYS)

    def build_retry_download_spec(
        self,
        rec: dict[str, Any],
    ) -> tuple[str, DownloadOptions, list[int] | None, None]:
        url = str(rec.get("url") or "").strip()
        opts = self._options.build_options(urls_override=[url])
        profile = str(rec.get("profile") or "").strip()
        if profile:
            opts.profile = profile
        opts = self.apply_job_overrides_to_options(opts, rec)
        raw_indices = rec.get("selected_indices")
        selected_indices = (
            [i for i in raw_indices if isinstance(i, int) and not isinstance(i, bool)]
            if isinstance(raw_indices, list)
            else None
        )
        return (url, opts, selected_indices, None)

    @classmethod
    def build_retry_pack_options(
        cls,
        base_opts: DownloadOptions,
        req: PackJobRequest,
        rec: Mapping[str, Any] | None = None,
    ) -> DownloadOptions:
        overrides = rec.get("job_overrides") if isinstance(rec, Mapping) else None
        opts = base_opts.with_overrides(
            overrides if isinstance(overrides, Mapping) else {},
            allowed_keys=EDITABLE_OVERRIDE_KEYS,
        )
        override_fmt = (
            overrides.get("output_format") if isinstance(overrides, Mapping) else None
        )
        fmt = override_fmt or req.output_format or base_opts.output_format or "m4b"
        opts.output_format = fmt
        opts._explicit_fields = frozenset(opts._explicit_fields | {"output_format"})
        return opts

    def enqueue_imported_queue_records(self, items: Sequence[dict[str, Any]]) -> None:
        base_opts = self._options.try_build_options(urls_override=[])
        if base_opts is None:
            return
        dl_specs: list[tuple[str, DownloadOptions, list[int] | None, None]] = []
        pack_specs: list[tuple[PackJobRequest, DownloadOptions]] = []
        for rec in items:
            job_kind = str(rec.get("job_kind") or "download")
            if job_kind == "pack" and isinstance(rec.get("pack_request"), dict):
                req = PackJobRequest.from_dict(rec["pack_request"])
                if req is not None:
                    pack_specs.append((req, self.build_retry_pack_options(base_opts, req, rec)))
            else:
                url = str(rec.get("url") or "").strip()
                if url:
                    dl_specs.append(self.build_retry_download_spec(rec))
        if not dl_specs and not pack_specs:
            self._status_var.set("No valid jobs found in imported queue")
            return
        if not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        queue_panel = self._download_page().queue_panel
        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs,
            pack_requests=pack_specs,
            service_label_resolver=queue_panel.service_label_for_url,
        )
        self.register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Imported {len(queued_jobs)} queued job(s) ---",
            append_status=f"Imported +{len(queued_jobs)} job(s) (total {new_total})",
            append_log_msg=f"--- Imported {len(queued_jobs)} job(s) into queue (total {new_total}) ---",
        )

    def retry_failed_queue_items(self, job_ids: Sequence[str] | None = None) -> None:
        queue_panel = self._download_page().queue_panel
        target_ids = list(job_ids) if job_ids is not None else list(queue_panel.queue_order)
        retry_records = self._controller.retryable_jobs(target_ids)
        if not retry_records:
            self._status_var.set("No failed or cancelled jobs to retry")
            return

        download_recs: list[dict[str, Any]] = []
        seen_download_keys: set[tuple[str, str, tuple[int, ...] | None]] = set()
        pack_entries: list[tuple[PackJobRequest, dict[str, Any]]] = []
        for rec in retry_records:
            if rec.get("job_kind", "download") == "pack":
                parsed = PackJobRequest.from_dict(rec.get("pack_request"))
                if parsed is not None:
                    pack_entries.append((parsed, rec))
            else:
                url = str(rec.get("url") or "").strip()
                if not url:
                    continue
                prof = str(rec.get("profile") or "").strip()
                raw_idx = rec.get("selected_indices")
                idx_key = (
                    tuple(
                        i for i in raw_idx if isinstance(i, int) and not isinstance(i, bool)
                    )
                    if isinstance(raw_idx, list)
                    else None
                )
                dedup_key = (url, prof, idx_key)
                if dedup_key not in seen_download_keys:
                    seen_download_keys.add(dedup_key)
                    download_recs.append(rec)

        if not download_recs and not pack_entries:
            self._status_var.set("No failed or cancelled jobs to retry")
            return

        if self.is_running and self._controller.cancel_event.is_set():
            return
        if download_recs and not self._auth.validate_cookie_file():
            return
        base_opts = self._options.try_build_options(urls_override=[])
        if base_opts is None:
            return
        pack_pairs: list[tuple[PackJobRequest, DownloadOptions]] = []
        for req, rec in pack_entries:
            opts = self.build_retry_pack_options(base_opts, req, rec)
            if not self._ffmpeg_assistant.ensure_available(opts):
                return
            pack_pairs.append((req, opts))
        if download_recs and not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        self._options.save_run_preferences(base_opts)
        dl_specs = [self.build_retry_download_spec(rec) for rec in download_recs]

        if not self.is_running and dl_specs and not pack_pairs:
            self.start_new_run(dl_specs)
            return

        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs,
            pack_requests=pack_pairs,
            service_label_resolver=queue_panel.service_label_for_url,
        )
        self.register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Retrying {len(queued_jobs)} job(s) ---",
            append_status=f"Queued +{len(queued_jobs)} retry job(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} retry job(s) to queue (total {new_total}) ---",
        )

    def on_pack_submitted(self, requests_list: list[PackJobRequest], output_format: str) -> None:
        """Queue one or more local repackaging requests onto `GuiController`."""
        if not requests_list:
            return
        base_opts = self._options.try_build_options(urls_override=[])
        if base_opts is None:
            return
        self._options.save_run_preferences(base_opts)
        pairs: list[tuple[PackJobRequest, DownloadOptions]] = []
        for req in requests_list:
            fmt = output_format or req.output_format or "m4b"
            opts = dataclasses.replace(base_opts, output_format=fmt)
            opts._explicit_fields = frozenset(base_opts._explicit_fields | {"output_format"})
            pairs.append((req, opts))

        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_pack_jobs(
            pairs
        )
        self.register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Starting local pack ({len(queued_jobs)} book{'s' if len(queued_jobs) != 1 else ''}) ---",
            append_status=f"Queued +{len(queued_jobs)} pack job(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} local pack job(s) to queue ---",
        )
        self._show_view_in_download_action()

    def start_or_enqueue_urls(
        self,
        urls: list[str],
        *,
        profile_override: str | None = None,
    ) -> None:
        if self._is_previewing_provider() or not urls:
            return
        if self.is_running and self._controller.cancel_event.is_set():
            return
        if not self._auth.validate_cookie_file():
            return
        if not self._auth.warn_mixed_service_credentials(urls):
            return
        options = self._options.try_build_options(urls_override=urls)
        if options is None:
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            return
        self._options.save_run_preferences(options)

        if self.is_running:
            if self.enqueue_additional_urls(urls, profile_override=profile_override):
                return

        unique_urls = list(dict.fromkeys(urls))
        job_specs: list[
            tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]
        ] = []
        for u in unique_urls:
            job_opts = self._options.build_options(urls_override=[u])
            if profile_override:
                job_opts.profile = profile_override
            job_specs.append((u, job_opts, None, None))
        self.start_new_run(job_specs)

    def start_download(self) -> None:
        if self._is_previewing_provider():
            return
        urls_text = self._download_page().urls_text
        urls = parse_url_text(urls_text.get("1.0", tk.END))
        if self.is_running:
            if self._controller.cancel_event.is_set():
                return
            if not urls:
                self._status_var.set("Enter at least one URL to queue")
                urls_text.focus_set()
                return
            if self.enqueue_additional_urls(urls):
                return

        if not urls:
            self._status_var.set("Enter at least one URL")
            messagebox.showwarning(
                "Missing URL", "Please enter at least one audiobook URL.", parent=self._root
            )
            urls_text.focus_set()
            return

        self.start_or_enqueue_urls(urls)

    def finish_run(self, summary: DownloadSummary | None, exc: Exception | None) -> None:
        self.set_running_state(False)
        self._controller.clear_seen_urls()
        self._download_page().queue_panel.finish_run(
            summary,
            exc,
            notify_enabled=self._notify_on_complete_var.get(),
        )
        lib_page = self._shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()

"""Library synchronization, auto-sync scheduling, and Audiobookshelf upload coordination."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Any

from audiobookdl.config import VALID_AUTO_SYNC_MODES, make_source_profile_key
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.notifications import send_os_notification_async
from audiobookdl.options_schema import DownloadOptions, SyncOptions
from audiobookdl.utils.archive import resolve_archive_path

if TYPE_CHECKING:
    from audiobookdl.gui.coordinators import AutomationCoordinator
    from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
    from audiobookdl.gui.options_state import OptionsCoordinator
    from audiobookdl.gui.shell import SidebarShell


class SyncCoordinator:
    """Coordinates library sync runs, sync-to-queue discovery, and Audiobookshelf uploads."""

    def __init__(
        self,
        root: tk.Tk,
        controller: GuiController,
        *,
        automation: AutomationCoordinator,
        ffmpeg_assistant: FFmpegAssistant,
        options: OptionsCoordinator,
        shell: SidebarShell,
        status_var: tk.StringVar,
        is_running_provider: Callable[[], bool],
        is_previewing_provider: Callable[[], bool],
        is_in_tray_provider: Callable[[], bool] | None=None,
        schedule_poll_soon: Callable[[], None],
        append_log: Callable[[str], None],
        register_enqueued_batch: Callable[..., None],
    ) -> None:
        self._root = root
        self._controller = controller
        self._automation = automation
        self._ffmpeg_assistant = ffmpeg_assistant
        self._options = options
        self._shell = shell
        self._status_var = status_var
        self._is_running_provider = is_running_provider
        self._is_previewing_provider = is_previewing_provider
        self._is_in_tray_provider = is_in_tray_provider or (lambda: False)
        self._schedule_poll_soon = schedule_poll_soon
        self._append_log = append_log
        self._register_enqueued_batch = register_enqueued_batch

        self.is_syncing = False
        self._last_sync_unattended = False

    def _is_window_hidden(self) -> bool:
        try:
            return bool(self._is_in_tray_provider())
        except Exception:
            return False

    def archive_ledger_path(self) -> str:
        return resolve_archive_path(self._controller.config_snapshot())

    def set_syncing_state(
        self, syncing: bool, *, status_text: str | None=None
    ) -> None:
        self.is_syncing = syncing
        dl_page = self._shell.get_page("download")
        if dl_page is not None:
            dl_page.queue_panel.set_running_state(
                self._is_running_provider(),
                is_previewing=self._is_previewing_provider(),
                is_syncing=syncing,
            )
        lib_page = self._shell.get_page("library")
        if lib_page is not None:
            lib_page.sync_tab.set_syncing_state(syncing, status_text=status_text)
        self._shell.set_footer_running_state(
            running=self._is_running_provider(),
            paused=self._controller.is_paused(),
            previewing=self._is_previewing_provider(),
            syncing=syncing,
        )
        if syncing:
            self._schedule_poll_soon()

    def upload_archive_entry_to_abs(self, entry: Any) -> None:
        if not entry.output_path:
            return
        self._status_var.set(f"Uploading '{entry.title}' to Audiobookshelf...")
        self._append_log(f"Uploading to Audiobookshelf: {entry.title}")
        authors = [a.strip() for a in (entry.author or "").split(";") if a.strip()]
        self._controller.start_abs_upload(
            entry.output_path,
            title=entry.title,
            authors=authors,
            series=entry.series,
        )
        self._schedule_poll_soon()

    def execute_sync_targets(
        self,
        targets: Sequence[tuple[str, str]],
        mode: str,
        *,
        unattended: bool=False,
    ) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in VALID_AUTO_SYNC_MODES:
            raise ValueError(
                f"Invalid auto_sync_mode {mode!r}; expected one of {VALID_AUTO_SYNC_MODES}"
            )
        if normalized == "enqueue":
            self.sync_and_enqueue_targets(targets, unattended=unattended)
        elif normalized == "dry_run":
            self.sync_libraries(
                services=[s for s, _p in targets],
                scan_only=True,
                dry_run=True,
                unattended=unattended,
            )
        else:
            self.sync_libraries(
                services=[s for s, _p in targets],
                scan_only=True,
                unattended=unattended,
            )

    def save_sync_schedule(
        self,
        enabled: bool,
        minutes: int,
        mode: str,
        targets: list[tuple[str, str]],
    ) -> None:
        norm_minutes = max(1, minutes)
        norm_mode = str(mode or "").strip().lower()
        if norm_mode not in VALID_AUTO_SYNC_MODES:
            raise ValueError(
                f"Invalid auto_sync_mode {mode!r}; expected one of {VALID_AUTO_SYNC_MODES}"
            )
        self._automation.auto_sync_mode = norm_mode
        self._automation.auto_sync_targets = list(targets)
        self._automation.configure_auto_sync_timer(enabled, norm_minutes)
        serialized_targets = [make_source_profile_key(s, p) for s, p in targets]
        try:
            self._controller.save_gui_preferences(
                auto_sync_enabled=enabled,
                auto_sync_minutes=norm_minutes,
                auto_sync_mode=norm_mode,
                auto_sync_targets=serialized_targets,
            )
        except OSError as exc:
            self._status_var.set(f"Failed to save auto-sync schedule: {exc}")
            return
        if enabled:
            self._status_var.set(f"Saved auto-sync schedule (every {norm_minutes} min)")
        else:
            self._status_var.set("Saved auto-sync schedule (disabled)")

    def sync_and_enqueue_targets(
        self,
        targets: Sequence[tuple[str, str]],
        *,
        unattended: bool=False,
    ) -> None:
        if self._is_running_provider():
            self._status_var.set("Wait for active downloads before syncing libraries")
            return
        if self._is_previewing_provider():
            self._status_var.set("Wait for metadata preview before syncing libraries")
            return
        if self.is_syncing:
            self._status_var.set("Library sync already in progress")
            return
        options = self._options.try_build_options(urls_override=[])
        if options is None:
            return
        self._last_sync_unattended = bool(
            unattended
            or getattr(self._automation, "is_auto_sync_running", False)
            or self._is_window_hidden()
        )
        self.set_syncing_state(True)
        self._status_var.set("Scanning libraries for pending books...")
        self._append_log("--- Scanning libraries for pending books ---")
        sync_opts = SyncOptions(
            services=[s for s, _p in targets],
            scan_only=True,
            output_template=options.output_template,
            output_format=options.output_format,
            archive_path=self.archive_ledger_path(),
            config_location=self._controller.config_location,
        )
        self._controller.start_sync_enqueue_discovery(targets, sync_opts)

    def sync_libraries(
        self,
        *,
        services: Sequence[str]=(),
        scan_only: bool=False,
        dry_run: bool=False,
        unattended: bool=False,
    ) -> None:
        """Run one library synchronization cycle in the background."""
        if self._is_running_provider():
            self._status_var.set("Wait for active downloads before syncing libraries")
            return
        if self._is_previewing_provider():
            self._status_var.set("Wait for metadata preview before syncing libraries")
            return
        if self.is_syncing:
            self._status_var.set("Library sync already in progress")
            return
        options = self._options.try_build_options(urls_override=[])
        if options is None:
            return
        self._last_sync_unattended = bool(
            unattended
            or getattr(self._automation, "is_auto_sync_running", False)
            or self._is_window_hidden()
        )
        self.set_syncing_state(True)
        self._status_var.set("Syncing libraries...")
        self._append_log("--- Starting library sync ---")
        sync_opts = SyncOptions(
            services=list(services),
            scan_only=scan_only,
            dry_run=dry_run,
            output_template=options.output_template,
            output_format=options.output_format,
            archive_path=self.archive_ledger_path(),
            config_location=self._controller.config_location,
        )
        self._controller.start_library_sync(sync_opts)

    def _update_sync_tab_status(self, status_text: str) -> None:
        lib_page = self._shell.get_page("library")
        if lib_page is not None:
            lib_page.sync_tab.set_syncing_state(False, status_text=status_text)

    def handle_sync_done(self, cycle: Any, exc: Exception | None) -> None:
        self.set_syncing_state(False)
        unattended = self._last_sync_unattended or self._is_window_hidden()
        self._last_sync_unattended = False
        if exc is not None:
            self._status_var.set("Library sync failed")
            self._update_sync_tab_status(f"Last sync: Failed ({exc})")
            self._append_log(f"[ERROR] Library sync failed: {exc}")
            if unattended:
                send_os_notification_async("Library Sync Failed", str(exc))
            else:
                messagebox.showerror("Library Sync Failed", str(exc), parent=self._root)
            return
        if cycle is None:
            self._status_var.set("Library sync finished")
            self._update_sync_tab_status("Last sync: Finished")
            return
        if getattr(cycle, "cancelled", False):
            self._status_var.set("Library sync cancelled")
            self._update_sync_tab_status("Last sync: Cancelled")
            self._append_log("--- Library sync cancelled ---")
            return
        msg = (
            f"Discovered {cycle.books_discovered}, new {cycle.newly_added}, "
            f"downloaded {cycle.downloaded}, skipped {cycle.skipped}, failed {cycle.failed}"
        )
        self._status_var.set(f"Sync complete: {msg}")
        self._update_sync_tab_status(f"Last sync: {msg}")
        self._append_log(f"--- Library sync finished: {msg} ---")
        for err in getattr(cycle, "errors", ()):
            self._append_log(f"[WARN] {err}")
        lib_page = self._shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()
        if unattended:
            title = (
                "Library Sync Finished with Warnings"
                if (cycle.failed > 0 or getattr(cycle, "errors", ()))
                else "Library Sync Complete"
            )
            send_os_notification_async(title, msg)
        elif cycle.failed > 0 or getattr(cycle, "errors", ()):
            messagebox.showwarning("Library Sync Finished with Warnings", msg, parent=self._root)
        else:
            messagebox.showinfo("Library Sync Complete", msg, parent=self._root)

    def handle_sync_enqueue_ready(
        self,
        pending_groups: Sequence[tuple[str, str, list[str]]],
        stats: dict[str, Any],
        exc: Exception | None,
    ) -> None:
        self.set_syncing_state(False)
        unattended = self._last_sync_unattended or self._is_window_hidden()
        self._last_sync_unattended = False
        if exc is not None:
            self._status_var.set("Library sync failed")
            self._update_sync_tab_status(f"Last sync: Failed ({exc})")
            self._append_log(f"[ERROR] Library sync failed: {exc}")
            if unattended:
                send_os_notification_async("Library Sync Failed", str(exc))
            else:
                messagebox.showerror("Library Sync Failed", str(exc), parent=self._root)
            return
        if stats.get("cancelled"):
            self._status_var.set("Library sync cancelled")
            self._update_sync_tab_status("Last sync: Cancelled")
            self._append_log("--- Library sync cancelled ---")
            return
        for err in stats.get("errors") or ():
            self._append_log(f"[WARN] {err}")
        lib_page = self._shell.get_page("library")
        if lib_page is not None:
            lib_page.archive_tab.refresh()
        total_urls = sum(len(urls) for _svc, _prof, urls in pending_groups)
        if total_urls == 0:
            discovered = stats.get("discovered", 0)
            self._status_var.set(f"Sync complete: {discovered} discovered, 0 pending")
            self._update_sync_tab_status(
                f"Last sync: {discovered} discovered, 0 pending"
            )
            self._append_log(
                f"--- Library sync finished: {discovered} discovered, 0 pending ---"
            )
            if unattended:
                send_os_notification_async(
                    "Library Sync Complete", f"{discovered} discovered, 0 pending"
                )
            return
        base_opts = self._options.try_build_options(urls_override=[])
        if base_opts is None:
            return
        if not unattended and not self._ffmpeg_assistant.ensure_available(base_opts):
            return
        dl_specs: list[tuple[str, DownloadOptions, list[int] | None, None]] = []
        for _svc, prof, urls in pending_groups:
            for u in urls:
                opts = self._options.build_options(urls_override=[u])
                if prof:
                    opts.profile = prof
                dl_specs.append((u, opts, None, None))
        dl_page = self._shell.get_page("download")
        was_appended, run_id, queued_jobs, new_total = self._controller.start_or_enqueue_jobs(
            download_specs=dl_specs,
            service_label_resolver=(
                dl_page.queue_panel.service_label_for_url if dl_page is not None else None
            ),
        )
        self._register_enqueued_batch(
            was_appended=was_appended,
            run_id=run_id,
            queued_jobs=queued_jobs,
            new_total=new_total,
            start_header=f"--- Sync queued {len(queued_jobs)} pending book(s) ---",
            append_status=f"Queued +{len(queued_jobs)} synced book(s) (total {new_total})",
            append_log_msg=f"--- Added {len(queued_jobs)} synced book(s) to queue (total {new_total}) ---",
        )
        self._update_sync_tab_status(f"Last sync: Queued {len(queued_jobs)} book(s)")
        if unattended and queued_jobs:
            send_os_notification_async(
                "Library Sync Queued Books",
                f"Queued {len(queued_jobs)} pending book(s) for download",
            )

    def sync_from_tray(self) -> None:
        if self._automation.auto_sync_targets:
            self.execute_sync_targets(
                self._automation.auto_sync_targets,
                self._automation.auto_sync_mode,
                unattended=True,
            )
        else:
            self.sync_libraries(unattended=True)

"""Controller event queue polling, deferred activity logs, modal prompts, and update checks."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import logging as stdlib_logging
import tkinter as tk
from tkinter import messagebox, simpledialog
from typing import TYPE_CHECKING, Any

from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.events import (
    AbsUploadDoneEvent,
    AccountsVerificationDoneEvent,
    AccountsVerificationItemEvent,
    AuthVerifiedEvent,
    BatchItemEvent,
    CatalogLoadedEvent,
    ControllerEvent,
    DownloadEstimateEvent,
    FfmpegInstallDoneEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogBatchEvent,
    LogEvent,
    OAuthConnectedEvent,
    OAuthDisconnectedEvent,
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
from audiobookdl.gui.preview_dialog import ChoiceDialog
from audiobookdl.gui.queue_panel import ActivityQueuePanel
from audiobookdl.gui.theme import ThemePalette
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources.source import Source
from audiobookdl.utils.audiobook import Result
from audiobookdl.utils.updater import UpdateInfo

if TYPE_CHECKING:
    from audiobookdl.gui.pages import CatalogTab
    from audiobookdl.gui.shell import SidebarShell


class EventDispatcher:
    """Drains `GuiController` events onto the Tk thread and handles modal/update interactions."""

    def __init__(
        self,
        root: tk.Tk,
        controller: GuiController,
        *,
        shell: SidebarShell,
        status_var: tk.StringVar,
        active_job_var: tk.StringVar,
        progress_var: tk.DoubleVar,
        check_updates_var: tk.BooleanVar,
        palette_provider: Callable[[], ThemePalette],
        is_running_provider: Callable[[], bool],
        is_previewing_provider: Callable[[], bool],
        is_syncing_provider: Callable[[], bool],
        on_preview_done: Callable[[str, tuple[Source, Result] | None, Exception | None], None],
        on_auth_verified: Callable[[str, bool, Exception | None], None],
        on_oauth_connected: Callable[[str, str, str, bool, Exception | None], None] | None = None,
        on_oauth_disconnected: Callable[[str, str, str, bool, Exception | None], None] | None = None,
        on_ffmpeg_install_done: Callable[[bool, str], None],
        on_sync_done: Callable[[Any, Exception | None], None],
        on_sync_enqueue_ready: Callable[
            [Sequence[tuple[str, str, list[str]]], dict[str, Any], Exception | None], None
        ],
        on_run_done: Callable[[DownloadSummary | None, Exception | None], None],
        is_tray_running_provider: Callable[[], bool] | None = None,
        drain_tray_actions: Callable[[], int] | None = None,
    ) -> None:
        self._root = root
        self._controller = controller
        self._shell = shell
        self._status_var = status_var
        self._active_job_var = active_job_var
        self._progress_var = progress_var
        self._check_updates_var = check_updates_var
        self._palette_provider = palette_provider
        self._is_running_provider = is_running_provider
        self._is_previewing_provider = is_previewing_provider
        self._is_syncing_provider = is_syncing_provider
        self._on_preview_done = on_preview_done
        self._on_auth_verified = on_auth_verified
        self._on_oauth_connected = on_oauth_connected
        self._on_oauth_disconnected = on_oauth_disconnected
        self._on_ffmpeg_install_done = on_ffmpeg_install_done
        self._on_sync_done = on_sync_done
        self._on_sync_enqueue_ready = on_sync_enqueue_ready
        self._on_run_done = on_run_done
        self._is_tray_running_provider = is_tray_running_provider
        self._drain_tray_actions = drain_tray_actions

        self._pending_log_entries: deque[tuple[str | None, str]] = deque(
            maxlen=ActivityQueuePanel.MAX_LOG_LINES
        )
        self._poll_after_id: str | None = None
        self._modal_prompt_active = False
        self._deferred_modal_events: deque[ControllerEvent] = deque()

    def start_polling(self) -> None:
        self._controller.set_activity_callback(self.schedule_poll_soon)
        self._poll_after_id = self._root.after(50, self.poll_queue)

    def refresh_saved_queue_badge(self) -> None:
        records = self._controller.load_saved_queue_records()
        self.update_download_badge_from_records(records)

    def update_download_badge_from_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        active = sum(
            1 for r in records if str(r.get("status") or "") in ("Running", "Queued", "Paused")
        )
        failed = sum(1 for r in records if str(r.get("status") or "") in ("Failed", "Interrupted"))
        self._shell.set_download_badge(active, failed)

    def append_log(self, text: str, *, level: str | None = None) -> None:
        dl_page = self._shell.get_page("download")
        if dl_page is not None:
            if level is None:
                dl_page.queue_panel.append_log(text)
            else:
                dl_page.queue_panel.append_log_batch([(level, text)])
            return
        self._pending_log_entries.append((level, text))

    def flush_pending_logs(self, panel: ActivityQueuePanel) -> None:
        if not self._pending_log_entries:
            return
        entries: list[tuple[str, str]] = []
        for level, text in self._pending_log_entries:
            if level is None:
                tag = panel._classify_log_tag("INFO", text)
                resolved = {"error": "ERROR", "warn": "WARNING", "debug": "DEBUG"}.get(tag, "INFO")
                entries.append((resolved, text))
            else:
                entries.append((level, text))
        self._pending_log_entries.clear()
        panel.append_log_batch(entries)

    def handle_deferred_activity_event(self, item: ControllerEvent) -> bool:
        """Handle activity events without forcing lazy `DownloadPage` creation."""
        if isinstance(item, LogEvent):
            prefix = f"[{item.level}] " if item.level in ("DEBUG", "ERROR") else ""
            self.append_log(f"{prefix}{item.message}", level=item.level)
            if item.level == "INFO" and item.message.strip():
                self._status_var.set(item.message.strip().splitlines()[0][:80])
            return True
        if isinstance(item, LogBatchEvent):
            last_info = ""
            for level, message in item.entries:
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                self.append_log(f"{prefix}{message}", level=level)
                if level == "INFO" and message.strip():
                    last_info = message
            if last_info:
                self._status_var.set(last_info.strip().splitlines()[0][:80])
            return True
        if isinstance(item, JobStatusEvent):
            self._controller.update_queue_record(item.job_id, item.status, item.detail)
            with self._controller.queue_lock:
                records = list(self._controller.queue_records.values())
            self.update_download_badge_from_records(records)
            return True
        if isinstance(item, JobMetaEvent):
            self._controller.update_queue_metadata(
                item.job_id,
                title=item.title or None,
                output_path=item.output_path or None,
            )
            if item.title:
                self._active_job_var.set(item.title.strip()[:60])
            return True
        if isinstance(item, ProgressStartEvent):
            self._progress_var.set(0.0)
            self._status_var.set(item.description[:80])
            if item.description.strip():
                self._active_job_var.set(item.description.strip()[:60])
            return True
        if isinstance(item, ProgressAdvanceEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self._progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressMetricsEvent):
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            self._progress_var.set(min(100.0, max(0.0, pct)))
            return True
        if isinstance(item, ProgressFinishEvent):
            self._progress_var.set(100.0)
            return True
        return isinstance(item, (BatchItemEvent, ItemProgressEvent, DownloadEstimateEvent))

    def schedule_poll_soon(self, delay_ms: int = 10) -> None:
        self.cancel_poll_timer()
        try:
            if self._root.winfo_exists():
                self._poll_after_id = self._root.after(delay_ms, self.poll_queue)
        except tk.TclError:
            self._poll_after_id = None

    def _log_dispatch_error(self, context: str, exc: Exception) -> None:
        stdlib_logging.getLogger(__name__).exception("Unhandled error in %s", context, exc_info=exc)
        try:
            self.append_log(f"[ERROR] Dispatch error ({context}): {exc}", level="ERROR")
        except Exception:
            pass

    def _safe_handle_event(self, item: ControllerEvent) -> None:
        try:
            self.handle_event(item)
        except Exception as exc:
            self._log_dispatch_error(type(item).__name__, exc)

    def _run_modal_with_polling(self, fn: Callable[[], object]) -> None:
        was_active = self._modal_prompt_active
        self._modal_prompt_active = True
        self.schedule_poll_soon(50)
        try:
            fn()
        finally:
            self._modal_prompt_active = was_active

    def poll_queue(self) -> None:
        self._poll_after_id = None
        try:
            if not self._root.winfo_exists():
                return
        except tk.TclError:
            return

        processed = 0
        try:
            processed = self._controller.drain_events(
                self._safe_handle_event,
                max_batch=self._controller.MAX_QUEUE_BATCH,
            )
            while not self._modal_prompt_active and self._deferred_modal_events:
                self._safe_handle_event(self._deferred_modal_events.popleft())
            if (
                self._is_tray_running_provider is not None
                and self._is_tray_running_provider()
                and self._drain_tray_actions is not None
            ):
                try:
                    self._drain_tray_actions()
                except Exception as exc:
                    self._log_dispatch_error("tray actions", exc)
            if self._is_running_provider():
                dl_page = self._shell.get_page("download")
                if dl_page is not None:
                    try:
                        dl_page.queue_panel.update_metrics_display(is_running=True)
                    except Exception as exc:
                        self._log_dispatch_error("metrics update", exc)
        finally:
            try:
                if self._root.winfo_exists():
                    tray_running = (
                        self._is_tray_running_provider is not None
                        and self._is_tray_running_provider()
                    )
                    if processed >= self._controller.MAX_QUEUE_BATCH:
                        self.schedule_poll_soon(10)
                    elif (
                        self._modal_prompt_active
                        or self._is_running_provider()
                        or self._is_previewing_provider()
                        or self._is_syncing_provider()
                        or self._controller.has_active_background_tasks()
                        or bool(self._deferred_modal_events)
                        or not self._controller.event_queue.empty()
                    ):
                        self.schedule_poll_soon(50)
                    elif tray_running:
                        self.schedule_poll_soon(250)
                    else:
                        self.cancel_poll_timer()
            except tk.TclError:
                pass

    def cancel_poll_timer(self) -> None:
        after_id = getattr(self, "_poll_after_id", None)
        self._poll_after_id = None
        if after_id is not None:
            try:
                self._root.after_cancel(after_id)
            except tk.TclError:
                pass

    def handle_event(self, item: ControllerEvent) -> None:
        if isinstance(
            item,
            (
                LogEvent,
                LogBatchEvent,
                BatchItemEvent,
                JobStatusEvent,
                JobMetaEvent,
                ItemProgressEvent,
                ProgressStartEvent,
                ProgressAdvanceEvent,
                ProgressMetricsEvent,
                ProgressFinishEvent,
                DownloadEstimateEvent,
            ),
        ):
            dl_page = self._shell.get_page("download")
            if dl_page is not None:
                if dl_page.queue_panel.handle_activity_event(item):
                    return
            elif self.handle_deferred_activity_event(item):
                return
        if isinstance(item, (PromptInputEvent, PromptConfirmEvent, PromptChoiceEvent)):
            if self._modal_prompt_active:
                self._deferred_modal_events.append(item)
                return
            self._modal_prompt_active = True
            self.schedule_poll_soon(50)
            try:
                if isinstance(item, PromptInputEvent):
                    item.holder["value"] = simpledialog.askstring(
                        "Authentication Required",
                        f"{item.prompt}:",
                        show="*" if item.hidden else None,
                        parent=self._root,
                    )
                elif isinstance(item, PromptConfirmEvent):
                    item.holder["value"] = messagebox.askyesno(
                        "Confirm", item.message, parent=self._root
                    )
                elif isinstance(item, PromptChoiceEvent):
                    dlg = ChoiceDialog(
                        self._root,
                        item.title,
                        item.choices,
                        default=item.default_index,
                        palette=self._palette_provider(),
                    )
                    self._root.wait_window(dlg)
                    item.holder["value"] = dlg.selected_index
            finally:
                item.done.set()
                self._modal_prompt_active = False
        elif isinstance(item, PreviewDoneEvent):
            self._on_preview_done(item.url, item.prepared, item.error)
        elif isinstance(item, AuthVerifiedEvent):
            self._on_auth_verified(item.service_label, item.ok, item.error)
        elif isinstance(item, AccountsVerificationItemEvent):
            acct_page = self._shell.get_page("accounts")
            if acct_page is not None:
                acct_page.handle_verification_item(
                    item.run_id,
                    item.service_key,
                    item.profile,
                    item.ok,
                    item.error,
                )
        elif isinstance(item, AccountsVerificationDoneEvent):
            acct_page = self._shell.get_page("accounts")
            if acct_page is not None:
                acct_page.handle_verification_done(
                    item.run_id,
                    item.valid_count,
                    item.checked_count,
                    item.total_count,
                    item.cancelled,
                )
        elif isinstance(item, OAuthConnectedEvent):
            if self._on_oauth_connected is not None:
                self._on_oauth_connected(
                    item.service_key,
                    item.service_label,
                    item.profile,
                    item.ok,
                    item.error,
                )
        elif isinstance(item, OAuthDisconnectedEvent):
            if self._on_oauth_disconnected is not None:
                self._on_oauth_disconnected(
                    item.service_key,
                    item.service_label,
                    item.profile,
                    item.revoked,
                    item.error,
                )
        elif isinstance(item, CatalogLoadedEvent):
            if item.request_id != self._controller.catalog_request_id:
                return
            catalog_tab: CatalogTab = item.target_tab
            try:
                if not catalog_tab.winfo_exists():
                    return
            except tk.TclError:
                return
            if item.error is not None:
                err_text = str(item.error).strip() or type(item.error).__name__
                first_line = err_text.splitlines()[0].strip()
                catalog_tab.set_error_status(f"Error: {first_line}")
                self.append_log(f"[ERROR] Catalog fetch failed: {err_text}")
                self._run_modal_with_polling(
                    lambda: messagebox.showerror("Catalog Error", err_text, parent=self._root)
                )
            else:
                if isinstance(item.payload, tuple) and len(item.payload) == 2:
                    books, statuses = item.payload
                else:
                    books, statuses = item.payload, None
                catalog_tab.populate_items(books, statuses=statuses)
        elif isinstance(item, FfmpegInstallDoneEvent):
            self._on_ffmpeg_install_done(item.ok, item.detail)
        elif isinstance(item, UpdateCheckedEvent):
            self.handle_update_checked(item.info, item.interactive)
        elif isinstance(item, SyncDoneEvent):
            self._on_sync_done(item.cycle, item.error)
        elif isinstance(item, SyncEnqueueReadyEvent):
            self._on_sync_enqueue_ready(item.pending_groups, item.stats, item.error)
        elif isinstance(item, AbsUploadDoneEvent):
            if item.ok:
                self._status_var.set(f"Uploaded '{item.title}' to Audiobookshelf")
                self.append_log(f"Uploaded '{item.title}' to Audiobookshelf")
            else:
                self._status_var.set("Audiobookshelf upload failed")
                self.append_log(f"[ERROR] Audiobookshelf upload failed: {item.error_message}")
                self._run_modal_with_polling(
                    lambda: messagebox.showerror(
                        "Audiobookshelf Upload Failed",
                        str(item.error_message),
                        parent=self._root,
                    )
                )
        elif isinstance(item, RunDoneEvent):
            dl_page = self._shell.get_page("download")
            expected_run_id = (
                dl_page.queue_panel.current_ui_run_id
                if dl_page is not None
                else self._controller.latest_run_id()
            )
            if item.run_id == expected_run_id:
                self._on_run_done(item.summary, item.error)

    def schedule_update_check(self) -> None:
        """Run a quiet background update check at launch unless opted out."""
        if not self._check_updates_var.get() or self._controller.config.check_updates is False:
            return
        self._controller.start_update_check(interactive=False, respect_opt_out=True)

    def check_updates_interactive(self) -> None:
        """Check for newer releases."""
        self._status_var.set("Checking for updates...")
        self._controller.start_update_check(interactive=True, respect_opt_out=False)

    def handle_update_checked(self, info: UpdateInfo | None, interactive: bool) -> None:
        if info is None:
            if interactive:
                self._status_var.set("Update check failed")
                self._run_modal_with_polling(
                    lambda: messagebox.showwarning(
                        "Update Check",
                        "Could not reach update server to check for new releases.",
                        parent=self._root,
                    )
                )
            return
        if info.has_update:
            msg = (
                f"A new version of audiobook-dl is available: v{info.latest_version} "
                f"(current: v{info.current_version}).\n\n"
                f"Download: {info.release_url}"
            )
            self._status_var.set(f"Update available: v{info.latest_version}")
            self.append_log(f"Update available: v{info.latest_version} ({info.release_url})")
            if interactive:
                self._run_modal_with_polling(
                    lambda: messagebox.showinfo("Update Available", msg, parent=self._root)
                )
        elif interactive:
            self._status_var.set(f"Up to date (v{info.current_version})")
            self._run_modal_with_polling(
                lambda: messagebox.showinfo(
                    "Up to Date",
                    f"You are running the latest version (v{info.current_version}).",
                    parent=self._root,
                )
            )

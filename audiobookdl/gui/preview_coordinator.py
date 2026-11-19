"""Metadata preview fetching, series inspection, enrichment, and preview-to-queue handoff."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import os
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Any

from audiobookdl.exceptions import DownloadCancelled
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.coordinators import series_book_exists
from audiobookdl.gui.formatting import parse_url_text
from audiobookdl.gui.preview_dialog import PreviewDialog
from audiobookdl.gui.theme import ThemePalette
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.sources.source import Source
from audiobookdl.utils.archive import DownloadArchive, resolve_archive_path
from audiobookdl.utils.audiobook import Audiobook, Result, Series

if TYPE_CHECKING:
    from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
    from audiobookdl.gui.ffmpeg_assistant import FFmpegAssistant
    from audiobookdl.gui.options_state import OptionsCoordinator
    from audiobookdl.gui.pages import DownloadPage
    from audiobookdl.gui.shell import SidebarShell


class PreviewCoordinator:
    """Manages metadata preview background fetches and the `PreviewDialog` lifecycle."""

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
        active_job_var: tk.StringVar,
        remember_login_var: tk.BooleanVar,
        skip_downloaded_var: tk.BooleanVar,
        download_archive_var: tk.BooleanVar,
        palette_provider: Callable[[], ThemePalette],
        is_running_provider: Callable[[], bool],
        is_syncing_provider: Callable[[], bool],
        schedule_poll_soon: Callable[[], None],
        start_new_run: Callable[
            [Sequence[tuple[str, DownloadOptions, list[int] | None, tuple[Source, Result] | None]]],
            None,
        ],
    ) -> None:
        self._root = root
        self._controller = controller
        self._auth = auth
        self._ffmpeg_assistant = ffmpeg_assistant
        self._options = options
        self._shell = shell
        self._status_var = status_var
        self._active_job_var = active_job_var
        self._remember_login_var = remember_login_var
        self._skip_downloaded_var = skip_downloaded_var
        self._download_archive_var = download_archive_var
        self._palette_provider = palette_provider
        self._is_running_provider = is_running_provider
        self._is_syncing_provider = is_syncing_provider
        self._schedule_poll_soon = schedule_poll_soon
        self._start_new_run = start_new_run

        self.is_previewing = False
        self.dialog: PreviewDialog | None = None

    def _download_page(self) -> DownloadPage:
        return self._shell.ensure_page("download")

    def set_previewing_state(self, previewing: bool) -> None:
        self.is_previewing = previewing
        dl_page = self._shell.get_page("download")
        if dl_page is not None:
            dl_page.queue_panel.set_previewing_state(
                previewing, is_running=self._is_running_provider()
            )
        self._shell.set_footer_running_state(
            running=self._is_running_provider(),
            paused=self._controller.is_paused(),
            previewing=previewing,
            syncing=self._is_syncing_provider(),
        )
        if previewing:
            self._schedule_poll_soon()

    def preview_metadata(self, url_override: str | None = None) -> None:
        """Fetch book or series metadata for `url_override` or the first entered URL."""
        if self._is_running_provider() or self.is_previewing:
            return
        urls_text = self._download_page().urls_text
        urls = (
            [url_override.strip()]
            if (url_override and url_override.strip())
            else parse_url_text(urls_text.get("1.0", tk.END))
        )
        if not urls:
            self._status_var.set("Enter a URL to preview")
            messagebox.showwarning(
                "Missing URL",
                "Please enter an audiobook or series URL to preview.",
                parent=self._root,
            )
            urls_text.focus_set()
            return
        if not self._auth.validate_cookie_file():
            return

        if self._remember_login_var.get():
            self._auth.save_current_credentials(quiet=True)

        target_url = urls[0]
        queue_panel = self._download_page().queue_panel
        if len(urls) > 1:
            queue_panel.append_log(f"Previewing first URL: {target_url}")

        options = self._options.try_build_options(urls_override=[target_url])
        if options is None:
            return
        self.set_previewing_state(True)
        queue_panel._set_progress(0.0, indeterminate=True)
        self._status_var.set("Fetching preview...")
        self._active_job_var.set("Fetching preview...")
        queue_panel.append_log(f"--- Fetching preview: {target_url} ---")
        self._controller.start_preview(
            target_url,
            options,
            check_duplicate=bool(self._download_archive_var.get()),
        )

    def handle_preview_done(
        self,
        url: str,
        prepared: tuple[Source, Result] | None,
        exc: Exception | None,
        *,
        duplicate_checked: bool = False,
        duplicate_warning: str | None = None,
        existing_series_indices: frozenset[int] | None = None,
    ) -> None:
        self.set_previewing_state(False)
        self._controller.preview_interaction = None
        queue_panel = self._download_page().queue_panel
        queue_panel._set_progress(0.0)
        if not self._is_running_provider():
            self._active_job_var.set("Idle")

        if exc is not None:
            if isinstance(exc, DownloadCancelled):
                self._status_var.set("Preview cancelled")
                return
            self._status_var.set("Preview failed")
            queue_panel.append_log(f"[ERROR] {exc}")
            messagebox.showerror("Preview Failed", str(exc), parent=self._root)
            return

        if prepared is None:
            self._status_var.set("Preview failed")
            return

        _source, result = prepared
        try:
            root_exists = self._root.winfo_exists()
        except tk.TclError:
            root_exists = False
        if not root_exists:
            self.close_prefetched_source(prepared)
            return
        self._status_var.set(f"Preview ready: {result.title[:60]}")
        self.show_preview_dialog(
            url,
            _source,
            result,
            duplicate_checked=duplicate_checked,
            duplicate_warning=duplicate_warning,
            existing_series_indices=existing_series_indices,
        )

    @staticmethod
    def close_prefetched_source(prefetched: tuple[Source, Result] | None) -> None:
        if prefetched is None:
            return
        close_fn = getattr(prefetched[0], "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def show_preview_dialog(
        self,
        url: str,
        source: Source,
        result: Result,
        *,
        duplicate_checked: bool = False,
        duplicate_warning: str | None = None,
        existing_series_indices: frozenset[int] | None = None,
    ) -> PreviewDialog:
        """Display resolved `Audiobook` metadata/cover or a selectable `Series` checklist."""
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()

        def _prepare_enrichment_options() -> DownloadOptions | None:
            return self._options.try_build_options(urls_override=[url], parent=self.dialog)

        def _search_enrichment(book: Audiobook, opts: DownloadOptions | None) -> Any:
            resolved = opts if opts is not None else DownloadOptions(urls=[url])
            return self._controller.search_enrichment_for_audiobook(book, resolved)

        def _apply_enrichment(
            book: Audiobook, candidate: Any, opts: DownloadOptions | None
        ) -> Any:
            resolved = opts if opts is not None else DownloadOptions(urls=[url])
            return self._controller.apply_enrichment_candidate(book, candidate, resolved)

        cached_series_opts: DownloadOptions | None = None
        dir_stems_cache: dict[str, set[str]] = {}
        archive_titles_cache: set[str] | None = None
        series_title = result.title if isinstance(result, Series) else None
        existing_book_ids: set[int] | None = None
        if isinstance(result, Series):
            if existing_series_indices is not None:
                existing_book_ids = {
                    id(result.books[idx])
                    for idx in existing_series_indices
                    if 0 <= idx < len(result.books)
                }
            else:
                cached_series_opts = self._options.build_options()
                if cached_series_opts.download_archive and os.path.exists(cached_series_opts.download_archive):
                    with DownloadArchive(cached_series_opts.download_archive, persistent=True) as archive:
                        archive_titles_cache = {
                            e.title.strip().casefold()
                            for e in archive.list_entries(limit=5000, include_hidden=True)
                            if e.status == "downloaded" and e.title and e.title.strip()
                        }

        def _series_book_exists(book: Any) -> bool:
            if existing_book_ids is not None and not isinstance(book, Audiobook):
                return id(book) in existing_book_ids
            if existing_book_ids is not None and id(book) in existing_book_ids:
                return True
            nonlocal cached_series_opts
            if cached_series_opts is None:
                cached_series_opts = self._options.build_options()
            return series_book_exists(
                book,
                options=cached_series_opts,
                dir_stems_cache=dir_stems_cache,
                archive_titles_cache=archive_titles_cache,
                series_title=series_title,
            )

        resolved_duplicate_warning = duplicate_warning
        if not duplicate_checked and isinstance(result, Audiobook) and self._download_archive_var.get():
            archive_path = resolve_archive_path(self._controller.config_snapshot())
            dup = self._controller.find_cross_source_duplicate(
                result.metadata,
                source_name=source.name,
                archive_path=archive_path,
            )
            if dup is not None:
                dup_where = dup.get("output_path") or dup.get("title") or ""
                resolved_duplicate_warning = (
                    f"Duplicate Alert: Already in library from {dup['source']}"
                    + (f" ({dup_where})" if dup_where else "")
                )
        dialog = PreviewDialog(
            self._root,
            url,
            source,
            result,
            is_running=self._is_running_provider(),
            skip_downloaded=self._skip_downloaded_var.get(),
            series_book_exists=_series_book_exists,
            on_confirm=self.enqueue_from_preview,
            palette=self._palette_provider(),
            on_prepare_enrichment_options=_prepare_enrichment_options,
            on_search_enrichment=_search_enrichment,
            on_apply_enrichment=_apply_enrichment,
            duplicate_warning=resolved_duplicate_warning,
        )
        self.dialog = dialog
        return dialog

    def enqueue_from_preview(
        self,
        url: str,
        prefetched: tuple[Source, Result],
        selected_indices: list[int] | None,
    ) -> None:
        """Start or queue a download from an inspected preview result."""
        options = self._options.try_build_options(urls_override=[url])
        if options is None:
            self.close_prefetched_source(prefetched)
            return
        if not self._ffmpeg_assistant.ensure_available(options):
            self.close_prefetched_source(prefetched)
            return
        if self._is_running_provider():
            queue_panel = self._download_page().queue_panel
            enqueued = self._controller.enqueue_from_preview(
                url,
                options,
                prefetched,
                selected_indices,
                service_label=queue_panel._service_label_for_url(url),
            )
            if enqueued is not None:
                added_job, new_total = enqueued
                queue_panel.register_queue_job(added_job)
                queue_panel.ui_item_total = new_total
                queue_panel.refresh_batch_label()
                self._status_var.set(f"Queued preview item (total {new_total})")
                return

        self._options.save_run_preferences(options)
        self._start_new_run(
            [(url, self._options.build_options(urls_override=[url]), selected_indices, prefetched)]
        )

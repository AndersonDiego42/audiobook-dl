"""Download page combining URL inputs, output folder, collapsible naming/options, and queue/log."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections.abc import Callable, Sequence
from typing import Any

from audiobookdl.gui.formatting import TEMPLATE_PRESET_LABELS
from audiobookdl.gui.options_panel import OptionsPanel
from audiobookdl.gui.queue_panel import ActivityQueuePanel
from audiobookdl.gui.theme import ThemePalette, scale_px
from audiobookdl.gui.widgets import (
    CollapsibleSection,
    ScrollableFrame,
    attach_tooltip,
    attach_url_context_menu,
    bind_text_tab_navigation,
)
from audiobookdl.options_schema import OPTIONS_BY_NAME


TEMPLATE_TOKENS: tuple[tuple[str, str], ...] = (
    ("{title}", "Title"),
    ("{author}", "Author"),
    ("{narrator}", "Narrator"),
    ("{series}", "Series"),
    ("{series_order_padded}", "Series Order (01)"),
    ("{year}", "Release Year"),
    ("{source}", "Service"),
    ("{isbn}", "ISBN"),
    ("{asin}", "ASIN"),
    ("{series?{series}/}", "Conditional Series Folder"),
)


class DownloadPage(ttk.Frame):
    """Primary Download page with fit-to-content vertical splitter and minimum queue height."""

    MIN_TOP_DP = 150
    MIN_QUEUE_DP = 210

    def __init__(
        self,
        parent: tk.Misc,
        root: tk.Tk,
        app: Any,
        *,
        style: ttk.Style,
        palette_provider: Callable[[], ThemePalette],
        initial_split_ratio: float | None = None,
        options_collapsed: bool = True,
        output_naming_collapsed: bool = True,
        on_locate_ffmpeg: Callable[[], object],
        on_open_accounts: Callable[[], None],
        on_open_folder: Callable[[], None],
        on_browse_output_dir: Callable[[], None],
        on_paste_urls: Callable[[], object],
        on_paste_and_queue: Callable[[], None],
        on_browse_url_file: Callable[[], None],
        on_clear_urls: Callable[[], None],
        on_urls_modified: Callable[[tk.Event | None], None],
        on_start_download: Callable[[], None],
        on_cancel_download: Callable[[], None],
        on_preview_metadata: Callable[[], None],
        on_pack_local: Callable[[], object],
        on_retry_failed: Callable[[], None],
        service_label_for_url: Callable[[str], str],
        on_preview_url: Callable[[str], None],
        on_send_to_urls: Callable[[Sequence[str]], None],
        on_open_path: Callable[[str, bool], None],
        on_import_queue: Callable[[list[dict[str, Any]]], None],
        on_queue_summary_changed: Callable[[int, int], None],
        on_progress_mode_changed: Callable[[str], None],
        progress_var: tk.DoubleVar,
        active_job_var: tk.StringVar,
    ) -> None:
        super().__init__(parent, padding=(scale_px(parent, 12), scale_px(parent, 8)))
        self._window = root
        self._app = app
        self._palette_provider = palette_provider
        self._split_ratio: float | None = (
            float(initial_split_ratio)
            if (initial_split_ratio is not None and 0.0 < float(initial_split_ratio) < 1.0)
            else None
        )
        self._user_dragged_split = self._split_ratio is not None
        self._initial_split_applied = False
        self._ffmpeg_banner_dismissed = False
        self._ffmpeg_available = True
        self._fit_after_id: str | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Row 0: Dismissible FFmpeg-missing warning banner (hidden when ready)
        self._ffmpeg_banner = ttk.Frame(
            self,
            style="Banner.TFrame",
            padding=(scale_px(self, 10), scale_px(self, 6)),
        )
        self._ffmpeg_banner.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._ffmpeg_banner.columnconfigure(0, weight=1)
        self._ffmpeg_banner_var = tk.StringVar(
            value="FFmpeg/FFprobe not found. Combining parts or converting to .m4b/.mp3 requires FFmpeg."
        )
        ttk.Label(
            self._ffmpeg_banner,
            textvariable=self._ffmpeg_banner_var,
            style="Banner.TLabel",
        ).grid(row=0, column=0, sticky="w")
        banner_btns = ttk.Frame(self._ffmpeg_banner, style="Banner.TFrame")
        banner_btns.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(banner_btns, text="Locate...", command=on_locate_ffmpeg).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(banner_btns, text="Dismiss", command=self.dismiss_ffmpeg_banner).pack(side=tk.LEFT)
        self._ffmpeg_banner.grid_remove()

        # Row 1: Vertical splitter (top = inputs/collapsibles, bottom = queue/log)
        self.paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self.paned.grid(row=1, column=0, sticky="nsew")

        self.top_scroll = ScrollableFrame(
            self.paned,
            style=style,
            content_padding=(scale_px(self, 2), scale_px(self, 2)),
            inner_scrollables=lambda: (self.urls_text,),
        )
        top_pane = self.top_scroll.content_frame
        top_pane.columnconfigure(0, weight=1)

        # URLs
        self.url_frame = ttk.LabelFrame(top_pane, text="Audiobook URL(s)", padding=(10, 6))
        self.url_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.url_frame.columnconfigure(0, weight=1)

        self.urls_text = tk.Text(self.url_frame, height=3, width=40, wrap="word")
        self.urls_text.grid(row=0, column=0, sticky="ew")
        self.urls_text.tag_configure(
            "warning_line", foreground=palette_provider().warning, underline=True
        )
        bind_text_tab_navigation(self.urls_text)
        self.urls_text.bind("<Control-Return>", lambda _e: on_start_download())
        self.urls_text.bind("<<Modified>>", on_urls_modified)
        self.url_menu = attach_url_context_menu(
            self.urls_text,
            on_paste=on_paste_urls,
            on_paste_and_queue=on_paste_and_queue,
            on_clear=on_clear_urls,
        )

        url_footer = ttk.Frame(self.url_frame)
        url_footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        url_footer.columnconfigure(0, weight=1)
        ttk.Label(
            url_footer,
            text="One URL per line \u2022 Right-click: Paste & Queue \u2022 Ctrl+Enter / F5 to start",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")
        url_btn_row = ttk.Frame(url_footer)
        url_btn_row.grid(row=0, column=1, sticky="e")
        ttk.Button(url_btn_row, text="Paste", command=on_paste_urls).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(url_btn_row, text="Load .txt...", command=on_browse_url_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(url_btn_row, text="Clear", command=on_clear_urls).pack(side=tk.LEFT)

        # Output folder + Account chip row (always visible)
        folder_bar = ttk.Frame(top_pane)
        folder_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        folder_bar.columnconfigure(1, weight=1)

        ttk.Label(folder_bar, text="Output Folder:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.output_entry = ttk.Entry(folder_bar, textvariable=app.output_dir_var)
        self.output_entry.grid(row=0, column=1, sticky="ew")
        self.output_browse_btn = ttk.Button(
            folder_bar, text="Browse...", command=on_browse_output_dir
        )
        self.output_browse_btn.grid(row=0, column=2, padx=(6, 0))
        self.output_open_btn = ttk.Button(folder_bar, text="Open", command=on_open_folder)
        self.output_open_btn.grid(row=0, column=3, padx=(4, 0))

        self.account_chip_var = tk.StringVar(value="")
        self.account_chip_btn = ttk.Button(
            folder_bar,
            textvariable=self.account_chip_var,
            style="Chip.TButton",
            command=on_open_accounts,
        )
        self.account_chip_btn.grid(row=0, column=4, padx=(10, 0))
        attach_tooltip(
            self.account_chip_btn,
            "Active service and profile (click to edit credentials on the Accounts page)",
            palette_provider=palette_provider,
        )
        attach_tooltip(
            self.output_entry,
            "Destination folder where downloaded or packed audiobooks are written",
            palette_provider=palette_provider,
        )

        # Collapsible Output naming section (collapsed by default)
        self.naming_section = CollapsibleSection(
            top_pane,
            "Output naming",
            collapsed=output_naming_collapsed,
            on_toggle=lambda _c: self._on_section_toggled(),
        )
        self.naming_section.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        naming_content = self.naming_section.content
        naming_content.columnconfigure(1, weight=1)

        ttk.Label(naming_content, text="Template:").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )
        tpl_row = ttk.Frame(naming_content)
        tpl_row.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        tpl_row.columnconfigure(0, weight=1)
        self.tpl_entry = ttk.Entry(tpl_row, textvariable=app.template_var)
        self.tpl_entry.grid(row=0, column=0, sticky="ew")
        self.token_btn = ttk.Menubutton(tpl_row, text="Insert Token")
        token_menu = tk.Menu(self.token_btn, tearoff=0)
        for tok, desc in TEMPLATE_TOKENS:

            def _insert_cb(t: str = tok) -> None:
                self.insert_template_token(t)

            token_menu.add_command(label=f"{tok}  \u2014  {desc}", command=_insert_cb)
        self.token_btn.configure(menu=token_menu)
        self.token_btn.grid(row=0, column=1, padx=(6, 0))
        self.preset_combo = ttk.Combobox(
            tpl_row,
            textvariable=app.template_preset_var,
            values=[label for label, _tpl in TEMPLATE_PRESET_LABELS],
            state="readonly",
            width=16,
        )
        self.preset_combo.grid(row=0, column=2, padx=(6, 0))

        ttk.Label(naming_content, text="Series:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Label(naming_content, textvariable=app.template_preview_var, style="Muted.TLabel").grid(
            row=1, column=1, sticky="w"
        )
        ttk.Label(naming_content, text="Standalone:").grid(row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Label(
            naming_content,
            textvariable=app.template_standalone_preview_var,
            style="Muted.TLabel",
        ).grid(row=2, column=1, sticky="w")

        tpl_spec = OPTIONS_BY_NAME.get("output_template")
        if tpl_spec and tpl_spec.cli_flags:
            raw_help = tpl_spec.cli_flags[0].help
            tpl_help = raw_help() if callable(raw_help) else str(raw_help or "")
            if tpl_help:
                attach_tooltip(self.tpl_entry, tpl_help, palette_provider=palette_provider)

        # Collapsible Options section (collapsed by default)
        self.options_section = CollapsibleSection(
            top_pane,
            "Options",
            collapsed=options_collapsed,
            on_toggle=lambda _c: self._on_section_toggled(),
        )
        self.options_section.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        self.options_panel = OptionsPanel(
            self.options_section.content,
            app,
            on_m4b_toggled=app._on_m4b_toggled,
            on_format_selected=app._on_format_selected,
            palette_provider=palette_provider,
        )
        self.options_panel.grid(row=0, column=0, sticky="ew")

        self.output_widgets: list[ttk.Entry | ttk.Button | ttk.Combobox | ttk.Menubutton] = [
            self.output_entry,
            self.output_browse_btn,
            self.tpl_entry,
            self.token_btn,
            self.preset_combo,
        ]

        # Bottom pane: Queue & Activity Log
        self.queue_panel = ActivityQueuePanel(
            self.paned,
            root,
            app.controller,
            status_var=app.status_var,
            batch_var=app.batch_var,
            metrics_var=app.metrics_var,
            on_start_download=on_start_download,
            on_cancel_download=on_cancel_download,
            on_preview_metadata=on_preview_metadata,
            on_pack_local=on_pack_local,
            on_open_folder=on_open_folder,
            on_retry_failed=on_retry_failed,
            service_label_for_url=service_label_for_url,
            on_retry_selected=on_retry_failed,
            on_preview_url=on_preview_url,
            on_send_to_urls=on_send_to_urls,
            on_open_path=on_open_path,
            on_import_queue=on_import_queue,
            on_queue_summary_changed=on_queue_summary_changed,
            on_progress_mode_changed=on_progress_mode_changed,
            progress_var=progress_var,
            active_job_var=active_job_var,
            palette=palette_provider(),
        )

        self.paned.add(self.top_scroll, weight=0)
        self.paned.add(self.queue_panel, weight=1)
        self.paned.bind("<Configure>", self._on_paned_configure, add="+")
        self.paned.bind("<ButtonRelease-1>", self._on_paned_release, add="+")

    def apply_theme(self, palette: ThemePalette) -> None:
        self.urls_text.tag_configure("warning_line", foreground=palette.warning, underline=True)
        self.top_scroll.apply_theme(palette.bg)
        self.queue_panel.apply_theme_palette(palette)

    def update_ffmpeg_banner(self, available: bool, status_text: str) -> None:
        self._ffmpeg_available = available
        if available:
            self._ffmpeg_banner_dismissed = False
            self._ffmpeg_banner.grid_remove()
            return
        if not self._ffmpeg_banner_dismissed:
            self._ffmpeg_banner_var.set(
                f"{status_text} \u2014 Combining parts or converting formats requires FFmpeg."
            )
            self._ffmpeg_banner.grid()
        if not self._user_dragged_split:
            self.schedule_fit_splitter()

    def dismiss_ffmpeg_banner(self) -> None:
        self._ffmpeg_banner_dismissed = True
        self._ffmpeg_banner.grid_remove()
        if not self._user_dragged_split:
            self.schedule_fit_splitter()

    def update_account_chip(self, service_label: str, profile: str, saved_badge: str) -> None:
        svc = service_label or "Service"
        prof = profile or "default"
        status = saved_badge if saved_badge else "Not set"
        self.account_chip_var.set(f"{svc} \u2022 {prof} [{status}]")

    def insert_template_token(self, token: str) -> None:
        entry = self.tpl_entry
        if str(entry.cget("state")) == "disabled":
            return
        try:
            pos = str(entry.index(tk.INSERT))
        except tk.TclError:
            pos = tk.END
        entry.insert(pos, token)
        entry.focus_set()

    def _splitter_bounds(self, total_h: int) -> tuple[int, int]:
        min_top = min(scale_px(self, self.MIN_TOP_DP), max(60, total_h // 2))
        min_queue = min(scale_px(self, self.MIN_QUEUE_DP), max(80, total_h // 2))
        max_top = max(min_top, total_h - min_queue)
        return min_top, max_top

    def fit_splitter_to_top_content(self, *, total_height: int | None = None) -> int | None:
        """Resize the vertical splitter so top content fits without shrinking queue below minimum."""
        try:
            self.update_idletasks()
            total_h = int(total_height) if total_height is not None else int(self.paned.winfo_height())
            if total_h <= 1:
                return None
            top_req = int(self.top_scroll.content_frame.winfo_reqheight()) + scale_px(self, 4)
            min_top, max_top = self._splitter_bounds(total_h)
            target = max(min_top, min(top_req, max_top))
            self.paned.sashpos(0, target)
            self._split_ratio = target / total_h
            self.top_scroll.sync_layout()
            return target
        except tk.TclError:
            return None

    def schedule_fit_splitter(self) -> None:
        if self._fit_after_id is not None:
            try:
                self.after_cancel(self._fit_after_id)
            except tk.TclError:
                pass
        try:
            self._fit_after_id = self.after_idle(self._run_scheduled_fit)
        except tk.TclError:
            self._fit_after_id = None

    def _run_scheduled_fit(self) -> None:
        self._fit_after_id = None
        self.fit_splitter_to_top_content()

    def _on_section_toggled(self) -> None:
        self._user_dragged_split = False
        if self.fit_splitter_to_top_content() is None:
            self.schedule_fit_splitter()

    def _apply_split_for_height(self, total_h: int) -> None:
        if total_h <= 1:
            return
        min_top, max_top = self._splitter_bounds(total_h)
        if self._user_dragged_split and self._split_ratio is not None:
            target = max(min_top, min(round(total_h * self._split_ratio), max_top))
            self.paned.sashpos(0, target)
            self.top_scroll.sync_layout()
        else:
            self.fit_splitter_to_top_content()

    def _on_paned_configure(self, event: tk.Event) -> None:
        total_h = int(getattr(event, "height", 0) or self.paned.winfo_height())
        if total_h <= 1:
            return
        if not self._initial_split_applied:
            self._initial_split_applied = True
        self._apply_split_for_height(total_h)

    def _on_paned_release(self, _event: tk.Event) -> None:
        def _record() -> None:
            try:
                if not self.winfo_exists():
                    return
                total_h = int(self.paned.winfo_height())
                if total_h <= 1:
                    return
                pos = int(self.paned.sashpos(0))
                min_top, max_top = self._splitter_bounds(total_h)
                clamped = max(min_top, min(pos, max_top))
                if clamped != pos:
                    self.paned.sashpos(0, clamped)
                self._split_ratio = clamped / total_h
                self._user_dragged_split = True
                self.top_scroll.sync_layout()
            except tk.TclError:
                pass

        self.after_idle(_record)

    def get_split_ratio(self) -> float | None:
        """Return current download splitter ratio `(0..1)` for persistence."""
        try:
            total_h = int(self.paned.winfo_height())
            if total_h > 10:
                pos = int(self.paned.sashpos(0))
                ratio = pos / total_h
                if 0.0 < ratio < 1.0:
                    return ratio
        except tk.TclError:
            pass
        if self._split_ratio is not None and 0.0 < self._split_ratio < 1.0:
            return self._split_ratio
        return None

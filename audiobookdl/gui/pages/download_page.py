"""Download page combining URL inputs, output folder, collapsible naming/options, and queue/log."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections.abc import Callable, Sequence
from typing import Any, Literal

from audiobookdl.gui.formatting import TEMPLATE_PRESET_LABELS
from audiobookdl.gui.options_panel import OptionsPanel
from audiobookdl.gui.queue_panel import ActivityQueuePanel
from audiobookdl.gui.theme import ThemePalette, scale_px
from audiobookdl.gui.widgets import (
    CollapsibleSection,
    ResponsiveMode,
    ScrollableFrame,
    attach_tooltip,
    attach_url_context_menu,
    bind_text_tab_navigation,
    resolve_responsive_mode,
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
    """Primary Download page with responsive vertical/two-column splitter."""

    MIN_TOP_DP = 150
    MIN_QUEUE_DP = 210
    MIN_LEFT_DP = 560
    MIN_RIGHT_DP = 370
    TWO_COL_ENTER_DP = 980
    TWO_COL_EXIT_DP = 940
    _FOLDER_BAR_WIDE_ENTER_DP = 620
    _FOLDER_BAR_WIDE_EXIT_DP = 580
    _URL_FOOTER_WIDE_ENTER_DP = 570
    _URL_FOOTER_WIDE_EXIT_DP = 530

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
        on_m4b_toggled: Callable[[], None],
        on_format_selected: Callable[[tk.Event | None], None],
        progress_var: tk.DoubleVar,
        active_job_var: tk.StringVar,
        concurrency_var: tk.IntVar | None = None,
        on_concurrency_changed: Callable[[int], None] | None = None,
        on_retry_selected: Callable[[Sequence[str]], None] | None = None,
        profile_choices_provider: Callable[[], list[str]] | None = None,
        on_select_profile: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent, padding=(scale_px(parent, 12), scale_px(parent, 8)))
        self._window = root
        self._palette_provider = palette_provider
        self._on_open_accounts = on_open_accounts
        self._profile_choices_provider = profile_choices_provider
        self._on_select_profile = on_select_profile
        self._split_ratio: float | None = (
            float(initial_split_ratio)
            if (initial_split_ratio is not None and 0.0 < float(initial_split_ratio) < 1.0)
            else None
        )
        self._user_dragged_split = self._split_ratio is not None
        self._h_split_ratio: float | None = None
        self._h_user_dragged_split = False
        self._layout_mode: Literal["vertical", "horizontal"] = "vertical"
        self._folder_bar_mode: ResponsiveMode = "wide"
        self._url_footer_mode: ResponsiveMode = "wide"
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

        # Row 1: Responsive splitter (vertical stack when narrow, two-column when wide)
        self._vpaned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self._hpaned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.paned: ttk.PanedWindow = self._vpaned
        self.paned.grid(row=1, column=0, sticky="nsew")

        self.top_scroll = ScrollableFrame(
            self,
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
        attach_url_context_menu(
            self.urls_text,
            on_paste=on_paste_urls,
            on_paste_and_queue=on_paste_and_queue,
            on_clear=on_clear_urls,
        )

        self._url_footer = ttk.Frame(self.url_frame)
        self._url_footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._url_hint_label = ttk.Label(
            self._url_footer,
            text="One URL per line \u2022 Right-click: Paste & Queue \u2022 Ctrl+Enter / F5 to start",
            style="Muted.TLabel",
        )
        self._url_btn_row = ttk.Frame(self._url_footer)
        ttk.Button(self._url_btn_row, text="Paste", command=on_paste_urls).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(self._url_btn_row, text="Load .txt...", command=on_browse_url_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(self._url_btn_row, text="Clear", command=on_clear_urls).pack(side=tk.LEFT)
        self._apply_url_footer_layout("wide")
        self._url_footer.bind("<Configure>", self._on_url_footer_configure, add="+")

        # Output folder + Account chip row (always visible)
        self._folder_bar = ttk.Frame(top_pane)
        self._folder_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        self._output_label = ttk.Label(self._folder_bar, text="Output Folder:")
        self.output_entry = ttk.Entry(self._folder_bar, textvariable=app.output_dir_var)
        self.output_browse_btn = ttk.Button(
            self._folder_bar, text="Browse...", command=on_browse_output_dir
        )
        self.output_open_btn = ttk.Button(self._folder_bar, text="Open", command=on_open_folder)

        self.account_chip_var = tk.StringVar(value="")
        self.account_chip_btn = ttk.Button(
            self._folder_bar,
            textvariable=self.account_chip_var,
            style="Chip.TButton",
            command=on_open_accounts,
        )
        self.account_chip_btn.bind("<Button-3>", self.show_profile_menu)
        self._apply_folder_bar_layout("wide")
        self._folder_bar.bind("<Configure>", self._on_folder_bar_configure, add="+")
        attach_tooltip(
            self.account_chip_btn,
            "Active service and profile (click to edit credentials on Accounts page; right-click to switch profile)",
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
        if tpl_spec:
            tpl_help = tpl_spec.get_help()
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
            on_m4b_toggled=on_m4b_toggled,
            on_format_selected=on_format_selected,
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

        # Secondary pane: Queue & Activity Log
        self.queue_panel = ActivityQueuePanel(
            self,
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
            on_retry_selected=on_retry_selected or (lambda _ids: on_retry_failed()),
            on_preview_url=on_preview_url,
            on_send_to_urls=on_send_to_urls,
            on_open_path=on_open_path,
            on_import_queue=on_import_queue,
            on_queue_summary_changed=on_queue_summary_changed,
            on_progress_mode_changed=on_progress_mode_changed,
            progress_var=progress_var,
            active_job_var=active_job_var,
            concurrency_var=concurrency_var,
            on_concurrency_changed=on_concurrency_changed,
            palette=palette_provider(),
        )

        self.paned.add(self.top_scroll, weight=0)
        self.paned.add(self.queue_panel, weight=1)
        self.bind("<Configure>", self._on_page_configure, add="+")
        self._vpaned.bind("<Configure>", lambda e: self._on_paned_configure("vertical", e), add="+")
        self._vpaned.bind(
            "<ButtonRelease-1>", lambda e: self._on_paned_release("vertical", e), add="+"
        )
        self._hpaned.bind("<Configure>", lambda e: self._on_paned_configure("horizontal", e), add="+")
        self._hpaned.bind(
            "<ButtonRelease-1>", lambda e: self._on_paned_release("horizontal", e), add="+"
        )
        self.top_scroll.guard_scroll_inputs()

    def apply_theme(self, palette: ThemePalette) -> None:
        self.urls_text.tag_configure("warning_line", foreground=palette.warning, underline=True)
        self.top_scroll.apply_theme(palette)
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

    def show_profile_menu(self, event: tk.Event | None = None) -> None:
        """Open a quick context menu to switch active profile or manage accounts."""
        menu = tk.Menu(self, tearoff=False)
        profiles = self._profile_choices_provider() if self._profile_choices_provider is not None else ["default"]
        if not profiles:
            profiles = ["default"]
        for prof in profiles:
            def _select_cb(target: str = prof) -> None:
                if self._on_select_profile is not None:
                    self._on_select_profile(target)

            menu.add_command(
                label=f"Profile: {prof}",
                command=_select_cb,
            )
        menu.add_separator()
        menu.add_command(label="Manage Accounts...", command=self._on_open_accounts)
        if event is not None:
            x, y = event.x_root, event.y_root
        else:
            x = self.account_chip_btn.winfo_rootx()
            y = self.account_chip_btn.winfo_rooty() + self.account_chip_btn.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

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

    def _on_url_footer_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._url_footer.winfo_width())
        mode = resolve_responsive_mode(
            self._url_footer_mode,
            w,
            scale_px(self, self._URL_FOOTER_WIDE_ENTER_DP),
            scale_px(self, self._URL_FOOTER_WIDE_EXIT_DP),
        )
        if mode != self._url_footer_mode:
            self._apply_url_footer_layout(mode)

    def _apply_url_footer_layout(self, mode: ResponsiveMode) -> None:
        self._url_footer_mode = mode
        self._url_footer.columnconfigure(0, weight=1)
        self._url_hint_label.grid(row=0, column=0, sticky="w", pady=0)
        if mode == "wide":
            self._url_btn_row.grid(row=0, column=1, sticky="e", pady=0)
        else:
            self._url_btn_row.grid(row=1, column=0, sticky="e", pady=(4, 0))

    def _on_folder_bar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._folder_bar.winfo_width())
        mode = resolve_responsive_mode(
            self._folder_bar_mode,
            w,
            scale_px(self, self._FOLDER_BAR_WIDE_ENTER_DP),
            scale_px(self, self._FOLDER_BAR_WIDE_EXIT_DP),
        )
        if mode != self._folder_bar_mode:
            self._apply_folder_bar_layout(mode)

    def _apply_folder_bar_layout(self, mode: ResponsiveMode) -> None:
        self._folder_bar_mode = mode
        self._folder_bar.columnconfigure(1, weight=1)
        self._output_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=0)
        self.output_entry.grid(row=0, column=1, sticky="ew", pady=0)
        self.output_browse_btn.grid(row=0, column=2, padx=(6, 0), pady=0)
        self.output_open_btn.grid(row=0, column=3, padx=(4, 0), pady=0)
        if mode == "wide":
            self.account_chip_btn.grid(
                row=0, column=4, columnspan=1, sticky="e", padx=(10, 0), pady=0
            )
        else:
            self.account_chip_btn.grid(
                row=1, column=0, columnspan=4, sticky="w", padx=0, pady=(4, 0)
            )

    def _splitter_bounds(self, total_h: int) -> tuple[int, int]:
        min_top = min(scale_px(self, self.MIN_TOP_DP), max(60, total_h // 2))
        min_queue = min(scale_px(self, self.MIN_QUEUE_DP), max(80, total_h // 2))
        max_top = max(min_top, total_h - min_queue)
        return min_top, max_top

    def _horizontal_splitter_bounds(self, total_w: int) -> tuple[int, int]:
        min_left = min(scale_px(self, self.MIN_LEFT_DP), max(240, total_w // 2))
        min_right = min(scale_px(self, self.MIN_RIGHT_DP), max(220, total_w // 2))
        max_left = max(min_left, total_w - min_right)
        return min_left, max_left

    def _set_layout_mode(self, mode: Literal["vertical", "horizontal"]) -> None:
        if mode == self._layout_mode:
            return
        old_paned = self._vpaned if self._layout_mode == "vertical" else self._hpaned
        new_paned = self._hpaned if mode == "horizontal" else self._vpaned
        for pane in (self.top_scroll, self.queue_panel):
            try:
                old_paned.forget(pane)
            except tk.TclError:
                pass
        old_paned.grid_remove()
        new_paned.grid(row=1, column=0, sticky="nsew")
        new_paned.add(self.top_scroll, weight=0)
        new_paned.add(self.queue_panel, weight=1)
        self._layout_mode = mode
        self.paned = new_paned
        self.schedule_fit_splitter()

    def _on_page_configure(self, event: tk.Event) -> None:
        if getattr(event, "widget", self) is not self:
            return
        if isinstance(event, tk.Event) and not self.winfo_ismapped():
            return
        total_w = int(getattr(event, "width", 0) or self.winfo_width())
        if total_w <= 1:
            return
        if self._layout_mode == "vertical" and total_w >= scale_px(self, self.TWO_COL_ENTER_DP):
            self._set_layout_mode("horizontal")
        elif self._layout_mode == "horizontal" and total_w < scale_px(self, self.TWO_COL_EXIT_DP):
            self._set_layout_mode("vertical")

    def fit_splitter_to_top_content(self, *, total_height: int | None = None) -> int | None:
        """Resize the vertical splitter so top content fits without shrinking queue below minimum."""
        if total_height is None and self._layout_mode != "vertical":
            self.top_scroll.sync_layout()
            return None
        try:
            self.update_idletasks()
            total_h = total_height if total_height is not None else self._vpaned.winfo_height()
            if total_h <= 1:
                return None
            top_req = self.top_scroll.content_frame.winfo_reqheight() + scale_px(self, 4)
            min_top, max_top = self._splitter_bounds(total_h)
            target = max(min_top, min(top_req, max_top))
            self._vpaned.sashpos(0, target)  # type: ignore[no-untyped-call]
            self._split_ratio = target / total_h
            self.top_scroll.sync_layout()
            return target
        except tk.TclError:
            return None

    def fit_horizontal_splitter(self, *, total_width: int | None = None) -> int | None:
        """Size the horizontal splitter to fit left-column content while preserving Activity minimum width."""
        try:
            self.update_idletasks()
            if self._layout_mode != "horizontal" or len(self._hpaned.panes()) < 2:  # type: ignore[no-untyped-call]
                return None
            total_w = total_width if total_width is not None else self._hpaned.winfo_width()
            if total_w <= 1:
                return None
            left_req = self.top_scroll.content_frame.winfo_reqwidth() + scale_px(self, 10)
            min_left, max_left = self._horizontal_splitter_bounds(total_w)
            target = max(min_left, min(left_req, max_left))
            self._hpaned.sashpos(0, target)  # type: ignore[no-untyped-call]
            self._h_split_ratio = target / total_w
            self.top_scroll.sync_layout()
            self.queue_panel.sync_responsive_toolbars(max(1, total_w - target))
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
        if self._layout_mode == "horizontal":
            self._apply_horizontal_split_for_width(self._hpaned.winfo_width())
        else:
            self.fit_splitter_to_top_content()

    def _on_section_toggled(self) -> None:
        if self._layout_mode == "horizontal":
            self._h_user_dragged_split = False
            if self.fit_horizontal_splitter() is None:
                self.schedule_fit_splitter()
            return
        self._user_dragged_split = False
        if self.fit_splitter_to_top_content() is None:
            self.schedule_fit_splitter()

    def _apply_split_for_height(self, total_h: int) -> None:
        if total_h <= 1:
            return
        min_top, max_top = self._splitter_bounds(total_h)
        if self._user_dragged_split and self._split_ratio is not None:
            target = max(min_top, min(round(total_h * self._split_ratio), max_top))
            self._vpaned.sashpos(0, target)  # type: ignore[no-untyped-call]
            self.top_scroll.sync_layout()
        else:
            self.fit_splitter_to_top_content()

    def _apply_horizontal_split_for_width(self, total_w: int) -> None:
        if total_w <= 1:
            return
        min_left, max_left = self._horizontal_splitter_bounds(total_w)
        if self._h_user_dragged_split and self._h_split_ratio is not None:
            target = max(min_left, min(round(total_w * self._h_split_ratio), max_left))
            self._hpaned.sashpos(0, target)  # type: ignore[no-untyped-call]
            self.top_scroll.sync_layout()
            self.queue_panel.sync_responsive_toolbars(max(1, total_w - target))
        else:
            self.fit_horizontal_splitter(total_width=total_w)

    def _on_paned_configure(self, mode: Literal["vertical", "horizontal"], event: tk.Event) -> None:
        if mode != self._layout_mode:
            return
        if not self._initial_split_applied:
            self._initial_split_applied = True
        if mode == "horizontal":
            total_w = int(getattr(event, "width", 0) or self._hpaned.winfo_width())
            if total_w > 1:
                self._apply_horizontal_split_for_width(total_w)
            return
        total_h = int(getattr(event, "height", 0) or self._vpaned.winfo_height())
        if total_h > 1:
            self._apply_split_for_height(total_h)

    def _on_paned_release(self, mode: Literal["vertical", "horizontal"], _event: tk.Event) -> None:
        def _record() -> None:
            try:
                if not self.winfo_exists() or mode != self._layout_mode:
                    return
                if mode == "horizontal":
                    total_w = self._hpaned.winfo_width()
                    if total_w <= 1:
                        return
                    pos = int(self._hpaned.sashpos(0))  # type: ignore[no-untyped-call]
                    min_left, max_left = self._horizontal_splitter_bounds(total_w)
                    clamped = max(min_left, min(pos, max_left))
                    if clamped != pos:
                        self._hpaned.sashpos(0, clamped)  # type: ignore[no-untyped-call]
                    self._h_split_ratio = clamped / total_w
                    self._h_user_dragged_split = True
                    self.top_scroll.sync_layout()
                    self.queue_panel.sync_responsive_toolbars(max(1, total_w - clamped))
                    return

                total_h = self._vpaned.winfo_height()
                if total_h <= 1:
                    return
                pos = int(self._vpaned.sashpos(0))  # type: ignore[no-untyped-call]
                min_top, max_top = self._splitter_bounds(total_h)
                clamped = max(min_top, min(pos, max_top))
                if clamped != pos:
                    self._vpaned.sashpos(0, clamped)  # type: ignore[no-untyped-call]
                self._split_ratio = clamped / total_h
                self._user_dragged_split = True
                self.top_scroll.sync_layout()
            except tk.TclError:
                pass

        self.after_idle(_record)

    def get_split_ratio(self) -> float | None:
        """Return current vertical download splitter ratio `(0..1)` for persistence."""
        if self._layout_mode == "vertical":
            try:
                total_h = self._vpaned.winfo_height()
                if total_h > 10:
                    pos = int(self._vpaned.sashpos(0))  # type: ignore[no-untyped-call]
                    ratio = pos / total_h
                    if 0.0 < ratio < 1.0:
                        return ratio
            except tk.TclError:
                pass
        if self._split_ratio is not None and 0.0 < self._split_ratio < 1.0:
            return self._split_ratio
        return None

"""Three-tab options panel for output format, metadata sidecars, and network settings."""

import tkinter as tk
from tkinter import ttk
from typing import Any
from collections.abc import Callable

from audiobookdl.gui.formatting import MEDIA_TYPE_OPTIONS, OUTPUT_FORMATS, OVERWRITE_OPTIONS
from audiobookdl.gui.theme import ThemePalette
from audiobookdl.gui.widgets import attach_tooltip
from audiobookdl.options_schema import GUI_INTRO_CHAPTER_MODES, OPTIONS_BY_NAME
from audiobookdl.utils import AUDIO_BITRATE_PRESETS
from audiobookdl.utils.audiobook import parse_decryption_key_spec
from audiobookdl.utils.http import parse_byte_rate

OptionWidget = ttk.Entry | ttk.Checkbutton | ttk.Combobox | ttk.Spinbox


def _option_help(name: str) -> str:
    spec = OPTIONS_BY_NAME.get(name)
    return spec.get_help() if spec is not None else ""


class OptionsPanel(ttk.LabelFrame):
    """LabelFrame containing the General, Chapters & Sidecars, and Network & Performance tabs."""

    def __init__(
        self,
        master: tk.Misc,
        vars_owner: Any,
        *,
        on_m4b_toggled: Callable[[], None],
        on_format_selected: Callable[[tk.Event | None], None],
        palette_provider: Callable[[], ThemePalette] | None = None,
    ) -> None:
        super().__init__(master, text="Options", padding=(8, 4))
        self._vars = vars_owner
        self._on_m4b_toggled = on_m4b_toggled
        self._on_format_selected = on_format_selected
        self._palette_provider = palette_provider
        self._widgets: list[OptionWidget] = []
        self._panel_enabled = True
        self._dependency_traces_wired = False
        self._build_widgets()
        self._attach_tooltips()
        self._wire_dependencies()
        self.sync_dependency_states()

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)

        self.options_notebook = ttk.Notebook(self)
        self.options_notebook.grid(row=0, column=0, sticky="ew")

        general_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        self.options_notebook.add(general_tab, text="General")

        cb_m4b = ttk.Checkbutton(
            general_tab,
            text="Download as .m4b",
            variable=self._vars.m4b_var,
            command=self._on_m4b_toggled,
        )
        cb_m4b.grid(row=0, column=0, columnspan=2, sticky="w", padx=(0, 12))

        ttk.Label(general_tab, text="Format:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        format_combo = ttk.Combobox(
            general_tab,
            textvariable=self._vars.output_format_var,
            values=OUTPUT_FORMATS,
            state="readonly",
            width=10,
        )
        format_combo.grid(row=0, column=3, sticky="w", padx=(0, 12))
        format_combo.bind("<<ComboboxSelected>>", self._on_format_selected)

        cb_combine = ttk.Checkbutton(
            general_tab,
            text="Combine parts",
            variable=self._vars.combine_var,
            command=self._on_combine_clicked,
        )
        cb_combine.grid(row=0, column=4, sticky="w", padx=(0, 12))

        cb_skip = ttk.Checkbutton(
            general_tab, text="Skip downloaded", variable=self._vars.skip_downloaded_var
        )
        cb_skip.grid(row=0, column=5, sticky="w")

        cb_resume = ttk.Checkbutton(general_tab, text="Resume partial", variable=self._vars.resume_var)
        cb_resume.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 12), pady=(4, 0))

        ttk.Label(general_tab, text="Overwrite:").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        overwrite_combo = ttk.Combobox(
            general_tab,
            textvariable=self._vars.overwrite_var,
            values=OVERWRITE_OPTIONS,
            state="readonly",
            width=12,
        )
        overwrite_combo.grid(row=1, column=3, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_archive = ttk.Checkbutton(
            general_tab,
            text="Use archive ledger",
            variable=self._vars.download_archive_var,
        )
        cb_archive.grid(row=1, column=4, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_cover_only = ttk.Checkbutton(
            general_tab,
            text="Cover only",
            variable=self._vars.cover_only_var,
        )
        cb_cover_only.grid(row=1, column=5, sticky="w", pady=(4, 0))

        ttk.Label(general_tab, text="Media:").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        media_combo = ttk.Combobox(
            general_tab,
            textvariable=self._vars.media_type_var,
            values=MEDIA_TYPE_OPTIONS,
            state="readonly",
            width=10,
        )
        media_combo.grid(row=2, column=1, sticky="w", padx=(0, 12), pady=(4, 0))
        media_combo.bind("<<ComboboxSelected>>", self._on_media_selected)

        ttk.Label(general_tab, text="M4B Quality:").grid(
            row=2, column=2, sticky="w", padx=(0, 4), pady=(4, 0)
        )
        bitrate_values = list(AUDIO_BITRATE_PRESETS)
        if self._vars.audio_bitrate_var.get() not in bitrate_values:
            bitrate_values.append(self._vars.audio_bitrate_var.get())
        bitrate_combo = ttk.Combobox(
            general_tab,
            textvariable=self._vars.audio_bitrate_var,
            values=bitrate_values,
            state="readonly",
            width=10,
        )
        bitrate_combo.grid(row=2, column=3, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_enrich = ttk.Checkbutton(
            general_tab,
            text="Enrich metadata",
            variable=self._vars.enrich_metadata_var,
        )
        cb_enrich.grid(row=2, column=4, sticky="w", padx=(0, 12), pady=(4, 0))

        cb_debug = ttk.Checkbutton(general_tab, text="Debug logs", variable=self._vars.debug_var)
        cb_debug.grid(row=2, column=5, sticky="w", pady=(4, 0))

        self.notify_cb = ttk.Checkbutton(
            general_tab,
            text="Notify on finish",
            variable=self._vars.notify_on_complete_var,
        )
        self.notify_cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        cb_enrich_interactive = ttk.Checkbutton(
            general_tab,
            text="Interactive enrich",
            variable=self._vars.enrich_interactive_var,
        )
        cb_enrich_interactive.grid(row=3, column=2, columnspan=2, sticky="w", padx=(0, 12), pady=(4, 0))

        enrich_row = ttk.Frame(general_tab)
        enrich_row.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(4, 0))
        ttk.Label(enrich_row, text="Enrich providers:").pack(side=tk.LEFT, padx=(0, 6))
        enrich_providers_entry = ttk.Entry(
            enrich_row,
            textvariable=self._vars.enrich_providers_var,
            width=26,
        )
        enrich_providers_entry.pack(side=tk.LEFT, padx=(0, 8))
        self._enrich_providers_hint_var = tk.StringVar(
            value="audnexus, itunes, google, openlibrary"
        )
        ttk.Label(
            enrich_row,
            textvariable=self._enrich_providers_hint_var,
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        sidecars_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        sidecars_tab.columnconfigure(3, weight=1)
        self.options_notebook.add(sidecars_tab, text="Chapters & Sidecars")

        chapters_top_row = ttk.Frame(sidecars_tab)
        chapters_top_row.grid(row=0, column=0, columnspan=4, sticky="ew")

        cb_no_chapters = ttk.Checkbutton(
            chapters_top_row, text="No chapters", variable=self._vars.no_chapters_var
        )
        cb_no_chapters.pack(side=tk.LEFT, padx=(0, 14))

        cb_split_chapters = ttk.Checkbutton(
            chapters_top_row,
            text="Split chapters",
            variable=self._vars.split_chapters_var,
            command=self._on_split_chapters_clicked,
        )
        cb_split_chapters.pack(side=tk.LEFT, padx=(0, 14))

        cb_silence_chapters = ttk.Checkbutton(
            chapters_top_row,
            text="Silence chapters",
            variable=self._vars.chapters_from_silence_var,
        )
        cb_silence_chapters.pack(side=tk.LEFT, padx=(0, 14))

        cb_override_chapters = ttk.Checkbutton(
            chapters_top_row,
            text="Override chapters",
            variable=self._vars.override_chapters_var,
        )
        cb_override_chapters.pack(side=tk.LEFT, padx=(0, 14))

        offset_box = ttk.Frame(chapters_top_row)
        offset_box.pack(side=tk.LEFT)
        ttk.Label(offset_box, text="Offset (ms):").pack(side=tk.LEFT, padx=(0, 4))
        chapter_offset_entry = ttk.Entry(
            offset_box, textvariable=self._vars.chapter_offset_ms_var, width=7
        )
        chapter_offset_entry.pack(side=tk.LEFT)

        cb_json = ttk.Checkbutton(
            sidecars_tab, text="Write JSON metadata", variable=self._vars.write_json_var
        )
        cb_json.grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_opf = ttk.Checkbutton(sidecars_tab, text="Write OPF", variable=self._vars.write_opf_var)
        cb_opf.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_cue = ttk.Checkbutton(sidecars_tab, text="Write CUE", variable=self._vars.write_cue_var)
        cb_cue.grid(row=1, column=2, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_write_cover = ttk.Checkbutton(
            sidecars_tab, text="Write cover", variable=self._vars.write_cover_var
        )
        cb_write_cover.grid(row=1, column=3, sticky="w", pady=(4, 0))

        cb_abs_meta = ttk.Checkbutton(
            sidecars_tab, text="Write ABS metadata.json", variable=self._vars.write_abs_metadata_var
        )
        cb_abs_meta.grid(row=2, column=0, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_upload_abs = ttk.Checkbutton(
            sidecars_tab, text="Upload to ABS", variable=self._vars.upload_to_abs_var
        )
        cb_upload_abs.grid(row=2, column=1, sticky="w", padx=(0, 14), pady=(4, 0))

        cb_square_cover = ttk.Checkbutton(
            sidecars_tab, text="Square cover", variable=self._vars.square_cover_var
        )
        cb_square_cover.grid(row=2, column=2, sticky="w", padx=(0, 14), pady=(4, 0))

        max_cover_box = ttk.Frame(sidecars_tab)
        max_cover_box.grid(row=2, column=3, sticky="w", pady=(4, 0))
        ttk.Label(max_cover_box, text="Max cover:").pack(side=tk.LEFT, padx=(0, 4))
        max_cover_entry = ttk.Entry(
            max_cover_box, textvariable=self._vars.max_cover_size_var, width=6
        )
        max_cover_entry.pack(side=tk.LEFT)

        audio_row = ttk.Frame(sidecars_tab)
        audio_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        cb_loudnorm = ttk.Checkbutton(
            audio_row, text="Loudnorm", variable=self._vars.loudnorm_var
        )
        cb_loudnorm.pack(side=tk.LEFT, padx=(0, 14))
        cb_mono = ttk.Checkbutton(audio_row, text="Mono audio", variable=self._vars.mono_var)
        cb_mono.pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(audio_row, text="Intro numbering:").pack(side=tk.LEFT, padx=(0, 4))
        intro_chapter_combo = ttk.Combobox(
            audio_row,
            textvariable=self._vars.intro_chapter_mode_var,
            values=GUI_INTRO_CHAPTER_MODES,
            state="readonly",
            width=19,
        )
        intro_chapter_combo.pack(side=tk.LEFT)

        remove_row = ttk.Frame(sidecars_tab)
        remove_row.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(remove_row, text="Remove chars:").pack(side=tk.LEFT, padx=(0, 6))
        remove_chars_entry = ttk.Entry(remove_row, textvariable=self._vars.remove_chars_var, width=18)
        remove_chars_entry.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            remove_row,
            text="Characters stripped from output paths",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        network_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        network_tab.columnconfigure(5, weight=1)
        self.options_notebook.add(network_tab, text="Network & Performance")

        ttk.Label(network_tab, text="Max workers:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        max_workers_spin = ttk.Spinbox(
            network_tab,
            from_=1,
            to=20,
            textvariable=self._vars.max_workers_var,
            width=6,
        )
        max_workers_spin.grid(row=0, column=1, sticky="w", padx=(0, 14))

        ttk.Label(network_tab, text="Book delay (s):").grid(row=0, column=2, sticky="w", padx=(0, 4))
        book_delay_spin = ttk.Spinbox(
            network_tab,
            from_=0,
            to=3600,
            increment=0.5,
            textvariable=self._vars.book_delay_var,
            width=7,
        )
        book_delay_spin.grid(row=0, column=3, sticky="w", padx=(0, 14))

        ttk.Label(network_tab, text="Timeout (s):").grid(row=0, column=4, sticky="w", padx=(0, 4))
        timeout_spin = ttk.Spinbox(
            network_tab,
            from_=1,
            to=3600,
            increment=5,
            textvariable=self._vars.timeout_var,
            width=7,
        )
        timeout_spin.grid(row=0, column=5, sticky="w")

        ttk.Label(network_tab, text="Limit rate:").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        limit_rate_entry = ttk.Entry(network_tab, textvariable=self._vars.limit_rate_var, width=10)
        limit_rate_entry.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=(6, 0))

        ttk.Label(network_tab, text="Impersonate:").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        self._impersonate_combo = ttk.Combobox(
            network_tab,
            textvariable=self._vars.impersonate_var,
            values=("", "chrome", "chrome124", "edge", "safari", "firefox"),
            state="normal",
            width=10,
        )
        self._impersonate_combo.grid(row=1, column=3, sticky="w", padx=(0, 14), pady=(6, 0))

        ttk.Label(network_tab, text="Proxy URL:").grid(
            row=1, column=4, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        proxy_entry = ttk.Entry(network_tab, textvariable=self._vars.proxy_var)
        proxy_entry.grid(row=1, column=5, sticky="ew", pady=(6, 0))

        ttk.Label(network_tab, text="Post-exec:").grid(
            row=2, column=0, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        post_exec_entry = ttk.Entry(network_tab, textvariable=self._vars.post_exec_var)
        post_exec_entry.grid(row=2, column=1, columnspan=5, sticky="ew", pady=(6, 0))

        ttk.Label(network_tab, text="Decryption key:").grid(
            row=3, column=0, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        self._show_decryption_key_var = tk.BooleanVar(value=False)
        decryption_key_entry = ttk.Entry(
            network_tab,
            textvariable=self._vars.decryption_key_var,
            show="*",
        )
        decryption_key_entry.grid(row=3, column=1, columnspan=4, sticky="ew", padx=(0, 8), pady=(6, 0))
        show_key_cb = ttk.Checkbutton(
            network_tab,
            text="Show key",
            variable=self._show_decryption_key_var,
            command=self._toggle_decryption_key_visibility,
        )
        show_key_cb.grid(row=3, column=5, sticky="w", pady=(6, 0))

        ttk.Label(
            network_tab,
            text="Defaults: 6 workers, unlimited bandwidth (e.g. 2M), TLS impersonate via curl_cffi (e.g. chrome)",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(4, 0))
        self._validation_var = tk.StringVar(value="")
        self._validation_label = ttk.Label(
            self,
            textvariable=self._validation_var,
            style="Error.TLabel",
        )
        self._validation_label.grid(row=1, column=0, sticky="w", padx=(4, 0), pady=(2, 0))
        self._validation_label.grid_remove()

        self._cb_m4b = cb_m4b
        self._format_combo = format_combo
        self._cb_combine = cb_combine
        self._bitrate_combo = bitrate_combo
        self._cb_enrich_interactive = cb_enrich_interactive
        self._enrich_providers_entry = enrich_providers_entry
        self._cb_split_chapters = cb_split_chapters
        self._cb_silence_chapters = cb_silence_chapters
        self._cb_override_chapters = cb_override_chapters
        self._chapter_offset_entry = chapter_offset_entry
        self._intro_chapter_combo = intro_chapter_combo
        self._cb_cue = cb_cue
        self._cb_loudnorm = cb_loudnorm
        self._cb_mono = cb_mono
        self._max_cover_entry = max_cover_entry
        self._limit_rate_entry = limit_rate_entry
        self._decryption_key_entry = decryption_key_entry

        self._tooltip_bindings: list[tuple[tk.Misc, str]] = [
            (cb_m4b, "Combine and convert output into a single .m4b audiobook with chapters"),
            (format_combo, _option_help("output_format")),
            (cb_combine, _option_help("combine")),
            (cb_skip, _option_help("skip_downloaded")),
            (cb_resume, _option_help("resume")),
            (overwrite_combo, _option_help("overwrite")),
            (cb_archive, _option_help("download_archive")),
            (cb_cover_only, _option_help("cover")),
            (media_combo, _option_help("media_type")),
            (bitrate_combo, _option_help("audio_bitrate")),
            (cb_enrich, _option_help("enrich_metadata")),
            (cb_enrich_interactive, _option_help("enrich_interactive")),
            (enrich_providers_entry, _option_help("enrich_providers")),
            (cb_debug, _option_help("debug")),
            (self.notify_cb, "Show a desktop notification when downloads finish"),
            (cb_no_chapters, _option_help("no_chapters")),
            (cb_split_chapters, _option_help("split_chapters")),
            (cb_silence_chapters, _option_help("chapters_from_silence")),
            (cb_override_chapters, _option_help("override_chapters")),
            (chapter_offset_entry, _option_help("chapter_offset_ms")),
            (intro_chapter_combo, _option_help("intro_chapter_mode")),
            (cb_json, _option_help("write_json_metadata")),
            (cb_opf, _option_help("write_opf")),
            (cb_cue, _option_help("write_cue")),
            (cb_write_cover, _option_help("write_cover")),
            (cb_abs_meta, _option_help("write_abs_metadata")),
            (cb_upload_abs, _option_help("upload_to_abs")),
            (cb_square_cover, _option_help("square_cover")),
            (max_cover_entry, _option_help("max_cover_size")),
            (cb_loudnorm, _option_help("loudnorm")),
            (cb_mono, _option_help("mono")),
            (remove_chars_entry, _option_help("remove_chars")),
            (max_workers_spin, _option_help("max_workers")),
            (book_delay_spin, _option_help("book_delay")),
            (timeout_spin, _option_help("timeout")),
            (limit_rate_entry, _option_help("limit_rate")),
            (self._impersonate_combo, _option_help("impersonate")),
            (proxy_entry, _option_help("proxy")),
            (post_exec_entry, _option_help("post_exec")),
            (decryption_key_entry, _option_help("decryption_key")),
            (show_key_cb, "Temporarily reveal or mask the session decryption key"),
        ]

        self._widgets = [
            cb_m4b,
            format_combo,
            overwrite_combo,
            bitrate_combo,
            media_combo,
            cb_combine,
            cb_skip,
            cb_resume,
            cb_archive,
            cb_cover_only,
            cb_no_chapters,
            cb_split_chapters,
            cb_silence_chapters,
            cb_override_chapters,
            chapter_offset_entry,
            intro_chapter_combo,
            cb_json,
            cb_opf,
            cb_cue,
            cb_write_cover,
            cb_abs_meta,
            cb_upload_abs,
            cb_square_cover,
            max_cover_entry,
            cb_loudnorm,
            cb_mono,
            remove_chars_entry,
            max_workers_spin,
            book_delay_spin,
            timeout_spin,
            limit_rate_entry,
            self._impersonate_combo,
            proxy_entry,
            post_exec_entry,
            decryption_key_entry,
            show_key_cb,
            cb_enrich,
            cb_enrich_interactive,
            enrich_providers_entry,
            cb_debug,
        ]

    def _attach_tooltips(self) -> None:
        for widget, help_text in self._tooltip_bindings:
            attach_tooltip(widget, help_text, palette_provider=self._palette_provider)

    def _wire_dependencies(self) -> None:
        if self._dependency_traces_wired:
            return
        self._dependency_traces_wired = True
        for var in (
            self._vars.enrich_metadata_var,
            self._vars.cover_only_var,
            self._vars.no_chapters_var,
            self._vars.split_chapters_var,
            self._vars.combine_var,
        ):
            var.trace_add("write", lambda *_args: self.sync_dependency_states())
        for entry in (
            self._chapter_offset_entry,
            self._max_cover_entry,
            self._limit_rate_entry,
            self._decryption_key_entry,
        ):
            entry.bind("<KeyRelease>", lambda _e: self._validate_inline_fields(), add="+")
            entry.bind("<FocusOut>", lambda _e: self._validate_inline_fields(), add="+")

    def _toggle_decryption_key_visibility(self) -> None:
        self._decryption_key_entry.configure(
            show="" if self._show_decryption_key_var.get() else "*"
        )

    def _on_media_selected(self, _event: tk.Event | None = None) -> None:
        if self._vars.media_type_var.get().strip().lower() == "video":
            cur_fmt = self._vars.output_format_var.get().strip().lower()
            if cur_fmt in ("", "original", "m4b"):
                self._vars.output_format_var.set("mp4")
                self._vars.m4b_var.set(False)
        self.sync_dependency_states()

    def _on_split_chapters_clicked(self) -> None:
        if self._vars.split_chapters_var.get() and self._vars.combine_var.get():
            self._vars.combine_var.set(False)
        self.sync_dependency_states()

    def _on_combine_clicked(self) -> None:
        if self._vars.combine_var.get() and self._vars.split_chapters_var.get():
            self._vars.split_chapters_var.set(False)
        self.sync_dependency_states()

    def _validate_inline_fields(self) -> None:
        errors: list[str] = []
        raw_offset = self._vars.chapter_offset_ms_var.get().strip()
        if raw_offset:
            try:
                int(raw_offset)
            except ValueError:
                errors.append("Offset (ms) must be a whole integer")
        raw_max_cover = self._vars.max_cover_size_var.get().strip()
        if raw_max_cover:
            try:
                if int(raw_max_cover) < 1:
                    errors.append("Max cover must be >= 1")
            except ValueError:
                errors.append("Max cover must be a positive integer")
        raw_rate = self._vars.limit_rate_var.get().strip()
        if raw_rate:
            try:
                parse_byte_rate(raw_rate)
            except ValueError:
                errors.append("Limit rate is invalid (e.g. 2M, 500K)")
        raw_dec_key = self._vars.decryption_key_var.get().strip()
        if raw_dec_key:
            try:
                parse_decryption_key_spec(raw_dec_key)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            self._validation_var.set(" • ".join(errors))
            self._validation_label.grid()
        else:
            self._validation_var.set("")
            self._validation_label.grid_remove()

    def update_custom_providers_hint(self, custom_names: tuple[str, ...] | list[str]) -> None:
        """Update the provider hint label to include configured custom providers."""
        base = "audnexus, itunes, google, openlibrary"
        cleaned = [str(n).strip() for n in custom_names if str(n).strip()]
        if cleaned:
            self._enrich_providers_hint_var.set(f"{base} + custom: {', '.join(cleaned)}")
        else:
            self._enrich_providers_hint_var.set(base)

    def sync_dependency_states(self) -> None:
        """Update enabled/disabled states for dependent controls when idle."""
        if not self._panel_enabled:
            return
        cover_only = bool(self._vars.cover_only_var.get())
        no_chapters = bool(self._vars.no_chapters_var.get())
        enrich_on = bool(self._vars.enrich_metadata_var.get())
        split_on = bool(self._vars.split_chapters_var.get())

        def _set_widget(widget: OptionWidget, enabled: bool, *, readonly_combo: bool = False) -> None:
            if not enabled:
                widget.configure(state="disabled")
            elif readonly_combo:
                widget.configure(state="readonly")
            else:
                widget.configure(state="normal")

        _set_widget(self._cb_enrich_interactive, enrich_on)
        _set_widget(self._enrich_providers_entry, enrich_on)

        _set_widget(self._cb_m4b, not cover_only)
        _set_widget(self._format_combo, not cover_only, readonly_combo=True)
        _set_widget(self._bitrate_combo, not cover_only, readonly_combo=True)
        _set_widget(self._cb_loudnorm, not cover_only)
        _set_widget(self._cb_mono, not cover_only)
        _set_widget(self._cb_combine, (not cover_only) and (not split_on))
        _set_widget(self._cb_split_chapters, (not cover_only) and (not no_chapters))
        _set_widget(self._cb_silence_chapters, (not cover_only) and (not no_chapters))
        _set_widget(self._cb_override_chapters, (not cover_only) and (not no_chapters))
        _set_widget(self._chapter_offset_entry, not no_chapters)
        _set_widget(self._intro_chapter_combo, (not cover_only) and (not no_chapters), readonly_combo=True)
        _set_widget(self._cb_cue, not no_chapters)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable interactive option controls cleanly."""
        self._panel_enabled = enabled
        for widget in self._widgets:
            if isinstance(widget, ttk.Combobox) and widget is not self._impersonate_combo:
                widget.configure(state="readonly" if enabled else "disabled")
            else:
                widget.configure(state="normal" if enabled else "disabled")
        if enabled:
            self.sync_dependency_states()

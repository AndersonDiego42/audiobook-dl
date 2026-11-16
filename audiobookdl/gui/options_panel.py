"""Three-tab options panel for output format, metadata sidecars, and network settings."""

import tkinter as tk
from tkinter import ttk
from typing import Any
from collections.abc import Callable

from audiobookdl.gui.formatting import MEDIA_TYPE_OPTIONS, OUTPUT_FORMATS, OVERWRITE_OPTIONS
from audiobookdl.utils import AUDIO_BITRATE_PRESETS

OptionWidget = ttk.Entry | ttk.Checkbutton | ttk.Combobox | ttk.Spinbox


class OptionsPanel(ttk.LabelFrame):
    """LabelFrame containing the General, Chapters & Sidecars, and Network & Performance tabs."""

    def __init__(
        self,
        master: tk.Misc,
        vars_owner: Any,
        *,
        on_m4b_toggled: Callable[[], None],
        on_format_selected: Callable[[tk.Event | None], None],
    ) -> None:
        super().__init__(master, text="Options", padding=(8, 4))
        self._vars = vars_owner
        self._on_m4b_toggled = on_m4b_toggled
        self._on_format_selected = on_format_selected
        self._widgets: list[OptionWidget] = []
        self._build_widgets()

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

        cb_combine = ttk.Checkbutton(general_tab, text="Combine parts", variable=self._vars.combine_var)
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
        ttk.Label(
            enrich_row,
            text="audnexus, itunes, google, openlibrary",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        sidecars_tab = ttk.Frame(self.options_notebook, padding=(8, 6))
        sidecars_tab.columnconfigure(3, weight=1)
        self.options_notebook.add(sidecars_tab, text="Chapters & Sidecars")

        cb_no_chapters = ttk.Checkbutton(
            sidecars_tab, text="No chapters", variable=self._vars.no_chapters_var
        )
        cb_no_chapters.grid(row=0, column=0, sticky="w", padx=(0, 14))

        cb_split_chapters = ttk.Checkbutton(
            sidecars_tab,
            text="Split chapters",
            variable=self._vars.split_chapters_var,
        )
        cb_split_chapters.grid(row=0, column=1, columnspan=2, sticky="w")

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
        cb_abs_meta.grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 14), pady=(4, 0))

        remove_row = ttk.Frame(sidecars_tab)
        remove_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
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

        ttk.Label(
            network_tab,
            text="Defaults: 6 workers, unlimited bandwidth (e.g. 2M), TLS impersonate via curl_cffi (e.g. chrome)",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

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
            cb_json,
            cb_opf,
            cb_cue,
            cb_write_cover,
            cb_abs_meta,
            remove_chars_entry,
            max_workers_spin,
            book_delay_spin,
            timeout_spin,
            limit_rate_entry,
            self._impersonate_combo,
            proxy_entry,
            cb_enrich,
            cb_enrich_interactive,
            enrich_providers_entry,
            cb_debug,
        ]

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable interactive option controls cleanly."""
        for widget in self._widgets:
            if isinstance(widget, ttk.Combobox) and widget is not self._impersonate_combo:
                widget.configure(state="readonly" if enabled else "disabled")
            else:
                widget.configure(state="normal" if enabled else "disabled")

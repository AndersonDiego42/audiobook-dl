"""Modal dialog displaying editable audiobook metadata/cover or a selectable series checklist."""

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any
from collections.abc import Callable, Sequence

from audiobookdl.gui.chapter_editor import ChapterEditorFrame
from audiobookdl.gui.formatting import (
    apply_audiobook_metadata_edits,
    describe_series_book,
    format_audiobook_preview,
    format_multi_metadata_values,
    load_cover_from_file,
    render_cover_thumbnail,
)
from audiobookdl.gui.theme import (
    ThemePalette,
    apply_theme_to_widget_tree,
    make_bold_font,
    scale_px,
)
from audiobookdl.gui.widgets import (
    MainThreadCallbackQueue,
    ModalDialog,
    ScrollableFrame,
    ScrolledTreeview,
    TreeColumnSpec,
)
from audiobookdl.sources.source import Source
from audiobookdl.utils.audiobook import Audiobook, AudiobookMetadata, BookId, Cover, Result, Series
from audiobookdl.utils.enrichment import (
    EnrichmentCandidate,
    apply_candidate_to_audiobook,
    search_enrichment_candidates,
)


_EDITABLE_PREVIEW_LABELS = frozenset(
    {
        "Title",
        "Author(s)",
        "Narrator(s)",
        "Series",
        "Genre(s)",
        "Publisher",
        "Released",
        "Language",
        "ISBN",
        "ASIN",
    }
)


class ChoiceDialog(ModalDialog):
    """Modal selection dialog returning a 0-based index or `None` when skipped/cancelled."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        choices: Sequence[str],
        *,
        default: int | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            title,
            palette=palette,
            default_size=(540, 320),
            min_size=(520, 280),
        )
        self.selected_index: int | None = None

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        ttk.Label(body, text=title, font=make_bold_font(self, 10)).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        list_frame = ttk.Frame(body)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self._listbox = tk.Listbox(list_frame, activestyle="none", exportselection=False)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scroll.set)
        self._listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        for item in choices:
            self._listbox.insert(tk.END, item)
        if choices:
            initial = default if (default is not None and 0 <= default < len(choices)) else 0
            self._listbox.selection_set(initial)
            self._listbox.activate(initial)
            self._listbox.see(initial)

        self._listbox.bind("<Double-Button-1>", lambda _e: self._confirm())
        self._listbox.bind("<Return>", lambda _e: self._confirm())

        btns = ttk.Frame(body)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Select", command=self._confirm).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Skip", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.finalize_modal(initial_focus=self._listbox)

    def _confirm(self) -> None:
        sel = self._listbox.curselection()  # type: ignore[no-untyped-call]
        if sel:
            self.selected_index = int(sel[0])
        self.destroy()


class _EnrichmentPickerDialog(ModalDialog):
    """Modal table allowing the user to inspect and select an `EnrichmentCandidate`."""

    _COLUMNS = (
        TreeColumnSpec("provider", "Provider", width=90, stretch=False, anchor="w"),
        TreeColumnSpec("score", "Match", width=64, stretch=False, anchor="center", sort_kind="number"),
        TreeColumnSpec("title", "Title", width=200, stretch=True, anchor="w"),
        TreeColumnSpec("authors", "Author(s)", width=150, stretch=True, anchor="w"),
        TreeColumnSpec("narrators", "Narrator(s)", width=140, stretch=True, anchor="w"),
        TreeColumnSpec("extras", "Details", width=160, stretch=True, anchor="w"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        candidates: Sequence[EnrichmentCandidate],
        *,
        current_metadata: AudiobookMetadata | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "Select Enrichment Match",
            palette=palette,
            default_size=(820, 480),
            min_size=(780, 440),
        )
        self.selected_candidate: EnrichmentCandidate | None = None
        self._candidates = list(candidates)
        self._current_metadata = current_metadata

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        ttk.Label(
            body,
            text="Choose a metadata match (missing fields, cover, and chapters will be filled):",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        table = ScrolledTreeview(body, self._COLUMNS, selectmode="browse", height=8)
        table.grid(row=1, column=0, sticky="nsew")
        self._tree = table.tree

        for idx, cand in enumerate(self._candidates):
            extras: list[str] = []
            if cand.release_date:
                extras.append(str(cand.release_date.year))
            if cand.asin:
                extras.append(f"ASIN:{cand.asin}")
            elif cand.isbn:
                extras.append(f"ISBN:{cand.isbn}")
            if cand.cover_url:
                extras.append("Cover")
            if cand.provider == "audnexus" and cand.asin:
                extras.append("Chapters")
            self._tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    cand.provider,
                    f"{round(cand.score * 100)}%",
                    cand.title,
                    ", ".join(cand.authors),
                    ", ".join(cand.narrators),
                    " | ".join(extras),
                ),
            )

        diff_frame = ttk.LabelFrame(body, text="Current vs Selected Match", padding=scale_px(self, 6))
        diff_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        diff_frame.columnconfigure(0, weight=1)
        self._diff_text = tk.Text(diff_frame, height=6, wrap="word", state="disabled")
        self._diff_text.grid(row=0, column=0, sticky="ew")

        self._tree.bind("<<TreeviewSelect>>", self._render_candidate_diff)
        if self._candidates:
            self._tree.selection_set("0")
            self._tree.focus("0")
            self._render_candidate_diff()

        self._tree.bind("<Double-1>", lambda _e: self._apply_selected())
        self._tree.bind("<Return>", lambda _e: self._apply_selected())

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Apply Selected", command=self._apply_selected).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.finalize_modal(initial_focus=self._tree)

    def _render_candidate_diff(self, _event: tk.Event | None = None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._candidates)):
            return
        cand = self._candidates[idx]
        cur = self._current_metadata
        pairs = [
            ("Title", cur.title if cur else "", cand.title),
            ("Authors", cur.author if cur else "", "; ".join(cand.authors)),
            ("Narrators", cur.narrator if cur else "", "; ".join(cand.narrators)),
            ("Series", (cur.series or "") if cur else "", cand.series or ""),
            ("Publisher", (cur.publisher or "") if cur else "", cand.publisher or ""),
            (
                "Release",
                str(cur.release_date) if (cur and cur.release_date) else "",
                str(cand.release_date) if cand.release_date else "",
            ),
            ("Genres", cur.genre if cur else "", "; ".join(cand.genres)),
            ("ISBN / ASIN", f"{cur.isbn or '-'} / {cur.asin or '-'}" if cur else "", f"{cand.isbn or '-'} / {cand.asin or '-'}"),
        ]
        lines = [f"{label}:  [{cur_v or 'empty'}]  ->  [{cand_v or 'empty'}]" for label, cur_v, cand_v in pairs]
        self._diff_text.configure(state="normal")
        self._diff_text.delete("1.0", tk.END)
        self._diff_text.insert("1.0", "\n".join(lines))
        self._diff_text.configure(state="disabled")

    def _apply_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._candidates):
            self.selected_candidate = self._candidates[idx]
        self.destroy()


class PreviewDialog(ModalDialog):
    """Modal `Toplevel` window hosting editable book metadata or a series selection checklist."""

    _SERIES_COLUMNS = (
        TreeColumnSpec("selected", "Download", width=72, stretch=False, anchor="center"),
        TreeColumnSpec("book", "Book", width=420, stretch=True, anchor="w"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        url: str,
        source: Source,
        result: Result,
        *,
        is_running: bool,
        skip_downloaded: bool,
        series_book_exists: Callable[[BookId[Any] | Audiobook], bool],
        on_confirm: Callable[[str, tuple[Source, Result], list[int] | None], None],
        palette: ThemePalette | None = None,
        on_search_enrichment: Callable[[Audiobook], list[EnrichmentCandidate]] | None = None,
        on_apply_enrichment: Callable[[Audiobook, EnrichmentCandidate], tuple[list[str], bool, bool]] | None = None,
        manage_source_lifecycle: bool = True,
        confirm_button_text: str | None = None,
    ) -> None:
        super().__init__(
            parent,
            f"Preview \u2014 {result.title}",
            palette=palette,
            default_size=(820, 560) if isinstance(result, Audiobook) else (720, 540),
            min_size=(560, 420),
        )
        self._url = url
        self._source = source
        self._result = result
        self._is_running = is_running
        self._skip_downloaded = skip_downloaded
        self._series_book_exists = series_book_exists
        self._on_confirm = on_confirm
        self._on_search_enrichment = on_search_enrichment
        self._on_apply_enrichment = on_apply_enrichment
        self._manage_source_lifecycle = manage_source_lifecycle
        self._confirm_button_text = confirm_button_text
        self._source_transferred = False
        self._source_closed = False
        self._cb_queue = MainThreadCallbackQueue(self)
        self._initial_focus_widget: tk.Misc | None = None

        self._cover_photo: Any | None = None
        self._pending_cover: Cover | None = result.cover if isinstance(result, Audiobook) else None
        self._cover_modified = False
        self._cover_label: ttk.Label | None = None
        self._clear_cover_btn: ttk.Button | None = None
        self._readonly_frame: ttk.Frame | None = None
        self._enrich_btn: ttk.Button | None = None
        self._enrich_status_var = tk.StringVar(value="")

        self._title_var = tk.StringVar()
        self._authors_var = tk.StringVar()
        self._narrators_var = tk.StringVar()
        self._series_var = tk.StringVar()
        self._series_order_var = tk.StringVar()
        self._genres_var = tk.StringVar()
        self._publisher_var = tk.StringVar()
        self._release_date_var = tk.StringVar()
        self._language_var = tk.StringVar()
        self._isbn_var = tk.StringVar()
        self._asin_var = tk.StringVar()
        self._desc_text: ScrolledText | None = None
        self._meta_scroll: ScrollableFrame | None = None
        self._chapter_editor: ChapterEditorFrame | None = None

        self._series_ref: Series[Any] | None = None
        self._series_title_var = tk.StringVar()
        self._preview_book_btn: ttk.Button | None = None
        self._check_vars: list[tk.BooleanVar] = []
        self._filter_var = tk.StringVar()
        self._filter_after_id: str | None = None
        self._item_labels: list[str] = []
        self._last_clicked_index: int | None = None
        self._tree: ttk.Treeview | None = None

        body = ttk.Frame(self, padding=scale_px(self, 14))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)

        if isinstance(result, Audiobook):
            self._build_audiobook_view(body, result)
        else:
            self._build_series_view(body, result)

        self.finalize_modal(initial_focus=self._initial_focus_widget)
        self.bind("<Destroy>", self._on_dialog_destroy, add="+")

    def _close_untransferred_source(self) -> None:
        if not self._manage_source_lifecycle or self._source_transferred or self._source_closed:
            return
        self._source_closed = True
        close_fn = getattr(self._source, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def _on_dialog_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self:
            self._close_untransferred_source()

    def destroy(self) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        self._close_untransferred_source()
        super().destroy()

    def _refresh_cover_preview(self) -> None:
        if self._cover_label is None:
            return
        max_size = scale_px(self, 180)
        photo = render_cover_thumbnail(self._pending_cover, self, max_size=max_size)
        if photo is not None:
            self._cover_photo = photo
            self._cover_label.configure(
                image=photo,
                text="",
                width=0,
                relief="flat",
                padding=0,
            )
        else:
            self._cover_photo = None
            self._cover_label.configure(
                image="",
                text="No Cover",
                width=20,
                relief="groove",
                padding=scale_px(self, 24),
            )
        if self._clear_cover_btn is not None:
            self._clear_cover_btn.configure(
                state="normal" if self._pending_cover is not None else "disabled"
            )

    def _choose_cover_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select Cover Image",
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            cover = load_cover_from_file(path)
        except Exception as exc:
            messagebox.showerror(
                "Invalid Cover Image",
                f"Could not load image:\n{exc}",
                parent=self,
            )
            return
        self._pending_cover = cover
        self._cover_modified = True
        self._refresh_cover_preview()

    def _clear_cover(self) -> None:
        self._pending_cover = None
        self._cover_modified = True
        self._refresh_cover_preview()

    def _apply_audiobook_edits(self, audiobook: Audiobook) -> bool:
        try:
            desc = (
                self._desc_text.get("1.0", tk.END).strip()
                if self._desc_text is not None
                else None
            )
            apply_audiobook_metadata_edits(
                audiobook,
                title=self._title_var.get(),
                authors_text=self._authors_var.get(),
                narrators_text=self._narrators_var.get(),
                series_text=self._series_var.get(),
                series_order_text=self._series_order_var.get(),
                cover=self._pending_cover,
                update_cover=self._cover_modified,
                genres_text=self._genres_var.get(),
                publisher=self._publisher_var.get(),
                release_date_text=self._release_date_var.get(),
                language_text=self._language_var.get(),
                isbn=self._isbn_var.get(),
                asin=self._asin_var.get(),
                description=desc,
            )
            if self._chapter_editor is not None:
                audiobook.chapters = self._chapter_editor.get_chapters()
            return True
        except ValueError as exc:
            messagebox.showwarning("Invalid Metadata", str(exc), parent=self)
            return False

    def _build_audiobook_view(self, body: ttk.Frame, audiobook: Audiobook) -> None:
        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        cover_frame = ttk.Frame(top)
        cover_frame.grid(row=0, column=0, sticky="nw", padx=(0, scale_px(self, 14)))
        self._cover_label = ttk.Label(cover_frame, anchor="center")
        self._cover_label.pack()

        cover_btns = ttk.Frame(cover_frame)
        cover_btns.pack(fill=tk.X, pady=(scale_px(self, 8), 0))
        self._change_cover_btn = ttk.Button(
            cover_btns,
            text="Change Cover...",
            command=self._choose_cover_file,
        )
        self._change_cover_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._clear_cover_btn = ttk.Button(
            cover_btns,
            text="Clear Cover",
            command=self._clear_cover,
        )
        self._clear_cover_btn.pack(side=tk.LEFT)
        self._refresh_cover_preview()

        self._sync_editable_vars_from_audiobook(audiobook)

        right_nb = ttk.Notebook(top)
        right_nb.grid(row=0, column=1, sticky="nsew")

        pad = scale_px(self, 8)
        self._meta_scroll = ScrollableFrame(
            right_nb,
            content_padding=(pad, pad),
            inner_scrollables=lambda: (self._desc_text,) if self._desc_text is not None else (),
        )
        right_nb.add(self._meta_scroll, text="Metadata")
        meta_frame = self._meta_scroll.content_frame
        meta_frame.columnconfigure(1, weight=1)
        meta_frame.columnconfigure(3, weight=1)
        self._meta_scroll.apply_theme(self._palette)

        full_rows: tuple[tuple[str, tk.StringVar], ...] = (
            ("Title", self._title_var),
            ("Author(s)", self._authors_var),
            ("Narrator(s)", self._narrators_var),
        )
        row_idx = 0
        for label, var in full_rows:
            ttk.Label(
                meta_frame,
                text=f"{label}:",
                font=make_bold_font(self, 9),
            ).grid(row=row_idx, column=0, sticky="w", padx=(0, 8), pady=2)
            entry = ttk.Entry(meta_frame, textvariable=var)
            entry.grid(
                row=row_idx, column=1, columnspan=3, sticky="ew", pady=2
            )
            if self._initial_focus_widget is None:
                self._initial_focus_widget = entry
            row_idx += 1

        ttk.Label(meta_frame, text="Series:", font=make_bold_font(self, 9)).grid(
            row=row_idx, column=0, sticky="w", padx=(0, 8), pady=2
        )
        ttk.Entry(meta_frame, textvariable=self._series_var).grid(
            row=row_idx, column=1, sticky="ew", padx=(0, 8), pady=2
        )
        ttk.Label(meta_frame, text="Order:", font=make_bold_font(self, 9)).grid(
            row=row_idx, column=2, sticky="w", padx=(0, 6), pady=2
        )
        ttk.Entry(meta_frame, textvariable=self._series_order_var, width=10).grid(
            row=row_idx, column=3, sticky="w", pady=2
        )
        row_idx += 1

        paired_rows: tuple[tuple[str, tk.StringVar, str, tk.StringVar], ...] = (
            ("Genre(s)", self._genres_var, "Language", self._language_var),
            ("Publisher", self._publisher_var, "Released", self._release_date_var),
            ("ISBN", self._isbn_var, "ASIN", self._asin_var),
        )
        for l1, v1, l2, v2 in paired_rows:
            ttk.Label(meta_frame, text=f"{l1}:", font=make_bold_font(self, 9)).grid(
                row=row_idx, column=0, sticky="w", padx=(0, 8), pady=2
            )
            ttk.Entry(meta_frame, textvariable=v1).grid(
                row=row_idx, column=1, sticky="ew", padx=(0, 8), pady=2
            )
            ttk.Label(meta_frame, text=f"{l2}:", font=make_bold_font(self, 9)).grid(
                row=row_idx, column=2, sticky="w", padx=(0, 6), pady=2
            )
            ttk.Entry(meta_frame, textvariable=v2).grid(
                row=row_idx, column=3, sticky="ew", pady=2
            )
            row_idx += 1

        ttk.Label(
            meta_frame,
            text="Separate multiple authors, narrators, or genres with ';'",
            style="Muted.TLabel",
        ).grid(row=row_idx, column=1, columnspan=3, sticky="w", pady=(0, 4))
        row_idx += 1

        ttk.Label(meta_frame, text="Description:", font=make_bold_font(self, 9)).grid(
            row=row_idx, column=0, sticky="nw", padx=(0, 8), pady=(2, 2)
        )
        self._desc_text = ScrolledText(meta_frame, height=4, width=34, wrap="word")
        self._desc_text.grid(row=row_idx, column=1, columnspan=3, sticky="nsew", pady=(2, 4))
        meta_frame.rowconfigure(row_idx, weight=1)
        if audiobook.metadata.description:
            self._desc_text.insert("1.0", audiobook.metadata.description.strip())
        row_idx += 1

        self._readonly_frame = ttk.Frame(meta_frame)
        self._readonly_frame.grid(row=row_idx, column=0, columnspan=4, sticky="ew")
        self._readonly_frame.columnconfigure(1, weight=1)
        self._render_readonly_metadata(audiobook)

        chapters_tab = ttk.Frame(right_nb, padding=scale_px(self, 6))
        chapters_tab.columnconfigure(0, weight=1)
        chapters_tab.rowconfigure(0, weight=1)
        right_nb.add(chapters_tab, text="Chapters")
        self._chapter_editor = ChapterEditorFrame(
            chapters_tab,
            palette=self._palette,
            on_change=lambda chs: self._on_chapters_edited(audiobook, chs),
        )
        self._chapter_editor.grid(row=0, column=0, sticky="nsew")
        self._chapter_editor.set_chapters(audiobook.chapters, source_label=self._source.name)

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._enrich_btn = ttk.Button(
            btn_bar,
            text="Enrich Metadata...",
            command=lambda: self._start_enrichment(audiobook),
        )
        self._enrich_btn.pack(side=tk.LEFT)
        ttk.Label(
            btn_bar,
            textvariable=self._enrich_status_var,
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))
        action_text = self._confirm_button_text or (
            "Add to Queue" if self._is_running else "Download"
        )

        def _confirm_book() -> None:
            if not self._apply_audiobook_edits(audiobook):
                return
            self._source_transferred = True
            self.destroy()
            self._on_confirm(self._url, (self._source, audiobook), None)

        ttk.Button(btn_bar, text=action_text, command=_confirm_book).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _on_chapters_edited(self, audiobook: Audiobook, chapters: list[Any]) -> None:
        audiobook.chapters = chapters
        self._render_readonly_metadata(audiobook)

    def _render_readonly_metadata(self, audiobook: Audiobook) -> None:
        if self._readonly_frame is None:
            return
        for child in self._readonly_frame.winfo_children():
            child.destroy()

        readonly_rows = [
            (label, value)
            for label, value in format_audiobook_preview(audiobook)
            if label not in _EDITABLE_PREVIEW_LABELS
        ]
        row_idx = 0
        for label, value in readonly_rows:
            ttk.Label(
                self._readonly_frame,
                text=f"{label}:",
                font=make_bold_font(self, 9),
            ).grid(row=row_idx, column=0, sticky="nw", padx=(0, 8), pady=2)
            ttk.Label(
                self._readonly_frame,
                text=value,
                wraplength=scale_px(self, 320),
                justify="left",
            ).grid(row=row_idx, column=1, sticky="nw", pady=2)
            row_idx += 1

        apply_theme_to_widget_tree(self._readonly_frame, self._palette)

    def _is_alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _sync_editable_vars_from_audiobook(self, audiobook: Audiobook) -> None:
        meta = audiobook.metadata
        self._title_var.set(meta.title)
        self._authors_var.set(format_multi_metadata_values(meta.authors))
        self._narrators_var.set(format_multi_metadata_values(meta.narrators))
        self._series_var.set(meta.series or "")
        self._series_order_var.set("" if meta.series_order is None else str(meta.series_order))
        self._genres_var.set(format_multi_metadata_values(meta.genres))
        self._publisher_var.set(meta.publisher or "")
        self._release_date_var.set(str(meta.release_date) if meta.release_date is not None else "")
        lang_val = ""
        if meta.language is not None:
            lang_val = getattr(meta.language, "name", None) or str(meta.language)
        self._language_var.set(lang_val)
        self._isbn_var.set(meta.isbn or "")
        self._asin_var.set(meta.asin or "")
        if self._desc_text is not None:
            self._desc_text.delete("1.0", tk.END)
            if meta.description:
                self._desc_text.insert("1.0", meta.description.strip())

    def _start_enrichment(self, audiobook: Audiobook) -> None:
        if not self._apply_audiobook_edits(audiobook):
            return
        if self._enrich_btn is not None:
            self._enrich_btn.configure(state="disabled")
        self._enrich_status_var.set("Searching providers...")

        search_fn = self._on_search_enrichment or (
            lambda book: search_enrichment_candidates(
                book.metadata,
                session=getattr(self._source, "_session", None),
            )
        )

        self._cb_queue.begin_task()

        def _worker() -> None:
            candidates: list[EnrichmentCandidate] = []
            exc: Exception | None = None
            try:
                candidates = search_fn(audiobook)
            except Exception as err:
                exc = err
            finally:
                def _post_search() -> None:
                    self._on_enrichment_search_done(audiobook, candidates, exc)

                self._cb_queue.post(_post_search)
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_enrichment_search_done(
        self,
        audiobook: Audiobook,
        candidates: list[EnrichmentCandidate],
        exc: Exception | None,
    ) -> None:
        if not self._is_alive():
            return
        if self._enrich_btn is not None:
            self._enrich_btn.configure(state="normal")
        self._enrich_status_var.set("")
        if exc is not None:
            messagebox.showerror("Enrichment Failed", str(exc), parent=self)
            return
        if not candidates:
            messagebox.showinfo(
                "No Matches Found",
                "No matching enrichment metadata was found across configured providers.",
                parent=self,
            )
            return

        picker = _EnrichmentPickerDialog(
            self,
            candidates,
            current_metadata=audiobook.metadata,
            palette=self._palette,
        )
        self.wait_window(picker)
        if not self._is_alive():
            return
        chosen = picker.selected_candidate
        if chosen is None:
            return

        if self._enrich_btn is not None:
            self._enrich_btn.configure(state="disabled")
        self._enrich_status_var.set("Applying match...")

        apply_fn = self._on_apply_enrichment or (
            lambda book, cand: apply_candidate_to_audiobook(
                book,
                cand,
                session=getattr(self._source, "_session", None),
            )
        )

        self._cb_queue.begin_task()

        def _apply_worker() -> None:
            fields: list[str] = []
            cover_updated = False
            chapters_updated = False
            err_exc: Exception | None = None
            try:
                fields, cover_updated, chapters_updated = apply_fn(audiobook, chosen)
            except Exception as err:
                err_exc = err
            finally:
                def _post_apply() -> None:
                    self._on_enrichment_applied(
                        audiobook, fields, cover_updated, chapters_updated, err_exc
                    )

                self._cb_queue.post(_post_apply)
                self._cb_queue.finish_worker()

        threading.Thread(target=_apply_worker, daemon=True).start()

    def _on_enrichment_applied(
        self,
        audiobook: Audiobook,
        fields: list[str],
        cover_updated: bool,
        chapters_updated: bool,
        exc: Exception | None,
    ) -> None:
        if not self._is_alive():
            return
        if self._enrich_btn is not None:
            self._enrich_btn.configure(state="normal")
        if exc is not None:
            self._enrich_status_var.set("")
            messagebox.showerror("Enrichment Failed", str(exc), parent=self)
            return
        self._sync_editable_vars_from_audiobook(audiobook)
        if cover_updated:
            self._pending_cover = audiobook.cover
            self._cover_modified = True
            self._refresh_cover_preview()
        self._render_readonly_metadata(audiobook)
        if self._chapter_editor is not None:
            self._chapter_editor.set_chapters(audiobook.chapters, source_label="enriched")
        updates = list(fields)
        if cover_updated:
            updates.append("cover")
        if chapters_updated:
            updates.append(f"chapters({len(audiobook.chapters)})")
        if updates:
            self._enrich_status_var.set(f"Updated: {', '.join(updates)}")
        else:
            self._enrich_status_var.set("No missing fields updated")

    def _build_series_view(self, body: ttk.Frame, series: Series[Any]) -> None:
        self._series_ref = series
        self._series_title_var.set(series.title)
        body.rowconfigure(2, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Series Title:", font=make_bold_font(self, 10)).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(header, textvariable=self._series_title_var).grid(
            row=0, column=1, sticky="ew"
        )
        book_count = len(series.books)
        ttk.Label(
            header,
            text=f"{self._source.name}  \u2022  {book_count} book{'s' if book_count != 1 else ''}",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self._selected_count_var = tk.StringVar()
        self._check_vars = [tk.BooleanVar(value=True) for _ in series.books]

        toolbar = ttk.Frame(body)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Select All", command=lambda: self._set_all(True)).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Deselect All", command=lambda: self._set_all(False)).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        deselect_existing_btn = ttk.Button(
            toolbar, text="Deselect Existing", command=self._deselect_existing
        )
        deselect_existing_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._preview_book_btn = ttk.Button(
            toolbar,
            text="Preview Selected Book...",
            command=self.preview_selected_book,
            state="disabled" if book_count == 0 else "normal",
        )
        self._preview_book_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        filter_entry = ttk.Entry(toolbar, textvariable=self._filter_var, width=20)
        filter_entry.pack(side=tk.LEFT, padx=(0, 8))
        self._initial_focus_widget = filter_entry
        ttk.Label(
            toolbar,
            textvariable=self._selected_count_var,
            style="Secondary.TLabel",
        ).pack(side=tk.RIGHT)
        self._update_selected_count(book_count)

        list_container = ttk.Frame(body, relief="sunken", borderwidth=1)
        list_container.grid(row=2, column=0, sticky="nsew")
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)

        if book_count == 0:
            ttk.Label(
                list_container,
                text="No downloadable books found in this series.",
                padding=20,
            ).grid(row=0, column=0)
        else:
            scrolled_tree = ScrolledTreeview(
                list_container,
                self._SERIES_COLUMNS,
                selectmode="browse",
                height=12,
            )
            scrolled_tree.grid(row=0, column=0, sticky="nsew")
            tree = scrolled_tree.tree
            self._tree = tree

            has_existing = False
            for idx, book in enumerate(series.books):
                label = f"{idx + 1}. {describe_series_book(idx, book)}"
                if self._series_book_exists(book):
                    label = f"{label} [Exists]"
                    has_existing = True
                    if self._skip_downloaded:
                        self._check_vars[idx].set(False)
                self._item_labels.append(label)
                tree.insert(
                    "",
                    tk.END,
                    iid=str(idx),
                    values=("[x]" if self._check_vars[idx].get() else "[ ]", label),
                )
            self._update_selected_count(book_count)
            if not has_existing:
                deselect_existing_btn.configure(state="disabled")
            visible = tree.get_children()
            if visible:
                tree.selection_set(visible[0])
                tree.focus(visible[0])
            self._sync_preview_book_btn()

            tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_preview_book_btn())
            tree.bind("<Button-1>", self._on_tree_click)
            tree.bind("<Double-1>", self._on_tree_double_click)
            tree.bind("<space>", self._on_tree_key)
            tree.bind("<Return>", self._on_tree_key)
            self._filter_var.trace_add("write", lambda *_args: self._schedule_filter())

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        action_text = "Queue Selected" if self._is_running else "Download Selected"

        def _confirm_series() -> None:
            indices = [i for i, v in enumerate(self._check_vars) if v.get()]
            if not indices:
                messagebox.showwarning(
                    "No Selection",
                    "Please select at least one book to download.",
                    parent=self,
                )
                return
            edited_series_title = self._series_title_var.get().strip()
            if edited_series_title:
                series.title = edited_series_title
            self._source_transferred = True
            self.destroy()
            self._on_confirm(self._url, (self._source, series), indices)

        ttk.Button(
            btn_bar,
            text=action_text,
            command=_confirm_series,
            state="disabled" if book_count == 0 else "normal",
        ).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _selected_series_book_index(self) -> int | None:
        if self._tree is None or self._series_ref is None:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        if 0 <= idx < len(self._series_ref.books):
            return idx
        return None

    def _sync_preview_book_btn(self) -> None:
        if self._preview_book_btn is None:
            return
        idx = self._selected_series_book_index()
        self._preview_book_btn.configure(state="normal" if idx is not None else "disabled")

    def preview_selected_book(self) -> None:
        """Inspect/enrich the currently selected book in a `Series` view."""
        if self._series_ref is None:
            return
        idx = self._selected_series_book_index()
        if idx is None:
            return
        book_item = self._series_ref.books[idx]
        if isinstance(book_item, Audiobook):
            self._open_series_book_subdialog(idx, book_item)
            return

        download_by_id = getattr(self._source, "download_from_id", None)
        if not callable(download_by_id):
            messagebox.showinfo(
                "Unavailable",
                "This source does not support inspecting individual lazy series entries.",
                parent=self,
            )
            return

        if self._preview_book_btn is not None:
            self._preview_book_btn.configure(state="disabled", text="Loading...")
        self._cb_queue.begin_task()

        def _worker() -> None:
            try:
                resolved = download_by_id(book_item.id)
                audiobook: Audiobook | None = None
                if isinstance(resolved, Audiobook):
                    audiobook = resolved
                elif isinstance(resolved, (list, tuple)):
                    for item in resolved:
                        if isinstance(item, Audiobook):
                            audiobook = item
                            break
                err: Exception | None = (
                    None
                    if audiobook is not None
                    else ValueError("Selected entry did not resolve to an audiobook.")
                )

                def _deliver_ok() -> None:
                    self._on_series_book_resolved(idx, audiobook, err)

                self._cb_queue.post(_deliver_ok)
            except Exception as exc:
                err_exc = exc

                def _deliver_err() -> None:
                    self._on_series_book_resolved(idx, None, err_exc)

                self._cb_queue.post(_deliver_err)
            finally:
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_series_book_resolved(
        self,
        idx: int,
        audiobook: Audiobook | None,
        exc: Exception | None,
    ) -> None:
        if not self._is_alive():
            return
        if self._preview_book_btn is not None:
            self._preview_book_btn.configure(text="Preview Selected Book...")
            self._sync_preview_book_btn()
        if exc is not None or audiobook is None:
            messagebox.showerror(
                "Preview Book Failed",
                str(exc or "Failed to load selected book."),
                parent=self,
            )
            return
        if (
            self._series_ref is not None
            and isinstance(self._series_ref.books, list)
            and 0 <= idx < len(self._series_ref.books)
        ):
            self._series_ref.books[idx] = audiobook
            self._refresh_series_row_label(idx, audiobook)
        self._open_series_book_subdialog(idx, audiobook)

    def _refresh_series_row_label(self, idx: int, book: Audiobook) -> None:
        if self._tree is None or not (0 <= idx < len(self._item_labels)):
            return
        label = f"{idx + 1}. {describe_series_book(idx, book)}"
        if self._series_book_exists(book):
            label = f"{label} [Exists]"
        self._item_labels[idx] = label
        if self._tree.exists(str(idx)):
            self._tree.set(str(idx), "book", label)

    def _open_series_book_subdialog(self, idx: int, audiobook: Audiobook) -> "PreviewDialog":
        def _on_book_confirmed(_u: str, pair: tuple[Source, Result], _sel: list[int] | None) -> None:
            updated = pair[1]
            if isinstance(updated, Audiobook) and self._series_ref is not None:
                if (
                    isinstance(self._series_ref.books, list)
                    and 0 <= idx < len(self._series_ref.books)
                ):
                    self._series_ref.books[idx] = updated
                    self._refresh_series_row_label(idx, updated)

        dlg = PreviewDialog(
            self,
            self._url,
            self._source,
            audiobook,
            is_running=self._is_running,
            skip_downloaded=self._skip_downloaded,
            series_book_exists=self._series_book_exists,
            on_confirm=_on_book_confirmed,
            palette=self._palette,
            on_search_enrichment=self._on_search_enrichment,
            on_apply_enrichment=self._on_apply_enrichment,
            manage_source_lifecycle=False,
            confirm_button_text="Apply to Series",
        )
        return dlg

    def _update_selected_count(self, book_count: int | None = None) -> None:
        total = len(self._check_vars) if book_count is None else book_count
        chosen = sum(1 for v in self._check_vars if v.get())
        self._selected_count_var.set(f"Selected: {chosen} / {total}")

    def _sync_tree_marks(self) -> None:
        if self._tree is None:
            return
        for idx_item in range(len(self._item_labels)):
            self._tree.set(str(idx_item), "selected", "[x]" if self._check_vars[idx_item].get() else "[ ]")

    def _deselect_existing(self) -> None:
        for idx_item, label_item in enumerate(self._item_labels):
            if label_item.endswith("[Exists]"):
                self._check_vars[idx_item].set(False)
        self._sync_tree_marks()
        self._update_selected_count()

    def _set_all(self, val: bool) -> None:
        query = self._filter_var.get().strip().lower()
        if query:
            for idx_item, label_item in enumerate(self._item_labels):
                if query in label_item.lower():
                    self._check_vars[idx_item].set(val)
        else:
            for v in self._check_vars:
                v.set(val)
        self._sync_tree_marks()
        self._update_selected_count()

    def _toggle_series_item(self, idx: int, shift_held: bool = False) -> None:
        if shift_held and self._last_clicked_index is not None:
            start, end = sorted((self._last_clicked_index, idx))
            target_val = self._check_vars[idx].get()
            query = self._filter_var.get().strip().lower()
            for pos in range(start, end + 1):
                if not query or query in self._item_labels[pos].lower():
                    self._check_vars[pos].set(target_val)
        self._last_clicked_index = idx
        self._sync_tree_marks()
        self._update_selected_count()

    def _on_tree_click(self, event: tk.Event) -> str | None:
        if self._tree is None:
            return None
        region = self._tree.identify_region(event.x, event.y)
        if region not in ("cell", "tree"):
            return None
        row_iid = self._tree.identify_row(event.y)
        if not row_iid:
            return None
        self._tree.focus(row_iid)
        self._tree.selection_set(row_iid)
        if self._tree.identify_column(event.x) == "#1":
            idx_row = int(row_iid)
            self._check_vars[idx_row].set(not self._check_vars[idx_row].get())
            self._toggle_series_item(idx_row, bool(getattr(event, "state", 0) & 0x0001))
            return "break"
        return None

    def _on_tree_double_click(self, event: tk.Event) -> str | None:
        if self._tree is None:
            return None
        if self._tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return None
        row_iid = self._tree.identify_row(event.y)
        if not row_iid:
            return None
        if self._tree.identify_column(event.x) != "#1":
            idx_row = int(row_iid)
            self._check_vars[idx_row].set(not self._check_vars[idx_row].get())
            self._toggle_series_item(idx_row, bool(getattr(event, "state", 0) & 0x0001))
            return "break"
        return None

    def _on_tree_key(self, event: tk.Event) -> str:
        if self._tree is None:
            return "break"
        visible = self._tree.get_children()
        if not visible:
            return "break"
        focused = self._tree.focus()
        if not focused or focused not in visible:
            focused = visible[0]
            self._tree.focus(focused)
            self._tree.selection_set(focused)
        idx_row = int(focused)
        self._check_vars[idx_row].set(not self._check_vars[idx_row].get())
        self._toggle_series_item(idx_row, bool(getattr(event, "state", 0) & 0x0001))
        return "break"

    _FILTER_DEBOUNCE_MS = 150

    def _schedule_filter(self) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        try:
            if self.winfo_exists():
                self._filter_after_id = self.after(
                    self._FILTER_DEBOUNCE_MS, self._apply_filter
                )
        except tk.TclError:
            self._filter_after_id = None

    def _apply_filter(self, *_args: object) -> None:
        if self._filter_after_id is not None:
            try:
                self.after_cancel(self._filter_after_id)
            except tk.TclError:
                pass
            self._filter_after_id = None
        if self._tree is None:
            return
        query = self._filter_var.get().strip().lower()
        visible_pos = 0
        for idx_item, item_label in enumerate(self._item_labels):
            iid = str(idx_item)
            if not query or query in item_label.lower():
                self._tree.reattach(iid, "", visible_pos)
                visible_pos += 1
            else:
                self._tree.detach(iid)
        visible = self._tree.get_children()
        focused = self._tree.focus()
        if focused not in visible:
            if visible:
                self._tree.focus(visible[0])
                self._tree.selection_set(visible[0])
            else:
                self._tree.focus("")
                self._tree.selection_set(())

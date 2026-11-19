"""Page for discovering, previewing, and queuing local audiobook repackaging."""

from __future__ import annotations

import dataclasses
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any
from collections.abc import Callable, Sequence

from audiobookdl.options_schema import DownloadOptions
from audiobookdl.exceptions import AudiobookDLException, DownloadCancelled
from audiobookdl.gui.chapter_editor import ChapterEditorFrame
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.gui.formatting import format_duration
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ModalDialog, ScrolledTreeview, TreeColumnSpec, tree_item_values
from audiobookdl.pack import PackBookSpec, PackOverrides, scope_overrides_for_batch
from audiobookdl.utils.audiobook import Chapter, parse_language, parse_release_date


def _parse_series_order(raw_order: str) -> int | None:
    cleaned = raw_order.strip()
    if not cleaned:
        return None
    try:
        num = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid series order {cleaned!r}") from exc
    if not num.is_integer():
        raise ValueError(f"Series order must be a whole integer, got {cleaned!r}")
    return int(num)


class _PackBookEditDialog(ModalDialog):
    """Modal dialog for overriding a single detected book's Title, Author(s), and Series #."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        authors: Sequence[str],
        series_order: int | None,
        palette: ThemePalette,
    ) -> None:
        super().__init__(
            parent,
            "Edit Detected Book",
            palette=palette,
            resizable=(False, False),
        )
        self.result: dict[str, Any] | None = None

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._title_var = tk.StringVar(value=title)
        self._authors_var = tk.StringVar(value="; ".join(authors))
        self._order_var = tk.StringVar(value="" if series_order is None else str(series_order))

        ttk.Label(body, text="Title:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        title_entry = ttk.Entry(body, textvariable=self._title_var, width=40)
        title_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Author(s):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(body, textvariable=self._authors_var, width=40).grid(
            row=1, column=1, sticky="ew", pady=4
        )

        ttk.Label(body, text="Series #:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(body, textvariable=self._order_var, width=10).grid(
            row=2, column=1, sticky="w", pady=4
        )

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._save())
        self.finalize_modal(initial_focus=title_entry)

    def _save(self) -> None:
        title = self._title_var.get().strip()
        if not title:
            messagebox.showwarning("Invalid Book", "Title cannot be empty.", parent=self)
            return
        try:
            series_order = _parse_series_order(self._order_var.get())
        except ValueError as exc:
            messagebox.showwarning("Invalid Series #", str(exc), parent=self)
            return
        authors = [part.strip() for part in self._authors_var.get().split(";") if part.strip()]
        self.result = {
            "title": title,
            "authors": authors,
            "series_order": series_order,
        }
        self.destroy()


class PackPage(ttk.Frame):
    """Page for inspecting local audio folders and queuing `.m4b` repackaging."""

    _PREVIEW_COLUMNS = (
        TreeColumnSpec("title", "Title", width=180, stretch=True),
        TreeColumnSpec("authors", "Author", width=130, stretch=True),
        TreeColumnSpec("tracks", "Tracks", width=60, stretch=False, anchor="e", sort_kind="number"),
        TreeColumnSpec("chapters", "Chapters", width=70, stretch=False, anchor="e", sort_kind="number"),
        TreeColumnSpec("duration", "Duration", width=80, stretch=False, anchor="e", sort_kind="duration"),
        TreeColumnSpec("output", "Target Path", width=220, stretch=True),
    )

    def __init__(
        self,
        parent: tk.Misc,
        controller: GuiController,
        *,
        options_provider: Callable[[], DownloadOptions | None],
        output_dir_provider: Callable[[], str],
        ensure_ffmpeg: Callable[[DownloadOptions], bool],
        on_submit: Callable[[list[PackJobRequest], str], None],
        initial_paths: Sequence[str] = (),
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent, padding=scale_px(parent, 12))
        self._controller = controller
        self._options_provider = options_provider
        self._output_dir_provider = output_dir_provider
        self._ensure_ffmpeg = ensure_ffmpeg
        self._on_submit = on_submit
        self._palette = palette or LIGHT_PALETTE
        self._paths: list[str] = []
        self._preview_specs: list[PackBookSpec] = []
        self._edited_chapters_by_key: dict[str, list[Chapter]] = {}
        self._book_overrides_by_key: dict[str, dict[str, Any]] = {}
        self._scan_cancel_event: threading.Event | None = None
        self._prune_edits_on_scan: bool = False
        self._scan_token = 0
        self._is_scanning = False
        self._cb_queue = MainThreadCallbackQueue(self)
        self.bind("<Destroy>", self._on_destroy_page, add="+")

        default_fmt = "m4b"
        try:
            init_opts = options_provider()
            if init_opts and init_opts.output_format:
                default_fmt = init_opts.output_format
        except Exception:
            pass

        self.recursive_var = tk.BooleanVar(value=False)
        self.delete_source_var = tk.BooleanVar(value=False)
        self.format_var = tk.StringVar(value=default_fmt)
        self.title_var = tk.StringVar()
        self.author_var = tk.StringVar()
        self.narrator_var = tk.StringVar()
        self.series_var = tk.StringVar()
        self.series_order_var = tk.StringVar()
        self.asin_var = tk.StringVar()
        self.isbn_var = tk.StringVar()
        self.cover_path_var = tk.StringVar()
        self.genres_var = tk.StringVar()
        self.publisher_var = tk.StringVar()
        self.release_date_var = tk.StringVar()
        self.language_var = tk.StringVar()
        self.description_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Add one or more local folders or audio tracks to preview.")
        self._output_label_var = tk.StringVar(value=f"Output folder: {self._output_dir_provider()}")

        self._build_ui()
        if initial_paths:
            self.add_initial_paths(initial_paths)

    def add_initial_paths(self, paths: Sequence[str]) -> None:
        """Append existing file/folder paths and trigger a metadata scan."""
        added = False
        for p in paths:
            if p and os.path.exists(p):
                before = len(self._paths)
                self._append_path(p)
                if len(self._paths) > before:
                    added = True
        if added or (paths and self._paths):
            self._start_scan()

    def on_show(self) -> None:
        """Refresh output folder label when navigating to the Pack page."""
        self._output_label_var.set(f"Output folder: {self._output_dir_provider()}")

    def apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        if hasattr(self, "_chapter_editor"):
            self._chapter_editor.apply_theme(palette)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Pack Local Audiobooks", font=make_bold_font(self, 13)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Combine local audio folders or track files into tagged .m4b / audio packages.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        # Row 1: Source Paths
        sources_frame = ttk.LabelFrame(self, text="Local Sources", padding=scale_px(self, 8))
        sources_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        sources_frame.columnconfigure(0, weight=1)

        list_row = ttk.Frame(sources_frame)
        list_row.grid(row=0, column=0, sticky="ew")
        list_row.columnconfigure(0, weight=1)

        self._paths_listbox = tk.Listbox(list_row, height=4, selectmode=tk.EXTENDED)
        self._paths_listbox.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        src_btns = ttk.Frame(list_row)
        src_btns.grid(row=0, column=1, sticky="ns")
        ttk.Button(src_btns, text="Add Folder...", command=self._add_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(src_btns, text="Add Tracks...", command=self._add_tracks).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(src_btns, text="Remove", command=self._remove_selected_paths).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(src_btns, text="Clear All", command=self._clear_all_paths).pack(fill=tk.X)

        opts_row = ttk.Frame(sources_frame)
        opts_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(
            opts_row,
            text="Recursive subfolders (each folder with tracks = 1 book)",
            variable=self.recursive_var,
            command=self._on_scan_option_changed,
        ).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Checkbutton(
            opts_row,
            text="Delete source tracks after packing",
            variable=self.delete_source_var,
        ).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(opts_row, text="Format:").pack(side=tk.LEFT, padx=(0, 4))
        format_combo = ttk.Combobox(
            opts_row,
            textvariable=self.format_var,
            values=("m4b", "m4a", "mp3", "ogg", "opus"),
            state="readonly",
            width=7,
        )
        format_combo.pack(side=tk.LEFT)
        format_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_scan_option_changed())

        # Row 2: Optional Metadata Overrides (Basic & Extended tabs)
        meta_nb = ttk.Notebook(self)
        meta_nb.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        meta_frame = ttk.Frame(meta_nb, padding=scale_px(self, 8))
        meta_frame.columnconfigure(1, weight=1)
        meta_frame.columnconfigure(3, weight=1)
        meta_nb.add(meta_frame, text="Metadata Overrides")

        ttk.Label(meta_frame, text="Title:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(meta_frame, textvariable=self.title_var).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(meta_frame, text="Author(s):").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(meta_frame, textvariable=self.author_var).grid(row=0, column=3, sticky="ew")

        ttk.Label(meta_frame, text="Series:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        series_row = ttk.Frame(meta_frame)
        series_row.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        series_row.columnconfigure(0, weight=1)
        ttk.Entry(series_row, textvariable=self.series_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(series_row, text="#").grid(row=0, column=1, padx=(0, 2))
        ttk.Entry(series_row, textvariable=self.series_order_var, width=6).grid(row=0, column=2)

        ttk.Label(meta_frame, text="Narrator(s):").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(meta_frame, textvariable=self.narrator_var).grid(row=1, column=3, sticky="ew", pady=(6, 0))

        ttk.Label(meta_frame, text="ASIN:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        id_row = ttk.Frame(meta_frame)
        id_row.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        id_row.columnconfigure(0, weight=1)
        id_row.columnconfigure(2, weight=1)
        ttk.Entry(id_row, textvariable=self.asin_var, width=14).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(id_row, text="ISBN:").grid(row=0, column=1, padx=(0, 4))
        ttk.Entry(id_row, textvariable=self.isbn_var, width=16).grid(row=0, column=2, sticky="ew")

        ttk.Label(meta_frame, text="Cover image:").grid(row=2, column=2, sticky="w", padx=(0, 6), pady=(6, 0))
        cover_row = ttk.Frame(meta_frame)
        cover_row.grid(row=2, column=3, sticky="ew", pady=(6, 0))
        cover_row.columnconfigure(0, weight=1)
        ttk.Entry(cover_row, textvariable=self.cover_path_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(cover_row, text="Browse...", command=self._browse_cover).grid(row=0, column=1)

        ext_frame = ttk.Frame(meta_nb, padding=scale_px(self, 8))
        ext_frame.columnconfigure(1, weight=1)
        ext_frame.columnconfigure(3, weight=1)
        meta_nb.add(ext_frame, text="Extended Metadata")

        ttk.Label(ext_frame, text="Genre(s):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(ext_frame, textvariable=self.genres_var).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(ext_frame, text="Publisher:").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(ext_frame, textvariable=self.publisher_var).grid(row=0, column=3, sticky="ew")

        ttk.Label(ext_frame, text="Released:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(ext_frame, textvariable=self.release_date_var).grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        ttk.Label(ext_frame, text="Language:").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(ext_frame, textvariable=self.language_var).grid(row=1, column=3, sticky="ew", pady=(6, 0))

        ttk.Label(ext_frame, text="Description:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(ext_frame, textvariable=self.description_var).grid(row=2, column=1, columnspan=3, sticky="ew", pady=(6, 0))

        # Row 3: Scan Bar
        scan_bar = ttk.Frame(self)
        scan_bar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        scan_bar.columnconfigure(1, weight=1)
        self._scan_btn = ttk.Button(scan_bar, text="Scan & Preview", command=self._start_scan)
        self._scan_btn.grid(row=0, column=0, padx=(0, 6))
        self._cancel_scan_btn = ttk.Button(
            scan_bar,
            text="Cancel Scan",
            command=self._cancel_scan_clicked,
            state="disabled",
        )
        self._cancel_scan_btn.grid(row=0, column=1, padx=(0, 6))
        self._edit_book_btn = ttk.Button(
            scan_bar, text="Edit Book...", command=self._edit_selected_book
        )
        self._edit_book_btn.grid(row=0, column=2, padx=(0, 10))
        scan_bar.columnconfigure(1, weight=0)
        scan_bar.columnconfigure(3, weight=1)
        ttk.Label(scan_bar, textvariable=self.status_var, style="Secondary.TLabel").grid(
            row=0, column=3, sticky="w"
        )
        self._loading_bar = ttk.Progressbar(scan_bar, mode="indeterminate", length=120)
        self._loading_bar.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self._loading_bar.grid_remove()

        # Row 4: Preview Tree & Chapters Editor Notebook
        preview_nb = ttk.Notebook(self)
        preview_nb.grid(row=4, column=0, sticky="nsew")

        books_tab = ttk.Frame(preview_nb, padding=4)
        books_tab.columnconfigure(0, weight=1)
        books_tab.rowconfigure(0, weight=1)
        preview_nb.add(books_tab, text="Detected Books")

        self._scrolled_tree = ScrolledTreeview(books_tab, self._PREVIEW_COLUMNS, selectmode="browse", height=7)
        self._scrolled_tree.grid(row=0, column=0, sticky="nsew")
        self._tree = self._scrolled_tree.tree
        self._tree.bind("<<TreeviewSelect>>", self._on_book_selected)
        self._tree.bind("<Double-1>", lambda _e: self._edit_selected_book())
        self._scrolled_tree.set_empty_state(
            True,
            "Add a local folder or audio tracks above to preview.",
            action_text="Add Folder...",
            action_command=self._add_folder,
        )

        chapters_tab = ttk.Frame(preview_nb, padding=4)
        chapters_tab.columnconfigure(0, weight=1)
        chapters_tab.rowconfigure(0, weight=1)
        preview_nb.add(chapters_tab, text="Chapters")
        self._chapter_editor = ChapterEditorFrame(
            chapters_tab,
            palette=self._palette,
            on_change=self._on_chapters_edited,
        )
        self._chapter_editor.grid(row=0, column=0, sticky="nsew")

        # Row 5: Footer Actions
        btn_bar = ttk.Frame(self)
        btn_bar.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(
            btn_bar,
            textvariable=self._output_label_var,
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        self._pack_btn = ttk.Button(
            btn_bar,
            text="Pack / Queue",
            command=self._submit,
            style="Accent.TButton",
        )
        self._pack_btn.pack(side=tk.RIGHT)

    def _append_path(self, path: str) -> None:
        normalized = os.path.abspath(path)
        if any(os.path.normcase(p) == os.path.normcase(normalized) for p in self._paths):
            return
        self._paths.append(normalized)
        self._paths_listbox.insert(tk.END, normalized)

    def _add_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self.winfo_toplevel(), title="Select Audiobook Folder")
        if chosen:
            self._append_path(chosen)
            self._start_scan()

    def _add_tracks(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self.winfo_toplevel(),
            title="Select Audio Tracks",
            filetypes=[
                ("Audio files", "*.mp3 *.m4a *.m4b *.mp4 *.aac *.ogg *.opus *.flac *.wav *.wma"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            for item in chosen:
                self._append_path(item)
            self._start_scan()

    @staticmethod
    def _spec_key(spec: PackBookSpec) -> str:
        root_norm = os.path.normcase(os.path.abspath(spec.source_root))
        if os.path.isdir(spec.source_root):
            return root_norm
        tracks_norm = "|".join(sorted(os.path.normcase(os.path.abspath(p)) for p in spec.track_paths))
        return f"{root_norm}|{tracks_norm}"

    def _book_key_for_index(self, idx: int | None) -> str | None:
        if idx is None:
            return None
        if 0 <= idx < len(self._preview_specs):
            return self._spec_key(self._preview_specs[idx])
        if not self._preview_specs and idx == 0 and len(self._paths) == 1:
            return os.path.normcase(os.path.abspath(self._paths[0]))
        return None

    def _on_destroy_page(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self:
            return
        self._scan_token += 1
        self._cancel_active_scan()

    def _cancel_scan_clicked(self) -> None:
        self._scan_token += 1
        self._cancel_active_scan()
        self._is_scanning = False
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._scan_btn.configure(state="normal")
        self._cancel_scan_btn.configure(state="disabled")
        self._pack_btn.configure(state="normal")
        self.status_var.set("Scan cancelled.")

    def _cancel_active_scan(self) -> None:
        if self._scan_cancel_event is not None:
            self._scan_cancel_event.set()
            self._scan_cancel_event = None

    def _clear_all_paths(self) -> None:
        self._paths.clear()
        self._paths_listbox.delete(0, tk.END)
        self._invalidate_preview(
            "Add one or more local folders or audio tracks to preview.",
            clear_edits=True,
        )

    def _invalidate_preview(self, message: str, *, clear_edits: bool = False) -> None:
        self._scan_token += 1
        self._cancel_active_scan()
        self._is_scanning = False
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._scan_btn.configure(state="normal")
        self._cancel_scan_btn.configure(state="disabled")
        self._pack_btn.configure(state="normal")
        self._preview_specs.clear()
        if clear_edits:
            self._edited_chapters_by_key.clear()
            self._book_overrides_by_key.clear()
            self._prune_edits_on_scan = False
        self._chapter_editor.set_chapters([], audition_tracks=[])
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._scrolled_tree.set_empty_state(
            True,
            message,
            action_text="Add Folder...",
            action_command=self._add_folder,
        )
        self.status_var.set(message)

    def _selected_book_index(self) -> int | None:
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        return idx if 0 <= idx < len(self._preview_specs) else None

    def _on_book_selected(self, _event: tk.Event | None = None) -> None:
        idx = self._selected_book_index()
        if idx is None:
            return
        spec = self._preview_specs[idx]
        key = self._spec_key(spec)
        dur_ms = round(spec.total_duration_seconds * 1000) if spec.total_duration_seconds > 0 else None
        chapters = self._edited_chapters_by_key.get(key, spec.audiobook.chapters)
        audition_tracks = [
            (
                path,
                spec.track_durations_ms[i] if i < len(spec.track_durations_ms) else 0,
            )
            for i, path in enumerate(spec.track_paths)
        ]
        self._chapter_editor.set_chapters(
            chapters,
            total_duration_ms=dur_ms,
            source_label=spec.chapters_source,
            audition_tracks=audition_tracks,
        )

    def _on_chapters_edited(self, chapters: list[Chapter]) -> None:
        idx = self._selected_book_index()
        if idx is None and len(self._preview_specs) == 1:
            idx = 0
        if idx is None:
            return
        spec = self._preview_specs[idx]
        key = self._spec_key(spec)
        ch_copy = [Chapter(start=ch.start, title=ch.title) for ch in chapters]
        self._edited_chapters_by_key[key] = ch_copy
        spec.audiobook.chapters = ch_copy
        spec.chapters_source = "edited"
        iid = str(idx)
        if self._tree.exists(iid):
            vals = tree_item_values(self._tree, iid)
            if len(vals) >= 4:
                vals[3] = len(ch_copy)
                self._tree.item(iid, values=tuple(vals))

    def _edit_selected_book(self) -> None:
        idx = self._selected_book_index()
        if idx is None:
            return
        spec = self._preview_specs[idx]
        key = self._spec_key(spec)
        existing = self._book_overrides_by_key.get(key, {})
        dlg = _PackBookEditDialog(
            self,
            title=str(existing.get("title") or spec.audiobook.title),
            authors=existing.get("authors", spec.audiobook.metadata.authors),
            series_order=(
                existing["series_order"]
                if "series_order" in existing
                else spec.audiobook.metadata.series_order
            ),
            palette=self._palette,
        )
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self._apply_book_override(idx, dlg.result)

    def _apply_book_override(self, idx: int, book_ov: dict[str, Any]) -> None:
        if not (0 <= idx < len(self._preview_specs)):
            return
        spec = self._preview_specs[idx]
        key = self._spec_key(spec)
        self._book_overrides_by_key[key] = dict(book_ov)
        self._apply_saved_edits_to_spec(spec)
        if len(self._preview_specs) == 1:
            self.title_var.set(str(book_ov.get("title") or ""))
            self.author_var.set("; ".join(book_ov.get("authors") or []))
            order_val = book_ov.get("series_order")
            self.series_order_var.set("" if order_val is None else str(order_val))
        iid = str(idx)
        if self._tree.exists(iid):
            vals = tree_item_values(self._tree, iid)
            if len(vals) >= 2:
                vals[0] = spec.audiobook.title
                vals[1] = spec.audiobook.metadata.author or "(Unknown)"
                self._tree.item(iid, values=tuple(vals))

    def _apply_saved_edits_to_spec(self, spec: PackBookSpec) -> None:
        """Reapply any saved per-book overrides or chapter edits onto `spec`."""
        key = self._spec_key(spec)
        raw_chapters = self._edited_chapters_by_key.get(key)
        if raw_chapters is not None:
            spec.audiobook.chapters = [Chapter(start=ch.start, title=ch.title) for ch in raw_chapters]
            spec.chapters_source = "edited"
        book_ov = self._book_overrides_by_key.get(key)
        if book_ov:
            if book_ov.get("title"):
                spec.audiobook.metadata.title = str(book_ov["title"])
            if "authors" in book_ov:
                spec.audiobook.metadata.authors = list(book_ov["authors"])
            if "series_order" in book_ov:
                spec.audiobook.metadata.series_order = book_ov["series_order"]

    def _on_scan_option_changed(self) -> None:
        if self._paths:
            self._start_scan()
        else:
            self._invalidate_preview("Add one or more local folders or audio tracks to preview.")

    def _remove_selected_paths(self) -> None:
        selected = list(self._paths_listbox.curselection())  # type: ignore[no-untyped-call]
        if not selected:
            return
        for idx in reversed(selected):
            self._paths_listbox.delete(idx)
            del self._paths[idx]
        if not self._paths:
            self._invalidate_preview(
                "Add one or more local folders or audio tracks to preview.",
                clear_edits=True,
            )
        else:
            self._prune_edits_on_scan = True
            self._start_scan()

    def _browse_cover(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="Select Cover Image",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")],
        )
        if chosen:
            self.cover_path_var.set(chosen)

    @staticmethod
    def _split_multi_value(raw: str) -> list[str]:
        return [part.strip() for part in raw.split(";") if part.strip()]

    def _build_overrides(self, *, book_idx: int | None = None) -> PackOverrides | None:
        title = self.title_var.get().strip() or None
        authors = self._split_multi_value(self.author_var.get())
        narrators = self._split_multi_value(self.narrator_var.get())
        series = self.series_var.get().strip() or None
        series_order = _parse_series_order(self.series_order_var.get())
        asin = self.asin_var.get().strip() or None
        isbn = self.isbn_var.get().strip() or None
        cover_path = self.cover_path_var.get().strip() or None
        genres = self._split_multi_value(self.genres_var.get())
        publisher = self.publisher_var.get().strip() or None
        raw_release = self.release_date_var.get().strip()
        if raw_release and parse_release_date(raw_release) is None:
            raise ValueError(f"Invalid release date {raw_release!r} (use YYYY-MM-DD or YYYY)")
        raw_lang = self.language_var.get().strip()
        if raw_lang and parse_language(raw_lang) is None:
            raise ValueError(f"Unrecognized language {raw_lang!r}")
        description = self.description_var.get().strip() or None
        book_key = self._book_key_for_index(book_idx)
        raw_chapters = self._edited_chapters_by_key.get(book_key) if book_key is not None else None
        chapters = (
            [Chapter(start=ch.start, title=ch.title) for ch in raw_chapters]
            if raw_chapters is not None
            else None
        )
        if not any(
            (
                title,
                authors,
                narrators,
                series,
                series_order is not None,
                asin,
                isbn,
                cover_path,
                genres,
                publisher,
                raw_release,
                raw_lang,
                description,
                chapters is not None,
            )
        ):
            return None
        return PackOverrides(
            title=title,
            authors=authors,
            narrators=narrators,
            series=series,
            series_order=series_order,
            asin=asin,
            isbn=isbn,
            cover_path=cover_path,
            genres=genres,
            publisher=publisher,
            release_date=raw_release or None,
            language=raw_lang or None,
            description=description,
            chapters=chapters,
        )

    def _merge_book_overrides(
        self,
        base_overrides: PackOverrides | None,
        idx: int,
    ) -> PackOverrides | None:
        book_key = self._book_key_for_index(idx)
        raw_chapters = self._edited_chapters_by_key.get(book_key) if book_key is not None else None
        book_ov = self._book_overrides_by_key.get(book_key) if book_key is not None else None
        if raw_chapters is None and not book_ov:
            return base_overrides
        merged = (
            dataclasses.replace(base_overrides)
            if base_overrides is not None
            else PackOverrides()
        )
        if raw_chapters is not None:
            merged.chapters = [Chapter(start=ch.start, title=ch.title) for ch in raw_chapters]
        if book_ov:
            if book_ov.get("title"):
                merged.title = str(book_ov["title"])
            if "authors" in book_ov:
                merged.authors = list(book_ov["authors"])
            if "series_order" in book_ov:
                raw_so = book_ov["series_order"]
                merged.series_order = int(raw_so) if raw_so is not None else None
        return merged

    def _clone_options_for_pack(self) -> DownloadOptions | None:
        base = self._options_provider()
        if base is None:
            return None
        fmt = self.format_var.get().strip().lower() or "m4b"
        cloned = dataclasses.replace(base, urls=[], output_format=fmt)
        cloned._explicit_fields = frozenset(base._explicit_fields | {"output_format"})
        return cloned

    def _start_scan(self) -> None:
        if not self._paths:
            messagebox.showwarning(
                "No Sources",
                "Please add a folder or audio tracks first.",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            overrides = self._build_overrides(book_idx=None)
        except ValueError as exc:
            messagebox.showerror("Invalid Override", str(exc), parent=self.winfo_toplevel())
            return
        opts = self._clone_options_for_pack()
        if opts is None:
            return

        self._scan_token += 1
        token = self._scan_token
        self._cancel_active_scan()
        cancel_event = threading.Event()
        self._scan_cancel_event = cancel_event
        self._is_scanning = True
        self._scan_btn.configure(state="disabled")
        self._cancel_scan_btn.configure(state="normal")
        self._pack_btn.configure(state="disabled")
        self._scrolled_tree.set_empty_state(False)
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self.status_var.set("Scanning tracks and reading metadata...")
        paths_snapshot = list(self._paths)
        recursive = self.recursive_var.get()
        output_dir = self._output_dir_provider()
        self._output_label_var.set(f"Output folder: {output_dir}")

        def _apply(s: list[PackBookSpec] | None, e: str | None) -> None:
            if not self.winfo_exists() or token != self._scan_token:
                return
            self._is_scanning = False
            if self._scan_cancel_event is cancel_event:
                self._scan_cancel_event = None
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
            self._scan_btn.configure(state="normal")
            self._cancel_scan_btn.configure(state="disabled")
            self._pack_btn.configure(state="normal")
            for iid in self._tree.get_children():
                self._tree.delete(iid)
            if e is not None:
                self._preview_specs.clear()
                self._scrolled_tree.set_empty_state(True, f"Scan error: {e}")
                self.status_var.set(f"Scan error: {e}")
                return
            self._preview_specs = s or []
            if self._prune_edits_on_scan:
                active_keys = {self._spec_key(spec) for spec in self._preview_specs}
                self._edited_chapters_by_key = {
                    k: v for k, v in self._edited_chapters_by_key.items() if k in active_keys
                }
                self._book_overrides_by_key = {
                    k: v for k, v in self._book_overrides_by_key.items() if k in active_keys
                }
                self._prune_edits_on_scan = False
            for idx, spec in enumerate(self._preview_specs):
                self._apply_saved_edits_to_spec(spec)
                dur = format_duration(spec.total_duration_seconds) if spec.total_duration_seconds > 0 else "-"
                self._tree.insert(
                    "",
                    tk.END,
                    iid=str(idx),
                    values=(
                        spec.audiobook.title,
                        spec.audiobook.metadata.author or "(Unknown)",
                        len(spec.track_paths),
                        len(spec.audiobook.chapters),
                        dur,
                        spec.output_path,
                    ),
                )
            count = len(self._preview_specs)
            if count == 0:
                self._chapter_editor.set_chapters([], audition_tracks=[])
                self._scrolled_tree.set_empty_state(True, "No audiobooks found in selected paths.")
                self.status_var.set("No audiobooks found in selected paths.")
            else:
                self._tree.selection_set("0")
                self._on_book_selected()
                self._scrolled_tree.set_empty_state(False)
                self.status_var.set(f"Ready: {count} audiobook{'s' if count != 1 else ''} detected.")

        self._cb_queue.begin_task()

        def _worker() -> None:
            specs: list[PackBookSpec] | None = None
            err: str | None = None
            cancelled = False
            try:
                specs = self._controller.inspect_local_pack_preview(
                    paths_snapshot,
                    opts,
                    recursive=recursive,
                    output_dir=output_dir,
                    overrides=overrides,
                    cancel_event=cancel_event,
                )
            except DownloadCancelled:
                cancelled = True
            except AudiobookDLException as exc:
                err = exc.error_description
            except Exception as exc:
                err = str(exc)
            finally:
                self._cb_queue.post(
                    lambda: (
                        self._finish_cancelled_scan(token)
                        if cancelled
                        else _apply(specs, err)
                    )
                )
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_cancelled_scan(self, token: int) -> None:
        if self.winfo_exists() and token == self._scan_token:
            self._cancel_scan_clicked()

    def _submit(self) -> None:
        if not self._paths:
            messagebox.showwarning(
                "No Sources",
                "Please add at least one folder or track list.",
                parent=self.winfo_toplevel(),
            )
            return
        try:
            book_idx = 0 if len(self._preview_specs) <= 1 else None
            overrides = self._build_overrides(book_idx=book_idx)
        except ValueError as exc:
            messagebox.showerror("Invalid Override", str(exc), parent=self.winfo_toplevel())
            return
        opts = self._clone_options_for_pack()
        if opts is None or not self._ensure_ffmpeg(opts):
            return
        output_dir = self._output_dir_provider()
        fmt = self.format_var.get().strip().lower() or "m4b"
        recursive = self.recursive_var.get()
        delete_source = self.delete_source_var.get()
        if delete_source:
            confirm = messagebox.askyesno(
                "Confirm Source Deletion",
                "Original source tracks will be deleted after successful packing.\nContinue?",
                parent=self.winfo_toplevel(),
            )
            if not confirm:
                return

        if len(self._preview_specs) > 1:
            batch_overrides = scope_overrides_for_batch(overrides, len(self._preview_specs))
            requests = []
            for idx, spec in enumerate(self._preview_specs):
                book_ov = self._book_overrides_by_key.get(self._spec_key(spec))
                label = str((book_ov or {}).get("title") or spec.audiobook.title)
                requests.append(
                    PackJobRequest(
                        paths=[spec.source_root] if os.path.isdir(spec.source_root) else list(spec.track_paths),
                        recursive=False,
                        delete_source=delete_source,
                        output_dir=output_dir,
                        overrides=self._merge_book_overrides(batch_overrides, idx),
                        label=label,
                        output_format=fmt,
                    )
                )
        else:
            single_key = self._book_key_for_index(0)
            book_ov = self._book_overrides_by_key.get(single_key) if single_key is not None else None
            label = str(
                (book_ov or {}).get("title")
                or (
                    self._preview_specs[0].audiobook.title
                    if self._preview_specs
                    else (os.path.basename(self._paths[0]) or self._paths[0])
                )
            )
            requests = [
                PackJobRequest(
                    paths=list(self._paths),
                    recursive=recursive,
                    delete_source=delete_source,
                    output_dir=output_dir,
                    overrides=self._merge_book_overrides(overrides, 0),
                    label=label,
                    output_format=fmt,
                )
            ]

        self._on_submit(requests, fmt)
        self.status_var.set(f"Queued {len(requests)} pack job{'s' if len(requests) != 1 else ''}.")

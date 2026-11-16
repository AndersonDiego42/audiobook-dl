"""Modal dialog for discovering, previewing, and queuing local audiobook repackaging."""

from __future__ import annotations

import dataclasses
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl import args
from audiobookdl.exceptions import AudiobookDLException
from audiobookdl.gui.chapter_editor import ChapterEditorFrame
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.gui.formatting import format_duration
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ScrolledTreeview, TreeColumnSpec
from audiobookdl.pack import PackBookSpec, PackOverrides, scope_overrides_for_batch
from audiobookdl.utils.audiobook import Chapter, parse_language, parse_release_date


class PackDialog(tk.Toplevel):
    """Modal dialog for inspecting local audio folders and queuing `.m4b` repackaging."""

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
        options: args.DownloadOptions,
        *,
        output_dir: str,
        on_submit: Callable[[list[PackJobRequest], str], None],
        initial_paths: Sequence[str] = (),
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._base_options = options
        self._output_dir = output_dir
        self._on_submit = on_submit
        self._palette = palette or LIGHT_PALETTE
        self._paths: list[str] = []
        self._preview_specs: list[PackBookSpec] = []
        self._edited_chapters: list[Chapter] | None = None
        self._scan_token = 0
        self._is_scanning = False
        self._cb_queue = MainThreadCallbackQueue(self)

        self.title("Pack Local Audiobooks")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self.geometry(f"{scale_px(self, 860)}x{scale_px(self, 680)}")
        self.minsize(scale_px(self, 640), scale_px(self, 520))

        self.recursive_var = tk.BooleanVar(value=False)
        self.delete_source_var = tk.BooleanVar(value=False)
        self.format_var = tk.StringVar(value=options.output_format or "m4b")
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

        self._build_ui()
        apply_theme_to_window(self, self._palette)
        self.bind("<Escape>", lambda _e: self.destroy())
        if initial_paths:
            for p in initial_paths:
                if p and os.path.exists(p):
                    self._append_path(p)
            if self._paths:
                self._start_scan()

    def _build_ui(self) -> None:
        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)

        # Row 0: Source Paths
        sources_frame = ttk.LabelFrame(body, text="Local Sources", padding=scale_px(self, 8))
        sources_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
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
        ttk.Button(src_btns, text="Remove", command=self._remove_selected_paths).pack(fill=tk.X)

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

        # Row 1: Optional Metadata Overrides (Basic & Extended tabs)
        meta_nb = ttk.Notebook(body)
        meta_nb.grid(row=1, column=0, sticky="ew", pady=(0, 8))

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

        # Row 2: Scan Bar
        scan_bar = ttk.Frame(body)
        scan_bar.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        scan_bar.columnconfigure(1, weight=1)
        self._scan_btn = ttk.Button(scan_bar, text="Scan & Preview", command=self._start_scan)
        self._scan_btn.grid(row=0, column=0, padx=(0, 10))
        ttk.Label(scan_bar, textvariable=self.status_var, style="Secondary.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self._loading_bar = ttk.Progressbar(scan_bar, mode="indeterminate", length=120)
        self._loading_bar.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self._loading_bar.grid_remove()

        # Row 3: Preview Tree & Chapters Editor Notebook
        preview_nb = ttk.Notebook(body)
        preview_nb.grid(row=3, column=0, sticky="nsew")

        books_tab = ttk.Frame(preview_nb, padding=4)
        books_tab.columnconfigure(0, weight=1)
        books_tab.rowconfigure(0, weight=1)
        preview_nb.add(books_tab, text="Detected Books")

        self._scrolled_tree = ScrolledTreeview(books_tab, self._PREVIEW_COLUMNS, selectmode="browse", height=7)
        self._scrolled_tree.grid(row=0, column=0, sticky="nsew")
        self._tree = self._scrolled_tree.tree
        self._tree.bind("<<TreeviewSelect>>", self._on_book_selected)
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

        # Row 4: Footer Actions
        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(
            btn_bar,
            text=f"Output folder: {self._output_dir}",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self._pack_btn = ttk.Button(btn_bar, text="Pack / Queue", command=self._submit)
        self._pack_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _append_path(self, path: str) -> None:
        normalized = os.path.abspath(path)
        if any(os.path.normcase(p) == os.path.normcase(normalized) for p in self._paths):
            return
        self._paths.append(normalized)
        self._paths_listbox.insert(tk.END, normalized)

    def _add_folder(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Select Audiobook Folder")
        if chosen:
            self._append_path(chosen)
            self._start_scan()

    def _add_tracks(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
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

    def _invalidate_preview(self, message: str) -> None:
        self._scan_token += 1
        self._is_scanning = False
        self._loading_bar.stop()
        self._loading_bar.grid_remove()
        self._scan_btn.configure(state="normal")
        self._pack_btn.configure(state="normal")
        self._preview_specs.clear()
        self._edited_chapters = None
        self._chapter_editor.set_chapters([])
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._scrolled_tree.set_empty_state(
            True,
            message,
            action_text="Add Folder...",
            action_command=self._add_folder,
        )
        self.status_var.set(message)

    def _on_book_selected(self, _event: tk.Event | None = None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if 0 <= idx < len(self._preview_specs):
            spec = self._preview_specs[idx]
            dur_ms = round(spec.total_duration_seconds * 1000) if spec.total_duration_seconds > 0 else None
            self._chapter_editor.set_chapters(
                spec.audiobook.chapters,
                total_duration_ms=dur_ms,
                source_label=spec.chapters_source,
            )

    def _on_chapters_edited(self, chapters: list[Chapter]) -> None:
        self._edited_chapters = chapters
        sel = self._tree.selection()
        if sel:
            try:
                idx = int(sel[0])
                if 0 <= idx < len(self._preview_specs):
                    self._preview_specs[idx].audiobook.chapters = chapters
                    self._preview_specs[idx].chapters_source = "edited"
                    vals = list(self._tree.item(sel[0], "values"))
                    if len(vals) >= 4:
                        vals[3] = len(chapters)
                        self._tree.item(sel[0], values=tuple(vals))
            except ValueError:
                pass

    def _on_scan_option_changed(self) -> None:
        if self._paths:
            self._start_scan()
        else:
            self._invalidate_preview("Add one or more local folders or audio tracks to preview.")

    def _remove_selected_paths(self) -> None:
        selected = list(self._paths_listbox.curselection())
        if not selected:
            return
        for idx in reversed(selected):
            self._paths_listbox.delete(idx)
            del self._paths[idx]
        if not self._paths:
            self._invalidate_preview("Add one or more local folders or audio tracks to preview.")
        else:
            self._start_scan()

    def _browse_cover(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Select Cover Image",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")],
        )
        if chosen:
            self.cover_path_var.set(chosen)

    @staticmethod
    def _split_multi_value(raw: str) -> list[str]:
        return [part.strip() for part in raw.split(";") if part.strip()]

    def _build_overrides(self) -> PackOverrides | None:
        title = self.title_var.get().strip() or None
        authors = self._split_multi_value(self.author_var.get())
        narrators = self._split_multi_value(self.narrator_var.get())
        series = self.series_var.get().strip() or None
        raw_order = self.series_order_var.get().strip()
        series_order: int | None = None
        if raw_order:
            try:
                num = float(raw_order)
            except ValueError as exc:
                raise ValueError(f"Invalid series order {raw_order!r}") from exc
            if not num.is_integer():
                raise ValueError(f"Series order must be a whole integer, got {raw_order!r}")
            series_order = int(num)
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
        chapters = self._edited_chapters
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

    def _clone_options_for_pack(self) -> args.DownloadOptions:
        fmt = self.format_var.get().strip().lower() or "m4b"
        cloned = dataclasses.replace(self._base_options, urls=[], output_format=fmt)
        cloned._explicit_fields = frozenset(self._base_options._explicit_fields | {"output_format"})
        return cloned

    def _start_scan(self) -> None:
        if not self._paths:
            messagebox.showwarning("No Sources", "Please add a folder or audio tracks first.", parent=self)
            return
        try:
            overrides = self._build_overrides()
        except ValueError as exc:
            messagebox.showerror("Invalid Override", str(exc), parent=self)
            return

        self._scan_token += 1
        token = self._scan_token
        self._is_scanning = True
        self._scan_btn.configure(state="disabled")
        self._pack_btn.configure(state="disabled")
        self._scrolled_tree.set_empty_state(False)
        self._loading_bar.grid()
        self._loading_bar.start(12)
        self.status_var.set("Scanning tracks and reading metadata...")
        paths_snapshot = list(self._paths)
        recursive = bool(self.recursive_var.get())
        opts = self._clone_options_for_pack()
        output_dir = self._output_dir
        def _apply(s: list[PackBookSpec] | None, e: str | None) -> None:
            if not self.winfo_exists() or token != self._scan_token:
                return
            self._is_scanning = False
            self._loading_bar.stop()
            self._loading_bar.grid_remove()
            self._scan_btn.configure(state="normal")
            self._pack_btn.configure(state="normal")
            for iid in self._tree.get_children():
                self._tree.delete(iid)
            if e is not None:
                self._preview_specs.clear()
                self._scrolled_tree.set_empty_state(True, f"Scan error: {e}")
                self.status_var.set(f"Scan error: {e}")
                return
            self._preview_specs = s or []
            for idx, spec in enumerate(self._preview_specs):
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
                self._chapter_editor.set_chapters([])
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
            try:
                specs = self._controller.inspect_local_pack_preview(
                    paths_snapshot,
                    opts,
                    recursive=recursive,
                    output_dir=output_dir,
                    overrides=overrides,
                )
            except AudiobookDLException as exc:
                err = exc.error_description
            except Exception as exc:
                err = str(exc)
            finally:
                self._cb_queue.post(lambda s=specs, e=err: _apply(s, e))
                self._cb_queue.finish_worker()

        threading.Thread(target=_worker, daemon=True).start()

    def _submit(self) -> None:
        if not self._paths:
            messagebox.showwarning("No Sources", "Please add at least one folder or track list.", parent=self)
            return
        try:
            overrides = self._build_overrides()
        except ValueError as exc:
            messagebox.showerror("Invalid Override", str(exc), parent=self)
            return
        fmt = self.format_var.get().strip().lower() or "m4b"
        recursive = bool(self.recursive_var.get())
        delete_source = bool(self.delete_source_var.get())
        if delete_source:
            confirm = messagebox.askyesno(
                "Confirm Source Deletion",
                "Original source tracks will be deleted after successful packing.\nContinue?",
                parent=self,
            )
            if not confirm:
                return

        if len(self._preview_specs) > 1:
            batch_overrides = scope_overrides_for_batch(overrides, len(self._preview_specs))
            requests = [
                PackJobRequest(
                    paths=[spec.source_root] if os.path.isdir(spec.source_root) else list(spec.track_paths),
                    recursive=False,
                    delete_source=delete_source,
                    output_dir=self._output_dir,
                    overrides=batch_overrides,
                    label=spec.audiobook.title,
                    output_format=fmt,
                )
                for spec in self._preview_specs
            ]
        else:
            label = (
                self._preview_specs[0].audiobook.title
                if self._preview_specs
                else (os.path.basename(self._paths[0]) or self._paths[0])
            )
            requests = [
                PackJobRequest(
                    paths=list(self._paths),
                    recursive=recursive,
                    delete_source=delete_source,
                    output_dir=self._output_dir,
                    overrides=overrides,
                    label=label,
                    output_format=fmt,
                )
            ]

        self._on_submit(requests, fmt)
        self.destroy()

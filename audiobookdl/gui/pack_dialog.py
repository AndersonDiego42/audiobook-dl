"""Modal dialog for discovering, previewing, and queuing local audiobook repackaging."""

from __future__ import annotations

import dataclasses
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable

from audiobookdl import args
from audiobookdl.exceptions import AudiobookDLException
from audiobookdl.gui.controller import GuiController, PackJobRequest
from audiobookdl.gui.formatting import format_duration
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, apply_theme_to_window, scale_px
from audiobookdl.gui.widgets import MainThreadCallbackQueue, ScrolledTreeview, TreeColumnSpec
from audiobookdl.pack import PackBookSpec, PackOverrides, scope_overrides_for_batch


class PackDialog(tk.Toplevel):
    """Modal dialog for inspecting local audio folders and queuing `.m4b` repackaging."""

    _PREVIEW_COLUMNS = (
        TreeColumnSpec("title", "Title", width=180, stretch=True),
        TreeColumnSpec("authors", "Author", width=130, stretch=True),
        TreeColumnSpec("tracks", "Tracks", width=60, stretch=False, anchor="e"),
        TreeColumnSpec("chapters", "Chapters", width=70, stretch=False, anchor="e"),
        TreeColumnSpec("duration", "Duration", width=80, stretch=False, anchor="e"),
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
        self._scan_token = 0
        self._is_scanning = False
        self._cb_queue = MainThreadCallbackQueue(self)

        self.title("Pack Local Audiobooks")
        self.transient(parent)  # type: ignore[arg-type]
        self.grab_set()
        self.geometry(f"{scale_px(self, 820)}x{scale_px(self, 620)}")
        self.minsize(scale_px(self, 640), scale_px(self, 480))

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
        self.status_var = tk.StringVar(value="Add one or more local folders or audio tracks to preview.")

        self._build_ui()
        apply_theme_to_window(self, self._palette)
        self.bind("<Escape>", lambda _e: self.destroy())

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

        # Row 1: Optional Metadata Overrides
        meta_frame = ttk.LabelFrame(body, text="Optional Metadata Overrides", padding=scale_px(self, 8))
        meta_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        meta_frame.columnconfigure(1, weight=1)
        meta_frame.columnconfigure(3, weight=1)

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

        # Row 2: Scan Bar
        scan_bar = ttk.Frame(body)
        scan_bar.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        scan_bar.columnconfigure(1, weight=1)
        self._scan_btn = ttk.Button(scan_bar, text="Scan & Preview", command=self._start_scan)
        self._scan_btn.grid(row=0, column=0, padx=(0, 10))
        ttk.Label(scan_bar, textvariable=self.status_var, style="Secondary.TLabel").grid(
            row=0, column=1, sticky="w"
        )

        # Row 3: Preview Tree
        scrolled_tree = ScrolledTreeview(body, self._PREVIEW_COLUMNS, selectmode="browse", height=8)
        scrolled_tree.grid(row=3, column=0, sticky="nsew")
        self._tree = scrolled_tree.tree

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

    def _remove_selected_paths(self) -> None:
        selected = list(self._paths_listbox.curselection())
        for idx in reversed(selected):
            self._paths_listbox.delete(idx)
            del self._paths[idx]
        if not self._paths:
            self._preview_specs.clear()
            for iid in self._tree.get_children():
                self._tree.delete(iid)
            self.status_var.set("Add one or more local folders or audio tracks to preview.")

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
        if not any((title, authors, narrators, series, series_order is not None, asin, isbn, cover_path)):
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
        self.status_var.set("Scanning tracks and reading metadata...")
        paths_snapshot = list(self._paths)
        recursive = bool(self.recursive_var.get())
        opts = self._clone_options_for_pack()
        output_dir = self._output_dir
        def _apply(s: list[PackBookSpec] | None, e: str | None) -> None:
            if not self.winfo_exists() or token != self._scan_token:
                return
            self._is_scanning = False
            self._scan_btn.configure(state="normal")
            for iid in self._tree.get_children():
                self._tree.delete(iid)
            if e is not None:
                self._preview_specs.clear()
                self.status_var.set(f"Scan error: {e}")
                return
            self._preview_specs = s or []
            for spec in self._preview_specs:
                dur = format_duration(spec.total_duration_seconds) if spec.total_duration_seconds > 0 else "-"
                self._tree.insert(
                    "",
                    tk.END,
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
                self.status_var.set("No audiobooks found in selected paths.")
            else:
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
                )
            ]

        self._on_submit(requests, fmt)
        self.destroy()

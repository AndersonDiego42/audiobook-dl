"""Modal dialog for editing archive ledger entry metadata."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from audiobookdl.gui.theme import ThemePalette, scale_px
from audiobookdl.gui.widgets import ModalDialog
from audiobookdl.utils.archive import VALID_LIBRARY_STATUSES, ArchiveEntry, parse_tags_input


class _ArchiveMetadataDialog(ModalDialog):
    """Modal editor for updating title, author, narrator, series, IDs, status, path, and tags."""

    def __init__(
        self,
        parent: tk.Misc,
        entry: ArchiveEntry,
        *,
        palette: ThemePalette,
    ) -> None:
        super().__init__(
            parent,
            f"Edit Metadata — {entry.title}",
            palette=palette,
            default_size=(540, 440),
            min_size=(480, 380),
        )
        self.result_updates: dict[str, Any] | None = None

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._title_var = tk.StringVar(value=entry.title)
        self._author_var = tk.StringVar(value=entry.author)
        self._narrator_var = tk.StringVar(value=entry.narrator)
        self._series_var = tk.StringVar(value=entry.series or "")
        self._series_order_var = tk.StringVar(
            value="" if entry.series_order is None else str(entry.series_order)
        )
        self._asin_var = tk.StringVar(value=entry.asin or "")
        self._isbn_var = tk.StringVar(value=entry.isbn or "")
        self._status_var = tk.StringVar(value=entry.status or "downloaded")
        self._output_path_var = tk.StringVar(value=entry.output_path or "")
        self._tags_var = tk.StringVar(value=" ".join(entry.tags))

        fields: list[tuple[str, tk.StringVar]] = [
            ("Title:", self._title_var),
            ("Author:", self._author_var),
            ("Narrator:", self._narrator_var),
            ("Series:", self._series_var),
            ("Series Order:", self._series_order_var),
            ("ASIN:", self._asin_var),
            ("ISBN:", self._isbn_var),
            ("Tags:", self._tags_var),
        ]
        first_entry: ttk.Entry | None = None
        row_idx = 0
        for label_text, var in fields:
            ttk.Label(body, text=label_text).grid(
                row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
            )
            widget = ttk.Entry(body, textvariable=var)
            widget.grid(row=row_idx, column=1, columnspan=2, sticky="ew", pady=3)
            if first_entry is None:
                first_entry = widget
            row_idx += 1

        ttk.Label(body, text="Status:").grid(
            row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
        )
        status_combo = ttk.Combobox(
            body,
            textvariable=self._status_var,
            values=list(VALID_LIBRARY_STATUSES),
            state="readonly",
            width=18,
        )
        status_combo.grid(row=row_idx, column=1, sticky="w", pady=3)
        row_idx += 1

        ttk.Label(body, text="Output Path:").grid(
            row=row_idx, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(body, textvariable=self._output_path_var).grid(
            row=row_idx, column=1, sticky="ew", pady=3
        )
        ttk.Button(body, text="Browse...", command=self._browse_output_path).grid(
            row=row_idx, column=2, padx=(6, 0), pady=3
        )
        row_idx += 1

        btns = ttk.Frame(body)
        btns.grid(row=row_idx, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._save())
        self.finalize_modal(initial_focus=first_entry)

    def _browse_output_path(self) -> None:
        current = self._output_path_var.get().strip()
        initial_dir = os.path.dirname(current) if current and os.path.dirname(current) else os.getcwd()
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Relink Audiobook File",
            initialdir=initial_dir,
        )
        if chosen:
            self._output_path_var.set(chosen)

    def _save(self) -> None:
        title = self._title_var.get().strip()
        if not title:
            messagebox.showwarning("Invalid Metadata", "Title cannot be empty.", parent=self)
            return
        raw_order = self._series_order_var.get().strip()
        series_order: int | None = None
        if raw_order:
            try:
                series_order = int(raw_order)
            except ValueError:
                messagebox.showwarning(
                    "Invalid Metadata",
                    "Series Order must be a whole number (or empty).",
                    parent=self,
                )
                return
        status = self._status_var.get().strip().lower()
        if status not in VALID_LIBRARY_STATUSES:
            messagebox.showwarning(
                "Invalid Metadata",
                f"Status must be one of: {', '.join(VALID_LIBRARY_STATUSES)}.",
                parent=self,
            )
            return
        self.result_updates = {
            "title": title,
            "author": self._author_var.get().strip(),
            "narrator": self._narrator_var.get().strip(),
            "series": self._series_var.get().strip() or None,
            "series_order": series_order,
            "asin": self._asin_var.get().strip() or None,
            "isbn": self._isbn_var.get().strip() or None,
            "output_path": self._output_path_var.get().strip() or None,
            "status": status,
            "tags": parse_tags_input(self._tags_var.get()),
        }
        self.destroy()

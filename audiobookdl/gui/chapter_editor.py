"""Reusable interactive chapter inspector and editor widget for Preview and Pack dialogs."""

from __future__ import annotations

import json
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl.gui.formatting import format_duration, format_timestamp_ms, parse_timestamp_ms
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, scale_px
from audiobookdl.gui.widgets import ModalDialog, ScrolledTreeview, TreeColumnSpec
from audiobookdl.utils.audiobook import Chapter

_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+\s+", re.IGNORECASE)
_CUE_TITLE_RE = re.compile(r'^\s*TITLE\s+"?(.*?)"?\s*$', re.IGNORECASE)
_CUE_INDEX01_RE = re.compile(r"^\s*INDEX\s+0?1\s+(\d+):(\d{2}):(\d{2})\s*$", re.IGNORECASE)


def parse_chapters_from_file(path: str) -> list[Chapter]:
    """Parse chapters from a `.json` or `.cue` sidecar file."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig") as fh:
        content = fh.read()
    if ext == ".json":
        raw = json.loads(content)
        items = raw.get("chapters") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("JSON file must contain a list of chapters or {'chapters': [...]}.")
        out: list[Chapter] = []
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"Chapter {idx}").strip()
            if "start_ms" in item:
                start_ms = int(item["start_ms"])
            elif "start" in item:
                start_ms = int(item["start"])
            elif "start_seconds" in item:
                start_ms = round(float(item["start_seconds"]) * 1000)
            else:
                continue
            out.append(Chapter(start=max(0, start_ms), title=title))
        out.sort(key=lambda ch: ch.start)
        return out

    if ext == ".cue":
        out = []
        current_title: str | None = None
        in_track = False
        for line in content.splitlines():
            if _CUE_TRACK_RE.match(line):
                in_track = True
                current_title = None
                continue
            if not in_track:
                continue
            m_title = _CUE_TITLE_RE.match(line)
            if m_title:
                current_title = m_title.group(1).strip()
                continue
            m_idx = _CUE_INDEX01_RE.match(line)
            if m_idx:
                mins = int(m_idx.group(1))
                secs = int(m_idx.group(2))
                frames = int(m_idx.group(3))
                ms = round((mins * 60 + secs + frames / 75.0) * 1000)
                out.append(Chapter(start=ms, title=current_title or f"Chapter {len(out) + 1}"))
        out.sort(key=lambda ch: ch.start)
        return out

    raise ValueError(f"Unsupported chapter file extension '{ext}' (use .cue or .json).")


class _ChapterEditDialog(ModalDialog):
    """Small themed modal for editing a single chapter's title and start timestamp."""

    def __init__(
        self,
        parent: tk.Misc,
        chapter: Chapter,
        *,
        palette: ThemePalette,
    ) -> None:
        super().__init__(
            parent,
            "Edit Chapter",
            palette=palette,
            resizable=(False, False),
        )
        self.result_chapter: Chapter | None = None

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._title_var = tk.StringVar(value=chapter.title)
        self._start_var = tk.StringVar(value=format_timestamp_ms(chapter.start))

        ttk.Label(body, text="Title:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        title_entry = ttk.Entry(body, textvariable=self._title_var, width=36)
        title_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Start (MM:SS.mmm):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        start_entry = ttk.Entry(body, textvariable=self._start_var, width=18)
        start_entry.grid(row=1, column=1, sticky="w", pady=4)

        btns = ttk.Frame(body)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._save())
        self.finalize_modal(initial_focus=title_entry)

    def _save(self) -> None:
        title = self._title_var.get().strip()
        if not title:
            messagebox.showwarning("Invalid Chapter", "Chapter title cannot be empty.", parent=self)
            return
        try:
            start_ms = parse_timestamp_ms(self._start_var.get())
        except ValueError as exc:
            messagebox.showwarning("Invalid Timestamp", str(exc), parent=self)
            return
        self.result_chapter = Chapter(start=start_ms, title=title)
        self.destroy()


class ChapterEditorFrame(ttk.Frame):
    """Interactive chapter table allowing rename, timestamp edit, delete, merge, and import."""

    _COLUMNS = (
        TreeColumnSpec("num", "#", width=42, stretch=False, anchor="e", sort_kind="number"),
        TreeColumnSpec("start", "Start", width=95, stretch=False, anchor="e"),
        TreeColumnSpec("duration", "Duration", width=80, stretch=False, anchor="e", sort_kind="duration"),
        TreeColumnSpec("title", "Title", width=220, stretch=True, anchor="w"),
        TreeColumnSpec("source", "Source", width=80, stretch=False, anchor="w"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        *,
        palette: ThemePalette | None = None,
        on_change: Callable[[list[Chapter]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or LIGHT_PALETTE
        self._on_change = on_change
        self._chapters: list[Chapter] = []
        self._chapter_sources: list[str] = []
        self._total_duration_ms: int | None = None
        self._source_label = ""
        self._info_var = tk.StringVar(value="No chapters")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Edit...", command=self.edit_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Delete", command=self.delete_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Merge Next", command=self.merge_with_next).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Import .cue/.json...", command=self.import_from_file).pack(
            side=tk.LEFT
        )
        ttk.Label(toolbar, textvariable=self._info_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        self._scrolled = ScrolledTreeview(
            self,
            self._COLUMNS,
            selectmode="extended",
            height=9,
            enable_sorting=False,
        )
        self._scrolled.grid(row=1, column=0, sticky="nsew")
        self.tree = self._scrolled.tree
        self.tree.bind("<Double-1>", lambda _e: self.edit_selected())

    def set_chapters(
        self,
        chapters: Sequence[Chapter],
        *,
        total_duration_ms: int | None = None,
        source_label: str = "",
    ) -> None:
        src = source_label or "Embedded"
        paired = sorted(
            [(Chapter(start=int(c.start), title=str(c.title)), src) for c in chapters],
            key=lambda item: item[0].start,
        )
        self._chapters = [item[0] for item in paired]
        self._chapter_sources = [item[1] for item in paired]
        self._total_duration_ms = total_duration_ms
        self._source_label = source_label
        self._render()

    def get_chapters(self) -> list[Chapter]:
        return [Chapter(start=c.start, title=c.title) for c in self._chapters]

    def _sort_paired(self) -> None:
        paired = sorted(
            zip(self._chapters, self._chapter_sources, strict=False),
            key=lambda item: item[0].start,
        )
        self._chapters = [item[0] for item in paired]
        self._chapter_sources = [item[1] for item in paired]

    def _notify_changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self.get_chapters())

    def _render(self) -> None:
        existing = self.tree.get_children()
        if existing:
            self.tree.delete(*existing)
        total = len(self._chapters)
        for idx, ch in enumerate(self._chapters):
            end_ms: int | None = None
            if idx + 1 < total:
                end_ms = self._chapters[idx + 1].start
            elif self._total_duration_ms is not None and self._total_duration_ms >= ch.start:
                end_ms = self._total_duration_ms
            dur_str = format_duration((end_ms - ch.start) / 1000.0) if end_ms is not None else "--"
            row_src = (
                self._chapter_sources[idx]
                if idx < len(self._chapter_sources)
                else (self._source_label or "Embedded")
            )
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(idx + 1, format_timestamp_ms(ch.start), dur_str, ch.title, row_src),
            )
        src_suffix = f" ({self._source_label})" if self._source_label else ""
        self._info_var.set(f"{total} chapter{'s' if total != 1 else ''}{src_suffix}")
        self._scrolled.set_empty_state(
            total == 0,
            "No chapters detected. Import a .cue or .json file to add chapters.",
        )

    def edit_selected(self) -> None:
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._chapters)):
            return
        dlg = _ChapterEditDialog(self, self._chapters[idx], palette=self._palette)
        self.wait_window(dlg)
        if dlg.result_chapter is None:
            return
        self._chapters[idx] = dlg.result_chapter
        if idx < len(self._chapter_sources):
            self._chapter_sources[idx] = "Edited"
        self._sort_paired()
        self._render()
        self._notify_changed()

    def delete_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        indices = sorted({int(iid) for iid in sel}, reverse=True)
        for idx in indices:
            if 0 <= idx < len(self._chapters):
                del self._chapters[idx]
            if 0 <= idx < len(self._chapter_sources):
                del self._chapter_sources[idx]
        self._render()
        self._notify_changed()

    def merge_with_next(self) -> None:
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._chapters) - 1):
            return
        # Keep current chapter's start & title, drop the following boundary.
        del self._chapters[idx + 1]
        if idx + 1 < len(self._chapter_sources):
            del self._chapter_sources[idx + 1]
        self._render()
        if self.tree.exists(str(idx)):
            self.tree.selection_set(str(idx))
        self._notify_changed()

    def import_from_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Import Chapters (.cue or .json)",
            filetypes=[("Chapter files", "*.cue *.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            loaded = parse_chapters_from_file(path)
        except Exception as exc:
            messagebox.showerror("Import Failed", str(exc), parent=self)
            return
        if not loaded:
            messagebox.showinfo("No Chapters", "No chapters were found in the selected file.", parent=self)
            return
        self._chapters = loaded
        ext_src = os.path.splitext(path)[1].lower() or "File"
        self._chapter_sources = [ext_src] * len(loaded)
        self._source_label = os.path.basename(path)
        self._render()
        self._notify_changed()

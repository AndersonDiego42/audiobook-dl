"""Reusable interactive chapter inspector and editor widget for Preview and Pack dialogs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable, Sequence

from audiobookdl.gui.formatting import format_duration, format_timestamp_ms, parse_timestamp_ms
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, scale_px
from audiobookdl.gui.widgets import ModalDialog, ScrolledTreeview, TreeColumnSpec
from audiobookdl.utils import find_executable, subprocess_kwargs
from audiobookdl.utils.audiobook import Chapter

_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+\s+", re.IGNORECASE)
_CUE_TITLE_RE = re.compile(r'^\s*TITLE\s+"?(.*?)"?\s*$', re.IGNORECASE)
_CUE_INDEX01_RE = re.compile(r"^\s*INDEX\s+0?1\s+(\d+):(\d{2}):(\d{2})\s*$", re.IGNORECASE)


def parse_chapters_from_file(path: str) -> list[Chapter]:
    """Parse chapters from a `.json` or `.cue` sidecar file.

    JSON items with `start_ms` are treated as milliseconds; Audiobookshelf
    `metadata.json` payloads (float `start`, `end`, or top-level ABS keys) and
    `start_seconds` are converted from seconds to milliseconds.
    """
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig") as fh:
        content = fh.read()
    if ext == ".json":
        raw = json.loads(content)
        items = raw.get("chapters") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("JSON file must contain a list of chapters or {'chapters': [...]}.")
        is_abs_seconds = (
            isinstance(raw, dict)
            and any(k in raw for k in ("mediaMetadata", "duration", "tags", "authors"))
        ) or any(
            isinstance(entry, dict)
            and ("end" in entry or isinstance(entry.get("start"), float))
            for entry in items
        )
        out: list[Chapter] = []
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"Chapter {idx}").strip()
            if "start_ms" in item:
                start_ms = int(item["start_ms"])
            elif "start_seconds" in item:
                start_ms = round(float(item["start_seconds"]) * 1000)
            elif "start" in item:
                start_val = item["start"]
                if is_abs_seconds or isinstance(start_val, float):
                    start_ms = round(float(start_val) * 1000)
                else:
                    start_ms = int(start_val)
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


def export_chapters_to_file(path: str, chapters: Sequence[Chapter]) -> None:
    """Serialize `chapters` to a `.json` or `.cue` sidecar file compatible with `parse_chapters_from_file`."""
    ext = os.path.splitext(path)[1].lower()
    ordered = sorted((Chapter(start=max(0, int(c.start)), title=str(c.title)) for c in chapters), key=lambda c: c.start)
    if ext == ".json":
        payload = {
            "chapters": [
                {"title": ch.title, "start_ms": ch.start}
                for ch in ordered
            ]
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return

    if ext == ".cue":
        lines = ['TITLE "Chapters"', 'FILE "audiobook.m4b" WAVE']
        for idx, ch in enumerate(ordered, start=1):
            total_frames = max(0, round(ch.start * 75.0 / 1000.0))
            mins, rem_frames = divmod(total_frames, 60 * 75)
            secs, frames = divmod(rem_frames, 75)
            safe_title = ch.title.replace('"', "'")
            lines.append(f"  TRACK {idx:02d} AUDIO")
            lines.append(f'    TITLE "{safe_title}"')
            lines.append(f"    INDEX 01 {mins:02d}:{secs:02d}:{frames:02d}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return

    raise ValueError(f"Unsupported chapter file extension '{ext}' (use .cue or .json).")


class _ChapterEditDialog(ModalDialog):
    """Small themed modal for creating or editing a single chapter's title and start timestamp."""

    def __init__(
        self,
        parent: tk.Misc,
        chapter: Chapter,
        *,
        palette: ThemePalette,
        dialog_title: str = "Edit Chapter",
    ) -> None:
        super().__init__(
            parent,
            dialog_title,
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


class _ChapterShiftDialog(ModalDialog):
    """Modal dialog for offsetting or scaling chapter start timestamps."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        has_selection: bool,
        palette: ThemePalette,
    ) -> None:
        super().__init__(
            parent,
            "Shift Chapter Timestamps",
            palette=palette,
            resizable=(False, False),
        )
        self.result_mode: str | None = None
        self.result_value: float = 0.0
        self.result_selected_only: bool = False

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._mode_var = tk.StringVar(value="offset")
        self._value_var = tk.StringVar(value="0.0")
        self._scope_var = tk.StringVar(value="selected" if has_selection else "all")

        ttk.Label(body, text="Operation:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        mode_row = ttk.Frame(body)
        mode_row.grid(row=0, column=1, sticky="w", pady=4)
        ttk.Radiobutton(
            mode_row,
            text="Offset (+/- sec)",
            variable=self._mode_var,
            value="offset",
            command=self._on_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            mode_row,
            text="Scale factor",
            variable=self._mode_var,
            value="scale",
            command=self._on_mode_changed,
        ).pack(side=tk.LEFT)

        self._value_label = ttk.Label(body, text="Seconds (+/-):")
        self._value_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        value_entry = ttk.Entry(body, textvariable=self._value_var, width=16)
        value_entry.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(body, text="Apply to:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        scope_row = ttk.Frame(body)
        scope_row.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Radiobutton(
            scope_row,
            text="Selected",
            variable=self._scope_var,
            value="selected",
            state="normal" if has_selection else "disabled",
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            scope_row,
            text="All chapters",
            variable=self._scope_var,
            value="all",
        ).pack(side=tk.LEFT)

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Apply", command=self._apply).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._apply())
        self.finalize_modal(initial_focus=value_entry)

    def _on_mode_changed(self) -> None:
        if self._mode_var.get() == "scale":
            self._value_label.configure(text="Scale factor:")
            if self._value_var.get().strip() in ("", "0", "0.0"):
                self._value_var.set("1.0")
        else:
            self._value_label.configure(text="Seconds (+/-):")
            if self._value_var.get().strip() == "1.0":
                self._value_var.set("0.0")

    def _apply(self) -> None:
        raw = self._value_var.get().strip()
        try:
            val = float(raw)
        except ValueError:
            messagebox.showwarning("Invalid Value", "Enter a valid number.", parent=self)
            return
        mode = self._mode_var.get()
        if mode == "scale" and val <= 0:
            messagebox.showwarning("Invalid Scale", "Scale factor must be greater than 0.", parent=self)
            return
        self.result_mode = mode
        self.result_value = val
        self.result_selected_only = self._scope_var.get() == "selected"
        self.destroy()


def resolve_audition_target(
    chapter_start_ms: int,
    tracks: Sequence[tuple[str, int]],
) -> tuple[str, float] | None:
    """Resolve `(audio_file_path, seek_seconds)` for `chapter_start_ms` within local `tracks`."""
    cleaned = [(str(path), max(0, int(dur_ms))) for path, dur_ms in tracks if str(path).strip()]
    if not cleaned:
        return None
    target_ms = max(0, int(chapter_start_ms))
    if len(cleaned) == 1:
        return cleaned[0][0], target_ms / 1000.0
    cursor_ms = 0
    for path, dur_ms in cleaned:
        if dur_ms > 0 and target_ms < cursor_ms + dur_ms:
            return path, max(0, target_ms - cursor_ms) / 1000.0
        cursor_ms += dur_ms
    last_path, last_dur = cleaned[-1]
    last_start_ms = max(0, cursor_ms - last_dur)
    return last_path, max(0, target_ms - last_start_ms) / 1000.0


class ChapterEditorFrame(ttk.Frame):
    """Interactive chapter table allowing add, rename, timestamp edit/shift, delete, merge, and import/export."""

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
        self._audition_tracks: list[tuple[str, int]] = []
        self._audition_proc: subprocess.Popen[bytes] | None = None
        self._audition_poll_id: str | None = None
        self._total_duration_ms: int | None = None
        self._source_label = ""
        self._info_var = tk.StringVar(value="No chapters")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Add...", command=self.add_chapter).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Edit...", command=self.edit_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Delete", command=self.delete_selected).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Merge Next", command=self.merge_with_next).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Shift...", command=self.shift_chapters).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Import .cue/.json...", command=self.import_from_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Export...", command=self.export_to_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        self._audition_btn = ttk.Button(
            toolbar,
            text="Audition 5s",
            command=self.toggle_audition,
            state="disabled",
        )
        self._audition_btn.pack(side=tk.LEFT)
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
        self.bind("<Destroy>", self._on_destroy, add="+")

    def apply_theme(self, palette: ThemePalette) -> None:
        """Update the stored palette used when opening chapter edit dialogs."""
        self._palette = palette

    def set_chapters(
        self,
        chapters: Sequence[Chapter],
        *,
        total_duration_ms: int | None = None,
        source_label: str = "",
        audition_tracks: Sequence[tuple[str, int]] | None = None,
    ) -> None:
        self.stop_audition()
        src = source_label or "Embedded"
        paired = sorted(
            [(Chapter(start=int(c.start), title=str(c.title)), src) for c in chapters],
            key=lambda item: item[0].start,
        )
        self._chapters = [item[0] for item in paired]
        self._chapter_sources = [item[1] for item in paired]
        if audition_tracks is not None:
            self._audition_tracks = [
                (str(path), max(0, int(dur_ms)))
                for path, dur_ms in audition_tracks
                if str(path).strip()
            ]
        self._total_duration_ms = total_duration_ms
        self._source_label = source_label
        self._render()

    def _update_audition_button_state(self) -> None:
        if self._audition_proc is not None and self._audition_proc.poll() is None:
            self._audition_btn.configure(text="Stop Audition", state="normal")
            return
        can_play = bool(
            self._chapters
            and self._audition_tracks
            and find_executable("ffplay") is not None
        )
        self._audition_btn.configure(
            text="Audition 5s",
            state="normal" if can_play else "disabled",
        )

    def stop_audition(self) -> None:
        """Terminate any active `ffplay` audition subprocess."""
        if self._audition_poll_id is not None:
            try:
                self.after_cancel(self._audition_poll_id)
            except tk.TclError:
                pass
            self._audition_poll_id = None
        proc = self._audition_proc
        self._audition_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            if self.winfo_exists():
                self._update_audition_button_state()
        except tk.TclError:
            pass

    def toggle_audition(self) -> bool:
        """Play a 5-second preview around the selected chapter boundary via `ffplay`, or stop if active."""
        if self._audition_proc is not None and self._audition_proc.poll() is None:
            self.stop_audition()
            return False
        if not self._chapters or not self._audition_tracks:
            return False
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else 0
        if not (0 <= idx < len(self._chapters)):
            return False
        target = resolve_audition_target(self._chapters[idx].start, self._audition_tracks)
        if target is None or not os.path.isfile(target[0]):
            messagebox.showwarning(
                "Audition Unavailable",
                "Local audio file for the selected chapter could not be found.",
                parent=self,
            )
            return False
        ffplay_bin = find_executable("ffplay")
        if ffplay_bin is None:
            messagebox.showwarning(
                "ffplay Not Found",
                "Audio auditioning requires 'ffplay' (included with full FFmpeg builds).",
                parent=self,
            )
            self._update_audition_button_state()
            return False
        audio_path, offset_sec = target
        cmd = [
            ffplay_bin,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "quiet",
            "-ss",
            f"{offset_sec:.3f}",
            "-t",
            "5",
            audio_path,
        ]
        try:
            self._audition_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_kwargs(),
            )
        except OSError as exc:
            messagebox.showerror("Audition Failed", f"Could not launch ffplay: {exc}", parent=self)
            self._audition_proc = None
            self._update_audition_button_state()
            return False
        self._update_audition_button_state()
        self._audition_poll_id = self.after(200, self._poll_audition)
        return True

    def _poll_audition(self) -> None:
        self._audition_poll_id = None
        proc = self._audition_proc
        if proc is None:
            self._update_audition_button_state()
            return
        if proc.poll() is not None:
            self._audition_proc = None
            self._update_audition_button_state()
            return
        self._audition_poll_id = self.after(200, self._poll_audition)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self.stop_audition()

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
        self._update_audition_button_state()

    def add_chapter(self) -> None:
        default_start = self._chapters[-1].start if self._chapters else 0
        default_title = f"Chapter {len(self._chapters) + 1}"
        dlg = _ChapterEditDialog(
            self,
            Chapter(start=default_start, title=default_title),
            palette=self._palette,
            dialog_title="Add Chapter",
        )
        self.wait_window(dlg)
        if dlg.result_chapter is None:
            return
        self._chapters.append(dlg.result_chapter)
        self._chapter_sources.append("Added")
        self._sort_paired()
        self._render()
        self._notify_changed()

    def apply_chapter_shift(
        self,
        *,
        mode: str,
        value: float,
        indices: Sequence[int] | None = None,
    ) -> None:
        """Apply a timestamp `offset` (seconds) or `scale` factor to selected or all chapters."""
        if not self._chapters:
            return
        target_indices = (
            set(indices) if indices is not None else set(range(len(self._chapters)))
        )
        for idx in sorted(target_indices):
            if not (0 <= idx < len(self._chapters)):
                continue
            ch = self._chapters[idx]
            if mode == "scale":
                new_start = max(0, round(ch.start * value))
            else:
                new_start = max(0, round(ch.start + value * 1000.0))
            self._chapters[idx] = Chapter(start=new_start, title=ch.title)
            if idx < len(self._chapter_sources):
                self._chapter_sources[idx] = "Edited"
        self._sort_paired()
        self._render()
        self._notify_changed()

    def shift_chapters(self) -> None:
        if not self._chapters:
            return
        selected = [int(iid) for iid in self.tree.selection()]
        dlg = _ChapterShiftDialog(
            self,
            has_selection=bool(selected),
            palette=self._palette,
        )
        self.wait_window(dlg)
        if dlg.result_mode is None:
            return
        indices = selected if (dlg.result_selected_only and selected) else None
        self.apply_chapter_shift(mode=dlg.result_mode, value=dlg.result_value, indices=indices)

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

    def export_to_file(self) -> None:
        if not self._chapters:
            messagebox.showinfo("No Chapters", "There are no chapters to export.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export Chapters (.cue or .json)",
            defaultextension=".json",
            filetypes=[("JSON chapter file", "*.json"), ("CUE sheet", "*.cue")],
        )
        if not path:
            return
        try:
            export_chapters_to_file(path, self._chapters)
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self)

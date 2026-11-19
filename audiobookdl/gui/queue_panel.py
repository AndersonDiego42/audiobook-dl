"""Action bar, progress metrics, scrollable activity log, and job queue panel."""

from collections import deque
from dataclasses import dataclass
import json
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from audiobookdl.config import DEFAULT_PROFILE, list_source_profiles
from audiobookdl.gui.controller import GuiController, QueuedJob
from audiobookdl.gui.events import (
    BatchItemEvent,
    ControllerEvent,
    DownloadEstimateEvent,
    ItemProgressEvent,
    JobMetaEvent,
    JobStatusEvent,
    LogBatchEvent,
    LogEvent,
    ProgressAdvanceEvent,
    ProgressFinishEvent,
    ProgressMetricsEvent,
    ProgressStartEvent,
)
from audiobookdl.gui.formatting import (
    format_duration,
    format_size_estimate,
    format_speed,
    parse_url_text,
)
from audiobookdl.utils.audiobook import format_bytes
from audiobookdl.gui.notifications import format_completion_notification, send_os_notification_async
from audiobookdl.gui.theme import LIGHT_PALETTE, ThemePalette, make_bold_font, scale_px
from audiobookdl.gui.widgets import (
    ModalDialog,
    ResponsiveMode,
    ScrolledTreeview,
    Tooltip,
    TreeColumnSpec,
    resolve_responsive_mode,
)
from audiobookdl.options_schema import (
    GUI_INTRO_CHAPTER_MODES,
    GUI_MEDIA_TYPE_OPTIONS,
    GUI_OUTPUT_FORMATS,
    OPTIONS_BY_NAME,
)
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources import detect_service_key
from audiobookdl.utils import (
    AUDIO_BITRATE_PRESETS,
    AUDIO_SAMPLE_RATE_PRESETS,
    normalize_audio_bitrate,
    normalize_sample_rate,
)


@dataclass
class _JobProgressState:
    """Per-job live download stage, percentage, throughput, and ETA."""

    stage: str = "Stage: Downloading"
    pct: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    bytes_downloaded: int = 0
    estimated_bytes: int | None = None
    estimated_exact: bool = False
    last_row_render_at: float = 0.0


def export_queue_to_path(records: Sequence[Mapping[str, Any]], path: str) -> int:
    """Export `records` as `.json` (full specs) or `.txt` (URLs); return exported count."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        urls = [
            str(r.get("url") or "").strip()
            for r in records
            if r.get("job_kind", "download") == "download" and str(r.get("url") or "").strip()
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(urls) + ("\n" if urls else ""))
        return len(urls)

    exported: list[dict[str, Any]] = []
    for r in records:
        item: dict[str, Any] = {
            "url": str(r.get("url") or ""),
            "job_kind": str(r.get("job_kind") or "download"),
            "service": str(r.get("service") or ""),
        }
        if r.get("profile"):
            item["profile"] = r["profile"]
        if r.get("selected_indices") is not None:
            item["selected_indices"] = list(r["selected_indices"])
        if isinstance(r.get("job_overrides"), Mapping):
            item["job_overrides"] = dict(r["job_overrides"])
        if isinstance(r.get("pack_request"), Mapping):
            item["pack_request"] = dict(r["pack_request"])
        exported.append(item)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "jobs": exported}, fh, indent=2, ensure_ascii=False)
    return len(exported)


def load_queue_import_file(path: str) -> list[dict[str, Any]]:
    """Load queued job descriptors from a `.json` or `.txt` file."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig") as fh:
        raw_text = fh.read()
    if ext == ".json":
        parsed = json.loads(raw_text)
        items = parsed.get("jobs") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            raise ValueError("JSON queue file must contain a list or {'jobs': [...]}.")
        out: list[dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, str) and entry.strip():
                out.append({"job_kind": "download", "url": entry.strip()})
            elif isinstance(entry, Mapping):
                out.append(dict(entry))
        return out

    return [{"job_kind": "download", "url": u} for u in parse_url_text(raw_text)]


class _JobOptionsDialog(ModalDialog):
    """Modal dialog for editing per-job profile and format overrides on queued/retryable jobs."""

    _TRISTATE_CHOICES = ("Default", "On", "Off")
    _OVERWRITE_CHOICES = ("Default (inherit)", "Ask", "Overwrite", "Keep existing")

    @staticmethod
    def _init_tristate(ov_map: Mapping[str, Any], key: str) -> str:
        val = ov_map.get(key)
        if val is True:
            return "On"
        if val is False:
            return "Off"
        return "Default"

    @staticmethod
    def _read_tristate(raw: str) -> bool | None:
        return {"On": True, "Off": False}.get(raw)

    def __init__(
        self,
        parent: tk.Misc,
        record: Mapping[str, Any],
        *,
        profiles: Sequence[str] = (),
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "Edit Job Options",
            palette=palette,
            default_size=(460, 520),
            min_size=(400, 420),
            resizable=(True, True),
        )
        self.result: tuple[str, dict[str, Any]] | None = None

        ov = record.get("job_overrides")
        ov_map: Mapping[str, Any] = ov if isinstance(ov, Mapping) else {}
        pack_req = record.get("pack_request")
        pack_map: Mapping[str, Any] = pack_req if isinstance(pack_req, Mapping) else {}
        is_pack = record.get("job_kind", "download") == "pack"
        self._is_pack = is_pack

        profile_values = [DEFAULT_PROFILE] + [p for p in profiles if p and p != DEFAULT_PROFILE]
        cur_prof = str(record.get("profile") or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
        if cur_prof not in profile_values:
            profile_values.append(cur_prof)

        raw_fmt = str(ov_map.get("output_format") or pack_map.get("output_format") or "Original").strip()
        cur_fmt = raw_fmt if raw_fmt in GUI_OUTPUT_FORMATS else "Original"
        raw_media = str(ov_map.get("media_type") or "auto").strip().lower()
        cur_media = raw_media.capitalize() if raw_media in ("auto", "audiobook", "ebook", "both") else "Auto"
        if cur_media not in GUI_MEDIA_TYPE_OPTIONS:
            cur_media = "Auto"
        cur_bitrate = str(ov_map.get("audio_bitrate") or "").strip()
        raw_sr = ov_map.get("sample_rate")
        cur_sample_rate = "" if raw_sr is None else str(raw_sr).strip()

        if "overwrite" not in ov_map:
            cur_overwrite = "Default (inherit)"
        elif ov_map.get("overwrite") is True:
            cur_overwrite = "Overwrite"
        elif ov_map.get("overwrite") is False:
            cur_overwrite = "Keep existing"
        else:
            cur_overwrite = "Ask"

        intro_binding = OPTIONS_BY_NAME["intro_chapter_mode"].gui
        assert intro_binding is not None
        cur_intro_mode = intro_binding.init_value(ov_map.get("intro_chapter_mode"))

        self._profile_var = tk.StringVar(value=cur_prof)
        self._format_var = tk.StringVar(value=cur_fmt)
        self._media_var = tk.StringVar(value=cur_media)
        self._bitrate_var = tk.StringVar(value=cur_bitrate)
        self._sample_rate_var = tk.StringVar(value=cur_sample_rate)
        self._skip_dl_var = tk.StringVar(value=self._init_tristate(ov_map, "skip_downloaded"))
        self._intro_mode_var = tk.StringVar(value=cur_intro_mode)
        self._combine_var = tk.StringVar(value=self._init_tristate(ov_map, "combine"))
        self._split_var = tk.StringVar(value=self._init_tristate(ov_map, "split_chapters"))
        self._overwrite_var = tk.StringVar(value=cur_overwrite)
        self._write_abs_var = tk.StringVar(
            value=self._init_tristate(ov_map, "write_abs_metadata")
        )
        self._upload_abs_var = tk.StringVar(value=self._init_tristate(ov_map, "upload_to_abs"))

        body = ttk.Frame(self, padding=scale_px(self, 14))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Profile:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        prof_combo = ttk.Combobox(
            body,
            textvariable=self._profile_var,
            values=profile_values,
            state="disabled" if is_pack else "normal",
        )
        prof_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Output Format:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        fmt_combo = ttk.Combobox(
            body,
            textvariable=self._format_var,
            values=GUI_OUTPUT_FORMATS,
            state="readonly",
        )
        fmt_combo.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Media Type:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._media_var,
            values=GUI_MEDIA_TYPE_OPTIONS,
            state="disabled" if is_pack else "readonly",
        ).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Audio Bitrate:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._bitrate_var,
            values=("", *(p for p in AUDIO_BITRATE_PRESETS if p != "Auto")),
            state="normal",
        ).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Sample Rate (Hz):").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._sample_rate_var,
            values=("", *(p for p in AUDIO_SAMPLE_RATE_PRESETS if p != "Auto")),
            state="normal",
        ).grid(row=4, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Skip Downloaded:").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._skip_dl_var,
            values=self._TRISTATE_CHOICES,
            state="disabled" if is_pack else "readonly",
        ).grid(row=5, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Intro Numbering:").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._intro_mode_var,
            values=GUI_INTRO_CHAPTER_MODES,
            state="readonly",
        ).grid(row=6, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Combine Parts:").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._combine_var,
            values=self._TRISTATE_CHOICES,
            state="disabled" if is_pack else "readonly",
        ).grid(row=7, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Split Chapters:").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._split_var,
            values=self._TRISTATE_CHOICES,
            state="disabled" if is_pack else "readonly",
        ).grid(row=8, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Overwrite Existing:").grid(row=9, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._overwrite_var,
            values=self._OVERWRITE_CHOICES,
            state="readonly",
        ).grid(row=9, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Write ABS Metadata:").grid(row=10, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._write_abs_var,
            values=self._TRISTATE_CHOICES,
            state="readonly",
        ).grid(row=10, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="Upload to ABS:").grid(row=11, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(
            body,
            textvariable=self._upload_abs_var,
            values=self._TRISTATE_CHOICES,
            state="readonly",
        ).grid(row=11, column=1, sticky="ew", pady=4)

        btns = ttk.Frame(body)
        btns.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Save", command=self._save, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.finalize_modal(initial_focus=fmt_combo if is_pack else prof_combo)

    def _save(self) -> None:
        prof = (self._profile_var.get() or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
        fmt_raw = self._format_var.get().strip()
        fmt = None if fmt_raw in ("", "Original") else fmt_raw
        media = (self._media_var.get() or "Auto").strip().lower()
        try:
            bitrate = normalize_audio_bitrate(self._bitrate_var.get())
            sample_rate = normalize_sample_rate(self._sample_rate_var.get())
        except ValueError as exc:
            messagebox.showwarning("Invalid Option", str(exc), parent=self)
            return
        intro_binding = OPTIONS_BY_NAME["intro_chapter_mode"].gui
        assert intro_binding is not None
        intro_mode = intro_binding.read_option(self._intro_mode_var.get())
        overwrite_val = {"Overwrite": True, "Keep existing": False}.get(self._overwrite_var.get())
        overrides: dict[str, Any] = {
            "output_format": fmt,
            "intro_chapter_mode": intro_mode,
            "audio_bitrate": bitrate,
            "sample_rate": sample_rate,
            "overwrite": overwrite_val,
            "write_abs_metadata": self._read_tristate(self._write_abs_var.get()),
            "upload_to_abs": self._read_tristate(self._upload_abs_var.get()),
        }
        if not self._is_pack:
            overrides["media_type"] = media
            overrides["skip_downloaded"] = self._read_tristate(self._skip_dl_var.get())
            overrides["combine"] = self._read_tristate(self._combine_var.get())
            overrides["split_chapters"] = self._read_tristate(self._split_var.get())
        self.result = (prof, overrides)
        self.destroy()


class JobInspectorDialog(ModalDialog):
    """Themed modal dialog inspecting full job metadata, overrides, and error text."""

    _EDITABLE_STATUSES = frozenset({"Queued", "Paused", "Failed", "Cancelled", "Interrupted"})

    def __init__(
        self,
        parent: tk.Misc,
        record: Mapping[str, Any],
        *,
        profiles: Sequence[str] = (),
        on_save_options: Callable[[str, str, dict[str, Any]], Mapping[str, Any] | None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "Job Inspector",
            palette=palette,
            default_size=(620, 420),
            min_size=(560, 360),
        )
        self._record = dict(record)
        self._profiles = list(profiles)
        self._on_save_options = on_save_options

        body = ttk.Frame(self, padding=scale_px(self, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(7, weight=1)

        self._overrides_var = tk.StringVar(value=self._format_overrides(record))
        fields = [
            ("Service:", str(record.get("service") or "")),
            ("Kind:", str(record.get("job_kind") or "download")),
            ("Status:", str(record.get("status") or "")),
            ("Title:", str(record.get("title") or "--")),
            ("Source / URL:", str(record.get("url") or "")),
            ("Output Path:", str(record.get("output_path") or "--")),
        ]
        for row_idx, (label, val) in enumerate(fields):
            ttk.Label(body, text=label, font=make_bold_font(self, 0)).grid(
                row=row_idx, column=0, sticky="nw", padx=(0, scale_px(self, 8)), pady=scale_px(self, 2)
            )
            ttk.Label(body, text=val, wraplength=scale_px(self, 420), justify="left").grid(
                row=row_idx, column=1, sticky="w", pady=scale_px(self, 2)
            )
        ttk.Label(body, text="Profile / Options:", font=make_bold_font(self, 0)).grid(
            row=6, column=0, sticky="nw", padx=(0, scale_px(self, 8)), pady=scale_px(self, 2)
        )
        ttk.Label(
            body,
            textvariable=self._overrides_var,
            wraplength=scale_px(self, 420),
            justify="left",
        ).grid(row=6, column=1, sticky="w", pady=scale_px(self, 2))

        ttk.Label(body, text="Detail / Error:", font=make_bold_font(self, 0)).grid(
            row=7, column=0, sticky="nw", padx=(0, scale_px(self, 8)), pady=(scale_px(self, 6), scale_px(self, 2))
        )
        detail_box = ScrolledText(body, height=6, width=52, wrap="word")
        detail_box.grid(row=7, column=1, sticky="nsew", pady=(6, 2))
        detail_box.insert("1.0", str(record.get("detail") or "(none)"))
        detail_box.configure(state="disabled")

        btns = ttk.Frame(body)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="Copy Detail / Error", command=self._copy_detail).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        if record.get("output_path"):
            ttk.Button(btns, text="Copy Path", command=self._copy_path).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        ttk.Button(btns, text="Copy All Info", command=self._copy_all_info).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        can_edit = (
            on_save_options is not None
            and record.get("job_kind", "download") in ("download", "pack")
            and str(record.get("status") or "") in self._EDITABLE_STATUSES
        )
        self._edit_btn = ttk.Button(
            btns,
            text="Edit Job Options...",
            command=self._edit_options,
            state="normal" if can_edit else "disabled",
        )
        self._edit_btn.pack(side=tk.LEFT)
        close_btn = ttk.Button(btns, text="Close", command=self.destroy)
        close_btn.pack(side=tk.RIGHT)

        self.finalize_modal(initial_focus=close_btn)

    def _edit_options(self) -> None:
        if self._on_save_options is None:
            return
        job_id = str(self._record.get("job_id") or "")
        if not job_id:
            return
        dlg = _JobOptionsDialog(
            self,
            self._record,
            profiles=self._profiles,
            palette=self._palette,
        )
        self.wait_window(dlg)
        if dlg.result is None:
            return
        prof, overrides = dlg.result
        updated = self._on_save_options(job_id, prof, overrides)
        if updated is not None:
            self._record = dict(updated)
            self._overrides_var.set(self._format_overrides(self._record))

    @staticmethod
    def _format_overrides(record: Mapping[str, Any]) -> str:
        parts: list[str] = []
        if record.get("profile"):
            parts.append(f"profile={record['profile']}")
        if record.get("selected_indices") is not None:
            parts.append(f"books={len(record['selected_indices'])} selected")
        ov = record.get("job_overrides")
        if isinstance(ov, Mapping):
            for k, v in ov.items():
                if k != "output_template":
                    parts.append(f"{k}={v}")
        return ", ".join(parts) if parts else "Default"

    def _copy_detail(self) -> None:
        detail = str(self._record.get("detail") or "").strip()
        if detail:
            self.clipboard_clear()
            self.clipboard_append(detail)

    def _copy_path(self) -> None:
        path = str(self._record.get("output_path") or "").strip()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def _copy_all_info(self) -> None:
        lines = [
            f"Job ID: {self._record.get('job_id') or '-'}",
            f"Service: {self._record.get('service') or '-'}",
            f"Status: {self._record.get('status') or '-'}",
            f"URL / Source: {self._record.get('url') or '-'}",
            f"Title / Name: {self._record.get('title') or '-'}",
            f"Output Path: {self._record.get('output_path') or '-'}",
            f"Profile / Options: {self._overrides_var.get()}",
            f"Detail / Error: {self._record.get('detail') or '(none)'}",
        ]
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))


class ActivityQueuePanel(ttk.LabelFrame):
    """LabelFrame managing action buttons, progress/metrics display, activity log, and queue table."""

    MAX_LOG_LINES = 5000
    _ACTION_BAR_WIDE_ENTER_DP = 680
    _ACTION_BAR_WIDE_EXIT_DP = 640
    _LOG_TOOLBAR_WIDE_ENTER_DP = 480
    _LOG_TOOLBAR_WIDE_EXIT_DP = 450
    _QUEUE_TOOLBAR_WIDE_ENTER_DP = 650
    _QUEUE_TOOLBAR_WIDE_EXIT_DP = 610

    def __init__(
        self,
        master: tk.Misc,
        root: tk.Tk,
        controller: GuiController,
        *,
        status_var: tk.StringVar,
        batch_var: tk.StringVar,
        metrics_var: tk.StringVar,
        on_start_download: Callable[[], None],
        on_cancel_download: Callable[[], None],
        on_preview_metadata: Callable[[], None],
        on_pack_local: Callable[[], Any],
        on_open_folder: Callable[[], None],
        on_retry_failed: Callable[[], None],
        service_label_for_url: Callable[[str], str],
        on_retry_selected: Callable[[Sequence[str]], None] | None = None,
        on_preview_url: Callable[[str], None] | None = None,
        on_send_to_urls: Callable[[Sequence[str]], None] | None = None,
        on_open_path: Callable[[str, bool], None] | None = None,
        on_import_queue: Callable[[list[dict[str, Any]]], None] | None = None,
        on_queue_summary_changed: Callable[[int, int], None] | None = None,
        on_progress_mode_changed: Callable[[str], None] | None = None,
        progress_var: tk.DoubleVar | None = None,
        active_job_var: tk.StringVar | None = None,
        concurrency_var: tk.IntVar | None = None,
        on_concurrency_changed: Callable[[int], None] | None = None,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(master, text="Activity", padding=(scale_px(master, 10), scale_px(master, 6)))
        self._window = root
        self._controller = controller
        self._palette = palette or LIGHT_PALETTE
        self.status_var = status_var
        self.batch_var = batch_var
        self.stage_var = tk.StringVar(value="")
        self.metrics_var = metrics_var
        self.progress_var = progress_var or tk.DoubleVar(value=0.0)
        self.active_job_var = active_job_var or tk.StringVar(value="Idle")
        self.concurrency_var = concurrency_var or tk.IntVar(value=controller.queue_concurrency)
        self._on_concurrency_changed = on_concurrency_changed
        self._on_start_download = on_start_download
        self._on_cancel_download = on_cancel_download
        self._on_preview_metadata = on_preview_metadata
        self._on_pack_local = on_pack_local
        self._on_open_folder = on_open_folder
        self._on_retry_failed = on_retry_failed
        self._service_label_for_url = service_label_for_url
        self._on_retry_selected = on_retry_selected
        self._on_preview_url = on_preview_url
        self._on_send_to_urls = on_send_to_urls
        self._on_open_path = on_open_path
        self._on_import_queue = on_import_queue
        self._on_queue_summary_changed = on_queue_summary_changed
        self._on_progress_mode_changed = on_progress_mode_changed

        self.current_ui_run_id = 0
        self.ui_item_index = 0
        self.ui_item_total = 0
        self.ui_book_index = 0
        self.ui_book_total = 0
        self._run_start_time: float | None = None
        self._running_job_ids: list[str] = []
        self._job_progress: dict[str, _JobProgressState] = {}
        self._default_progress_key = "__default__"
        self._last_metrics_render_at = 0.0
        self._log_line_count = 0
        self._log_entries: deque[tuple[str, str]] = deque(maxlen=self.MAX_LOG_LINES)
        self.log_level_var = tk.StringVar(value="All")
        self.log_search_var = tk.StringVar(value="")
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.queue_filter_var = tk.StringVar(value="All")
        self.queue_search_var = tk.StringVar(value="")
        self._queue_order: list[str] = []
        self._detached_iids: set[str] = set()
        self._drag_job_id: str | None = None
        self._drag_start_y: int | None = None
        self._drag_hover_iid: str | None = None
        self._log_search_after_id: str | None = None
        self._queue_filter_after_id: str | None = None
        self._action_bar_mode: ResponsiveMode = "wide"
        self._log_toolbar_mode: ResponsiveMode = "wide"
        self._queue_toolbar_mode: ResponsiveMode = "wide"
        self._last_is_previewing = False
        self._last_is_syncing = False

        self._build_widgets()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.apply_theme_palette(self._palette)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self._action_bar = ttk.Frame(self)
        self._action_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self.download_btn = ttk.Button(
            self._action_bar, text="Download", command=self._on_start_download
        )
        self.pause_btn = ttk.Button(self._action_bar, text="Pause", command=self.toggle_pause)
        self.cancel_btn = ttk.Button(
            self._action_bar, text="Cancel", command=self._on_cancel_download
        )
        self.preview_btn = ttk.Button(
            self._action_bar, text="Preview", command=self._on_preview_metadata
        )
        self.pack_btn = ttk.Button(self._action_bar, text="Pack Local...", command=self._on_pack_local)
        self.open_folder_btn = ttk.Button(
            self._action_bar, text="Open Folder", command=self._on_open_folder
        )
        self.clear_log_btn = ttk.Button(self._action_bar, text="Clear Log", command=self.clear_log)
        self.status_label = ttk.Label(self._action_bar, textvariable=self.status_var, anchor="e")
        self._apply_action_bar_layout("wide")
        self._action_bar.bind("<Configure>", self._on_action_bar_configure, add="+")

        meta_bar = ttk.Frame(self)
        meta_bar.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        meta_bar.columnconfigure(1, weight=1)

        self.batch_label = ttk.Label(meta_bar, textvariable=self.batch_var, style="Secondary.TLabel")
        self.batch_label.grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.stage_label = ttk.Label(meta_bar, textvariable=self.stage_var, style="Muted.TLabel")
        self.stage_label.grid(row=0, column=1, sticky="w")

        self.metrics_label = ttk.Label(
            meta_bar, textvariable=self.metrics_var, style="Muted.TLabel", anchor="e"
        )
        self.metrics_label.grid(row=0, column=2, sticky="e")

        self.progressbar = ttk.Progressbar(
            self,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        )
        self.progressbar.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        self.activity_notebook = ttk.Notebook(self)
        self.activity_notebook.grid(row=3, column=0, sticky="nsew")
        self.activity_notebook.enable_traversal()

        log_tab = ttk.Frame(self.activity_notebook)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(1, weight=1)

        self._log_toolbar = ttk.Frame(log_tab, padding=(4, 4))
        self._log_toolbar.grid(row=0, column=0, sticky="ew")
        self._log_level_label = ttk.Label(self._log_toolbar, text="Level:")
        self._log_level_combo = ttk.Combobox(
            self._log_toolbar,
            textvariable=self.log_level_var,
            values=("All", "Info+", "Errors Only"),
            state="readonly",
            width=11,
        )
        self._log_level_combo.bind("<<ComboboxSelected>>", lambda _e: self._rerender_logs())
        self._log_search_label = ttk.Label(self._log_toolbar, text="Search:")
        self._log_search_entry = ttk.Entry(
            self._log_toolbar, textvariable=self.log_search_var, width=20
        )
        self.log_search_var.trace_add("write", lambda *_args: self._schedule_rerender_logs())
        self._log_autoscroll_cb = ttk.Checkbutton(
            self._log_toolbar, text="Auto-scroll", variable=self.autoscroll_var
        )
        self._log_save_btn = ttk.Button(
            self._log_toolbar, text="Save Log...", command=self._save_log_to_file
        )
        self._apply_log_toolbar_layout("wide")
        self._log_toolbar.bind("<Configure>", self._on_log_toolbar_configure, add="+")

        self.log_text = ScrolledText(log_tab, height=4, width=40, state="disabled", wrap="word")
        try:
            fixed_font = tkfont.nametofont("TkFixedFont", root=self._window)
            self.log_text.configure(font=fixed_font)
        except tk.TclError:
            pass
        self.log_text.grid(row=1, column=0, sticky="nsew")

        def _focus_log_search(_e: tk.Event) -> str:
            self._log_search_entry.focus_set()
            self._log_search_entry.selection_range(0, tk.END)
            return "break"

        def _clear_log_search(_e: tk.Event) -> str | None:
            if self.log_search_var.get():
                self.log_search_var.set("")
                return "break"
            return None

        self.log_text.bind("<Control-f>", _focus_log_search, add="+")
        self.log_text.bind("<Control-F>", _focus_log_search, add="+")
        self._log_search_entry.bind("<Escape>", _clear_log_search, add="+")
        self.activity_notebook.add(log_tab, text="Activity Log")

        queue_tab = ttk.Frame(self.activity_notebook, padding=4)
        queue_tab.columnconfigure(0, weight=1)
        queue_tab.rowconfigure(2, weight=1)
        self._queue_tab = queue_tab

        self._queue_toolbar = ttk.Frame(queue_tab)
        self._queue_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        self.queue_remove_btn = ttk.Button(
            self._queue_toolbar, text="Remove", command=self.remove_selected_item
        )
        self.queue_up_btn = ttk.Button(
            self._queue_toolbar, text="Move Up", command=lambda: self.move_selected_item(-1)
        )
        self.queue_down_btn = ttk.Button(
            self._queue_toolbar, text="Move Down", command=lambda: self.move_selected_item(1)
        )
        self.queue_retry_btn = ttk.Button(
            self._queue_toolbar, text="Retry Failed", command=self._on_retry_failed
        )
        self.queue_open_btn = ttk.Button(
            self._queue_toolbar,
            text="Open Output",
            command=lambda: self._open_selected_output(reveal_in_folder=False),
        )
        self.queue_clear_btn = ttk.Button(
            self._queue_toolbar, text="Clear Completed & Failed", command=self.clear_completed_history
        )
        self._conc_frame = ttk.Frame(self._queue_toolbar)
        ttk.Label(self._conc_frame, text="Parallel jobs:").pack(side=tk.LEFT, padx=(0, 4))
        self.queue_concurrency_spin = ttk.Spinbox(
            self._conc_frame,
            from_=1,
            to=4,
            width=3,
            textvariable=self.concurrency_var,
            state="readonly",
            command=self._handle_concurrency_change,
        )
        self.queue_concurrency_spin.pack(side=tk.LEFT)
        self.queue_concurrency_spin.bind(
            "<FocusOut>", lambda _e: self._handle_concurrency_change(), add="+"
        )
        self._apply_queue_toolbar_layout("wide")
        self._queue_toolbar.bind("<Configure>", self._on_queue_toolbar_configure, add="+")

        queue_filter_bar = ttk.Frame(queue_tab)
        queue_filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(queue_filter_bar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        q_filter_combo = ttk.Combobox(
            queue_filter_bar,
            textvariable=self.queue_filter_var,
            values=("All", "Queued", "Running", "Completed", "Failed"),
            state="readonly",
            width=11,
        )
        q_filter_combo.pack(side=tk.LEFT, padx=(0, 8))
        q_filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_queue_filter())
        ttk.Label(queue_filter_bar, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        q_search_entry = ttk.Entry(queue_filter_bar, textvariable=self.queue_search_var, width=24)
        q_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._queue_search_entry = q_search_entry
        self.queue_search_var.trace_add("write", lambda *_args: self._schedule_queue_filter())

        queue_columns = (
            TreeColumnSpec("service", "Service", width=76, stretch=False),
            TreeColumnSpec("profile", "Profile", width=64, stretch=False),
            TreeColumnSpec("url", "URL / Title", width=150, stretch=True),
            TreeColumnSpec("status", "Status", width=74, stretch=False),
            TreeColumnSpec("detail", "Detail", width=120, stretch=True),
        )
        self._scrolled_queue = ScrolledTreeview(
            queue_tab, queue_columns, selectmode="extended", height=3, enable_sorting=False
        )
        self._scrolled_queue.grid(row=2, column=0, sticky="nsew")
        self.queue_tree = self._scrolled_queue.tree
        self._build_queue_context_menu()
        self.queue_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_queue_toolbar_buttons(), add="+")
        self.queue_tree.bind("<Double-1>", self._on_queue_double_click)
        self.queue_tree.bind("<Delete>", lambda _e: self.remove_selected_item())
        self.queue_tree.bind("<ButtonPress-1>", self._on_queue_drag_start, add="+")
        self.queue_tree.bind("<B1-Motion>", self._on_queue_drag_motion, add="+")
        self.queue_tree.bind("<ButtonRelease-1>", self._on_queue_drag_drop, add="+")
        self.queue_tree.bind("<Button-3>", self._show_queue_context_menu)
        if sys.platform == "darwin":
            self.queue_tree.bind("<Button-2>", self._show_queue_context_menu)
        self._bind_queue_shortcuts()
        self._tooltips = [
            Tooltip(
                self.queue_concurrency_spin,
                "Maximum number of downloads running concurrently (1-4)",
                palette_provider=lambda: self._palette,
            ),
            Tooltip(
                self._queue_search_entry,
                "Filter queued and historical jobs by URL, title, service, or profile (Ctrl+F)",
                palette_provider=lambda: self._palette,
            ),
        ]
        self.activity_notebook.add(queue_tab, text="Queue & History")
        self._refresh_queue_empty_and_badge()

    def sync_responsive_toolbars(self, width: int | None = None) -> None:
        """Reflow action, log, and queue toolbars for the available panel width."""
        w = int(width if width is not None else self.winfo_width())
        if w <= 1:
            return
        action_mode = resolve_responsive_mode(
            self._action_bar_mode,
            w,
            scale_px(self, self._ACTION_BAR_WIDE_ENTER_DP),
            scale_px(self, self._ACTION_BAR_WIDE_EXIT_DP),
        )
        if action_mode != self._action_bar_mode:
            self._apply_action_bar_layout(action_mode)
        log_mode = resolve_responsive_mode(
            self._log_toolbar_mode,
            w,
            scale_px(self, self._LOG_TOOLBAR_WIDE_ENTER_DP),
            scale_px(self, self._LOG_TOOLBAR_WIDE_EXIT_DP),
        )
        if log_mode != self._log_toolbar_mode:
            self._apply_log_toolbar_layout(log_mode)
        queue_mode = resolve_responsive_mode(
            self._queue_toolbar_mode,
            w,
            scale_px(self, self._QUEUE_TOOLBAR_WIDE_ENTER_DP),
            scale_px(self, self._QUEUE_TOOLBAR_WIDE_EXIT_DP),
        )
        if queue_mode != self._queue_toolbar_mode:
            self._apply_queue_toolbar_layout(queue_mode)

    def _on_action_bar_configure(self, event: tk.Event) -> None:
        self.sync_responsive_toolbars(int(getattr(event, "width", 0) or self._action_bar.winfo_width()))

    def _on_log_toolbar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._log_toolbar.winfo_width())
        mode = resolve_responsive_mode(
            self._log_toolbar_mode,
            w,
            scale_px(self, self._LOG_TOOLBAR_WIDE_ENTER_DP),
            scale_px(self, self._LOG_TOOLBAR_WIDE_EXIT_DP),
        )
        if mode != self._log_toolbar_mode:
            self._apply_log_toolbar_layout(mode)

    def _on_queue_toolbar_configure(self, event: tk.Event) -> None:
        w = int(getattr(event, "width", 0) or self._queue_toolbar.winfo_width())
        mode = resolve_responsive_mode(
            self._queue_toolbar_mode,
            w,
            scale_px(self, self._QUEUE_TOOLBAR_WIDE_ENTER_DP),
            scale_px(self, self._QUEUE_TOOLBAR_WIDE_EXIT_DP),
        )
        if mode != self._queue_toolbar_mode:
            self._apply_queue_toolbar_layout(mode)

    def _apply_action_bar_layout(self, mode: Literal["wide", "compact"]) -> None:
        self._action_bar_mode = mode
        for col in range(8):
            self._action_bar.columnconfigure(col, weight=0)
        if mode == "wide":
            self._action_bar.columnconfigure(7, weight=1)
            buttons = (
                self.download_btn,
                self.pause_btn,
                self.cancel_btn,
                self.preview_btn,
                self.pack_btn,
                self.open_folder_btn,
                self.clear_log_btn,
            )
            for idx, btn in enumerate(buttons):
                btn.grid(row=0, column=idx, padx=(0, 6) if idx < 6 else 0, pady=0, sticky="w")
            self.status_label.grid(row=0, column=7, columnspan=1, padx=(8, 0), pady=0, sticky="e")
            return

        self._action_bar.columnconfigure(4, weight=1)
        for idx, btn in enumerate((self.download_btn, self.pause_btn, self.cancel_btn, self.preview_btn)):
            btn.grid(row=0, column=idx, padx=(0, 6), pady=0, sticky="w")
        self.status_label.grid(row=0, column=4, columnspan=4, padx=(8, 0), pady=0, sticky="e")
        for idx, btn in enumerate((self.pack_btn, self.open_folder_btn, self.clear_log_btn)):
            btn.grid(row=1, column=idx, padx=(0, 6) if idx < 2 else 0, pady=(4, 0), sticky="w")

    def _apply_log_toolbar_layout(self, mode: Literal["wide", "compact"]) -> None:
        self._log_toolbar_mode = mode
        for col in range(6):
            self._log_toolbar.columnconfigure(col, weight=0)
        self._log_level_label.grid(row=0, column=0, padx=(0, 4), pady=0, sticky="w")
        self._log_level_combo.grid(row=0, column=1, padx=(0, 8), pady=0, sticky="w")
        self._log_search_label.grid(row=0, column=2, padx=(0, 4), pady=0, sticky="w")
        if mode == "wide":
            self._log_toolbar.columnconfigure(4, weight=1)
            self._log_search_entry.grid(row=0, column=3, padx=(0, 8), pady=0, sticky="w")
            self._log_autoscroll_cb.grid(row=0, column=4, columnspan=1, padx=(0, 8), pady=0, sticky="w")
            self._log_save_btn.grid(row=0, column=5, columnspan=1, padx=0, pady=0, sticky="e")
            return

        self._log_toolbar.columnconfigure(3, weight=1)
        self._log_search_entry.grid(row=0, column=3, padx=0, pady=0, sticky="ew")
        self._log_autoscroll_cb.grid(row=1, column=0, columnspan=2, padx=0, pady=(4, 0), sticky="w")
        self._log_save_btn.grid(row=1, column=2, columnspan=2, padx=0, pady=(4, 0), sticky="e")

    def _apply_queue_toolbar_layout(self, mode: Literal["wide", "compact"]) -> None:
        self._queue_toolbar_mode = mode
        for col in range(8):
            self._queue_toolbar.columnconfigure(col, weight=0)
        if mode == "wide":
            self._queue_toolbar.columnconfigure(5, weight=1)
            row_btns = (
                (self.queue_remove_btn, 4),
                (self.queue_up_btn, 4),
                (self.queue_down_btn, 6),
                (self.queue_retry_btn, 4),
                (self.queue_open_btn, 4),
            )
            for idx, (btn, pad_r) in enumerate(row_btns):
                btn.grid(row=0, column=idx, columnspan=1, padx=(0, pad_r), pady=0, sticky="w")
            self._conc_frame.grid(row=0, column=6, columnspan=1, padx=(0, 8), pady=0, sticky="e")
            self.queue_clear_btn.grid(row=0, column=7, padx=0, pady=0, sticky="e")
            return

        self._queue_toolbar.columnconfigure(4, weight=1)
        row0_btns = (
            (self.queue_remove_btn, 4),
            (self.queue_up_btn, 4),
            (self.queue_down_btn, 6),
            (self.queue_retry_btn, 0),
        )
        for idx, (btn, pad_r) in enumerate(row0_btns):
            btn.grid(row=0, column=idx, columnspan=1, padx=(0, pad_r), pady=0, sticky="w")
        self.queue_open_btn.grid(row=1, column=0, columnspan=2, padx=(0, 6), pady=(4, 0), sticky="w")
        self._conc_frame.grid(row=1, column=2, columnspan=2, padx=(0, 8), pady=(4, 0), sticky="e")
        self.queue_clear_btn.grid(row=1, column=4, padx=0, pady=(4, 0), sticky="e")

    def _bind_queue_shortcuts(self) -> None:
        def _select_all(_e: tk.Event) -> str:
            visible = self.queue_tree.get_children()
            if visible:
                self.queue_tree.selection_set(visible)
                self.queue_tree.focus(visible[0])
            return "break"

        def _focus_q_search(_e: tk.Event) -> str:
            self._queue_search_entry.focus_set()
            self._queue_search_entry.selection_range(0, tk.END)
            return "break"

        def _clear_q_search(_e: tk.Event) -> str | None:
            if self.queue_search_var.get():
                self.queue_search_var.set("")
                return "break"
            return None

        def _on_return(_e: tk.Event) -> str:
            self._show_selected_details()
            return "break"

        def _on_f2(_e: tk.Event) -> str:
            self._edit_selected_job_options()
            return "break"

        def _on_alt_up(_e: tk.Event) -> str:
            self.move_selected_item(-1)
            return "break"

        def _on_alt_down(_e: tk.Event) -> str:
            self.move_selected_item(1)
            return "break"

        def _on_space(_e: tk.Event) -> str:
            self.toggle_pause()
            return "break"

        self.queue_tree.bind("<Control-a>", _select_all, add="+")
        self.queue_tree.bind("<Control-A>", _select_all, add="+")
        self.queue_tree.bind("<Control-f>", _focus_q_search, add="+")
        self.queue_tree.bind("<Control-F>", _focus_q_search, add="+")
        self.queue_tree.bind("<Return>", _on_return, add="+")
        self.queue_tree.bind("<F2>", _on_f2, add="+")
        self.queue_tree.bind("<Alt-Up>", _on_alt_up, add="+")
        self.queue_tree.bind("<Alt-Down>", _on_alt_down, add="+")
        self.queue_tree.bind("<space>", _on_space, add="+")
        self._queue_search_entry.bind("<Escape>", _clear_q_search, add="+")
        for seq in ("<Shift-F10>", "<Menu>"):
            try:
                self.queue_tree.bind(seq, self._show_queue_context_menu_from_key, add="+")
            except tk.TclError:
                pass

    def _sync_queue_toolbar_buttons(self) -> None:
        records = self._selected_records()
        single = records[0] if len(records) == 1 else None
        can_remove = any(rec.get("status") != "Running" for rec in records)
        can_move = bool(single is not None and single.get("status") == "Queued")
        can_open = bool(
            single is not None
            and self._on_open_path is not None
            and self._resolve_record_path(single, reveal_in_folder=False)
        )
        has_failed = False
        has_clearable = False
        for iid in self._queue_order:
            rec = self._controller.get_queue_record(iid)
            st = str((rec or {}).get("status") or "")
            if st in ("Failed", "Cancelled", "Interrupted"):
                has_failed = True
                has_clearable = True
            elif st == "Completed":
                has_clearable = True
            if has_failed and has_clearable:
                break
        self.queue_remove_btn.configure(state="normal" if can_remove else "disabled")
        self.queue_up_btn.configure(state="normal" if can_move else "disabled")
        self.queue_down_btn.configure(state="normal" if can_move else "disabled")
        self.queue_open_btn.configure(state="normal" if can_open else "disabled")
        self.queue_retry_btn.configure(state="normal" if has_failed else "disabled")
        self.queue_clear_btn.configure(state="normal" if has_clearable else "disabled")

    def _build_queue_context_menu(self) -> None:
        menu = tk.Menu(self.queue_tree, tearoff=0)
        menu.add_command(
            label="Open Output",
            command=lambda: self._open_selected_output(reveal_in_folder=False),
        )
        menu.add_command(
            label="Open Containing Folder",
            command=lambda: self._open_selected_output(reveal_in_folder=True),
        )
        menu.add_command(label="View Job Details...", command=self._show_selected_details)
        menu.add_command(label="Edit Job Options...", command=self._edit_selected_job_options)
        menu.add_command(label="Change Profile...", command=self._change_selected_profile)
        menu.add_separator()
        menu.add_command(label="Retry Selected", command=self._retry_selected_items)
        menu.add_command(label="Preview Metadata...", command=self._preview_selected_item)
        menu.add_command(label="Send URL to Input", command=self._send_selected_to_urls)
        menu.add_separator()
        menu.add_command(label="Copy URL / Source", command=self._copy_selected_urls)
        menu.add_command(label="Copy Detail / Error", command=self._copy_selected_detail)
        menu.add_separator()
        menu.add_command(label="Move to Top", command=lambda: self.move_selected_to_edge(to_top=True))
        menu.add_command(label="Move Up", command=lambda: self.move_selected_item(-1))
        menu.add_command(label="Move Down", command=lambda: self.move_selected_item(1))
        menu.add_command(label="Move to Bottom", command=lambda: self.move_selected_to_edge(to_top=False))
        menu.add_command(label="Remove Selected", command=self.remove_selected_item)
        menu.add_command(
            label="Clear Completed Only",
            command=lambda: self.clear_completed_history(statuses=("Completed",)),
        )
        menu.add_command(
            label="Clear Failed & Cancelled",
            command=lambda: self.clear_completed_history(
                statuses=("Failed", "Cancelled", "Interrupted")
            ),
        )
        menu.add_command(label="Clear Completed & Failed", command=self.clear_completed_history)
        menu.add_separator()
        menu.add_command(label="Export Queue...", command=self.export_queue_dialog)
        if self._on_import_queue is not None:
            menu.add_command(label="Import Queue...", command=self.import_queue_dialog)
        self._queue_menu = menu

    def export_queue_dialog(self) -> None:
        """Prompt for a file path and export current queue records as JSON or text URLs."""
        with self._controller.queue_lock:
            records = [
                dict(self._controller.queue_records[jid])
                for jid in self._queue_order
                if jid in self._controller.queue_records
            ]
        if not records:
            messagebox.showinfo("Queue Empty", "There are no queue items to export.", parent=self._window)
            return
        path = filedialog.asksaveasfilename(
            parent=self._window,
            title="Export Queue",
            defaultextension=".json",
            filetypes=[("JSON Queue File", "*.json"), ("Text URL List", "*.txt")],
        )
        if not path:
            return
        try:
            count = export_queue_to_path(records, path)
            self.status_var.set(f"Exported {count} queue item(s)")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self._window)

    def import_queue_dialog(self) -> None:
        """Prompt for a `.json` or `.txt` queue file and enqueue imported jobs."""
        if self._on_import_queue is None:
            return
        path = filedialog.askopenfilename(
            parent=self._window,
            title="Import Queue",
            filetypes=[("Queue files", "*.json *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            items = load_queue_import_file(path)
        except Exception as exc:
            messagebox.showerror("Import Failed", str(exc), parent=self._window)
            return
        if not items:
            messagebox.showinfo("Empty File", "No queue items found in the selected file.", parent=self._window)
            return
        self._on_import_queue(items)

    def _handle_concurrency_change(self) -> None:
        try:
            raw = int(self.concurrency_var.get())
        except (tk.TclError, TypeError, ValueError):
            raw = self._controller.queue_concurrency
        clamped = max(1, min(4, raw))
        if self.concurrency_var.get() != clamped:
            self.concurrency_var.set(clamped)
        if self._on_concurrency_changed is not None:
            self._on_concurrency_changed(clamped)
        else:
            self._controller.set_queue_concurrency(clamped)

    def apply_theme_palette(self, palette: ThemePalette) -> None:
        """Apply semantic log and queue status tag colors from `palette`."""
        self._palette = palette
        self.log_text.tag_configure("error", foreground=palette.error)
        self.log_text.tag_configure("warn", foreground=palette.warning)
        self.log_text.tag_configure("debug", foreground=palette.fg_muted)
        self.log_text.tag_configure("section", foreground=palette.accent_text)
        self.log_text.tag_configure("info", foreground=palette.text_fg)
        self.queue_tree.tag_configure("running", foreground=palette.accent_text)
        self.queue_tree.tag_configure("completed", foreground=palette.success)
        self.queue_tree.tag_configure("failed", foreground=palette.error)
        self.queue_tree.tag_configure("paused", foreground=palette.warning)
        self.queue_tree.tag_configure("interrupted", foreground=palette.warning)
        self.queue_tree.tag_configure("cancelled", foreground=palette.fg_muted)
        self.queue_tree.tag_configure("queued", foreground=palette.fg)
        self.queue_tree.tag_configure("drag_target", background=palette.surface_alt)

    @staticmethod
    def _status_tag(status: str) -> tuple[str, ...]:
        lower = status.strip().lower()
        if lower in ("running", "completed", "failed", "paused", "interrupted", "cancelled", "queued"):
            return (lower,)
        return ()

    @staticmethod
    def _classify_log_tag(level: str, text: str) -> str:
        stripped = text.strip()
        if level == "ERROR" or stripped.startswith("[ERROR]"):
            return "error"
        if level == "WARNING" or stripped.startswith("[WARN]") or stripped.startswith("[WARNING]"):
            return "warn"
        if level == "DEBUG" or stripped.startswith("[DEBUG]"):
            return "debug"
        if stripped.startswith("---") and stripped.endswith("---"):
            return "section"
        return "info"

    def _log_entry_matches(self, level: str, text: str) -> bool:
        level_filter = self.log_level_var.get()
        tag = self._classify_log_tag(level, text)
        if level_filter == "Info+" and tag == "debug":
            return False
        if level_filter == "Errors Only" and tag not in ("error", "warn"):
            return False
        query = self.log_search_var.get().strip().lower()
        if query and query not in text.lower():
            return False
        return True

    def _save_log_to_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self._window,
            title="Save Activity Log",
            defaultextension=".log",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for _level, line in self._log_entries:
                    fh.write(line + "\n")
            self.status_var.set("Saved activity log")
        except OSError as exc:
            messagebox.showerror("Save Log Failed", str(exc), parent=self._window)

    _SEARCH_DEBOUNCE_MS = 150

    def _cancel_after(self, attr_name: str) -> None:
        after_id = getattr(self, attr_name, None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            setattr(self, attr_name, None)

    def _on_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self:
            return
        self._cancel_after("_log_search_after_id")
        self._cancel_after("_queue_filter_after_id")

    def _schedule_rerender_logs(self) -> None:
        self._cancel_after("_log_search_after_id")
        try:
            if self.winfo_exists():
                self._log_search_after_id = self.after(
                    self._SEARCH_DEBOUNCE_MS, self._rerender_logs
                )
        except tk.TclError:
            self._log_search_after_id = None

    def _schedule_queue_filter(self) -> None:
        self._cancel_after("_queue_filter_after_id")
        try:
            if self.winfo_exists():
                self._queue_filter_after_id = self.after(
                    self._SEARCH_DEBOUNCE_MS, self._apply_queue_filter
                )
        except tk.TclError:
            self._queue_filter_after_id = None

    @staticmethod
    def _coalesce_log_chunks(entries: Sequence[tuple[str, str]]) -> tuple[list[Any], int]:
        """Coalesce adjacent `(text, tag)` entries sharing a tag into flat `Text.insert` args."""
        if not entries:
            return [], 0
        flat: list[Any] = []
        buf: list[str] = []
        current_tag: str | None = None
        line_count = 0
        for text, tag in entries:
            line_count += text.count("\n") + 1
            if tag == current_tag:
                buf.append(text + "\n")
            else:
                if buf and current_tag is not None:
                    flat.append("".join(buf))
                    flat.append((current_tag,))
                current_tag = tag
                buf = [text + "\n"]
        if buf and current_tag is not None:
            flat.append("".join(buf))
            flat.append((current_tag,))
        return flat, line_count

    def _rerender_logs(self) -> None:
        self._cancel_after("_log_search_after_id")
        matched = [
            (line, self._classify_log_tag(level, line))
            for level, line in self._log_entries
            if self._log_entry_matches(level, line)
        ]
        flat_chunks, total_lines = self._coalesce_log_chunks(matched)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self._log_line_count = total_lines
        if flat_chunks:
            self.log_text.insert(tk.END, *flat_chunks)
        if self.autoscroll_var.get():
            self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self._log_entries.clear()
        self._log_line_count = 0
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log_batch(self, entries: Sequence[tuple[str, str]]) -> None:
        """Append a batch of `(level, line)` entries efficiently with filtering and smart auto-scroll."""
        if not entries:
            return
        try:
            was_at_bottom = self.log_text.yview()[1] >= 0.99
        except tk.TclError:
            was_at_bottom = True

        matched: list[tuple[str, str]] = []
        for level, text in entries:
            self._log_entries.append((level, text))
            if self._log_entry_matches(level, text):
                matched.append((text, self._classify_log_tag(level, text)))

        if not matched:
            return

        flat_chunks, added_lines = self._coalesce_log_chunks(matched)
        self.log_text.configure(state="normal")
        if flat_chunks:
            self.log_text.insert(tk.END, *flat_chunks)
        self._log_line_count += added_lines
        if self._log_line_count > self.MAX_LOG_LINES:
            excess = self._log_line_count - self.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
            self._log_line_count -= excess
        if self.autoscroll_var.get() and was_at_bottom:
            self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, text: str) -> None:
        """Append a line to the scrollable activity log, capping total line count."""
        tag = self._classify_log_tag("INFO", text)
        level = {"error": "ERROR", "warn": "WARNING", "debug": "DEBUG"}.get(tag, "INFO")
        self.append_log_batch([(level, text)])

    def set_running_state(
        self,
        running: bool,
        *,
        is_previewing: bool,
        is_syncing: bool = False,
    ) -> None:
        self._last_is_previewing = is_previewing
        self._last_is_syncing = is_syncing
        if running:
            self.download_btn.configure(
                text="Add to Queue",
                state="disabled" if is_previewing else "normal",
            )
            self.pause_btn.configure(
                text="Resume" if self._controller.is_paused() else "Pause",
                state="normal",
            )
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        elif is_syncing or is_previewing:
            self.download_btn.configure(text="Download", state="disabled")
            self.pause_btn.configure(text="Pause", state="disabled")
            self.cancel_btn.configure(state="normal")
            self.preview_btn.configure(state="disabled")
        else:
            self.download_btn.configure(text="Download", state="normal")
            self.pause_btn.configure(text="Pause", state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.preview_btn.configure(state="normal")

    def set_previewing_state(
        self,
        previewing: bool,
        *,
        is_running: bool,
        is_syncing: bool | None = None,
    ) -> None:
        effective_syncing = self._last_is_syncing if is_syncing is None else is_syncing
        self.set_running_state(
            is_running,
            is_previewing=previewing,
            is_syncing=effective_syncing,
        )

    def toggle_pause(self, *, is_running: bool | None = None) -> None:
        """Toggle pause/resume state for remaining queued jobs."""
        active = self._controller.worker_has_active_run if is_running is None else is_running
        if not active:
            return
        paused = self._controller.toggle_pause()
        self.pause_btn.configure(text="Resume" if paused else "Pause")
        if paused:
            self.status_var.set("Queue paused")
            self.append_log("--- Queue paused ---")
        else:
            self.status_var.set("Queue resumed")
            self.append_log("--- Queue resumed ---")

    def refresh_batch_label(self) -> None:
        parts = []
        if self.ui_item_total > 0 and self.ui_item_index > 0:
            parts.append(f"Item {self.ui_item_index} / {self.ui_item_total}")
        if self.ui_book_total > 1 and self.ui_book_index > 0:
            parts.append(f"Book {self.ui_book_index} / {self.ui_book_total}")
        self.batch_var.set("  \u2022  ".join(parts))

    def _resolve_progress_key(self, job_id: str | None) -> str:
        if job_id is not None:
            return job_id
        if len(self._running_job_ids) == 1:
            return self._running_job_ids[0]
        return self._default_progress_key

    def _get_or_create_progress_state(self, job_id: str | None) -> tuple[str, _JobProgressState]:
        key = self._resolve_progress_key(job_id)
        state = self._job_progress.get(key)
        if state is None:
            state = _JobProgressState()
            self._job_progress[key] = state
        return key, state

    def _aggregate_progress_summary(
        self,
    ) -> tuple[float, int, int | None, bool, float, float | None, str]:
        states = [
            self._job_progress[jid]
            for jid in self._running_job_ids
            if jid in self._job_progress
        ]
        if not states and self._default_progress_key in self._job_progress:
            states = [self._job_progress[self._default_progress_key]]
        if not states:
            return 0.0, 0, None, False, 0.0, None, ""
        if len(states) == 1:
            s = states[0]
            return (
                s.pct,
                s.bytes_downloaded,
                s.estimated_bytes,
                s.estimated_exact,
                s.speed_bps,
                s.eta_seconds,
                s.stage,
            )
        known_est = [s for s in states if s.estimated_bytes is not None and s.estimated_bytes > 0]
        if len(known_est) == len(states):
            total_est = sum(s.estimated_bytes for s in known_est if s.estimated_bytes is not None)
            pct = (
                sum(
                    s.pct * (s.estimated_bytes or 0)
                    for s in known_est
                )
                / total_est
                if total_est > 0
                else sum(s.pct for s in states) / len(states)
            )
        else:
            pct = sum(s.pct for s in states) / len(states)
        bytes_downloaded = sum(s.bytes_downloaded for s in states)
        estimated_bytes = (
            sum(s.estimated_bytes for s in known_est if s.estimated_bytes is not None)
            if known_est
            else None
        )
        estimated_exact = len(known_est) == len(states) and all(
            s.estimated_exact for s in known_est
        )
        speed_bps = sum(s.speed_bps for s in states)
        etas = [s.eta_seconds for s in states if s.eta_seconds is not None]
        eta_seconds = max(etas) if etas else None
        stages = {s.stage for s in states if s.stage}
        stage = next(iter(stages)) if len(stages) == 1 else f"Stage: Running {len(states)} jobs"
        return pct, bytes_downloaded, estimated_bytes, estimated_exact, speed_bps, eta_seconds, stage

    def _refresh_header_progress(self, *, force_metrics: bool = False) -> None:
        pct, _bytes, _est, _exact, _speed, _eta, stage = self._aggregate_progress_summary()
        try:
            if str(self.progressbar.cget("mode")) != "indeterminate":
                self.progress_var.set(pct)
        except tk.TclError:
            self.progress_var.set(pct)
        if stage:
            self.stage_var.set(stage)
        self.update_metrics_display(is_running=True, force=force_metrics)

    def update_metrics_display(self, *, is_running: bool, force: bool = False) -> None:
        if not is_running or self._run_start_time is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_metrics_render_at) < 0.5:
            return
        self._last_metrics_render_at = now
        elapsed = max(0.0, now - self._run_start_time)
        pct, bytes_downloaded, estimated_bytes, estimated_exact, speed_bps, eta_seconds, _stage = (
            self._aggregate_progress_summary()
        )
        parts = []
        if pct > 0:
            parts.append(f"{pct:.0f}%")
        if estimated_bytes is not None and estimated_bytes > 0:
            est_str = format_size_estimate(estimated_bytes, is_exact=estimated_exact)
            if bytes_downloaded > 0:
                parts.append(f"{format_bytes(bytes_downloaded)} / {est_str}")
            else:
                parts.append(f"Est. {est_str}")
        elif bytes_downloaded > 0:
            parts.append(format_bytes(bytes_downloaded))
        if speed_bps > 0:
            parts.append(format_speed(speed_bps))
        parts.append(f"Elapsed {format_duration(elapsed)}")
        if eta_seconds is not None:
            parts.append(f"ETA {format_duration(eta_seconds)}")
        self.metrics_var.set("  \u2022  ".join(parts))

    @staticmethod
    def _stage_from_message(message: str) -> str | None:
        lower = message.strip().lower()
        if not lower:
            return None
        if "combining" in lower or "packing" in lower:
            return "Stage: Combining & muxing"
        if "converting" in lower:
            return "Stage: Converting audio"
        if "splitting" in lower:
            return "Stage: Splitting chapters"
        if "enrich" in lower:
            return "Stage: Enriching metadata"
        if "adding chapters" in lower:
            return "Stage: Writing chapters"
        if "adding metadata" in lower:
            return "Stage: Writing tags"
        if "embedding cover" in lower or "adding cover" in lower:
            return "Stage: Embedding cover"
        if "uploading" in lower or "audiobookshelf" in lower:
            return "Stage: Syncing library"
        if "downloading" in lower:
            return "Stage: Downloading"
        return None

    def _update_stage_from_message(
        self, message: str, *, job_id: str | None = None
    ) -> None:
        stage = self._stage_from_message(message)
        if stage is None:
            return
        if job_id is not None or len(self._running_job_ids) <= 1:
            key, state = self._get_or_create_progress_state(job_id)
            state.stage = stage
            self._update_job_row_live_detail(key, force=True)
            if len(self._running_job_ids) <= 1:
                self.stage_var.set(stage)
        else:
            self.stage_var.set(stage)

    @staticmethod
    def _format_live_row_detail(state: _JobProgressState) -> str:
        stage = state.stage.strip()
        if stage == "Stage: Downloading":
            pct_str = f"Downloading {state.pct:.0f}%"
            extras: list[str] = []
            if state.estimated_bytes is not None and state.estimated_bytes > 0:
                est_str = format_size_estimate(
                    state.estimated_bytes, is_exact=state.estimated_exact
                )
                if state.bytes_downloaded > 0:
                    extras.append(f"{format_bytes(state.bytes_downloaded)} / {est_str}")
                else:
                    extras.append(f"Est. {est_str}")
            if state.speed_bps > 0:
                extras.append(format_speed(state.speed_bps))
            return " \u2022 ".join([pct_str, *extras])
        return stage or "Running"

    def _update_job_row_live_detail(self, job_id: str, *, force: bool = False) -> None:
        if not job_id or job_id == self._default_progress_key or not self.queue_tree.exists(job_id):
            return
        state = self._job_progress.get(job_id)
        if state is None:
            return
        now = time.monotonic()
        if not force and (now - state.last_row_render_at) < 0.5:
            return
        state.last_row_render_at = now
        self.queue_tree.set(job_id, "detail", self._format_live_row_detail(state))

    def _set_progress(self, value: float, *, indeterminate: bool = False) -> None:
        if indeterminate:
            self.progressbar.configure(mode="indeterminate")
            self.progress_var.set(0.0)
            self.progressbar.start(12)
            if self._on_progress_mode_changed is not None:
                self._on_progress_mode_changed("indeterminate")
            return
        self.progressbar.stop()
        self.progressbar.configure(mode="determinate")
        if self._on_progress_mode_changed is not None:
            self._on_progress_mode_changed("determinate")
        self.progress_var.set(float(value))

    def prepare_run_ui_state(self, run_id: int, item_total: int, log_header: str) -> None:
        """Initialize progress, timer, and batch counters for a fresh queue run."""
        self._set_progress(0.0)
        self.status_var.set("Starting...")
        self.active_job_var.set("Starting...")
        self.stage_var.set("Stage: Preparing")
        self._run_start_time = time.monotonic()
        self._running_job_ids.clear()
        self._job_progress.clear()
        self._last_metrics_render_at = 0.0
        self.current_ui_run_id = run_id
        self.ui_item_index = 1
        self.ui_item_total = item_total
        self.ui_book_index = 0
        self.ui_book_total = 0
        self.refresh_batch_label()
        self.update_metrics_display(is_running=True, force=True)
        self.append_log(log_header)

    @staticmethod
    def format_queue_row_values(rec: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
        """Format `(service, profile, display_target, status, detail)` columns for `queue_tree`."""
        service = str(rec.get("service") or "")
        url = str(rec.get("url") or "")
        title = str(rec.get("title") or "").strip()
        job_kind = str(rec.get("job_kind") or "download")
        if job_kind == "pack":
            profile_text = ""
        else:
            profile_text = str(rec.get("profile") or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
        if title and title != url:
            display_target = title if job_kind == "pack" else f"{title} \u2014 {url}"
        else:
            display_target = url
        return (
            service,
            profile_text,
            display_target,
            str(rec.get("status") or ""),
            str(rec.get("detail") or ""),
        )

    def _queue_record_matches_filter(self, rec: Mapping[str, Any] | None) -> bool:
        if rec is None:
            return False
        filt = self.queue_filter_var.get()
        st = str(rec.get("status") or "")
        if filt == "Queued" and st not in ("Queued", "Paused"):
            return False
        if filt == "Running" and st != "Running":
            return False
        if filt == "Completed" and st != "Completed":
            return False
        if filt == "Failed" and st not in ("Failed", "Cancelled", "Interrupted"):
            return False
        q = self.queue_search_var.get().strip().lower()
        if q:
            hay = " ".join(
                str(rec.get(k) or "") for k in ("service", "profile", "title", "url", "status", "detail")
            ).lower()
            if q not in hay:
                return False
        return True

    def _sync_single_row_visibility(
        self,
        job_id: str,
        rec: dict[str, Any] | None = None,
    ) -> None:
        if not self.queue_tree.exists(job_id):
            self._detached_iids.discard(job_id)
            return
        if rec is None:
            rec = self._controller.get_queue_record(job_id)
        if self._queue_record_matches_filter(rec):
            if job_id in self._detached_iids:
                pos = 0
                for iid in self._queue_order:
                    if iid == job_id:
                        break
                    if iid not in self._detached_iids and self.queue_tree.exists(iid):
                        pos += 1
                self.queue_tree.reattach(job_id, "", pos)
                self._detached_iids.discard(job_id)
        elif job_id not in self._detached_iids:
            self.queue_tree.detach(job_id)
            self._detached_iids.add(job_id)

    def _apply_queue_filter(self, *, force_reorder: bool = False) -> None:
        self._cancel_after("_queue_filter_after_id")
        is_unfiltered = (
            (self.queue_filter_var.get() or "All") == "All"
            and not (self.queue_search_var.get() or "").strip()
        )
        if not is_unfiltered or self._detached_iids or force_reorder:
            pos = 0
            for iid in list(self._queue_order):
                if not self.queue_tree.exists(iid):
                    self._detached_iids.discard(iid)
                    continue
                rec = self._controller.get_queue_record(iid)
                if self._queue_record_matches_filter(rec):
                    if iid in self._detached_iids or not is_unfiltered or force_reorder:
                        self.queue_tree.reattach(iid, "", pos)
                        self._detached_iids.discard(iid)
                    pos += 1
                elif iid not in self._detached_iids:
                    self.queue_tree.detach(iid)
                    self._detached_iids.add(iid)
        self._refresh_queue_empty_and_badge()

    def _refresh_queue_empty_and_badge(self) -> None:
        visible = self.queue_tree.get_children()
        self._scrolled_queue.set_empty_state(
            not visible,
            "No matching jobs in queue." if self._queue_order else "Queue is empty. Enter URLs above or browse a library to get started.",
        )
        active = 0
        failed = 0
        for iid in self._queue_order:
            rec = self._controller.get_queue_record(iid)
            st = str((rec or {}).get("status") or "")
            if st in ("Running", "Queued", "Paused"):
                active += 1
            elif st in ("Failed", "Interrupted"):
                failed += 1
        parts: list[str] = []
        if active:
            parts.append(f"{active} active")
        if failed:
            parts.append(f"{failed} failed")
        label = f"Queue & History ({', '.join(parts)})" if parts else "Queue & History"
        try:
            self.activity_notebook.tab(self._queue_tab, text=label)  # type: ignore[no-untyped-call]
        except tk.TclError:
            pass
        if self._on_queue_summary_changed is not None:
            self._on_queue_summary_changed(active, failed)
        self._sync_queue_toolbar_buttons()

    def restore_saved_queue_history(self) -> None:
        """Restore unresolved queue items persisted across GUI restarts."""
        for rec in self._controller.load_saved_queue_records():
            job_id = rec["job_id"]
            if job_id in self._queue_order or self.queue_tree.exists(job_id):
                continue
            self._queue_order.append(job_id)
            self.queue_tree.insert(
                "",
                tk.END,
                iid=job_id,
                values=self.format_queue_row_values(rec),
                tags=self._status_tag(str(rec.get("status") or "")),
            )
        self._apply_queue_filter()

    def register_queue_job(self, job: QueuedJob, *, persist: bool = True) -> None:
        """Insert a queued job row into the Queue & History table and internal index."""
        service_label = (
            "Local Pack" if job.pack_request is not None else self._service_label_for_url(job.url)
        )
        existing = self._controller.get_queue_record(job.job_id)
        if existing is not None:
            rec = existing
            if persist:
                self._controller._request_save_queue_state()
        else:
            rec = self._controller.register_queue_record(job, service_label, persist=persist)
        self._queue_order.append(job.job_id)
        self.queue_tree.insert(
            "",
            tk.END,
            iid=job.job_id,
            values=self.format_queue_row_values(rec),
            tags=self._status_tag(str(rec.get("status") or "")),
        )
        self._sync_single_row_visibility(job.job_id, rec)
        self._refresh_queue_empty_and_badge()

    def update_queue_row(self, job_id: str, status: str, detail: str = "") -> None:
        rec = self._controller.update_queue_record(job_id, status, detail)
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=self.format_queue_row_values(rec),
                tags=self._status_tag(status),
            )
        self._sync_single_row_visibility(job_id, rec)
        self._refresh_queue_empty_and_badge()

    def update_queue_row_metadata(
        self,
        job_id: str,
        *,
        title: str | None = None,
        output_path: str | None = None,
    ) -> None:
        """Update resolved title / output path on a queue row and refresh its display."""
        rec = self._controller.update_queue_metadata(
            job_id,
            title=title,
            output_path=output_path,
        )
        if rec is None:
            return
        if self.queue_tree.exists(job_id):
            self.queue_tree.item(
                job_id,
                values=self.format_queue_row_values(rec),
            )
        self._sync_queue_toolbar_buttons()

    def _selected_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for iid in self.queue_tree.selection():
            rec = self._controller.get_queue_record(iid)
            if rec is not None:
                out.append(rec)
        return out

    @staticmethod
    def _resolve_record_path(rec: Mapping[str, Any], *, reveal_in_folder: bool) -> str | None:
        out_path = str(rec.get("output_path") or "").strip()
        if out_path:
            if os.path.exists(out_path):
                return out_path
            if reveal_in_folder:
                parent = os.path.dirname(out_path)
                if parent and os.path.isdir(parent):
                    return parent
        if reveal_in_folder and rec.get("job_kind") == "pack":
            pack_req = rec.get("pack_request")
            if isinstance(pack_req, Mapping):
                for raw_path in pack_req.get("paths") or ():
                    cand = str(raw_path).strip()
                    if cand and os.path.exists(cand):
                        return cand
        return None

    def _open_selected_output(self, *, reveal_in_folder: bool = False) -> None:
        records = self._selected_records()
        if not records or self._on_open_path is None:
            return
        target = self._resolve_record_path(records[0], reveal_in_folder=reveal_in_folder)
        if not target:
            self.status_var.set("No output file or folder recorded for selected job")
            return
        self._on_open_path(target, reveal_in_folder)

    def _retry_selected_items(self) -> None:
        selected = list(self.queue_tree.selection())
        if not selected or self._on_retry_selected is None:
            return
        self._on_retry_selected(selected)

    def _preview_selected_item(self) -> None:
        records = self._selected_records()
        if len(records) != 1 or self._on_preview_url is None:
            return
        rec = records[0]
        if rec.get("job_kind", "download") == "download" and rec.get("url"):
            self._on_preview_url(str(rec["url"]))

    def _send_selected_to_urls(self) -> None:
        if self._on_send_to_urls is None:
            return
        urls = [
            str(rec["url"])
            for rec in self._selected_records()
            if rec.get("job_kind", "download") == "download" and rec.get("url")
        ]
        if urls:
            self._on_send_to_urls(urls)

    def _copy_selected_urls(self) -> None:
        values = [str(rec.get("url") or "") for rec in self._selected_records() if rec.get("url")]
        if not values:
            return
        self._window.clipboard_clear()
        self._window.clipboard_append("\n".join(values))
        self.status_var.set(f"Copied {len(values)} item{'s' if len(values) != 1 else ''}")

    def _copy_selected_detail(self) -> None:
        records = self._selected_records()
        if len(records) != 1:
            return
        detail = str(records[0].get("detail") or "").strip()
        if not detail:
            return
        self._window.clipboard_clear()
        self._window.clipboard_append(detail)
        self.status_var.set("Copied job detail to clipboard")

    def _profiles_for_record(self, rec: Mapping[str, Any]) -> list[str]:
        url = str(rec.get("url") or "").strip()
        svc_key = detect_service_key(url) if url else None
        if not svc_key:
            svc_key = str(rec.get("service") or "").strip().lower()
        if not svc_key:
            return [DEFAULT_PROFILE]
        profiles = list_source_profiles(self._controller.config_snapshot(), svc_key)
        if DEFAULT_PROFILE not in profiles:
            profiles = [DEFAULT_PROFILE, *profiles]
        return profiles

    def _apply_job_config_update(
        self,
        job_id: str,
        profile: str,
        job_overrides: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        updated = self._controller.update_queue_job_config(
            job_id,
            profile=profile,
            job_overrides=job_overrides,
        )
        if updated is not None and self.queue_tree.exists(job_id):
            self.queue_tree.item(job_id, values=self.format_queue_row_values(updated))
        return updated

    def _edit_selected_job_options(self) -> None:
        records = self._selected_records()
        if len(records) != 1:
            return
        rec = records[0]
        if rec.get("job_kind", "download") not in ("download", "pack"):
            return
        if str(rec.get("status") or "") not in JobInspectorDialog._EDITABLE_STATUSES:
            return
        dlg = _JobOptionsDialog(
            self._window,
            rec,
            profiles=self._profiles_for_record(rec),
            palette=self._palette,
        )
        self._window.wait_window(dlg)
        if dlg.result is None:
            return
        prof, overrides = dlg.result
        job_id = str(rec.get("job_id") or "")
        if job_id:
            self._apply_job_config_update(job_id, prof, overrides)
            self.status_var.set(f"Updated job options (profile: {prof})")

    def _change_selected_profile(self) -> None:
        records = [
            rec
            for rec in self._selected_records()
            if rec.get("job_kind", "download") == "download"
            and str(rec.get("status") or "") in JobInspectorDialog._EDITABLE_STATUSES
        ]
        if not records:
            return
        profiles: list[str] = [DEFAULT_PROFILE]
        for rec in records:
            for p in self._profiles_for_record(rec):
                if p not in profiles:
                    profiles.append(p)
        current_prof = str(records[0].get("profile") or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
        dlg = ModalDialog(
            self._window,
            "Change Profile",
            palette=self._palette,
            default_size=(340, 140),
            min_size=(300, 130),
            resizable=(True, True),
        )
        prof_var = tk.StringVar(value=current_prof)
        chosen: list[str] = []
        body = ttk.Frame(dlg, padding=scale_px(dlg, 12))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Profile:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        combo = ttk.Combobox(body, textvariable=prof_var, values=profiles, state="normal")
        combo.grid(row=0, column=1, sticky="ew", pady=4)
        btns = ttk.Frame(body)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        def _ok() -> None:
            chosen.append((prof_var.get() or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE)
            dlg.destroy()

        ttk.Button(btns, text="Apply", command=_ok, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        dlg.finalize_modal(initial_focus=combo)
        self._window.wait_window(dlg)
        if not chosen:
            return
        target_prof = chosen[0]
        for rec in records:
            jid = str(rec.get("job_id") or "")
            if jid:
                self._apply_job_config_update(jid, target_prof)
        self.status_var.set(f"Updated profile to '{target_prof}' for {len(records)} job(s)")

    def _show_selected_details(self) -> None:
        records = self._selected_records()
        if len(records) != 1:
            return
        JobInspectorDialog(
            self._window,
            records[0],
            profiles=self._profiles_for_record(records[0]),
            on_save_options=self._apply_job_config_update,
            palette=self._palette,
        )

    def _on_queue_double_click(self, event: tk.Event) -> None:
        row_iid = self.queue_tree.identify_row(event.y)
        if not row_iid:
            return
        self.queue_tree.focus(row_iid)
        self.queue_tree.selection_set(row_iid)
        rec = self._controller.get_queue_record(row_iid)
        if rec is None:
            return
        if rec.get("status") == "Completed" and self._on_open_path is not None:
            target = self._resolve_record_path(rec, reveal_in_folder=False)
            if target:
                self._on_open_path(target, False)
                return
        self._show_selected_details()

    def _show_queue_context_menu_from_key(self, _event: tk.Event | None = None) -> str:
        sel = self.queue_tree.selection()
        focus_iid = self.queue_tree.focus() or (sel[0] if sel else "")
        try:
            bbox = self.queue_tree.bbox(focus_iid) if focus_iid else None
            if bbox and len(bbox) == 4:
                x_root = self.queue_tree.winfo_rootx() + bbox[0] + 16
                y_root = self.queue_tree.winfo_rooty() + bbox[1] + bbox[3]
            else:
                x_root = self.queue_tree.winfo_rootx() + 16
                y_root = self.queue_tree.winfo_rooty() + 16
        except tk.TclError:
            return "break"
        return self._popup_queue_context_menu_at(x_root, y_root)

    def _show_queue_context_menu(self, event: tk.Event) -> str:
        row_iid = self.queue_tree.identify_row(event.y)
        if row_iid:
            if row_iid not in self.queue_tree.selection():
                self.queue_tree.selection_set(row_iid)
            self.queue_tree.focus(row_iid)
        return self._popup_queue_context_menu_at(event.x_root, event.y_root)

    def _popup_queue_context_menu_at(self, x_root: int, y_root: int) -> str:

        records = self._selected_records()
        single = records[0] if len(records) == 1 else None
        can_open = bool(
            single is not None
            and self._on_open_path is not None
            and self._resolve_record_path(single, reveal_in_folder=False)
        )
        can_reveal = bool(
            single is not None
            and self._on_open_path is not None
            and self._resolve_record_path(single, reveal_in_folder=True)
        )
        can_retry = any(
            rec.get("status") in ("Failed", "Cancelled", "Interrupted")
            and (
                (rec.get("job_kind", "download") == "download" and rec.get("url"))
                or (rec.get("job_kind") == "pack" and isinstance(rec.get("pack_request"), dict))
            )
            for rec in records
        )
        can_preview = bool(
            single is not None
            and self._on_preview_url is not None
            and single.get("job_kind", "download") == "download"
            and single.get("url")
        )
        can_send = (
            self._on_send_to_urls is not None
            and any(rec.get("job_kind", "download") == "download" and rec.get("url") for rec in records)
        )
        can_copy_url = any(bool(rec.get("url")) for rec in records)
        can_copy_detail = bool(single is not None and str(single.get("detail") or "").strip())
        can_move = bool(single is not None and single.get("status") == "Queued")
        can_remove = any(rec.get("status") != "Running" for rec in records)
        has_completed_only = any(
            (rec := self._controller.get_queue_record(iid)) is not None
            and rec.get("status") == "Completed"
            for iid in self._queue_order
        )
        has_failed_only = any(
            (rec := self._controller.get_queue_record(iid)) is not None
            and rec.get("status") in ("Failed", "Cancelled", "Interrupted")
            for iid in self._queue_order
        )
        has_clearable = has_completed_only or has_failed_only

        can_edit_job = (
            single is not None
            and single.get("job_kind", "download") in ("download", "pack")
            and str(single.get("status") or "") in JobInspectorDialog._EDITABLE_STATUSES
        )
        can_change_profile = bool(
            records
            and all(
                rec.get("job_kind", "download") == "download"
                and str(rec.get("status") or "") in JobInspectorDialog._EDITABLE_STATUSES
                for rec in records
            )
        )

        menu = self._queue_menu
        menu.entryconfigure("Open Output", state="normal" if can_open else "disabled")
        menu.entryconfigure("Open Containing Folder", state="normal" if can_reveal else "disabled")
        menu.entryconfigure("View Job Details...", state="normal" if single is not None else "disabled")
        menu.entryconfigure("Edit Job Options...", state="normal" if can_edit_job else "disabled")
        menu.entryconfigure("Change Profile...", state="normal" if can_change_profile else "disabled")
        menu.entryconfigure("Retry Selected", state="normal" if (can_retry and self._on_retry_selected) else "disabled")
        menu.entryconfigure("Preview Metadata...", state="normal" if can_preview else "disabled")
        menu.entryconfigure("Send URL to Input", state="normal" if can_send else "disabled")
        menu.entryconfigure("Copy URL / Source", state="normal" if can_copy_url else "disabled")
        menu.entryconfigure("Copy Detail / Error", state="normal" if can_copy_detail else "disabled")
        menu.entryconfigure("Move to Top", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move Up", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move Down", state="normal" if can_move else "disabled")
        menu.entryconfigure("Move to Bottom", state="normal" if can_move else "disabled")
        menu.entryconfigure("Remove Selected", state="normal" if can_remove else "disabled")
        menu.entryconfigure(
            "Clear Completed Only", state="normal" if has_completed_only else "disabled"
        )
        menu.entryconfigure(
            "Clear Failed & Cancelled", state="normal" if has_failed_only else "disabled"
        )
        menu.entryconfigure("Clear Completed & Failed", state="normal" if has_clearable else "disabled")

        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()
        return "break"

    def remove_selected_item(self) -> None:
        selected = list(self.queue_tree.selection())
        if not selected:
            return
        saw_active_running = False
        removed_any = False
        latest_total: int | None = None
        for job_id in selected:
            removed, new_total, is_active_running = self._controller.remove_pending_job(job_id)
            if is_active_running:
                saw_active_running = True
                continue
            if not removed:
                continue
            removed_any = True
            if new_total is not None:
                latest_total = new_total
            if self.queue_tree.exists(job_id):
                self.queue_tree.delete(job_id)
            self._detached_iids.discard(job_id)
            if job_id in self._queue_order:
                self._queue_order.remove(job_id)
        if latest_total is not None:
            self.ui_item_total = max(self.ui_item_index, latest_total)
            self.refresh_batch_label()
        if removed_any:
            self._refresh_queue_empty_and_badge()
        if saw_active_running and not removed_any:
            messagebox.showinfo(
                "Job Active",
                "The currently running download cannot be removed; use Cancel instead.",
                parent=self._window,
            )

    def move_selected_item(self, direction: int) -> None:
        selected = self.queue_tree.selection()
        if len(selected) != 1:
            return
        job_id = selected[0]
        neighbor_id = self._controller.move_pending_job(job_id, direction)
        if neighbor_id is None:
            return
        if job_id in self._queue_order and neighbor_id in self._queue_order:
            ia = self._queue_order.index(job_id)
            ib = self._queue_order.index(neighbor_id)
            self._queue_order[ia], self._queue_order[ib] = self._queue_order[ib], self._queue_order[ia]
        self._apply_queue_filter(force_reorder=True)
        if self.queue_tree.exists(job_id):
            self.queue_tree.see(job_id)

    def _sync_pending_order_in_tree(self, pending_order: Sequence[str]) -> None:
        pending_set = set(pending_order)
        # Keep non-pending (running/completed) in place; place reordered pending jobs in order
        it = iter(pending_order)
        self._queue_order = [
            next(it) if jid in pending_set else jid for jid in self._queue_order
        ]
        self._apply_queue_filter(force_reorder=True)

    def move_selected_to_edge(self, *, to_top: bool) -> None:
        selected = self.queue_tree.selection()
        if len(selected) != 1:
            return
        new_order = self._controller.move_pending_job_to_edge(selected[0], to_top=to_top)
        if new_order is not None:
            self._sync_pending_order_in_tree(new_order)

    def _clear_drag_hover(self) -> None:
        hover_iid = self._drag_hover_iid
        self._drag_hover_iid = None
        if hover_iid and self.queue_tree.exists(hover_iid):
            tags = [t for t in self.queue_tree.item(hover_iid, "tags") if t != "drag_target"]
            self.queue_tree.item(hover_iid, tags=tuple(tags))

    def _is_queued_row(self, iid: str | None) -> bool:
        if not iid:
            return False
        rec = self._controller.get_queue_record(iid)
        return rec is not None and rec.get("status") == "Queued"

    def _on_queue_drag_start(self, event: tk.Event) -> None:
        self._clear_drag_hover()
        if self._scrolled_queue._sort_col is not None:
            self._drag_job_id = None
            self._drag_start_y = None
            return
        if self.queue_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            self._drag_job_id = None
            self._drag_start_y = None
            return
        row_iid = self.queue_tree.identify_row(event.y)
        if not self._is_queued_row(row_iid):
            self._drag_job_id = None
            self._drag_start_y = None
            return
        self._drag_job_id = row_iid
        self._drag_start_y = event.y

    def _on_queue_drag_motion(self, event: tk.Event) -> str | None:
        if not self._drag_job_id or self._drag_start_y is None:
            return None
        if not self._is_queued_row(self._drag_job_id):
            self._drag_job_id = None
            self._drag_start_y = None
            self._clear_drag_hover()
            return None
        if abs(event.y - self._drag_start_y) < scale_px(self, 6):
            self._clear_drag_hover()
            return None
        target = self.queue_tree.identify_row(event.y)
        if target == self._drag_hover_iid:
            return "break"
        self._clear_drag_hover()
        if (
            target
            and target != self._drag_job_id
            and self.queue_tree.exists(target)
            and self._is_queued_row(target)
        ):
            tags = list(self.queue_tree.item(target, "tags"))
            if "drag_target" not in tags:
                tags.append("drag_target")
                self.queue_tree.item(target, tags=tuple(tags))
            self._drag_hover_iid = target
        return "break"

    def _on_queue_drag_drop(self, event: tk.Event) -> None:
        src = self._drag_job_id
        start_y = self._drag_start_y
        self._drag_job_id = None
        self._drag_start_y = None
        self._clear_drag_hover()
        if not src or start_y is None or self._scrolled_queue._sort_col is not None:
            return
        if abs(event.y - start_y) < scale_px(self, 6):
            return
        target = self.queue_tree.identify_row(event.y)
        if (
            not target
            or target == src
            or not self._is_queued_row(src)
            or not self._is_queued_row(target)
        ):
            return
        new_order = self._controller.move_pending_job_to_target(src, target)
        if new_order is not None:
            self._sync_pending_order_in_tree(new_order)

    def clear_completed_history(self, *, statuses: Sequence[str] | None = None) -> None:
        target_statuses = (
            set(statuses)
            if statuses is not None
            else {"Completed", "Failed", "Cancelled", "Interrupted"}
        )
        matching_failed = [
            iid
            for iid in self._queue_order
            if (rec := self._controller.get_queue_record(iid)) is not None
            and rec.get("status") in target_statuses
            and rec.get("status") in ("Failed", "Cancelled", "Interrupted")
        ]
        if matching_failed:
            matching_total = sum(
                1
                for iid in self._queue_order
                if (rec := self._controller.get_queue_record(iid)) is not None
                and rec.get("status") in target_statuses
            )
            if not messagebox.askyesno(
                "Clear Queue History",
                (
                    f"Remove {matching_total} finished/failed job(s) from the queue history "
                    f"(including {len(matching_failed)} failed or cancelled)?"
                ),
                parent=self._window,
            ):
                return
        for iid in self._controller.clear_completed_records(
            list(self._queue_order), statuses=tuple(target_statuses)
        ):
            if self.queue_tree.exists(iid):
                self.queue_tree.delete(iid)
            self._detached_iids.discard(iid)
            if iid in self._queue_order:
                self._queue_order.remove(iid)
        self._refresh_queue_empty_and_badge()

    def handle_activity_event(self, item: ControllerEvent) -> bool:
        """Handle log, batch/book progress, and queue status events; return True if handled."""
        if isinstance(item, LogEvent):
            level, message = item.level, item.message
            prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
            self.append_log_batch([(level, f"{prefix}{message}")])
            if level == "INFO" and message.strip():
                self.status_var.set(message.strip().splitlines()[0][:80])
                self._update_stage_from_message(message, job_id=item.job_id)
            return True
        if isinstance(item, LogBatchEvent):
            formatted: list[tuple[str, str]] = []
            last_info = ""
            for level, message in item.entries:
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                formatted.append((level, f"{prefix}{message}"))
                if level == "INFO" and message.strip():
                    last_info = message
            self.append_log_batch(formatted)
            if last_info:
                self.status_var.set(last_info.strip().splitlines()[0][:80])
                self._update_stage_from_message(last_info)
            return True
        if isinstance(item, BatchItemEvent):
            if item.run_id == self.current_ui_run_id:
                self.ui_item_index = item.item_index
                self.ui_item_total = max(self.ui_item_total, item.item_total)
                self.ui_book_index = 0
                self.ui_book_total = 0
                if len(self._running_job_ids) <= 1:
                    self.stage_var.set("Stage: Fetching metadata")
                    if item.target_url:
                        self.active_job_var.set(item.target_url.strip()[:60])
                self.refresh_batch_label()
            return True
        if isinstance(item, JobStatusEvent):
            self.update_queue_row(item.job_id, item.status, item.detail)
            if item.status == "Running":
                if item.job_id not in self._running_job_ids:
                    self._running_job_ids.append(item.job_id)
                self._job_progress[item.job_id] = _JobProgressState(stage="Stage: Preparing")
                if len(self._running_job_ids) > 1:
                    self.active_job_var.set(f"{len(self._running_job_ids)} active jobs")
                else:
                    rec = self._controller.get_queue_record(item.job_id)
                    if rec is not None:
                        label = str(rec.get("title") or rec.get("url") or "").strip()
                        if label:
                            self.active_job_var.set(label[:60])
            else:
                if item.job_id in self._running_job_ids:
                    self._running_job_ids.remove(item.job_id)
                self._job_progress.pop(item.job_id, None)
                if len(self._running_job_ids) > 1:
                    self.active_job_var.set(f"{len(self._running_job_ids)} active jobs")
                elif len(self._running_job_ids) == 1:
                    rem_rec = self._controller.get_queue_record(self._running_job_ids[0])
                    if rem_rec is not None:
                        label = str(rem_rec.get("title") or rem_rec.get("url") or "").strip()
                        if label:
                            self.active_job_var.set(label[:60])
            return True
        if isinstance(item, JobMetaEvent):
            self.update_queue_row_metadata(
                item.job_id,
                title=item.title or None,
                output_path=item.output_path or None,
            )
            if item.title and len(self._running_job_ids) <= 1:
                self.active_job_var.set(item.title.strip()[:60])
            return True
        if isinstance(item, ItemProgressEvent):
            if item.run_id == self.current_ui_run_id:
                self.ui_book_index = item.current
                self.ui_book_total = item.total
                if item.title and len(self._running_job_ids) <= 1:
                    self.active_job_var.set(item.title.strip()[:60])
                self.refresh_batch_label()
            return True
        if isinstance(item, ProgressStartEvent):
            self._set_progress(0.0)
            key, state = self._get_or_create_progress_state(item.job_id)
            state.pct = 0.0
            state.speed_bps = 0.0
            state.eta_seconds = None
            state.bytes_downloaded = 0
            state.estimated_bytes = None
            state.estimated_exact = False
            state.stage = "Stage: Downloading"
            self._refresh_header_progress(force_metrics=True)
            self._update_job_row_live_detail(key, force=True)
            state.last_row_render_at = 0.0
            self.status_var.set(item.description[:80])
            if item.description.strip() and len(self._running_job_ids) <= 1:
                self.active_job_var.set(item.description.strip()[:60])
            return True
        if isinstance(item, DownloadEstimateEvent):
            key, state = self._get_or_create_progress_state(item.job_id)
            state.estimated_bytes = item.estimated_bytes
            state.estimated_exact = item.is_exact
            self._refresh_header_progress(force_metrics=True)
            self._update_job_row_live_detail(key, force=True)
            return True
        if isinstance(item, ProgressAdvanceEvent):
            key, state = self._get_or_create_progress_state(item.job_id)
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            state.pct = min(100.0, max(0.0, pct))
            self._refresh_header_progress(force_metrics=False)
            self._update_job_row_live_detail(key, force=False)
            return True
        if isinstance(item, ProgressMetricsEvent):
            key, state = self._get_or_create_progress_state(item.job_id)
            pct = (item.completed / item.total * 100.0) if item.total > 0 else 0.0
            state.pct = min(100.0, max(0.0, pct))
            state.bytes_downloaded = item.bytes_downloaded
            state.speed_bps = item.speed_bps
            state.eta_seconds = item.eta_seconds
            self._refresh_header_progress(force_metrics=True)
            self._update_job_row_live_detail(key, force=False)
            return True
        if isinstance(item, ProgressFinishEvent):
            key, state = self._get_or_create_progress_state(item.job_id)
            state.pct = 100.0
            state.eta_seconds = 0.0
            self._refresh_header_progress(force_metrics=True)
            self._update_job_row_live_detail(key, force=True)
            return True
        return False

    def finish_run(
        self,
        summary: DownloadSummary | None,
        exc: Exception | None,
        *,
        notify_enabled: bool,
        notifier: Callable[[str, str], None] | None = None,
    ) -> None:
        """Reset progress/metrics UI and report run completion/failure/cancellation."""
        notify_fn = notifier or send_os_notification_async
        self._set_progress(0.0)
        final_elapsed = (
            format_duration(max(0.0, time.monotonic() - self._run_start_time))
            if self._run_start_time is not None
            else None
        )
        self._run_start_time = None
        self._running_job_ids.clear()
        self._job_progress.clear()
        self.batch_var.set("")
        self.stage_var.set("")
        if exc is not None:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Error")
            self.active_job_var.set("Idle (last run failed)")
            self.append_log(f"[ERROR] {exc}")
            if notify_enabled:
                payload = format_completion_notification(summary, exc)
                if payload is not None:
                    notify_fn(payload[0], payload[1])
            messagebox.showerror("Download Failed", str(exc), parent=self._window)
            return

        if summary is None or summary.cancelled:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Cancelled")
            self.active_job_var.set("Idle (cancelled)")
            return

        if summary.failed > 0 and summary.completed == 0:
            self._set_progress(0.0)
            self.metrics_var.set("")
            self.status_var.set("Failed")
            self.active_job_var.set("Idle (failed)")
        elif summary.failed > 0:
            self._set_progress(100.0)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
            self.active_job_var.set(f"Done ({summary.completed} ok, {summary.failed} failed)")
        else:
            self._set_progress(100.0)
            self.metrics_var.set(f"Elapsed {final_elapsed}" if final_elapsed else "")
            self.status_var.set("Completed")
            self.active_job_var.set("Completed")
            self.append_log("--- Completed ---")
        if notify_enabled:
            payload = format_completion_notification(summary, None)
            if payload is not None:
                notify_fn(payload[0], payload[1])

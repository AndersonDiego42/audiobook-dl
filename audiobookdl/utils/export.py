"""Library export utilities for CSV, JSON, and Excel (.xlsx) formats."""

import csv
import json
import os
from typing import Any
from collections.abc import Sequence

from audiobookdl.exceptions import GenericAudiobookDLException
from audiobookdl.utils.archive import ArchiveEntry

SUPPORTED_EXPORT_FORMATS = ("csv", "json", "xlsx")

EXPORT_COLUMNS = (
    "id",
    "status",
    "source",
    "profile",
    "title",
    "author",
    "narrator",
    "series",
    "series_order",
    "tags",
    "audio_format",
    "duration_seconds",
    "isbn",
    "asin",
    "output_path",
    "url",
    "book_key",
    "downloaded_at",
    "last_seen_at",
)


def resolve_export_format(output_path: str, explicit_format: str | None = None) -> str:
    """Resolve and validate the export format (`csv`, `json`, or `xlsx`)."""
    raw = (explicit_format or os.path.splitext(output_path)[1].lstrip(".") or "csv").strip().lower()
    if raw not in SUPPORTED_EXPORT_FORMATS:
        raise GenericAudiobookDLException(
            f"Unsupported export format '{raw}'. Supported formats: {', '.join(SUPPORTED_EXPORT_FORMATS)}"
        )
    return raw


def _entry_to_dict(entry: ArchiveEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "status": entry.status,
        "source": entry.source,
        "profile": entry.profile,
        "title": entry.title,
        "author": entry.author,
        "narrator": entry.narrator,
        "series": entry.series,
        "series_order": entry.series_order,
        "tags": list(entry.tags),
        "audio_format": entry.audio_format,
        "duration_seconds": entry.duration_seconds,
        "isbn": entry.isbn,
        "asin": entry.asin,
        "output_path": entry.output_path,
        "url": entry.url,
        "book_key": entry.book_key,
        "downloaded_at": entry.downloaded_at,
        "last_seen_at": entry.last_seen_at,
    }


def _entry_to_row(entry: ArchiveEntry) -> list[Any]:
    return [
        entry.id,
        entry.status,
        entry.source,
        entry.profile,
        entry.title,
        entry.author,
        entry.narrator,
        entry.series or "",
        entry.series_order if entry.series_order is not None else "",
        " ".join(entry.tags),
        entry.audio_format or "",
        entry.duration_seconds if entry.duration_seconds is not None else "",
        entry.isbn or "",
        entry.asin or "",
        entry.output_path or "",
        entry.url or "",
        entry.book_key,
        entry.downloaded_at,
        entry.last_seen_at or "",
    ]


def export_library_entries(
    entries: Sequence[ArchiveEntry],
    output_path: str,
    *,
    fmt: str | None = None,
) -> int:
    """Export `entries` to `output_path` in CSV, JSON, or XLSX format and return count exported."""
    resolved_fmt = resolve_export_format(output_path, fmt)
    abs_path = os.path.abspath(output_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if resolved_fmt == "csv":
        with open(abs_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            for entry in entries:
                writer.writerow(_entry_to_row(entry))
    elif resolved_fmt == "json":
        payload = [_entry_to_dict(entry) for entry in entries]
        with open(abs_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    else:
        try:
            import openpyxl
        except ImportError as exc:
            raise GenericAudiobookDLException(
                "Excel (.xlsx) export requires 'openpyxl'. Install with: pip install audiobook-dl[export]"
            ) from exc
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Library"
        sheet.append(list(EXPORT_COLUMNS))
        for entry in entries:
            sheet.append(_entry_to_row(entry))
        workbook.save(abs_path)

    return len(entries)

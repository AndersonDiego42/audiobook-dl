"""Library export utilities for CSV, JSON, and Excel (.xlsx) formats."""

import csv
import io
import json
import os
import tempfile
from typing import Any
from collections.abc import Sequence

from audiobookdl.exceptions import GenericAudiobookDLException
from audiobookdl.utils.archive import ArchiveEntry
from audiobookdl.utils.fs import atomic_write_text, replace_with_retry

_FORMULA_PREFIXES = ("=", "+", "-", "@")

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


def _sanitize_spreadsheet_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula/DDE injection payloads in string cells."""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in ("\t", "\r", "\n") or value.lstrip("\t\r\n ").startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _entry_to_row(entry: ArchiveEntry) -> list[Any]:
    raw_row: list[Any] = [
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
    return [_sanitize_spreadsheet_cell(cell) for cell in raw_row]


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
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_COLUMNS)
        for entry in entries:
            writer.writerow(_entry_to_row(entry))
        atomic_write_text(abs_path, buf.getvalue(), encoding="utf-8", newline="")
    elif resolved_fmt == "json":
        payload = [_entry_to_dict(entry) for entry in entries]
        atomic_write_text(
            abs_path,
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
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
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(abs_path)}.",
            suffix=".xlsx.tmp",
            dir=parent or ".",
        )
        os.close(fd)
        try:
            workbook.save(tmp_path)
            replace_with_retry(tmp_path, abs_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return len(entries)

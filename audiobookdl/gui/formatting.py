"""Pure formatting, validation, and preview-building helpers for the desktop GUI."""

from __future__ import annotations

from datetime import date
import io
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any
from collections.abc import Sequence

if TYPE_CHECKING:
    import tkinter as tk

from audiobookdl import output
from audiobookdl.options_schema import (
    GUI_MEDIA_TYPE_OPTIONS,
    GUI_OUTPUT_FORMATS,
    GUI_OVERWRITE_OPTIONS,
)
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookMetadata,
    BookId,
    Cover,
    is_ebook,
    parse_language,
    parse_release_date,
)


OUTPUT_FORMATS = GUI_OUTPUT_FORMATS
MEDIA_TYPE_OPTIONS = GUI_MEDIA_TYPE_OPTIONS
OVERWRITE_OPTIONS = GUI_OVERWRITE_OPTIONS
TEMPLATE_PRESET_LABELS: tuple[tuple[str, str], ...] = (
    ("Custom", ""),
    ("Default", output.OUTPUT_TEMPLATE_PRESETS["default"]),
    ("Audiobookshelf", output.OUTPUT_TEMPLATE_PRESETS["audiobookshelf"]),
    ("Plex", output.OUTPUT_TEMPLATE_PRESETS["plex"]),
    ("Kavita", output.OUTPUT_TEMPLATE_PRESETS["kavita"]),
)
PRESET_LABEL_TO_TEMPLATE: dict[str, str] = {label: tpl for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
TEMPLATE_TO_PRESET_LABEL: dict[str, str] = {tpl: label for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
SAMPLE_PREVIEW_METADATA = AudiobookMetadata(
    title="Dune",
    authors=["Frank Herbert"],
    series="Dune Chronicles",
    series_order=1,
    release_date=date(1965, 8, 1),
    source="storytel",
)
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]-?\d+)([+-]-?\d+))?$")


def _parse_tk_offset(token: str) -> int:
    if token.startswith("+-"):
        return int(token[1:])
    return int(token)


def parse_geometry(geometry: str) -> tuple[int, int, int | None, int | None] | None:
    """Parse a Tkinter geometry string (`WxH` or `WxH+X+Y` / `WxH+-X+Y`) into `(w, h, x, y)`."""
    match = _GEOMETRY_RE.fullmatch(geometry.strip())
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    raw_x = match.group(3)
    raw_y = match.group(4)
    x = _parse_tk_offset(raw_x) if raw_x is not None else None
    y = _parse_tk_offset(raw_y) if raw_y is not None else None
    return width, height, x, y


def format_geometry(width: int, height: int, x: int | None = None, y: int | None = None) -> str:
    """Format `(width, height, x, y)` as a Tk geometry string using `+X+Y` (preserving negative coords as `+-X`)."""
    if x is None or y is None:
        return f"{int(width)}x{int(height)}"
    return f"{int(width)}x{int(height)}+{int(x)}+{int(y)}"


def is_valid_geometry(geometry: str) -> bool:
    """Validate a Tkinter geometry string (`WxH` or `WxH+X+Y`) with sane minimum dimensions."""
    parsed = parse_geometry(geometry)
    if parsed is None:
        return False
    width, height, _x, _y = parsed
    return width >= 400 and height >= 400


def get_monitor_work_areas(widget: tk.Misc) -> list[tuple[int, int, int, int]]:
    """Return `[(left, top, right, bottom), ...]` work areas for connected monitors."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            windll: Any = getattr(ctypes, "windll", None)
            winfunc: Any = getattr(ctypes, "WINFUNCTYPE", None)
            if windll is not None and winfunc is not None:
                monitors: list[tuple[int, int, int, int]] = []
                primary: list[tuple[int, int, int, int]] = []
                user32 = windll.user32
                monitor_enum_proc = winfunc(
                    wintypes.BOOL,
                    wintypes.HMONITOR,
                    wintypes.HDC,
                    ctypes.POINTER(wintypes.RECT),
                    wintypes.LPARAM,
                )

                def _cb(hmon: Any, _hdc: Any, _rect: Any, _data: Any) -> bool:
                    info = _MONITORINFO()
                    info.cbSize = ctypes.sizeof(_MONITORINFO)
                    if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                        rc = info.rcWork
                        area = (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
                        if area[2] > area[0] and area[3] > area[1]:
                            if info.dwFlags & 1:  # MONITORINFOF_PRIMARY
                                primary.append(area)
                            else:
                                monitors.append(area)
                    return True

                user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(_cb), 0)
                ordered = primary + monitors
                if ordered:
                    return ordered
        except Exception:
            pass
    try:
        sw = max(1, int(widget.winfo_screenwidth()))
        sh = max(1, int(widget.winfo_screenheight()))
        return [(0, 0, sw, sh)]
    except Exception:
        return [(0, 0, 1280, 800)]


def _pick_monitor_for_rect(
    rect: tuple[int, int, int, int],
    monitors: Sequence[tuple[int, int, int, int]],
) -> tuple[int, int, int, int] | None:
    rx1, ry1, rx2, ry2 = rect
    best_area = 0
    best_mon: tuple[int, int, int, int] | None = None
    for mon in monitors:
        ml, mt, mr, mb = mon
        ix = max(0, min(rx2, mr) - max(rx1, ml))
        iy = max(0, min(ry2, mb) - max(ry1, mt))
        area = ix * iy
        if area > best_area:
            best_area = area
            best_mon = mon
    return best_mon


def clamp_rect_to_monitor(
    width: int,
    height: int,
    x: int | None,
    y: int | None,
    monitors: Sequence[tuple[int, int, int, int]],
    *,
    min_width: int = 200,
    min_height: int = 200,
) -> tuple[int, int, int, int]:
    """Clamp `(width, height, x, y)` onto a visible monitor, centering on primary if offscreen."""
    mon_list = list(monitors) if monitors else [(0, 0, 1280, 800)]
    primary = mon_list[0]
    target_mon = (
        _pick_monitor_for_rect((x, y, x + width, y + height), mon_list)
        if (x is not None and y is not None)
        else None
    )
    if target_mon is None:
        target_mon = primary
        ml, mt, mr, mb = target_mon
        mon_w = max(1, mr - ml)
        mon_h = max(1, mb - mt)
        w = min(max(min(min_width, mon_w), width), mon_w)
        h = min(max(min(min_height, mon_h), height), mon_h)
        cx = ml + max(0, (mon_w - w) // 2)
        cy = mt + max(0, (mon_h - h) // 2)
        return w, h, cx, cy

    ml, mt, mr, mb = target_mon
    mon_w = max(1, mr - ml)
    mon_h = max(1, mb - mt)
    w = min(max(min(min_width, mon_w), width), mon_w)
    h = min(max(min(min_height, mon_h), height), mon_h)
    assert x is not None and y is not None
    cx = max(ml, min(x, mr - w))
    cy = max(mt, min(y, mb - h))
    return w, h, cx, cy


def clamp_geometry_to_monitors(
    geometry: str,
    monitors: Sequence[tuple[int, int, int, int]],
    *,
    min_width: int = 400,
    min_height: int = 400,
) -> str | None:
    """Validate and clamp a Tk geometry string onto `monitors`, falling back to primary center."""
    parsed = parse_geometry(geometry)
    if parsed is None:
        return None
    width, height, x, y = parsed
    if width < 400 or height < 400:
        return None
    w, h, cx, cy = clamp_rect_to_monitor(
        width,
        height,
        x,
        y,
        monitors,
        min_width=min_width,
        min_height=min_height,
    )
    return format_geometry(w, h, cx, cy)


def format_speed(bytes_per_sec: float) -> str:
    """Format a byte rate into human-readable B/s, KB/s, or MB/s."""
    if bytes_per_sec <= 0:
        return "--"
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    if bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"


def format_duration(seconds: float | None) -> str:
    """Format a duration in seconds as `MM:SS` or `H:MM:SS`."""
    if seconds is None or seconds < 0:
        return "--"
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_failed_job_detail(
    book_errors: Sequence[tuple[str, str]],
    max_length: int = 120,
) -> str:
    """Format the first `(label, message)` error tuple into a single-line status string."""
    if not book_errors:
        return "Download failed"[:max_length]
    label, message = book_errors[0]
    label_text = " ".join(str(label).split())
    message_text = " ".join(str(message).split())
    if label_text and message_text:
        detail = f"{label_text}: {message_text}"
    else:
        detail = message_text or label_text or "Download failed"
    return detail[:max_length]


def describe_series_book(index: int, book: BookId[Any] | Audiobook) -> str:
    """Return a readable display label for an entry inside a `Series`."""
    if isinstance(book, Audiobook):
        author = book.metadata.author
        return f"{book.title} — {author}" if author else book.title
    title = getattr(book, "title", None)
    if title:
        return str(title)
    book_id = getattr(book, "id", book)
    if isinstance(book_id, dict):
        dict_title = book_id.get("title") or book_id.get("name")
        if dict_title:
            return str(dict_title)
    return f"Book {index + 1} (ID: {book_id})"


def format_audiobook_preview(audiobook: Audiobook) -> list[tuple[str, str]]:
    """Build ordered `(label, value)` metadata rows for previewing an `Audiobook`."""
    meta = audiobook.metadata
    rows: list[tuple[str, str]] = [("Title", audiobook.title)]
    if meta.author:
        rows.append(("Author(s)", meta.author))
    if meta.narrator:
        rows.append(("Narrator(s)", meta.narrator))
    if meta.series:
        series_text = (
            f"{meta.series} #{meta.series_order}"
            if meta.series_order is not None
            else meta.series
        )
        rows.append(("Series", series_text))
    if meta.genre:
        rows.append(("Genre(s)", meta.genre))
    if meta.language is not None:
        lang_name = getattr(meta.language, "name", None) or str(meta.language)
        rows.append(("Language", lang_name))
    if meta.publisher:
        rows.append(("Publisher", meta.publisher))
    if meta.release_date is not None:
        rows.append(("Released", str(meta.release_date)))
    if meta.isbn:
        rows.append(("ISBN", meta.isbn))
    if meta.asin:
        rows.append(("ASIN", meta.asin))
    file_count = len(audiobook.files)
    exts = sorted({f.ext.lstrip(".").lower() for f in audiobook.files if f.ext})
    ext_suffix = f" ({', '.join(exts)})" if exts else ""
    rows.append(("Parts", f"{file_count} file{'s' if file_count != 1 else ''}{ext_suffix}"))
    chap_count = len(audiobook.chapters)
    if not (is_ebook(audiobook) and chap_count == 0):
        rows.append(("Chapters", f"{chap_count} chapter{'s' if chap_count != 1 else ''}"))
    return rows


def format_multi_metadata_values(values: Sequence[str]) -> str:
    """Format a sequence of metadata strings (authors/narrators) as semicolon-separated text."""
    return "; ".join(v.strip() for v in values if v and v.strip())


def parse_multi_metadata_values(raw: str) -> list[str]:
    """Parse semicolon-separated metadata text into trimmed non-empty values."""
    return [part.strip() for part in raw.split(";") if part.strip()]


def apply_audiobook_metadata_edits(
    audiobook: Audiobook,
    *,
    title: str,
    authors_text: str,
    narrators_text: str,
    series_text: str,
    series_order_text: str,
    cover: Cover | None = None,
    update_cover: bool = False,
    genres_text: str | None = None,
    publisher: str | None = None,
    release_date_text: str | None = None,
    language_text: str | None = None,
    isbn: str | None = None,
    asin: str | None = None,
    description: str | None = None,
) -> None:
    """Validate and apply inline metadata/cover edits onto `audiobook`."""
    cleaned_title = title.strip()
    if not cleaned_title:
        raise ValueError("Title cannot be empty.")

    order_raw = series_order_text.strip()
    parsed_order: int | None
    if not order_raw:
        parsed_order = None
    else:
        try:
            parsed_order = int(order_raw)
        except ValueError as exc:
            raise ValueError(f"Series order must be a whole number, got '{order_raw}'.") from exc

    parsed_date = audiobook.metadata.release_date
    if release_date_text is not None:
        raw_date = release_date_text.strip()
        if not raw_date:
            parsed_date = None
        else:
            parsed_date = parse_release_date(raw_date)
            if parsed_date is None:
                raise ValueError(f"Invalid release date '{raw_date}' (use YYYY-MM-DD or YYYY).")

    parsed_lang = audiobook.metadata.language
    if language_text is not None:
        raw_lang = language_text.strip()
        if not raw_lang:
            parsed_lang = None
        else:
            parsed_lang = parse_language(raw_lang)
            if parsed_lang is None:
                raise ValueError(f"Unrecognized language '{raw_lang}' (e.g. 'en' or 'English').")

    audiobook.metadata.title = cleaned_title
    audiobook.metadata.authors = parse_multi_metadata_values(authors_text)
    audiobook.metadata.narrators = parse_multi_metadata_values(narrators_text)
    audiobook.metadata.series = series_text.strip() or None
    audiobook.metadata.series_order = parsed_order
    if genres_text is not None:
        audiobook.metadata.genres = parse_multi_metadata_values(genres_text)
    if publisher is not None:
        audiobook.metadata.publisher = publisher.strip() or None
    if release_date_text is not None:
        audiobook.metadata.release_date = parsed_date
    if language_text is not None:
        audiobook.metadata.language = parsed_lang
    if isbn is not None:
        audiobook.metadata.isbn = isbn.strip() or None
    if asin is not None:
        audiobook.metadata.asin = asin.strip() or None
    if description is not None:
        audiobook.metadata.description = description.strip() or None
    if update_cover:
        audiobook.cover = cover


def format_timestamp_ms(ms: int) -> str:
    """Format milliseconds as `HH:MM:SS.mmm` or `MM:SS.mmm`."""
    clamped = max(0, int(ms))
    total_secs, rem_ms = divmod(clamped, 1000)
    hours, rem_secs = divmod(total_secs, 3600)
    minutes, secs = divmod(rem_secs, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{rem_ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{rem_ms:03d}"


def parse_timestamp_ms(raw: str) -> int:
    """Parse `HH:MM:SS(.mmm)`, `MM:SS(.mmm)`, or seconds string into milliseconds."""
    text = raw.strip()
    if not text:
        raise ValueError("Timestamp cannot be empty.")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            sec = float(parts[0])
            if sec < 0:
                raise ValueError("Timestamp cannot be negative.")
            return round(sec * 1000)
        if len(parts) == 2:
            mins = int(parts[0])
            sec = float(parts[1])
            if mins < 0 or sec < 0 or sec >= 60:
                raise ValueError(f"Invalid timestamp '{text}'.")
            return round((mins * 60 + sec) * 1000)
        if len(parts) == 3:
            hrs = int(parts[0])
            mins = int(parts[1])
            sec = float(parts[2])
            if hrs < 0 or mins < 0 or mins >= 60 or sec < 0 or sec >= 60:
                raise ValueError(f"Invalid timestamp '{text}'.")
            return round((hrs * 3600 + mins * 60 + sec) * 1000)
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp '{text}' (use MM:SS.mmm or HH:MM:SS.mmm).") from exc
    raise ValueError(f"Invalid timestamp '{text}'.")


def load_cover_from_file(path: str) -> Cover:
    """Read and validate an image file from disk into a `Cover` object."""
    from PIL import Image

    with open(path, "rb") as handle:
        raw_bytes = handle.read()
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img.verify()
        fmt = (img.format or "").lower()
    if fmt == "jpeg":
        ext = "jpg"
    elif fmt:
        ext = fmt
    else:
        ext = Path(path).suffix.lstrip(".").lower() or "jpg"
    return Cover(image=raw_bytes, extension=ext)


def render_cover_thumbnail(
    cover: Cover | None,
    master: tk.Misc,
    max_size: int = 180,
) -> Any | None:
    """Decode `cover` bytes into a resized `ImageTk.PhotoImage`, or `None` on failure."""
    if cover is None or not cover.image:
        return None
    try:
        from PIL import Image, ImageTk

        image = Image.open(io.BytesIO(cover.image))
        image.thumbnail((max_size, max_size))
        return ImageTk.PhotoImage(image, master=master)
    except Exception:
        return None


def resize_cover_thumbnail_bytes(raw_bytes: bytes, size: int = 48) -> bytes | None:
    """Decode raw image bytes in a worker thread into square `size x size` PNG bytes."""
    if not raw_bytes:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw_bytes)) as opened:
            rgba = opened.convert("RGBA")
            square = ImageOps.pad(rgba, (size, size), color=(0, 0, 0, 0))
            buf = io.BytesIO()
            square.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def resize_cover_bytes_aspect_thumbnail(raw_bytes: bytes, max_size: int = 160) -> bytes | None:
    """Decode and aspect-preserving resize raw cover bytes into PNG bytes on a worker thread."""
    if not raw_bytes:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as opened:
            img = opened.convert("RGBA") if opened.mode not in ("RGB", "RGBA") else opened.copy()
        img.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def resize_cover_file_thumbnail_bytes(path: str, max_size: int = 140) -> bytes | None:
    """Read and resize a local cover image file into PNG bytes on a worker thread."""
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            raw_bytes = handle.read()
        return resize_cover_bytes_aspect_thumbnail(raw_bytes, max_size=max_size)
    except Exception:
        return None


def photo_from_image_bytes(raw_bytes: bytes, master: tk.Misc) -> Any | None:
    """Convert pre-sized PNG/image bytes into an `ImageTk.PhotoImage` on the Tk main thread."""
    if not raw_bytes:
        return None
    try:
        from PIL import Image, ImageTk

        with Image.open(io.BytesIO(raw_bytes)) as image:
            return ImageTk.PhotoImage(image.copy(), master=master)
    except Exception:
        return None


def create_placeholder_cover_thumbnail(
    master: tk.Misc,
    size: int = 48,
    *,
    bg_hex: str,
    border_hex: str,
    glyph_hex: str,
) -> Any | None:
    """Render a minimal square placeholder book-cover icon matching the active palette."""
    try:
        from PIL import Image, ImageDraw, ImageTk

        img = Image.new("RGBA", (size, size), bg_hex)
        draw = ImageDraw.Draw(img)
        pad = max(2, size // 12)
        draw.rectangle([pad, pad, size - 1 - pad, size - 1 - pad], outline=border_hex, width=1)
        bx0 = size // 3
        bx1 = size - bx0
        by0 = size // 4
        by1 = size - by0
        draw.rectangle([bx0, by0, bx1, by1], outline=glyph_hex, width=1)
        draw.line([bx0 + max(2, size // 14), by0, bx0 + max(2, size // 14), by1], fill=glyph_hex, width=1)
        return ImageTk.PhotoImage(img, master=master)
    except Exception:
        return None


def format_catalog_formats(book: Any) -> str:
    """Format advertised catalog media types (`Audiobook + Ebook`, `Audiobook`, `Ebook`)."""
    formats = getattr(book, "formats", ()) or ()
    has_ab = "audiobook" in formats
    has_eb = "ebook" in formats
    if has_ab and has_eb:
        return "Audiobook + Ebook"
    if has_eb:
        return "Ebook"
    if has_ab:
        return "Audiobook"
    return ""


def default_output_dir() -> str:
    """Return the default output directory (user Downloads folder if it exists)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return str(downloads)
    return os.getcwd()


def parse_url_text(raw: str) -> list[str]:
    """Extract non-empty URLs from whitespace/newline-separated input text."""
    return [token.strip() for token in raw.split() if token.strip()]

"""Pure formatting, validation, and preview-building helpers for the desktop GUI."""

from datetime import date
import io
import os
from pathlib import Path
import re
import tkinter as tk
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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
)


OUTPUT_FORMATS = GUI_OUTPUT_FORMATS
MEDIA_TYPE_OPTIONS = GUI_MEDIA_TYPE_OPTIONS
OVERWRITE_OPTIONS = GUI_OVERWRITE_OPTIONS
TEMPLATE_PRESET_LABELS: Tuple[Tuple[str, str], ...] = (
    ("Custom", ""),
    ("Default", output.OUTPUT_TEMPLATE_PRESETS["default"]),
    ("Audiobookshelf", output.OUTPUT_TEMPLATE_PRESETS["audiobookshelf"]),
    ("Plex", output.OUTPUT_TEMPLATE_PRESETS["plex"]),
    ("Kavita", output.OUTPUT_TEMPLATE_PRESETS["kavita"]),
)
PRESET_LABEL_TO_TEMPLATE: Dict[str, str] = {label: tpl for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
TEMPLATE_TO_PRESET_LABEL: Dict[str, str] = {tpl: label for label, tpl in TEMPLATE_PRESET_LABELS if tpl}
SAMPLE_PREVIEW_METADATA = AudiobookMetadata(
    title="Dune",
    authors=["Frank Herbert"],
    series="Dune Chronicles",
    series_order=1,
    release_date=date(1965, 8, 1),
    source="storytel",
)
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


def is_valid_geometry(geometry: str) -> bool:
    """Validate a Tkinter geometry string (`WxH` or `WxH+X+Y`) with sane minimum dimensions."""
    match = _GEOMETRY_RE.fullmatch(geometry.strip())
    if not match:
        return False
    width = int(match.group(1))
    height = int(match.group(2))
    return width >= 400 and height >= 400


def format_speed(bytes_per_sec: float) -> str:
    """Format a byte rate into human-readable B/s, KB/s, or MB/s."""
    if bytes_per_sec <= 0:
        return "--"
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    if bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"


def format_duration(seconds: Optional[float]) -> str:
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
    book_errors: Sequence[Tuple[str, str]],
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


def describe_series_book(index: int, book: Union[BookId[Any], Audiobook]) -> str:
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


def format_audiobook_preview(audiobook: Audiobook) -> List[Tuple[str, str]]:
    """Build ordered `(label, value)` metadata rows for previewing an `Audiobook`."""
    meta = audiobook.metadata
    rows: List[Tuple[str, str]] = [("Title", audiobook.title)]
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
    file_count = len(audiobook.files)
    exts = sorted({f.ext.lstrip(".").lower() for f in audiobook.files if f.ext})
    ext_suffix = f" ({', '.join(exts)})" if exts else ""
    rows.append(("Parts", f"{file_count} file{'s' if file_count != 1 else ''}{ext_suffix}"))
    chap_count = len(audiobook.chapters)
    if not (is_ebook(audiobook) and chap_count == 0):
        rows.append(("Chapters", f"{chap_count} chapter{'s' if chap_count != 1 else ''}"))
    return rows


def render_cover_thumbnail(
    cover: Optional[Cover],
    master: tk.Misc,
    max_size: int = 180,
) -> Optional[Any]:
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


def default_output_dir() -> str:
    """Return the default output directory (user Downloads folder if it exists)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return str(downloads)
    return os.getcwd()


def parse_url_text(raw: str) -> List[str]:
    """Extract non-empty URLs from whitespace/newline-separated input text."""
    return [token.strip() for token in raw.split() if token.strip()]

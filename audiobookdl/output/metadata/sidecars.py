import os
from typing import Optional, Sequence
from xml.sax.saxutils import escape, quoteattr

from audiobookdl import AudiobookMetadata, Chapter, Cover


def _cue_timestamp(ms: int) -> str:
    """Format milliseconds as a CUE `MM:SS:FF` timestamp (75 frames/sec)."""
    total_ms = max(0, int(ms))
    total_seconds, rem_ms = divmod(total_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    frames = min(74, (rem_ms * 75) // 1000)
    return f"{minutes:02d}:{seconds:02d}:{frames:02d}"


def build_opf_xml(
    metadata: AudiobookMetadata,
    cover_filename: Optional[str] = None,
) -> str:
    """Build an OPF 2.0 XML sidecar document compatible with Audiobookshelf and Calibre."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
        f"    <dc:title>{escape(metadata.title or '')}</dc:title>",
    ]
    for author in metadata.authors:
        lines.append(f'    <dc:creator opf:role="aut">{escape(author)}</dc:creator>')
    for narrator in metadata.narrators:
        lines.append(f'    <dc:creator opf:role="nrt">{escape(narrator)}</dc:creator>')
    if metadata.description:
        lines.append(f"    <dc:description>{escape(metadata.description)}</dc:description>")
    if metadata.publisher:
        lines.append(f"    <dc:publisher>{escape(metadata.publisher)}</dc:publisher>")
    if metadata.release_date:
        lines.append(f"    <dc:date>{escape(metadata.release_date.strftime('%Y-%m-%d'))}</dc:date>")
    if metadata.language:
        lang_code = getattr(metadata.language, "alpha_2", None) or getattr(metadata.language, "alpha_3", "")
        if lang_code:
            lines.append(f"    <dc:language>{escape(str(lang_code))}</dc:language>")
    if metadata.isbn:
        lines.append(f'    <dc:identifier opf:scheme="ISBN">{escape(metadata.isbn)}</dc:identifier>')
    if metadata.asin:
        lines.append(f'    <dc:identifier opf:scheme="ASIN">{escape(metadata.asin)}</dc:identifier>')
    if metadata.scrape_url:
        lines.append(f'    <dc:identifier opf:scheme="URL">{escape(metadata.scrape_url)}</dc:identifier>')
    for genre in metadata.genres:
        lines.append(f"    <dc:subject>{escape(genre)}</dc:subject>")
    if metadata.series:
        lines.append(f'    <meta name="calibre:series" content={quoteattr(metadata.series)} />')
        if metadata.series_order is not None:
            lines.append(
                f'    <meta name="calibre:series_index" content={quoteattr(str(metadata.series_order))} />'
            )
    if cover_filename:
        lines.append('    <meta name="cover" content="cover-image" />')
    lines.append("  </metadata>")
    if cover_filename:
        ext = os.path.splitext(cover_filename)[1].lower().lstrip(".")
        media_type = "image/png" if ext == "png" else "image/jpeg"
        lines.extend([
            "  <manifest>",
            f'    <item id="cover-image" href={quoteattr(cover_filename)} media-type={quoteattr(media_type)} />',
            "  </manifest>",
            "  <guide>",
            f'    <reference type="cover" title="Cover" href={quoteattr(cover_filename)} />',
            "  </guide>",
        ])
    lines.append("</package>")
    return "\n".join(lines) + "\n"


def _cue_file_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext == "mp3":
        return "MP3"
    if ext in ("aiff", "aif"):
        return "AIFF"
    return "WAVE"


def _escape_cue_text(text: str) -> str:
    return text.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def build_cue_sheet(
    title: str,
    audio_filename: str,
    chapters: Sequence[Chapter],
    authors: Sequence[str] = (),
) -> str:
    """Build a CUE sheet listing each chapter with `MM:SS:FF` start offsets."""
    lines: list[str] = []
    if authors:
        lines.append(f'PERFORMER "{_escape_cue_text("; ".join(authors))}"')
    lines.append(f'TITLE "{_escape_cue_text(title)}"')
    lines.append(f'FILE "{_escape_cue_text(audio_filename)}" {_cue_file_type(audio_filename)}')
    for idx, ch in enumerate(chapters, start=1):
        lines.append(f"  TRACK {idx:02d} AUDIO")
        lines.append(f'    TITLE "{_escape_cue_text(ch.title)}"')
        lines.append(f"    INDEX 01 {_cue_timestamp(ch.start)}")
    return "\n".join(lines) + "\n"


def write_sidecar_cover(cover: Cover, target_path: str) -> str:
    """Write `cover` bytes to `target_path` and return the path written."""
    with open(target_path, "wb") as f:
        f.write(cover.image)
    return target_path


def write_sidecar_opf(
    metadata: AudiobookMetadata,
    target_path: str,
    cover_filename: Optional[str] = None,
) -> str:
    """Write an OPF XML metadata sidecar file to `target_path`."""
    content = build_opf_xml(metadata, cover_filename=cover_filename)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)
    return target_path


def write_sidecar_cue(
    metadata: AudiobookMetadata,
    audio_filename: str,
    chapters: Sequence[Chapter],
    target_path: str,
) -> str:
    """Write a chapter CUE sheet to `target_path`."""
    content = build_cue_sheet(
        title=metadata.title,
        audio_filename=audio_filename,
        chapters=chapters,
        authors=metadata.authors,
    )
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)
    return target_path


def build_abs_metadata_dict(
    metadata: AudiobookMetadata,
    chapters: Sequence[Chapter] = (),
    *,
    tags: Sequence[str] = (),
    explicit: bool = False,
    abridged: bool = False,
    duration_seconds: Optional[float] = None,
) -> dict:
    """Build an Audiobookshelf-compatible `metadata.json` dictionary."""
    series_list = []
    if metadata.series:
        series_entry = {"name": metadata.series}
        if metadata.series_order is not None:
            series_entry["sequence"] = str(metadata.series_order)
        series_list.append(series_entry)

    abs_chapters = []
    for idx, ch in enumerate(chapters):
        start_sec = round(max(0, int(ch.start)) / 1000.0, 3)
        if idx + 1 < len(chapters):
            end_sec = round(max(start_sec, int(chapters[idx + 1].start) / 1000.0), 3)
        elif duration_seconds is not None and duration_seconds > start_sec:
            end_sec = round(max(start_sec, float(duration_seconds)), 3)
        else:
            end_sec = start_sec
        abs_chapters.append(
            {
                "id": idx,
                "start": start_sec,
                "end": end_sec,
                "title": ch.title,
            }
        )

    lang_val = None
    if metadata.language is not None:
        lang_val = getattr(metadata.language, "name", None) or str(metadata.language)

    return {
        "tags": list(tags),
        "chapters": abs_chapters,
        "title": metadata.title or None,
        "subtitle": None,
        "authors": list(metadata.authors),
        "narrators": list(metadata.narrators),
        "series": series_list,
        "genres": list(metadata.genres),
        "publishedYear": str(metadata.release_date.year) if metadata.release_date else None,
        "publishedDate": metadata.release_date.isoformat() if metadata.release_date else None,
        "publisher": metadata.publisher,
        "description": metadata.description,
        "isbn": metadata.isbn,
        "asin": metadata.asin,
        "language": lang_val,
        "explicit": bool(explicit),
        "abridged": bool(abridged),
    }


def write_sidecar_abs_metadata(
    metadata: AudiobookMetadata,
    target_path: str,
    *,
    chapters: Sequence[Chapter] = (),
    tags: Sequence[str] = (),
    explicit: bool = False,
    abridged: bool = False,
    duration_seconds: Optional[float] = None,
) -> str:
    """Write an Audiobookshelf-compatible `metadata.json` sidecar file to `target_path`."""
    import json

    payload = build_abs_metadata_dict(
        metadata,
        chapters,
        tags=tags,
        explicit=explicit,
        abridged=abridged,
        duration_seconds=duration_seconds,
    )
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return target_path

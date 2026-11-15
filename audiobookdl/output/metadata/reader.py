"""Read-only extractors for embedded audio tags, folder sidecars, and path heuristics."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4, MP4Cover
import pycountry

from audiobookdl import AudiobookMetadata, Chapter, Cover
from audiobookdl.utils.audiobook import parse_release_date


_AUDIO_COVER_CANDIDATES = (
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "cover.webp",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
)

_EXT_TO_MIME_REVERSE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+\s+AUDIO\b", re.IGNORECASE)
_CUE_TITLE_RE = re.compile(r'^\s*TITLE\s+"(.*)"\s*$', re.IGNORECASE)
_CUE_PERFORMER_RE = re.compile(r'^\s*PERFORMER\s+"(.*)"\s*$', re.IGNORECASE)
_CUE_INDEX01_RE = re.compile(r"^\s*INDEX\s+01\s+(\d+):(\d{2}):(\d{2})\s*$", re.IGNORECASE)
_TRACK_INT_RE = re.compile(r"^\s*(\d+)")
_SERIES_HASH_RE = re.compile(r"^(.*?)\s*#\s*(\d+(?:\.\d+)?)\s*$")
_YEAR_SUFFIX_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


@dataclass
class TrackTagInfo:
    """Normalized metadata and track numbers read from a single local audio file."""

    disc_number: Optional[int] = None
    track_number: Optional[int] = None
    title: Optional[str] = None
    album: Optional[str] = None
    metadata: AudiobookMetadata = field(default_factory=lambda: AudiobookMetadata(""))
    cover: Optional[Cover] = None


def _parse_int_prefix(raw: Any) -> Optional[int]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    match = _TRACK_INT_RE.match(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_series_order(raw: Any) -> Optional[int]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return int(num) if num.is_integer() else None


def _parse_language(raw: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return pycountry.languages.lookup(text)
    except LookupError:
        return None


def _clean_text_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raw_items = [values.decode("utf-8", "replace") if isinstance(values, bytes) else values]
    elif isinstance(values, Sequence):
        raw_items = [
            v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            for v in values
        ]
    else:
        raw_items = [str(values)]
    out: List[str] = []
    for item in raw_items:
        for part in re.split(r"\s*;\s*", item.strip()):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _first_text(values: Any) -> Optional[str]:
    items = _clean_text_list(values)
    return items[0] if items else None


def _cover_from_bytes_and_mime(data: bytes, mime: Optional[str] = None) -> Optional[Cover]:
    if not data:
        return None
    ext = _EXT_TO_MIME_REVERSE.get((mime or "").lower().strip(), "")
    if not ext:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            ext = "jpg"
    return Cover(image=data, extension=ext)


def _read_id3_tags(filepath: str) -> TrackTagInfo:
    info = TrackTagInfo()
    try:
        tags = ID3(filepath)
    except Exception:
        return info

    def _frame_texts(frame_id: str) -> List[str]:
        out: List[str] = []
        for frame in tags.getall(frame_id):
            text_attr = getattr(frame, "text", None)
            if text_attr:
                out.extend(str(t) for t in text_attr if str(t).strip())
        return out

    txxx_map: Dict[str, List[str]] = {}
    for frame in tags.getall("TXXX"):
        desc = str(getattr(frame, "desc", "") or "").strip().lower()
        if desc:
            vals = [str(t) for t in getattr(frame, "text", []) if str(t).strip()]
            if vals:
                txxx_map.setdefault(desc, []).extend(vals)

    info.track_number = _parse_int_prefix(_first_text(_frame_texts("TRCK")))
    info.disc_number = _parse_int_prefix(_first_text(_frame_texts("TPOS")))
    info.title = _first_text(_frame_texts("TIT2"))
    info.album = _first_text(_frame_texts("TALB"))

    meta = info.metadata
    meta.title = info.title or ""
    authors = _clean_text_list(_frame_texts("TPE2") or _frame_texts("TPE1"))
    if not authors:
        authors = _clean_text_list(_frame_texts("TPE1"))
    meta.add_authors(authors)
    narrators = _clean_text_list(_frame_texts("TCOM") + _frame_texts("TPE3"))
    meta.add_narrators(narrators)
    meta.add_genres(_clean_text_list(_frame_texts("TCON")))
    meta.publisher = _first_text(_frame_texts("TPUB"))
    comm_texts = _frame_texts("COMM")
    if comm_texts:
        meta.description = comm_texts[0]

    series_vals = txxx_map.get("series") or _frame_texts("TIT1")
    if series_vals:
        meta.series = series_vals[0]
    series_order_raw = _first_text(txxx_map.get("series-part") or txxx_map.get("series_order"))
    if series_order_raw is not None:
        meta.series_order = _parse_series_order(series_order_raw)

    meta.isbn = _first_text(txxx_map.get("isbn"))
    meta.asin = _first_text(txxx_map.get("asin"))
    meta.source = _first_text(txxx_map.get("source"))

    wcom_frames = tags.getall("WCOM")
    if wcom_frames:
        url = str(getattr(wcom_frames[0], "url", "") or "").strip()
        if url:
            meta.scrape_url = url

    date_raw = _first_text(
        _frame_texts("TDOR") or _frame_texts("TDRC") or _frame_texts("TYER") or _frame_texts("TORY")
    )
    if date_raw:
        meta.release_date = parse_release_date(date_raw)

    lang_raw = _first_text(_frame_texts("TLAN"))
    if lang_raw:
        meta.language = _parse_language(lang_raw)

    apics = tags.getall("APIC")
    if apics:
        chosen = next((a for a in apics if getattr(a, "type", None) == 3), apics[0])
        info.cover = _cover_from_bytes_and_mime(getattr(chosen, "data", b""), getattr(chosen, "mime", None))
    return info


def _decode_mp4_freeform(values: Any) -> Optional[str]:
    if not values:
        return None
    first = values[0]
    if isinstance(first, bytes):
        return first.decode("utf-8", "replace").strip() or None
    return str(first).strip() or None


def _read_mp4_tags(filepath: str) -> TrackTagInfo:
    info = TrackTagInfo()
    try:
        audio = MP4(filepath)
    except Exception:
        return info
    tags = audio.tags
    if not tags:
        return info

    trkn = tags.get("trkn")
    if trkn and isinstance(trkn[0], (tuple, list)) and trkn[0]:
        info.track_number = _parse_int_prefix(trkn[0][0])
    disk = tags.get("disk")
    if disk and isinstance(disk[0], (tuple, list)) and disk[0]:
        info.disc_number = _parse_int_prefix(disk[0][0])

    info.title = _first_text(tags.get("\xa9nam"))
    info.album = _first_text(tags.get("\xa9alb"))

    meta = info.metadata
    meta.title = info.title or ""
    authors = _clean_text_list(tags.get("\xa9ART") or tags.get("aART"))
    meta.add_authors(authors)
    narrators = _clean_text_list(tags.get("\xa9nrt") or tags.get("\xa9wrt"))
    meta.add_narrators(narrators)
    meta.add_genres(_clean_text_list(tags.get("\xa9gen")))
    meta.publisher = _first_text(tags.get("\xa9pub") or tags.get("cprt"))
    meta.description = _first_text(tags.get("ldes") or tags.get("desc") or tags.get("\xa9cmt"))

    freeform_ci: Dict[str, Any] = {}
    for k, v in tags.items():
        if isinstance(k, str) and k.lower().startswith("----:com.apple.itunes:"):
            freeform_ci[k.split(":", 2)[2].lower()] = v

    series_val = _decode_mp4_freeform(freeform_ci.get("series")) or _first_text(tags.get("\xa9grp"))
    if series_val:
        meta.series = series_val
    series_part = _decode_mp4_freeform(freeform_ci.get("series-part") or freeform_ci.get("series_order"))
    if series_part is not None:
        meta.series_order = _parse_series_order(series_part)

    meta.isbn = _decode_mp4_freeform(freeform_ci.get("isbn"))
    meta.asin = _decode_mp4_freeform(freeform_ci.get("asin"))
    meta.source = _decode_mp4_freeform(freeform_ci.get("source"))
    meta.scrape_url = _decode_mp4_freeform(freeform_ci.get("url") or freeform_ci.get("scrape_url"))

    date_raw = _first_text(tags.get("\xa9day"))
    if date_raw:
        meta.release_date = parse_release_date(date_raw)

    lang_raw = _decode_mp4_freeform(freeform_ci.get("language"))
    if lang_raw:
        meta.language = _parse_language(lang_raw)

    covr = tags.get("covr")
    if covr:
        first_cover = covr[0]
        fmt = getattr(first_cover, "imageformat", None)
        mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
        info.cover = _cover_from_bytes_and_mime(bytes(first_cover), mime)
    return info


def _read_vorbis_tags(filepath: str) -> TrackTagInfo:
    info = TrackTagInfo()
    try:
        audio = MutagenFile(filepath)
    except Exception:
        return info
    if audio is None:
        return info
    tags = getattr(audio, "tags", None)
    tag_ci: Dict[str, List[str]] = {}
    if tags:
        for k, v in tags.items():
            key_lower = str(k).lower()
            tag_ci[key_lower] = _clean_text_list(v)

    info.track_number = _parse_int_prefix(_first_text(tag_ci.get("tracknumber")))
    info.disc_number = _parse_int_prefix(_first_text(tag_ci.get("discnumber")))
    info.title = _first_text(tag_ci.get("title"))
    info.album = _first_text(tag_ci.get("album"))

    meta = info.metadata
    meta.title = info.title or ""
    meta.add_authors(_clean_text_list(tag_ci.get("albumartist") or tag_ci.get("artist")))
    meta.add_narrators(_clean_text_list(tag_ci.get("narrator") or tag_ci.get("composer")))
    meta.add_genres(_clean_text_list(tag_ci.get("genre")))
    meta.publisher = _first_text(tag_ci.get("publisher") or tag_ci.get("organization"))
    meta.description = _first_text(tag_ci.get("description") or tag_ci.get("comment"))
    meta.series = _first_text(tag_ci.get("series"))
    meta.series_order = _parse_series_order(_first_text(tag_ci.get("series-part") or tag_ci.get("series_order")))
    meta.isbn = _first_text(tag_ci.get("isbn"))
    meta.asin = _first_text(tag_ci.get("asin"))
    meta.source = _first_text(tag_ci.get("source"))
    meta.scrape_url = _first_text(tag_ci.get("wwwaudiofile") or tag_ci.get("url"))

    date_raw = _first_text(tag_ci.get("date") or tag_ci.get("year"))
    if date_raw:
        meta.release_date = parse_release_date(date_raw)

    lang_raw = _first_text(tag_ci.get("language"))
    if lang_raw:
        meta.language = _parse_language(lang_raw)

    if isinstance(audio, FLAC) and getattr(audio, "pictures", None):
        pics = audio.pictures
        chosen = next((p for p in pics if getattr(p, "type", None) == 3), pics[0])
        info.cover = _cover_from_bytes_and_mime(chosen.data, getattr(chosen, "mime", None))
    elif tag_ci.get("metadata_block_picture"):
        try:
            raw = base64.b64decode(tag_ci["metadata_block_picture"][0])
            pic = Picture(raw)
            info.cover = _cover_from_bytes_and_mime(pic.data, pic.mime)
        except Exception:
            pass
    return info


def read_track_tags(filepath: str) -> TrackTagInfo:
    """Read normalized embedded metadata, track numbering, and cover art from `filepath`."""
    ext = os.path.splitext(filepath)[1].lstrip(".").lower()
    if ext == "mp3":
        return _read_id3_tags(filepath)
    if ext in ("mp4", "m4a", "m4b", "m4p", "m4r"):
        return _read_mp4_tags(filepath)
    if ext in ("flac", "ogg", "opus", "oga"):
        return _read_vorbis_tags(filepath)
    return _read_vorbis_tags(filepath)


def read_cover_file(filepath: str) -> Optional[Cover]:
    """Load an external cover image file from disk into a `Cover` object."""
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    ext = os.path.splitext(filepath)[1].lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    if not ext:
        return _cover_from_bytes_and_mime(data)
    return Cover(image=data, extension=ext)


def find_sidecar_cover(directory: str) -> Optional[Cover]:
    """Find a folder-level cover image in `directory`, if present."""
    if not os.path.isdir(directory):
        return None
    try:
        entries = {name.lower(): os.path.join(directory, name) for name in os.listdir(directory)}
    except OSError:
        return None
    for candidate in _AUDIO_COVER_CANDIDATES:
        path = entries.get(candidate)
        if path and os.path.isfile(path):
            cover = read_cover_file(path)
            if cover is not None:
                return cover
    return None


def _parse_metadata_json(path: str) -> Tuple[Optional[AudiobookMetadata], List[Chapter]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None, []
    if not isinstance(payload, dict):
        return None, []

    meta = AudiobookMetadata(str(payload.get("title") or "").strip())
    meta.add_authors(_clean_text_list(payload.get("authors") or payload.get("author")))
    meta.add_narrators(_clean_text_list(payload.get("narrators") or payload.get("narrator")))
    meta.add_genres(_clean_text_list(payload.get("genres") or payload.get("genre")))
    meta.description = str(payload.get("description") or "").strip() or None
    meta.publisher = str(payload.get("publisher") or "").strip() or None
    meta.isbn = str(payload.get("isbn") or "").strip() or None
    meta.asin = str(payload.get("asin") or "").strip() or None
    meta.scrape_url = str(payload.get("scrape_url") or "").strip() or None
    meta.source = str(payload.get("source") or "").strip() or None

    raw_series = payload.get("series")
    series_text: Optional[str] = None
    if isinstance(raw_series, list) and raw_series:
        first_series = raw_series[0]
        if isinstance(first_series, dict):
            series_text = str(first_series.get("name") or "").strip()
            if first_series.get("sequence") is not None and meta.series_order is None:
                meta.series_order = _parse_series_order(first_series.get("sequence"))
        else:
            series_text = str(first_series or "").strip()
    elif isinstance(raw_series, dict):
        series_text = str(raw_series.get("name") or "").strip()
        if raw_series.get("sequence") is not None and meta.series_order is None:
            meta.series_order = _parse_series_order(raw_series.get("sequence"))
    elif isinstance(raw_series, str):
        series_text = raw_series.strip()
    if series_text:
        m = _SERIES_HASH_RE.match(series_text)
        if m and m.group(1).strip():
            meta.series = m.group(1).strip()
            meta.series_order = _parse_series_order(m.group(2))
        else:
            meta.series = series_text
    if payload.get("series_order") is not None and meta.series_order is None:
        meta.series_order = _parse_series_order(payload.get("series_order"))

    rel_date_raw = payload.get("release_date") or payload.get("publishedDate") or payload.get("publishedYear")
    if rel_date_raw:
        meta.release_date = parse_release_date(str(rel_date_raw))
    if payload.get("language"):
        meta.language = _parse_language(payload["language"])

    chapters: List[Chapter] = []
    raw_chapters = payload.get("chapters")
    if isinstance(raw_chapters, list):
        for item in raw_chapters:
            if not isinstance(item, dict):
                continue
            ch_title = str(item.get("title") or "").strip()
            start_ms = item.get("start_ms")
            if start_ms is None:
                sec_val = item.get("start_seconds")
                if sec_val is None:
                    sec_val = item.get("start")
                if sec_val is not None:
                    try:
                        start_ms = round(float(sec_val) * 1000)
                    except (TypeError, ValueError):
                        start_ms = None
            if ch_title and start_ms is not None:
                try:
                    chapters.append(Chapter(start=max(0, int(start_ms)), title=ch_title))
                except (TypeError, ValueError):
                    continue
    return meta, chapters


def _local_tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_opf_file(path: str, base_dir: str) -> Tuple[Optional[AudiobookMetadata], Optional[Cover]]:
    try:
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        return None, None
    root = tree.getroot()
    meta = AudiobookMetadata("")
    cover_href: Optional[str] = None
    cover_id: Optional[str] = None
    manifest_items: Dict[str, str] = {}

    for elem in root.iter():
        lname = _local_tag_name(elem.tag)
        text = (elem.text or "").strip()
        if lname == "title" and text and not meta.title:
            meta.title = text
        elif lname == "creator" and text:
            role = ""
            for ak, av in elem.attrib.items():
                if _local_tag_name(ak) == "role":
                    role = av.strip().lower()
                    break
            if role == "nrt":
                if text not in meta.narrators:
                    meta.add_narrator(text)
            else:
                if text not in meta.authors:
                    meta.add_author(text)
        elif lname == "description" and text and not meta.description:
            meta.description = text
        elif lname == "publisher" and text and not meta.publisher:
            meta.publisher = text
        elif lname == "date" and text and meta.release_date is None:
            meta.release_date = parse_release_date(text)
        elif lname == "language" and text and meta.language is None:
            meta.language = _parse_language(text)
        elif lname == "subject" and text:
            if text not in meta.genres:
                meta.add_genre(text)
        elif lname == "identifier" and text:
            scheme = ""
            for ak, av in elem.attrib.items():
                if _local_tag_name(ak) == "scheme":
                    scheme = av.strip().upper()
                    break
            if scheme == "ISBN" and not meta.isbn:
                meta.isbn = text
            elif scheme == "ASIN" and not meta.asin:
                meta.asin = text
            elif scheme == "URL" and not meta.scrape_url:
                meta.scrape_url = text
        elif lname == "meta":
            name = (elem.attrib.get("name") or "").strip().lower()
            content = (elem.attrib.get("content") or "").strip()
            if name == "calibre:series" and content:
                meta.series = content
            elif name == "calibre:series_index" and content:
                meta.series_order = _parse_series_order(content)
            elif name == "cover" and content:
                cover_id = content
        elif lname == "item":
            item_id = (elem.attrib.get("id") or "").strip()
            href = (elem.attrib.get("href") or "").strip()
            if item_id and href:
                manifest_items[item_id] = href
        elif lname == "reference":
            if (elem.attrib.get("type") or "").strip().lower() == "cover":
                href = (elem.attrib.get("href") or "").strip()
                if href:
                    cover_href = href

    resolved_cover_href = manifest_items.get(cover_id or "") or cover_href
    cover: Optional[Cover] = None
    if resolved_cover_href:
        cover_path = os.path.join(base_dir, resolved_cover_href)
        cover = read_cover_file(cover_path)
    return meta, cover


def parse_cue_chapters(path: str) -> List[Chapter]:
    """Parse chapters (`TRACK nn AUDIO` / `TITLE` / `INDEX 01 MM:SS:FF`) from a `.cue` sheet."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    chapters: List[Chapter] = []
    in_track = False
    pending_title: Optional[str] = None
    for line in lines:
        if _CUE_TRACK_RE.match(line):
            in_track = True
            pending_title = None
            continue
        if not in_track:
            continue
        m_title = _CUE_TITLE_RE.match(line)
        if m_title:
            pending_title = m_title.group(1).strip()
            continue
        m_idx = _CUE_INDEX01_RE.match(line)
        if m_idx:
            mins = int(m_idx.group(1))
            secs = int(m_idx.group(2))
            frames = int(m_idx.group(3))
            start_ms = ((mins * 60 + secs) * 1000) + ((frames * 1000) // 75)
            title = pending_title or f"Chapter {len(chapters) + 1}"
            chapters.append(Chapter(start=start_ms, title=title))
            pending_title = None
    return chapters


def merge_metadata(primary: AudiobookMetadata, fallback: AudiobookMetadata) -> AudiobookMetadata:
    """Fill missing fields on `primary` from `fallback`, mutating and returning `primary`."""
    if not primary.title and fallback.title:
        primary.title = fallback.title
    if not primary.authors and fallback.authors:
        primary.add_authors(fallback.authors)
    if not primary.narrators and fallback.narrators:
        primary.add_narrators(fallback.narrators)
    if not primary.genres and fallback.genres:
        primary.add_genres(fallback.genres)
    if not primary.series and fallback.series:
        primary.series = fallback.series
    if primary.series_order is None and fallback.series_order is not None:
        primary.series_order = fallback.series_order
    if primary.language is None and fallback.language is not None:
        primary.language = fallback.language
    if not primary.description and fallback.description:
        primary.description = fallback.description
    if not primary.isbn and fallback.isbn:
        primary.isbn = fallback.isbn
    if not primary.asin and fallback.asin:
        primary.asin = fallback.asin
    if not primary.publisher and fallback.publisher:
        primary.publisher = fallback.publisher
    if primary.release_date is None and fallback.release_date is not None:
        primary.release_date = fallback.release_date
    if not primary.scrape_url and fallback.scrape_url:
        primary.scrape_url = fallback.scrape_url
    if not primary.source and fallback.source:
        primary.source = fallback.source
    return primary


def read_sidecar_metadata(
    directory: str,
    audio_stem: Optional[str] = None,
) -> Tuple[Optional[AudiobookMetadata], Optional[Cover], List[Chapter]]:
    """Inspect `directory` for `metadata.json`, `.opf`, cover art, and `.cue` chapters."""
    if not os.path.isdir(directory):
        return None, None, []
    try:
        filenames = sorted(os.listdir(directory))
    except OSError:
        return None, None, []

    cover = find_sidecar_cover(directory)
    meta = AudiobookMetadata("")
    chapters: List[Chapter] = []

    json_candidates: List[str] = []
    opf_candidates: List[str] = []
    cue_candidates: List[str] = []
    for name in filenames:
        low = name.lower()
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if low in ("metadata.json", "audiobook.json") or low.endswith(".metadata.json") or (
            audio_stem and low == f"{audio_stem.lower()}.json"
        ) or (audio_stem and low.startswith(f"{audio_stem.lower()}.") and low.endswith(".json")):
            json_candidates.append(full)
        elif low == "metadata.opf" or low.endswith(".opf"):
            opf_candidates.append(full)
        elif low == "chapters.cue" or (audio_stem and low == f"{audio_stem.lower()}.cue") or low.endswith(".cue"):
            cue_candidates.append(full)

    # Prefer exact `metadata.json` first, then other JSON sidecars.
    json_candidates.sort(key=lambda p: (os.path.basename(p).lower() != "metadata.json", os.path.basename(p).lower()))
    for jp in json_candidates:
        parsed_meta, parsed_chapters = _parse_metadata_json(jp)
        if parsed_meta is not None:
            merge_metadata(meta, parsed_meta)
        if parsed_chapters and not chapters:
            chapters = parsed_chapters

    opf_candidates.sort(key=lambda p: (os.path.basename(p).lower() != "metadata.opf", os.path.basename(p).lower()))
    for op in opf_candidates:
        opf_meta, opf_cover = _parse_opf_file(op, directory)
        if opf_meta is not None:
            merge_metadata(meta, opf_meta)
        if cover is None and opf_cover is not None:
            cover = opf_cover

    if not chapters and cue_candidates:
        if audio_stem:
            cue_candidates.sort(
                key=lambda p: (
                    os.path.basename(p).lower() != f"{audio_stem.lower()}.cue",
                    os.path.basename(p).lower() != "chapters.cue",
                    os.path.basename(p).lower(),
                )
            )
        for cp in cue_candidates:
            parsed_cue = parse_cue_chapters(cp)
            if parsed_cue:
                chapters = parsed_cue
                break

    has_meta = bool(
        meta.title
        or meta.authors
        or meta.narrators
        or meta.series
        or meta.description
        or meta.isbn
        or meta.asin
        or meta.publisher
        or meta.genres
    )
    return (meta if has_meta else None), cover, chapters


def infer_metadata_from_path(path: str) -> AudiobookMetadata:
    """Infer fallback `title`, optional `authors`, and `release_date` year from folder or file name."""
    cleaned = os.path.abspath(path)
    base = os.path.basename(cleaned)
    if os.path.isfile(cleaned):
        base = os.path.splitext(base)[0]
    base = re.sub(r"\s+", " ", base).strip()
    meta = AudiobookMetadata("")
    year_match = _YEAR_SUFFIX_RE.match(base)
    if year_match and year_match.group(1).strip():
        base = year_match.group(1).strip()
        try:
            meta.release_date = date(int(year_match.group(2)), 1, 1)
        except ValueError:
            pass
    if " - " in base:
        left, right = base.split(" - ", 1)
        left = left.strip()
        right = right.strip()
        if left and right:
            meta.add_author(left)
            meta.title = right
            return meta
    meta.title = base or "Audiobook"
    return meta

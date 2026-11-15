import os
from datetime import date
from typing import Dict, List, Optional, Tuple

from audiobookdl import AudiobookMetadata, Cover
from ._cover import normalize_cover_image
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

MP4_EXTENSIONS = ("mp4", "m4a", "m4p", "m4b", "m4r", "m4v")

# iTunes Media Kind atom value for Audiobooks
MP4_MEDIA_KIND_AUDIOBOOK = 2

MP4_TEXT_ATOMS: Dict[str, str] = {
    "authors": "\xa9ART",
    "narrators": "\xa9nrt",
    "publisher": "\xa9pub",
    "series": "\xa9alb",
    "title": "\xa9nam",
    "genres": "\xa9gen",
}

MP4_FREEFORM_NAMES: Dict[str, str] = {
    "scrape_url": "URL",
    "isbn": "ISBN",
    "asin": "ASIN",
}

MP4_COVER_FORMATS = {
    "jpg": MP4Cover.FORMAT_JPEG,
    "jpeg": MP4Cover.FORMAT_JPEG,
    "png": MP4Cover.FORMAT_PNG,
}


def is_mp4_file(filepath: str) -> bool:
    """Returns true if `filepath` points to an mp4 file"""
    ext = os.path.splitext(filepath)[1][1:].lower()
    return ext in MP4_EXTENSIONS


def _freeform_atom(name: str, value: str) -> Tuple[str, List[MP4FreeForm]]:
    return (
        f"----:com.apple.iTunes:{name}",
        [MP4FreeForm(value.encode("utf-8"))],
    )


def _build_mp4_cover(cover: Cover) -> Optional[MP4Cover]:
    image_data, ext = normalize_cover_image(cover)
    if ext not in MP4_COVER_FORMATS:
        return None
    return MP4Cover(image_data, imageformat=MP4_COVER_FORMATS[ext])


def add_mp4_metadata(
    filepath: str,
    metadata: AudiobookMetadata,
    cover: Optional[Cover] = None,
) -> bool:
    """Add mp4 metadata tags (and optional cover art) in a single save pass."""
    mp4_cover = _build_mp4_cover(cover) if cover is not None else None
    audio = MP4(filepath)
    audio["stik"] = [MP4_MEDIA_KIND_AUDIOBOOK]
    for key, value in metadata.all_properties(allow_duplicate_keys=None):
        if key == "release_date":
            release_date: date = value
            audio["\xa9day"] = [release_date.strftime("%Y-%m-%d")]
            audio["yrrc"] = [str(release_date.year)]
        elif key == "description":
            text = value if isinstance(value, list) else [str(value)]
            audio["desc"] = text
            audio["ldes"] = text
        elif key == "language":
            atom, val = _freeform_atom("Language", value.alpha_3)
            audio[atom] = val
        elif key == "series_order":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535:
                audio["trkn"] = [(value, 0)]
        elif key in MP4_TEXT_ATOMS:
            audio[MP4_TEXT_ATOMS[key]] = value if isinstance(value, list) else [str(value)]
        else:
            freeform_name = MP4_FREEFORM_NAMES.get(key, key.capitalize())
            atom, val = _freeform_atom(freeform_name, str(value))
            audio[atom] = val
    if mp4_cover is not None:
        audio["covr"] = [mp4_cover]
    audio.save()
    return mp4_cover is not None


def embed_mp4_cover(filepath: str, cover: Cover) -> None:
    mp4_cover = _build_mp4_cover(cover)
    if mp4_cover is None:
        return
    audio = MP4(filepath)
    audio["covr"] = [mp4_cover]
    audio.save()

import base64
from io import BytesIO
import os
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from PIL import Image

from audiobookdl import AudiobookMetadata, Cover
from ._cover import normalize_cover_image

VORBIS_EXTENSIONS = ("flac", "ogg", "opus", "oga")

_EXTENSION_TO_MIMETYPE = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
}


def is_vorbis_file(filepath: str) -> bool:
    """Return True when `filepath` uses VorbisComment metadata (FLAC, Ogg, Opus)."""
    ext = os.path.splitext(filepath)[1][1:].lower()
    return ext in VORBIS_EXTENSIONS


def _as_tag_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    text = str(value)
    return [text] if text else []


def _build_vorbis_picture(cover: Cover) -> Picture | None:
    image_data, ext = normalize_cover_image(cover)
    mimetype = _EXTENSION_TO_MIMETYPE.get(ext)
    if not mimetype:
        return None
    picture = Picture()
    picture.type = 3
    picture.mime = mimetype
    picture.desc = "Cover"
    picture.data = image_data
    try:
        with Image.open(BytesIO(image_data)) as img:
            picture.width, picture.height = img.size
            picture.depth = 32 if img.mode in ("RGBA", "LA", "PA") else 24
    except Exception:
        picture.width = 0
        picture.height = 0
        picture.depth = 24
    return picture


def _attach_vorbis_picture(audio: Any, picture: Picture) -> None:
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        audio.add_picture(picture)
    else:
        encoded = base64.b64encode(picture.write()).decode("ascii")
        audio.tags["METADATA_BLOCK_PICTURE"] = [encoded]


def add_vorbis_metadata(
    filepath: str,
    metadata: AudiobookMetadata,
    cover: Cover | None = None,
) -> bool:
    """Write VorbisComment metadata tags (and optional cover art) in a single save pass."""
    audio = MutagenFile(filepath)
    if audio is None:
        return False
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    for key, value in metadata.all_properties(allow_duplicate_keys=None):
        if key == "release_date":
            iso_date = value.strftime("%Y-%m-%d")
            tags["DATE"] = [iso_date]
            tags["YEAR"] = [value.strftime("%Y")]
        elif key == "language":
            tags["LANGUAGE"] = [value.alpha_3]
        elif key == "authors":
            vals = _as_tag_values(value)
            if vals:
                tags["ARTIST"] = vals
                tags["ALBUMARTIST"] = vals
        elif key == "narrators":
            vals = _as_tag_values(value)
            if vals:
                tags["COMPOSER"] = vals
                tags["NARRATOR"] = vals
        elif key == "series":
            vals = _as_tag_values(value)
            if vals:
                tags["ALBUM"] = vals
                tags["SERIES"] = vals
        elif key == "series_order":
            tags["TRACKNUMBER"] = [str(value)]
            tags["SERIES-PART"] = [str(value)]
        elif key == "genres":
            vals = _as_tag_values(value)
            if vals:
                tags["GENRE"] = vals
        elif key == "publisher":
            vals = _as_tag_values(value)
            if vals:
                tags["PUBLISHER"] = vals
                tags["ORGANIZATION"] = vals
        elif key == "description":
            vals = _as_tag_values(value)
            if vals:
                tags["DESCRIPTION"] = vals
                tags["COMMENT"] = vals
        elif key == "scrape_url":
            vals = _as_tag_values(value)
            if vals:
                tags["WWWAUDIOFILE"] = vals
        elif key == "title":
            vals = _as_tag_values(value)
            if vals:
                tags["TITLE"] = vals
        elif key == "isbn":
            vals = _as_tag_values(value)
            if vals:
                tags["ISBN"] = vals
        else:
            vals = _as_tag_values(value)
            if vals:
                tags[key.upper()] = vals

    picture = _build_vorbis_picture(cover) if cover is not None else None
    if picture is not None:
        _attach_vorbis_picture(audio, picture)
    audio.save()
    return picture is not None


def embed_vorbis_cover(filepath: str, cover: Cover) -> None:
    """Embed `cover` as a FLAC Picture block or Ogg `METADATA_BLOCK_PICTURE` tag."""
    picture = _build_vorbis_picture(cover)
    if picture is None:
        return
    audio = MutagenFile(filepath)
    if audio is None:
        return
    _attach_vorbis_picture(audio, picture)
    audio.save()

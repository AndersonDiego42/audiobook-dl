import fnmatch
import os
from audiobookdl import logging, Chapter, AudiobookMetadata, Cover
from audiobookdl.utils.audiobook import TrackTagContext
from ._chapters import build_bounded_chapters
from ._cover import normalize_cover_image

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3, EasyID3KeyError
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, CHAP, COMM, TIT2, CTOC, CTOCFlags, WCOM, ID3NoHeaderError
from requests import utils

from typing import Any
from collections.abc import Sequence

def _comment_get(id3: Any, key: str) -> list[str]:
    comments = [
        str(t)
        for frame in id3.getall("COMM")
        for t in getattr(frame, "text", [])
        if str(t)
    ]
    if comments:
        return comments
    raise EasyID3KeyError(key)


def _comment_set(id3: Any, key: str, value: Sequence[str]) -> None:
    id3.delall("COMM")
    texts = [str(v) for v in value if str(v)]
    if texts:
        id3.add(COMM(encoding=3, lang="eng", desc="", text=texts))


def _comment_delete(id3: Any, key: str) -> None:
    id3.delall("COMM")


EasyID3.RegisterKey("comment", _comment_get, _comment_set, _comment_delete)
EasyID3.RegisterTextKey("year", "TYER")
for _easy_key, _txxx_desc in (
    ("isbn", "ISBN"),
    ("asin", "ASIN"),
    ("source", "Source"),
    ("series_tag", "SERIES"),
    ("series_part", "SERIES-PART"),
):
    EasyID3.RegisterTXXXKey(_easy_key, _txxx_desc)

def commercialurl_get(id3: Any, key: str) -> list[str]:
    urls = [frame.url for frame in id3.getall("WCOM")]
    if urls:
        return urls
    else:
        raise EasyID3KeyError(key)

def commercialurl_set(id3: Any, key: str, value: Sequence[str]) -> None:
    id3.delall("WCOM")
    for v in value:
        # scrape urls might contain characters in the unicode range
        # for audiobooks with non-english titles
        encoded_url = utils.requote_uri(v)
        id3.add(WCOM(url=encoded_url))

def commercialurl_delete(id3: Any, key: str) -> None:
    id3.delall("WCOM")

EasyID3.RegisterKey("commercialurl", commercialurl_get, commercialurl_set, commercialurl_delete)

# Conversion table between metadata names and ID3 tags
ID3_CONVERT = {
    "authors": "artist",
    "series": "album",
    "title": "title",
    "publisher": "organization", # TPUB
    "description": "comment", # COMM
    "genres": "genre", # TCON
    "scrape_url": "commercialurl", # WCOM
}

# List of file formats that use ID3 metadata
ID3_FORMATS = ["mp3"]

EXTENSION_TO_MIMETYPE = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
}

def is_id3_file(filepath: str) -> bool:
    """Returns true if `filepath` points to an id3 file"""
    ext = os.path.splitext(filepath)[1][1:].lower()
    return ext in ID3_FORMATS


def _set_easy_id3_tag(id3_tags: ID3, key: str, value: str | Sequence[str]) -> None:
    """Apply an `EasyID3`-mapped tag directly onto an `ID3` container."""
    values = [value] if isinstance(value, str) else [str(v) for v in value]
    lower_key = key.lower()
    setter = EasyID3.Set.get(lower_key)
    if setter is None:
        for pattern, candidate in EasyID3.Set.items():
            if "*" in pattern and fnmatch.fnmatchcase(lower_key, pattern):
                setter = candidate
                break
    if setter is None:
        setter = EasyID3.SetFallback
    if setter is None:
        raise EasyID3KeyError(f"{key!r} is not a valid EasyID3 key")
    setter(id3_tags, key, values)


def add_id3_metadata(
    filepath: str,
    metadata: AudiobookMetadata,
    cover: Cover | None = None,
    chapters: Sequence[Chapter] | None = None,
    track_context: TrackTagContext | None = None,
) -> bool:
    """Add ID3 metadata tags (plus optional chapters and cover art) in a single save pass.

    Returns ``True`` when ``cover`` was embedded inline.
    """
    audio = MP3(filepath, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    id3_tags = audio.tags
    assert id3_tags is not None

    # Adding tags
    for key, value in metadata.all_properties(allow_duplicate_keys=None):
        if key == "release_date":
            iso_date = value.strftime("%Y-%m-%d")
            _set_easy_id3_tag(id3_tags, "originaldate", iso_date)
            _set_easy_id3_tag(id3_tags, "date", iso_date)
            _set_easy_id3_tag(id3_tags, "year", value.strftime("%Y"))
        elif key == "language":
            _set_easy_id3_tag(id3_tags, "language", value.alpha_3)
        elif key == "narrators":
            _set_easy_id3_tag(id3_tags, "composer", value)
            _set_easy_id3_tag(id3_tags, "performer", value)
        elif key == "series":
            _set_easy_id3_tag(id3_tags, "series_tag", value)
            if track_context is None:
                _set_easy_id3_tag(id3_tags, "album", value)
        elif key == "series_order":
            _set_easy_id3_tag(id3_tags, "series_part", str(value))
            if track_context is None:
                _set_easy_id3_tag(id3_tags, "tracknumber", str(value))
        elif key in ID3_CONVERT:
            _set_easy_id3_tag(id3_tags, ID3_CONVERT[key], value)
        elif key in EasyID3.valid_keys.keys():
            _set_easy_id3_tag(id3_tags, key, value)
    if track_context is not None:
        _set_easy_id3_tag(id3_tags, "title", track_context.title)
        album_name = track_context.album or metadata.title
        if album_name:
            _set_easy_id3_tag(id3_tags, "album", album_name)
        _set_easy_id3_tag(id3_tags, "tracknumber", f"{track_context.track_number}/{track_context.total_tracks}")
    elif not metadata.series and metadata.title:
        _set_easy_id3_tag(id3_tags, "album", metadata.title)

    if chapters:
        _apply_id3_chapters(id3_tags, filepath, chapters)
    cover_embedded = False
    if cover is not None:
        cover_embedded = _apply_id3_cover(id3_tags, cover)
    audio.save(v2_version=4)
    return cover_embedded


def _apply_id3_cover(audio: ID3, cover: Cover) -> bool:
    image_data, ext = normalize_cover_image(cover)
    mimetype = EXTENSION_TO_MIMETYPE.get(ext)
    if not mimetype:
        return False
    audio.delall("APIC")
    audio.add(APIC(type=3, data=image_data, mime=mimetype))
    return True


def _apply_id3_chapters(audio: ID3, filepath: str, chapters: Sequence[Chapter]) -> bool:
    if not chapters:
        return False
    bounded = build_bounded_chapters(filepath, chapters, mutagen_opener=MutagenFile)
    if not bounded:
        logging.debug(f"No valid chapters within audio duration for {filepath}; skipping ID3 chapters")
        return False
    audio.delall("CTOC")
    audio.delall("CHAP")
    child_ids = []
    for index, (start, end, title) in enumerate(bounded, start=1):
        child_ids.append(f"chp{index}")
        add_id3_chapter(
            audio,
            start=start,
            end=end,
            title=title,
            index=index,
        )
    audio.add(CTOC(
        element_id="toc",
        flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
        child_element_ids=child_ids,
    ))
    return True


def embed_id3_cover(filepath: str, cover: Cover) -> None:
    try:
        audio = ID3(filepath)
    except ID3NoHeaderError:
        audio = ID3()
    if _apply_id3_cover(audio, cover):
        audio.save(filepath)


def add_id3_chapter(audio: ID3, start: int, end: int, title: str, index: int) -> None:
    """Adds a single chapter to the given audio file"""
    audio.add(CHAP(
        element_id="chp"+str(index),
        start_time=int(start),
        end_time=int(end),
        sub_frames=[TIT2(text=[title])]
    ))


def add_id3_chapters(filepath: str, chapters: Sequence[Chapter]) -> None:
    """Adds chapters to the given audio file"""
    add_id3_chapters_and_cover(filepath, chapters=chapters, cover=None)


def add_id3_chapters_and_cover(
    filepath: str,
    chapters: Sequence[Chapter] | None = None,
    cover: Cover | None = None,
) -> bool:
    """Embed ID3 chapters and optional APIC cover art in a single `ID3.save()` pass.

    Returns ``True`` when cover art was embedded.
    """
    if not chapters and cover is None:
        return False
    try:
        audio = ID3(filepath)
    except ID3NoHeaderError:
        audio = ID3()
    dirty = False
    if chapters and _apply_id3_chapters(audio, filepath, chapters):
        dirty = True
    cover_embedded = False
    if cover is not None and _apply_id3_cover(audio, cover):
        dirty = True
        cover_embedded = True
    if dirty:
        audio.save(filepath)
    return cover_embedded

import os
from datetime import date
from audiobookdl import logging, Chapter, AudiobookMetadata, Cover
from ._chapters import build_bounded_chapters
from ._cover import normalize_cover_image

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3, EasyID3KeyError
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, CHAP, TIT2, CTOC, CTOCFlags, WCOM, ID3NoHeaderError
from requests import utils

from typing import Sequence

EasyID3.RegisterTextKey("comment", "COMM")
EasyID3.RegisterTextKey("year", "TYER")
EasyID3.RegisterTextKey("originalreleaseyear", "TORY")
EasyID3.RegisterTXXXKey("isbn", "ISBN")

def commercialurl_get(id3, key):
    urls = [frame.url for frame in id3.getall("WCOM")]
    if urls:
        return urls
    else:
        raise EasyID3KeyError(key)

def commercialurl_set(id3, key, value):
    id3.delall("WCOM")
    for v in value:
        # scrape urls might contain characters in the unicode range
        # for audiobooks with non-english titles
        encoded_url = utils.requote_uri(v)
        id3.add(WCOM(url=encoded_url))

def commercialurl_delete(id3, key):
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


def add_id3_metadata(filepath: str, metadata: AudiobookMetadata):
    """Add ID3 metadata tags to the given audio file"""
    audio = MP3(filepath, ID3=EasyID3)
    # Adding tags
    for key, value in metadata.all_properties(allow_duplicate_keys=None):
        if key == "release_date":
            iso_date = value.strftime("%Y-%m-%d")
            audio["originaldate"] = iso_date
            audio["date"] = iso_date
            audio["year"] = value.strftime("%Y")
        elif key == "language":
            audio["language"] = value.alpha_3
        elif key == "narrators":
            audio["composer"] = value
            audio["performer"] = value
        elif key == "series_order":
            audio["tracknumber"] = str(value)
        elif key in ID3_CONVERT:
            audio[ID3_CONVERT[key]] = value
        elif key in EasyID3.valid_keys.keys():
            audio[key] = value
    audio.save(v2_version=4)


def embed_id3_cover(filepath: str, cover: Cover):
    image_data, ext = normalize_cover_image(cover)
    mimetype = EXTENSION_TO_MIMETYPE[ext]
    try:
        audio = ID3(filepath)
    except ID3NoHeaderError:
        audio = ID3()
    audio.delall("APIC")
    audio.add(APIC(type=3, data=image_data, mime=mimetype))
    audio.save(filepath)


def add_id3_chapter(audio: ID3, start: int, end: int, title: str, index: int):
    """Adds a single chapter to the given audio file"""
    audio.add(CHAP(
        element_id=u"chp"+str(index),
        start_time=int(start),
        end_time=int(end),
        sub_frames=[TIT2(text=[title])]
    ))


def add_id3_chapters(filepath: str, chapters: Sequence[Chapter]):
    """Adds chapters to the given audio file"""
    if not chapters:
        return
    bounded = build_bounded_chapters(filepath, chapters, mutagen_opener=MutagenFile)
    if not bounded:
        logging.debug(f"No valid chapters within audio duration for {filepath}; skipping ID3 chapters")
        return
    try:
        audio = ID3(filepath)
    except ID3NoHeaderError:
        audio = ID3()
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
    audio.save(filepath)

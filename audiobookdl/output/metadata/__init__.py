from . import id3, mp4, ffmpeg, vorbis
from audiobookdl import logging, Chapter, AudiobookMetadata, Cover
from audiobookdl.utils import program_in_path
from audiobookdl.utils.audiobook import TrackTagContext

import os
import threading
from collections.abc import Sequence

def add_metadata(
    filepath: str,
    metadata: AudiobookMetadata,
    cover: Cover | None = None,
    track_context: TrackTagContext | None = None,
) -> bool:
    """Adds metadata (and optional cover art where supported inline) to `filepath`.

    Returns ``True`` if ``cover`` was embedded in the same save pass.
    """
    if id3.is_id3_file(filepath):
        id3.add_id3_metadata(filepath, metadata, track_context=track_context)
        return False
    elif mp4.is_mp4_file(filepath):
        return mp4.add_mp4_metadata(filepath, metadata, cover=cover, track_context=track_context)
    elif vorbis.is_vorbis_file(filepath):
        return vorbis.add_vorbis_metadata(filepath, metadata, cover=cover, track_context=track_context)
    else:
        logging.debug("Could not add any metadata")
        return False


def embed_cover(filepath: str, cover: Cover):
    """Embeds an image into the given audio file"""
    if id3.is_id3_file(filepath):
        id3.embed_id3_cover(filepath, cover)
    elif mp4.is_mp4_file(filepath):
        mp4.embed_mp4_cover(filepath, cover)
    elif vorbis.is_vorbis_file(filepath):
        vorbis.embed_vorbis_cover(filepath, cover)
    else:
        logging.debug("Could not embed cover")


def add_chapters(
    filepath: str,
    chapters: Sequence[Chapter],
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
    cover: Cover | None = None,
) -> bool:
    """Adds chapters (and optional ID3 cover art in one save) to `filepath`.

    Returns ``True`` if ``cover`` was embedded in the same pass.
    """
    if id3.is_id3_file(filepath):
        return id3.add_id3_chapters_and_cover(filepath, chapters=chapters, cover=cover)
    elif program_in_path("ffmpeg"):
        ffmpeg.add_chapters_ffmpeg(
            filepath,
            chapters,
            cancel_event=cancel_event,
            audio_bitrate=audio_bitrate,
        )
        return False
    else:
        if logging.is_debug_mode():
            logging.debug("Could not add chapters")
        else:
            filetype = os.path.splitext(filepath)[1][1:]
            logging.print_error_file("chapters_add", filetype=filetype)
        return False

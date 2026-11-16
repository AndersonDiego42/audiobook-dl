from audiobookdl import AudiobookMetadata, Chapter, utils, logging
from audiobookdl.utils.concurrency import run_cancellable_subprocess
from ._chapters import build_bounded_chapters
from mutagen import File as MutagenFile
import os
import threading
from collections.abc import Sequence


LOSSLESS_CONTAINERS = ("flac", "wav", "aiff", "alac", "wv")

def _escape_ffmetadata(text: str) -> str:
    """Escape special FFMETADATA1 characters (=, ;, #, \\, and newlines)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", "\\\n")
    )


def create_metadata_text(metadata: AudiobookMetadata | None) -> str:
    """Render global FFMETADATA1 tag lines (`key=value\\n`) for `metadata`."""
    if metadata is None:
        return ""
    lines: list[str] = []
    if metadata.title:
        lines.append(f"title={_escape_ffmetadata(metadata.title)}")
    if metadata.authors:
        lines.append(f"artist={_escape_ffmetadata(', '.join(metadata.authors))}")
    album = metadata.series or metadata.title
    if album:
        lines.append(f"album={_escape_ffmetadata(album)}")
    if metadata.narrators:
        lines.append(f"composer={_escape_ffmetadata(', '.join(metadata.narrators))}")
    if metadata.genres:
        lines.append(f"genre={_escape_ffmetadata(', '.join(metadata.genres))}")
    if metadata.release_date is not None:
        lines.append(f"date={_escape_ffmetadata(metadata.release_date.strftime('%Y-%m-%d'))}")
    if metadata.description:
        lines.append(f"comment={_escape_ffmetadata(metadata.description)}")
    return "".join(f"{line}\n" for line in lines)


def create_chapter_text(title: str, start: int, end: int) -> str:
    chapter_template = utils.read_asset_file("assets/ffmpeg_chapter_template.txt")
    return chapter_template.format(
        title = _escape_ffmetadata(title),
        start = start,
        end = end
    )


def create_tmp_chapter_file(filepath: str, chapters: Sequence[Chapter]) -> str:
    result = ";FFMETADATA1\n"
    if not chapters:
        return result
    for start, end, title in build_bounded_chapters(
        filepath,
        chapters,
        mutagen_opener=MutagenFile,
    ):
        result += create_chapter_text(title=title, start=start, end=end)
    return result

def _build_fallback_codec_args(
    filepath: str,
    audio_bitrate: str | None = None,
) -> list[str]:
    """Return FFmpeg audio encoding flags matching `filepath`'s container and `audio_bitrate`."""
    ext = os.path.splitext(filepath)[1].lstrip(".").lower()
    if not ext or ext in utils.MP4_CONTAINERS:
        bitrate = utils.resolve_audio_bitrate(filepath, audio_bitrate)
        return ["-c:a", "aac", "-b:a", bitrate]
    explicit_bitrate = utils.normalize_audio_bitrate(audio_bitrate)
    if explicit_bitrate is not None and ext not in LOSSLESS_CONTAINERS:
        return ["-b:a", explicit_bitrate]
    return []


def add_chapters_ffmpeg(
    filepath: str,
    chapters: Sequence[Chapter],
    cancel_event: threading.Event | None = None,
    audio_bitrate: str | None = None,
):
    if not chapters:
        return
    utils.normalize_audio_bitrate(audio_bitrate)
    ffmpeg = utils.find_executable("ffmpeg")
    if ffmpeg is None:
        logging.log("ffmpeg not found; skipping chapter embedding")
        return
    stem, ext = os.path.splitext(filepath)
    container_ext = ext.lstrip(".").lower()
    faststart_args = ["-movflags", "+faststart"] if container_ext in utils.MP4_CONTAINERS else []
    tmp_chapter_file = f"{filepath}.chapters.tmp.txt"
    tmp_media_file = f"{stem}.tmp{ext or '.mp4'}"
    capture = not logging.is_ffmpeg_output()
    try:
        with open(tmp_chapter_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(create_tmp_chapter_file(filepath, chapters))
        result = run_cancellable_subprocess(
            [ffmpeg, "-y",
             "-i", filepath,
             "-i", tmp_chapter_file,
             "-map_chapters", "1",
             "-c", "copy",
             "-map", "0",
             "-metadata:s:a:0", "title=",
             *faststart_args,
             tmp_media_file],
            capture_output=capture,
            cancel_event=cancel_event,
        )
        succeeded = (
            result.returncode == 0
            and os.path.exists(tmp_media_file)
            and os.path.getsize(tmp_media_file) > 0
        )
        if not succeeded:
            logging.debug("add_chapters_ffmpeg copy mode failed, retrying with re-encode")
            if os.path.exists(tmp_media_file):
                os.remove(tmp_media_file)
            fallback_result = run_cancellable_subprocess(
                [ffmpeg, "-y",
                 "-i", filepath,
                 "-i", tmp_chapter_file,
                 "-map_chapters", "1",
                 *_build_fallback_codec_args(filepath, audio_bitrate),
                 "-map", "0",
                 "-metadata:s:a:0", "title=",
                 *faststart_args,
                 tmp_media_file],
                capture_output=capture,
                cancel_event=cancel_event,
            )
            succeeded = (
                fallback_result.returncode == 0
                and os.path.exists(tmp_media_file)
                and os.path.getsize(tmp_media_file) > 0
            )
        # ffmpeg produced no valid output: keep the chapterless original instead of crashing the run
        if not succeeded:
            logging.log("Could not embed chapters; leaving file as-is")
            return
        os.replace(tmp_media_file, filepath)
    finally:
        if os.path.exists(tmp_chapter_file):
            os.remove(tmp_chapter_file)
        if os.path.exists(tmp_media_file):
            os.remove(tmp_media_file)
        

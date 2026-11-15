from audiobookdl import Chapter, utils, logging
from audiobookdl.utils.concurrency import run_cancellable_subprocess
from mutagen import File as MutagenFile
import os
import threading
from typing import Optional, Sequence

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
    for i in range(len(chapters)-1):
        chapter = chapters[i]
        result += create_chapter_text(chapter.title, chapter.start, chapters[i+1].start)
    length = MutagenFile(filepath).info.length*1000
    last_chapter = chapters[-1]
    result += create_chapter_text(
        title = last_chapter.title,
        start = last_chapter.start,
        end = int(length)
    )
    return result

def add_chapters_ffmpeg(
    filepath: str,
    chapters: Sequence[Chapter],
    cancel_event: Optional[threading.Event] = None,
):
    ffmpeg = utils.find_executable("ffmpeg")
    if ffmpeg is None:
        logging.log("ffmpeg not found; skipping chapter embedding")
        return
    stem, ext = os.path.splitext(filepath)
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
             tmp_media_file],
            capture_output=capture,
            cancel_event=cancel_event,
        )
        produced_output = (
            os.path.exists(tmp_media_file) and os.path.getsize(tmp_media_file) > 0
        )
        if result.returncode != 0 or not produced_output:
            logging.debug("add_chapters_ffmpeg copy mode failed, retrying with re-encode")
            if os.path.exists(tmp_media_file):
                os.remove(tmp_media_file)
            run_cancellable_subprocess(
                [ffmpeg, "-y",
                 "-i", filepath,
                 "-i", tmp_chapter_file,
                 "-map_chapters", "1",
                 "-c:a", "aac",
                 "-b:a", "128k",
                 "-map", "0",
                 "-metadata:s:a:0", "title=",
                 tmp_media_file],
                capture_output=capture,
                cancel_event=cancel_event,
            )
        # ffmpeg produced no output: keep the chapterless original instead of crashing the run
        if not (os.path.exists(tmp_media_file) and os.path.getsize(tmp_media_file) > 0):
            logging.log("Could not embed chapters; leaving file as-is")
            return
        os.remove(filepath)
        os.rename(tmp_media_file, filepath)
    finally:
        if os.path.exists(tmp_chapter_file):
            os.remove(tmp_chapter_file)
        if os.path.exists(tmp_media_file):
            os.remove(tmp_media_file)
        

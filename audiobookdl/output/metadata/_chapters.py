import math
from typing import Any
from collections.abc import Callable, Sequence

from mutagen import MutagenError

from audiobookdl import Chapter, logging
from audiobookdl.utils import ffprobe_duration


BoundedChapter = tuple[int, int, str]


def resolve_audio_duration_ms(
    filepath: str,
    mutagen_opener: Callable[[str], Any],
) -> int:
    """Return the duration of `filepath` in milliseconds, falling back to `ffprobe`."""
    length_sec: float | None = None
    try:
        audio = mutagen_opener(filepath)
    except (MutagenError, OSError, EOFError, ValueError) as exc:
        logging.debug(f"Mutagen failed to inspect {filepath}: {exc}")
        audio = None

    if audio is not None:
        info = getattr(audio, "info", None)
        raw_length = getattr(info, "length", None)
        if (
            isinstance(raw_length, (int, float))
            and not isinstance(raw_length, bool)
            and math.isfinite(raw_length)
            and raw_length > 0
        ):
            length_sec = float(raw_length)

    if length_sec is None:
        probed = ffprobe_duration(filepath)
        if math.isfinite(probed) and probed > 0:
            length_sec = probed

    if length_sec is None:
        return 0
    return max(0, int(length_sec * 1000))


def normalize_chapters(
    chapters: Sequence[Chapter],
    duration_ms: int,
) -> list[BoundedChapter]:
    """Sort chapters by `start`, clamp bounds to `[0, duration_ms]`, and skip empty spans."""
    if not chapters or duration_ms <= 0:
        return []

    ordered = sorted(chapters, key=lambda ch: int(ch.start))
    clamped_starts = [max(0, min(int(ch.start), duration_ms)) for ch in ordered]

    bounded: list[BoundedChapter] = []
    for i, chapter in enumerate(ordered):
        start = clamped_starts[i]
        end = clamped_starts[i + 1] if i + 1 < len(ordered) else duration_ms
        if end <= start:
            continue
        bounded.append((start, end, chapter.title))
    return bounded


def build_bounded_chapters(
    filepath: str,
    chapters: Sequence[Chapter],
    mutagen_opener: Callable[[str], Any],
) -> list[BoundedChapter]:
    """Resolve `filepath` duration and normalize `chapters` into valid `(start, end, title)` spans."""
    if not chapters:
        return []
    duration_ms = resolve_audio_duration_ms(filepath, mutagen_opener=mutagen_opener)
    return normalize_chapters(chapters, duration_ms)

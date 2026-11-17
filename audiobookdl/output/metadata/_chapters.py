import math
import re
from typing import Any
from collections.abc import Callable, Sequence

from mutagen import MutagenError

from audiobookdl import Chapter, logging
from audiobookdl.utils import ffprobe_duration


BoundedChapter = tuple[int, int, str]
INTRO_CHAPTER_MODES: tuple[str, ...] = ("auto", "always", "off")

_LEADING_INDEX_RE = re.compile(r"^\s*\d+\s*(?:[-._:]+|\s)\s*(?=[A-Za-zÀ-ÿ])")
_ZERO_NUMBERED_CHAPTER_RE = re.compile(
    r"^\s*(?:chapter|chap\.?|ch\.?|part|track)\s*0+\s*$",
    re.IGNORECASE,
)
_INTRO_TITLE_RE = re.compile(
    r"^(?:"
    r"intro(?:duction)?(?:\s+to\b.*|\s*[:\-–—(/].*)?|"
    r"opening(?:\s+(?:credits|titles?|remarks?|notes?))?(?:\s*[:\-–—(/].*)?|"
    r"(?:prologue|prelude|overture|foreword|preface|dedication|epigraph|"
    r"acknowledg(?:e)?ments?|title(?:\s+(?:page|credits))?|"
    r"(?:audio\s+)?credits|front\s*matter|"
    r"author'?s?\s+note|translator'?s?\s+note|editor'?s?\s+note|"
    r"note\s+to\s+the\s+reader|a\s+note\s+from\s+the\s+author|"
    r"about\s+(?:the\s+author|this\s+book)|"
    r"introduktion|inledning|f[öo]rord|prolog|"
    r"einleitung|vorwort|vorspann|"
    r"introdu[çc][ãa]o|introducci[óo]n|pref[áa]cio|prefacio|pr[óo]logo|prologo|abertura|"
    r"pr[ée]face|avant-propos|"
    r"introduzione|prefazione|"
    r"inleiding|voorwoord)(?:\s*[:\-–—(/].*)?"
    r")$",
    re.IGNORECASE,
)
_GENERIC_NUMBERED_RE = re.compile(
    r"^\s*(Chapter|Chap\.?|Ch\.?|Part|Track)\s*(0*)(\d+)\s*$",
    re.IGNORECASE,
)


def normalize_intro_chapter_mode(mode: str | None) -> str:
    """Normalize `intro_chapter_mode` to `'auto'`, `'always'`, or `'off'`."""
    cleaned = str(mode or "auto").strip().lower()
    if cleaned in ("always", "force", "true"):
        return "always"
    if cleaned in ("off", "never", "false"):
        return "off"
    return "auto"


def is_intro_chapter_title(title: str) -> bool:
    """Return True when `title` clearly represents an introduction, prologue, or front-matter track."""
    cleaned = (title or "").strip()
    if not cleaned:
        return False
    if _ZERO_NUMBERED_CHAPTER_RE.fullmatch(cleaned):
        return True
    stripped = _LEADING_INDEX_RE.sub("", cleaned, count=1).strip()
    return bool(_INTRO_TITLE_RE.fullmatch(stripped))


def _parse_generic_numbered_title(title: str) -> tuple[str, int, int] | None:
    """Parse generic `<Label> <Number>` titles into `(label, pad_width, number)`."""
    match = _GENERIC_NUMBERED_RE.fullmatch((title or "").strip())
    if not match:
        return None
    label, zeros, digits = match.group(1), match.group(2), match.group(3)
    pad_width = len(zeros) + len(digits) if zeros else 0
    return label, pad_width, int(digits)


def resolve_split_chapter_entries(
    raw_titles: Sequence[str],
    intro_mode: str = "auto",
) -> list[tuple[int, str]]:
    """
    Resolve `(file_number, chapter_title)` pairs for split multi-file outputs.

    When multiple chapters exist and an introduction/prologue/front-matter track
    is detected (`intro_mode='auto'`) or forced (`intro_mode='always'`), file
    numbering starts at `0` (`00 - ...`) and shifted generic fallback titles
    (`Chapter 2`, `Chapter 3`, ...) are renumbered starting at `Chapter 1`.
    """
    total = len(raw_titles)
    if total == 0:
        return []
    if total == 1:
        single_title = (raw_titles[0] or "").strip() or "Chapter 1"
        return [(1, single_title)]

    mode = normalize_intro_chapter_mode(intro_mode)
    cleaned_titles = [(t or "").strip() for t in raw_titles]

    if mode == "off":
        return [
            (idx + 1, title or f"Chapter {idx + 1}")
            for idx, title in enumerate(cleaned_titles)
        ]

    start_at_zero = mode == "always" or is_intro_chapter_title(cleaned_titles[0])
    if not start_at_zero:
        return [
            (idx + 1, title or f"Chapter {idx + 1}")
            for idx, title in enumerate(cleaned_titles)
        ]

    leading_intros = 1
    while leading_intros < total - 1 and is_intro_chapter_title(cleaned_titles[leading_intros]):
        leading_intros += 1

    # Detect whether generic numbered titles after the intro(s) were shifted by 1-based indexing.
    generic_after: list[tuple[int, tuple[str, int, int]]] = []
    for idx in range(leading_intros, total):
        parsed = _parse_generic_numbered_title(cleaned_titles[idx])
        if parsed is not None:
            generic_after.append((idx, parsed))

    should_shift_generic = bool(generic_after) and all(
        num == idx + 1 for idx, (_lbl, _pad, num) in generic_after
    )
    max_pad = max((pad for _idx, (_lbl, pad, _num) in generic_after), default=0)

    resolved: list[tuple[int, str]] = []
    for idx, title in enumerate(cleaned_titles):
        if idx == 0:
            if not title:
                resolved_title = "Introduction"
            elif mode == "always":
                parsed_first = _parse_generic_numbered_title(title)
                if parsed_first is not None and parsed_first[2] == 1:
                    resolved_title = "Introduction"
                else:
                    resolved_title = title
            else:
                resolved_title = title
            resolved.append((0, resolved_title))
            continue

        if idx < leading_intros:
            resolved.append((idx, title or "Introduction"))
            continue

        logical_num = idx - leading_intros + 1
        if not title:
            resolved_title = f"Chapter {logical_num}"
        elif should_shift_generic:
            parsed = _parse_generic_numbered_title(title)
            if parsed is not None:
                label, _pad, _num = parsed
                formatted_num = f"{logical_num:0{max_pad}d}" if max_pad > 0 else str(logical_num)
                resolved_title = f"{label} {formatted_num}"
            else:
                resolved_title = title
        else:
            resolved_title = title
        resolved.append((idx, resolved_title))

    return resolved


def apply_intro_chapter_titles(
    chapters: Sequence[Chapter],
    intro_mode: str = "auto",
) -> list[Chapter]:
    """Return `chapters` with intro/fallback titles resolved according to `intro_mode`."""
    if len(chapters) <= 1:
        return list(chapters)
    entries = resolve_split_chapter_entries([ch.title for ch in chapters], intro_mode=intro_mode)
    return [
        Chapter(start=ch.start, title=resolved_title)
        for ch, (_num, resolved_title) in zip(chapters, entries, strict=True)
    ]


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

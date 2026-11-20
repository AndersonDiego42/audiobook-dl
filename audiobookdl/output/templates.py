import os
import platform
import re
from typing import Any
from urllib.parse import urlparse

from audiobookdl import logging, AudiobookMetadata
from collections.abc import Sequence, Mapping

LOCATION_DEFAULTS = {
    'album': 'NA',
    'artist': 'NA',
    'title': '',
    'scrape_url': '',
    'series': '',
    'series_order': '',
    'series_order_padded': '',
    'author': '',
    'narrator': '',
    'genre': '',
    'language': '',
    'description': '',
    'isbn': '',
    'asin': '',
    'publisher': '',
    'release_date': '',
    'year': '',
    'source': '',
}

OUTPUT_TEMPLATE_PRESETS: dict[str, str] = {
    "default": "{title}",
    "audiobookshelf": "{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}",
    "plex": "{author}/{title}/{title}",
    "kavita": "{author}/{series?{series}/}{year?({year}) }{title}",
}


class _SafeFormatDict(dict):
    """Mapping for `str.format_map` that substitutes `""` for missing keys."""

    def __missing__(self, key: str) -> str:
        return ""


def get_max_name_length() -> int:
    """
    Get the max length for file names supported by the OS

    :returns: max length for file names
    """
    pathconf = getattr(os, "pathconf", None)
    if callable(pathconf):
        try:
            # should work on Linux/MacOS
            return int(pathconf(".", "PC_NAME_MAX"))
        except OSError:
            pass
    try:
        # Windows
        from ctypes.wintypes import MAX_PATH
        return MAX_PATH
    except Exception:
        # default
        return 255

# Extra length reserved on output_dir/filename for extensions, sidecar json, and
# temp paths such as `.work/ts_parts/0000.ts` (22) or `.m4b.chapters.tmp.txt` (21)
OUTPUT_PATH_EXT_RESERVE = 32
WINDOWS_MAX_PATH = 260
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _sanitize_windows_reserved_name(name: str) -> str:
    """Append `_` to the stem when `name` matches a Windows reserved device name."""
    stripped = name.rstrip(". ")
    stem, dot, rest = stripped.partition(".")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        return f"{stem}_{dot}{rest}"
    return name


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate `text` so its UTF-8 encoding does not exceed `max_bytes` bytes."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _utf16_len(text: str) -> int:
    """Return length of `text` in UTF-16 code units (matching Windows Win32 path limits)."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_utf16_units(text: str, max_units: int) -> str:
    """Truncate `text` so its UTF-16 code unit count does not exceed `max_units`."""
    if max_units <= 0:
        return ""
    if _utf16_len(text) <= max_units:
        return text
    encoded = text.encode("utf-16-le")
    # Walk back by UTF-16 code units if we split a surrogate pair
    truncated = encoded[: max_units * 2].decode("utf-16-le", errors="ignore")
    return truncated


def _sanitize_metadata_dict(
    metadata: AudiobookMetadata | None,
    remove_chars: str,
) -> dict[str, Any]:
    """Return metadata mapping with all string values sanitized for filesystem paths."""
    props = metadata.all_properties_dict() if metadata is not None else {}
    raw: dict[str, Any] = {**LOCATION_DEFAULTS, **props}
    if metadata is not None:
        if metadata.release_date is not None:
            raw["year"] = str(metadata.release_date.year)
        if (
            metadata.series_order is not None
            and not isinstance(metadata.series_order, bool)
        ):
            raw["series_order_padded"] = f"{int(metadata.series_order):02d}"
        if not raw.get("source") and metadata.scrape_url:
            host = urlparse(metadata.scrape_url).netloc.lower().removeprefix("www.")
            if host:
                raw["source"] = host.split(".")[0]
        if metadata.language is not None:
            raw["language"] = (
                getattr(metadata.language, "alpha_3", None)
                or getattr(metadata.language, "alpha_2", None)
                or str(metadata.language)
            )
    sanitized: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            cleaned = _fix_output(value)
            if remove_chars:
                cleaned = _remove_chars(cleaned, remove_chars)
            sanitized[key] = cleaned
        else:
            sanitized[key] = value
    return _SafeFormatDict(sanitized)


def _expand_conditional_blocks(template: str, metadata_dict: Mapping[str, Any]) -> str:
    """Resolve balanced `{key?true_branch}` / `{key?true_branch|false_branch}` expressions."""
    out: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        # Scan matching '}' tracking nested braces
        depth = 0
        j = i
        q_idx = -1
        pipe_idx = -1
        while j < n:
            c = template[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1:
                if c == "?" and q_idx == -1:
                    q_idx = j
                elif c == "|" and q_idx != -1 and pipe_idx == -1:
                    pipe_idx = j
            j += 1
        if depth != 0 or q_idx == -1:
            # Not a well-formed conditional block; emit literally up to matching close or one char
            if depth == 0 and j < n:
                out.append(template[i : j + 1])
                i = j + 1
            else:
                out.append(ch)
                i += 1
            continue
        key = template[i + 1 : q_idx].strip()
        if pipe_idx != -1:
            true_branch = template[q_idx + 1 : pipe_idx]
            false_branch = template[pipe_idx + 1 : j]
        else:
            true_branch = template[q_idx + 1 : j]
            false_branch = ""
        val = metadata_dict.get(key, "")
        is_truthy = val not in ("", None, 0, False) if key != "series_order" else (val not in ("", None))
        chosen = true_branch if is_truthy else false_branch
        out.append(_expand_conditional_blocks(chosen, metadata_dict))
        i = j + 1
    return "".join(out)


def _join_segments(segments: Sequence[str], seps: Sequence[str]) -> str:
    parts = [segments[0]]
    for sep, seg in zip(seps, segments[1:], strict=False):
        parts.append(sep)
        parts.append(seg)
    return "".join(parts)


def gen_output_location(
    template: str,
    metadata: AudiobookMetadata | None,
    remove_chars: str,
) -> str:
    """
    Generates the location of the output based on attributes of the audiobook.

    :param template: Python string template audiobook metadata is put into
    :param metadata: Audiobook metadata,
    :param remove_chars: List of characters to be removed from metadata values
    :returns: `template` with sanitized and length-bounded metadata inserted
    """
    metadata_dict = _sanitize_metadata_dict(metadata, remove_chars)
    resolved_template = _expand_conditional_blocks(template, metadata_dict)
    is_windows = platform.system() == "Windows"
    component_cap = min(get_max_name_length(), 255)

    # Split template on path separators so we sanitize/bound each path segment
    # independently while preserving intentional directory separators in `template`.
    tokens = re.split(r"([/\\])", resolved_template)
    raw_segments = tokens[0::2]
    seps = tokens[1::2]
    segments: list[str] = []
    kept_seps: list[str] = []
    dynamic_indices: list[int] = []
    last_idx = len(raw_segments) - 1

    for idx, raw_seg in enumerate(raw_segments):
        if "{" in raw_seg:
            seg = raw_seg.format_map(metadata_dict)
            if is_windows:
                seg = seg.rstrip(". ")
            if seg == "" and idx < last_idx:
                continue
            if seg in ("", ".", ".."):
                seg = "_"
            if is_windows:
                seg = _sanitize_windows_reserved_name(seg)
            reserve = OUTPUT_PATH_EXT_RESERVE if idx == last_idx else 0
            seg_limit = max(1, component_cap - reserve)
            seg = _truncate_utf8(seg, seg_limit)
            if is_windows:
                seg = _sanitize_windows_reserved_name(seg.rstrip(". ") or "_")
            dynamic_indices.append(len(segments))
        else:
            seg = raw_seg
        segments.append(seg)
        if idx < len(seps):
            kept_seps.append(seps[idx])

    truncated_for_length = False
    if is_windows and dynamic_indices:
        abs_path = os.path.abspath(_join_segments(segments, kept_seps))
        max_total_units = WINDOWS_MAX_PATH - 1 - OUTPUT_PATH_EXT_RESERVE
        excess = _utf16_len(abs_path) - max_total_units
        if excess > 0:
            # Trim dynamic segments from right to left (preserving at least 32 units
            # on the final segment when earlier dynamic segments can absorb overflow).
            for pos in range(len(dynamic_indices) - 1, -1, -1):
                if excess <= 0:
                    break
                seg_idx = dynamic_indices[pos]
                seg = segments[seg_idx]
                cur_units = _utf16_len(seg)
                min_keep = 32 if pos > 0 and cur_units > 32 else 1
                reducible = max(0, cur_units - min_keep)
                trim = min(excess, reducible)
                if trim > 0:
                    new_seg = _sanitize_windows_reserved_name(
                        _truncate_utf16_units(seg, cur_units - trim).rstrip(". ") or "_"
                    )
                    excess -= cur_units - _utf16_len(new_seg)
                    segments[seg_idx] = new_seg
                    truncated_for_length = True

    formatted = _join_segments(segments, kept_seps)
    if truncated_for_length:
        logging.log(f"output path too long, using {segments[-1]} as filename base")
    return formatted


def _fix_output(value: str) -> str:
    """Returns `value` without characters or segments the filesystem can't handle safely"""
    value = value.replace("/", "-").replace("\\", "-")
    # ':' is stripped on every platform: it is illegal on Windows and breaks
    # exFAT/SMB/Finder targets even where the local FS allows it.
    value = "".join(ch for ch in value if ord(ch) >= 0x20 and ch != ":")
    if platform.system() == "Windows":
        value = _remove_chars(value, '*?<>|"')
        value = _sanitize_windows_reserved_name(value)
    if value in (".", ".."):
        value = "-" * len(value)
    return value


def _remove_chars(s: str, chars: str) -> str:
    """Removes `chars` from `s`"""
    for i in chars:
        s = s.replace(i, "")
    return s

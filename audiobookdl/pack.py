"""Offline local audiobook discovery, tag/sidecar inspection, and repackaging."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any
from collections.abc import Iterable, Mapping, Sequence
import uuid

import requests

from audiobookdl import Audiobook, AudiobookMetadata, Chapter, Cover, logging
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.utils.audiobook import parse_language, parse_release_date
from audiobookdl.config import Config, load_config
from audiobookdl.exceptions import AudiobookDLException, DownloadCancelled, GenericAudiobookDLException
from audiobookdl.output import download as output_download
from audiobookdl.output import output
from audiobookdl.output.metadata._cover import apply_cover_postprocessing
from audiobookdl.output.metadata.reader import (
    TrackTagInfo,
    infer_metadata_from_path,
    merge_metadata,
    read_cover_file,
    read_sidecar_metadata,
    read_track_tags,
)
from audiobookdl.pipeline import (
    apply_runtime_options,
    enrich_audiobook_with_options,
    run_post_exec_hook,
)
from audiobookdl.utils import (
    AudioProbeInfo,
    find_executable,
    probe_audio_info,
    subprocess_kwargs,
)
from audiobookdl.utils.audiobook import AudiobookFile
from audiobookdl.utils.concurrency import run_cancellable_pool


SUPPORTED_PACK_EXTENSIONS = frozenset(
    {"mp3", "m4a", "m4b", "mp4", "aac", "ogg", "opus", "flac", "wav", "wma"}
)

_NATURAL_CHUNK_RE = re.compile(r"(\d+)")
_LEADING_TRACK_NUM_RE = re.compile(r"^(?:\d{1,4}[\s._-]+)+")


@dataclass
class PackOverrides:
    """Explicit user overrides applied on top of extracted local metadata."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: str | None = None
    series_order: int | None = None
    isbn: str | None = None
    asin: str | None = None
    cover_path: str | None = None
    genres: list[str] = field(default_factory=list)
    publisher: str | None = None
    release_date: str | None = None
    language: str | None = None
    description: str | None = None
    chapters: list[Chapter] | None = None


@dataclass
class PackBookSpec:
    """Resolved local book ready for previewing or repackaging."""

    source_root: str
    track_paths: list[str]
    audiobook: Audiobook
    output_format: str
    output_path: str
    total_duration_seconds: float = 0.0
    chapters_source: str = "tracks"


@dataclass
class PackSummary:
    """Result totals for a `run_pack` invocation."""

    books_completed: int = 0
    books_skipped: int = 0
    books_failed: int = 0
    cancelled: bool = False
    book_errors: list[tuple[str, str]] = field(default_factory=list)
    output_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary to a JSON-compatible dictionary."""
        return {
            "books_completed": self.books_completed,
            "books_skipped": self.books_skipped,
            "books_failed": self.books_failed,
            "cancelled": self.cancelled,
            "output_paths": list(self.output_paths),
            "errors": [{"source": s, "error": e} for s, e in self.book_errors],
        }


def natural_sort_key(text: str) -> tuple[Any, ...]:
    """Return a case-insensitive natural sort key splitting numeric runs cleanly."""
    parts = _NATURAL_CHUNK_RE.split(text.casefold())
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part), part))
        else:
            key.append((1, 0, part))
    return tuple(key)


def _is_supported_audio_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in SUPPORTED_PACK_EXTENSIONS


def _direct_audio_files(directory: str) -> list[str]:
    try:
        entries = sorted(os.listdir(directory), key=natural_sort_key)
    except OSError as exc:
        raise GenericAudiobookDLException(f"Cannot read folder '{directory}': {exc}") from exc
    out: list[str] = []
    for name in entries:
        full = os.path.join(directory, name)
        if _is_supported_audio_file(full):
            out.append(os.path.abspath(full))
    return out


def discover_pack_groups(
    paths: Sequence[str],
    *,
    recursive: bool = False,
    cancel_event: threading.Event | None = None,
) -> list[tuple[str, list[str]]]:
    """Discover `(source_root, track_paths)` book units from local directories/files.

    Non-recursive directory arguments treat each folder's direct audio files as one book.
    Recursive directory arguments walk subdirectories in natural order and emit every
    directory containing at least one supported audio file. Explicit file arguments are
    combined into a single book group.
    """
    if not paths:
        raise GenericAudiobookDLException("Provide at least one local folder or audio file to pack")

    groups: list[tuple[str, list[str]]] = []
    standalone_files: list[str] = []
    seen_groups: set[tuple[str, ...]] = set()

    def _append_group(root_dir: str, files: list[str]) -> None:
        normalized = tuple(os.path.normcase(os.path.abspath(f)) for f in files)
        if normalized in seen_groups:
            return
        seen_groups.add(normalized)
        groups.append((os.path.abspath(root_dir), files))

    for raw_path in paths:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        abs_path = os.path.abspath(raw_path)
        if not os.path.exists(abs_path):
            raise GenericAudiobookDLException(f"Local path not found: {raw_path}")
        if os.path.isfile(abs_path):
            if not _is_supported_audio_file(abs_path):
                raise GenericAudiobookDLException(f"Unsupported audio file format: {raw_path}")
            standalone_files.append(abs_path)
            continue

        if not os.path.isdir(abs_path):
            raise GenericAudiobookDLException(f"Not a file or directory: {raw_path}")

        if not recursive:
            direct = _direct_audio_files(abs_path)
            if not direct:
                raise GenericAudiobookDLException(
                    f"No audio files found directly in '{raw_path}'. "
                    "Enable recursive scanning to scan subdirectories."
                )
            _append_group(abs_path, direct)
        else:
            found_any = False
            for walk_root, dirs, files in os.walk(abs_path):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled
                dirs.sort(key=natural_sort_key)
                audio_in_dir = [
                    os.path.abspath(os.path.join(walk_root, name))
                    for name in sorted(files, key=natural_sort_key)
                    if _is_supported_audio_file(os.path.join(walk_root, name))
                ]
                if audio_in_dir:
                    _append_group(walk_root, audio_in_dir)
                    found_any = True
            if not found_any:
                raise GenericAudiobookDLException(
                    f"No supported audio files found under '{raw_path}'."
                )

    if standalone_files:
        deduped_files: list[str] = []
        seen_file_keys: set[str] = set()
        for fpath in standalone_files:
            key = os.path.normcase(fpath)
            if key not in seen_file_keys:
                seen_file_keys.add(key)
                deduped_files.append(fpath)
        if len(deduped_files) == 1:
            root = deduped_files[0]
        else:
            root = os.path.dirname(deduped_files[0])
        _append_group(root, deduped_files)

    return groups


def sort_track_paths(
    paths: Sequence[str],
    tag_map: Mapping[str, TrackTagInfo] | None = None,
) -> tuple[list[str], dict[str, TrackTagInfo]]:
    """Sort audio tracks by `(disc, track)` tags when present, falling back to natural filenames."""
    tags: dict[str, TrackTagInfo] = dict(tag_map) if tag_map is not None else {}
    for p in paths:
        if p not in tags:
            tags[p] = read_track_tags(p)

    def _sort_key(path: str) -> tuple[Any, ...]:
        info = tags.get(path)
        has_track = info is not None and info.track_number is not None
        has_disc = info is not None and info.disc_number is not None
        missing_flag = 0 if (has_track or has_disc) else 1
        disc = (info.disc_number or 0) if info is not None else 0
        track = (info.track_number or 0) if info is not None else 0
        return (
            missing_flag,
            disc,
            track,
            natural_sort_key(os.path.basename(path)),
            os.path.normcase(path),
        )

    return sorted(paths, key=_sort_key), tags


def _probe_embedded_chapters(filepath: str) -> list[Chapter]:
    """Read embedded chapters from a single audio file via `ffprobe`, returning `[]` if unavailable."""
    ffprobe = find_executable("ffprobe")
    if ffprobe is None:
        return []
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_chapters",
                "-of",
                "json",
                filepath,
            ],
            capture_output=True,
            text=True,
            **subprocess_kwargs(capture_output=True),
        )
    except OSError:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    raw = payload.get("chapters")
    if not isinstance(raw, list):
        return []
    chapters: list[Chapter] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        try:
            start_ms = max(0, round(float(item["start_time"]) * 1000))
        except (KeyError, TypeError, ValueError):
            continue
        tags = item.get("tags") or {}
        title = ""
        if isinstance(tags, dict):
            title = str(tags.get("title") or "").strip()
        chapters.append(Chapter(start=start_ms, title=title or f"Chapter {idx}"))
    return chapters


def _clean_filename_chapter_title(filepath: str, index: int) -> str:
    stem = os.path.splitext(os.path.basename(filepath))[0].strip()
    stripped = _LEADING_TRACK_NUM_RE.sub("", stem).strip(" -_.")
    return stripped or stem or f"Chapter {index}"


def _most_common_non_empty(values: Iterable[str | None]) -> str | None:
    counts: Counter[str] = Counter()
    for v in values:
        cleaned = (v or "").strip()
        if cleaned:
            counts[cleaned] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def aggregate_book_metadata(
    source_root: str,
    sorted_tracks: Sequence[str],
    tag_map: Mapping[str, TrackTagInfo],
    sidecar_meta: AudiobookMetadata | None,
    overrides: PackOverrides | None,
) -> AudiobookMetadata:
    """Combine track tags, folder sidecars, path heuristics, and user overrides cleanly."""
    tag_infos = [tag_map.get(p, TrackTagInfo()) for p in sorted_tracks]
    meta = AudiobookMetadata("")

    if len(tag_infos) == 1:
        t0 = tag_infos[0]
        merge_metadata(meta, t0.metadata)
        if not meta.title and t0.album:
            meta.title = t0.album
    elif tag_infos:
        shared_series = _most_common_non_empty(t.metadata.series for t in tag_infos)
        shared_album = _most_common_non_empty(t.album for t in tag_infos)
        distinct_titles = {t.title.strip() for t in tag_infos if t.title and t.title.strip()}
        if shared_album and (len(distinct_titles) > 1 or not distinct_titles):
            if not shared_series or shared_album != shared_series:
                meta.title = shared_album
        if not meta.title and len(distinct_titles) == 1:
            meta.title = next(iter(distinct_titles))
        for info in tag_infos:
            # Copy per-track shared fields (authors, narrators, series, etc.), preserving computed title
            saved_title = meta.title
            merge_metadata(meta, info.metadata)
            meta.title = saved_title

    if sidecar_meta is not None:
        # Sidecar title overrides generic missing title; fill all remaining fields
        if sidecar_meta.title and (not meta.title or meta.title == "Audiobook"):
            meta.title = sidecar_meta.title
        merge_metadata(meta, sidecar_meta)

    inferred = infer_metadata_from_path(source_root)
    merge_metadata(meta, inferred)

    if overrides is not None:
        if overrides.title:
            meta.title = overrides.title.strip()
        if overrides.authors:
            meta.authors = list(overrides.authors)
        if overrides.narrators:
            meta.narrators = list(overrides.narrators)
        if overrides.series is not None:
            meta.series = overrides.series.strip() or None
        if overrides.series_order is not None:
            meta.series_order = overrides.series_order
        if overrides.isbn is not None:
            meta.isbn = overrides.isbn.strip() or None
        if overrides.asin is not None:
            meta.asin = overrides.asin.strip() or None
        if overrides.genres:
            meta.genres = list(overrides.genres)
        if overrides.publisher is not None:
            meta.publisher = overrides.publisher.strip() or None
        if overrides.release_date is not None:
            parsed_rd = parse_release_date(overrides.release_date)
            if parsed_rd is not None:
                meta.release_date = parsed_rd
        if overrides.language is not None:
            parsed_lang = parse_language(overrides.language)
            if parsed_lang is not None:
                meta.language = parsed_lang
        if overrides.description is not None:
            meta.description = overrides.description.strip() or None

    if not meta.title:
        meta.title = "Audiobook"
    return meta


def _resolve_pack_cover(
    sorted_tracks: Sequence[str],
    tag_map: Mapping[str, TrackTagInfo],
    sidecar_cover: Cover | None,
    overrides: PackOverrides | None,
) -> Cover | None:
    if overrides and overrides.cover_path:
        cover = read_cover_file(overrides.cover_path)
        if cover is None:
            raise GenericAudiobookDLException(f"Cover file could not be read: {overrides.cover_path}")
        return cover
    if sidecar_cover is not None:
        return sidecar_cover
    for p in sorted_tracks:
        info = tag_map.get(p)
        if info is not None and info.cover is not None:
            return info.cover
    return None


def _build_pack_chapters(
    sorted_tracks: Sequence[str],
    tag_map: Mapping[str, TrackTagInfo],
    sidecar_chapters: Sequence[Chapter],
    book_title: str,
    probe_cache: dict[str, AudioProbeInfo],
    *,
    no_chapters: bool,
    cancel_event: threading.Event | None = None,
    max_workers: int | None = None,
) -> tuple[list[Chapter], float, str]:
    uncached = [p for p in dict.fromkeys(sorted_tracks) if p not in probe_cache]
    if uncached:
        probed = run_cancellable_pool(
            probe_audio_info,
            uncached,
            max_workers=output.resolve_ffmpeg_pool_workers(max_workers, cpu_bound=False),
            cancel_event=cancel_event,
        )
        for path, info in zip(uncached, probed, strict=True):
            probe_cache[path] = info

    total_seconds = 0.0
    durations_ms: list[int] = []
    for path in sorted_tracks:
        info = probe_cache[path]
        total_seconds += max(0.0, info.duration)
        durations_ms.append(max(0, round(info.duration * 1000)))

    if no_chapters:
        return [], total_seconds, "disabled"

    if sidecar_chapters:
        return list(sidecar_chapters), total_seconds, "sidecar"

    if len(sorted_tracks) == 1:
        single_info = probe_cache[sorted_tracks[0]]
        if single_info.chapters_probed:
            embedded = [Chapter(start=start_ms, title=ch_title) for start_ms, ch_title in single_info.chapters]
        else:
            embedded = _probe_embedded_chapters(sorted_tracks[0])
        if embedded:
            return embedded, total_seconds, "embedded"
        return [], total_seconds, "none"

    chapters: list[Chapter] = []
    cursor_ms = 0
    for idx, (path, dur_ms) in enumerate(zip(sorted_tracks, durations_ms, strict=True), start=1):
        tag = tag_map.get(path)
        tag_title = (tag.title or "").strip() if tag else ""
        if tag_title and tag_title != book_title:
            ch_title = tag_title
        else:
            ch_title = _clean_filename_chapter_title(path, idx)
        chapters.append(Chapter(start=cursor_ms, title=ch_title))
        cursor_ms += dur_ms
    return chapters, total_seconds, "tracks"


def resolve_pack_output_path(
    metadata: AudiobookMetadata,
    options: DownloadOptions,
    output_format: str,
    *,
    output_dir: str | None = None,
) -> str:
    """Resolve destination file path using `options.output_template`, `output_dir`, and `output_format`."""
    ext = output_format.lstrip(".").lower() or "m4b"
    location = output.gen_output_location(
        options.output_template,
        metadata,
        options.remove_chars,
    )
    if output_dir and not os.path.isabs(location):
        location = os.path.join(output_dir, location)
    suffix = f".{ext}"
    if location.lower().endswith(suffix):
        return os.path.abspath(location)
    return os.path.abspath(f"{location}{suffix}")


def inspect_local_book(
    source_root: str,
    raw_paths: Sequence[str],
    options: DownloadOptions,
    *,
    output_dir: str | None = None,
    overrides: PackOverrides | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    session: requests.Session | None = None,
    cancel_event: threading.Event | None = None,
) -> PackBookSpec:
    """Inspect tracks, sidecars, and tags for a local audiobook without modifying disk."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    sorted_tracks, tag_map = sort_track_paths(raw_paths)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    sidecar_dir = source_root if os.path.isdir(source_root) else os.path.dirname(source_root)
    audio_stem = os.path.splitext(os.path.basename(sorted_tracks[0]))[0] if len(sorted_tracks) == 1 else None
    sidecar_meta, sidecar_cover, sidecar_chapters = read_sidecar_metadata(sidecar_dir, audio_stem=audio_stem)

    metadata = aggregate_book_metadata(
        source_root,
        sorted_tracks,
        tag_map,
        sidecar_meta,
        overrides,
    )
    cover = _resolve_pack_cover(sorted_tracks, tag_map, sidecar_cover, overrides)
    cache = probe_cache if probe_cache is not None else {}
    chapters, total_seconds, chapters_source = _build_pack_chapters(
        sorted_tracks,
        tag_map,
        sidecar_chapters,
        metadata.title,
        cache,
        no_chapters=bool(getattr(options, "no_chapters", False)),
        cancel_event=cancel_event,
        max_workers=getattr(options, "max_workers", None),
    )
    if overrides is not None and overrides.chapters is not None and not getattr(options, "no_chapters", False):
        chapters = [Chapter(start=c.start, title=c.title) for c in overrides.chapters]
        chapters_source = "edited"

    output_format = (getattr(options, "output_format", None) or "m4b").lstrip(".").lower()
    output_path = resolve_pack_output_path(
        metadata,
        options,
        output_format,
        output_dir=output_dir,
    )
    files = [
        AudiobookFile(url=p, ext=os.path.splitext(p)[1].lstrip(".").lower())
        for p in sorted_tracks
    ]
    book_session = session if session is not None else requests.Session()
    audiobook = Audiobook(
        session=book_session,
        metadata=metadata,
        files=files,
        chapters=chapters,
        cover=cover,
    )
    return PackBookSpec(
        source_root=source_root,
        track_paths=sorted_tracks,
        audiobook=audiobook,
        output_format=output_format,
        output_path=output_path,
        total_duration_seconds=total_seconds,
        chapters_source=chapters_source,
    )


def _pack_single_track(
    src_path: str,
    staged_path: str,
    spec: PackBookSpec,
    options: DownloadOptions,
    *,
    cancel_event: threading.Event | None,
    probe_cache: dict[str, AudioProbeInfo],
) -> tuple[bool, bool]:
    """Convert or copy a single local track directly into `staged_path`.

    Returns `(chapters_embedded, cover_embedded)`.
    """
    src_ext = os.path.splitext(src_path)[1].lstrip(".").lower()
    target_ext = spec.output_format.lstrip(".").lower()
    chapters = None if options.no_chapters else (spec.audiobook.chapters or None)
    use_loudnorm = bool(getattr(options, "loudnorm", False))
    use_mono = bool(getattr(options, "mono", False))
    needs_mp4_mux = (
        src_ext == target_ext
        and target_ext in output.MP4_CONTAINERS
        and (chapters is not None or spec.audiobook.cover is not None)
    )
    if src_ext == target_ext and not needs_mp4_mux and not use_loudnorm and not use_mono:
        shutil.copy2(src_path, staged_path)
        return False, False

    extra_filter_kwargs: dict[str, Any] = {}
    if use_loudnorm:
        extra_filter_kwargs["loudnorm"] = True
    if use_mono:
        extra_filter_kwargs["mono"] = True
    _converted_path, chapters_embedded = output.convert_single_file(
        src_path,
        target_ext,
        cancel_event=cancel_event,
        audio_bitrate=options.audio_bitrate,
        chapters=chapters,
        remove_source=False,
        metadata=spec.audiobook.metadata,
        cover=spec.audiobook.cover,
        probe_cache=probe_cache,
        output_path=staged_path,
        faststart=True,
        **extra_filter_kwargs,
    )
    cover_embedded = spec.audiobook.cover is not None and target_ext in output.MP4_CONTAINERS
    return chapters_embedded, cover_embedded


def _remove_source_tracks(track_paths: Sequence[str], source_root: str) -> None:
    """Remove packed source tracks and prune `source_root` only if it becomes empty."""
    for path in track_paths:
        if os.path.exists(path):
            os.remove(path)
    if os.path.isdir(source_root):
        try:
            if not os.listdir(source_root):
                os.rmdir(source_root)
        except OSError:
            pass


def pack_book(
    spec: PackBookSpec,
    options: DownloadOptions,
    *,
    output_dir: str | None = None,
    delete_source: bool = False,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    probe_cache: dict[str, AudioProbeInfo] | None = None,
    config: Config | None = None,
) -> str:
    """Repackage a single inspected `PackBookSpec` and return the final output path."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled

    if getattr(options, "enrich_metadata", False):
        enrich_audiobook_with_options(
            spec.audiobook,
            options,
            interaction=interaction,
            config=config,
        )
        # Recompute output path after enrichment in case template uses newly populated fields
        spec.output_path = resolve_pack_output_path(
            spec.audiobook.metadata,
            options,
            spec.output_format,
            output_dir=output_dir,
        )

    output_path = spec.output_path
    output_download.confirm_overwrite_file(
        output_path,
        interaction=interaction,
        overwrite=options.overwrite,
    )

    final_parent = os.path.dirname(output_path) or os.getcwd()
    os.makedirs(final_parent, exist_ok=True)
    base_name = os.path.basename(output_path)
    staged_path = os.path.join(
        final_parent,
        f".{base_name}.pack-{uuid.uuid4().hex[:8]}.{spec.output_format}",
    )
    work_tmp_dir: str | None = None
    cache = probe_cache if probe_cache is not None else {}

    if spec.audiobook.cover is not None and (
        getattr(options, "square_cover", False)
        or getattr(options, "max_cover_size", None)
    ):
        spec.audiobook.cover = apply_cover_postprocessing(
            spec.audiobook.cover,
            square=bool(getattr(options, "square_cover", False)),
            max_size=getattr(options, "max_cover_size", None),
        )
    chapter_offset_ms = int(getattr(options, "chapter_offset_ms", 0) or 0)
    if spec.audiobook.chapters and chapter_offset_ms != 0:
        spec.audiobook.chapters = output.apply_chapter_offset(
            spec.audiobook.chapters, chapter_offset_ms
        )

    may_silence_chapters = (
        not options.no_chapters
        and bool(getattr(options, "chapters_from_silence", False))
        and not spec.audiobook.chapters
    )
    if may_silence_chapters and len(spec.track_paths) == 1:
        detected = output.detect_silence_chapters(
            spec.track_paths[0],
            cancel_event=cancel_event,
            probe_cache=cache,
        )
        if detected:
            if chapter_offset_ms != 0:
                detected = output.apply_chapter_offset(detected, chapter_offset_ms)
            spec.audiobook.chapters = detected

    use_loudnorm = bool(getattr(options, "loudnorm", False))
    use_mono = bool(getattr(options, "mono", False))
    extra_filter_kwargs: dict[str, Any] = {}
    if use_loudnorm:
        extra_filter_kwargs["loudnorm"] = True
    if use_mono:
        extra_filter_kwargs["mono"] = True

    try:
        chapters = None if options.no_chapters else (spec.audiobook.chapters or None)
        if len(spec.track_paths) == 1:
            chapters_embedded, cover_embedded = _pack_single_track(
                spec.track_paths[0],
                staged_path,
                spec,
                options,
                cancel_event=cancel_event,
                probe_cache=cache,
            )
        else:
            work_tmp_dir = tempfile.mkdtemp(prefix="audiobookdl-pack-")
            logging.book_update(f"Packing {len(spec.track_paths)} tracks to .{spec.output_format}")
            chapters_embedded = output.combine_audiofiles(
                list(spec.track_paths),
                work_tmp_dir,
                staged_path,
                cancel_event=cancel_event,
                chapters=chapters,
                audio_bitrate=options.audio_bitrate,
                metadata=spec.audiobook.metadata,
                cover=spec.audiobook.cover,
                probe_cache=cache,
                faststart=True,
                max_workers=getattr(options, "max_workers", None),
                **extra_filter_kwargs,
            )
            cover_embedded = spec.audiobook.cover is not None and spec.output_format in output.MP4_CONTAINERS

        if not (os.path.isfile(staged_path) and os.path.getsize(staged_path) > 0):
            raise GenericAudiobookDLException(
                f"Repackaging failed to produce output file '{output_path}'"
            )

        os.replace(staged_path, output_path)
        output.move_cached_probe(cache, staged_path, output_path)
        output_download.add_metadata_to_file(
            spec.audiobook,
            output_path,
            options,
            cancel_event=cancel_event,
            chapters_embedded=chapters_embedded,
            cover_embedded=cover_embedded,
            probe_cache=cache,
        )
    except Exception:
        if os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except OSError:
                pass
        raise
    finally:
        if work_tmp_dir is not None and os.path.isdir(work_tmp_dir):
            shutil.rmtree(work_tmp_dir, ignore_errors=True)

    if delete_source:
        src_norm = {os.path.normcase(os.path.abspath(p)) for p in spec.track_paths}
        if os.path.normcase(os.path.abspath(output_path)) not in src_norm:
            _remove_source_tracks(spec.track_paths, spec.source_root)

    run_post_exec_hook(
        getattr(options, "post_exec", None),
        output_path=output_path,
        metadata=spec.audiobook.metadata,
        audio_format=spec.output_format,
    )
    return output_path


def scope_overrides_for_batch(
    overrides: PackOverrides | None,
    total_books: int,
) -> PackOverrides | None:
    """Return full `overrides` for single-book batches or shared fields for multi-book batches."""
    if overrides is None or total_books <= 1:
        return overrides
    return PackOverrides(
        authors=list(overrides.authors),
        narrators=list(overrides.narrators),
        series=overrides.series,
        cover_path=overrides.cover_path,
        genres=list(overrides.genres),
        publisher=overrides.publisher,
        language=overrides.language,
    )


def run_pack(
    paths: Sequence[str],
    options: DownloadOptions,
    config: Config | None = None,
    *,
    output_dir: str | None = None,
    recursive: bool = False,
    delete_source: bool = False,
    dry_run: bool = False,
    overrides: PackOverrides | None = None,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
) -> PackSummary:
    """Discover, inspect, and repackage local audiobooks with isolated `JobContext`."""
    resolved_config = config if config is not None else load_config(options.config_location)
    ctx = apply_runtime_options(options, resolved_config)
    ctx.interaction = interaction if interaction is not None else logging.NonInteractiveInteractionHandler()
    ctx.cancel_event = cancel_event
    ctx.pause_event = pause_event
    ctx.sink = sink if sink is not None else logging.get_ui_sink()

    if not getattr(options, "output_format", None):
        options.output_format = "m4b"

    summary = PackSummary()
    session = requests.Session()
    try:
        with logging.job_context(ctx):
            groups = discover_pack_groups(paths, recursive=recursive)
            total = len(groups)
            probe_cache: dict[str, AudioProbeInfo] = {}
            for idx, (source_root, raw_tracks) in enumerate(groups, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    summary.cancelled = True
                    break
                try:
                    logging.wait_if_paused(cancel_event=cancel_event)
                except DownloadCancelled:
                    summary.cancelled = True
                    summary.books_skipped += 1
                    logging.log("Pack cancelled")
                    break
                book_overrides = scope_overrides_for_batch(overrides, total)
                try:
                    spec = inspect_local_book(
                        source_root,
                        raw_tracks,
                        options,
                        output_dir=output_dir,
                        overrides=book_overrides,
                        probe_cache=probe_cache,
                        session=session,
                    )
                    logging.emit_item_progress(idx, total, spec.audiobook.title)
                    if dry_run:
                        logging.log(
                            f"[Dry Run] [blue]{spec.audiobook.title}[/] "
                            f"({len(spec.track_paths)} track{'s' if len(spec.track_paths) != 1 else ''}, "
                            f"{len(spec.audiobook.chapters)} chapter{'s' if len(spec.audiobook.chapters) != 1 else ''}) "
                            f"-> {spec.output_path}"
                        )
                        summary.output_paths.append(spec.output_path)
                        summary.books_completed += 1
                        continue

                    logging.log(f"Packing [blue]{spec.audiobook.title}[/] ({len(spec.track_paths)} tracks)")
                    final_path = pack_book(
                        spec,
                        options,
                        output_dir=output_dir,
                        delete_source=delete_source,
                        interaction=ctx.interaction,
                        cancel_event=cancel_event,
                        probe_cache=probe_cache,
                        config=resolved_config,
                    )
                    summary.output_paths.append(final_path)
                    summary.books_completed += 1
                    logging.log(f"Packed [green]{final_path}[/]")
                    try:
                        from audiobookdl.integrations.audiobookshelf import sync_book_to_audiobookshelf

                        if sync_book_to_audiobookshelf(
                            final_path,
                            spec.audiobook.metadata,
                            resolved_config.audiobookshelf,
                            force_upload=bool(getattr(options, "upload_to_abs", False)),
                            session=session,
                        ):
                            logging.log(f"Synced [blue]{spec.audiobook.title}[/] to Audiobookshelf")
                    except Exception as exc:
                        logging.error(f"Audiobookshelf sync failed for '{spec.audiobook.title}': {exc}")
                except (DownloadCancelled, KeyboardInterrupt):
                    summary.cancelled = True
                    summary.books_skipped += 1
                    logging.log("Pack cancelled")
                    break
                except AudiobookDLException as exc:
                    summary.books_failed += 1
                    summary.book_errors.append((source_root, exc.error_description))
                    exc.print()
                    if logging.is_debug_mode():
                        logging.print_traceback()
                except Exception as exc:
                    summary.books_failed += 1
                    summary.book_errors.append((source_root, str(exc)))
                    logging.error(f"Failed packing '{source_root}': {exc}")
                    if logging.is_debug_mode():
                        logging.print_traceback()
    finally:
        session.close()

    return summary

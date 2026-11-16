"""Local library scanner that discovers existing audiobooks and reconciles `downloads.db`."""

from dataclasses import dataclass, field
import os
from typing import Callable, List, Optional, Sequence, Tuple

from audiobookdl.output.metadata.reader import read_sidecar_metadata, read_track_tags
from audiobookdl.pack import (
    SUPPORTED_PACK_EXTENSIONS,
    aggregate_book_metadata,
    natural_sort_key,
    sort_track_paths,
)
from audiobookdl.utils.archive import DownloadArchive


@dataclass(frozen=True)
class ScannedLocalBook:
    """Result for a single discovered local audiobook unit."""

    action: str  # "matched" | "registered"
    title: str
    author: str
    output_path: str
    audio_format: str
    entry_id: Optional[int] = None
    source: str = "local"


@dataclass
class LocalScanResult:
    """Aggregated counters and items produced by scanning local library paths."""

    scanned_books: int = 0
    matched: int = 0
    registered: int = 0
    missing: int = 0
    dry_run: bool = False
    items: List[ScannedLocalBook] = field(default_factory=list)
    missing_paths: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scanned_books": self.scanned_books,
            "matched": self.matched,
            "registered": self.registered,
            "missing": self.missing,
            "dry_run": self.dry_run,
            "missing_paths": list(self.missing_paths),
            "errors": list(self.errors),
            "items": [
                {
                    "action": item.action,
                    "title": item.title,
                    "author": item.author,
                    "output_path": item.output_path,
                    "audio_format": item.audio_format,
                    "entry_id": item.entry_id,
                    "source": item.source,
                }
                for item in self.items
            ],
        }


def _is_supported_audio(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return bool(ext and ext in SUPPORTED_PACK_EXTENSIONS)


def _split_folder_units(walk_root: str, audio_paths: List[str]) -> List[Tuple[str, List[str], str]]:
    """Split audio files inside `walk_root` into `(source_root, tracks, output_path)` book units."""
    if len(audio_paths) == 1:
        p = audio_paths[0]
        return [(p, [p], p)]

    all_m4b = all(os.path.splitext(p)[1].lower() in (".m4b", ".m4a") for p in audio_paths)
    if all_m4b:
        albums = set()
        for p in audio_paths:
            tag = read_track_tags(p)
            alb = (tag.album or "").strip().lower()
            if alb:
                albums.add(alb)
        # Multiple standalone .m4b books placed together in one folder
        if len(albums) != 1:
            return [(p, [p], p) for p in audio_paths]

    return [(walk_root, audio_paths, walk_root)]


def _discover_local_units(paths: Sequence[str]) -> Tuple[List[Tuple[str, List[str], str]], List[str]]:
    """Walk `paths` and return `(units, errors)` without mutating the database."""
    units: List[Tuple[str, List[str], str]] = []
    errors: List[str] = []
    seen_outputs: set[str] = set()

    def _add_unit(unit: Tuple[str, List[str], str]) -> None:
        key = os.path.normcase(os.path.abspath(unit[2]))
        if key not in seen_outputs:
            seen_outputs.add(key)
            units.append(unit)

    for raw_path in paths:
        abs_path = os.path.abspath(raw_path)
        if not os.path.exists(abs_path):
            errors.append(f"Path not found: {raw_path}")
            continue
        if os.path.isfile(abs_path):
            if _is_supported_audio(abs_path):
                _add_unit((abs_path, [abs_path], abs_path))
            else:
                errors.append(f"Unsupported audio file: {raw_path}")
            continue
        if not os.path.isdir(abs_path):
            errors.append(f"Not a file or directory: {raw_path}")
            continue

        for walk_root, dirs, files in os.walk(abs_path):
            dirs.sort(key=natural_sort_key)
            audio_paths = [
                os.path.abspath(os.path.join(walk_root, name))
                for name in sorted(files, key=natural_sort_key)
                if _is_supported_audio(os.path.join(walk_root, name))
            ]
            if audio_paths:
                for unit in _split_folder_units(walk_root, audio_paths):
                    _add_unit(unit)

    return units, errors


def _build_local_book_key(
    *,
    scrape_url: Optional[str],
    isbn: Optional[str],
    asin: Optional[str],
    output_path: str,
) -> str:
    if scrape_url and scrape_url.strip():
        return scrape_url.strip()
    if asin and asin.strip():
        return f"asin:{asin.strip()}"
    if isbn and isbn.strip():
        return f"isbn:{isbn.strip()}"
    return f"local:{os.path.normcase(os.path.abspath(output_path))}"


def scan_local_library(
    paths: Sequence[str],
    archive: DownloadArchive,
    *,
    profile: str = "default",
    verify_missing: bool = False,
    dry_run: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> LocalScanResult:
    """Scan local directories/files, match or register books in `archive`, and optionally check missing files."""
    result = LocalScanResult(dry_run=dry_run)
    units, errors = _discover_local_units(paths)
    result.errors.extend(errors)

    with archive.batch_connection():
        match_index = archive.build_match_index()
        for source_root, track_paths, unit_output_path in units:
            try:
                sorted_tracks, tag_map = sort_track_paths(track_paths)
                sidecar_dir = source_root if os.path.isdir(source_root) else os.path.dirname(source_root)
                audio_stem = os.path.splitext(os.path.basename(sorted_tracks[0]))[0] if len(sorted_tracks) == 1 else None
                sidecar_meta, _cover, _chapters = read_sidecar_metadata(sidecar_dir, audio_stem=audio_stem)
                meta = aggregate_book_metadata(source_root, sorted_tracks, tag_map, sidecar_meta, None)
                audio_format = os.path.splitext(sorted_tracks[0])[1].lstrip(".").lower()

                result.scanned_books += 1
                if on_progress is not None:
                    on_progress(f"Scanning: {meta.title}")

                match_id = match_index.find(
                    title=meta.title,
                    author=meta.author,
                    isbn=meta.isbn,
                    asin=meta.asin,
                    scrape_url=meta.scrape_url,
                    output_path=unit_output_path,
                )
                if match_id is not None:
                    if not dry_run:
                        archive.mark_local_match(
                            match_id,
                            output_path=unit_output_path,
                            audio_format=audio_format,
                            series=meta.series,
                            series_order=meta.series_order,
                            narrator=meta.narrator,
                            isbn=meta.isbn,
                            asin=meta.asin,
                        )
                    result.matched += 1
                    result.items.append(
                        ScannedLocalBook(
                            action="matched",
                            title=meta.title,
                            author=meta.author,
                            output_path=unit_output_path,
                            audio_format=audio_format,
                            entry_id=match_id,
                            source=(meta.source or "local"),
                        )
                    )
                else:
                    src = (meta.source or "local").strip().lower() or "local"
                    book_key = _build_local_book_key(
                        scrape_url=meta.scrape_url,
                        isbn=meta.isbn,
                        asin=meta.asin,
                        output_path=unit_output_path,
                    )
                    entry_id: Optional[int] = None
                    if not dry_run:
                        entry_id = archive.record(
                            src,
                            book_key,
                            title=meta.title,
                            author=meta.author,
                            url=meta.scrape_url,
                            output_path=unit_output_path,
                            audio_format=audio_format,
                            profile=profile,
                            status="downloaded",
                            series=meta.series,
                            series_order=meta.series_order,
                            narrator=meta.narrator,
                            isbn=meta.isbn,
                            asin=meta.asin,
                        )
                        match_index.add(
                            entry_id,
                            book_key=book_key,
                            url=meta.scrape_url,
                            title=meta.title,
                            author=meta.author,
                            output_path=unit_output_path,
                            isbn=meta.isbn,
                            asin=meta.asin,
                        )
                    result.registered += 1
                    result.items.append(
                        ScannedLocalBook(
                            action="registered",
                            title=meta.title,
                            author=meta.author,
                            output_path=unit_output_path,
                            audio_format=audio_format,
                            entry_id=entry_id,
                            source=src,
                        )
                    )
            except Exception as exc:
                result.errors.append(f"{unit_output_path}: {exc}")

        if verify_missing:
            for entry in archive.list_entries(limit=100000, include_trashed=False, include_hidden=True):
                if entry.status == "downloaded" and entry.output_path and not os.path.exists(entry.output_path):
                    result.missing += 1
                    result.missing_paths.append(entry.output_path)
                    if not dry_run:
                        archive.set_status(entry.id, "pending")

    return result

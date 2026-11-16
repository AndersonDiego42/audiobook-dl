"""Internet Archive (`archive.org`) public-domain audiobook adapter."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

from .source import CatalogBook, Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import BookNotFound, RequestError
from audiobookdl.utils.audiobook import parse_language, parse_release_date

# Higher value = preferred container/encoding when Archive.org stores derived variants for one stem.
_FORMAT_PRIORITY: dict[str, int] = {
    "m4b": 100,
    "mpeg4 audio": 95,
    "m4a": 90,
    "vbr mp3": 85,
    "mp3": 80,
    "ogg vorbis": 70,
    "opus": 70,
    "flac": 60,
    "wave": 50,
}


def _parse_ia_length_ms(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return max(0, round(float(raw) * 1000))
    text = str(raw).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0, round(float(text) * 1000))
    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60.0 + float(part)
        return max(0, round(seconds * 1000))
    return None


def _natural_sort_key(text: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", text.lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _strip_html(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = " ".join(re.sub(r"<[^>]+>", " ", str(raw)).split())
    return cleaned or None


class ArchiveOrgSource(Source):
    """Internet Archive (`archive.org`) Metadata API & search adapter."""

    _authentication_methods: list[str] = []
    names = ["Archive.org", "Internet Archive"]
    supports_search = True
    supports_library = False

    match = [
        r"https?://(?:www\.)?archive\.org/(?:details|metadata|download)/([^/?#]+)",
    ]

    def _extract_identifier(self, url: str) -> str:
        m = re.match(self.match[0], url)
        if not m:
            raise BookNotFound
        ident = m.group(1).strip()
        if not ident:
            raise BookNotFound
        return ident

    def _select_audio_files(self, raw_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate derived transcodes per stem, picking highest-priority audio per track."""
        by_stem: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name or name.startswith("__ia_"):
                continue
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            fmt = str(entry.get("format") or "").strip().lower()
            name_lower = name.lower()
            if fmt == "64kbps mp3" or name_lower.endswith("_64kb.mp3"):
                priority = 20
            elif fmt == "128kbps mp3" or name_lower.endswith("_128kb.mp3"):
                priority = 30
            else:
                priority = _FORMAT_PRIORITY.get(fmt) or _FORMAT_PRIORITY.get(ext, 0)
            if priority <= 0:
                continue
            raw_stem = os.path.splitext(name)[0]
            orig_name = str(entry.get("original") or "").strip()
            if orig_name:
                orig_ext = os.path.splitext(orig_name)[1].lstrip(".").lower()
                if orig_ext in _FORMAT_PRIORITY:
                    raw_stem = os.path.splitext(orig_name)[0]
            base_stem = re.sub(r"_(?:64kb|128kb|vbr)$", "", raw_stem, flags=re.IGNORECASE).lower()
            source_rank = 0 if str(entry.get("source") or "").strip().lower() == "derivative" else 1
            rank = (priority, source_rank)
            existing = by_stem.get(base_stem)
            if existing is None or rank > existing[0]:
                by_stem[base_stem] = (rank, entry)

        selected = [pair[1] for pair in by_stem.values()]

        # If an item has both a whole-book .m4b and multiple chapter tracks (.mp3/.ogg/.flac),
        # keep only the .m4b container so the book is not downloaded twice.
        m4b_items = [
            e for e in selected
            if os.path.splitext(str(e.get("name") or ""))[1].lstrip(".").lower() == "m4b"
            or str(e.get("format") or "").strip().lower() == "m4b"
        ]
        other_items = [e for e in selected if e not in m4b_items]
        if len(m4b_items) >= 1 and len(other_items) >= 2:
            def _track_nums(items: list[dict[str, Any]]) -> set[int]:
                nums: set[int] = set()
                for item in items:
                    raw_t = str(item.get("track") or "").split("/", 1)[0].strip()
                    if raw_t.isdigit():
                        nums.add(int(raw_t))
                return nums

            m4b_tracks = _track_nums(m4b_items)
            other_tracks = _track_nums(other_items)
            is_disjoint_sequence = bool(
                m4b_tracks
                and other_tracks
                and not (m4b_tracks & other_tracks)
                and (max(m4b_tracks) < min(other_tracks) or max(other_tracks) < min(m4b_tracks))
            )
            if not is_disjoint_sequence:
                selected = m4b_items

        def _track_key(item: dict[str, Any]) -> tuple[int, int, tuple[Any, ...]]:
            raw_track = str(item.get("track") or "").split("/", 1)[0].strip()
            if raw_track.isdigit():
                return (0, int(raw_track), _natural_sort_key(str(item.get("name") or "")))
            return (1, 0, _natural_sort_key(str(item.get("name") or "")))

        selected.sort(key=_track_key)
        return selected

    def _fetch_cover(self, identifier: str, raw_files: list[dict[str, Any]]) -> Cover | None:
        image_exts = ("jpg", "jpeg", "png", "webp")
        candidates: list[tuple[int, str]] = []
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext not in image_exts:
                continue
            lower_name = name.lower()
            if "cover" in lower_name or "folder" in lower_name:
                candidates.append((3, name))
            elif lower_name == "__ia_thumb.jpg":
                candidates.append((1, name))
            else:
                candidates.append((2, name))
        candidates.sort(key=lambda c: -c[0])
        for _prio, fname in candidates:
            img_url = f"https://archive.org/download/{quote(identifier)}/{quote(fname)}"
            try:
                data = self.get(img_url)
                ext = os.path.splitext(fname)[1].lstrip(".").lower() or "jpg"
                return Cover(image=data, extension=ext)
            except Exception:
                continue
        return None

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        try:
            payload = self.get_json(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f"({cleaned}) AND mediatype:audio",
                    "fl[]": ["identifier", "title", "creator"],
                    "rows": 25,
                    "output": "json",
                },
            )
        except RequestError:
            return []
        docs = (payload.get("response") or {}).get("docs") or []
        results: list[CatalogBook] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            ident = str(doc.get("identifier") or "").strip()
            title = str(doc.get("title") or ident).strip()
            if not ident:
                continue
            creator = doc.get("creator") or ""
            if isinstance(creator, list):
                author = ", ".join(str(c).strip() for c in creator if c)
            else:
                author = str(creator).strip()
            results.append(
                CatalogBook(
                    title=title,
                    url=f"https://archive.org/details/{ident}",
                    author=author,
                    source="archive.org",
                )
            )
        return results

    def download(self, url: str) -> Audiobook:
        identifier = self._extract_identifier(url)
        data = self.get_json(f"https://archive.org/metadata/{quote(identifier)}")
        if not isinstance(data, dict) or data.get("is_dark") or data.get("error"):
            raise BookNotFound
        raw_files = data.get("files") or []
        selected = self._select_audio_files(raw_files)
        if not selected:
            raise BookNotFound

        md = data.get("metadata") or {}
        raw_title = md.get("title") or identifier
        title = str(raw_title[0] if isinstance(raw_title, list) else raw_title).strip() or identifier
        meta = AudiobookMetadata(title=title)
        meta.source = "archive.org"
        meta.scrape_url = f"https://archive.org/details/{identifier}"

        creators = md.get("creator") or md.get("artist") or []
        if isinstance(creators, str):
            creators = [p.strip() for p in re.split(r"[;]", creators) if p.strip()]
        for author in creators:
            if str(author).strip():
                meta.add_author(str(author).strip())

        desc = md.get("description")
        if isinstance(desc, list):
            desc = "\n".join(str(d) for d in desc if d)
        cleaned_desc = _strip_html(desc)
        if cleaned_desc:
            meta.description = cleaned_desc

        raw_lang = md.get("language")
        if isinstance(raw_lang, list) and raw_lang:
            raw_lang = raw_lang[0]
        if raw_lang:
            meta.language = parse_language(raw_lang)

        raw_date = md.get("date") or md.get("year")
        if isinstance(raw_date, list) and raw_date:
            raw_date = raw_date[0]
        if raw_date:
            meta.release_date = parse_release_date(raw_date)

        if md.get("publisher"):
            pub = md["publisher"][0] if isinstance(md["publisher"], list) else md["publisher"]
            meta.publisher = str(pub).strip()

        if md.get("isbn"):
            isbn_val = md["isbn"][0] if isinstance(md["isbn"], list) else md["isbn"]
            meta.isbn = str(isbn_val).strip()

        files: list[AudiobookFile] = []
        chapters: list[Chapter] = []
        running_ms = 0
        lengths: list[int | None] = [_parse_ia_length_ms(entry.get("length")) for entry in selected]
        all_have_lengths = all(dur is not None and dur > 0 for dur in lengths)

        for entry, dur_ms in zip(selected, lengths, strict=True):
            fname = str(entry["name"])
            ext = os.path.splitext(fname)[1].lstrip(".").lower() or "mp3"
            track_title = str(entry.get("title") or os.path.splitext(os.path.basename(fname))[0]).strip()
            file_url = f"https://archive.org/download/{quote(identifier)}/{quote(fname)}"
            files.append(AudiobookFile(url=file_url, ext=ext, title=track_title))
            if all_have_lengths and dur_ms is not None:
                chapters.append(Chapter(start=running_ms, title=track_title))
                running_ms += dur_ms

        cover = self._fetch_cover(identifier, raw_files)
        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

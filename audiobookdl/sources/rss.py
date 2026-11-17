"""Generic RSS 2.0 / Atom podcast & serialized audiobook feed source."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from urllib.parse import urljoin, urlparse

from lxml import etree

from .source import Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import BookNotFound
from audiobookdl.utils.audiobook import (
    clean_html_description,
    detect_image_extension,
    parse_duration_ms,
    parse_language,
)

_AUDIO_MIME_TO_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4b": "m4b",
    "audio/m4b": "m4b",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "opus",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}
_AUDIO_EXTENSIONS = frozenset({"mp3", "m4a", "m4b", "ogg", "opus", "flac", "aac", "wav"})


def _local_tag(elem: etree._Element) -> str:
    tag = str(elem.tag)
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _find_child_text(parent: etree._Element, names: tuple[str, ...]) -> str | None:
    lowered = set(names)
    for child in parent:
        if _local_tag(child) in lowered:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return None


def _guess_extension(audio_url: str, mime_type: str | None) -> str:
    ext = os.path.splitext(urlparse(audio_url).path)[1].lstrip(".").lower()
    if ext in _AUDIO_EXTENSIONS:
        return ext
    if mime_type:
        mapped = _AUDIO_MIME_TO_EXT.get(mime_type.split(";", 1)[0].strip().lower())
        if mapped:
            return mapped
    return "mp3"


def _parse_feed_timestamp(raw: str | None) -> float | None:
    """Parse RFC 2822 RSS `<pubDate>` or ISO-8601 Atom `<published>`/`<updated>` timestamps."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return parsedate_to_datetime(cleaned).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        iso_val = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
        dt = datetime.fromisoformat(iso_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class RssSource(Source):
    """Generic RSS 2.0 / Atom podcast & audiobook XML feed adapter."""

    _authentication_methods: list[str] = []
    names = ["RSS"]
    supports_search = False
    supports_library = False

    match = [
        r"https?://[^?#]+\.(?:rss|xml|atom)(?:[?#].*)?$",
        r"https?://[^?#]+/(?:feed|rss|atom)/?(?:[?#].*)?$",
        r"https?://[^/]+/[^?#]*[?&](?:format|alt|feed)=(?:rss|atom)(?:&|#|$)",
        r"https?://(?:[^/]+\.)?substack\.com/feed/?(?:[?#].*)?$",
        r"https?://feeds\.feedburner\.com/[^?#]+",
        r"https?://feeds\.libsyn\.com/[^?#]+",
        r"https?://feeds\.(?:megaphone\.fm|simplecast\.com|buzzsprout\.com|transistor\.fm|captivate\.fm|acast\.com|podbean\.com)/[^?#]+",
    ]

    def _parse_feed_root(self, xml_bytes: bytes) -> etree._Element:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
        root = etree.fromstring(xml_bytes, parser=parser)
        if root is None:
            raise BookNotFound
        return root

    def _extract_cover(self, container: etree._Element, base_url: str) -> Cover | None:
        for child in container:
            tag = _local_tag(child)
            if tag in ("image", "thumbnail"):
                href = (child.get("href") or child.get("url") or "").strip()
                if not href:
                    href = (_find_child_text(child, ("url",)) or "").strip()
                if href:
                    abs_url = urljoin(base_url, href)
                    default_ext = os.path.splitext(urlparse(abs_url).path)[1].lstrip(".").lower() or "jpg"
                    try:
                        img_bytes = self.get(abs_url)
                        if not img_bytes:
                            return None
                        return Cover(
                            image=img_bytes,
                            extension=detect_image_extension(img_bytes, default=default_ext),
                        )
                    except Exception:
                        return None
        return None

    def _fetch_podcast_chapters(self, chapters_url: str) -> list[Chapter]:
        """Fetch Podcasting 2.0 JSON chapters from `chapters_url` non-fatally."""
        try:
            raw = self.get(chapters_url)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        raw_chapters = payload.get("chapters")
        if not isinstance(raw_chapters, list):
            return []
        parsed: list[Chapter] = []
        for idx, entry in enumerate(raw_chapters, start=1):
            if not isinstance(entry, dict):
                continue
            start_raw = entry.get("startTime")
            if start_raw is None:
                continue
            try:
                start_sec = float(start_raw)
            except (TypeError, ValueError):
                continue
            if start_sec < 0:
                continue
            title = str(entry.get("title") or f"Chapter {idx}").strip() or f"Chapter {idx}"
            parsed.append(Chapter(start=round(start_sec * 1000), title=title))
        return parsed

    def _extract_rss_items(
        self,
        channel: etree._Element,
        base_url: str,
    ) -> list[tuple[str, str, str, int | None, int | None, float | None, str | None]]:
        """Return list of `(url, ext, title, duration_ms, episode_num, pub_ts, chapters_url)`."""
        episodes: list[tuple[str, str, str, int | None, int | None, float | None, str | None]] = []
        raw_items = [c for c in channel if _local_tag(c) in ("item", "entry")]
        for idx, item in enumerate(raw_items, start=1):
            enclosure_url: str | None = None
            enclosure_type: str | None = None
            chapters_url: str | None = None
            for child in item:
                ctag = _local_tag(child)
                if ctag == "chapters":
                    ch_href = (child.get("url") or "").strip()
                    ch_type = (child.get("type") or "").split(";", 1)[0].strip().lower()
                    if ch_href and (not ch_type or "json" in ch_type):
                        chapters_url = urljoin(base_url, ch_href)
                elif ctag == "enclosure" and not enclosure_url:
                    enclosure_url = (child.get("url") or "").strip() or None
                    enclosure_type = (child.get("type") or "").strip() or None
                elif ctag == "link" and not enclosure_url and (child.get("rel") or "").lower() == "enclosure":
                    enclosure_url = (child.get("href") or "").strip() or None
                    enclosure_type = (child.get("type") or "").strip() or None
                elif ctag == "content" and not enclosure_url and child.get("url"):
                    ctype = (child.get("type") or "").lower()
                    if not ctype or ctype.startswith("audio/"):
                        enclosure_url = (child.get("url") or "").strip() or None
                        enclosure_type = ctype or None
            if not enclosure_url:
                continue
            abs_url = urljoin(base_url, enclosure_url)
            mime_base = (enclosure_type or "").split(";", 1)[0].strip().lower()
            raw_ext = os.path.splitext(urlparse(abs_url).path)[1].lstrip(".").lower()
            if mime_base and not mime_base.startswith("audio/") and mime_base != "application/ogg" and raw_ext not in _AUDIO_EXTENSIONS:
                continue

            ext = _guess_extension(abs_url, enclosure_type)
            ep_title = _find_child_text(item, ("title",)) or f"Episode {idx}"
            duration_ms = parse_duration_ms(_find_child_text(item, ("duration",)))
            raw_ep = _find_child_text(item, ("episode",))
            ep_num = int(raw_ep) if (raw_ep and raw_ep.isdigit()) else None
            pub_raw = _find_child_text(item, ("pubdate", "published", "updated"))
            pub_ts = _parse_feed_timestamp(pub_raw)
            episodes.append((abs_url, ext, ep_title, duration_ms, ep_num, pub_ts, chapters_url))
        return episodes

    def _order_episodes(
        self,
        episodes: list[tuple[str, str, str, int | None, int | None, float | None, str | None]],
    ) -> list[tuple[str, str, str, int | None, int | None, float | None, str | None]]:
        if len(episodes) <= 1:
            return episodes
        if all(ep[4] is not None for ep in episodes):
            return sorted(episodes, key=lambda ep: ep[4] or 0)
        pub_timestamps = [ep[5] for ep in episodes]
        if all(ts is not None for ts in pub_timestamps):
            first_ts = pub_timestamps[0]
            last_ts = pub_timestamps[-1]
            if first_ts is not None and last_ts is not None and first_ts > last_ts:
                return list(reversed(episodes))
        return episodes

    def download(self, url: str) -> Audiobook:
        xml_bytes = self.get(url)
        root = self._parse_feed_root(xml_bytes)
        container = root
        for child in root:
            if _local_tag(child) == "channel":
                container = child
                break

        episodes = self._extract_rss_items(container, url)
        if not episodes:
            raise BookNotFound
        episodes = self._order_episodes(episodes)

        title = _find_child_text(container, ("title",)) or "Podcast Feed"
        meta = AudiobookMetadata(title=title)
        meta.source = "rss"
        meta.scrape_url = url
        author = _find_child_text(container, ("author", "creator", "managingeditor"))
        if author:
            meta.add_author(author)
        desc = clean_html_description(_find_child_text(container, ("summary", "description", "subtitle")))
        if desc:
            meta.description = desc
        lang = _find_child_text(container, ("language",))
        if lang:
            meta.language = parse_language(lang)
        cover = self._extract_cover(container, url)

        files: list[AudiobookFile] = []
        chapters: list[Chapter] = []
        running_ms = 0
        all_have_duration = all(ep[3] is not None and ep[3] > 0 for ep in episodes)
        for ep_url, ext, ep_title, duration_ms, _ep_num, _pub_ts, ch_url in episodes:
            files.append(AudiobookFile(url=ep_url, ext=ext, title=ep_title))
            if len(episodes) == 1 and ch_url:
                chapters.extend(self._fetch_podcast_chapters(ch_url))
            elif all_have_duration and duration_ms is not None:
                if ch_url:
                    ep_chapters = self._fetch_podcast_chapters(ch_url)
                    if ep_chapters:
                        for ch in ep_chapters:
                            chapters.append(Chapter(start=running_ms + ch.start, title=ch.title))
                    else:
                        chapters.append(Chapter(start=running_ms, title=ep_title))
                else:
                    chapters.append(Chapter(start=running_ms, title=ep_title))
                running_ms += duration_ms

        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

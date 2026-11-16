"""Generic RSS 2.0 / Atom podcast & serialized audiobook feed source."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import os
import re
from urllib.parse import urljoin, urlparse

from lxml import etree

from .source import Source
from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.exceptions import BookNotFound
from audiobookdl.utils.audiobook import parse_language

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


def _parse_duration_ms(raw: str | None) -> int | None:
    """Parse `<itunes:duration>` (`HH:MM:SS`, `MM:SS`, or seconds) into milliseconds."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0, round(float(text) * 1000))
    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60.0 + float(part)
        return max(0, round(seconds * 1000))
    return None


def _strip_html(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
    return cleaned or None


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


class RssSource(Source):
    """Generic RSS 2.0 / Atom podcast & audiobook XML feed adapter."""

    _authentication_methods: list[str] = []
    names = ["RSS"]
    supports_search = False
    supports_library = False

    match = [
        r"https?://[^?#]+\.(?:rss|xml)(?:[?#].*)?$",
        r"https?://[^?#]+/(?:feed|rss)/?(?:[?#].*)?$",
        r"https?://[^/]+/[^?#]*[?&](?:format|alt|feed)=rss(?:&|#|$)",
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
                    try:
                        img_bytes = self.get(abs_url)
                        ext = os.path.splitext(urlparse(abs_url).path)[1].lstrip(".").lower() or "jpg"
                        return Cover(image=img_bytes, extension=ext)
                    except Exception:
                        return None
        return None

    def _extract_rss_items(
        self,
        channel: etree._Element,
        base_url: str,
    ) -> list[tuple[str, str, str, int | None, int | None, float | None]]:
        """Return list of `(url, ext, title, duration_ms, episode_num, pub_ts)`."""
        episodes: list[tuple[str, str, str, int | None, int | None, float | None]] = []
        raw_items = [c for c in channel if _local_tag(c) in ("item", "entry")]
        for idx, item in enumerate(raw_items, start=1):
            enclosure_url: str | None = None
            enclosure_type: str | None = None
            for child in item:
                ctag = _local_tag(child)
                if ctag == "enclosure":
                    enclosure_url = (child.get("url") or "").strip() or None
                    enclosure_type = (child.get("type") or "").strip() or None
                    break
                if ctag == "link" and (child.get("rel") or "").lower() == "enclosure":
                    enclosure_url = (child.get("href") or "").strip() or None
                    enclosure_type = (child.get("type") or "").strip() or None
                    break
                if ctag == "content" and child.get("url"):
                    ctype = (child.get("type") or "").lower()
                    if not ctype or ctype.startswith("audio/"):
                        enclosure_url = (child.get("url") or "").strip() or None
                        enclosure_type = ctype or None
                        break
            if not enclosure_url:
                continue
            abs_url = urljoin(base_url, enclosure_url)
            mime_base = (enclosure_type or "").split(";", 1)[0].strip().lower()
            raw_ext = os.path.splitext(urlparse(abs_url).path)[1].lstrip(".").lower()
            if mime_base and not mime_base.startswith("audio/") and mime_base != "application/ogg" and raw_ext not in _AUDIO_EXTENSIONS:
                continue

            ext = _guess_extension(abs_url, enclosure_type)
            ep_title = _find_child_text(item, ("title",)) or f"Episode {idx}"
            duration_ms = _parse_duration_ms(_find_child_text(item, ("duration",)))
            raw_ep = _find_child_text(item, ("episode",))
            ep_num = int(raw_ep) if (raw_ep and raw_ep.isdigit()) else None
            pub_raw = _find_child_text(item, ("pubdate", "published", "updated"))
            pub_ts: float | None = None
            if pub_raw:
                try:
                    pub_ts = parsedate_to_datetime(pub_raw).timestamp()
                except (TypeError, ValueError):
                    pub_ts = None
            episodes.append((abs_url, ext, ep_title, duration_ms, ep_num, pub_ts))
        return episodes

    def _order_episodes(
        self,
        episodes: list[tuple[str, str, str, int | None, int | None, float | None]],
    ) -> list[tuple[str, str, str, int | None, int | None, float | None]]:
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
        desc = _strip_html(_find_child_text(container, ("summary", "description", "subtitle")))
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
        for ep_url, ext, ep_title, duration_ms, _ep_num, _pub_ts in episodes:
            files.append(AudiobookFile(url=ep_url, ext=ext, title=ep_title))
            if all_have_duration and duration_ms is not None:
                chapters.append(Chapter(start=running_ms, title=ep_title))
                running_ms += duration_ms

        return Audiobook(
            session=self._session,
            metadata=meta,
            files=files,
            chapters=chapters,
            cover=cover,
        )

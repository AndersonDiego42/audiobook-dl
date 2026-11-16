from datetime import date, datetime
from html import unescape
import math
import re
import requests
from typing import Generic, Optional, TypeVar, Any
from collections.abc import Callable, Sequence, MutableMapping
import json
from attrs import define, Factory, field
from dateutil.parser import parse as _dateutil_parse
import pycountry


@define
class Chapter:
    # Start time of chapter in milliseconds
    start: int
    # Title of chapter
    title: str


@define
class Cover:
    image: bytes
    extension: str


@define
class AESEncryption:
    key: bytes
    iv: bytes


AudiobookFileEncryption = AESEncryption


@define
class AudiobookFile:
    # Url to audio file
    url: str
    # Output file extension
    ext: str
    # Title of file
    title: str | None = None
    # Headers for request
    headers: MutableMapping[str, str | bytes] = Factory(dict)
    # Encryption method
    encryption_method: AudiobookFileEncryption | None = None
    # Expected content-type of the download request
    expected_content_type: str | None = None
    # Expected status code of the download request
    expected_status_code: int = 200
    # Optional lazy URL resolver invoked when downloading starts
    url_resolver: Callable[[], str] | None = None
    # Optional HLS fMP4 initialization section (#EXT-X-MAP) prepended before segment frames
    init_section: bytes | None = None


_LANGUAGE_ALIASES: dict[str, str] = {
    "dansk": "dan",
    "svenska": "swe",
    "norsk": "nor",
    "bokmål": "nob",
    "bokmal": "nob",
    "nynorsk": "nno",
    "deutsch": "deu",
    "français": "fra",
    "francais": "fra",
    "español": "spa",
    "espanol": "spa",
    "português": "por",
    "portugues": "por",
    "italiano": "ita",
    "nederlands": "nld",
    "polski": "pol",
    "suomi": "fin",
    "íslenska": "isl",
    "islenska": "isl",
    "türkçe": "tur",
    "turkce": "tur",
    "русский": "rus",
    "العربية": "ara",
}


def parse_language(raw: Any) -> Optional["pycountry.db.Language"]:  # type: ignore
    """Normalize an ISO-639 code, BCP-47 locale tag, or language name to a `pycountry` Language."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, pycountry.db.Data) and hasattr(raw, "alpha_3"):
        return raw
    text = str(raw).strip()
    if not text:
        return None

    base = re.split(r"[-_]", text, maxsplit=1)[0].strip()
    candidates = [text] if base == text or not base else [text, base]

    for candidate in candidates:
        lower = candidate.lower()
        if len(candidate) == 2:
            found = pycountry.languages.get(alpha_2=lower)
            if found is not None:
                return found
        elif len(candidate) == 3:
            found = (
                pycountry.languages.get(alpha_3=lower)
                or pycountry.languages.get(bibliographic=lower)
            )
            if found is not None:
                return found
        else:
            alias_alpha3 = _LANGUAGE_ALIASES.get(lower)
            if alias_alpha3 is not None:
                found = pycountry.languages.get(alpha_3=alias_alpha3)
                if found is not None:
                    return found
            found = (
                pycountry.languages.get(name=candidate)
                or pycountry.languages.get(name=candidate.title())
            )
            if found is not None:
                return found
            try:
                return pycountry.languages.lookup(candidate)
            except LookupError:
                pass
    return None


def parse_release_date(raw: Any) -> date | None:
    """Parse a release date (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`, `int`, `date`, or natural text)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, int):
        if 1 <= raw <= 9999:
            return date(raw, 1, 1)
        return None
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    m_full = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if m_full:
        try:
            return date(int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3)))
        except ValueError:
            pass
    m_ym = re.match(r"^(\d{4})-(\d{1,2})$", cleaned)
    if m_ym:
        try:
            return date(int(m_ym.group(1)), int(m_ym.group(2)), 1)
        except ValueError:
            pass
    m_y = re.match(r"^(\d{4})$", cleaned)
    if m_y:
        year = int(m_y.group(1))
        if 1 <= year <= 9999:
            return date(year, 1, 1)
        return None
    try:
        return _dateutil_parse(cleaned, default=datetime(1900, 1, 1)).date()
    except (ValueError, OverflowError, TypeError):
        return None


_DURATION_ISO_RE = re.compile(
    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
    re.IGNORECASE,
)
_HTML_BREAK_NEWLINE_RE = re.compile(r"<(?:br\s*/?|/p|/div|/li)\s*>", re.IGNORECASE)
_HTML_BLOCK_SPACE_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6])\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def parse_duration_ms(raw: Any) -> int | None:
    """Parse ISO-8601 (`PT12M34S`), numeric seconds, or `HH:MM:SS` / `MM:SS` into milliseconds."""
    if raw is None or isinstance(raw, bool) or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return max(0, round(val * 1000)) if math.isfinite(val) else None
    text = str(raw).strip()
    if not text:
        return None
    iso_match = _DURATION_ISO_RE.fullmatch(text)
    if iso_match and any(iso_match.groups()):
        hours = float(iso_match.group(1) or 0)
        minutes = float(iso_match.group(2) or 0)
        seconds = float(iso_match.group(3) or 0)
        return max(0, round((hours * 3600.0 + minutes * 60.0 + seconds) * 1000))
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0, round(float(text) * 1000))
    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60.0 + float(part)
        return max(0, round(seconds * 1000))
    return None


def clean_html_description(raw: Any, *, preserve_newlines: bool = False) -> str | None:
    """Strip HTML tags, unescape entities, and normalize whitespace in text/descriptions."""
    if not isinstance(raw, str) or not raw:
        return None
    if preserve_newlines:
        text = _HTML_BREAK_NEWLINE_RE.sub("\n", raw)
        text = _HTML_TAG_RE.sub("", text)
        text = unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip() or None
    text = _HTML_BLOCK_SPACE_RE.sub(" ", raw)
    text = _HTML_TAG_RE.sub("", text)
    cleaned = " ".join(unescape(text).split())
    return cleaned or None


def parse_series_order(raw: Any) -> int | None:
    """Parse an integer series index from an `int`, integer-valued `float`, or numeric string."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if math.isfinite(raw) and raw.is_integer() else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return int(num) if math.isfinite(num) and num.is_integer() else None


@define
class AudiobookMetadata:
    title: str
    scrape_url: str | None = None
    series: str | None = None
    series_order: int | None = None
    authors: list[str] = Factory(list)
    narrators: list[str] = Factory(list)
    genres: list[str] = Factory(list)
    language: Optional["pycountry.db.Language"] = field(default=None, converter=parse_language) # type: ignore
    description: str | None = None
    isbn: str | None = None
    asin: str | None = None
    publisher: str | None = None
    release_date: date | None = field(default=None, converter=parse_release_date)
    source: str | None = None

    def add_author(self, author: str):
        """Add author to metadata"""
        self.authors.append(author)

    def add_narrator(self, narrator: str):
        """Add narrator to metadata"""
        self.narrators.append(narrator)

    def add_genre(self, genre: str):
        """Add genre to metadata"""
        self.genres.append(genre)

    def add_authors(self, authors: Sequence[str]):
        self.authors.extend(authors)

    def add_narrators(self, narrators: Sequence[str]):
        self.narrators.extend(narrators)

    def add_genres(self, genres: Sequence[str]):
        self.genres.extend(genres)

    def all_properties(self, allow_duplicate_keys: bool | None = False) -> list[tuple[str, Any]]:
        result: list[tuple[str, Any]] = []
        add = add_if_value_exists(self, result)
        add("title")
        add("scrape_url")
        add("series")
        add("series_order")
        add("language")
        add("description")
        add("isbn")
        add("asin")
        add("publisher")
        add("release_date")
        add("source")
        if allow_duplicate_keys is None: # return original lists
            add("authors")
            add("narrators")
            add("genres")
        elif allow_duplicate_keys is True: # return lists as multiple keys
            for author in self.authors:
                result.append(("author", author))
            for narrator in self.narrators:
                result.append(("narrator", narrator))
            for genre in self.genres:
                result.append(("genre", genre))
        else: # return lists concatenated into a string
            result.append(("author", self.author))
            result.append(("narrator", self.narrator))
            result.append(("genre", self.genre))
        return result

    def all_properties_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for (key, value) in self.all_properties(allow_duplicate_keys=False):
            result[key] = value
        return result

    @property
    def author(self) -> str:
        """All authors concatenated into a single string"""
        return "; ".join(self.authors)

    @property
    def narrator(self) -> str:
        """All narrators concatenated into a single string"""
        return "; ".join(self.narrators)

    @property
    def genre(self) -> str:
        """All genres concatenated into a single string"""
        return "; ".join(self.genres)


    def as_dict(self, chapters: Sequence[Chapter] | None = None) -> dict:
        """
        Export metadata as dictionary (compatible with Audiobookshelf/Plex readers).

        :returns: Metadata as dictionary
        """
        result: dict = {
            "title": self.title,
            "authors": self.authors,
            "narrators": self.narrators,
            "genres": self.genres,
        }
        if self.scrape_url:
            result["scrape_url"] = self.scrape_url
        if self.series:
            # Audiobookshelf expects series as a string array, e.g. ["Name #1"]
            if self.series_order is not None:
                result["series"] = [f"{self.series} #{self.series_order}"]
                result["series_order"] = self.series_order
            else:
                result["series"] = [self.series]
        elif self.series_order is not None:
            result["series_order"] = self.series_order
        if self.language:
            result["language"] = self.language
        if self.description:
            result["description"] = self.description
        if self.isbn:
            result["isbn"] = self.isbn
        if self.asin:
            result["asin"] = self.asin
        if self.publisher:
            result["publisher"] = self.publisher
        if self.release_date:
            result["release_date"] = self.release_date
        if self.source:
            result["source"] = self.source
        if chapters:
            result["chapters"] = [
                {
                    "id": idx,
                    "start_ms": ch.start,
                    "start_seconds": round(ch.start / 1000.0, 3),
                    "title": ch.title,
                }
                for idx, ch in enumerate(chapters, start=1)
            ]

        return result


    def as_json(self, chapters: Sequence[Chapter] | None = None) -> str:
        """
        Export metadata as json

        :returns: Metadata as json
        """
        class AudiobookMetadataJSONEncoder(json.JSONEncoder):
            def default(self, z):
                if isinstance(z, date):
                    return str(z)
                elif isinstance(z, pycountry.db.Data) and z.__class__.__name__ == "Language":
                    return z.alpha_3
                else:
                    return super().default(z)
        return json.dumps(self.as_dict(chapters=chapters), cls=AudiobookMetadataJSONEncoder, ensure_ascii=False)


def add_if_value_exists(metadata: AudiobookMetadata, l: list[tuple[str, Any]]):
    def add(key: str):
        value = getattr(metadata, key, None)
        if value is not None and value != "" and value != []:
            l.append((key, value))
    return add


@define(frozen=True)
class TrackTagContext:
    """Per-track tag overrides used when tagging multi-file or split-chapter outputs."""

    title: str
    track_number: int
    total_tracks: int
    album: str | None = None


@define
class Audiobook:
    session: requests.Session
    metadata: AudiobookMetadata
    files: list[AudiobookFile]
    chapters: list[Chapter] = Factory(list)
    cover: Cover | None = None
    source_data: Any = None

    @property
    def title(self) -> str:
        return self.metadata.title

T = TypeVar("T")

@define
class BookId(Generic[T]):
    id: T
    title: str | None = None

@define
class Series(Generic[T]):
    # Title of series
    title: str
    # Internal ids of book in series
    books: Sequence[BookId[T] | Audiobook]

Result = Audiobook | Series

EBOOK_EXTENSIONS = frozenset({"epub", "pdf", "mobi", "azw3", "cbz", "cbr"})


def is_ebook_extension(ext: str | None) -> bool:
    """Return True when `ext` represents an ebook file rather than audio."""
    if not ext:
        return False
    return ext.lstrip(".").lower() in EBOOK_EXTENSIONS


def is_ebook(audiobook: Audiobook) -> bool:
    """Return True when `audiobook` consists only of ebook file(s) (e.g. `.epub`)."""
    return bool(audiobook.files) and all(is_ebook_extension(f.ext) for f in audiobook.files)

from datetime import date, datetime
import re
import requests
from typing import Generic, Optional, TypeVar, Any
from collections.abc import Callable, Sequence, MutableMapping
import json
from attrs import define, Factory
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


@define
class AudiobookMetadata:
    title: str
    scrape_url: str | None = None
    series: str | None = None
    series_order: int | None = None
    authors: list[str] = Factory(list)
    narrators: list[str] = Factory(list)
    genres: list[str] = Factory(list)
    language: Optional["pycountry.db.Language"] = None # type: ignore
    description: str | None = None
    isbn: str | None = None
    asin: str | None = None
    publisher: str | None = None
    release_date: date | None = None
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
        if value:
            l.append((key, value))
    return add


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


def parse_release_date(raw: Any) -> date | None:
    """Parse a release date (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`, `int`, or `date`) cleanly."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, int) and not isinstance(raw, bool):
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


def is_ebook(audiobook: Audiobook) -> bool:
    """Return True when `audiobook` consists only of ebook file(s) (e.g. `.epub`)."""
    return bool(audiobook.files) and all(is_ebook_extension(f.ext) for f in audiobook.files)

from datetime import date
import requests
from typing import Callable, Dict, Generic, List, Optional, Union, Sequence, Tuple, TypeVar, Any, MutableMapping
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
    title: Optional[str] = None
    # Headers for request
    headers: MutableMapping[str, Union[str, bytes]] = Factory(dict)
    # Encryption method
    encryption_method: Optional[AudiobookFileEncryption] = None
    # Expected content-type of the download request
    expected_content_type: Optional[str] = None
    # Expected status code of the download request
    expected_status_code: int = 200
    # Optional lazy URL resolver invoked when downloading starts
    url_resolver: Optional[Callable[[], str]] = None


@define
class AudiobookMetadata:
    title: str
    scrape_url: Optional[str] = None
    series: Optional[str] = None
    series_order: Optional[int] = None
    authors: List[str] = Factory(list)
    narrators: List[str] = Factory(list)
    genres: List[str] = Factory(list)
    language: Optional["pycountry.db.Language"] = None # type: ignore
    description: Optional[str] = None
    isbn: Optional[str] = None
    asin: Optional[str] = None
    publisher: Optional[str] = None
    release_date: Optional[date] = None
    source: Optional[str] = None

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

    def all_properties(self, allow_duplicate_keys: Optional[bool] = False) -> List[Tuple[str, Any]]:
        result: List[Tuple[str, Any]] = []
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

    def all_properties_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
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


    def as_dict(self, chapters: Optional[Sequence[Chapter]] = None) -> dict:
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


    def as_json(self, chapters: Optional[Sequence[Chapter]] = None) -> str:
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


def add_if_value_exists(metadata: AudiobookMetadata, l: List[Tuple[str, Any]]):
    def add(key: str):
        value = getattr(metadata, key, None)
        if value:
            l.append((key, value))
    return add


@define
class Audiobook:
    session: requests.Session
    metadata: AudiobookMetadata
    files: List[AudiobookFile]
    chapters: List[Chapter] = Factory(list)
    cover: Optional[Cover] = None
    source_data: Any = None

    @property
    def title(self) -> str:
        return self.metadata.title

T = TypeVar("T")

@define
class BookId(Generic[T]):
    id: T
    title: Optional[str] = None

@define
class Series(Generic[T]):
    # Title of series
    title: str
    # Internal ids of book in series
    books: Sequence[Union[BookId[T], Audiobook]]

Result = Union[
    Audiobook,
    Series
]

EBOOK_EXTENSIONS = frozenset({"epub", "pdf", "mobi", "azw3", "cbz", "cbr"})


def is_ebook_extension(ext: Optional[str]) -> bool:
    """Return True when `ext` represents an ebook file rather than audio."""
    if not ext:
        return False
    return ext.lstrip(".").lower() in EBOOK_EXTENSIONS


def is_ebook(audiobook: Audiobook) -> bool:
    """Return True when `audiobook` consists only of ebook file(s) (e.g. `.epub`)."""
    return bool(audiobook.files) and all(is_ebook_extension(f.ext) for f in audiobook.files)

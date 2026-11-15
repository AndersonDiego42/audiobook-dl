from .source import CatalogBook, Source
from audiobookdl import (
    Audiobook,
    AudiobookFile,
    AudiobookMetadata,
    BookId,
    Chapter,
    Cover,
    Result,
    Series,
    logging,
)
from audiobookdl.exceptions import DataNotPresent, GenericAudiobookDLException, NoSourceFound
from rich.markup import escape

import json
import lxml.html
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urljoin
from urllib3.util import parse_url

BASEURL = "https://www.audiobooks.com/book/stream/"


def _parse_timestamp_ms(raw: Any) -> Optional[int]:
    """Parse numeric seconds or `HH:MM:SS` / `MM:SS` strings into milliseconds."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return max(0, round(val * 1000))
    text = str(raw).strip()
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


def _split_series_order(raw_series: str) -> Tuple[str, Optional[int]]:
    cleaned = " ".join(raw_series.split())
    match = re.match(r"^(.*?)\s*(?:#|,\s*Book\s+|Book\s+)(\d+(?:\.\d+)?)\s*$", cleaned, re.IGNORECASE)
    if match and match.group(1).strip():
        num_str = match.group(2)
        order = int(num_str) if "." not in num_str else int(float(num_str))
        return match.group(1).strip(), order
    return cleaned, None


class AudiobooksdotcomSource(Source):
    match = [
        r"{}\d+(/\d)?".format(BASEURL),
        r"https?://(www\.)?audiobooks\.com/audiobook/.+",
        r"https?://(www\.)?audiobooks\.com/browse/library.*",
    ]
    names = ["audiobooks.com"]
    supports_library = True

    def download(self, url: str) -> Result:
        path = parse_url(url).path
        if not path:
            raise NoSourceFound

        if "/browse/library" in path:
            self._apply_cookie_user_agent()
            return self.download_library(url)

        if "/book/stream/" in path:
            book_id = path.split("/")[3]
        elif "/audiobook/" in path:
            book_id = path.split("/")[-1]
        else:
            raise NoSourceFound
        return self.download_from_id(book_id)

    def _apply_cookie_user_agent(self) -> None:
        user_agent = self.extract_useragent_from_cookies()
        logging.debug(f"{user_agent=}")
        self._session.headers.update({"User-Agent": user_agent})

    def download_from_id(self, book_id: str) -> Audiobook:
        self._apply_cookie_user_agent()
        scrape_url = f"{BASEURL}{book_id}/1"
        return Audiobook(
            session=self._session,
            metadata=self.extract_metadata(scrape_url),
            cover=self.download_cover(scrape_url),
            files=self.extract_file(scrape_url),
            chapters=self.extract_chapters(scrape_url),
        )

    def _scrape_library_entries(self, url: str) -> List[Tuple[str, str, Optional[str], str]]:
        """Scrape library pages in display order returning `(book_id, title, author, book_url)`."""
        logging.debug(f"Downloading library from {url}")
        entries: List[Tuple[str, str, Optional[str], str]] = []
        seen_ids = set()
        page_url: Optional[str] = url
        parsed_initial_url = parse_url(url)
        origin = f"{parsed_initial_url.scheme}://{parsed_initial_url.host}"

        while page_url:
            logging.debug(f"Scraping page {page_url}")
            page_html = self._get_page(page_url).decode("utf-8", errors="replace")
            tree = lxml.html.fromstring(page_html)
            for card in tree.cssselect("div.book"):
                links = card.cssselect("a.no-decoration")
                if not links:
                    continue
                link = links[0]
                href = (link.get("href") or "").strip()
                if "/audiobook/" not in href:
                    continue
                book_id = href.rstrip("/").split("/")[-1]
                if not book_id or book_id in seen_ids:
                    continue
                seen_ids.add(book_id)

                title_elems = card.cssselect(".book-title, h3, h4, .title")
                title = " ".join(title_elems[0].text_content().split()) if title_elems else ""
                if not title:
                    imgs = card.cssselect("img[alt]")
                    if imgs:
                        title = " ".join((imgs[0].get("alt") or "").split())
                if not title:
                    title = " ".join(link.text_content().split()) or book_id

                author_elems = card.cssselect(".book-author, .author, a[href*='/author/']")
                author = " ".join(author_elems[0].text_content().split()) if author_elems else None
                if author == "":
                    author = None

                full_url = urljoin(origin, href)
                entries.append((book_id, title, author, full_url))

            next_links = tree.cssselect("li.page-item.next > a.page-link")
            next_href = (next_links[0].get("href") or "").strip() if next_links else ""
            if next_href:
                next_candidate = urljoin(origin, next_href)
                page_url = next_candidate if next_candidate != page_url else None
            else:
                page_url = None

        return entries

    def download_library(self, url: str) -> Series:
        entries = self._scrape_library_entries(url)
        books = [BookId(book_id, title=title) for book_id, title, _author, _url in entries]
        return Series(
            title="audiobooks.com Library",
            books=books,
        )

    @staticmethod
    def _extract_jsonld_blocks(tree: lxml.html.HtmlElement) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for script in tree.cssselect('script[type="application/ld+json"]'):
            raw = (script.text or "").strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                blocks.append(parsed)
            elif isinstance(parsed, list):
                blocks.extend(item for item in parsed if isinstance(item, dict))
        return blocks

    def extract_metadata(self, scrape_url: str) -> AudiobookMetadata:
        page_html = self._get_page(scrape_url).decode("utf-8", errors="replace")
        tree = lxml.html.fromstring(page_html)

        title_elems = tree.cssselect("h2#bookTitle") or tree.cssselect("h1")
        title = " ".join(title_elems[0].text_content().split()) if title_elems else ""

        authors: List[str] = []
        narrators: List[str] = []
        description: Optional[str] = None
        series_name: Optional[str] = None
        series_order: Optional[int] = None
        isbn: Optional[str] = None
        publisher: Optional[str] = None

        for block in self._extract_jsonld_blocks(tree):
            if not title and isinstance(block.get("name"), str):
                title = block["name"].strip()
            if not description and isinstance(block.get("description"), str):
                description = " ".join(block["description"].split()) or None
            if not isbn and block.get("isbn"):
                isbn = str(block["isbn"]).strip() or None
            if not publisher:
                pub = block.get("publisher")
                if isinstance(pub, dict):
                    publisher = str(pub.get("name") or "").strip() or None
                elif isinstance(pub, str):
                    publisher = pub.strip() or None
            raw_authors = block.get("author")
            if raw_authors:
                items = raw_authors if isinstance(raw_authors, list) else [raw_authors]
                for item in items:
                    name = item.get("name") if isinstance(item, dict) else item
                    if isinstance(name, str) and name.strip() and name.strip() not in authors:
                        authors.append(name.strip())
            raw_narrators = block.get("readBy") or block.get("narrator")
            if raw_narrators:
                items = raw_narrators if isinstance(raw_narrators, list) else [raw_narrators]
                for item in items:
                    name = item.get("name") if isinstance(item, dict) else item
                    if isinstance(name, str) and name.strip() and name.strip() not in narrators:
                        narrators.append(name.strip())

        if not title:
            og_title = tree.cssselect('meta[property="og:title"]')
            if og_title:
                title = (og_title[0].get("content") or "").strip()
        if not title:
            raise DataNotPresent

        if not authors:
            for elem in tree.cssselect('.book-author a, .book-author, .author a, a[href*="/author/"], [itemprop="author"]'):
                text = " ".join(elem.text_content().split())
                text = re.sub(r"^(?:By|Written\s+by)\s*:?\s*", "", text, flags=re.IGNORECASE).strip()
                if text and text not in authors:
                    authors.append(text)

        if not narrators:
            for elem in tree.cssselect('.book-narrator a, .book-narrator, .narrator a, a[href*="/narrator/"]'):
                text = " ".join(elem.text_content().split())
                text = re.sub(r"^(?:Narrated\s+by|Read\s+by)\s*:?\s*", "", text, flags=re.IGNORECASE).strip()
                if text and text not in narrators:
                    narrators.append(text)

        for elem in tree.cssselect('.book-series a, .book-series, a[href*="/series/"]'):
            raw_series = " ".join(elem.text_content().split())
            if raw_series:
                series_name, series_order = _split_series_order(raw_series)
                if series_name:
                    break

        if not description:
            for selector in ("#book-description", ".book-description", ".description", '[itemprop="description"]'):
                desc_elems = tree.cssselect(selector)
                if desc_elems:
                    text = " ".join(desc_elems[0].text_content().split())
                    if text:
                        description = text
                        break
        if not description:
            meta_desc = tree.cssselect('meta[name="description"]')
            if meta_desc:
                description = " ".join((meta_desc[0].get("content") or "").split()) or None

        metadata = AudiobookMetadata(
            title=title,
            authors=authors,
            narrators=narrators,
            description=description,
            series=series_name,
            series_order=series_order,
            isbn=isbn,
            publisher=publisher,
            source="audiobooks.com",
            scrape_url=scrape_url,
        )
        return metadata

    def extract_chapters(self, scrape_url: str) -> List[Chapter]:
        """Extract chapter timestamps from inline script payloads or chapter list elements."""
        page_html = self._get_page(scrape_url).decode("utf-8", errors="replace")
        chapters: List[Chapter] = []

        # Try inline JS chapters array (e.g., chapters: [{"title": "...", "start": 0}])
        js_match = re.search(r"chapters\s*:\s*(\[[^\]]*\])", page_html, re.DOTALL)
        if js_match:
            try:
                parsed = json.loads(js_match.group(1))
            except ValueError:
                parsed = []
            if isinstance(parsed, list):
                for idx, entry in enumerate(parsed, start=1):
                    if not isinstance(entry, dict):
                        continue
                    start_ms = _parse_timestamp_ms(
                        entry.get("start")
                        if entry.get("start") is not None
                        else entry.get("start_time", entry.get("offset"))
                    )
                    if start_ms is None:
                        continue
                    ch_title = str(entry.get("title") or entry.get("name") or f"Chapter {idx}").strip()
                    chapters.append(Chapter(start=start_ms, title=ch_title))
                if chapters:
                    return chapters

        tree = lxml.html.fromstring(page_html)
        for idx, elem in enumerate(
            tree.cssselect("ul#chapters li, div.chapter-list li, select#chapterSelect option, [data-start]"),
            start=1,
        ):
            start_raw = elem.get("data-start") or elem.get("data-time") or elem.get("value")
            start_ms = _parse_timestamp_ms(start_raw)
            if start_ms is None:
                continue
            ch_title = " ".join(elem.text_content().split()) or f"Chapter {idx}"
            chapters.append(Chapter(start=start_ms, title=ch_title))
        return chapters

    def download_cover(self, scrape_url: str) -> Cover:
        raw_cover_url = self.find_elem_in_page(
            scrape_url,
            "img.bookimage",
            data="src",
        )
        cover_url = urljoin(scrape_url, raw_cover_url)
        return Cover(self.get(cover_url), "jpg")

    def extract_useragent_from_cookies(self) -> str:
        """
        Extracts user agent from cookies in local session.

        :returns: User-Agent string
        """
        raw: Union[str, None] = self._session.cookies.get("ci_session", domain="www.audiobooks.com")
        if not raw:
            raise GenericAudiobookDLException("ci_session missing from cookie")
        decoded_cookie = unquote(raw)
        match = re.search(r's:10:"user_agent";s:\d+:"([^"]+)";', decoded_cookie)
        if match:
            return match.group(1)
        try:
            return decoded_cookie.split('"')[11]
        except IndexError:
            raise GenericAudiobookDLException("Could not extract user_agent from ci_session cookie")

    def extract_file(self, scrape_url: str) -> List[AudiobookFile]:
        """
        Extract audio url from html page using the cached page response.

        :param scrape_url: Url of page to scrape for audio link
        :returns: List of audio files with a single file in it
        """
        page_text = self._get_page(scrape_url).decode("utf-8", errors="replace")
        audio_match = re.search(
            r'(?<=(mp3: "))[^"]+',
            page_text,
        )
        if audio_match is None:
            logging.debug(f"Could not find audio URL. Page content from {scrape_url} was:\n{escape(page_text)}")
            raise DataNotPresent
        audio_url = audio_match.group()
        return [AudiobookFile(url=audio_url, ext="mp3")]

    def list_library(self) -> List[CatalogBook]:
        if self._session.cookies.get("ci_session", domain="www.audiobooks.com"):
            self._apply_cookie_user_agent()
        entries = self._scrape_library_entries("https://www.audiobooks.com/browse/library")
        return [
            CatalogBook(
                title=title,
                author=author or "",
                url=book_url,
                source="audiobooks.com",
            )
            for _book_id, title, author, book_url in entries
        ]

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
from audiobookdl.exceptions import DataNotPresent, GenericAudiobookDLException, NoSourceFound, RequestError, UserNotAuthorized
from audiobookdl.utils.audiobook import parse_duration_ms
from audiobookdl.utils.http import DEFAULT_USER_AGENT, extract_session_cookies, get_header_ci

import json
import lxml.html
import re
from typing import Any
from urllib.parse import unquote, urljoin
from urllib3.util import parse_url

BASEURL = "https://www.audiobooks.com/book/stream/"


def _split_series_order(raw_series: str) -> tuple[str, int | None]:
    cleaned = " ".join(raw_series.split())
    match = re.match(r"^(.*?)\s*(?:#|,\s*Book\s+|Book\s+)(\d+(?:\.\d+)?)\s*$", cleaned, re.IGNORECASE)
    if match and match.group(1).strip():
        num_str = match.group(2)
        order = int(num_str) if "." not in num_str else int(float(num_str))
        return match.group(1).strip(), order
    return cleaned, None


class AudiobooksdotcomSource(Source):
    match = [
        rf"{BASEURL}\d+(/\d+)?/?",
        r"https?://(www\.)?audiobooks\.com/audiobook/.+",
        r"https?://(www\.)?audiobooks\.com/browse/library.*",
    ]
    names = ["audiobooks.com"]
    supports_library = True

    def verify_auth(self, url: str = "") -> bool:
        if not self._session.cookies and not self.authenticated:
            return False
        if self._get_ci_session_cookie():
            self._apply_cookie_user_agent()
        try:
            resp = self.request(
                "GET",
                "https://www.audiobooks.com/browse/library",
                allow_redirects=False,
            )
        except (RequestError, UserNotAuthorized):
            return False
        if resp.status_code in (301, 302, 303, 307, 308):
            raw_loc = get_header_ci(resp.headers, "location")
            location = raw_loc.lower() if isinstance(raw_loc, str) else ""
            return not any(tok in location for tok in ("login", "signin", "sign-in"))
        if resp.status_code in (401, 403) or resp.status_code != 200:
            return False
        if "/login" in (getattr(resp, "url", "") or "").lower():
            return False
        body = (getattr(resp, "content", b"") or b"").decode("utf-8", errors="replace").lower()
        if ("sign in" in body or "log in" in body) and 'div class="book"' not in body:
            return False
        return True

    def download(self, url: str) -> Result:
        path = parse_url(url).path
        if not path:
            raise NoSourceFound

        clean_path = path.rstrip("/")
        if "/browse/library" in clean_path:
            self._apply_cookie_user_agent()
            return self.download_library(url)

        if "/book/stream/" in clean_path:
            segments = [seg for seg in clean_path.split("/") if seg]
            if len(segments) < 3:
                raise NoSourceFound
            book_id = segments[2]
        elif "/audiobook/" in clean_path:
            book_id = clean_path.split("/")[-1]
        else:
            raise NoSourceFound
        if not book_id:
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

    def _scrape_library_entries(self, url: str) -> list[tuple[str, str, str | None, str]]:
        """Scrape library pages in display order returning `(book_id, title, author, book_url)`."""
        logging.debug(f"Downloading library from {url}")
        entries: list[tuple[str, str, str | None, str]] = []
        seen_ids = set()
        page_url: str | None = url
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
    def _extract_jsonld_blocks(tree: lxml.html.HtmlElement) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
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

        authors: list[str] = []
        narrators: list[str] = []
        description: str | None = None
        series_name: str | None = None
        series_order: int | None = None
        isbn: str | None = None
        publisher: str | None = None

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

    def extract_chapters(self, scrape_url: str) -> list[Chapter]:
        """Extract chapter timestamps from inline script payloads or chapter list elements."""
        page_html = self._get_page(scrape_url).decode("utf-8", errors="replace")
        chapters: list[Chapter] = []

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
                    start_ms = parse_duration_ms(
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
            start_ms = parse_duration_ms(start_raw)
            if start_ms is None:
                continue
            ch_title = " ".join(elem.text_content().split()) or f"Chapter {idx}"
            chapters.append(Chapter(start=start_ms, title=ch_title))
        return chapters

    def download_cover(self, scrape_url: str) -> Cover | None:
        try:
            raw_cover_url = self.find_elem_in_page(
                scrape_url,
                "img.bookimage",
                data="src",
            )
        except DataNotPresent:
            return None
        cover_url = urljoin(scrape_url, raw_cover_url)
        return self.fetch_cover_safe(cover_url)

    def _get_ci_session_cookie(self) -> str | None:
        fallback: str | None = None
        for cookie in extract_session_cookies(self._session):
            if cookie.name != "ci_session" or not cookie.value:
                continue
            domain = (cookie.domain or "").lower()
            if domain == "www.audiobooks.com":
                return str(cookie.value)
            if not domain or domain == "audiobooks.com" or domain.endswith(".audiobooks.com"):
                fallback = str(cookie.value)
        return fallback

    def extract_useragent_from_cookies(self) -> str:
        """
        Extracts user agent from cookies in local session.

        :returns: User-Agent string
        """
        raw = self._get_ci_session_cookie()
        if not raw:
            raise GenericAudiobookDLException("ci_session missing from cookie")
        decoded_cookie = unquote(raw)
        match = re.search(r's:10:"user_agent";s:\d+:"([^"]+)";', decoded_cookie)
        if match:
            return match.group(1)
        try:
            parsed = json.loads(decoded_cookie)
            if isinstance(parsed, dict):
                ua = parsed.get("user_agent") or parsed.get("userAgent")
                if isinstance(ua, str) and ua.strip():
                    return ua.strip()
        except ValueError:
            pass
        parts = decoded_cookie.split('"')
        if len(parts) > 11 and parts[11].strip():
            return parts[11]
        existing = self._session.headers.get("User-Agent")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
        return DEFAULT_USER_AGENT

    def _discover_part_urls(self, book_id: str, part1_url: str) -> list[tuple[int, str]]:
        """Discover all stream part URLs (`/book/stream/<id>/<n>`) linked from Part 1."""
        page_text = self._get_page(part1_url).decode("utf-8", errors="replace")
        parts: dict[int, str] = {1: part1_url}
        pattern = re.compile(rf"/book/stream/{re.escape(book_id)}/(\d+)(?:[/?#\"'\s]|$)")
        for match in pattern.finditer(page_text):
            part_num = int(match.group(1))
            if part_num >= 1 and part_num not in parts:
                parts[part_num] = f"{BASEURL}{book_id}/{part_num}"
        return sorted(parts.items())

    def extract_file(self, scrape_url: str) -> list[AudiobookFile]:
        """
        Extract audio urls across all discovered stream parts of the book.

        :param scrape_url: Url of Part 1 stream page
        :returns: List of audio files
        """
        id_match = re.search(r"/book/stream/([^/]+)/", scrape_url)
        if id_match:
            part_urls = self._discover_part_urls(id_match.group(1), scrape_url)
        else:
            part_urls = [(1, scrape_url)]

        files: list[AudiobookFile] = []
        multi_part = len(part_urls) > 1
        for part_num, part_url in part_urls:
            page_text = self._get_page(part_url).decode("utf-8", errors="replace")
            audio_match = re.search(
                r'(?<=(mp3: "))[^"]+',
                page_text,
            )
            if audio_match is None:
                logging.debug(
                    f"Could not find audio URL. Page content from {part_url} was:\n{logging.escape_markup(page_text)}"
                )
                raise DataNotPresent
            audio_url = audio_match.group()
            files.append(
                AudiobookFile(
                    url=audio_url,
                    ext="mp3",
                    title=f"Part {part_num}" if multi_part else None,
                )
            )
        return files

    def list_library(self) -> list[CatalogBook]:
        if self._get_ci_session_cookie():
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

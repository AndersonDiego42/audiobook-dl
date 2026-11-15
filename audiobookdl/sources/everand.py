from .source import Source
from audiobookdl import AudiobookFile, Chapter, logging, AudiobookMetadata, Cover, Audiobook, Series, Result, BookId
from audiobookdl.exceptions import RequestError
from typing import List, Optional

import re
import json

class EverandSource(Source[str]):
    match = [
        r"https?://(www\.)?(scribd|everand)\.com/listen/\d+",
        r"https?://(www\.)?(scribd|everand)\.com/audiobook/\d+/",
        r"https?://(www\.)?(scribd|everand)\.com/series/\d+"
    ]
    names = [ "Everand", "Scribd" ]

    def download(self, url: str) -> Result:
        # Matches series url
        if re.match(self.match[2], url):
            return self.download_series(url)
        else:
            return self.download_book_from_url(url)

    def download_from_id(self, book_id: str) -> Audiobook:
        return self.download_book_from_url(
            f"https://www.everand.com/listen/{book_id}"
        )

    def download_series(self, url: str) -> Series[str]:
        series_id: str = url.split("/")[-2]
        logging.debug(f"{series_id=}")

        return Series(
            title = self.find_elem_in_page(url, "h1"),
            books = self.download_series_books(series_id)
        )


    def download_series_books(self, series_id: str) -> list:
        """
        Downloads ids of books in series

        :param series_id: Id of series
        :returns: Book ids of books in series
        """
        response = self.get_json(
            f"https://www.everand.com/series/{series_id}/data",
            headers = {
                "X-Requested-With": "XMLHttpRequest"
            }
        )
        return [
            BookId(i["id"])
            for i in response["compilation"]["modules"][0]["documents"]
        ]


    def download_book_from_url(self, url: str) -> Audiobook:
        """
        Download audiobook

        :param url: Url to information page or listening page of audiobook
        :return: Audiobook object
        """
        url = self.create_listen_url(url)
        book_info = self.extract_info(url)
        metadata = book_info["doc"]
        logging.debug(f"{metadata=}")
        csrf = self.post_json(
            "https://www.everand.com/csrf_token",
            headers = { "href": url }
        )
        logging.debug(f"csrf_received={bool(csrf)}")
        return Audiobook(
            session = self._session,
            files = self.get_files(book_info),
            metadata = self.format_metadata(metadata),
            cover = self.download_cover(metadata),
            chapters = self.get_chapters(book_info),
        )


    def extract_info(self, url: str) -> dict:
        """
        Extract information from listening page

        :param url: Url of listening page
        :return: Metadata from page as dictionary
        """
        raw = self.find_in_page(
            url,
            r'(?<=(Scribd.Audiobooks.Show, )){.+}(?=\))'
        )
        return json.loads(raw)


    def get_files(self, book_info: dict) -> List[AudiobookFile]:
        """
        Format audio files

        :param book_info: Information extracted from listening page
        """
        if book_info["jwt_token"]["token"] is not None:
            return self.get_internal_files(book_info)
        else:
            return self.get_external_files(book_info)


    def get_internal_files(self, book_info: dict) -> List[AudiobookFile]:
        """
        Format audio files for internal books

        :param book_info: Information extracted from listening page
        """
        book_id = book_info["share_opts"]["id"]
        jwt = book_info["jwt_token"]["token"]
        headers = { "Authorization": jwt }
        master_url = f"https://audio.production.scribd.com/audiobooks/{book_id}/master.m3u8"
        try:
            return self.get_stream_files(master_url, headers=headers)
        except RequestError:
            fallback_url = f"https://audio.production.scribd.com/audiobooks/{book_id}/96kbps.m3u8"
            return self.get_stream_files(fallback_url, headers=headers)


    def get_external_files(self, book_info: dict) -> List[AudiobookFile]:
        """
        Format audio files for external books

        :param book_info: Information extracted from listening page
        """
        external_id = book_info["audiobook"]["external_id"]
        logging.debug(f"{external_id=}")
        account_id = book_info["audiobook"]["account_id"]
        logging.debug(f"{account_id=}")
        session_key = book_info["audiobook"]["session_key"]
        logging.debug(f"session_key={logging.mask_secret(session_key)}")
        headers = { "Session-Key": session_key }
        license_id = self.get_json(
            f"https://api.findawayworld.com/v4/accounts/{account_id}/audiobooks/{external_id}",
            headers = headers,
        )["licenses"][0]["id"]
        response = self.post_json(
            f"https://api.findawayworld.com/v4/audiobooks/{external_id}/playlists",
            json = { "license_id": license_id },
            headers = headers,
        )
        files = []
        for i in response["playlist"]:
            chapter = i["chapter_number"]
            files.append(AudiobookFile(
                url = i["url"],
                title = f"Chapter {chapter}",
                ext = "mp3",
            ))
        return files



    def create_listen_url(self, url: str) -> str:
        """
        Change url to listen page if information page is used

        :param url: Url of information or listen page
        :return: Listen page url
        """
        if re.match(self.match[1], url):
            url_id = url.split("/")[4]
            return f"https://www.everand.com/listen/{url_id}"
        return url


    def download_cover(self, metadata: dict) -> Optional[Cover]:
        """
        Download and clean cover

        :param metadata: Metadata dictionary containing `cover_url`
        :returns: Cover of book
        """
        # Downloading image from Everand
        cover_url = metadata["cover_url"]
        raw_cover = self.get(cover_url)
        if raw_cover is None:
            return None
        return Cover(raw_cover, "jpg")


    @staticmethod
    def clean_title(title: str):
        """
        Move ', The' from the end to the beginning of the title

        :param title: Original title
        :returns: Fixed title
        """
        if title[-3:] == ", A":
            return f"A {title[:-3]}"
        if title[-5:] == ", The":
            return f"The {title[:-5]}"
        return title


    @staticmethod
    def format_metadata(book_info: dict) -> AudiobookMetadata:
        return AudiobookMetadata(
            title = EverandSource.clean_title(book_info["title"]),
            authors = [ book_info["author"]["name"] ],
            narrators = [ narrator["name"] for narrator in book_info["narrators"] ],
        )


    @staticmethod
    def get_chapter_title(chapter) -> str:
        """Extract title for chapter"""
        number = chapter["chapter_number"]
        if number == 0:
            return "Introduction"
        return f"Chapter {number}"


    @staticmethod
    def get_chapters(book_info) -> List[Chapter]:
        chapters = []
        raw_chapters = book_info.get("chapters")
        if raw_chapters is None and isinstance(book_info.get("audiobook"), dict):
            raw_chapters = book_info["audiobook"].get("chapters")
        if raw_chapters:
            start_time = 0
            for chapter in raw_chapters:
                title = EverandSource.get_chapter_title(chapter)
                chapters.append(Chapter(start_time, title))
                start_time += chapter["duration"]
        return chapters

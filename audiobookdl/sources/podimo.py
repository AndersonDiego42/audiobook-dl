from .source import Source
from audiobookdl import logging
from audiobookdl.exceptions import NoSourceFound, DataNotPresent, RequestError
from audiobookdl.utils import read_asset_file
from audiobookdl.utils.audiobook import Audiobook, AudiobookFile, AudiobookMetadata, Cover, Series, BookId, Result
from audiobookdl.utils import http
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT

import re
from datetime import datetime
from typing import List
from urllib3.util import parse_url

class PodimoSource(Source[dict]):
    match = [
        "https://open.podimo.com/audiobook/[^/]+",
        "https://open.podimo.com/podcast/[^/]+",
        "https://share.podimo.com/s/[^/]+",
    ]
    names = [ "Podimo" ]
    _authentication_methods = [
        "login"
    ]


    def _login(self, url: str, username: str, password: str) -> None:
        data = self.graphql_request(
            operation_name = "web_logInUser",
            query = "login",
            variables = {
                "email": username,
                "password": password
            }
        )
        authorization_token = data["data"]["tokenWithCredentials"]["token"]
        logging.debug(f"authorization_token={logging.mask_secret(authorization_token)}")
        self._session.headers.update({"authorization": authorization_token})


    def graphql_request(self, operation_name: str, query: str, variables: dict) -> dict:
        """
        Make graphql request to Podimo

        :param operation_name: Name of operation
        :param query: File query is stored in
        :param variables: Variables for query
        :returns: Parsed JSON response from server
        """
        return self.post_json(
            "https://podimo.com/graphql",
            headers = {"User-Agent": "JS GraphQL"},
            json = {
                "operationName": operation_name,
                "query": read_asset_file(f"assets/sources/podimo/{query}.graphql"),
                "variables": variables
            }
        )


    @staticmethod
    def extract_id_from_url(url: str) -> str:
        """Extract audiobook id from url"""
        return url.split("/")[-1]


    def download(self, url: str) -> Result:
        if re.match(self.match[0], url):
            audiobook_id = self.extract_id_from_url(url)
            logging.debug(f"{audiobook_id=}")
            return self.download_audiobook(audiobook_id)
        if re.match(self.match[1], url):
            podcast_id = self.extract_id_from_url(url)
            return self.download_podcast(podcast_id)
        if re.match(self.match[2], url):
            kind, id = self.retrieve_id_from_share_link(url)
            logging.debug(f"{kind=} {id=}")
            if kind == "audiobook":
                return self.download_audiobook(id)
            if kind == "podcast":
                return self.download_podcast(id)
            raise DataNotPresent
        else:
            raise NoSourceFound


    UUID_REGEX = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8}"


    def extract_id_from_info_page(self, url: str) -> str:
        """Extract podcast id from information page"""
        return self.find_in_page(url, fr'\\"id\\":\\"({self.UUID_REGEX})\"')


    def retrieve_id_from_share_link(self, url: str) -> tuple[str, str]:
        """
        Retrieve the id of an item from a share link.

        :param url: the share link url
        :returns: the id of the shared item
        """
        url_with_id = http.redirect_of(url, amount = 2)
        if url_with_id is None:
            # describe why
            raise DataNotPresent
        parsed = parse_url(url_with_id)
        if parsed.path is None:
            raise DataNotPresent
        path = parsed.path.split("/")
        return path[-2], path[-1]



    def download_podcast(self, podcast_id: str) -> Series[dict]:
        """Download podcast info from Podimo"""
        logging.debug(f"{podcast_id=}")
        metadata = self.download_podcast_metadata(podcast_id)
        episodes = self.download_podcast_episode_ids(podcast_id)
        return Series(
            title = metadata["title"],
            books = episodes,
        )


    def download_podcast_metadata(self, podcast_id: str) -> dict:
        data = self.graphql_request(
            operation_name = "PodcastResultsQuery",
            query = "podcast",
            variables = {
                "id": podcast_id
            }
        )
        return data["data"]["podcastById"]


    def download_podcast_episode_ids(self, podcast_id: str) -> List:
        episodes = []
        limit = 100
        offset = 0
        while True:
            data = self.graphql_request(
                operation_name = "PodcastEpisodesResultsQuery",
                query = "podcast_episodes",
                variables = {
                    "limit": limit,
                    "offset": offset,
                    "podcastId": podcast_id,
                    "sorting": "PUBLISHED_ASCENDING"
                }
            )
            page = data["data"]["podcastEpisodes"] or []
            for episode in page:
                episodes.append(BookId(episode))
            if len(page) < limit:
                break
            offset += len(page)
        return episodes



    def download_from_id(self, episode_info: dict) -> Audiobook:
        episode_id = episode_info["id"]
        podcast_id = episode_info["podcastId"]
        return Audiobook(
            session = self.create_unauthenticated_session(),
            files = self.get_podcast_file(episode_id, podcast_id),
            metadata = self.format_podcast_metadata(episode_info),
            cover = self.download_cover(episode_info["imageUrl"])
        )


    def get_podcast_file(self, episode_id: str, podcast_id: str) -> List[AudiobookFile]:
        """
        Download url of podcast episode

        :param episode_id: Internal id for episode
        :param podcast_id: Internal id for podcast
        :returns: Links to all podcast audio files
        """
        data = self.graphql_request(
            operation_name = "ShortLivedPodcastMediaUrlQuery",
            query = "podcast_episode_file",
            variables = {
                "episodeId": episode_id,
                "podcastId": podcast_id
            }
        )
        file_url = data["data"]["podcastEpisodeAudioById"]["url"]
        if "m3u8" in file_url:
            audio_url = file_url.replace("main.m3u8", "stream_audio_high/stream.m3u8")
            return self.get_stream_files(audio_url)
        else:
            return [ AudiobookFile( url = file_url, ext = "mp3" ) ]


    def format_podcast_metadata(self, episode_info) -> AudiobookMetadata:
        """
        Format unstructed json response as `AudiobookMetadata`

        :param episode_info: Json response from Podimo
        :returns: Important metadata as `AudiobookMetadata`
        """
        metadata = AudiobookMetadata(
            title = episode_info["title"],
            series = episode_info.get("podcastName"),
            description = episode_info.get("description"),
        )
        pub = episode_info.get("publishDatetime")
        if pub:
            # Podimo returns ISO 8601, sometimes with a trailing Z
            metadata.release_date = datetime.fromisoformat(
                pub.replace("Z", "+00:00")
            ).date()
        if episode_info["authorName"]:
            metadata.add_author(episode_info["authorName"])
        return metadata


    def download_audiobook(self, audiobook_id: str) -> Audiobook:
        book_info = self.download_book_info(audiobook_id)
        return Audiobook(
            # Will sometimes get a 'Authentication required' message if logged in
            session = self.create_unauthenticated_session(),
            files = self.get_audiobook_files(audiobook_id),
            metadata = self.format_audiobook_metadata(book_info),
            cover = self.download_cover(book_info["coverImage"]["url"])
        )


    def get_audiobook_files(self, audiobook_id: str) -> List[AudiobookFile]:
        data = self.graphql_request(
            operation_name = "ShortLivedAudiobookMediaUrlQuery",
            query = "files",
            variables = {
                "id": audiobook_id
            }
        )
        audiobook_url = data["data"]["audiobookAudioById"]["url"]
        return [AudiobookFile(url = audiobook_url, ext = "mp3")]


    def download_book_info(self, audiobook_id: str) -> dict:
        data = self.graphql_request(
            operation_name = "AudiobookResultsQuery",
            query = "book_info",
            variables = {
                "id": audiobook_id
            }
        )
        return data["data"]["audiobookById"]


    def format_audiobook_metadata(self, book_info) -> AudiobookMetadata:
        metadata = AudiobookMetadata(book_info["title"])
        for author in book_info["authors"]:
            metadata.add_author(author["name"])
        for narrator in book_info["narrators"]:
            metadata.add_narrator(narrator["name"])
        metadata.description = book_info.get("description")
        return metadata


    def download_cover(self, cover_url: str) -> Cover:
        # Will sometimes get a 'Authentication required' message if logged in
        session = self.create_unauthenticated_session()
        response = session.get(cover_url, timeout=DEFAULT_HTTP_TIMEOUT)
        if response.status_code != 200:
            raise RequestError
        return Cover(image = response.content, extension = "png")

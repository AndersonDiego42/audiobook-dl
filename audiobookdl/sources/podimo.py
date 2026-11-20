from .source import CatalogBook, Source
from audiobookdl import logging
from audiobookdl.exceptions import DataNotPresent, DownloadCancelled, NoSourceFound, RequestError, UserNotAuthorized
from audiobookdl.utils import http, read_asset_file
from audiobookdl.utils.audiobook import (
    Audiobook,
    AudiobookFile,
    AudiobookMetadata,
    BookId,
    Chapter,
    Cover,
    Result,
    Series,
    detect_image_extension,
    parse_language,
    parse_release_date,
)
from audiobookdl.utils.http import DEFAULT_HTTP_TIMEOUT

from datetime import datetime
import re
from typing import Any
from urllib3.util import parse_url


class PodimoSource(Source[dict]):
    match = [
        r"https://open\.podimo\.com/audiobook/[^/]+",
        r"https://open\.podimo\.com/podcast/[^/]+",
        r"https://share\.podimo\.com/s/[^/]+",
    ]
    names = ["Podimo"]
    _authentication_methods = ["login"]
    supports_search = True
    supports_library = True

    def verify_auth(self, url: str = "") -> bool:
        has_auth = any(k.lower() == "authorization" for k in self._session.headers)
        if not self.authenticated and not has_auth:
            return False
        try:
            payload = self.post_json(
                "https://podimo.com/graphql",
                headers={"User-Agent": "JS GraphQL"},
                json={
                    "operationName": "LibraryQuery",
                    "query": "query LibraryQuery{userLibrary{audiobooks{id}}}",
                    "variables": {},
                },
            )
            data = (payload or {}).get("data") if isinstance(payload, dict) else None
            return isinstance(data, dict) and data.get("userLibrary") is not None
        except (RequestError, UserNotAuthorized, ValueError):
            return False

    def _login(self, url: str, username: str, password: str) -> None:
        data = self.graphql_request(
            operation_name="web_logInUser",
            query="login",
            variables={
                "email": username,
                "password": password,
            },
        )
        authorization_token = data["data"]["tokenWithCredentials"]["token"]
        logging.debug(f"authorization_token={logging.mask_secret(authorization_token)}")
        self._session.headers.update({"authorization": authorization_token})

    def graphql_request(self, operation_name: str, query: str, variables: dict) -> dict:
        """
        Make graphql request to Podimo.

        :param operation_name: Name of operation
        :param query: File query is stored in
        :param variables: Variables for query
        :returns: Parsed JSON response from server
        """
        return self.post_json(
            "https://podimo.com/graphql",
            headers={"User-Agent": "JS GraphQL"},
            json={
                "operationName": operation_name,
                "query": read_asset_file(f"assets/sources/podimo/{query}.graphql"),
                "variables": variables,
            },
        )

    @staticmethod
    def extract_id_from_url(url: str) -> str:
        """Extract audiobook or podcast id from url"""
        return url.rstrip("/").split("?")[0].split("/")[-1]

    def download(self, url: str) -> Result:
        if re.match(self.match[0], url):
            audiobook_id = self.extract_id_from_url(url)
            logging.debug(f"{audiobook_id=}")
            return self.download_audiobook(audiobook_id)
        if re.match(self.match[1], url):
            podcast_id = self.extract_id_from_url(url)
            return self.download_podcast(podcast_id)
        if re.match(self.match[2], url):
            kind, item_id = self.retrieve_id_from_share_link(url)
            logging.debug(f"{kind=} {item_id=}")
            if kind == "audiobook":
                return self.download_audiobook(item_id)
            if kind == "podcast":
                return self.download_podcast(item_id)
            raise DataNotPresent
        raise NoSourceFound

    def retrieve_id_from_share_link(self, url: str) -> tuple[str, str]:
        """
        Retrieve the kind and id of an item from a share link through `Source._session`.

        :param url: the share link url
        :returns: tuple `(kind, id)` of the shared item
        """
        url_with_id = http.redirect_of(
            url,
            amount=5,
            session=self._get_unauthenticated_session(),
            stop_non_redirect=True,
            target_pattern=r"https?://open\.podimo\.com/(?:audiobook|podcast)/[^/?#]+",
        )
        if url_with_id is None:
            raise DataNotPresent
        parsed = parse_url(url_with_id)
        if parsed.path is None:
            raise DataNotPresent
        path = [seg for seg in parsed.path.split("/") if seg]
        if len(path) < 2:
            raise DataNotPresent
        return path[-2], path[-1]

    def download_podcast(self, podcast_id: str) -> Series[dict]:
        """Download podcast info from Podimo"""
        logging.debug(f"{podcast_id=}")
        metadata = self.download_podcast_metadata(podcast_id)
        episodes = self.download_podcast_episode_ids(podcast_id)
        return Series(
            title=metadata["title"],
            books=episodes,
        )

    def download_podcast_metadata(self, podcast_id: str) -> dict:
        data = self.graphql_request(
            operation_name="PodcastResultsQuery",
            query="podcast",
            variables={"id": podcast_id},
        )
        return data["data"]["podcastById"]

    def download_podcast_episode_ids(self, podcast_id: str) -> list:
        episodes = []
        limit = 100
        offset = 0
        while True:
            ctx = logging.get_job_context()
            cancel_event = ctx.cancel_event if ctx is not None else None
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            data = self.graphql_request(
                operation_name="PodcastEpisodesResultsQuery",
                query="podcast_episodes",
                variables={
                    "limit": limit,
                    "offset": offset,
                    "podcastId": podcast_id,
                    "sorting": "PUBLISHED_ASCENDING",
                },
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
            session=self.create_unauthenticated_session(),
            files=self.get_podcast_file(episode_id, podcast_id),
            metadata=self.format_podcast_metadata(episode_info),
            cover=self.download_cover(episode_info.get("imageUrl")),
        )

    def _stream_or_file(self, file_url: str) -> list[AudiobookFile]:
        if ".m3u8" in file_url:
            return self.get_stream_files(file_url)
        return [AudiobookFile(url=file_url, ext="mp3")]

    def get_podcast_file(self, episode_id: str, podcast_id: str) -> list[AudiobookFile]:
        """Download url of podcast episode, resolving HLS streams when present."""
        data = self.graphql_request(
            operation_name="ShortLivedPodcastMediaUrlQuery",
            query="podcast_episode_file",
            variables={
                "episodeId": episode_id,
                "podcastId": podcast_id,
            },
        )
        file_url = data["data"]["podcastEpisodeAudioById"]["url"]
        return self._stream_or_file(file_url)

    def format_podcast_metadata(self, episode_info) -> AudiobookMetadata:
        """Format unstructured JSON response as `AudiobookMetadata`."""
        metadata = AudiobookMetadata(
            title=episode_info["title"],
            series=episode_info.get("podcastName"),
            description=episode_info.get("description"),
            source="podimo",
        )
        pub = episode_info.get("publishDatetime")
        if pub:
            metadata.release_date = datetime.fromisoformat(
                pub.replace("Z", "+00:00")
            ).date()
        if episode_info.get("authorName"):
            metadata.add_author(episode_info["authorName"])
        return metadata

    @staticmethod
    def get_chapters(book_info: dict[str, Any]) -> list[Chapter]:
        raw = book_info.get("chapters") or book_info.get("timestamps") or []
        chapters: list[Chapter] = []
        for idx, ch in enumerate(raw, start=1):
            if not isinstance(ch, dict):
                continue
            start_raw = ch.get("startMs", ch.get("start", ch.get("timestamp")))
            if start_raw is None:
                continue
            ch_title = str(ch.get("title") or ch.get("name") or f"Chapter {idx}").strip()
            chapters.append(Chapter(start=int(start_raw), title=ch_title))
        return chapters

    def download_audiobook(self, audiobook_id: str) -> Audiobook:
        book_info = self.download_book_info(audiobook_id)
        return Audiobook(
            session=self.create_unauthenticated_session(),
            files=self.get_audiobook_files(audiobook_id),
            metadata=self.format_audiobook_metadata(book_info),
            cover=self.download_cover((book_info.get("coverImage") or {}).get("url")),
            chapters=self.get_chapters(book_info),
        )

    def get_audiobook_files(self, audiobook_id: str) -> list[AudiobookFile]:
        data = self.graphql_request(
            operation_name="ShortLivedAudiobookMediaUrlQuery",
            query="files",
            variables={"id": audiobook_id},
        )
        audiobook_url = data["data"]["audiobookAudioById"]["url"]
        return self._stream_or_file(audiobook_url)

    def download_book_info(self, audiobook_id: str) -> dict:
        data = self.graphql_request(
            operation_name="AudiobookResultsQuery",
            query="book_info",
            variables={"id": audiobook_id},
        )
        return data["data"]["audiobookById"]

    def format_audiobook_metadata(self, book_info) -> AudiobookMetadata:
        metadata = AudiobookMetadata(book_info["title"], source="podimo")
        for author in book_info.get("authors") or []:
            metadata.add_author(author["name"])
        for narrator in book_info.get("narrators") or []:
            metadata.add_narrator(narrator["name"])
        metadata.description = book_info.get("description")
        pub_name = book_info.get("publisherName")
        if isinstance(pub_name, str) and pub_name.strip():
            metadata.publisher = pub_name.strip()
        metadata.release_date = parse_release_date(book_info.get("yearOfBookPublication"))
        lang_raw = book_info.get("language")
        if isinstance(lang_raw, dict):
            metadata.language = parse_language(lang_raw.get("isoLanguage")) or parse_language(
                lang_raw.get("localisedLanguage")
            )
        elif lang_raw:
            metadata.language = parse_language(lang_raw)
        return metadata

    def download_cover(self, cover_url: str | None) -> Cover | None:
        if not cover_url:
            return None
        session = self.create_unauthenticated_session()
        try:
            response = session.get(cover_url, timeout=DEFAULT_HTTP_TIMEOUT)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise RequestError
            headers = getattr(response, "headers", None) or {}
            ext = detect_image_extension(
                response.content,
                headers.get("Content-Type"),
                default="png",
            )
            return Cover(image=response.content, extension=ext)
        finally:
            session.close()

    def search(self, query: str) -> list[CatalogBook]:
        cleaned = query.strip()
        if not cleaned:
            return []
        payload = self.post_json(
            "https://podimo.com/graphql",
            headers={"User-Agent": "JS GraphQL"},
            json={
                "operationName": "SearchQuery",
                "query": (
                    "query SearchQuery($query:String!){"
                    "search(query:$query){audiobooks{id title authors{name}} podcasts{id title authorName}}"
                    "}"
                ),
                "variables": {"query": cleaned},
            },
        )
        search_data = (payload or {}).get("data", {}).get("search", {})
        results: list[CatalogBook] = []
        for item in search_data.get("audiobooks") or []:
            if isinstance(item, dict) and item.get("id") and item.get("title"):
                authors = [
                    str(a.get("name")) for a in (item.get("authors") or []) if isinstance(a, dict) and a.get("name")
                ]
                results.append(
                    CatalogBook(
                        title=str(item["title"]),
                        author=", ".join(authors),
                        url=f"https://open.podimo.com/audiobook/{item['id']}",
                        source="podimo",
                    )
                )
        for item in search_data.get("podcasts") or []:
            if isinstance(item, dict) and item.get("id") and item.get("title"):
                results.append(
                    CatalogBook(
                        title=str(item["title"]),
                        author=str(item.get("authorName") or "").strip(),
                        url=f"https://open.podimo.com/podcast/{item['id']}",
                        source="podimo",
                    )
                )
        return results

    def list_library(self) -> list[CatalogBook]:
        payload = self.post_json(
            "https://podimo.com/graphql",
            headers={"User-Agent": "JS GraphQL"},
            json={
                "operationName": "LibraryQuery",
                "query": (
                    "query LibraryQuery{"
                    "userLibrary{audiobooks{id title authors{name}} podcasts{id title authorName}}"
                    "}"
                ),
                "variables": {},
            },
        )
        lib = (payload or {}).get("data", {}).get("userLibrary", {})
        results: list[CatalogBook] = []
        for item in lib.get("audiobooks") or []:
            if isinstance(item, dict) and item.get("id") and item.get("title"):
                authors = [
                    str(a.get("name")) for a in (item.get("authors") or []) if isinstance(a, dict) and a.get("name")
                ]
                results.append(
                    CatalogBook(
                        title=str(item["title"]),
                        author=", ".join(authors),
                        url=f"https://open.podimo.com/audiobook/{item['id']}",
                        source="podimo",
                    )
                )
        for item in lib.get("podcasts") or []:
            if isinstance(item, dict) and item.get("id") and item.get("title"):
                results.append(
                    CatalogBook(
                        title=str(item["title"]),
                        author=str(item.get("authorName") or "").strip(),
                        url=f"https://open.podimo.com/podcast/{item['id']}",
                        source="podimo",
                    )
                )
        return results

from .source import Source
from audiobookdl import AudiobookFile, Chapter, AudiobookMetadata, Cover, Audiobook
from audiobookdl.exceptions import DataNotPresent, UserNotAuthorized

import re
import json
from urllib3.util import parse_url
from typing import List


class OverdriveSource(Source):
    match = [
        r"https://.+\.listen\.overdrive\.com"
    ]
    names = [ "Overdrive", "Libby" ]


    def download(self, url: str) -> Audiobook:
        # Parse url
        parsed_url = parse_url(url)
        hostname = parsed_url.hostname
        prefix = f"https://{hostname}"
        # Extract json from javascript
        raw = self.find_in_page(url, 'window.bData = {.+;')
        if raw is None:
            raise UserNotAuthorized
        raw_trimmed = raw[15:-1]
        book_info = json.loads(raw_trimmed)
        return Audiobook(
            session = self._session,
            files = self.get_files(prefix, book_info),
            metadata = self.get_metadata(book_info),
            cover = self.get_cover(prefix, book_info),
            chapters = self.get_chapters(book_info)
        )


    def get_metadata(self, book_info) -> AudiobookMetadata:
        title = book_info["title"]["main"]
        metadata = AudiobookMetadata(title)
        for creator in book_info["creator"]:
            if creator["role"] == "author":
                metadata.add_author(creator["name"])
            if creator["role"] == "narrator":
                metadata.add_narrator(creator["name"])
        return metadata

    def get_cover(self, prefix: str, book_info) -> Cover:
        cover_url = f"{prefix}/{book_info['-odread-furbish-uri']}"
        cover_data = self.get(cover_url)
        return Cover(cover_data, "jpg")

    def _compute_part_offsets(self, book_info) -> List[float]:
        """Return cumulative start offsets in seconds for each spine part."""
        offsets: List[float] = []
        elapsed = 0.0
        for part in book_info["spine"]:
            offsets.append(elapsed)
            elapsed += part["audio-duration"]
        return offsets

    def get_chapters(self, book_info) -> List[Chapter]:
        part_offsets = self._compute_part_offsets(book_info)
        chapters = []
        for chapter in book_info["nav"]["toc"]:
            timepoint = 0.
            if '#' in chapter["path"]:
                timepoint = float(chapter["path"].split("#")[1])
            part_result = re.search(r"(?<=(Part))\d+", chapter["path"])
            if part_result is None:
                continue
            part = int(part_result.group(0))-1
            base_offset = part_offsets[part] if 0 <= part < len(part_offsets) else 0.0
            start = int((base_offset + timepoint) * 1000)
            chapters.append(Chapter(start, chapter["title"]))
        return chapters


    def get_files(self, prefix: str, book_info) -> List[AudiobookFile]:
        toc: List[str] = []
        for part in book_info["nav"]["toc"]:
            if "contents" in part:
                toc = []
                for _ in range(len(book_info["spine"])):
                    toc.append(book_info["nav"]["toc"][0]["title"])
                break
            else:
                toc.append(part["title"])
        files = []
        for num, part in enumerate(book_info["spine"]):
            title = toc[num] if num < len(toc) else f"Part {num + 1}"
            files.append(AudiobookFile(
                url = f"{prefix}/{part['path']}",
                title = title,
                ext = "mp3"
            ))
        return files

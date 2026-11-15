"""Deterministic media, HTML, and encryption fixture builders for E2E tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
from typing import Dict, Optional, Sequence, Tuple

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image


class MediaFixtureFactory:
    """Generates and caches real audio/image test fixtures using FFmpeg and Pillow."""

    def __init__(self, cache_dir: Path, ffmpeg_path: Optional[str]) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = ffmpeg_path
        self._bytes_cache: Dict[Tuple[object, ...], bytes] = {}

    def require_ffmpeg(self) -> str:
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg executable is required to build audio fixtures")
        return self._ffmpeg

    def make_mp3(
        self,
        name: str = "track.mp3",
        *,
        duration_sec: float = 1.0,
        freq_hz: int = 440,
    ) -> bytes:
        """Generate a valid deterministic MP3 audio stream with a sine tone."""
        key = ("mp3", name, duration_sec, freq_hz)
        if key in self._bytes_cache:
            return self._bytes_cache[key]
        ffmpeg = self.require_ffmpeg()
        out_path = self._cache_dir / name
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq_hz}:sample_rate=44100:duration={duration_sec}",
                "-ac",
                "2",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        data = out_path.read_bytes()
        self._bytes_cache[key] = data
        return data

    def make_aac_mpegts(
        self,
        name: str = "seg.ts",
        *,
        duration_sec: float = 1.0,
        freq_hz: int = 440,
    ) -> bytes:
        """Generate a valid deterministic AAC-in-MPEG-TS segment."""
        key = ("ts", name, duration_sec, freq_hz)
        if key in self._bytes_cache:
            return self._bytes_cache[key]
        ffmpeg = self.require_ffmpeg()
        out_path = self._cache_dir / name
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq_hz}:sample_rate=44100:duration={duration_sec}",
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-f",
                "mpegts",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        data = out_path.read_bytes()
        self._bytes_cache[key] = data
        return data

    def make_jpeg(
        self,
        *,
        size: Tuple[int, int] = (32, 32),
        color: Tuple[int, int, int] = (35, 115, 195),
    ) -> bytes:
        """Generate a deterministic valid JPEG cover image."""
        key = ("jpg", size, color)
        if key in self._bytes_cache:
            return self._bytes_cache[key]
        buf = BytesIO()
        Image.new("RGB", size, color).save(buf, format="JPEG")
        data = buf.getvalue()
        self._bytes_cache[key] = data
        return data


def encrypt_aes_128_cbc(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """Encrypt `plaintext` with PKCS7 padding using AES-128-CBC."""
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))


def build_librivox_html(
    *,
    title: str,
    cover_url: str,
    parts: Sequence[Tuple[str, str]],
) -> bytes:
    """Build deterministic HTML matching `LibrivoxSource` CSS selectors."""
    part_links = "\n".join(
        f'<li><a class="chapter-name" href="{href}">{label}</a></li>'
        for label, href in parts
    )
    html = (
        "<!doctype html>\n"
        "<html><body>\n"
        f'<div class="content-wrap"><h1>{title}</h1></div>\n'
        f'<div class="book-page-book-cover"><img src="{cover_url}" alt="cover"/></div>\n'
        '<ul class="chapter-download">\n'
        f"{part_links}\n"
        "</ul>\n"
        "</body></html>\n"
    )
    return html.encode("utf-8")

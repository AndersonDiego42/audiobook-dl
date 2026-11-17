"""End-to-end tests for authenticated login, credential store, AES HLS, and series pipeline."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover
import pycountry
import pytest

from audiobookdl import (
    Audiobook,
    AudiobookMetadata,
    BookId,
    Chapter,
    Cover,
    DownloadEvent,
    DownloadSummary,
    Downloader,
    Series,
    logging,
)
from audiobookdl.options_schema import build_default_options
from audiobookdl.config import load_config, save_source_credentials
from audiobookdl.credentials import PLAINTEXT_OPT_IN_ENV, get_credential_store
from audiobookdl.sources.source import Source
from audiobookdl.utils import ffprobe_duration
from tests.e2e.fixtures import MediaFixtureFactory, encrypt_aes_128_cbc
from tests.e2e.harness import E2EWorkspace
from tests.e2e.server import LocalAudiobookServer, RouteRequest, RouteResponse


pytestmark = pytest.mark.e2e


class _E2EAuthHlsSource(Source):
    names = ["e2eauth"]
    match = [r"http://127\.0\.0\.1:\d+/series/\d+"]
    _authentication_methods = ["login"]
    login_data = ["username", "password"]
    create_storage_dir = True
    _base_url: str = ""

    def _login(self, url: str, username: str, password: str) -> None:
        base_url = url.split("/series/", 1)[0]
        token_data = self.post_json(
            f"{base_url}/auth/login",
            json={"username": username, "password": password},
        )
        self._session.headers.update({"Authorization": f"Bearer {token_data['token']}"})

    def download(self, url: str) -> Series:
        payload = self.get_json(url)
        books = [BookId(item["id"], title=item["title"]) for item in payload["books"]]
        return Series(title=payload["title"], books=books)

    def download_from_id(self, book_id: str) -> Audiobook:
        book_url = self._series_book_url(book_id)
        meta_json = self.get_json(book_url)
        files = self.get_stream_files(
            meta_json["playlist_url"],
            expected_content_type="video/mp2t",
        )
        cover_bytes = self.get(meta_json["cover_url"])
        metadata = AudiobookMetadata(
            title=meta_json["title"],
            scrape_url=book_url,
            series=meta_json["series"],
            series_order=meta_json["series_order"],
            authors=meta_json["authors"],
            narrators=meta_json["narrators"],
            language=pycountry.languages.get(alpha_3=meta_json["language"]),
            isbn=meta_json["isbn"],
            release_date=date.fromisoformat(meta_json["release_date"]),
        )
        chapters = [Chapter(start=c["start"], title=c["title"]) for c in meta_json["chapters"]]
        return Audiobook(
            session=self._session,
            metadata=metadata,
            files=files,
            chapters=chapters,
            cover=Cover(image=cover_bytes, extension="jpg"),
            source_data={"id": meta_json["id"]},
        )

    def _series_book_url(self, book_id: str) -> str:
        return f"{self._base_url}/books/{book_id}"

    def on_download_complete(self, audiobook: Audiobook) -> None:
        marker = Path(self.database_directory) / f"{audiobook.source_data['id']}.json"
        marker.write_text(json.dumps({"title": audiobook.title}), encoding="utf-8")


@pytest.mark.ffmpeg
def test_authenticated_login_credential_store_hls_aes_and_selective_series(
    e2e_workspace: E2EWorkspace,
    e2e_server: LocalAudiobookServer,
    media_factory: MediaFixtureFactory,
    ffmpeg_tools: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = ffmpeg_tools
    seg0_plain = media_factory.make_aac_mpegts("auth_s0.ts", duration_sec=1.0, freq_hz=440)
    seg1_plain = media_factory.make_aac_mpegts("auth_s1.ts", duration_sec=1.0, freq_hz=660)
    aes_key = b"0123456789abcdef"
    iv0 = bytes.fromhex("00000000000000000000000000000001")
    iv1 = bytes.fromhex("00000000000000000000000000000002")
    seg0_enc = encrypt_aes_128_cbc(seg0_plain, aes_key, iv0)
    seg1_enc = encrypt_aes_128_cbc(seg1_plain, aes_key, iv1)
    cover = media_factory.make_jpeg()

    playlist = (
        b"#EXTM3U\n"
        b"#EXT-X-VERSION:3\n"
        b"#EXT-X-TARGETDURATION:2\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001\n'
        b"#EXTINF:1.0,\n"
        b"seg0.ts\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000002\n'
        b"#EXTINF:1.0,\n"
        b"seg1.ts\n"
        b"#EXT-X-ENDLIST\n"
    )

    expected_token = "e2e-bearer-secret"
    expected_cookie = "e2e_session=valid-session-99"
    login_attempts: list[dict[str, str]] = []

    def login_handler(req: RouteRequest) -> RouteResponse:
        creds = json.loads(req.body.decode("utf-8"))
        login_attempts.append(creds)
        if creds.get("username") == "reader@example.com" and creds.get("password") == "s3cr3t-pw!":
            return RouteResponse(
                body=json.dumps({"token": expected_token}).encode("utf-8"),
                content_type="application/json",
                status_code=200,
                headers={"Set-Cookie": f"{expected_cookie}; Path=/"},
                support_range=False,
            )
        return RouteResponse(
            body=b'{"error":"invalid_credentials"}',
            content_type="application/json",
            status_code=401,
            support_range=False,
        )

    def require_auth(body: bytes, content_type: str):
        def _handler(req: RouteRequest) -> RouteResponse:
            auth_hdr = req.headers.get("Authorization", "")
            cookie_hdr = req.headers.get("Cookie", "")
            if auth_hdr != f"Bearer {expected_token}" or expected_cookie not in cookie_hdr:
                return RouteResponse(
                    body=b"unauthorized",
                    content_type="text/plain",
                    status_code=401,
                    support_range=False,
                )
            return RouteResponse(body=body, content_type=content_type, status_code=200)

        return _handler

    base_url = e2e_server.url
    series_url = f"{base_url}/series/10"
    _E2EAuthHlsSource._base_url = base_url  # type: ignore[attr-defined]

    e2e_server.add_handler("POST", "/auth/login", login_handler)
    e2e_server.add_handler(
        "GET",
        "/series/10",
        require_auth(
            json.dumps({
                "title": "E2E Auth Saga",
                "books": [
                    {"id": "101", "title": "Book One (Skipped)"},
                    {"id": "102", "title": "Book Two (Selected)"},
                ],
            }).encode("utf-8"),
            "application/json",
        ),
    )
    e2e_server.add_handler(
        "GET",
        "/books/102",
        require_auth(
            json.dumps({
                "id": "102",
                "title": "Book Two (Selected)",
                "series": "E2E Auth Saga",
                "series_order": 2,
                "authors": ["Ada Lovelace"],
                "narrators": ["Grace Hopper"],
                "language": "eng",
                "isbn": "9780123456789",
                "release_date": "2024-06-01",
                "playlist_url": f"{base_url}/hls/master.m3u8",
                "cover_url": f"{base_url}/cover.jpg",
                "chapters": [
                    {"start": 0, "title": "Opening"},
                    {"start": 1000, "title": "Finale"},
                ],
            }).encode("utf-8"),
            "application/json",
        ),
    )
    e2e_server.add_handler("GET", "/hls/master.m3u8", require_auth(playlist, "application/vnd.apple.mpegurl"))
    e2e_server.add_handler("GET", "/hls/key.bin", require_auth(aes_key, "application/octet-stream"))
    e2e_server.add_handler("GET", "/hls/seg0.ts", require_auth(seg0_enc, "video/mp2t"))
    e2e_server.add_handler("GET", "/hls/seg1.ts", require_auth(seg1_enc, "video/mp2t"))
    e2e_server.add_handler("GET", "/cover.jpg", require_auth(cover, "image/jpeg"))

    # Persist credentials via real CredentialStore (DPAPI on Windows, keyring or opted-in fallback on Linux)
    monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV, "1")
    store = get_credential_store(str(e2e_workspace.config_path))
    try:
        save_source_credentials(
            "e2eauth",
            username="reader@example.com",
            password="s3cr3t-pw!",
            overwrite=str(e2e_workspace.config_path),
            credential_store=store,
        )
        loaded_cfg = load_config(str(e2e_workspace.config_path), credential_store=store)
        assert loaded_cfg.sources["e2eauth"].username == "reader@example.com"
        assert loaded_cfg.sources["e2eauth"].password == "s3cr3t-pw!"

        monkeypatch.setattr("audiobookdl.sources.get_source_classes", lambda: [_E2EAuthHlsSource])
        options = build_default_options(
            config_location=str(e2e_workspace.config_path),
            output_template=str(e2e_workspace.output_dir / "{author}" / "{title}"),
            database_directory=str(e2e_workspace.db_dir),
            output_format="m4b",
            combine=True,
            write_json_metadata=True,
        )
        events: list[DownloadEvent] = []
        downloader = Downloader(config=loaded_cfg, options=options, on_event=events.append)

        # Phase 1: Authenticate + fetch series metadata without downloading audio
        source, result = downloader.prepare(series_url)
        assert isinstance(result, Series)
        assert [b.title for b in result.books] == ["Book One (Skipped)", "Book Two (Selected)"]
        assert login_attempts == [{"username": "reader@example.com", "password": "s3cr3t-pw!"}]

        # Phase 2: Selectively download book index 1 using authenticated session
        summary = downloader.download(
            series_url,
            selected_indices=[1],
            prefetched=(source, result),
        )
        assert summary == DownloadSummary(completed=1, failed=0, cancelled=False)
        assert logging.get_job_context() is None

        out_m4b = e2e_workspace.output_dir / "Ada Lovelace" / "Book Two (Selected).m4b"
        assert out_m4b.is_file()
        assert 1.8 <= ffprobe_duration(str(out_m4b)) <= 2.3
        assert json.loads((e2e_workspace.db_dir / "e2eauth" / "102.json").read_text(encoding="utf-8")) == {
            "title": "Book Two (Selected)"
        }

        mp4 = MP4(str(out_m4b))
        assert mp4.chapters is not None
        assert [c.title for c in mp4.chapters] == ["Opening", "Finale"]
        assert mp4.tags is not None
        assert mp4.tags["\xa9nam"] == ["Book Two (Selected)"]
        assert mp4.tags["\xa9alb"] == ["E2E Auth Saga"]
        assert isinstance(mp4.tags["covr"][0], MP4Cover)

        # Confirm Book 101 was never requested
        paths = {req.path for req in e2e_server.recorded_requests()}
        assert "/books/101" not in paths
        assert "/books/102" in paths
    finally:
        store.delete_password("e2eauth", "reader@example.com")

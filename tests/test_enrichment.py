from datetime import date
from io import BytesIO
from typing import Any

from PIL import Image
import pycountry
import pytest
import requests

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, Chapter, Cover
from audiobookdl.options_schema import OPTIONS_BY_NAME, build_default_options
from audiobookdl.utils.enrichment import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_PROVIDERS,
    EnrichmentCandidate,
    enrich_audiobook_metadata,
    parse_provider_list,
    score_candidate_match,
    search_enrichment_candidates,
)
from audiobookdl.utils.enrichment_cache import (
    EnrichmentCache,
    default_enrichment_cache_path,
    make_cache_key,
)


def _make_jpeg_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (16, 16), (40, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, *, json_data: Any = None, content: bytes = b"", status_code: int = 200) -> None:
        self._json_data = json_data
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class _RoutingSession:
    """Fake `requests.Session` dispatching URLs to scripted responses and recording calls."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        for pattern, handler in self.routes.items():
            if pattern in url:
                val = handler(url, kwargs) if callable(handler) else handler
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, bytes):
                    return _FakeResponse(content=val)
                return _FakeResponse(json_data=val)
        return _FakeResponse(status_code=404)


def test_parse_provider_list_normalizes_aliases_and_rejects_unknown():
    assert parse_provider_list(None) == DEFAULT_PROVIDERS
    assert parse_provider_list("") == DEFAULT_PROVIDERS
    assert parse_provider_list("apple, google_books, OL, audible, itunes") == (
        "itunes",
        "google",
        "openlibrary",
        "audnexus",
    )
    with pytest.raises(ValueError, match="Unknown enrichment provider"):
        parse_provider_list("audnexus,bogus")


def test_score_candidate_match_exact_ids_and_fuzzy_titles():
    meta = AudiobookMetadata(
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441172719",
        asin="B002V5B16O",
    )
    assert score_candidate_match(meta, EnrichmentCandidate(provider="audnexus", score=0.0, title="Other", asin="B002V5B16O")) == 1.0
    assert score_candidate_match(meta, EnrichmentCandidate(provider="google", score=0.0, title="Other", isbn="978-0-441-17271-9")) == 1.0

    title_meta = AudiobookMetadata(title="Project Hail Mary: A Novel", authors=["Andy Weir"])
    good = EnrichmentCandidate(
        provider="itunes",
        score=0.0,
        title="Project Hail Mary (Unabridged)",
        authors=["Andy Weir"],
    )
    bad = EnrichmentCandidate(
        provider="itunes",
        score=0.0,
        title="Completely Unrelated Cookbook",
        authors=["Someone Else"],
    )
    assert score_candidate_match(title_meta, good) >= 0.9
    assert score_candidate_match(title_meta, bad) < DEFAULT_CONFIDENCE_THRESHOLD


def test_audnexus_catalog_discovery_fills_metadata_cover_and_chapters():
    jpeg_bytes = _make_jpeg_bytes()
    session = _RoutingSession({
        "api.audible.com/1.0/catalog/products": {
            "products": [
                {
                    "asin": "B08G9PRS1K",
                    "title": "Project Hail Mary",
                    "authors": [{"name": "Andy Weir"}],
                }
            ]
        },
        "api.audnex.us/books/B08G9PRS1K/chapters": {
            "asin": "B08G9PRS1K",
            "chapters": [
                {"title": "Chapter 1", "startOffsetMs": 0},
                {"title": "Chapter 2", "startOffsetMs": 45000},
            ],
        },
        "api.audnex.us/books/B08G9PRS1K": {
            "asin": "B08G9PRS1K",
            "title": "Project Hail Mary",
            "authors": [{"name": "Andy Weir"}],
            "narrators": [{"name": "Ray Porter"}],
            "publisherName": "Audible Studios",
            "releaseDate": "2021-05-04T00:00:00.000Z",
            "summary": "<p>Ryland Grace is the sole survivor.<br/>Mission critical.</p>",
            "genres": [{"name": "Science Fiction"}],
            "seriesPrimary": {"name": "Standalone", "position": "1"},
            "language": "english",
            "isbn": "9780593135204",
            "image": "https://cdn.example/cover.jpg",
        },
        "cdn.example/cover.jpg": jpeg_bytes,
    })
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Project Hail Mary", authors=["Andy Weir"]),
        files=[AudiobookFile(url="https://example.com/1.m4b", ext="m4b")],
    )
    result = enrich_audiobook_metadata(
        book,
        providers=("audnexus",),
        session=session,  # type: ignore[arg-type]
    )
    assert result.updated is True
    assert result.providers_used == ["audnexus"]
    assert result.cover_updated is True
    assert result.chapters_updated is True
    assert book.metadata.asin == "B08G9PRS1K"
    assert book.metadata.isbn == "9780593135204"
    assert book.metadata.narrators == ["Ray Porter"]
    assert book.metadata.publisher == "Audible Studios"
    assert book.metadata.release_date == date(2021, 5, 4)
    assert book.metadata.description == "Ryland Grace is the sole survivor.\nMission critical."
    assert book.metadata.series == "Standalone"
    assert book.metadata.series_order == 1
    assert book.metadata.genres == ["Science Fiction"]
    assert book.metadata.language == pycountry.languages.get(alpha_3="eng")
    assert book.cover is not None and book.cover.extension == "jpg"
    assert [c.title for c in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert [c.start for c in book.chapters] == [0, 45000]


def test_itunes_provider_upgrades_artwork_and_extracts_fields():
    jpeg_bytes = _make_jpeg_bytes()
    requested_urls: list[str] = []

    def _cover_handler(url: str, _kwargs: dict[str, Any]) -> bytes:
        requested_urls.append(url)
        return jpeg_bytes

    session = _RoutingSession({
        "itunes.apple.com/search": {
            "resultCount": 1,
            "results": [
                {
                    "wrapperType": "audiobook",
                    "collectionId": 12345,
                    "collectionName": "Dune",
                    "artistName": "Frank Herbert",
                    "primaryGenreName": "Sci-Fi & Fantasy",
                    "releaseDate": "2007-04-01T07:00:00Z",
                    "description": "<b>Classic</b> sci-fi epic.",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/aa/100x100bb.jpg",
                }
            ],
        },
        "mzstatic.com": _cover_handler,
    })
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    result = enrich_audiobook_metadata(
        book,
        providers=("itunes",),
        session=session,  # type: ignore[arg-type]
    )
    assert result.updated is True
    assert result.cover_updated is True
    assert requested_urls == ["https://is1-ssl.mzstatic.com/image/thumb/Music/v4/aa/1200x1200bb.jpg"]
    assert book.metadata.genres == ["Sci-Fi & Fantasy"]
    assert book.metadata.release_date == date(2007, 4, 1)
    assert book.metadata.description == "Classic sci-fi epic."


def test_google_books_provider_prefers_isbn13_and_partial_dates():
    session = _RoutingSession({
        "googleapis.com/books/v1/volumes": {
            "items": [
                {
                    "id": "gb1",
                    "volumeInfo": {
                        "title": "Foundation",
                        "authors": ["Isaac Asimov"],
                        "publisher": "Spectra",
                        "publishedDate": "1991-06",
                        "description": "Galactic Empire falls.",
                        "categories": ["Fiction"],
                        "language": "en",
                        "industryIdentifiers": [
                            {"type": "ISBN_10", "identifier": "0553293354"},
                            {"type": "ISBN_13", "identifier": "9780553293357"},
                        ],
                        "imageLinks": {
                            "thumbnail": "http://books.google.com/books/content?id=gb1&edge=curl"
                        },
                    },
                }
            ]
        }
    })
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Foundation", authors=["Isaac Asimov"]),
        files=[],
    )
    candidates = search_enrichment_candidates(
        book.metadata,
        providers=("google",),
        session=session,  # type: ignore[arg-type]
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.isbn == "9780553293357"
    assert cand.release_date == date(1991, 6, 1)
    assert cand.language == pycountry.languages.get(alpha_2="en")
    assert cand.cover_url == "https://books.google.com/books/content?id=gb1"


def test_multi_provider_merge_preserves_existing_source_fields():
    existing_cover = Cover(image=_make_jpeg_bytes(), extension="jpg")
    existing_chapters = [Chapter(start=0, title="Source Chapter 1")]
    session = _RoutingSession({
        "api.audible.com/1.0/catalog/products": {
            "products": [{"asin": "B000000001", "title": "Mistborn", "authors": [{"name": "Brandon Sanderson"}]}]
        },
        "api.audnex.us/books/B000000001": {
            "asin": "B000000001",
            "title": "Mistborn",
            "authors": [{"name": "Brandon Sanderson"}],
            "narrators": [{"name": "Michael Kramer"}],
            "publisherName": "Wrong Publisher",
        },
        "googleapis.com/books/v1/volumes": {
            "items": [
                {
                    "volumeInfo": {
                        "title": "Mistborn",
                        "authors": ["Brandon Sanderson"],
                        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780765311788"}],
                        "description": "Allomancy epic.",
                    }
                }
            ]
        },
    })
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(
            title="Mistborn",
            authors=["Brandon Sanderson"],
            publisher="Tor Audio",
        ),
        files=[],
        cover=existing_cover,
        chapters=list(existing_chapters),
    )
    result = enrich_audiobook_metadata(
        book,
        providers=("audnexus", "google", "openlibrary"),
        session=session,  # type: ignore[arg-type]
    )
    assert result.updated is True
    assert book.metadata.publisher == "Tor Audio"
    assert book.metadata.narrators == ["Michael Kramer"]
    assert book.metadata.asin == "B000000001"
    assert book.metadata.isbn == "9780765311788"
    assert book.metadata.description == "Allomancy epic."
    assert book.cover is existing_cover
    assert book.chapters == existing_chapters
    assert result.cover_updated is False
    assert result.chapters_updated is False


def test_enrichment_cache_hit_ttl_expiry_and_bypass(tmp_path):
    cache_path = default_enrichment_cache_path(str(tmp_path))
    with EnrichmentCache(cache_path, ttl_seconds=60.0) as cache:
        key = make_cache_key("google", "https://example.com", {"q": "Dune"})
        cache.set(key, "google", {"items": []}, now=1000.0)
        assert cache.get(key, now=1030.0) == {"items": []}
        assert cache.get(key, now=1061.0) is None

    http_calls = 0

    def _google_handler(_url: str, _kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal http_calls
        http_calls += 1
        return {
            "items": [
                {
                    "volumeInfo": {
                        "title": "Dune",
                        "authors": ["Frank Herbert"],
                        "publisher": "Chilton",
                    }
                }
            ]
        }

    session = _RoutingSession({"googleapis.com/books/v1/volumes": _google_handler})
    book1 = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    enrich_audiobook_metadata(
        book1,
        providers=("google",),
        session=session,  # type: ignore[arg-type]
        database_directory=str(tmp_path),
        use_cache=True,
    )
    assert http_calls == 1
    assert book1.metadata.publisher == "Chilton"

    # Second lookup with offline session must succeed via SQLite cache
    offline = _RoutingSession({"googleapis.com": requests.ConnectionError("offline")})
    book2 = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    enrich_audiobook_metadata(
        book2,
        providers=("google",),
        session=offline,  # type: ignore[arg-type]
        database_directory=str(tmp_path),
        use_cache=True,
    )
    assert book2.metadata.publisher == "Chilton"

    # Bypassing cache (`use_cache=False`) performs fresh HTTP GET
    book3 = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    enrich_audiobook_metadata(
        book3,
        providers=("google",),
        session=session,  # type: ignore[arg-type]
        database_directory=str(tmp_path),
        use_cache=False,
    )
    assert http_calls == 2


def test_interactive_selector_chooses_candidate_or_skips():
    session = _RoutingSession({
        "googleapis.com/books/v1/volumes": {
            "items": [
                {"volumeInfo": {"title": "Dune", "authors": ["Frank Herbert"], "publisher": "First"}},
                {"volumeInfo": {"title": "Dune", "authors": ["Frank Herbert"], "publisher": "Second"}},
            ]
        }
    })
    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    seen_providers: list[str] = []

    def _pick_second(provider: str, cands: Any) -> int | None:
        seen_providers.append(provider)
        assert len(cands) == 2
        return 1

    res = enrich_audiobook_metadata(
        book,
        providers=("google",),
        session=session,  # type: ignore[arg-type]
        interactive=True,
        selector=_pick_second,
    )
    assert seen_providers == ["google"]
    assert res.updated is True
    assert book.metadata.publisher == "Second"

    book_skip = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Dune", authors=["Frank Herbert"]),
        files=[],
    )
    res_skip = enrich_audiobook_metadata(
        book_skip,
        providers=("google",),
        session=session,  # type: ignore[arg-type]
        interactive=True,
        selector=lambda _p, _c: None,
    )
    assert res_skip.updated is False
    assert book_skip.metadata.publisher is None


def test_enrichment_providers_validation_and_options():
    binding = OPTIONS_BY_NAME["enrich_providers"].gui
    assert binding is not None
    assert binding.read_option("apple,google_books") == "itunes,google"
    with pytest.raises(ValueError):
        binding.read_option("invalid_provider")

    opts = build_default_options(
        enrich_metadata=True,
        enrich_providers="itunes,google",
        enrich_interactive=True,
        no_enrich_cache=True,
    )
    assert opts.enrich_metadata is True
    assert opts.enrich_providers == "itunes,google"
    assert opts.enrich_interactive is True
    assert opts.no_enrich_cache is True


def test_enrichment_skips_incapable_providers_for_missing_fields():
    from audiobookdl import Chapter, Cover

    requested_urls: list[str] = []

    class _RecordingSession:
        def get(self, url, **_kwargs):
            requested_urls.append(url)
            if "audnex.us/books/B08F7X1234" in url:
                return _FakeResponse(
                    json_data={
                        "asin": "B08F7X1234",
                        "title": "Complete Book",
                        "authors": [{"name": "Author"}],
                        "narrators": [{"name": "Narrator Name"}],
                    }
                )
            return _FakeResponse(status_code=404)

    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(
            title="Complete Book",
            authors=["Author"],
            genres=["Sci-Fi"],
            language="en",
            description="Already present",
            isbn="9781234567890",
            asin="B08F7X1234",
            publisher="Pub",
            release_date=date(2020, 1, 1),
            series="Existing Series",
            series_order=1,
        ),
        files=[],
        cover=Cover(image=b"img", extension="jpg"),
        chapters=[Chapter(start=0, title="Ch 1")],
    )
    # Only `narrators` is missing: itunes/google/openlibrary cannot supply narrators, so only audnexus runs.
    res = enrich_audiobook_metadata(
        book,
        providers=("itunes", "google", "openlibrary", "audnexus"),
        session=_RecordingSession(),  # type: ignore[arg-type]
        use_cache=False,
    )
    assert res.updated is True
    assert book.metadata.narrators == ["Narrator Name"]
    assert all("itunes.apple.com" not in u for u in requested_urls)
    assert all("googleapis.com" not in u for u in requested_urls)
    assert all("openlibrary.org" not in u for u in requested_urls)


def test_abs_custom_provider_search_mapping_and_enrichment(tmp_path):
    from audiobookdl.config import CustomMetadataProviderConfig
    from audiobookdl.utils.enrichment import (
        resolve_provider_list,
        test_custom_metadata_provider,
    )

    cover_bytes = _make_jpeg_bytes()
    seen_requests: list[tuple[str, dict[str, Any] | None, dict[str, str]]] = []

    class _CustomProviderSession:
        def get(self, url, params=None, headers=None, **_kwargs):
            seen_requests.append((url, params, dict(headers or {})))
            if url == "https://abs-meta.example.com/search":
                return _FakeResponse(
                    json_data={
                        "matches": [
                            {
                                "title": "Project Hail Mary",
                                "author": "Andy Weir, Co Author",
                                "narrator": "Ray Porter",
                                "publisher": "Audible Studios",
                                "publishedYear": "2021",
                                "description": "<p>Sci-fi survival story.</p>",
                                "cover": "https://cdn.example.com/phm.jpg",
                                "isbn": "9780593135204",
                                "asin": "B08G9PRS1K",
                                "genres": ["Science Fiction", "Audiobook"],
                                "series": [{"series": "Hail Mary Verse", "sequence": "1"}],
                                "language": "eng",
                            }
                        ]
                    }
                )
            if url == "https://cdn.example.com/phm.jpg":
                return _FakeResponse(content=cover_bytes)
            return _FakeResponse(status_code=404)

    custom_map = {
        "hardcover": CustomMetadataProviderConfig(
            name="hardcover",
            url="https://abs-meta.example.com",
            api_key="my-secret-token",
            enabled=True,
        ),
        "disabledsvc": CustomMetadataProviderConfig(
            name="disabledsvc",
            url="https://disabled.example.com",
            enabled=False,
        ),
    }

    assert resolve_provider_list(None, custom_providers=custom_map) == (
        *DEFAULT_PROVIDERS,
        "custom:hardcover",
    )
    assert parse_provider_list("hardcover, audnexus", custom_providers=custom_map) == (
        "custom:hardcover",
        "audnexus",
    )
    with pytest.raises(ValueError):
        parse_provider_list("missing_custom", custom_providers=custom_map)

    book = Audiobook(
        session=requests.Session(),
        metadata=AudiobookMetadata(title="Project Hail Mary", authors=["Andy Weir"]),
        files=[],
    )
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    result = enrich_audiobook_metadata(
        book,
        providers=("custom:hardcover", "custom:disabledsvc"),
        custom_providers=custom_map,
        session=_CustomProviderSession(),  # type: ignore[arg-type]
        database_directory=str(db_dir),
        use_cache=True,
        include_chapters=False,
    )
    assert result.updated is True
    assert result.providers_used == ["custom:hardcover"]
    assert result.cover_updated is True
    assert book.metadata.narrators == ["Ray Porter"]
    assert book.metadata.series == "Hail Mary Verse"
    assert book.metadata.series_order == 1
    assert book.metadata.publisher == "Audible Studios"
    assert book.metadata.release_date == date(2021, 1, 1)
    assert book.metadata.asin == "B08G9PRS1K"
    assert book.metadata.isbn == "9780593135204"
    assert book.metadata.genres == ["Science Fiction", "Audiobook"]
    assert book.metadata.authors == ["Andy Weir"]  # existing author preserved

    # Verify AUTHORIZATION header matches ABS OpenAPI spec verbatim (raw key) and query/author params sent
    search_call = next(r for r in seen_requests if r[0] == "https://abs-meta.example.com/search")
    assert search_call[1] == {"query": "Project Hail Mary", "author": "Andy Weir"}
    assert search_call[2]["Authorization"] == "my-secret-token"
    assert all("disabled.example.com" not in r[0] for r in seen_requests)

    # Probe helper works and surfaces invalid payload cleanly
    count, summary = test_custom_metadata_provider(
        "https://abs-meta.example.com/",
        api_key="my-secret-token",
        session=_CustomProviderSession(),  # type: ignore[arg-type]
    )
    assert count == 1
    assert "1 sample match" in summary

    class _InvalidJsonSession:
        def get(self, *_args, **_kwargs):
            return _FakeResponse(json_data={"unexpected": True})

    with pytest.raises(ValueError, match="matches"):
        test_custom_metadata_provider(
            "https://abs-meta.example.com",
            session=_InvalidJsonSession(),  # type: ignore[arg-type]
        )


@pytest.mark.no_worker_cap
def test_search_enrichment_candidates_runs_concurrently_when_session_is_none(monkeypatch):
    import threading
    from audiobookdl.utils import enrichment as enr_mod

    active = 0
    peak_active = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=2.0)

    def _fake_query(provider, metadata, **_kwargs):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            barrier.wait()
            return [
                EnrichmentCandidate(
                    provider=provider,
                    score=0.90,
                    title=f"{metadata.title}-{provider}",
                )
            ]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(enr_mod, "_query_provider", _fake_query)
    meta = AudiobookMetadata(title="Dune", authors=["Frank Herbert"])
    results = search_enrichment_candidates(meta, providers=("itunes", "google", "openlibrary"))
    assert peak_active >= 2
    assert [c.provider for c in results] == ["itunes", "google", "openlibrary"]


def test_enrichment_plugin_entry_point_discovery_and_query(monkeypatch):
    from types import SimpleNamespace
    from audiobookdl.utils import enrichment as enr_mod

    class _FakePlugin:
        capabilities = frozenset({"publisher", "description"})

        def query(self, metadata, **_kw):
            return [
                {
                    "title": metadata.title,
                    "authors": list(metadata.authors),
                    "publisher": "Plugin Press",
                    "description": "Fetched from entry-point plugin",
                }
            ]

    fake_ep = SimpleNamespace(name="localwiki", load=lambda: _FakePlugin())
    monkeypatch.setattr(
        enr_mod.importlib.metadata,
        "entry_points",
        lambda *, group="": [fake_ep] if group == enr_mod.ENRICHMENT_ENTRY_POINT_GROUP else [],
    )
    enr_mod.reset_plugin_enrichment_providers_cache()
    try:
        resolved = enr_mod.resolve_provider_list()
        assert "plugin:localwiki" in resolved
        assert enr_mod.parse_provider_list("localwiki") == ("plugin:localwiki",)
        meta = AudiobookMetadata(title="Plugin Book", authors=["Plugin Author"])
        candidates = search_enrichment_candidates(meta, providers=("plugin:localwiki",))
        assert len(candidates) == 1
        assert candidates[0].publisher == "Plugin Press"
        assert candidates[0].description == "Fetched from entry-point plugin"
    finally:
        enr_mod.reset_plugin_enrichment_providers_cache()


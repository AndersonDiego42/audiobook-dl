# Plugin SDK Guide

`audiobook-dl` supports third-party extensions packaged as standard Python distributions using `importlib.metadata` entry points—no changes to core source files required.

## 1. Source Plugins (`audiobookdl.sources`)

Register subclasses of `audiobookdl.sources.source.Source` in your package's `pyproject.toml`:

```toml
[project.entry-points."audiobookdl.sources"]
my_service = "my_package.source:MyServiceSource"
```

### Discovery & Validation Contract

- **Entry point group**: `"audiobookdl.sources"` (`PLUGIN_API_VERSION = 1`).
- Plugins must subclass `Source`, remain distinct from `Source` itself, define non-empty `names: list[str]` and `match: list[str]`, and match `PLUGIN_API_VERSION` (optional class attribute `plugin_api_version: int = 1`).
- Service names colliding (case-insensitively) with any built-in source or previously loaded plugin are skipped.
- During tests or hot-reloads, clear the cached discovery list with `audiobookdl.sources.reset_plugin_sources_cache()`.

### Minimal Source Adapter Template

```python
from __future__ import annotations

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata, BookId, Chapter, Cover, Result, Series
from audiobookdl.exceptions import BookNotFound, UserNotAuthorized
from audiobookdl.sources.source import CatalogBook, Source


class MyServiceSource(Source):
    plugin_api_version = 1
    names = ["MyService", "my-service"]
    match = [r"https?://(?:www\.)?myservice\.example\.com/listen/\d+/?$"]
    _authentication_methods = ["cookies", "login"]
    login_data = ["username", "password"]
    create_storage_dir = True
    supports_search = True
    supports_library = True

    def _login(self, url: str, username: str, password: str) -> None:
        device_id = self.get_or_create_device_id(
            "myservice_device_id.txt",
            lambda: "device-uuid",
        )
        resp = self._session.post(
            "https://myservice.example.com/api/login",
            json={"username": username, "password": password, "deviceId": device_id},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            raise UserNotAuthorized(detail="Invalid MyService credentials")
        resp.raise_for_status()

    def verify_auth(self, url: str = "") -> bool:
        resp = self._session.get("https://myservice.example.com/api/me", timeout=self._timeout)
        return resp.status_code == 200

    def download(self, url: str) -> Result:
        # Return either `Audiobook(...)` or `Series(title=..., books=[BookId(...) | Audiobook(...)])`
        resp = self._session.get("https://myservice.example.com/api/book/1", timeout=self._timeout)
        if resp.status_code == 401:
            raise UserNotAuthorized(detail="Session expired")
        if resp.status_code == 404:
            raise BookNotFound
        payload = resp.json()

        metadata = AudiobookMetadata(
            title=payload["title"],
            authors=payload.get("authors", []),
            narrators=payload.get("narrators", []),
            scrape_url=url,
            source=self.name,
        )
        cover: Cover | None = self.fetch_cover_safe(payload.get("coverUrl"))
        files = [
            AudiobookFile(
                url=track["url"],
                ext="m4a",
                title=track.get("title"),
                headers={"Authorization": f"Bearer {payload.get('streamToken', '')}"},
            )
            for track in payload.get("tracks", [])
        ]
        chapters = [
            Chapter(start=int(ch["startMs"]), title=ch["title"])
            for ch in payload.get("chapters", [])
        ]
        return Audiobook(
            session=self._session,
            metadata=metadata,
            files=files,
            chapters=chapters,
            cover=cover,
        )

    def search(self, query: str) -> list[CatalogBook]:
        return [
            CatalogBook(
                title="Example Book",
                author="Example Author",
                url="https://myservice.example.com/listen/1",
                source=self.name,
                formats=("audiobook",),
            )
        ]

    def list_library(self) -> list[CatalogBook]:
        return self.search("")
```

### Helpful `Source` Base Utilities

- `self.get_or_create_device_id(filename, factory)`: Atomically reads/writes a private persistent device ID inside `self.database_directory`, scoped automatically per active profile.
- `self.fetch_cover_safe(url, *, default_ext="jpg", headers=None)`: Non-fatally downloads artwork bytes and sniffs the image extension via magic bytes.
- `self.cached_cookie_path()` / `self.save_cookie_file()`: Used automatically by the pipeline to persist profile-scoped login session cookies with restricted permissions and retry once if cached cookies expire.
- `_authentication_methods = ["oauth"]` + `oauth_provider = OAuthProviderConfig(...)`: Enables OAuth 2.0 PKCE loopback (or Device Authorization Grant) sign-in, encrypted per-profile token storage in `CredentialStore`, automatic token refresh, and override hooks `oauth_authorize()` / `apply_oauth_token()`.
- `self.get(url)` / `self.get_json(url)` / `self.post_json(url, ...)` / `self.get_stream_files(m3u8_url)`: Inherit configured timeouts, proxy, TLS/HTTP2 impersonation, and HLS decryption.

---

## 2. Metadata Enrichment Plugins (`audiobookdl.enrichment`)

Packages can also provide custom metadata enrichment providers under the `audiobookdl.enrichment` entry-point group:

```toml
[project.entry-points."audiobookdl.enrichment"]
localwiki = "my_package.enrichment:LocalWikiProvider"
```

### Provider Contract

- The entry point may resolve to either a callable `provider(metadata, *, session=..., proxy=..., timeout=...)` or an object implementing `.query(metadata, *, session=..., proxy=..., timeout=...)`.
- Optional `capabilities: frozenset[str]` attribute allows the enrichment scheduler to skip querying your plugin when all fields it can populate are already present.
- Each returned item may be an `audiobookdl.utils.enrichment.EnrichmentCandidate` or a mapping matching Audiobookshelf custom provider fields (`title`, `authors`, `narrators`, `series`, `series_order`, `genres`, `language`, `description`, `isbn`, `asin`, `publisher`, `cover`, `chapters`).
- Users select installed enrichment plugins using `enrich_providers = "plugin:<entry_point_name>"` (or `<entry_point_name>` in config).
- Clear cached plugin discovery in tests with `audiobookdl.utils.enrichment.reset_plugin_enrichment_providers_cache()`.

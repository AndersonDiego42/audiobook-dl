# Libation-Inspired Feature Implementation Roadmap

This document defines an actionable, phased implementation plan for bringing all **fully feasible** features from [rmcrackan/Libation](https://github.com/rmcrackan/Libation) into `audiobook-dl`.

It builds directly on existing infrastructure (`DownloadArchive` in `audiobookdl/utils/archive.py`, `pack.py`, `options_schema.py`, `credentials.py`, `sidecars.py`, and the Tkinter GUI dialogs) and complements the core bug-fix and optimization items tracked in [`ROADMAP.md`](ROADMAP.md).

---

## Roadmap Overview & Dependency Order

The 7 features fall into 4 sequential phases so foundational data models (multi-account profiles and unified SQLite library state) land before features that consume them (local scanner, exports, Audiobookshelf sync, and headless `sync` + Docker).

```text
Phase 1: Foundation (Data, Accounts & Persistence)
  ├── 1.1 Multi-Account / Multi-Region Profiles
  └── 1.2 Unified Library Ledger, Tags, Rich Search & Soft-Delete
          │
          ▼
Phase 2: Discovery, UI Grouping & Local Indexing
  ├── 2.1 Collapsible Series / Podcast Grouping in UI Trees
  └── 2.2 Local Library Scanner ("Locate Existing Books")
          │
          ▼
Phase 3: Exports & Media Server Integration
  ├── 3.1 Library Export (CSV / JSON / Excel)
  └── 3.2 Audiobookshelf Integration (metadata.json + Upload / Scan Trigger)
          │
          ▼
Phase 4: Automation & Packaging
  └── 4.1 Headless Auto-Sync (`audiobook-dl sync`) & Official Docker Image
```

---

## Phase 1 — Foundation: Multi-Account Profiles & Unified Library State

### 1.1 Multi-Account / Multi-Region Profiles
**Goal**: Support multiple named accounts or library cards per service (e.g., `storytel:personal`, `storytel:family`, `ereolen:card1`, `ereolen:card2`) across TOML config, OS credential storage, CLI, pipeline, and GUI.

#### Architecture & Changes
1. **Configuration (`audiobookdl/config.py`, `audiobookdl/options_schema.py`)**
   - Extend `SourceConfig` with `profile: str = "default"` (and optional `region` / `label` fields if needed).
   - Support nested profile tables in `audiobook-dl.toml`:
     ```toml
     [sources.storytel] # legacy/default profile -> key "storytel" / profile "default"
     username = "main@example.com"

     [sources.storytel.profiles.family]
     username = "family@example.com"
     ```
   - Maintain clean `Config.sources` keying (canonical `<service>` for default, `<service>:<profile>` for named profiles) and update `structure_config()` and `_write_config_dict()` to validate and serialize profiles cleanly.
2. **Credential Storage (`audiobookdl/credentials.py`)**
   - Namespace keyring / DPAPI entries by `(f"{service_key}:{profile}", username)` while remaining backwards-compatible with bare `service_key` lookups for default profiles.
   - Implement `KeyringCredentialStore.stored_services()` (tracked in `ROADMAP.md` §3) so profile discovery works identically across Windows DPAPI, macOS Keychain, and Linux SecretService.
3. **CLI & Pipeline (`audiobookdl/args.py`, `audiobookdl/options_schema.py`, `audiobookdl/pipeline.py`)**
   - Add `--profile <name>` (and optional `profile` field on `DownloadOptions`) to select which configured account authenticates when running downloads, `--library-list`, `--search`, or `--verify-auth`.
   - When persisting session cookies (`Source.cached_cookie_path()` in `audiobookdl/sources/source/__init__.py`), isolate cookie jars per profile (e.g., `db/<source>/session_cookies_<profile>.txt`) with `0o600` permissions on POSIX / restricted ACLs on Windows.
4. **GUI (`audiobookdl/gui/app.py`, `audiobookdl/gui/catalog_dialog.py`)**
   - Add a **Profile** selector/editor in the Settings/Credentials panel and in `CatalogBrowserDialog` so users can add, test, and switch between multiple accounts for the same service.

#### Acceptance Criteria & Tests
- Unit tests in `tests/test_config.py` and `tests/test_credentials.py` covering nested TOML profile parsing, credential round-trips for multiple profiles on one service, and per-profile cookie jar isolation.
- Pipeline test verifying `--profile` selects the matching `SourceConfig` and cookie path.

---

### 1.2 Unified Persistent Library Grid, Tags, Rich Search & Soft-Delete
**Goal**: Evolve `DownloadArchive` (`downloads.db`) from a download-only ledger into a persistent library database tracking remote catalog items, download state, user tags, and soft-deleted/hidden items, exposed through a unified GUI library view.

#### Architecture & Changes
1. **SQLite Schema Migration (`audiobookdl/utils/archive.py`)**
   - Add versioned schema migrations (`PRAGMA user_version`) to `downloads.db`:
     - Extend `downloads` (or introduce a `library_items` / `item_tags` table pair):
       - `profile TEXT NOT NULL DEFAULT 'default'`
       - `status TEXT NOT NULL DEFAULT 'downloaded'` (`pending`, `downloaded`, `hidden`, `trashed`)
       - `series TEXT`, `series_order INTEGER`, `narrator TEXT`, `duration_seconds REAL`, `isbn TEXT`, `asin TEXT`, `last_seen_at TEXT`
       - `tags` normalized via `item_tags(item_id INTEGER, tag TEXT, PRIMARY KEY(item_id, tag))`
   - Keep `make_book_key()` stable across rescans; integrate default ledger checking with `options.skip_downloaded` (respecting `status == 'downloaded'` and excluding `trashed` items from auto-downloads).
2. **Search Query Parser (`audiobookdl/utils/archive.py` or `audiobookdl/utils/search_query.py`)**
   - Implement a lightweight field-aware query parser supporting:
     - Bare terms (substring match across title, author, narrator, series, tags)
     - Field prefixes: `author:`, `title:`, `narrator:`, `series:`, `source:`, `tag:`, `status:` (e.g., `tag:scifi status:pending`)
     - Negation (`-tag:hidden`, `-status:trashed`) and quoted phrases (`author:"Brandon Sanderson"`).
3. **Tagging & Soft-Delete Operations (`audiobookdl/utils/archive.py`, `audiobookdl/args.py`)**
   - Add methods on `DownloadArchive`: `upsert_catalog_items()`, `set_status()`, `add_tags()`, `remove_tags()`, `search_entries()`.
   - Treat `hidden` tag / status as dimmed/filtered by default in UI views unless explicitly queried or toggled ("Show hidden" / "View Trash").
   - Add CLI management flags or subcommands (e.g., `--tag`, `--hide`, `--trash`, `--restore`) or integrate into a `library` subcommand.
4. **Unified GUI Library View (`audiobookdl/gui/archive_dialog.py`, `audiobookdl/gui/catalog_dialog.py`, `audiobookdl/gui/app.py`)**
   - Upgrade `ArchiveLedgerDialog` (or merge with `CatalogBrowserDialog` into a **Library** view) to show status badges (`Downloaded`, `Pending`, `Hidden`, `Trash`), tags column, inline tag editor, "Queue Selected", "Hide/Unhide", "Move to Trash / Restore", and the rich search bar with status/tag filter chips.

#### Acceptance Criteria & Tests
- Migration tests verifying existing `downloads.db` files upgrade cleanly with zero data loss and preserve `UNIQUE(source, book_key)`.
- Unit tests for field-scoped search queries (`author:`, `tag:`, `-status:trashed`), tag normalization (lowercase alphanumeric + underscores), and soft-delete/restore transitions.
- GUI core tests (`tests/test_gui_core.py`) verifying filtering, status toggling, and queueing `pending` items directly to `GuiController`.

---

## Phase 2 — Discovery, UI Grouping & Local Indexing

### 2.1 Collapsible Series / Podcast Grouping in UI Trees
**Goal**: Group episodic/multi-book items (series books and podcast feeds like Podimo/Storytel/eReolen) under collapsible parent rows in `ttk.Treeview` instead of flat ungrouped lists.

#### Architecture & Changes
1. **Model & Metadata Enrichment (`audiobookdl/sources/source/__init__.py`)**
   - Ensure `CatalogBook` carries `series: Optional[str]`, `series_order: Optional[int]`, and optional `item_type: str = "book"` (`"book"` / `"episode"` / `"podcast"`).
   - Update source `list_library()` / `search()` adapters where series/podcast metadata is available to populate these fields.
2. **Tree Grouping Helper (`audiobookdl/gui/widgets.py`, `audiobookdl/gui/catalog_dialog.py`, `audiobookdl/gui/archive_dialog.py`)**
   - Add a reusable grouping mode on `ScrolledTreeview` / dialog controllers ("Group by Series / Podcast" toggle, enabled by default when series/episodes are present):
     - Insert parent group nodes (`iid="group::<series_name>"`) showing aggregate count and completion summary (`3/5 downloaded`).
     - Insert child rows sorted by `series_order` / natural title sort under the parent node.
     - Support "Queue Entire Group" or individual child selection without duplicating URLs.

#### Acceptance Criteria & Tests
- Unit tests verifying grouping order, single-item series handling (optionally ungrouped or single-child toggleable), and recursive selection extraction (`add_selected`).

---

### 2.2 Local Library Scanner ("Locate Existing Books")
**Goal**: Scan user-specified local audiobook directories, extract metadata/identifiers via existing tag and sidecar readers, match against `downloads.db` (or register standalone local books), and prevent redundant re-downloads.

#### Architecture & Changes
1. **Scanner Service (`audiobookdl/utils/library_scanner.py` or `audiobookdl/pack.py`)**
   - Reuse folder discovery and `read_track_tags()`, `read_sidecar_metadata()`, and `infer_metadata_from_path()` from `audiobookdl/output/metadata/reader.py` and `audiobookdl/pack.py`.
   - Walk target directories to identify book folders or standalone `.m4b`/`.mp3`/`.m4a`/`.opus`/`.flac` files:
     - Extract `title`, `authors`, `isbn`, `asin`, `source`, `scrape_url` (including freeform MP4/ID3 `Source` tags and `.opf`/`.json` sidecars).
     - Match against `downloads.db` entries by exact `book_key` / `scrape_url` -> `asin` / `isbn` -> normalized `(title, primary_author)`.
   - Update matched `downloads.db` rows with `status='downloaded'`, resolved `output_path`, and `audio_format`; optionally insert unmatched local books under `source='local'` so the unified library grid tracks the entire collection.
   - Verify whether previously recorded `output_path` files still exist on disk and flag missing files cleanly.
2. **CLI & GUI Entry Points (`audiobookdl/args.py`, `audiobookdl/__main__.py`, `audiobookdl/gui/archive_dialog.py`, `audiobookdl/gui/app.py`)**
   - Add `audiobook-dl scan <dirs...>` (or `--locate-books <dir>`) with `--dry-run` and `--verify-paths`.
   - Add a **Locate Local Books...** button in the Library/Ledger GUI dialog with a progress bar and summary report (`Matched`, `Already Indexed`, `Missing on Disk`, `Unmatched Local`).

#### Acceptance Criteria & Tests
- Tests in `tests/test_library_scanner.py` covering matching by `scrape_url`/`asin`, normalized title+author matching, missing-file detection, and `--skip-downloaded` skipping books discovered by the scanner.

---

## Phase 3 — Exports & Media Server Integration

### 3.1 Library Export (CSV / JSON / Excel)
**Goal**: Export library and download history records cleanly to `.csv`, `.json`, and `.xlsx` for backups, spreadsheets, and external tooling.

#### Architecture & Changes
1. **Export Module (`audiobookdl/utils/export.py`)**
   - Export normalized rows from `DownloadArchive` (`id`, `source`, `profile`, `status`, `title`, `author`, `narrator`, `series`, `series_order`, `isbn`, `asin`, `tags`, `audio_format`, `output_path`, `url`, `downloaded_at`) plus optional `CatalogBook` query results.
   - Use Python stdlib `csv` and `json` for zero-dependency `.csv` and `.json` exports.
   - Support `.xlsx` via an optional dependency (`openpyxl` under `[project.optional-dependencies]` in `pyproject.toml`, e.g., `export = ["openpyxl>=3.1"]`, with a clear actionable error if absent when `.xlsx` is requested).
2. **CLI & GUI (`audiobookdl/args.py`, `audiobookdl/__main__.py`, `audiobookdl/gui/archive_dialog.py`)**
   - Add `audiobook-dl export --format csv|json|xlsx -o library.csv` (respecting the rich search query filter, e.g., `--query "status:downloaded"`).
   - Add an **Export...** button to the Library/Ledger dialog using a standard save-file dialog honoring the active search/filter view.

#### Acceptance Criteria & Tests
- Unit tests verifying UTF-8 BOM/escaping in CSV, valid structured JSON schema, filter application prior to export, and `.xlsx` generation (when `openpyxl` is installed) or graceful `MissingDependency` message.

---

### 3.2 Audiobookshelf Integration (`metadata.json` + Direct Upload / Scan Trigger)
**Goal**: Provide first-class Audiobookshelf (ABS) compatibility—both by writing ABS-native `metadata.json` sidecars and by automatically uploading completed books or triggering ABS library scans after download/repackage.

#### Architecture & Changes
1. **ABS `metadata.json` Sidecar (`audiobookdl/output/metadata/sidecars.py`, `audiobookdl/options_schema.py`)**
   - Add `build_abs_metadata_json(metadata, chapters)` and `write_sidecar_abs_metadata()` producing Audiobookshelf's expected `metadata.json` schema (`title`, `subtitle`, `authors`, `narrators`, `series`, `genres`, `publishedYear`, `publisher`, `description`, `isbn`, `asin`, `language`, `chapters`).
   - Add `--write-abs-metadata` / `write_abs_metadata` to `OPTION_REGISTRY` and expose it in GUI output options (paired naturally with the existing `audiobookshelf` output preset).
2. **ABS API Client (`audiobookdl/integrations/audiobookshelf.py`)**
   - Implement a focused `AudiobookshelfClient` using `requests.Session` (respecting `proxy` and `timeout`):
     - `test_connection()` (`GET /api/me` or `/api/libraries`)
     - `list_libraries()` (`GET /api/libraries`) to fetch library IDs and folder IDs
     - `upload_book(library_id, folder_id, file_paths, metadata)` (`POST /api/upload` multipart upload for audio files, cover image, and sidecars)
     - `trigger_library_scan(library_id)` (`POST /api/libraries/<id>/scan`) for setups where `audiobook-dl` writes directly into a local/NAS folder already mounted by ABS.
3. **Config, Credentials & Pipeline Hook (`audiobookdl/config.py`, `audiobookdl/credentials.py`, `audiobookdl/pipeline.py`, `audiobookdl/pack.py`)**
   - Add `[integrations.audiobookshelf]` section to `audiobook-dl.toml`:
     ```toml
     [integrations.audiobookshelf]
     enabled = false
     url = "https://abs.example.com"
     library_id = "..."
     folder_id = "..."
     mode = "upload" # "upload" (remote POST /api/upload) or "scan" (trigger library scan)
     ```
   - Store the ABS API token in `CredentialStore` (`service_key="audiobookshelf"`) rather than plaintext TOML.
   - Invoke the ABS hook at the end of `process_audiobook()` in `pipeline.py` (and optionally `run_pack()` in `pack.py`) with retry/error reporting that logs clearly without losing the downloaded local files if ABS is unreachable.
4. **GUI Configuration (`audiobookdl/gui/app.py`)**
   - Add an Audiobookshelf integration dialog/section (URL, API token stored securely, "Fetch Libraries" dropdowns for Library/Folder, Upload vs. Scan mode, and "Test Connection").

#### Acceptance Criteria & Tests
- Mocked HTTP tests for `AudiobookshelfClient` (`list_libraries`, `upload_book`, `trigger_library_scan`, auth failure `401`, and network timeout handling).
- Sidecar tests validating `metadata.json` schema output and `pipeline.py` hook execution on completion.

---

## Phase 4 — Automation & Packaging

### 4.1 Headless Auto-Sync (`audiobook-dl sync`) & Official Docker Image
**Goal**: Enable unattended, periodic library synchronization across all configured services/profiles for NAS and server deployments, packaged in an official Docker container with `ffmpeg`.

#### Architecture & Changes
1. **Sync Orchestrator (`audiobookdl/sync.py`, `audiobookdl/__main__.py`, `audiobookdl/args.py`)**
   - Add `audiobook-dl sync` subcommand supporting:
     - `--services <list>` / `--profile <name>` (defaults to all configured authenticated profiles)
     - `--scan-only` (refresh `downloads.db` with remote libraries without downloading)
     - `--watch` / `--interval <seconds|duration>` (daemon loop with `SIGTERM`/`SIGINT` graceful shutdown)
     - `--json` (machine-readable summary events for cron/systemd/container logs)
     - `--dry-run` (report pending books that would be downloaded)
   - Sync flow per configured `(service, profile)`:
     1. Authenticate (reusing persisted session cookies, falling back to configured credentials).
     2. Call `source.list_library()` (skipping sources where `supports_library` is `False` with a clear warning).
     3. Upsert discovered titles into `downloads.db` (`status='pending'` for unseen titles).
     4. Filter out `downloaded`, `hidden`, and `trashed` items.
     5. Download remaining `pending` books via `Downloader`, applying configured output templates, `skip_downloaded`, and post-download hooks (e.g., Audiobookshelf upload/scan).
2. **Docker Image & Compose (`Dockerfile`, `.dockerignore`, `.github/workflows/release.yml`)**
   - Create a multi-stage `Dockerfile` based on `python:3.12-slim` (or `3.13-slim`):
     - Install `ffmpeg` (includes `ffprobe`) and minimal runtime dependencies.
     - Run as a non-root user (`PUID`/`PGID` friendly or standard unprivileged user) with volume mounts for `/config` (`audiobook-dl.toml` + `db/`) and `/audiobooks` (output directory).
     - Set `AUDIOBOOK_DL_CONFIG=/config/audiobook-dl.toml` / `--config` defaults and container-friendly credential handling (documented headless auth via cookie files or explicit env opt-in where OS keyrings are unavailable).
   - Provide an example `docker-compose.yml` snippet in `README.md` showing `audiobook-dl sync --watch --interval 6h`.
   - Extend `.github/workflows/release.yml` to build and publish multi-arch (`linux/amd64`, `linux/arm64`) images on tagged releases.

#### Acceptance Criteria & Tests
- Integration tests for `run_sync()` verifying multi-service polling, incremental diffing against `downloads.db`, `--scan-only` / `--dry-run` behavior, and skipping `hidden`/`trashed` entries.
- CI check validating `docker build` succeeds and `audiobook-dl --version` + `ffmpeg -version` execute inside the container.

---

## Cross-Cutting Requirements

- **Single Source of Truth**: Register all new CLI/Config/GUI flags in `audiobookdl/options_schema.py` (`OPTION_REGISTRY`) rather than duplicating defaults across `args.py`, `config.py`, and `gui/app.py`.
- **Non-TTY / Redirect Safety**: Ensure `rich` progress output degrades cleanly when stdout/stderr is redirected in cron/Docker runs.
- **Type Checking & Linting**: Keep all new modules compatible with `mypy` (`disallow_untyped_calls = true` in `pyproject.toml`), `ruff`, and `pytest -m "not e2e"` before merging each phase.


---

## Implementation Status

| Phase | Feature | Primary Modules / Commands | Status |
|---|---|---|---|
| **1.1** | Multi-Account / Multi-Region Profiles | `audiobookdl/config.py`, `credentials.py`, `options_schema.py`, `pipeline.py`, `gui/app.py`, `gui/catalog_dialog.py` (`--profile`) | Implemented |
| **1.2** | Unified Library Ledger, Tags, Rich Search & Soft-Delete | `audiobookdl/utils/archive.py`, `gui/archive_dialog.py`, `audiobook-dl library` | Implemented |
| **2.1** | Collapsible Series / Podcast Grouping in UI Trees | `audiobookdl/gui/widgets.py`, `gui/catalog_dialog.py`, `gui/archive_dialog.py` | Implemented |
| **2.2** | Local Library Scanner ("Locate Existing Books") | `audiobookdl/utils/library_scanner.py`, `audiobook-dl scan`, `gui/archive_dialog.py` | Implemented |
| **3.1** | Library Export (CSV / JSON / XLSX) | `audiobookdl/utils/export.py`, `audiobook-dl export`, `gui/archive_dialog.py` | Implemented |
| **3.2** | Audiobookshelf Integration (`metadata.json` + Upload / Scan) | `audiobookdl/integrations/audiobookshelf.py`, `sidecars.py`, `output/download.py`, `pipeline.py`, `pack.py`, `gui/abs_dialog.py` (`--write-abs-metadata`, `--upload-to-abs`) | Implemented |
| **4.1** | Headless Auto-Sync & Official Docker Image | `audiobookdl/sync.py`, `audiobook-dl sync`, `Dockerfile`, `.dockerignore`, `.github/workflows/release.yml` | Implemented |

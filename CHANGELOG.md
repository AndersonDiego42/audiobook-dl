# Changelog

All notable changes to `audiobook-dl` are documented in this file.
Format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Parse-Only PSSH & `tenc` CENC Metadata Inspection**: Added `audiobookdl.utils.pssh` to parse ISO BMFF `pssh` boxes (Widevine protobuf `key_id`, PlayReady PRO/WRMHEADER with GUID byte-swapping, ClearKey, FairPlay), `tenc` default KIDs, and HLS `data:` key URIs; DRM-protected HLS errors now surface the DRM system and KID(s), `KID:KEY` mismatches fail fast before decryption, and single-KID bare keys bind automatically to `-decryption_keys` (parse-only; no license acquisition or CDM).
- **Desktop GUI**: Five-page Tkinter Desktop interface (**Download**, **Library**, **Pack**, **Accounts**, **Settings**) with persistent queues (`1`–`4` parallel jobs), per-job overrides, series selection/preview, interactive chapter editor, metadata enrichment diff picker, Audiobookshelf upload & duplicate checks, live `"system"` dark/light theme following, keyboard shortcuts, and drag-and-drop support.
- **Library SQLite Archive & FTS5 Search**: Versioned schema (`downloads.db`) with WAL mode, persistent connections, trigram FTS5 full-text search across title/author/series/tags, normalized output-path indexing, batch key lookups, local folder scanning, and `.csv`/`.json`/`.xlsx` exports.
- **Metadata Enrichment & Plugin SDK**: Built-in Audnexus, iTunes, Google Books, and OpenLibrary enrichment plus `EnrichmentCache`, custom Audiobookshelf metadata providers (`[enrichment.custom.<name>]`), `audiobookdl.enrichment` plugin entry points, and validated `audiobookdl.sources` (`PLUGIN_API_VERSION = 1`).
- **CI/CD & Documentation**: Multi-OS CI matrix (`ubuntu-latest`, `windows-latest`, `macos-latest`) across Python `3.10`/`3.11`/`3.13` with coverage reporting, PyInstaller Windows build verification, PyPI trusted publishing workflow, [docs/configuration.md](./docs/configuration.md), [docs/plugins.md](./docs/plugins.md), [CONTRIBUTING.md](./CONTRIBUTING.md), and [SECURITY.md](./SECURITY.md).

### Changed
- **Media & HLS Pipeline**: FFmpeg concat-demuxer merging (eliminating redundant `combined.ts` byte copies), live `-progress` telemetry during silence detection, atomic staged container tagging prior to `os.replace`, `locale`-ranked HLS audio rendition selection, UTF-8 BOM tolerance, pre-write bandwidth rate limiting, and exposed `silence_noise_db` / `silence_min_seconds` options.
- **Source Adapters & Auth Recovery**: Shared `Source.get_or_create_device_id()`, single automatic re-authentication retry when cached cookies fail with `UserNotAuthorized`, and lock-guarded `EreolenSource.refresh_library_domains()`.
- **Configuration & Logging**: Comment-preserving TOML updates via `tomlkit`, regex-based markup stripping/escaping (removing the `rich` runtime dependency), and safer `post_exec` `list[str]` execution with `shell=False`.

### Security
- Hardened credential storage (DPAPI vault validation, Win32 owner-only DACL enforcement, atomic writes for `PlaintextFallbackCredentialStore`, non-loopback `http://` Bearer token warnings) and SQL-side host filtering for browser cookie imports.

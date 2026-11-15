# Audiobook-dl Enhancement & Optimization Roadmap

> **Audit Baseline**: `audiobook-dl` v`0.7.3` ([`audiobookdl/__init__.py`](audiobookdl/__init__.py))  
> **Test Baseline**: `python -m pytest -m "not e2e" -q` -> `240 passed, 8 deselected in 44.19s` (`ruff` and `mypy` were not present on `PATH` during audit)

This document catalogs verified bugs, performance bottlenecks, security/resilience improvements, architectural refactors, CLI/GUI enhancements, source adapter upgrades, and major new feature opportunities across the codebase.

---

## Implementation Priority Matrix

| Priority | Theme | Key Outcomes |
| :--- | :--- | :--- |
| **P0 — Immediate** | Correctness & Data Safety | Fix GUI error tuple slicing, AES/Chirp unpadding, `YourCloudLibrarySource._login()` error handling, `Archive` ledger skip integration, safe staging directories, and cookie persistence/permissions |
| **P1 — High** | Performance & Stability | Collapse multi-pass M4B/FFmpeg/Mutagen rewrites, parallelize chapter splitting, add `429`/`5xx` streaming retries with safer default concurrency, reuse HTTP/SQLite sessions |
| **P2 — Medium** | Architecture & UX Parity | Modularize `audiobookdl/gui/app.py`, unify CLI/Config/GUI option schemas, expose missing CLI flags in GUI, upgrade CLI progress & throttling |
| **P3 — Expansion** | Adapters & New Features | Expand `search()` / `list_library()` coverage across adapters, add Libby / Audnexus enrichment / Watch-Sync / Web UI + Docker / Local `pack` mode |

---

## 1. High-Priority Bugs & Correctness Fixes

### Core, Pipeline & Encryption
- [x] **Fix failed-job error formatting in GUI** — [`audiobookdl/gui/app.py`](audiobookdl/gui/app.py#L2839-L2841)  
  `summary.book_errors` is `List[Tuple[str, str]]` per [`audiobookdl/pipeline.py`](audiobookdl/pipeline.py#L39). Currently `detail = summary.book_errors[0]` yields a tuple and `detail[:120]` slices a tuple instead of formatting a string (e.g., `f"{detail[0]}: {detail[1]}"[:120]`).
- [ ] **Integrate `downloads.db` ledger with `skip_downloaded`** — [`audiobookdl/pipeline.py`](audiobookdl/pipeline.py#L771-L799)  
  `downloads.db` is written unconditionally when `database_directory` is set (L797), yet archive deduplication (L777–L784) only runs `if explicit_archive:` (`--download-archive`). Integrate default ledger checking with `options.skip_downloaded` (with an opt-out such as `--no-download-archive`) so repeated runs skip previously recorded books cleanly even if moved/renamed on disk.
- [x] **Fix PKCS7 padding & block-alignment handling in AES decryption**
  - [`audiobookdl/output/encryption.py`](audiobookdl/output/encryption.py#L26-L32): `AESStreamDecryptor.finalize()` calls `self._cipher.decrypt(tail)` without guarding non-16-byte-aligned tails (which raises `ValueError`), and neither `AESStreamDecryptor` nor `decrypt_file_aes()` (L68) unpads PKCS7 trailing bytes.
  - [`audiobookdl/sources/chirp.py`](audiobookdl/sources/chirp.py#L70): `get_audio_url` strips `[:-1]` assuming padding length `1` instead of using proper PKCS7 unpadding (`Crypto.Util.Padding.unpad`), which corrupts decrypted URLs whenever padding length is `> 1`.
- [ ] **Close leaked `requests.Session` instances & respect proxy/SSL config**
  - [`audiobookdl/utils/http.py`](audiobookdl/utils/http.py#L18): `redirect_of()` calls bare `requests.get(...)` per hop, bypassing `Source._session`, cookies, custom headers, `SSLContext`, and `--proxy` (used by [`audiobookdl/sources/podimo.py`](audiobookdl/sources/podimo.py#L101)). Accept a `Session` or proxy/verify configuration instead.
  - [`audiobookdl/utils/enrichment.py`](audiobookdl/utils/enrichment.py#L23) (and L75) & [`audiobookdl/utils/updater.py`](audiobookdl/utils/updater.py#L66): Create unclosed `requests.Session()` objects whenever `session=None` (including calls from `pipeline.py` L340 & L787). Manage sessions with `with requests.Session() as sess:` or reuse a shared caller session.
- [ ] **Remove module-global mutable state breaking `Downloader` isolation**
  - [`audiobookdl/utils/__init__.py`](audiobookdl/utils/__init__.py#L60): `_custom_ffmpeg_dir` is module-global, leaking across concurrent/multiple `Downloader` instances with distinct `Config.ffmpeg_dir`. Move it into `JobContext` / `DownloadOptions`.
  - [`audiobookdl/__main__.py`](audiobookdl/__main__.py#L26-L28): Mutates module-level flags on `audiobookdl.logging` even though `Downloader` encapsulates state in `JobContext`.
- [ ] **Make directory preparation atomic (`setup_download_dir`)** — [`audiobookdl/output/download.py`](audiobookdl/output/download.py#L926)  
  Calling `shutil.rmtree(path)` before downloading deletes an existing book directory up front; if a transfer fails or cancels mid-way, the previous files are lost. Stage downloads in a temporary directory and atomically replace/commit after completion.
- [x] **Fix metadata typing, identity checks & dropped ID3 keys**
  - [`audiobookdl/utils/audiobook.py`](audiobookdl/utils/audiobook.py#L89): Replace `== None` / `== True` with `is None` / `is True`, and update `all_properties_dict()` ([L119](audiobookdl/utils/audiobook.py#L119)) return annotation from `Dict[str, str]` to match actual value types (`date`, `int`, `pycountry.db.Data`).
  - [`audiobookdl/output/metadata/id3.py`](audiobookdl/output/metadata/id3.py#L85-L86): Keys absent from `EasyID3.valid_keys` (such as `source`, unlike `isbn` registered at L18) are silently dropped, whereas [`audiobookdl/output/metadata/mp4.py`](audiobookdl/output/metadata/mp4.py#L69-L72) persists them as freeform atoms (`----:com.apple.iTunes:Source`). Register/map custom ID3 TXXX frames consistently.
- [ ] **Wire Cancel support during GUI metadata previews** — [`audiobookdl/gui/app.py`](audiobookdl/gui/app.py#L2326-L2372)  
  `_preview_cancel_event` is initialized, yet the Cancel button stays disabled during preview fetches because `_set_running_state` is not engaged for preview jobs.

### Source Adapter Bugs
- [x] **Everand (`everand.py`)**: Wire unused [`get_chapters()`](audiobookdl/sources/everand.py#L248-L256) into `download_book_from_url()` (L76–L81); select the highest-bandwidth stream from the master playlist instead of hardcoding `96kbps.m3u8`; remove dead `download_book_id()` (L172); escape `.` in URL regex `(www\.)?`.
- [x] **YourCloudLibrary (`yourcloudlibrary.py`)**: Inspect the login POST response in [`_login()`](audiobookdl/sources/yourcloudlibrary.py#L139-L149) and raise `UserNotAuthorized` on failed authentication instead of discarding the response object.
- [x] **Saxo (`saxo.py`)**: Guard empty search results in `_search_for_book()` before indexing `items[0]` to avoid `IndexError`.
- [x] **Podimo (`podimo.py`)**: Return `None` from `download_cover()` on HTTP 404 instead of failing the book; replace `"main.m3u8"` -> `"stream_audio_high/stream.m3u8"` string replacement in `get_podcast_file()` with proper master playlist selection via `get_stream_files()`.
- [x] **Storytel (`storytel.py`)**: Convert `_download_counter` from a mutated class attribute (`self._download_counter += 1`) into a proper instance attribute initialized in `__init__`.

---

## 2. Performance & Resource Optimizations

- [x] **Collapse redundant FFmpeg & Mutagen full-file rewrites** — [`audiobookdl/output/output.py`](audiobookdl/output/output.py#L336-L344)  
  Multi-part MPEG-TS combines currently skip inline chapter muxing (`if len(ts_paths) == 1 and chapters`), forcing a second full container pass in `add_chapters_ffmpeg()`, followed by `mutagen` rewriting atoms for tags and cover art. For 1 GB+ `.m4b` files this multiplies disk I/O 3x–4x. Embed chapters metadata, tags, and cover art during the primary remux/combine FFmpeg invocation whenever possible.
- [x] **Parallelize `split_audio_by_chapters()` & improve cut accuracy** — [`audiobookdl/output/output.py`](audiobookdl/output/output.py#L588-L628)  
  Currently runs sequential `ffmpeg` subprocesses per chapter and places `-ss` before `-i` with `-c:a copy` (risking keyframe snap drift). Dispatch chapter exports via `run_cancellable_pool()` and offer exact-seek / re-encode fallback.
- [x] **Cache `ffprobe` metadata across pipeline stages**  
  Avoid redundant `ffprobe` spawns (e.g., probing `ts_paths[0]` and immediately re-probing `combined_ts`) by attaching cached `MediaProbeInfo` to intermediate file descriptors.
- [x] **Tune default concurrency & add CDN retry/backoff in `download_file()`** — [`audiobookdl/output/download.py`](audiobookdl/output/download.py#L585-L798), [L853](audiobookdl/output/download.py#L853)  
  `download_files()` defaults to `MAX_DOWNLOAD_WORKERS = 20` concurrent connections per book, risking `429` throttling or account anti-abuse locks. Lower the default worker cap (e.g., 4–8, tunable via `--max-workers`) and add exponential backoff + `Retry-After` handling for HTTP `429` and `5xx` responses inside `download_file()` (which currently only catches `RequestException`).
- [x] **Reuse cached scrapes & shared HTTP sessions**
  - [`audiobookdl/sources/audiobooksdotcom.py`](audiobookdl/sources/audiobooksdotcom.py#L137): Reuse `_get_page(scrape_url)` in `extract_file()` instead of calling `self.get(scrape_url)` a second time.
  - Pass persistent HTTP sessions into `enrich_metadata()` and `check_for_updates()`.
- [x] **Gate Storytel raw JSON disk dumps behind debug mode** — [`audiobookdl/sources/storytel.py`](audiobookdl/sources/storytel.py)  
  `download_lists_api()` and `download_bookshelf()` unconditionally write JSON payloads (`books/`, `playback-metadata/`, `lists/`) to `database_directory`. Make debug JSON persistence opt-in.
- [x] **Fix GUI `ArchiveLedgerDialog` SQLite re-opening & virtualize series pickers** — [`audiobookdl/gui/app.py`](audiobookdl/gui/app.py#L1155)  
  Keep a single `DownloadArchive` instance/connection open in `ArchiveLedgerDialog` and debounce `search_var` filter events (150–250ms) rather than re-opening SQLite on every keystroke. In `PreviewDialog`, replace per-episode `ttk.Checkbutton` canvas stacks with a virtualized `ttk.Treeview` for large series.

---

## 3. Security, Reliability & Resilience

- [ ] **Persist session cookies after normal `login()` with `0o600` permissions**
  - [`audiobookdl/pipeline.py`](audiobookdl/pipeline.py#L629-L686): Save cookies after normal authentication in `authenticate()` (not solely inside `verify_service_auth()`) so subsequent CLI/GUI runs reuse live tokens instead of re-logging in on every execution.
  - [`audiobookdl/sources/source/__init__.py`](audiobookdl/sources/source/__init__.py#L105-L115): Enforce owner-only file permissions (`0o600` on POSIX / user-restricted ACLs on Windows) when writing cookie jars.
- [ ] **Implement `KeyringCredentialStore.stored_services()` & multi-account profiles** — [`audiobookdl/credentials.py`](audiobookdl/credentials.py#L219-L269)  
  `KeyringCredentialStore` lacks `stored_services()` (implemented on `DpapiCredentialStore` at L219–L226), making Windows vs. Linux/macOS credential discovery inconsistent in [`audiobookdl/config.py`](audiobookdl/config.py#L313-L324). Support multiple named accounts per service (e.g., `storytel:personal`, `ereolen:card2`).
- [ ] **Pre-commit media integrity verification**  
  Validate downloaded/combined audio with `ffprobe` (verifying non-zero duration, valid audio stream, and truncation check) *before* replacing existing files or marking entries completed in `downloads.db`.
- [ ] **Replace manual TOML writer with `tomli-w`** — [`audiobookdl/config.py`](audiobookdl/config.py)  
  Replace hand-rolled `_dump_toml()` serialization with `tomli-w` to eliminate edge-case escaping bugs on Windows paths, special characters in templates, or nested tables.
- [ ] **Anti-bot / Cloudflare handling options**  
  Add optional `curl_cffi` TLS impersonation or `browser-cookie3` session import for Cloudflare-gated catalogs.

---

## 4. Architecture, Code Quality & Packaging

- [x] **Decompose `audiobookdl/gui/app.py` (3,203 lines)**  
  Split into clean submodules under `audiobookdl/gui/`: state/queue controller (`controller.py`), `PreviewDialog` (`preview_dialog.py`), `ArchiveLedgerDialog` (`archive_dialog.py`), `CatalogBrowserDialog` (`catalog_dialog.py`), formatting helpers (`formatting.py`), reusable widgets (`widgets.py`), and the main window view (`app.py`).
- [x] **Unify runtime option definitions across CLI, Config, Pipeline & GUI**  
  Options are currently synchronized manually across [`args.py`](audiobookdl/args.py), [`config.py`](audiobookdl/config.py), [`pipeline.py`](audiobookdl/pipeline.py), and [`app.py`](audiobookdl/gui/app.py). Introduce a declarative option registry/schema as the single source of truth for defaults, serialization, and UI/CLI bindings.
- [ ] **Entry-point plugin architecture for sources** — [`audiobookdl/sources/__init__.py`](audiobookdl/sources/__init__.py)  
  Replace manual enumeration in `get_source_classes()` with `importlib.metadata.entry_points(group="audiobookdl.sources")` alongside built-in sources so third-party adapters load dynamically.
- [ ] **Externalize hardcoded overrides in `storytel.py`** — [`audiobookdl/sources/storytel.py`](audiobookdl/sources/storytel.py#L44-L101)  
  Move `metadata_corrections` and inline SVG assets out of Python source into JSON/TOML data assets and support user-defined override files.
- [ ] **Clean up packaging & release targets**
  - [`pyproject.toml`](pyproject.toml) & [`setup.py`](setup.py): Remove legacy `setup.py` if PEP 517 `pyproject.toml` suffices, and align `requires-python = ">=3.9"` (EOL) with CI targets (`3.11`, `3.13` in [`.github/workflows/ci.yml`](.github/workflows/ci.yml#L15)).
  - [`flake.nix`](flake.nix#L41-L101): Add missing `keyring` and `ruff` packages and fix `dateutils` -> `python-dateutil`.
  - [`scripts/build_exe.py`](scripts/build_exe.py) & [`.github/workflows/release.yml`](.github/workflows/release.yml): Windows builds currently produce a `--windowed` GUI-only executable; provide CLI dispatch or a companion CLI binary, plus macOS/Linux release artifacts.

---

## 5. CLI, Output Pipeline & Metadata Enhancements

- [ ] **Speed, byte count & ETA in CLI progress bars** — [`audiobookdl/output/download.py`](audiobookdl/output/download.py#L26-L31)  
  Add `rich.progress` `DownloadColumn`, `TransferSpeedColumn`, and `TimeRemainingColumn` so single-file downloads show byte-level progress and live throughput.
- [ ] **Real bandwidth rate limiting (`--limit-rate`)**  
  Distinguish per-book sleep (`--book-delay`) from a token-bucket byte-rate cap (`--limit-rate 2M`) enforced inside chunked downloads.
- [ ] **Machine-readable `--json` mode & post-download hooks**  
  Emit structured JSON for `--list-sources`, `--search`, `--library-list`, `--info`, and download summaries; add `--post-exec <cmd>` / webhook notifications (Discord, ntfy, Audiobookshelf library refresh).
- [ ] **Dry-run & shell completions**  
  Add `--dry-run` (resolves templates, skip rules, and metadata without downloading) and generate Bash/Zsh/Fish/PowerShell completions.
- [ ] **Audio post-processing & silence-based chaptering**  
  Add `--loudnorm` (EBU R128 normalization), `--mono` (voice-friendly size reduction), `--sample-rate`, `--chapters-from-silence` (FFmpeg `silencedetect` for chapterless books), `--chapter-offset <ms>`, and `--square-cover` / max-dimension cover resizing.
- [ ] **Audiobookshelf `metadata.json` export**  
  Generate Audiobookshelf-compatible `metadata.json` alongside existing `.opf` and `.cue` sidecars.

---

## 6. Desktop GUI Enhancements

- [x] **Reach full CLI feature parity in the GUI**  
  Add controls for `--split-chapters`, `--write-opf`, `--write-cue`, `--write-cover`, `--download-archive` toggle, `--max-workers`, `--book-delay`, `--proxy`, `--timeout`, `--overwrite`, `--remove-chars`, and `--cover`-only downloads.
- [x] **Persist all GUI toggles across sessions**  
  Save `skip_downloaded`, `write_json_metadata`, `enrich_metadata`, `no_chapters`, and `debug` alongside output preferences in `audiobook-dl.toml`.
- [ ] **Persistent job queue, Pause/Resume & per-phase sub-progress**  
  Persist queued/failed jobs across restarts, allow pausing/resuming queues, retry failed jobs in one click, and display multi-stage progress (Download -> Decrypt -> Remux/Combine -> Tagging).
- [ ] **Inline metadata editor in `PreviewDialog` & dark theme**  
  Allow editing Title, Authors, Narrators, Series, Order, and Cover inside `PreviewDialog`; replace hardcoded light-mode colors (`#555555`, `#333333`, `#1f6f3d`, `#9a6700`, `#b42318`) with light/dark theme support and HiDPI scaling.
- [ ] **Interactive browser-cookie importer & OS completion notifications**  
  Add a cookie import helper in Settings and desktop notifications when queued downloads finish.

---

## 7. Existing Source Adapter Upgrades

| Source | Planned Enhancements |
| :--- | :--- |
| [`audiobooksdotcom.py`](audiobookdl/sources/audiobooksdotcom.py) | Extract authors, narrators, description, series, and chapters; preserve library order and populate real titles/authors in `list_library()`; reuse `_get_page()` in `extract_file()` |
| [`blinkist.py`](audiobookdl/sources/blinkist.py) | Support non-`/en/` locales and `/books/...` URLs; extract chapters, narrators, and descriptions; add `search()` and `list_library()` |
| [`bookbeat.py`](audiobookdl/sources/bookbeat.py) | Expose `supports_library = True` (`list_library()`) and `search()`; allow downloading arbitrary catalog URLs without requiring books to be in `saved_books`; persist per-user random `device_id` instead of static `uuid3` |
| [`chirp.py`](audiobookdl/sources/chirp.py) | Support `/audiobooks/...` catalog URLs alongside `/player/\d+`; implement `list_library()` for purchased titles; replace `[:-1]` hack with PKCS7 unpadding |
| [`ereolen.py`](audiobookdl/sources/ereolen.py) | Re-enable `get_cover(meta)`; extract chapters, publisher, ISBN, series, and description; refresh `libraries.txt` dynamically; surface actionable "Not currently loaned" errors |
| [`everand.py`](audiobookdl/sources/everand.py) | Wire unused `get_chapters()`; select best stream from master `.m3u8` instead of hardcoded `96kbps.m3u8`; support `/book/` URLs, `list_library()`, `search()`, and full metadata (ISBN, series, language, release date) |
| [`librivox.py`](audiobookdl/sources/librivox.py) | Query `/api/feed/audiobooks` JSON API for full author, narrator, genre, language, description, and chapter metadata; implement RSS/API `search()` |
| [`nextory.py`](audiobookdl/sources/nextory.py) | Support profile selection beyond `profiles[0]` and configurable locales; resolve HLS master/media redirects cleanly (`# TODO` at L251); add `search()`, real chapter titles, and per-user `device_id` |
| [`overdrive.py`](audiobookdl/sources/overdrive.py) | Support modern **Libby (`libbyapp.com`)** APIs alongside `listen.overdrive.com`; extract ISBN, series, description, and publisher; validate auth cookies early |
| [`podimo.py`](audiobookdl/sources/podimo.py) | Route `retrieve_id_from_share_link()` through `Source._session`; use `get_stream_files()` on `main.m3u8`; add `search()`, `list_library()`, and chapters |
| [`saxo.py`](audiobookdl/sources/saxo.py) | Guard empty `_search_for_book()` results; enrich metadata via URL ISBN (description, release date, language, chapters); avoid hardcoded app version constants |
| [`storytel.py`](audiobookdl/sources/storytel.py) | Implement `supports_search = True` (`search()`); fix `_download_counter` class attribute; gate raw JSON dumps behind debug mode; externalize `metadata_corrections` |
| [`yourcloudlibrary.py`](audiobookdl/sources/yourcloudlibrary.py) | Check `_login()` HTTP response and raise `UserNotAuthorized` on invalid credentials; add `list_library()` and broader URL support |

---

## 8. Major New Features

1. **Multi-Provider Metadata Enrichment** ([`audiobookdl/utils/enrichment.py`](audiobookdl/utils/enrichment.py))  
   Extend `--enrich` beyond OpenLibrary to query **Audnexus** (chapters, ASIN, narrators, high-resolution covers), **Google Books**, and **iTunes Search API** with local response caching and interactive/confidence-scored matching.
2. **Bookshelf Auto-Sync & Watch Mode (`audiobook-dl sync`)**  
   Daemon/scheduled command that polls configured services' `list_library()` endpoints, diffs against `downloads.db`, and automatically downloads newly added books for NAS/Audiobookshelf setups.
3. **Web UI / Headless REST Server + Official Docker Image**  
   Expose `Downloader` jobs and `DownloadEvent` progress over a FastAPI/WebSocket API and browser UI packaged with `ffmpeg` in an official container image.
4. **Local Audiobook Repackager (`audiobook-dl pack`)**  
   Offline subcommand to merge folders of `.mp3`/`.m4a` tracks into chaptered `.m4b` files, run metadata enrichment, embed covers, and apply `--output-template` naming without touching a remote service.
5. **New Supported Sources**  
   - **RSS / Podcast Feeds** (`.xml` feeds into single `.m4b` or episodic output)
   - **Internet Archive** (`archive.org` public domain collections)
   - **Libby (`libbyapp.com`)**, **Libro.fm**, **Spotify Audiobooks**, and **Hoopla**
6. **Companion Ebook Pairing (`--with-ebook`)**  
   On dual-format platforms (`BookBeat`, `Storytel`, `Nextory`, `Everand`), optionally fetch companion `.epub` / read-along files alongside the audiobook into the same resolved output directory.

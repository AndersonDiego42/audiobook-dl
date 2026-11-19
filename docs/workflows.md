# Runtime & Release Workflows

This guide documents the primary runtime execution pipelines and build/release automation implemented in `audiobook-dl`.

## 1. End-to-End Download Pipeline

Triggered by GUI download jobs, `Downloader.download()`, or GUI library sync:

```mermaid
sequenceDiagram
    participant Caller as Desktop GUI / API Caller
    participant DL as Downloader (pipeline.py)
    participant Src as Source Adapter
    participant Auth as CredentialStore / Cookies
    participant IO as download.py / output.py
    participant DB as DownloadArchive (downloads.db)
    participant Hook as ABS & post_exec

    Caller->>DL: download(urls) inside JobContext
    DL->>Src: find_compatible_source(url) (or resolve_redirect_url)
    alt requires_authentication
        DL->>Auth: Check cookie_file or cached session_cookies
        Auth-->>Src: Load Netscape jar or authenticate via _login()
        Src->>Auth: save_cookie_file() (0600 permissions)
    end
    DL->>Src: source.download(url) -> Audiobook or Series
    DL->>DB: Skip if skip_downloaded / ledger contains book_key
    opt enrich_metadata
        DL->>DL: Query Audnexus, iTunes, Google Books, OpenLibrary (with SQLite cache)
    end
    DL->>IO: download_audiobook() (staged temp dir + Range resume + 429/5xx backoff)
    IO->>IO: Decrypt AES-CBC if encrypted + verify_audio_integrity(ffprobe)
    IO->>IO: Combine/convert/split via FFmpeg + embed MP4/ID3/Vorbis tags & sidecars
    IO->>DB: archive.record(..., status="downloaded")
    opt Audiobookshelf auto_upload / auto_scan / upload_to_abs
        DL->>Hook: upload_book() / scan_library()
    end
    opt post_exec
        DL->>Hook: Run trusted command with AUDIOBOOK_DL_* env vars (300s timeout)
    end
```

### Staging & Integrity Guarantees
- **Multi-part & chapter-split safety**: [`audiobookdl/output/download.py`](../audiobookdl/output/download.py) downloads multi-file books into a sibling temporary `.staging-*` directory and verifies audio integrity with `ffprobe` (`output.verify_audio_integrity()`) before atomically committing targets or updating `downloads.db`.
- **Resumable transfers**: With `resume` enabled, partial segments stay in `.tmp` files and resume via HTTP `Range` requests (`Content-Range` validated; stale temp files are reset automatically when servers ignore ranges).
- **DRM / PSSH Inspection**: [`audiobookdl/utils/drm_inspect.py`](../audiobookdl/utils/drm_inspect.py) and the **Inspect…** dialog beside `decryption_key` perform parse-only inspection of ISO BMFF `pssh`/`tenc` boxes, HLS playlists, and DASH MPD `<ContentProtection>` (`cenc:pssh` and `cenc:default_KID`) without license acquisition.

---

## 2. Offline Local Repackaging (`PackPage`)

[`audiobookdl/pack.py`](../audiobookdl/pack.py) converts local audio folders or track lists into chaptered `.m4b` (or configured output format) files without contacting a remote source:

```mermaid
flowchart LR
    In["Input Folders / Tracks"] --> Discover["discover_pack_groups()"]
    Discover --> Sort["sort_track_paths() by (disc, track) or natural filename"]
    Sort --> ReadMeta["Extract track tags + sidecars (metadata.json, audiobook.json, *.opf, *.cue, cover.*/folder.*)"]
    ReadMeta --> Merge["Merge GUI Overrides (Title, Author, Series, Cover, Chapters)"]
    Merge --> Enrich["Optional Metadata & Cover Enrichment"]
    Enrich --> Render["Combine/Convert via FFmpeg + Apply Loudnorm/Mono + Chapter Offset + Embed Metadata/Sidecars"]
    Render --> Post["Optional post-exec & delete source"]
```

---

## 3. GUI Library Sync

- **`run_sync_once()`** ([`audiobookdl/sync.py`](../audiobookdl/sync.py)):
  1. `_resolve_sync_targets()` selects configured `(service, profile)` pairs whose adapter has `supports_library = True` (or specific selected targets).
  2. Authenticates and calls `list_library()`, upserts discovered items into `downloads.db` (`upsert_catalog_items()`), filters out `downloaded`, `trashed`, and `hidden`-tagged entries, and downloads remaining `pending` URLs unless `scan_only` or `dry_run` is active.
  3. The Desktop GUI schedules periodic sync passes via `AutomationCoordinator` when enabled on the **Library -> Sync** tab.

---

## 4. CI & Release Packaging

```mermaid
flowchart TD
    Push["Push / PR (master, main)"] --> CI[".github/workflows/ci.yml (ubuntu-latest & windows-latest, Python 3.11 & 3.13)"]
    CI --> Lint["ruff check audiobookdl tests scripts"]
    CI --> TypeCheck["mypy audiobookdl"]
    CI --> UnitTest["pytest -m 'not e2e' -q"]
    CI --> E2ETest["pytest -m e2e -v (xvfb-run on Linux)"]

    Tag["Git Tag v*"] --> Rel[".github/workflows/release.yml"]
    Rel --> WinJob["Windows Job: pytest + build_exe.py (--onefile & --onedir) + --self-test"]
```

- **Windows Portable Builds** ([`scripts/build_exe.py`](../scripts/build_exe.py)): Packages [`audiobookdl/gui/app.py`](../audiobookdl/gui/app.py) (`audiobook-dl.exe`) with PyInstaller (`--onefile` or `--onedir`), optionally copies or bundles `ffmpeg`/`ffprobe`, and verifies builds via `--self-test`.

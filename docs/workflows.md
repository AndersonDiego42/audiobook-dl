# Runtime & Release Workflows

This guide documents the primary runtime execution pipelines and build/release automation implemented in `audiobook-dl`.

## 1. End-to-End Download Pipeline

Triggered by CLI URLs (`audiobookdl/__main__.py`), `Downloader.download()`, GUI jobs, `sync`, or `POST /api/download`:

```mermaid
sequenceDiagram
    participant Caller as CLI / GUI / API Caller
    participant DL as Downloader (pipeline.py)
    participant Src as Source Adapter
    participant Auth as CredentialStore / Cookies
    participant IO as download.py / output.py
    participant DB as DownloadArchive (downloads.db)
    participant Hook as ABS & --post-exec

    Caller->>DL: download(urls) inside JobContext
    DL->>Src: find_compatible_source(url) (or resolve_redirect_url)
    alt requires_authentication
        DL->>Auth: Check -c/--cookies, config cookie_file, ./cookies.txt, or cached session_cookies
        Auth-->>Src: Load Netscape jar or authenticate via _login()
        Src->>Auth: save_cookie_file() (0600 permissions)
    end
    DL->>Src: source.download(url) -> Audiobook or Series
    DL->>DB: Skip if --skip-downloaded / ledger contains book_key
    opt --enrich / --enrich-metadata
        DL->>DL: Query Audnexus, iTunes, Google Books, OpenLibrary (with SQLite cache)
    end
    DL->>IO: download_audiobook() (staged temp dir + Range resume + 429/5xx backoff)
    IO->>IO: Decrypt AES-CBC if encrypted + verify_audio_integrity(ffprobe)
    IO->>IO: Combine/convert/split via FFmpeg + embed MP4/ID3/Vorbis tags & sidecars
    IO->>DB: archive.record(..., status="downloaded")
    opt Audiobookshelf auto_upload / auto_scan / --upload-to-abs
        DL->>Hook: upload_book() / scan_library()
    end
    opt --post-exec
        DL->>Hook: Run command with AUDIOBOOK_DL_* env vars
    end
```

### Staging & Integrity Guarantees
- **Multi-part & chapter-split safety**: [`audiobookdl/output/download.py`](../audiobookdl/output/download.py) downloads multi-file books into a sibling temporary `.staging-*` directory and verifies audio integrity with `ffprobe` (`output.verify_audio_integrity()`) before atomically committing targets or updating `downloads.db`.
- **Resumable transfers**: With `--resume`, partial segments stay in `.tmp` files and resume via HTTP `Range` requests (`Content-Range` validated; stale temp files are reset automatically when servers ignore ranges).

---

## 2. Offline Local Repackaging (`audiobook-dl pack`)

[`audiobookdl/pack.py`](../audiobookdl/pack.py) converts local audio folders or track lists into chaptered `.m4b` (or configured `--output-format`) files without contacting a remote source:

```mermaid
flowchart LR
    In["Input Folders / Tracks"] --> Discover["discover_pack_groups()"]
    Discover --> Sort["sort_track_paths() by (disc, track) or natural filename"]
    Sort --> ReadMeta["Extract track tags + sidecars (metadata.json, audiobook.json, *.opf, *.cue, cover.*/folder.*)"]
    ReadMeta --> Merge["Merge CLI Overrides (--title, --author, --series, --cover)"]
    Merge --> Enrich["Optional --enrich Metadata & Cover Lookup"]
    Enrich --> DryRun{"--dry-run?"}
    DryRun -- Yes --> Report["Preview / --json Summary"]
    DryRun -- No --> Render["Combine/Convert via FFmpeg + Apply Loudnorm/Mono + Chapter Offset + Embed Metadata/Sidecars"]
    Render --> Post["Optional --post-exec & --delete-source"]
```

---

## 3. Headless `sync` & `serve` Automation

- **`audiobook-dl sync`** ([`audiobookdl/sync.py`](../audiobookdl/sync.py)):
  1. `_resolve_sync_targets()` selects configured `(service, profile)` pairs whose adapter has `supports_library = True` (or specific `--service` / `--profile` targets).
  2. `run_sync_once()` authenticates and calls `list_library()`, upserts discovered items into `downloads.db` (`upsert_catalog_items()`), filters out `downloaded`, `trashed`, and `hidden`-tagged entries, and downloads remaining `pending` URLs unless `--scan-only` or `--dry-run` is active.
  3. `run_sync_loop()` repeats on `--interval <seconds>` when `--watch` is enabled and handles `SIGINT`/`SIGTERM` gracefully.

- **`audiobook-dl serve`** ([`audiobookdl/web.py`](../audiobookdl/web.py)):
  Runs `WebDashboardState` + `ThreadingHTTPServer` bound to `--host` / `--port`, exposing the dark-mode Web UI at `/` and JSON endpoints (`/api/health`, `/api/status`, `/api/services`, `/api/library`, `/api/download`, `/api/pause`, `/api/cancel`, `/api/queue/clear`, `/api/catalog`, `/api/sync`).

---

## 4. CI, Release & Container Packaging

```mermaid
flowchart TD
    Push["Push / PR (master, main)"] --> CI[".github/workflows/ci.yml (ubuntu-latest & windows-latest, Python 3.11 & 3.13)"]
    CI --> Lint["ruff check audiobookdl tests scripts"]
    CI --> TypeCheck["mypy audiobookdl"]
    CI --> UnitTest["pytest -m 'not e2e' -q"]
    CI --> E2ETest["pytest -m e2e -v (xvfb-run on Linux)"]

    Tag["Git Tag v*"] --> Rel[".github/workflows/release.yml"]
    Rel --> WinJob["Windows Job: pytest + build_exe.py (--onefile & --onedir) + --self-test"]
    Rel --> DockerJob["Docker Job: multi-arch linux/amd64 & linux/arm64 to ghcr.io"]
```

- **Windows Portable Builds** ([`scripts/build_exe.py`](../scripts/build_exe.py)): Packages [`audiobookdl/gui/app.py`](../audiobookdl/gui/app.py) (`audiobook-dl.exe`) with PyInstaller (`--onefile` or `--onedir`, plus `audiobook-dl-cli.exe` console companion in `--onedir` mode), optionally copies or bundles `ffmpeg`/`ffprobe`, and verifies builds via `--self-test`.
- **Docker Image** ([`Dockerfile`](../Dockerfile)): Based on `python:3.12-slim` with `ffmpeg`, non-root user `audiobookdl`, volumes `/config` and `/audiobooks`, and default `CMD ["sync", "--watch", "--interval", "3600"]`.

# System Architecture

`audiobook-dl` is structured around a shared configuration/option schema and an isolated job execution pipeline (`Downloader` + `JobContext`) consumed by the CLI, Desktop GUI, Headless Web UI, and automation subcommands.

## Layered Module Overview

```mermaid
flowchart TD
    subgraph Entrypoints["Entrypoints & Interfaces"]
        CLI["audiobookdl/__main__.py (CLI & Subcommands)"]
        GUI["audiobookdl/gui/app.py (Tkinter Desktop GUI)"]
        WEB["audiobookdl/web.py (ThreadingHTTPServer Web UI)"]
        PACK["audiobookdl/pack.py (Offline Repackager)"]
        SYNC["audiobookdl/sync.py (Bookshelf Auto-Sync)"]
    end

    subgraph Control["Shared State, Config & Options"]
        SCHEMA["audiobookdl/options_schema.py (OPTION_REGISTRY)"]
        ARGS["audiobookdl/args.py (Parsers & DownloadOptions)"]
        CFG["audiobookdl/config.py (Comment-Preserving TOML)"]
        CRED["audiobookdl/credentials.py (DPAPI / Keyring / Fallback)"]
        CTRL["audiobookdl/gui/controller.py (GuiController Queue & Worker)"]
    end

    subgraph Core["Pipeline & Context Isolation"]
        PIPE["audiobookdl/pipeline.py (Downloader & process_audiobook)"]
        LOG["audiobookdl/logging.py (JobContext & LogSink)"]
    end

    subgraph Sources["Source Registry & Networking"]
        SREG["audiobookdl/sources/__init__.py (16 Built-in + Entry Points)"]
        SBASE["audiobookdl/sources/source/__init__.py (Source Base & Cookie Jar)"]
        SNET["audiobookdl/sources/source/networking.py (HTTP & curl_cffi)"]
    end

    subgraph Media["Download, Transcoding & Metadata"]
        DL["audiobookdl/output/download.py (HTTP Range, Pool, Integrity)"]
        OUT["audiobookdl/output/output.py (Templates, Presets, FFmpeg Combine/Split)"]
        ENC["audiobookdl/output/encryption.py (AESStreamDecryptor)"]
        META["audiobookdl/output/metadata/ (MP4, ID3, Vorbis, FFmpeg, Sidecars, Reader)"]
    end

    subgraph Persist["Persistence & Integrations"]
        DB["audiobookdl/utils/archive.py (DownloadArchive SQLite downloads.db)"]
        SCAN["audiobookdl/utils/library_scanner.py (Local Library Scanner)"]
        EXP["audiobookdl/utils/export.py (CSV / JSON / XLSX Export)"]
        ENR["audiobookdl/utils/enrichment.py (Audnexus, iTunes, Google, OpenLibrary)"]
        ABS["audiobookdl/integrations/audiobookshelf.py (AudiobookshelfClient)"]
    end

    CLI --> ARGS
    CLI --> PACK
    CLI --> SYNC
    CLI --> WEB
    GUI --> CTRL
    WEB --> CTRL
    CTRL --> PIPE
    ARGS --> SCHEMA
    CFG --> SCHEMA
    CFG --> CRED
    CLI --> PIPE
    SYNC --> PIPE
    PIPE --> LOG
    PIPE --> SREG
    SREG --> SBASE
    SBASE --> SNET
    PIPE --> DL
    PIPE --> ENR
    PIPE --> DB
    PIPE --> ABS
    PACK --> META
    PACK --> OUT
    DL --> OUT
    DL --> ENC
    DL --> META
    SCAN --> DB
    EXP --> DB
```

## Key Architectural Design Patterns

### 1. Single-Source Option Registry (`OPTION_REGISTRY`)
All runtime flags shared across CLI parsing, TOML loading/saving, pipeline option merging, and Tkinter GUI state bindings are declared once in [`audiobookdl/options_schema.py`](../audiobookdl/options_schema.py) (`OPTION_REGISTRY`):
- `CliFlag` declarations generate `argparse` arguments in [`audiobookdl/args.py`](../audiobookdl/args.py).
- `config_key`, `config_kind`, and `toml_serializer` drive validation and round-trip serialization in [`audiobookdl/config.py`](../audiobookdl/config.py).
- `merge_from_config` rules control how `apply_runtime_options()` in [`audiobookdl/pipeline.py`](../audiobookdl/pipeline.py) merges TOML defaults into `DownloadOptions` without overriding explicit CLI flags (`_explicit_fields`).
- `GuiBinding` maps options declaratively onto Tkinter variables in [`audiobookdl/gui/options_panel.py`](../audiobookdl/gui/options_panel.py) and [`audiobookdl/gui/controller.py`](../audiobookdl/gui/controller.py).

### 2. Per-Job Context Isolation (`JobContext`)
`Downloader` (and `run_downloads`) execute inside a `contextvars`-backed `logging.job_context(ctx)` ([`audiobookdl/logging.py`](../audiobookdl/logging.py)). Each `JobContext` encapsulates:
- `debug`, `quiet`, and `ffmpeg_output` flags
- `ffmpeg_dir` (custom FFmpeg directory override without global mutation)
- `sink` (`_EventLogSink` emitting structured `DownloadEvent`s, `GuiLogSink`, or rich CLI console output)
- `interaction` (`CliInteractionHandler` or GUI prompt synchronization)
- `cancel_event` (`threading.Event`)

This guarantees concurrent or embedded `Downloader` instances do not leak state across runs.

### 3. Shared Controller for Desktop GUI & Headless Web UI
Both the Tkinter desktop app ([`audiobookdl/gui/app.py`](../audiobookdl/gui/app.py)) and the stdlib HTTP server ([`audiobookdl/web.py`](../audiobookdl/web.py)) delegate queue persistence (`gui_queue.json`), pause/resume/cancel orchestration, catalog browsing, and background download workers to `GuiController` ([`audiobookdl/gui/controller.py`](../audiobookdl/gui/controller.py)):
- **Desktop GUI**: Drains `GuiController` event tuples (`log`, `batch_item`, `job_status`, `progress_*`, `prompt_*`, `done`) via Tkinter timers.
- **Headless Web UI**: `WebDashboardState` drains `GuiController` events on a daemon thread, serves a single-page HTML dashboard at `/`, and exposes JSON endpoints behind optional Bearer / `X-API-Token` authentication (`$AUDIOBOOK_DL_WEB_TOKEN`).

### 4. Extensible Source Adapters & Cookie Persistence
Source adapters subclass `Source` ([`audiobookdl/sources/source/__init__.py`](../audiobookdl/sources/source/__init__.py)) and declare:
- `names` (canonical key + aliases) and `match` (URL regex list)
- `_authentication_methods` (`"cookies"`, `"login"`, or `[]`) and `login_data`
- `supports_search` / `search(query)` and `supports_library` / `list_library()`

[`audiobookdl/sources/__init__.py`](../audiobookdl/sources/__init__.py) combines the 16 built-in `_BUILTIN_SOURCES` with external plugins registered under the `audiobookdl.sources` `importlib.metadata` entry point group. For HTTP requests, `networking.py` supports standard `requests.Session` as well as optional TLS/HTTP2 browser fingerprint impersonation via `curl_cffi` (`--impersonate`).

## Configuration, Credentials & State Layout

```mermaid
flowchart LR
    subgraph Resolution["Config Path Resolution Priority"]
        P1["1. --config FILE"] --> CFGFILE["audiobook-dl.toml"]
        P2["2. AUDIOBOOK_DL_CONFIG Env"] --> CFGFILE
        P3["3. Portable .portable / exe dir"] --> CFGFILE
        P4["4. platformdirs user_config_dir"] --> CFGFILE
    end

    subgraph Secrets["Credential Store Selection"]
        WIN["Windows: DpapiCredentialStore"] --> VAULT["credentials.dpapi"]
        NIX["macOS / Linux: KeyringCredentialStore"] --> KEYRING["OS Keyring + credentials.keyring.json"]
        HEADLESS["Headless / No Keyring: PlaintextFallbackCredentialStore"] --> ENVOPT["Opt-in via AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS=1"]
    end

    subgraph Data["Database Directory Layout"]
        DBDIR["database_directory (default: config_dir/db)"] --> LEDGER["downloads.db (DownloadArchive v2)"]
        DBDIR --> ECACHE["enrichment_cache.db (SQLite Cache)"]
        DBDIR --> SCACHE["{service}/session_cookies_{profile}.txt (0600 Netscape Jar)"]
    end

    CFGFILE --> Secrets
    CFGFILE --> Data
```

- **Comment-Preserving Config Updates**: [`audiobookdl/config.py`](../audiobookdl/config.py) updates existing keys in place when saving GUI/output preferences or migrating legacy plaintext passwords, retaining `#` comments and unknown sections.
- **SQLite Library Ledger (`downloads.db`)**: Schema version 2 tracks entries across `downloads` (with `source`, `profile`, `book_key`, `status` in `'downloaded' | 'pending' | 'trashed'`, `output_path`, `isbn`, `asin`, `series`, etc.) normalized alongside `item_tags` (including the reserved `'hidden'` tag).

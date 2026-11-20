# System Architecture

`audiobook-dl` is structured around a shared configuration/option schema and an isolated job execution pipeline (`Downloader` + `JobContext`) consumed by the Tkinter Desktop GUI (`audiobookdl/gui/`).

## Layered Module Overview

```mermaid
flowchart TD
    subgraph Entrypoints["Entrypoints & Interfaces"]
        ENTRY["audiobookdl/__main__.py"]
        GUI["audiobookdl/gui/app.py (Tkinter Desktop GUI)"]
        PACK["audiobookdl/pack.py (Offline Repackager)"]
        SYNC["audiobookdl/sync.py (Bookshelf Auto-Sync)"]
    end

    subgraph Control["Shared State, Config & Options"]
        SCHEMA["audiobookdl/options_schema.py (OPTION_REGISTRY & DownloadOptions)"]
        CFG["audiobookdl/config.py (Comment-Preserving TOML)"]
        CRED["audiobookdl/credentials.py (DPAPI / Keyring / Fallback)"]
        CTRL["audiobookdl/gui/controller.py (GuiController Queue & Worker)"]
    end

    subgraph Core["Pipeline & Context Isolation"]
        PIPE["audiobookdl/pipeline.py (Downloader & process_audiobook)"]
        LOG["audiobookdl/logging.py (JobContext & LogSink)"]
    end

    subgraph Sources["Source Registry & Networking"]
        SREG["audiobookdl/sources/__init__.py (Built-in + Entry Points)"]
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

    ENTRY --> GUI
    GUI --> CTRL
    CTRL --> PIPE
    CTRL --> PACK
    CTRL --> SYNC
    CFG --> SCHEMA
    CFG --> CRED
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
All runtime options shared across TOML loading/saving, pipeline option merging, and Tkinter GUI state bindings are declared once in [`audiobookdl/options_schema.py`](../audiobookdl/options_schema.py) (`OPTION_REGISTRY` and `DownloadOptions`):
- `config_key`, `config_kind`, and `toml_serializer` drive validation and round-trip serialization in [`audiobookdl/config.py`](../audiobookdl/config.py).
- `merge_from_config` rules control how `resolve_runtime_options()` in [`audiobookdl/pipeline.py`](../audiobookdl/pipeline.py) merges TOML defaults into a new `DownloadOptions` (pure; the caller's instance is never mutated) without overriding explicit overrides (`_explicit_fields`). `apply_runtime_options()` builds the per-job `JobContext` from the resolved options.
- `GuiBinding` maps options declaratively onto Tkinter variables in [`audiobookdl/gui/options_panel.py`](../audiobookdl/gui/options_panel.py) and [`audiobookdl/gui/controller.py`](../audiobookdl/gui/controller.py).

### 2. Per-Job Context Isolation (`JobContext`)
`Downloader` (and `run_downloads`) execute inside a `contextvars`-backed `logging.job_context(ctx)` ([`audiobookdl/logging.py`](../audiobookdl/logging.py)). Each `JobContext` encapsulates:
- `debug` and `quiet` flags
- `ffmpeg_dir` (custom FFmpeg directory override without global mutation)
- `sink` (`_EventLogSink` emitting structured `DownloadEvent`s or `GuiLogSink`)
- `interaction` (`GuiInteractionHandler` prompt synchronization or `NonInteractiveInteractionHandler`)
- `cancel_event` (`threading.Event`)
- `pause_event` (`threading.Event`)

This guarantees concurrent or embedded `Downloader` instances do not leak state across runs.

### 3. Desktop GUI & `GuiController`
The Tkinter desktop app ([`audiobookdl/gui/app.py`](../audiobookdl/gui/app.py)) delegates queue persistence (`gui_queue.json`), pause/resume/cancel orchestration, catalog browsing, and background download/pack/sync workers to `GuiController` ([`audiobookdl/gui/controller.py`](../audiobookdl/gui/controller.py)). `GuiController` is a compatibility facade that composes focused headless services:
- [`audiobookdl/gui/event_bus.py`](../audiobookdl/gui/event_bus.py) — bounded `ControllerEventQueue`, `GuiLogSink`, `GuiInteractionHandler` prompt bridge, and event drain/coalescing.
- [`audiobookdl/gui/config_store.py`](../audiobookdl/gui/config_store.py) — cached `Config`, snapshots, and all GUI preference persistence.
- [`audiobookdl/gui/http_pool.py`](../audiobookdl/gui/http_pool.py) — per-thread pooled HTTP sessions and cover fetching.
- [`audiobookdl/gui/queue_store.py`](../audiobookdl/gui/queue_store.py) — `PackJobRequest`/`QueuedJob`, queue records, pending deque, reorder helpers, and `gui_queue.json` load/save.
- [`audiobookdl/gui/task_runner.py`](../audiobookdl/gui/task_runner.py) — bounded background/DB executors, activity callback, and auxiliary interaction tracking.
- [`audiobookdl/gui/job_runner.py`](../audiobookdl/gui/job_runner.py) — download/pack worker loop, concurrency, pause/resume/cancel, and run state.
- [`audiobookdl/gui/service_tasks.py`](../audiobookdl/gui/service_tasks.py) — auxiliary cancellable operations (preview, auth/OAuth/accounts verification, catalog, library sync, Audiobookshelf upload, updates, FFmpeg install, enrichment, pack inspection).
- **GUI state & view model**: [`audiobookdl/gui/options_view_model.py`](../audiobookdl/gui/options_view_model.py) — typed `OptionsViewModel` dataclass holding every option/status Tk variable. `OptionsCoordinator.init_variables()` populates it once on the Tk thread and `AudiobookDLApp.__getattr__` delegates `app.<name>_var` lookups to it, so call sites never rely on dynamic `setattr` stamping.
- **Typed page dependencies**: [`audiobookdl/gui/page_deps.py`](../audiobookdl/gui/page_deps.py) — frozen dependency dataclasses (`DownloadPageDeps`, `LibraryPageDeps`, `SettingsPageDeps`, `QueuePanelDeps`) assembled by [`audiobookdl/gui/page_factories.py`](../audiobookdl/gui/page_factories.py), the composition root that may access `app`/coordinator internals. Pages receive typed deps instead of `app: Any` plus 25-36 loose parameters; `OptionsPanel` gets `deps.vars` (the `OptionsViewModel`) as its variable owner and opens the DRM inspector through an injected callback rather than importing the dialog itself.
- **Event/widget boundary**: worker threads emit only data — `CatalogLoadedEvent` carries `request_id`/`payload`/`error` with no Tk widget; `EventDispatcher` resolves the current `CatalogTab` via the library page on the Tk thread. [`audiobookdl/gui/deferred_activity.py`](../audiobookdl/gui/deferred_activity.py) applies activity events (logs, status/progress vars, queue records, download badge) while `DownloadPage` is not yet created, sharing log classification with `ActivityQueuePanel`.
- **Queue submission**: `GuiJobRunner` submission paths (`start_new_run`, `enqueue_additional_urls`, `enqueue_from_preview`, `start_or_enqueue_jobs`) share `_begin_run_state`/`_extend_run_state`/`_append_queued_job` helpers; `RunCoordinator._register_queued_jobs` is the single queue-panel registration + `gui_queue.json` save call site. Editable per-job overrides flow through `DownloadOptions.with_overrides()` gated by the public `EDITABLE_OVERRIDE_KEYS` in `queue_store.py`.
- **Desktop GUI**: [`audiobookdl/gui/app.py`](../audiobookdl/gui/app.py) hosts a left-sidebar navigation shell ([`audiobookdl/gui/shell.py`](../audiobookdl/gui/shell.py)) with persistent footer status and lazy-built pages under [`audiobookdl/gui/pages/`](../audiobookdl/gui/pages/) (`download_page.py`, `library_page.py`, `pack_page.py`, `accounts_page.py`, `settings_page.py`), standardized modal dialogs (`ModalDialog`), monitor-clamped window geometry/maximized/splitter persistence, and drains typed `GuiController` `ControllerEvent` objects via Tkinter timers.

### 4. Extensible Source Adapters & Cookie Persistence
Source adapters subclass `Source` ([`audiobookdl/sources/source/__init__.py`](../audiobookdl/sources/source/__init__.py)) and declare:
- `names` (canonical key + aliases) and `match` (URL regex list)
- `_authentication_methods` (`"cookies"`, `"login"`, or `[]`) and `login_data`
- `supports_search` / `search(query)` and `supports_library` / `list_library()`

[`audiobookdl/sources/__init__.py`](../audiobookdl/sources/__init__.py) combines the 18 built-in `_BUILTIN_SOURCES` with external plugins registered under the `audiobookdl.sources` `importlib.metadata` entry point group. For HTTP requests, `networking.py` supports standard `requests.Session` as well as optional TLS/HTTP2 browser fingerprint impersonation via `curl_cffi` (`impersonate`).

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
        HEADLESS --> PVAULT["credentials.plaintext.json"]
    end

    subgraph Data["Database Directory Layout"]
        DBDIR["database_directory (default: config_dir/db)"] --> LEDGER["downloads.db (DownloadArchive v2)"]
        DBDIR --> ECACHE["enrichment_cache.db (SQLite Cache)"]
        DBDIR --> SCACHE["{service}/session_cookies_{profile}.txt (0600 Netscape Jar)"]
    end

    CFGFILE --> Secrets
    CFGFILE --> Data
```

- **Comment-Preserving Config Updates**: [`audiobookdl/config.py`](../audiobookdl/config.py) updates existing keys in place when saving GUI/output preferences, `[audiobookshelf]`, `[enrichment.custom.<name>]` custom metadata providers (with vaulted `api_key` secrets), or migrating legacy plaintext passwords, retaining `#` comments and unknown sections.
- **SQLite Library Ledger (`downloads.db`)**: Schema version 2 tracks entries across `downloads` (with `source`, `profile`, `book_key`, `status` in `'downloaded' | 'pending' | 'trashed'`, `output_path`, `isbn`, `asin`, `series`, etc.) normalized alongside `item_tags` (including the reserved `'hidden'` tag).

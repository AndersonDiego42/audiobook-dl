# `audiobook-dl` Technical Audit, Shortcoming Analysis & Prioritized Engineering Roadmap

**Repository:** `audiobook-dl` (`v0.7.3`)  
**Audit Date:** 2026-11-18  
**Scope:** Full-stack review of core pipeline, configuration/credentials, HTTP/DRM/decryption, FFmpeg media processing, metadata & sidecars, SQLite persistence & search, 18 built-in source adapters, Audiobookshelf integration, Tkinter desktop GUI, packaging/build scripts, CI/CD, tests, and documentation.

---

## 1. Observed Baseline & Repository Health

Verification commands executed directly against `.venv\Scripts\python.exe` in the repository root:

| Check | Command | Observed Outcome |
|---|---|---|
| Unit & Integration Tests | `.venv\Scripts\python.exe -m pytest -m "not e2e" -q` | **FAILED (`exit 1`)** — `1 failed, 620 passed, 6 deselected` in `122.33s`. Failure: `tests/sources/test_source.py::test_download_file_invokes_lazy_encryption_resolver` (`ImportError: cannot import name 'AESEncryption' from 'audiobookdl.utils.audiobook'`). |
| Linter | `.venv\Scripts\python.exe -m ruff check audiobookdl tests scripts` | **FAILED (`exit 1`)** — `1 error`: `F401` unused import `audiobookdl.Chapter` at `tests/test_download.py:1907`. |
| Static Type Check | `.venv\Scripts\python.exe -m mypy audiobookdl` | **PASSED (`exit 0`)** — `Success: no issues found in 83 source files`. (`tests/` and `scripts/` are excluded from `mypy`.) |

### Module Scale & Concentration

Largest source files by observed line count:

- `audiobookdl/gui/pages/library_page.py` — 2,772 lines
- `audiobookdl/gui/app.py` — 2,752 lines
- `audiobookdl/gui/controller.py` — 2,235 lines
- `audiobookdl/output/output.py` — 2,155 lines
- `audiobookdl/gui/queue_panel.py` — 1,860 lines
- `audiobookdl/utils/enrichment.py` — 1,648 lines
- `audiobookdl/output/download.py` — 1,613 lines
- `audiobookdl/config.py` — 1,565 lines
- `audiobookdl/options_schema.py` — 1,411 lines
- `audiobookdl/utils/archive.py` — 1,303 lines
- `audiobookdl/pipeline.py` — 1,296 lines
- `audiobookdl/gui/widgets.py` — 1,094 lines

---

## 2. Prioritized Summary Matrix

| ID | Priority | Categories | Title |
|---|---|---|---|
| **P0-1** | P0 (Blocker) | `Fixes`, `Testing`, `Tooling` | Fix broken test import (`AESEncryption`) & `ruff` CI failure (`F401`) |
| **P0-2** | P0 (Blocker) | `Fixes`, `Data Safety`, `Output` | Prevent destructive `output_dir` deletion when `resume=True` + `combine=True` |
| **P0-3** | P0 (Blocker) | `Fixes`, `API`, `Sync` | Fix `SyncOptions` passing `output_template=None` into `_explicit_fields` and overriding `config.output_template` |
| **P0-4** | P0 (Critical) | `Fixes`, `Security`, `Architecture` | Fix `PlaintextFallbackCredentialStore` asymmetry and remove synchronous `icacls` subprocess overhead |
| **P1-1** | P1 (High) | `Performance`, `Database` | Eliminate per-call SQLite connection & `PRAGMA journal_mode=WAL` / schema-init churn in `DownloadArchive` and `EnrichmentCache` |
| **P1-2** | P1 (High) | `Fixes`, `Networking`, `Output` | Fix strict `Content-Type` header comparison, HLS playlist UTF-8/language handling, and `token_bucket` pacing |
| **P1-3** | P1 (High) | `Performance`, `Output`, `Media` | Eliminate double disk write in `combine_audiofiles` (`combined.ts`) and tag staged files atomically before `os.replace` |
| **P1-4** | P1 (High) | `Security`, `API`, `Config` | Replace hand-rolled single-line TOML merger (`config.py`) and harden `post_exec` / CENC key handling |
| **P1-5** | P1 (High) | `Features`, `API`, `DX` | Restore a first-class headless CLI (`download`, `sync`, `pack`, `search`, `accounts`) atop `pipeline.py`/`sync.py`/`pack.py` |
| **P1-6** | P1 (High) | `Architecture`, `Config`, `DX` | Derive `DownloadOptions` and `Config` fields directly from `OPTION_REGISTRY` (eliminate 4-way manual sync) |
| **P1-7** | P1 (High) | `Performance`, `UI` | Replace constant `50ms`/`250ms` & `25ms` GUI polling loops with event-driven wakeup and bounded worker pools |
| **P2-1** | P2 (Medium) | `Architecture`, `UI` | Decompose monolithic GUI/pipeline files (`app.py`, `library_page.py`, `controller.py`, `output.py`) |
| **P2-2** | P2 (Medium) | `Performance`, `Database` | Add SQLite FTS5 full-text index to `downloads.db` and push domain filtering into browser cookie SQLite queries |
| **P2-3** | P2 (Medium) | `UI`, `Accessibility`, `UX` | Add horizontal scrollbars to `ScrolledTreeview`, live OS theme tracking, keyboard focus/ARIA equivalents, and queue concurrency controls |
| **P2-4** | P2 (Medium) | `Architecture`, `API`, `Sources` | Extract shared source helpers (device-ID, auth-refresh retry on 401/403, capability/plugin validation) |
| **P2-5** | P2 (Medium) | `Features`, `Integrations`, `Output` | Add Audiobookshelf upload progress + retry, custom silence-detection tuning, and `audiobookdl.enrichment` plugin entry points |
| **P2-6** | P2 (Medium) | `Testing`, `Tooling`, `CI/CD` | Expand CI matrix (macOS, Python 3.10/3.12), add coverage gates, type-check `tests/` & `scripts/`, and harden `ruff` rules |
| **P2-7** | P2 (Medium) | `Cleanup`, `Dependencies`, `DX` | Remove dead code (`gen_output_filename`), drop unused `rich` runtime dependency, and clean stale `.lock` files |
| **P3-1** | P3 (Low) | `Documentation` | Document full `OPTION_REGISTRY` TOML reference, plugin SDK guide, `CONTRIBUTING.md`, and `CHANGELOG.md` |
| **P3-2** | P3 (Low) | `Features`, `UI` | Add embedded audio/chapter preview playback, system tray background sync mode, and cross-platform release artifacts |

---

## 3. Detailed Shortcomings & Actionable Implementation Steps

---

### P0 — Critical Blockers, Data Safety & Broken Gates

#### [P0-1] Fix Broken Unit Test Import (`AESEncryption`) & Ruff Lint Error (`F401`)
- **Priority:** P0 | **Categories:** `Fixes`, `Testing`, `Tooling`
- **Affected Files:**
  - `tests/sources/test_source.py` (lines 431, 450)
  - `tests/test_download.py` (line 1907)
- **Current Shortcoming:**
  - `audiobookdl/utils/audiobook.py` refactored symmetric encryption dataclasses into `AESCBCEncryption`, `AESCTREncryption`, `AESGCMEncryption`, `ChaCha20Encryption`, `SampleAESEncryption`, and `CENCDecryption`, removing `AESEncryption`. `tests/sources/test_source.py::test_download_file_invokes_lazy_encryption_resolver` still imports and instantiates `AESEncryption`, causing the unit test suite (`pytest -m "not e2e"`) to fail immediately.
  - `tests/test_download.py:1907` contains an unused `from audiobookdl import Chapter`, causing `ruff check audiobookdl tests scripts` (and the CI `lint-typecheck-test` job) to fail with exit code 1.
- **Actionable Implementation Steps:**
  1. In `tests/sources/test_source.py:431` and `:450`, replace `AESEncryption` with `AESCBCEncryption`.
  2. Remove the unused `from audiobookdl import Chapter` import at `tests/test_download.py:1907`.
  3. Add a backward-compatibility check or grep across `tests/` and `docs/` for any other stale references to `AESEncryption`.
  4. Verify both `ruff check audiobookdl tests scripts` and `pytest -m "not e2e" -q` exit `0`.

---

#### [P0-2] Prevent Destructive `output_dir` Deletion When `resume=True` and `combine=True`
- **Priority:** P0 | **Categories:** `Fixes`, `Data Safety`, `Output`
- **Affected Files:**
  - `audiobookdl/output/download.py` (lines 207–229, 459–460, 476–478, 953–955)
- **Current Shortcoming:**
  - In `download_audiobook`, `use_staging_dir = (not options.resume) and (len(audiobook.files) > 1 or expected_split_output)`. When `resume=True`, `use_staging_dir` is `False` and `work_output_dir = output_dir`.
  - After combining multi-part audio (`options.combine=True`), line 459 executes:
    ```python
    if not use_staging_dir and os.path.isdir(work_output_dir):
        shutil.rmtree(work_output_dir, ignore_errors=True)
    ```
  - Similarly, on `DownloadCancelled` (line 477) or in `download_files_with_cli_output` (line 953, guarded by `if resume: ... else: shutil.rmtree(output_dir)`), if `output_dir` was an existing user directory (for example a shared author/book folder or flat template directory containing other files or cover/sidecar artifacts), `shutil.rmtree(work_output_dir)` deletes the **entire directory** rather than only the downloaded part files (`filepaths`) created for this job.
- **Actionable Implementation Steps:**
  1. Decouple *resume part caching* from *un-staged destination mutation*: even when `resume=True`, stage transient part files in a deterministic hidden sibling directory (e.g., `f".{stage_base}.resume"` next to `output_dir`, keyed or validated by track count/URLs) or track the exact list of downloaded `filepaths` (`Part 00.ts`, etc.).
  2. Replace `shutil.rmtree(work_output_dir)` at `download.py:459-460` and `:477-478` with targeted cleanup that removes only tracked downloaded part files (`filepaths`) and prunes `work_output_dir` via `os.rmdir` **only if empty**.
  3. Add regression tests in `tests/test_download.py` verifying that pre-existing sibling files inside `output_dir` survive `resume=True` + `combine=True` and `resume=True` + `DownloadCancelled`.

---

#### [P0-3] Fix `SyncOptions` Overriding `Config.output_template` and `Config.output_format` with `None`
- **Priority:** P0 | **Categories:** `Fixes`, `API`, `Sync`
- **Affected Files:**
  - `audiobookdl/sync.py` (lines 141–156)
  - `audiobookdl/options_schema.py` (lines 1391–1395)
  - `audiobookdl/pipeline.py` (lines 225–250)
- **Current Shortcoming:**
  - `_build_sync_download_options()` calls:
    ```python
    base_opts = build_default_options(
        config_location=sync_opts.config_location,
        download_archive=archive_path,
        skip_downloaded=True,
        profile=None if profile == DEFAULT_PROFILE else profile,
        output_template=sync_opts.output_template,
        output_format=sync_opts.output_format,
    )
    ```
  - `build_default_options()` sets `opts._explicit_fields = frozenset(overrides.keys())`. When `SyncOptions()` is used with default `output_template=None` and `output_format=None` (e.g., from headless/API callers), `"output_template"` and `"output_format"` are both added to `_explicit_fields` with value `None`.
  - `apply_runtime_options(base_opts, config)` skips any field in `_explicit_fields` (`pipeline.py:235`), so `config.output_template` and `config.output_format` are never merged, and `base_opts.output_template` remains `None`—crashing `output.gen_output_location(options.output_template, ...)` with `AttributeError`/`TypeError`. (The GUI masks this only because `app.py:2033` passes explicit non-`None` strings.)
- **Actionable Implementation Steps:**
  1. In `audiobookdl/sync.py:_build_sync_download_options`, pass `output_template` and `output_format` into `build_default_options(...)` **only when not `None`** (filter out `None` optional overrides before calling `build_default_options`).
  2. Harden `apply_runtime_options()` / `gen_output_location()` so `output_template=None` falls back to `"{title}"` instead of crashing.
  3. Add unit tests in `tests/test_sync.py` invoking `run_sync_once(SyncOptions())` with `config.output_template` and `config.output_format` set in TOML and asserting both are respected.

---

#### [P0-4] Fix `PlaintextFallbackCredentialStore` Asymmetry & Synchronous `icacls` Bottleneck
- **Priority:** P0 | **Categories:** `Fixes`, `Security`, `Performance`
- **Affected Files:**
  - `audiobookdl/credentials.py` (lines 337–365)
  - `audiobookdl/utils/fs.py` (lines 37–76, 129–150)
  - `audiobookdl/config.py` (lines 1225–1235, 1452–1466)
- **Current Shortcoming:**
  - `PlaintextFallbackCredentialStore.get_password()` unconditionally returns `None` and `delete_password()` is a no-op; plaintext persistence actually happens in `config.py` via `isinstance(store, PlaintextFallbackCredentialStore)` checks. Callers using `CredentialStore` directly see broken `set_password`/`get_password`/`delete_password` contracts.
  - On Windows, `atomic_write_text(..., private=True)` in `audiobookdl/utils/fs.py:129-149` spawns `subprocess.run(["icacls", ...])` synchronously on every single secret, cookie jar, or device-ID write (~30–100 ms per call), and `file_lock()` creates `<target>.lock` files that are never unlinked.
  - `DpapiCredentialStore._write_entries` (`credentials.py:175-177`) deletes `credentials.dpapi` when empty, while an external reader might concurrently inspect or lock it.
- **Actionable Implementation Steps:**
  1. Either have `PlaintextFallbackCredentialStore` read/write a `0600` JSON vault file when `allow_plaintext=True` (and explicitly refuse persistence when `False`) or consolidate TOML fallback cleanly so `CredentialStore` satisfies its own ABC contract.
  2. Replace `subprocess.run(["icacls", ...])` in `audiobookdl/utils/fs.py` with direct Win32 `advapi32.dll` (`SetNamedSecurityInfoW` / `ConvertStringSecurityDescriptorToSecurityDescriptorW`) via `ctypes`, falling back to `icacls` only if Win32 API calls fail.
  3. Clean up `<target>.lock` best-effort on POSIX or document lock lifecycle, and avoid unlinking `credentials.dpapi` (write `{"version": 1, "entries": {}}` atomically instead of `os.remove`).

---

### P1 — High-Impact Performance, Reliability, Security & Core Features

#### [P1-1] Eliminate SQLite Connection & Schema-Init Churn in `DownloadArchive` and `EnrichmentCache`
- **Priority:** P1 | **Categories:** `Performance`, `Database`
- **Affected Files:**
  - `audiobookdl/utils/archive.py` (lines 279–340, 289–294)
  - `audiobookdl/utils/enrichment_cache.py` (lines 42–80)
  - `audiobookdl/pipeline.py` (lines 1193, 1225)
  - `audiobookdl/sync.py` (lines 197–234)
- **Current Shortcoming:**
  - In default `persistent=False` mode, **every single query** (`contains`, `get_entry_by_key`, `record`, `EnrichmentCache.get`, `set`) opens a fresh `sqlite3.connect(...)`, executes `PRAGMA journal_mode=WAL` (which acquires a write lock on the DB header even on read-only queries!), executes `PRAGMA foreign_keys=ON`, and closes the connection.
  - `process_audiobook()` (`pipeline.py:1193`, `1225`) instantiates `DownloadArchive(ledger_path)` twice per book, re-running `_init_schema()` (`CREATE TABLE IF NOT EXISTS`, `PRAGMA table_info`, 9 `CREATE INDEX IF NOT EXISTS`, `PRAGMA user_version = 2`) twice per book.
  - `discover_pending_sync_books()` (`sync.py:197-234`) instantiates `DownloadArchive(archive_path)` with `persistent=False` and calls `archive.get_entry_by_key(...)` inside the per-book loop—causing `O(N)` full SQLite open/WAL-pragma/close cycles for an `N`-book library.
- **Actionable Implementation Steps:**
  1. Run `PRAGMA journal_mode=WAL` **once** inside `_init_schema()` (since WAL mode is persistent at the database file level), and set `PRAGMA synchronous=NORMAL` and `PRAGMA busy_timeout=30000` on new connections.
  2. Cache schema initialization per `(os.path.abspath(db_path), mtime/inode)` or once per `DownloadArchive`/`EnrichmentCache` instance, and in `pipeline.py:process_audiobook` reuse the single `archive` instance across `contains()` and `record()` inside `with DownloadArchive(ledger_path, persistent=True) as archive:`.
  3. Wrap `discover_pending_sync_books()` (`sync.py:197`) in `with DownloadArchive(archive_path, persistent=True) as archive:` and replace the per-book `get_entry_by_key` loop with a single batch query (`SELECT book_key, status, ... WHERE source = ? AND book_key IN (...)` or preloaded lookup map).
  4. Expose a shared `EnrichmentCache` instance on `Downloader` / `JobContext` rather than opening/closing `enrichment_cache.db` per HTTP call.

---

#### [P1-2] Fix HTTP `Content-Type` Matching, HLS Language/Encoding Handling & Bandwidth Rate Limiting
- **Priority:** P1 | **Categories:** `Fixes`, `Networking`, `Output`
- **Affected Files:**
  - `audiobookdl/output/download.py` (lines 1173–1180, 1316–1320)
  - `audiobookdl/sources/source/networking.py` (lines 198–229, 301–340)
  - `audiobookdl/utils/http.py` (lines 44–91)
- **Current Shortcoming:**
  - `download.py:1179` checks `expected != content_type` using exact string equality. Servers returning `"audio/mpeg; charset=utf-8"` or `"application/octet-stream; name=..."` fail validation against `expected="audio/mpeg"`.
  - `networking.py:301, 311` calls `.decode("utf-8")` strictly without `errors="replace"` or `utf-8-sig` BOM stripping, causing `UnicodeDecodeError` on BOM-prefixed or non-UTF-8 `.m3u8` playlists.
  - `networking.py:334-338` fetches multiple HLS keys via `run_cancellable_pool(...)` without passing `cancel_event` from `logging.get_job_context()`.
  - `_select_variant_playlist_uri()` (`networking.py:198-229`) ignores `options.locale` / audio language and bandwidth among `#EXT-X-MEDIA:TYPE=AUDIO` renditions.
  - `download_file` (`download.py:1318`) calls `rate_limiter.acquire(chunk_len)` *after* reading each chunk instead of pre-throttling, and `networking.py` requests bypass `limit_rate`.
- **Actionable Implementation Steps:**
  1. Strip media-type parameters before comparison in `download.py`: `actual_ct = content_type.split(";", 1)[0].strip().lower()` and compare case-insensitively.
  2. Decode `.m3u8` payloads with `decode("utf-8-sig", errors="replace")` and pass `cancel_event=ctx.cancel_event` to `run_cancellable_pool` in `networking.py:334`.
  3. Enhance `_select_variant_playlist_uri` to score `AUDIO` media renditions by matching `options.locale` (language prefix), `DEFAULT=YES`, `AUTOSELECT=YES`, and bandwidth.
  4. Call `rate_limiter.acquire(...)` cleanly and add a `download_chunks` helper so chunked reads stay smoothly paced.

---

#### [P1-3] Eliminate Double Temp Disk Copy in `combine_audiofiles` & Tag Staged Files Atomically
- **Priority:** P1 | **Categories:** `Performance`, `Output`, `Media`
- **Affected Files:**
  - `audiobookdl/output/output.py` (lines 885–903, 987–994, 1072–1079)
  - `audiobookdl/output/download.py` (lines 456–463, 558–579)
  - `audiobookdl/pack.py` (lines 768–778)
- **Current Shortcoming:**
  - In `combine_audiofiles()` (`output.py:888-902`), whenever `len(ts_paths) > 64` or the `concat:` URI exceeds 16,000 characters (common for long audiobooks with 100+ chapters or HLS segments), it concatenates all `.ts` files into a giant `combined.ts` on disk before FFmpeg reads `combined.ts` to write `output_path`—tripling peak disk footprint (`raw parts` + `ts_parts` + `combined.ts` + `output_path`).
  - Both `download.py` and `pack.py:768-778` execute `os.replace(staged_path, output_path)` **before** `add_metadata_to_file(...)` runs Mutagen/FFmpeg tagging. If Mutagen raises (e.g., disk full, corrupt atom, cancelled mid-write), the final `output_path` is already committed in an untagged/corrupted state.
  - `detect_silence_chapters()` (`output.py:585-621`) hardcodes `noise_db=-35.0` and `min_silence_sec=1.8` with no config/GUI exposure and no `-progress pipe:1` feedback.
- **Actionable Implementation Steps:**
  1. Replace the `combined.ts` binary copy fallback (`output.py:895-902`) with FFmpeg's `concat` demuxer (`_write_concat_list_file(concat_list_path, ts_paths)` + `-f concat -safe 0 -i concat_list.txt`), which works directly on `.ts` files in `O(1)` extra disk space and avoids PTS-reset duration misreporting.
  2. Run `add_metadata_to_file` (or Mutagen tag/cover writing) against `staged_path` (before `os.replace(staged_path, final_output_path)`) in both `download.py` and `pack.py` so final paths are strictly atomic and fully tagged.
  3. Expose `silence_noise_db` and `silence_min_seconds` options in `OPTION_REGISTRY` and pass `-progress pipe:1` to `detect_silence_chapters`.

---

#### [P1-4] Replace Hand-Rolled TOML Merger and Harden `post_exec` & CENC Key Handling
- **Priority:** P1 | **Categories:** `Security`, `Architecture`, `Config`
- **Affected Files:**
  - `audiobookdl/config.py` (lines 909–1173)
  - `audiobookdl/pipeline.py` (lines 94–163)
  - `audiobookdl/output/encryption.py` (lines 573–642)
  - `audiobookdl/integrations/audiobookshelf.py` (lines 170–230)
  - `audiobookdl/utils/enrichment.py` (lines 119–128)
- **Current Shortcoming:**
  - `config.py:_merge_toml_preserving_comments` and `_strip_toml_assignment_key` parse TOML with line-by-line regexes. Multiline strings (`"""..."""`), multi-line arrays (e.g., long `auto_sync_targets`), inline tables (`{ a = 1 }`), and `[[array_of_tables]]` are not parsed as TOML AST nodes and can be silently corrupted or dropped when saving GUI/account settings.
  - `run_post_exec_hook` (`pipeline.py:123-132`) invokes `subprocess.run(cmd, shell=True)` unconditionally. While `_reload_config` in the GUI prompts once for confirmation, initial startup (`load_config`), portable mode configs beside `.exe`, and programmatic `Downloader` runs execute arbitrary `post_exec` shell commands without a trust boundary or argv-list support.
  - `decrypt_file_cenc` (`encryption.py:582, 591`) passes `-decryption_key` / `-decryption_keys` on the CLI args of `ffmpeg`, exposing keys in OS process tables.
  - `AudiobookshelfClient` and custom enrichment providers allow plain `http://` Bearer token transmission without explicit TLS/non-localhost warnings.
- **Actionable Implementation Steps:**
  1. Adopt `tomlkit` as a core dependency in `pyproject.toml` to replace the 200-line custom regex TOML merger (`_merge_toml_preserving_comments`, `_strip_toml_assignment_key`, `_split_toml_section_header`), preserving comments, formatting, multiline strings/arrays, and inline tables losslessly.
  2. Support `post_exec` as either a `list[str]` (executed with `shell=False`) or a string, and add a `trusted_post_exec` hash/opt-in check (or disable `post_exec` from portable/untrusted configs unless confirmed).
  3. Surface clear UI/log warnings when sending `Audiobookshelf` or custom metadata provider API keys over non-loopback `http://` URLs.

---

#### [P1-5] Restore a First-Class Headless CLI Subcommand Suite
- **Priority:** P1 | **Categories:** `Features`, `API`, `DX`
- **Affected Files:**
  - `pyproject.toml` (lines 58–60)
  - `audiobookdl/__main__.py`
  - `audiobookdl/gui/app.py` (lines 2678–2752)
  - `audiobookdl/options_schema.py`
- **Current Shortcoming:**
  - Despite `pipeline.py` (`Downloader`), `sync.py` (`run_sync_once`), `pack.py` (`run_pack`), `utils/library_scanner.py`, `utils/export.py`, and `OPTION_REGISTRY` (which already carries `help` text for every option!) being completely decoupled from Tkinter, `audiobookdl/__main__.py` and `[project.gui-scripts]` in `pyproject.toml` **only launch the Tkinter GUI**.
  - Users running on headless Linux servers, NAS/Docker containers (alongside Audiobookshelf), CI pipelines, or shell automation scripts cannot invoke `audiobook-dl download <url>`, `audiobook-dl sync`, `audiobook-dl pack`, `audiobook-dl scan`, or `audiobook-dl export` without writing custom Python scripts.
- **Actionable Implementation Steps:**
  1. Create `audiobookdl/cli.py` generating `argparse` flags dynamically from `DOWNLOAD_OPTION_SPECS` in `options_schema.py` (reusing `spec.name`, `spec.help`, `spec.config_kind`) with subcommands:
     - `download <urls...>` (with `--json` summary output, `--series-items`, `--select-interactive`)
     - `sync` (`--service`, `--profile`, `--scan-only`, `--dry-run`, `--watch`, `--interval`)
     - `pack <paths...>` (`--recursive`, `--delete-source`, `--dry-run`, `--output-dir`, `--title`, etc.)
     - `library list|scan|export` (`--format csv|json|xlsx`)
     - `accounts list|verify|import-cookies`
     - `gui` (default when invoked with no subcommand or via `audiobook-dl-gui`)
  2. Implement a `CliLogSink` and `CliInteractionHandler` (`input()` / `getpass.getpass()`) consuming `JobContext` in `audiobookdl/logging.py`.
  3. Register `[project.scripts]` (`audiobook-dl = "audiobookdl.cli:main"`) alongside `[project.gui-scripts]` (`audiobook-dl-gui = "audiobookdl.gui:main"`).

---

#### [P1-6] Derive `DownloadOptions` and `Config` Fields from `OPTION_REGISTRY`
- **Priority:** P1 | **Categories:** `Architecture`, `Config`, `DX`
- **Affected Files:**
  - `audiobookdl/options_schema.py` (lines 549–1396)
  - `audiobookdl/config.py` (lines 85–193)
  - `audiobookdl/gui/app.py` (lines 178–215)
- **Current Shortcoming:**
  - `options_schema.py` states `OPTION_REGISTRY` is the "Single source of truth", yet every runtime option is manually duplicated in:
    1. `OPTION_REGISTRY` (`options_schema.py:549-1301`)
    2. `DownloadOptions` dataclass (`options_schema.py:1326-1388`)
    3. `Config` dataclass (`config.py:85-131`)
    4. `AudiobookDLApp` variable annotations (`app.py:178-214`)
  - Adding or renaming an option requires editing 4 places manually and risks silent drift between defaults in `OPTION_REGISTRY` vs `DownloadOptions`.
- **Actionable Implementation Steps:**
  1. Add a `tests/test_options_schema.py` invariant (or generate/validate fields via `dataclasses.make_dataclass` / `__init_subclass__` / test assertion) ensuring `DownloadOptions` fields, types, and defaults match `DOWNLOAD_OPTION_SPECS` 1:1 and `Config` fields match `CONFIG_OPTION_SPECS` 1:1.
  2. Replace manual `GuiConfig`/`Config` field-by-field `clone()` boilerplate (`config.py:133-192`) with `copy.deepcopy` or `attrs.evolve`, eliminating future omission bugs when new fields are added.
  3. Group GUI `tk.Variable`s into a typed dict/holder (`self.option_vars: dict[str, tk.Variable]`) rather than 35+ loose attributes on `AudiobookDLApp`.

---

#### [P1-7] Replace Continuous `after(50)`/`after(250)` & `25ms` Polling Loops with Event-Driven Wakeups
- **Priority:** P1 | **Categories:** `Performance`, `UI`
- **Affected Files:**
  - `audiobookdl/gui/app.py` (lines 2363–2412)
  - `audiobookdl/gui/widgets.py` (lines 699–786, `MainThreadCallbackQueue`)
  - `audiobookdl/gui/controller.py` (lines 399–400, 1788–2069)
- **Current Shortcoming:**
  - `AudiobookDLApp._poll_queue` runs indefinitely on a `50ms` (active) / `250ms` (idle) `root.after` timer even when zero background tasks exist.
  - `MainThreadCallbackQueue` (`widgets.py:702`) spins an independent `25ms` timer (`40 Hz`) per instance (`ArchiveTab`, `PackPage`, `SettingsPage`) whenever `_active_workers > 0`, and `ArchiveTab._on_entry_selected` (`library_page.py:580`) spawns an unthrottled raw `threading.Thread` on every row click without debouncing or cancellation of OS `os.walk` size calculation.
  - `GuiController` spawns unmanaged daemon `threading.Thread` objects across `start_preview`, `start_auth_verification`, `start_catalog_fetch`, `start_library_sync`, `start_abs_upload`, `start_update_check`, and `start_winget_ffmpeg_install`.
- **Actionable Implementation Steps:**
  1. Have `GuiController` / `MainThreadCallbackQueue` emit a coalesced virtual Tk event (`root.event_generate("<<AbdlWake>>", when="tail")` guarded by an atomic `wake_pending` flag) when items are pushed to `event_queue`, keeping `after()` only as a periodic metrics refresh timer *while* `self._is_running` is True.
  2. Replace ad-hoc `threading.Thread(...)` calls in `GuiController` and `ArchiveTab` with a shared, bounded `ThreadPoolExecutor` (`max_workers=4`, shut down cleanly in `GuiController.shutdown()`), and debounce `ArchiveTab._on_entry_selected` by `75ms` with token cancellation inside `_format_size`'s `os.walk` loop.

---

### P2 — Medium-Priority Architecture, Performance, UI, Testing & Tooling Improvements

#### [P2-1] Decompose Monolithic GUI & Output Modules into Focused Subpackages
- **Priority:** P2 | **Categories:** `Architecture`, `Maintainability`, `DX`
- **Affected Files:**
  - `audiobookdl/gui/pages/library_page.py` (2,772 lines)
  - `audiobookdl/gui/app.py` (2,752 lines)
  - `audiobookdl/gui/controller.py` (2,235 lines)
  - `audiobookdl/output/output.py` (2,155 lines)
  - `audiobookdl/gui/queue_panel.py` (1,860 lines)
  - `audiobookdl/utils/enrichment.py` (1,648 lines)
- **Current Shortcoming:**
  - Six files exceed 1,600 lines each (`library_page.py` + `app.py` + `controller.py` alone account for ~7,759 lines). `library_page.py` bundles `ArchiveTab`, `CatalogTab`, `SyncTab`, `_ArchiveMetadataDialog`, and cover-art thumbnail caching in one file; `output.py` mixes path templating/Windows MAX_PATH truncation, FFmpeg muxing/transcoding, silence chapter detection, and chapter splitting.
- **Actionable Implementation Steps:**
  1. Split `audiobookdl/gui/pages/library_page.py` into `audiobookdl/gui/pages/library/` (`__init__.py`, `archive_tab.py`, `catalog_tab.py`, `sync_tab.py`, `dialogs.py`).
  2. Split `audiobookdl/gui/controller.py` into `queue_store.py` (persistence + `PackJobRequest`), `worker.py` (`_worker_loop`), and `controller.py` (facade).
  3. Split `audiobookdl/output/output.py` into `output/templates.py` (path sanitization & conditional templates), `output/ffmpeg_ops.py` (probe/combine/convert/split/silence), and keep `output/__init__.py` or `output/output.py` re-exporting public entry points cleanly after updating all call sites.
  4. Split `audiobookdl/utils/enrichment.py` into `enrichment/providers.py`, `enrichment/matching.py`, `enrichment/models.py`.

---

#### [P2-2] Add SQLite FTS5 Search to `downloads.db` & SQL-Side Cookie Filtering
- **Priority:** P2 | **Categories:** `Performance`, `Database`
- **Affected Files:**
  - `audiobookdl/utils/archive.py` (lines 845–1026, 1185–1200)
  - `audiobookdl/utils/browser_cookies.py` (lines 364–397, 521–597)
- **Current Shortcoming:**
  - `DownloadArchive._build_filter_plan()` (`archive.py:889-925`) uses `LIKE '%term%'` across 10 columns (which cannot use B-tree indexes) and falls back to full-table Python scans whenever any search term is negated (`-tag:hidden`) or contains non-ASCII characters (very common for Nordic/German/non-English titles on Storytel/Nextory/BookBeat/eReolen!).
  - `find_matching_entry_id()` (`archive.py:1192-1199`) falls back to `SELECT id, output_path FROM downloads WHERE output_path IS NOT NULL` (full column scan) if exact match fails.
  - `read_firefox_cookies()` and `read_chromium_cookies()` (`browser_cookies.py:373, 544`) execute `SELECT ... FROM moz_cookies` / `cookies` across the **entire browser cookie database** (often tens of thousands of rows) and only filter `cookie_matches_domains()` afterward in Python.
- **Actionable Implementation Steps:**
  1. Bump `DownloadArchive` schema version to `3`: add an `FTS5` external-content virtual table (`downloads_fts` with `unicode61` tokenizer) kept in sync via triggers (`AFTER INSERT/UPDATE/DELETE`), and store a normalized `output_path_norm` column with an index for O(1) case-folded path matching.
  2. In `browser_cookies.py`, build parameterized `WHERE host LIKE ? OR host LIKE ?` (or `host_key`) SQL clauses from `target_domains` before fetching and decrypting rows, avoiding both large row transfers and unnecessary AES-GCM/DPAPI decryption on unrelated domains.

---

#### [P2-3] Improve Tkinter UI/UX, Accessibility, Live Theme Sync & Multi-Job Queue Concurrency
- **Priority:** P2 | **Categories:** `UI`, `UX`, `Features`
- **Affected Files:**
  - `audiobookdl/gui/widgets.py` (lines 195–365)
  - `audiobookdl/gui/theme.py` (lines 106–157, 591–611)
  - `audiobookdl/gui/controller.py` (lines 1472–1708)
  - `audiobookdl/gui/queue_panel.py`
- **Current Shortcoming:**
  - `ScrolledTreeview` (`widgets.py:195-266`) only creates a vertical `ttk.Scrollbar`; horizontal overflow in wide tables (`ArchiveTab`, `ActivityQueuePanel`, `PackPage`) clips columns on `760px`-wide windows.
  - `detect_system_theme()` (`theme.py:106`) is only queried when `set_theme("system")` or app startup runs; OS dark/light switches while the app is open are ignored.
  - `GuiController._worker_loop()` executes queued jobs strictly one-by-one serially; downloading 50 single-file audiobooks across different services cannot overlap metadata prep, downloads, and FFmpeg post-processing.
  - `CatalogTab` / `ArchiveTab` cover thumbnails are fetched/decoded on-demand without a shared LRU disk/memory cache across refreshes, and `QueuePanel` lacks a concurrent-jobs setting (`1..4`).
- **Actionable Implementation Steps:**
  1. Add an auto-hiding horizontal `ttk.Scrollbar` (`xscrollcommand`) to `ScrolledTreeview` and `<Shift-MouseWheel>` horizontal scroll support in `widgets.py`.
  2. On Windows, listen for `WM_SETTINGCHANGE` (or poll `detect_system_theme()` at low frequency only when `theme_mode == "system"`) and trigger `set_theme("system")` on change.
  3. Add configurable queue job concurrency (`queue_concurrency: 1..4`) in `GuiController` (keeping per-job `JobContext` isolation, which is already thread-safe via `contextvars`!) or overlap network download of job $N+1$ with FFmpeg combining/transcoding of job $N$.

---

#### [P2-4] Standardize Source Adapters, Auth Recovery & Plugin Validation
- **Priority:** P2 | **Categories:** `Architecture`, `API`, `Sources`
- **Affected Files:**
  - `audiobookdl/sources/__init__.py` (lines 150–190)
  - `audiobookdl/sources/source/__init__.py` (lines 110–443)
  - `audiobookdl/sources/nextory.py` (lines 67–88)
  - `audiobookdl/sources/bookbeat.py` (lines 52–77)
  - `audiobookdl/sources/ereolen.py` (lines 37–74)
  - `audiobookdl/pipeline.py` (lines 563–621, 878–942)
- **Current Shortcoming:**
  - `NextorySource._get_or_create_device_id` and `BookBeatSource._get_or_create_device_id` duplicate file-backed UUID persistence.
  - When cached `session_cookies.txt` expires on disk, `authenticate()` (`pipeline.py:924-930`) marks `source.authenticated = True` solely because non-expired-timestamp cookies loaded into the jar; if the server subsequently returns `401`/`403` during `source.download(url)` (`UserNotAuthorized`), the pipeline fails immediately instead of deleting stale cached cookies and re-running `login()` once.
  - `EreolenSource.refresh_library_domains()` mutates class attributes `cls.library_domains` and `cls.match` without an internal lock, creating a race if called concurrently from `prepare_source_result` threads.
  - `_discover_plugin_sources()` (`sources/__init__.py`) is decorated with `@functools.lru_cache(maxsize=1)` with no cache-reset helper for testing/runtime reload and does not validate duplicate service keys or `Source` API version compatibility.
- **Actionable Implementation Steps:**
  1. Extract `Source.get_or_create_device_id(filename: str, factory: Callable[[], str]) -> str` onto `Source` in `audiobookdl/sources/source/__init__.py` and remove the duplicates in `nextory.py` and `bookbeat.py`.
  2. Add a single retry-on-expired-session hook in `pipeline.py:prepare_source_result`: if `source` loaded cached `session_cookies*.txt`, `source.supports_login` is `True`, and `source.download(url)` raises `UserNotAuthorized`, clear the stale cookie jar, re-authenticate via `login()`, and retry `source.download(url)` once.
  3. Guard `EreolenSource.refresh_library_domains()` with a class-level `threading.Lock` and add collision detection/logging in `_discover_plugin_sources()`.

---

#### [P2-5] Enhance Audiobookshelf Integration, Enrichment Plugins & Media Pipeline Features
- **Priority:** P2 | **Categories:** `Features`, `Integrations`, `API`
- **Affected Files:**
  - `audiobookdl/integrations/audiobookshelf.py` (lines 61–169, 198–230, 335–374)
  - `audiobookdl/utils/enrichment.py` (lines 35–211)
  - `audiobookdl/output/metadata/sidecars.py` (lines 17–71, 143–200)
- **Current Shortcoming:**
  - `AudiobookshelfClient._request` does not retry transient 5xx/connection failures, and `_MultipartUploadStream.read()` (`audiobookshelf.py:132-167`) does not report byte progress—large multi-gigabyte `.m4b` uploads appear frozen in the GUI.
  - `AudiobookshelfClient` has no duplicate-check against the target ABS library before uploading (`GET /api/libraries/{id}/items?q=...`), risking duplicate book folders on re-runs.
  - Custom metadata enrichment supports ABS HTTP endpoints (`[enrichment.custom.<name>]`), but there is no `audiobookdl.enrichment` `importlib.metadata` Python plugin entry point group analogous to `audiobookdl.sources`.
  - `sidecars.py:build_opf_xml` hardcodes `cover_filename` MIME type to `image/png` or `image/jpeg` (omitting `image/webp` even though `audiobookshelf.py` and `reader.py` support `.webp`).
- **Actionable Implementation Steps:**
  1. Add transient retry/backoff for idempotent ABS API calls and an `on_bytes: Callable[[int], None]` progress callback in `_MultipartUploadStream.read()` wired into `GuiLogSink`/`JobContext`.
  2. Add an optional `skip_if_exists_in_abs` check querying `/api/libraries/{id}/search?q=...` (matching ASIN/ISBN/title+author) before uploading.
  3. Add `audiobookdl.enrichment` entry-point discovery in `enrichment.py` and fix `.webp` MIME mapping in `sidecars.py:61`.

---

#### [P2-6] Harden CI Matrix, Static Analysis (`ruff` / `mypy`), Coverage & Multi-OS Releases
- **Priority:** P2 | **Categories:** `Testing`, `Tooling`, `CI/CD`
- **Affected Files:**
  - `.github/workflows/ci.yml`
  - `.github/workflows/release.yml`
  - `pyproject.toml` (lines 71–90)
  - `tests/sources/`
- **Current Shortcoming:**
  - CI matrix (`ci.yml:13-15`) runs `ubuntu-latest` and `windows-latest` on Python `3.11` and `3.13` only. `pyproject.toml` claims `requires-python = ">=3.10"` (with `tomli; python_version < '3.11'` and `scripts/build_exe.py:_collect_mypyc_binaries`), yet Python 3.10 is never tested in CI.
  - `macos-latest` is absent from both `ci.yml` and `release.yml`, leaving `osascript` notifications (`notifications.py:177`), macOS dark-mode detection (`theme.py:119`), `tkinterdnd2`, and Finder `-R` reveal (`app.py:1519`) completely untested in CI.
  - `ruff` rules in `pyproject.toml:76` (`["E9", "E722", "F", "B", "UP", "RUF100"]`) omit import sorting (`I`), comprehensions (`C4`), simplifications (`SIM`), performance (`PERF`), and security (`S`).
  - `mypy` runs only on `audiobookdl`, not `tests` or `scripts`, and CI tracks no code-coverage threshold (`pytest-cov`).
  - `release.yml` builds Windows `.exe` / `.zip` only; PyPI wheel/sdist publishing and Linux/macOS artifacts are manual or absent.
- **Actionable Implementation Steps:**
  1. Add `macos-latest` and Python `3.10` to `.github/workflows/ci.yml` (at minimum unit/lint jobs) and add `pytest --cov=audiobookdl --cov-report=xml` with a baseline coverage floor.
  2. Expand `[tool.ruff.lint]` incrementally to include `I`, `C4`, `SIM`, `PERF`, and `PT` (using `per-file-ignores` as needed), and include `scripts/` in `mypy`.
  3. Add a PyPI trusted-publishing (`pypa/gh-action-pypi-publish`) / wheel verification job to `.github/workflows/release.yml`.

---

#### [P2-7] Remove Dead Code, Drop Unused `rich` Runtime Dependency & Clean Temp Lock Artifacts
- **Priority:** P2 | **Categories:** `Cleanup`, `Dependencies`, `Performance`
- **Affected Files:**
  - `pyproject.toml` (line 32)
  - `audiobookdl/logging.py` (lines 5, 151–156)
  - `audiobookdl/output/download.py` (lines 1205–1211)
  - `audiobookdl/output/output.py` (lines 80–85)
- **Current Shortcoming:**
  - `gen_output_filename()` (`output.py:80-85`) has zero callers across `audiobookdl/` and `tests/`—it is dead legacy code superseded by `gen_output_location()`.
  - `rich` is listed as a mandatory runtime dependency (`pyproject.toml:32`) solely for `Text.from_markup(msg).plain` in `logging.py:154` and `rich.markup.escape` in `download.py:1205`, pulling `rich` (plus `markdown-it-py`, `mdurl`, `pygments`) into every pip install and PyInstaller `.exe` bundle even though the terminal UI was removed.
- **Actionable Implementation Steps:**
  1. Remove `gen_output_filename()` from `audiobookdl/output/output.py`.
  2. Replace `strip_markup` and `rich.markup.escape` with a lightweight regex/tag-stripper (supporting `\[` escapes and `[tag]...[/tag]`/`[/]` markup) in `audiobookdl/logging.py`, and remove `rich` from `dependencies` in `pyproject.toml` (cutting PyInstaller bundle size and import latency).

---

### P3 — Documentation & Strategic Feature Ideas

#### [P3-1] Complete Configuration Reference, Plugin SDK Guide & Contributor Docs
- **Priority:** P3 | **Categories:** `Documentation`, `DX`
- **Affected Files:**
  - `README.md`
  - `docs/` (`README.md`, `supported_sites.md`, `architecture.md`, `workflows.md`, `testing.md`)
- **Current Shortcoming:**
  - `OPTION_REGISTRY` defines 45+ runtime/config options (`loudnorm`, `mono`, `chapters_from_silence`, `chapter_offset_ms`, `intro_chapter_mode`, `square_cover`, `max_cover_size`, `limit_rate`, `proxy`, `timeout`, `impersonate`, `max_workers`, `book_delay`, `post_exec`, `enrich_*`, `write_opf`, `write_abs_metadata`, `write_cue`, `upload_to_abs`, etc.), of which `README.md` documents only a small subset in its TOML snippet.
  - `docs/supported_sites.md` documents `audiobookdl.sources` entry points in 5 lines without showing the required `Source` attributes, `Audiobook`/`Result` return contracts, `CatalogBook` fields, or `verify_auth()` expectations.
  - No `CONTRIBUTING.md`, `CHANGELOG.md`, or `SECURITY.md` exists in the repository.
- **Actionable Implementation Steps:**
  1. Add a script (or doc-sync test) that generates/verifies a complete TOML configuration reference table directly from `CONFIG_OPTION_SPECS` and `GUI_CONFIG_KEYS` into `docs/configuration.md`.
  2. Add `docs/plugins.md` with a complete minimal `Source` plugin template and link it from `docs/README.md`.
  3. Add `CONTRIBUTING.md` (covering `.venv`, `ruff`, `mypy`, `pytest -m "not e2e"`, and `pytest -m e2e`), `CHANGELOG.md`, and `SECURITY.md`.

---

#### [P3-2] Strategic Feature Ideas (Audio Auditioning, Tray Daemon, Smart Deduplication)
- **Priority:** P3 | **Categories:** `Features`, `UI`, `UX`
- **Affected Files:**
  - `audiobookdl/gui/chapter_editor.py`
  - `audiobookdl/gui/app.py`
  - `audiobookdl/utils/archive.py`
- **Current Shortcoming:**
  - `ChapterEditorFrame` lets users inspect/shift/merge chapter timestamps numerically, but has no audio playback/scrubbing preview (e.g., playing 5 seconds around a chapter boundary via `ffplay`/platform audio) to verify silence-detected timestamps before muxing.
  - Scheduled auto-sync (`AutomationCoordinator`) only runs while the main desktop window is open; closing the window exits the app with no minimize-to-system-tray option.
  - `DownloadArchive` has `ArchiveMatchIndex` for local scans, but lacks fuzzy duplicate detection across services (e.g., warning when queuing a Storytel book that already exists from Libro.fm by matching ISBN/normalized title+author).
- **Actionable Implementation Steps:**
  1. Add a lightweight `"Audition 5s"` button in `ChapterEditorFrame` launching `ffplay -nodisp -autoexit -ss <start> -t 5 <file>` (when `ffplay` is detected via `find_executable`) with clean subprocess cancellation.
  2. Add optional minimize-to-tray support (e.g. optional `[tray]` extra with `pystray`) for users running scheduled `auto_sync_enabled` passes.
  3. Add pre-enqueue duplicate detection using `ArchiveMatchIndex` in `GuiController` / `Downloader` to warn or skip cross-source duplicates when `skip_downloaded` is enabled.

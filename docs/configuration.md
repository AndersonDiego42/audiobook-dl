# Configuration Reference

`audiobook-dl` is configured through a TOML file (`audiobook-dl.toml`), optional environment variables, and Desktop GUI settings persisted cleanly with [`tomlkit`](https://github.com/python-poetry/tomlkit) (preserving comments and unrelated sections).

## Config File Discovery

1. **`AUDIOBOOK_DL_CONFIG` / `AUDIOBOOKDL_CONFIG`**: If set to a non-empty file path, overrides the default platform location.
2. **Portable Mode**: On frozen builds (or when a `.portable` marker file or `audiobook-dl.toml` sits beside `audiobook-dl.exe`), configuration lives beside the executable and `db/` defaults to the same directory.
3. **Platform Standard Directories**:
   - **Windows**: `%LOCALAPPDATA%\jo1gi\audiobook-dl\audiobook-dl.toml`
   - **macOS**: `~/Library/Application Support/audiobook-dl/audiobook-dl.toml`
   - **Linux**: `$XDG_CONFIG_HOME/audiobook-dl/audiobook-dl.toml` (defaults to `~/.config/audiobook-dl/audiobook-dl.toml`)

---

## Top-Level Options (`CONFIG_OPTION_SPECS`)

All runtime keys below map directly from `audiobookdl/options_schema.py` and may be placed at the top level of `audiobook-dl.toml`:

| Key | Kind | Default | Description |
|---|---|---|---|
| `combine` | `bool` | `false` | Combine output files into a single file |
| `output_template` | `str` | `"{title}"` | Output location template (supports conditional `{key?yes\|no}` blocks) |
| `remove_chars` | `str` | `""` | Characters stripped from sanitized output paths |
| `debug` | `bool` | `false` | Enable verbose debug logging |
| `cover` | `bool` | `false` | Download only cover artwork |
| `no_chapters` | `bool` | `false` | Omit embedded chapters from combined output |
| `split_chapters` | `bool` | `false` | Split downloaded audiobook into separate per-chapter files |
| `output_format` | `str` | unset (`None`) | Target audio container format (for example `m4b`, `mp3`, `m4a`, `flac`, `opus`) |
| `media_type` | `media_type` | `"auto"` | Media selection when a source offers multiple formats (`auto`, `audiobook`, `ebook`, `video`, `both`) |
| `skip_downloaded` | `bool` | `false` | Skip downloading books whose output file/directory or archive record already exists |
| `skip_unavailable` | `bool` | `false` | Skip unavailable or inaccessible books and continue bulk downloads |
| `download_archive` | `str` | unset (`None`) | Path to SQLite download archive database (`downloads.db`) |
| `database_directory` | `str` | unset (`None`) | Base directory for per-source state, cached cookies, and default databases |
| `write_json_metadata` | `bool` | `false` | Write raw metadata JSON sidecar beside downloaded files |
| `write_cover` | `bool` | `false` | Save cover artwork as a separate image sidecar |
| `write_opf` | `bool` | `false` | Write Calibre/Audiobookshelf `metadata.opf` XML sidecar |
| `write_abs_metadata` | `bool` | `false` | Write Audiobookshelf `metadata.json` sidecar |
| `upload_to_abs` | `bool` | `false` | Upload completed audiobooks to the configured Audiobookshelf server |
| `write_cue` | `bool` | `false` | Write chapter timestamps as a `.cue` sheet sidecar |
| `resume` | `bool` | `false` | Resume interrupted downloads using existing part files and HTTP `Range` requests |
| `audio_bitrate` | `audio_bitrate` | unset (`None`) | Target AAC bitrate when encoding `.m4b`/`.m4a` (`auto`, `64k`, `128k`, `192k`, etc.) |
| `overwrite` | `bool` | unset (`None`) | Non-interactive overwrite decision (`true` overwrite, `false` keep existing, unset prompt) |
| `max_workers` | `positive_int` | unset (`None`) | Maximum concurrent segment/file download and remux worker threads |
| `book_delay` | `non_negative_float` | `0.0` | Seconds to wait between consecutive books in a batch or series |
| `limit_rate` | `limit_rate` | unset (`None`) | Aggregate download bandwidth limit (for example `500K`, `2M`) |
| `post_exec` | `post_exec` | unset (`None`) | Command (`list[str]` argv strongly preferred over shell strings; `AUDIOBOOK_DL_*` env vars populated, 300s timeout) |
| `loudnorm` | `bool` | `false` | Apply FFmpeg EBU R128 loudness normalization during audio processing |
| `mono` | `bool` | `false` | Downmix audio streams to mono (1 channel) |
| `chapters_from_silence` | `bool` | `false` | Detect pause boundaries via FFmpeg `silencedetect` when chapter metadata is absent |
| `silence_noise_db` | `silence_noise_db` | `-35.0` | Noise floor threshold in dB for FFmpeg `silencedetect` |
| `silence_min_seconds` | `positive_float` | `1.8` | Minimum silence duration in seconds for FFmpeg `silencedetect` |
| `chapter_offset_ms` | `int` | `0` | Shift non-initial chapter timestamps by `N` milliseconds |
| `intro_chapter_mode` | `intro_chapter_mode` | `"auto"` | Numbering mode when splitting chapters with an intro/prologue (`auto`, `always`, `off`) |
| `square_cover` | `bool` | `false` | Pad cover artwork to a `1:1` square aspect ratio before embedding/writing |
| `max_cover_size` | `positive_int` | unset (`None`) | Downscale oversized covers so width and height do not exceed `N` pixels |
| `proxy` | `str` | unset (`None`) | HTTP/HTTPS proxy URL passed to source and enrichment requests |
| `timeout` | `positive_float` | unset (`None`) | HTTP request/read timeout in seconds |
| `max_retries` | `non_negative_int` | `4` | Maximum automatic retry attempts with exponential backoff for failed requests (`0` disables retries) |
| `impersonate` | `str` | unset (`None`) | Browser TLS/HTTP2 fingerprint for optional `curl_cffi` (`chrome`, `edge`, `safari`) |
| `locale` | `str` | unset (`None`) | Regional locale override for localized catalogs such as Nextory (e.g. `sv_SE`, `en_GB`) |
| `enrich_metadata` | `bool` | `false` | Fill missing metadata, cover art, and chapters from enrichment providers |
| `enrich_providers` | `enrich_providers` | unset (`None`) | Comma-separated provider order (`audnexus`, `itunes`, `google`, `openlibrary`, `custom:<name>`, `plugin:<name>`) |
| `enrich_interactive` | `bool` | `false` | Prompt user interactively to choose from ranked enrichment candidates |
| `check_updates` | `bool` | `false` | Check for newer `audiobook-dl` releases in the background at GUI startup |
| `ffmpeg_dir` | `str` | unset (`None`) | Custom directory containing `ffmpeg` and `ffprobe` executables |

---

## Desktop GUI Section (`[gui]`)

All 17 persisted GUI preferences declared in `GUI_CONFIG_KEYS`:

| Key | Type | Notes |
|---|---|---|
| `window_geometry` | `str` | Last main window `WxH+X+Y` geometry |
| `window_maximized` | `bool` | Whether the main window starts maximized |
| `download_split_ratio` | `float` | Paned split position on the Download page (`0.0`–`1.0`) |
| `active_page` | `str` | Last opened sidebar page (`download`, `library`, `pack`, `accounts`, `settings`) |
| `options_collapsed` | `bool` | Collapsible state of **Options** on the Download page |
| `output_naming_collapsed` | `bool` | Collapsible state of **Output & Naming** on the Download page |
| `output_dir` | `str` | Default base destination folder shown in the GUI |
| `output_template` | `str` | GUI-selected naming preset or custom template string |
| `use_download_archive` | `bool` | Enable tracking and skipping via `downloads.db` |
| `theme` | `str` | GUI theme mode (`"system"`, `"light"`, or `"dark"`; `"system"` tracks live OS theme changes) |
| `notify_on_complete` | `bool` | Show desktop notifications when downloads finish or fail |
| `clipboard_monitor` | `bool` | Watch clipboard in the background for supported audiobook URLs |
| `auto_sync_enabled` | `bool` | Enable periodic background library account sync |
| `auto_sync_minutes` | `int` | Polling interval in minutes for scheduled library sync |
| `auto_sync_mode` | `str` | Scheduled sync action (`"refresh"` or `"download"`) |
| `auto_sync_targets` | `list[str]` | List of `<service>:<profile>` pairs included in scheduled sync |
| `queue_concurrency` | `int` | Parallel download queue workers (`1`–`4`) |
| `minimize_to_tray` | `bool` | Minimize window to OS system tray on close when `pystray` is installed |

---

## Service Accounts & Profiles (`[sources.<service>]`)

Each source table (and optional named profile `[sources.<service>.profiles.<name>]`) supports the keys in `SOURCE_CONFIG_KEYS`:

| Key | Type | Notes |
|---|---|---|
| `username` | `str` | Account login identifier |
| `password` | `str` | Legacy fallback only; automatically migrated into Windows DPAPI / OS keyring and scrubbed on load |
| `library` | `str` | Library card / profile identifier (for eReolen, Overdrive/Libby, Nextory, etc.) |
| `cookie_file` | `str` | Path to a Netscape-format cookies file |
| `locale` | `str` | Per-source locale override |

---

## Audiobookshelf (`[audiobookshelf]`)

```toml
[audiobookshelf]
url = "https://abs.example.com"
library_id = "lib-1"
auto_upload = false
auto_scan = true
```

- `url` (`str`): Base URL of the Audiobookshelf server (`https://` strongly recommended for non-loopback hosts).
- `api_key` (`str`): Legacy inline token only; automatically migrated into the OS credential store (`credentials.dpapi` or keyring) on load.
- `library_id` (`str`): Target ABS library ID for duplicate checks, uploads, and scans.
- `auto_upload` (`bool`): Upload completed `.m4b`/folder outputs automatically when enabled.
- `auto_scan` (`bool`): Trigger a library scan after upload finishes.

---

## Custom Metadata Enrichment (`[enrichment.custom.<name>]`)

```toml
[enrichment.custom.hardcover]
url = "https://hardcover-provider.example.com"
enabled = true
```

- `url` (`str`): Base `http://` or `https://` endpoint implementing the Audiobookshelf metadata provider `/search` contract.
- `api_key` (`str`): Automatically migrated into the credential store when present in TOML.
- `enabled` (`bool`): Whether `custom:<name>` is available when enabled in the GUI or `enrich_providers`.

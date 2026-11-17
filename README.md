# audiobook-dl
![GitHub release](https://img.shields.io/github/v/release/jo1gi/audiobook-dl)
![GitHub top language](https://img.shields.io/github/languages/top/jo1gi/audiobook-dl)
![License](https://img.shields.io/github/license/jo1gi/audiobook-dl)

CLI tool, Python library, headless Web UI, and desktop GUI for downloading audiobooks from online sources.

## Maintainers
I'm currently looking for maintainers to support the different services. I
don't use all the services and have a hard time keeping up the support.

If you are interested, please contact me on:
- Matrix: jo1gi:matrix.org
- XMPP: verdantislet@movim.eu

## Supported Services
audiobook-dl currently supports downloading from the following built-in sources:
- [Archive.org (Internet Archive)](https://archive.org/details/audio)
- [audiobooks.com](https://audiobooks.com)
- [Blinkist](https://www.blinkist.com)
- [BookBeat](https://www.bookbeat.com/)
- [Chirp](https://www.chirpbooks.com/)
- [eReolen](https://ereolen.dk)
- [Everand (previously Scribd)](https://everand.com)
- [Libro.fm](https://libro.fm)
- [Librivox](https://librivox.org)
- [Nextory](https://nextory.com)
- [Overdrive](https://www.overdrive.com/) / [Libby](https://libbyapp.com/)
- [Podimo](https://podimo.com)
- RSS / Atom Podcast & Audiobook Feeds (`.xml` / `.rss`)
- [Saxo](https://saxo.com)
- [Storytel](https://www.storytel.com/) / [Mofibo](https://mofibo.com)
- [YourCloudLibrary](https://www.yourcloudlibrary.com/)

[Capability matrix, service aliases, and plugin entry points](./docs/supported_sites.md)

## Installation
audiobook-dl requires **Python 3.10 or newer** and can be installed from PyPI, directly from the repository, or run as a portable Windows executable.

To install the newest stable version with pip:
```shell
pip install audiobook-dl
```

Optional extras:
```shell
pip install "audiobook-dl[export]"       # openpyxl>=3.1 for .xlsx Excel catalog exports
pip install "audiobook-dl[impersonate]"  # curl_cffi>=0.7 for --impersonate browser TLS/HTTP2 impersonation
```

To install the latest development version from Git:
```shell
pip install "git+https://github.com/jo1gi/audiobook-dl.git"
```
or from a local clone:
```shell
git clone https://github.com/jo1gi/audiobook-dl.git
cd audiobook-dl
pip install .
# Development / Windows executable tooling:
pip install -e ".[dev]"   # pytest, mypy, ruff
pip install ".[build]"    # PyInstaller
```

Combining parts, format conversion (e.g. `.m4b`), chapter embedding, silence chapter detection, and audio verification require [ffmpeg](https://ffmpeg.org/) (`ffmpeg` and `ffprobe`), which can be installed through most package managers or from [ffmpeg.org/download.html](https://ffmpeg.org/download.html).

## Desktop GUI
audiobook-dl includes a Tkinter desktop interface (`audiobook-dl-gui`):
```shell
audiobook-dl-gui
# or
python -m audiobookdl.gui
```

Key GUI features:
- **Five-page sidebar navigation**: Switch between **Download**, **Library** (`Archive`, `Catalog`, `Sync`), **Pack**, **Accounts**, and **Settings** using the sidebar or keyboard shortcuts (`Ctrl+1`–`Ctrl+5`, `Ctrl+O` open URL list, `Ctrl+P` preview, `F5` refresh, `Ctrl+Enter` start/queue, `Ctrl+Q` exit).
- **URL auto-detection & drag-and-drop**: Paste, drop OS files/folders/URLs/`.txt` URL lists onto the window, capture supported URLs from the clipboard, or add extra items directly to an active batch with **Add to Queue**.
- **Preview, Enrichment & Chapter Editor**: Inspect and edit metadata/cover, preview individual books in a series, search external metadata providers via the diff picker, and edit, add, shift timestamps, merge, import, or export `.cue`/`.json` chapters in the interactive chapter editor.
- **Persistent Queue & Per-Job Options**: View inline job profiles and live stage progress, edit per-job profile/format overrides before running, cooperatively pause/resume mid-book, and export or import queues as `.json`/`.txt`.
- **One-click `.m4b` & Local Pack**: Repackage local audio folders into chaptered `.m4b` files on the **Pack** page, and choose **M4B Quality** (`Auto`, `64k`, `96k`, `128k`, `192k`, `256k`).
- **Paginated Library Ledger & Sync**: Browse `downloads.db` with pagination, edit item metadata/tags, hide/trash items, verify missing paths, scan local folders, sync linked accounts on a schedule, upload to Audiobookshelf, and export to `.csv`, `.json`, or `.xlsx`.
- **Credential & FFmpeg Management**: Configure multi-profile logins, import browser cookies, and run connection tests from **Accounts**, or pick a custom `ffmpeg`/`ffprobe` directory in **Settings**.

## Authentication

### Cookies
audiobook-dl uses Netscape cookie files for authentication on several services. You can use [this](https://github.com/rotemdan/ExportCookies) browser extension to export cookies (or import them interactively via the Desktop GUI).

Cookies can be placed in the current directory as `cookies.txt` or passed with `-c` / `--cookies`.

### Login
[Some sources](./docs/supported_sites.md) support authentication through username and password (and sometimes library). Use `--username`, `--password`, and `--library`, enter them interactively when prompted, or save them via the GUI / configuration file. Successful logins also cache session cookies per source/profile under `<database_directory>/<service>/` so subsequent runs reuse active sessions.

## Downloading audiobooks (CLI)
```shell
audiobook-dl -c <cookie file> <url>
```
To combine multi-part streams into a single chaptered `.m4b`:
```shell
audiobook-dl --combine -f m4b <url>
```
To list supported service keys, test credentials, search, or browse your library:
```shell
audiobook-dl --list-sources
audiobook-dl --service librivox --search "moby dick"
audiobook-dl --service storytel --profile family --verify-auth
audiobook-dl --service storytel --library-list
```
To launch the headless Web UI dashboard and JSON API (ideal for NAS/Docker setups):
```shell
audiobook-dl serve --host 0.0.0.0 --port 8080
```
**Most sites require you to provide the listening page, not just the book information page.**

## Arguments

| Argument | Description |
|---|---|
| `urls` | One or more book, series, or feed URLs to download |
| `-v`, `--version` | Show program version and exit |
| `-c`, `--cookies` | Path to a Netscape cookie file |
| `--combine`, `--no-combine` | Combine output files into a single file (requires `ffmpeg`), or keep separate (`--no-combine` overrides config) |
| `--split-chapters` | Split a continuous book into separate per-chapter audio files |
| `-o`, `--output` | Output path template (default: `{title}`) |
| `--output-preset` | Named layout preset (`default`, `audiobookshelf`, `plex`, `kavita`; overridden by `-o`/`--output`) |
| `--remove-chars` | List of characters to remove from metadata values in output paths |
| `-f`, `--output-format` | Output file format (e.g. `m4b`, `mp3`, `m4a`, `flac`, `opus`) |
| `--media-type` | Media format preference (`auto`, `audiobook`, `ebook`, `both`) |
| `--audio-bitrate`, `--m4b-bitrate` | Target AAC bitrate when converting to `.m4b`/`.m4a` (`auto`, `64k`, `128k`, `192k`, etc.) |
| `--loudnorm` | Apply EBU R128 loudness normalization during `ffmpeg` combine/conversion |
| `--mono` | Downmix audio to mono (`-ac 1`) during `ffmpeg` combine/conversion |
| `--resume`, `--no-resume` | Resume interrupted downloads using existing parts and HTTP `Range` requests (or override config) |
| `--skip-downloaded`, `--no-skip-downloaded` | Skip books already downloaded on disk or in SQLite `downloads.db` (or force re-download) |
| `--download-archive` | Custom SQLite path for library ledger (`downloads.db`) |
| `--no-download-archive` | Disable reading/writing the SQLite download archive ledger |
| `--no-chapters` | Don't include chapters in output file |
| `--chapters-from-silence` | Detect silence pauses on single-file inputs to synthesize chapters when none exist |
| `--chapter-offset` | Shift non-initial chapter timestamps by `N` milliseconds |
| `--cover` | Download only the cover image |
| `--square-cover` | Pad cover artwork to a `1:1` square canvas |
| `--max-cover-size` | Downscale cover artwork exceeding `N` pixels |
| `--write-cover` | Save cover artwork as a sidecar image file |
| `--write-json-metadata` | Write raw metadata JSON (`<file>.json` in single-file mode; `metadata.json` in directory mode, or `audiobook.json` when paired with `--write-abs-metadata`) |
| `--write-abs-metadata` | Write Audiobookshelf-compatible `metadata.json` sidecar (`<stem>.metadata.json` if sharing a flat directory) |
| `--write-opf` | Write OPF XML sidecar metadata |
| `--write-cue` | Write CUE chapter sheet sidecar |
| `--upload-to-abs` | Upload completed book to configured Audiobookshelf server |
| `-y`, `--yes`, `--overwrite` / `--no-overwrite` | Non-interactive file replacement policy |
| `--enrich`, `--enrich-metadata` | Fill missing metadata/cover/chapters via Audnexus, iTunes, Google Books, and OpenLibrary |
| `--enrich-providers` | Ordered enrichment provider list (`audnexus,itunes,google,openlibrary`) |
| `--enrich-interactive` | Choose interactively from ranked enrichment matches |
| `--no-enrich-cache` | Bypass enrichment SQLite cache |
| `--max-workers` | Maximum concurrent worker threads (`>= 1`; downloads default to `6`, capped at `20`) |
| `--limit-rate` | Aggregate bandwidth limit (e.g. `500K`, `2M`) |
| `--rate-limit`, `--book-delay` | Delay in seconds between consecutive books |
| `--proxy` | HTTP/HTTPS proxy URL |
| `--timeout` | HTTP read timeout in seconds |
| `--impersonate` | Browser TLS/HTTP2 fingerprint to impersonate via optional `curl_cffi` (e.g. `chrome`, `edge`, `safari`; install `audiobook-dl[impersonate]`) |
| `--service`, `--profile` | Target service key and named account profile |
| `--search`, `--library-list`, `--verify-auth` | Search catalog (`--service` defaults to `librivox`), list library books, or test credentials (`--service` required for library/auth) |
| `--list-sources` | List supported audiobook services |
| `--info`, `--dry-run` | Inspect metadata and output path without downloading |
| `--series-items` | Select 1-based range/indices in a series (e.g. `1,3-5`) |
| `--post-exec` | Shell command run after each book completes (`AUDIOBOOK_DL_*` env vars) |
| `--json` | Emit structured JSON output on stdout |
| `--print-output` | Print resolved output locations instead of downloading |
| `--input-file` | File containing URLs (whitespace/newline separated) |
| `--username`, `--password`, `--library` | Account credentials for login-based sources |
| `--database-directory`, `--database_directory` | Directory for SQLite databases and per-source state |
| `--config` | Alternative location of `audiobook-dl.toml` (defaults to `$AUDIOBOOK_DL_CONFIG` if set) |
| `--check-updates` | Check GitHub Releases (falling back to PyPI) for a newer version and exit |
| `-d`, `--debug` | Enable debug logging |
| `-q`, `--quiet` | Suppress non-essential output |
| `--verbose-ffmpeg` | Show `ffmpeg` output in terminal |

## Output
By default, audiobook-dl saves audiobooks to `{title}` relative to the current directory. This can be customized with `-o` / `--output` or `--output-preset` using metadata fields:
- `title`, `author`, `narrator`
- `series`, `series_order`, `series_order_padded`
- `genre`, `publisher`, `isbn`, `asin`, `language`
- `release_date`, `year`
- `source`, `scrape_url`, `description`, `album`, `artist`

Templates also support conditional blocks `{key?true_branch}` and `{key?true_branch|false_branch}`, which include a branch only when `key` has a value (any nested `{field}` inside the chosen branch is expanded normally):
```text
{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}
```

Built-in `--output-preset` layouts:
- `default`: `{title}`
- `audiobookshelf`: `{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}`
- `plex`: `{author}/{title}/{title}`
- `kavita`: `{author}/{series?{series}/}{year?({year}) }{title}`

Not all fields are available for every audiobook. Metadata values are sanitized for filesystem safety (removing invalid characters, normalizing Windows reserved filenames, bounding path/component length, and preventing path traversal).

The output container can be changed with `-f` / `--output-format` (for example `-f m4b`). When converting non-AAC audio to `.m4b`/`.m4a`, `--audio-bitrate` defaults to `auto` (probing source bitrate with `ffprobe`, clamped to `64k`–`192k`).

## Configuration
audiobook-dl can be configured via `audiobook-dl.toml` at:
- Windows: `%LOCALAPPDATA%\jo1gi\audiobook-dl\audiobook-dl.toml`
- macOS: `~/Library/Application Support/audiobook-dl/audiobook-dl.toml`
- Linux: `$XDG_CONFIG_HOME/audiobook-dl/audiobook-dl.toml` (defaults to `~/.config/audiobook-dl/audiobook-dl.toml`)

In portable Windows `.exe` mode, placing a `.portable` marker file or `audiobook-dl.toml` beside `audiobook-dl.exe` keeps `audiobook-dl.toml` and `db/` in that same folder instead of `%LOCALAPPDATA%`. Updates written by audiobook-dl preserve `#` comments and unknown TOML sections.

### Global Options
```toml
output_template = "{author}/{title}"
output_format = "m4b"
combine = true
audio_bitrate = "128k"
resume = true
skip_downloaded = true
ffmpeg_dir = "C:/ffmpeg/bin"
```

### Authentication, Profiles, GUI & Integrations
Non-secret source settings can be stored under `[sources.<service>]`, with optional named profiles under `[sources.<service>.profiles.<name>]`, GUI preferences under `[gui]`, and Audiobookshelf settings under `[audiobookshelf]`:
```toml
[sources.yourcloudlibrary]
username = "yourusername"
library = "hometown"

[sources.everand]
cookie_file = "./everand_cookies.txt"

[sources.storytel.profiles.family]
username = "family@example.com"

[gui]
output_dir = "/audiobooks"
theme = "dark"
use_download_archive = true
notify_on_complete = true

[audiobookshelf]
url = "https://abs.example.com"
library_id = "lib-1"
auto_upload = false
auto_scan = true
```

### Credential Storage & Session Cookies
- **Passwords & ABS API tokens**: Stored in `credentials.dpapi` (Windows DPAPI) or the OS keyring with a `credentials.keyring.json` index (macOS/Linux) rather than plaintext in `audiobook-dl.toml`. Legacy `password = "..."` entries in `audiobook-dl.toml` are automatically migrated and scrubbed on load when a secure store is available. In headless containers without an OS keyring, set `AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS=1` if you want to opt into plaintext config storage (or use cookie files).
- **Session cookies**: Cached automatically after authentication at `<database_directory>/<service>/session_cookies.txt` (or `session_cookies_<profile>.txt` for named profiles) with owner-only file permissions.

### Environment Variables
| Variable | Purpose |
|---|---|
| `AUDIOBOOK_DL_CONFIG` (or `AUDIOBOOKDL_CONFIG`) | Default path to `audiobook-dl.toml` when `--config` is not passed |
| `AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS` | Set to `1` / `true` to opt in when running without an OS keyring |
| `AUDIOBOOK_DL_WEB_TOKEN` | Default Bearer/`X-API-Token` secret for `audiobook-dl serve` |
| `AUDIOBOOK_DL_NO_UPDATE_CHECK` | Set to `1` / `true` to disable automatic background update checks |
| `AUDIOBOOK_DL_SAXO_APP_OS`, `AUDIOBOOK_DL_SAXO_APP_VERSION` | Override Saxo client request headers |
| `AUDIOBOOK_DL_PATH`, `AUDIOBOOK_DL_TITLE`, `AUDIOBOOK_DL_AUTHOR`, `AUDIOBOOK_DL_SOURCE`, `AUDIOBOOK_DL_FORMAT` | Populated in the child environment when running `--post-exec` commands |

## Python Library API
`audiobookdl` also exposes a programmatic client (`Downloader`, `DownloadOptions`, `DownloadSummary`, `DownloadEvent`):
```python
from audiobookdl import Downloader, DownloadOptions

options = DownloadOptions(combine=True, output_format="m4b", resume=True)
with Downloader(options=options, on_event=lambda ev: print(ev.kind, ev.data)) as downloader:
    summary = downloader.download("https://librivox.org/...")
```

`Downloader` isolates logging, interaction handlers, and `ffmpeg_dir` per instance via `JobContext` (so multiple instances do not leak global logging state), exposes `prepare()`, `fetch_metadata()`, `download_result()`, `download()`, and `close()`, and emits `DownloadEvent` instances with kinds `log`, `progress_start`, `progress_advance`, `progress_metrics`, `item_progress`, and `progress_finish`. Custom `Source` plugins can be registered via `audiobookdl.sources` entry points (see [supported_sites.md](./docs/supported_sites.md)).

## Portable Windows Executable
You can build a standalone Windows executable (`dist/audiobook-dl.exe`) using PyInstaller:
```shell
pip install ".[build]"
python scripts/build_exe.py
```

Useful build flags:
- `--onedir`: Build a fast-startup portable folder (`dist/audiobook-dl/`) containing `audiobook-dl.exe` and the console companion `audiobook-dl-cli.exe` instead of a single `--onefile` binary.
- `--copy-ffmpeg`: Copy `ffmpeg` and `ffprobe` from `PATH` alongside the built executable.
- `--bundle-ffmpeg`: Embed `ffmpeg` and `ffprobe` inside the executable bundle.
- `--no-verify`: Skip the post-build `--self-test` smoke check (enabled by default).

## Docker & Headless Auto-Sync
Run automated periodic library synchronization in Docker or headless environments:

```yaml
services:
  audiobook-dl:
    build: .
    container_name: audiobook-dl
    restart: unless-stopped
    volumes:
      - ./config:/config
      - ./audiobooks:/audiobooks
    command: ["sync", "--watch", "--interval", "3600"]
```

To run the headless Web UI dashboard in Docker instead, override `command` with `["serve", "--host", "0.0.0.0", "--port", "8080"]` (optionally setting `AUDIOBOOK_DL_WEB_TOKEN`) and expose port `8080`.

## Subcommands & Headless Web API

- **`pack`**: Repackage local audio folders/tracks (`mp3`, `m4a`, `m4b`, `mp4`, `aac`, `ogg`, `opus`, `flac`, `wav`, `wma`) offline into chaptered `.m4b` audiobooks, automatically reading embedded tags and sidecar files (`metadata.json`, `audiobook.json`, `*.opf`, `*.cue`, `cover.*`/`folder.*`):
  ```shell
  audiobook-dl pack <paths...> [--recursive] [--delete-source] [--dry-run] [--json] \
    [--output-dir DIR] [--output-template TPL] [--title ...] [--author ...] [--narrator ...] \
    [--series ...] [--series-order N] [--isbn ...] [--asin ...] [--cover FILE]
  ```
- **`library`**: Query and manage persistent entries, tags, hidden items, and trash in `downloads.db` (note that `--config` and `--download-archive` go before the action subcommand):
  ```shell
  audiobook-dl library [--config FILE] [--download-archive FILE] list [-q/--query QUERY] [--limit 100] [--include-trashed] [--no-hidden] [--json]
  audiobook-dl library [--config FILE] [--download-archive FILE] tag <entry_ids...> [--add TAG...] [--remove TAG...]
  audiobook-dl library [--config FILE] [--download-archive FILE] hide|unhide|trash|restore <entry_ids...>
  ```
  Queries (`-q`/`--query`) support bare search terms, field prefixes (`author:`, `title:`, `narrator:`, `series:`, `source:`, `profile:`, `tag:`, `status:`, `isbn:`, `asin:`), negation (`-tag:hidden`, `-status:trashed`), and quoted phrases (`author:"Brandon Sanderson"`).
- **`scan`**: Discover existing local audiobooks, match them against `downloads.db`, and optionally mark missing files back to `pending`:
  ```shell
  audiobook-dl scan <paths...> [--profile NAME] [--verify-missing] [--dry-run] [--json] [--config FILE] [--download-archive FILE]
  ```
- **`export`**: Export the library ledger to CSV, JSON, or Excel (`.xlsx` requires `audiobook-dl[export]`):
  ```shell
  audiobook-dl export <output_path> [-f/--format {csv,json,xlsx}] [-q/--query QUERY] [--limit 100000] [--include-trashed] [--no-hidden]
  ```
- **`sync`**: Poll configured authenticated libraries (`supports_library`) and download unseen `pending` items (skipping `downloaded` and `trashed` entries):
  ```shell
  audiobook-dl sync [--service SERVICE...] [--profile PROFILE] [--scan-only] [--dry-run] [--watch] [--interval 3600] [-o TEMPLATE] [-f FORMAT] [--json]
  ```
- **`serve`**: Launch the headless Web UI (`http://<host>:<port>/`) and JSON API:
  ```shell
  audiobook-dl serve [--host 127.0.0.1] [--port 8080] [--output-dir DIR] [-o TEMPLATE] [-f FORMAT] [--token SECRET]
  ```
  When `--token` (or `$AUDIOBOOK_DL_WEB_TOKEN`) is configured, all `/api/*` endpoints except `/api/health` require `Authorization: Bearer <token>` or `X-API-Token: <token>`. Endpoints:
  - `GET /api/health` — liveness/version check
  - `GET /api/status` — queue state, progress, speed, ETA, and recent logs
  - `GET /api/services` — available service definitions and capabilities
  - `GET /api/library?q=...&limit=100` — query `downloads.db`
  - `POST /api/download` — queue `{urls, output_format?, combine?, output_dir?, output_template?, profile?}`
  - `POST /api/pause`, `POST /api/cancel`, `POST /api/queue/clear` — control active queue/run
  - `POST /api/catalog` — `{service, mode: "search"|"library", query?, profile?}`
  - `POST /api/sync` — trigger an immediate library sync cycle

## Documentation
All additional technical and developer documentation lives in [`docs/`](./docs/README.md):
- [Documentation Index](./docs/README.md)
- [Supported Sites, Aliases & Plugins](./docs/supported_sites.md)
- [System Architecture](./docs/architecture.md)
- [Runtime & Release Workflows](./docs/workflows.md)
- [Testing & E2E Harness Guide](./docs/testing.md)

## Contributions
Issues, bug-reports, pull requests, or ideas for features and improvements are **very welcome**.

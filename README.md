# audiobook-dl
![GitHub release](https://img.shields.io/github/v/release/jo1gi/audiobook-dl)
![GitHub top language](https://img.shields.io/github/languages/top/jo1gi/audiobook-dl)
![License](https://img.shields.io/github/license/jo1gi/audiobook-dl)

Desktop GUI application and Python library for downloading audiobooks from online sources.

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
- [O'Reilly Learning](https://learning.oreilly.com)
- [Overdrive](https://www.overdrive.com/) / [Libby](https://libbyapp.com/)
- [Podimo](https://podimo.com)
- RSS / Atom Podcast & Audiobook Feeds (`.xml` / `.rss`)
- [Saxo](https://saxo.com)
- [Storytel](https://www.storytel.com/) / [Mofibo](https://mofibo.com)
- [YourCloudLibrary](https://www.yourcloudlibrary.com/)
- [YouTube Music](https://music.youtube.com/)

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
pip install "audiobook-dl[impersonate]"  # curl_cffi>=0.7 for browser TLS/HTTP2 impersonation (impersonate)
pip install "audiobook-dl[dnd]"          # tkinterdnd2>=0.4 for native drag-and-drop on macOS/Linux
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
Launch the Tkinter desktop application:
```shell
audiobook-dl
# or
python -m audiobookdl
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
audiobook-dl uses Netscape cookie files for authentication on several services. You can use [this](https://github.com/rotemdan/ExportCookies) browser extension to export cookies, or import them interactively via the **Accounts** page in the Desktop GUI.

### Login
[Some sources](./docs/supported_sites.md) support authentication through username and password (and sometimes library). Configure credentials and named profiles via the **Accounts** page or `audiobook-dl.toml`, or enter them interactively when prompted by the GUI. Successful logins also cache session cookies per source/profile under `<database_directory>/<service>/` so subsequent runs reuse active sessions.

**Most sites require you to provide the listening page, not just the book information page.**

## Output
By default, audiobook-dl saves audiobooks to `{title}` inside the chosen output folder. Output templates can be customized in the **Output & Naming** section using metadata fields:
- `title`, `author`, `narrator`
- `series`, `series_order`, `series_order_padded`
- `genre`, `publisher`, `isbn`, `asin`, `language`
- `release_date`, `year`
- `source`, `scrape_url`, `description`, `album`, `artist`

Templates also support conditional blocks `{key?true_branch}` and `{key?true_branch|false_branch}`, which include a branch only when `key` has a value (any nested `{field}` inside the chosen branch is expanded normally):
```text
{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}
```

Built-in naming presets:
- `default`: `{title}`
- `audiobookshelf`: `{author}/{series?{series}/}{series_order?Book {series_order_padded} - }{title}`
- `plex`: `{author}/{title}/{title}`
- `kavita`: `{author}/{series?{series}/}{year?({year}) }{title}`

Not all fields are available for every audiobook. Metadata values are sanitized for filesystem safety (removing invalid characters, normalizing Windows reserved filenames, bounding path/component length, and preventing path traversal).

The output container can be changed in **Options -> General** (for example `m4b`, `mp3`, `mp4`). When converting non-AAC audio to `.m4b`/`.m4a`, **M4B Quality** defaults to `Auto` (probing source bitrate with `ffprobe`, clamped to `64k`–`192k`).

## Configuration
audiobook-dl can be configured via `audiobook-dl.toml` at:
- Windows: `%LOCALAPPDATA%\jo1gi\audiobook-dl\audiobook-dl.toml`
- macOS: `~/Library/Application Support/audiobook-dl/audiobook-dl.toml`
- Linux: `$XDG_CONFIG_HOME/audiobook-dl/audiobook-dl.toml` (defaults to `~/.config/audiobook-dl/audiobook-dl.toml`)

In portable Windows `.exe` mode, placing a `.portable` marker file or `audiobook-dl.toml` beside `audiobook-dl.exe` keeps `audiobook-dl.toml` and `db/` in that same folder instead of `%LOCALAPPDATA%`. Updates written by audiobook-dl preserve `#` comments and unknown TOML sections.

See [docs/configuration.md](./docs/configuration.md) for the complete reference covering all global TOML keys, `[gui]` preferences, `[sources.<service>]` profiles, `[audiobookshelf]`, and `[enrichment.custom.<name>]`.

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

# Optional Audiobookshelf-spec custom/community metadata providers:
[enrichment.custom.hardcover]
url = "https://hardcover-provider.example.com"
enabled = true
```

### Credential Storage & Session Cookies
- **Passwords & API tokens**: Passwords, Audiobookshelf API keys, and `[enrichment.custom.<name>]` API keys are stored in `credentials.dpapi` (Windows DPAPI) or the OS keyring with a `credentials.keyring.json` index (macOS/Linux) rather than plaintext in `audiobook-dl.toml`. Legacy `password = "..."` or `api_key = "..."` entries in `audiobook-dl.toml` are automatically migrated and scrubbed on load when a secure store is available. In headless containers without an OS keyring, set `AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS=1` to opt into plaintext storage persisted in `credentials.plaintext.json` beside `audiobook-dl.toml` (or use cookie files).
- **Session cookies**: Cached automatically after authentication at `<database_directory>/<service>/session_cookies.txt` (or `session_cookies_<profile>.txt` for named profiles) with owner-only file permissions.

### Environment Variables
| Variable | Purpose |
|---|---|
| `AUDIOBOOK_DL_CONFIG` (or `AUDIOBOOKDL_CONFIG`) | Default path to `audiobook-dl.toml` when `--config` is not passed |
| `AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS` | Set to `1` / `true` to opt in when running without an OS keyring |
| `AUDIOBOOK_DL_NO_UPDATE_CHECK` | Set to `1` / `true` to disable automatic background update checks |
| `AUDIOBOOK_DL_SAXO_APP_OS`, `AUDIOBOOK_DL_SAXO_APP_VERSION` | Override Saxo client request headers |
| `AUDIOBOOK_DL_PATH`, `AUDIOBOOK_DL_TITLE`, `AUDIOBOOK_DL_AUTHOR`, `AUDIOBOOK_DL_SOURCE`, `AUDIOBOOK_DL_FORMAT` | Populated in the child environment when running `post_exec` hooks (300s default timeout; use only trusted commands) |

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
- `--onedir`: Build a fast-startup portable folder (`dist/audiobook-dl/`) containing `audiobook-dl.exe` instead of a single `--onefile` binary.
- `--copy-ffmpeg`: Copy `ffmpeg` and `ffprobe` from `PATH` alongside the built executable.
- `--bundle-ffmpeg`: Embed `ffmpeg` and `ffprobe` inside the executable bundle.
- `--no-verify`: Skip the post-build `--self-test` smoke check (enabled by default).

## Documentation
All additional technical and developer documentation lives in [`docs/`](./docs/README.md):
- [Documentation Index](./docs/README.md)
- [Configuration Reference](./docs/configuration.md)
- [Supported Sites & Aliases](./docs/supported_sites.md)
- [Plugin SDK Guide](./docs/plugins.md)
- [System Architecture](./docs/architecture.md)
- [Runtime & Release Workflows](./docs/workflows.md)
- [Testing & E2E Harness Guide](./docs/testing.md)
- [Contributing Guide](./CONTRIBUTING.md) · [Security Policy](./SECURITY.md) · [Changelog](./CHANGELOG.md)

## Contributions
Issues, bug-reports, pull requests, or ideas for features and improvements are **very welcome**. Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before opening a PR.

# audiobook-dl
![GitHub release](https://img.shields.io/github/v/release/jo1gi/audiobook-dl)
![GitHub top language](https://img.shields.io/github/languages/top/jo1gi/audiobook-dl)
![License](https://img.shields.io/github/license/jo1gi/audiobook-dl)

CLI tool, Python library, and desktop GUI for downloading audiobooks from online sources.

## Maintainers
I'm currently looking for maintainers to support the different services. I
don't use all the services and have a hard time keeping up the support.

If you are interested, please contact me on:
- Matrix: jo1gi:matrix.org
- XMPP: verdantislet@movim.eu

## Supported Services
audiobook-dl currently supports downloading from the following sources:
- [audiobooks.com](https://audiobooks.com)
- [Blinkist](https://www.blinkist.com)
- [BookBeat](https://www.bookbeat.com/)
- [Chirp](https://www.chirpbooks.com/)
- [eReolen](https://ereolen.dk)
- [Everand (previously Scribd)](https://everand.com)
- [Librivox](https://librivox.org)
- [Nextory](https://nextory.com)
- [Overdrive](https://www.overdrive.com/) / [Libby](https://libbyapp.com/)
- [Podimo](https://podimo.com)
- [Saxo](https://saxo.com)
- [Storytel](https://www.storytel.com/) / [Mofibo](https://mofibo.com)
- [YourCloudLibrary](https://www.yourcloudlibrary.com/)

[More info](./supported_sites.md)

## Installation
audiobook-dl can be installed from PyPI, directly from the repository, or run as a portable Windows executable.

To install the newest stable version with pip:
```shell
pip install audiobook-dl
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
```

Combining parts, format conversion (e.g. `.m4b`), and chapter embedding require [ffmpeg](https://ffmpeg.org/) (`ffmpeg` and `ffprobe`), which can be installed through most package managers or from [ffmpeg.org/download.html](https://ffmpeg.org/download.html).

## Desktop GUI
audiobook-dl includes a Tkinter desktop interface (`audiobook-dl-gui`):
```shell
audiobook-dl-gui
# or
python -m audiobookdl.gui
```

Key GUI features:
- **URL auto-detection & queueing**: Paste, drag, or load `.txt` URL lists; add extra URLs to the active queue with **Add to Queue**.
- **Preview & Series selection**: Click **Preview** to inspect cover art, metadata, and part/chapter counts before downloading, or select specific books from a series.
- **One-click `.m4b` & quality control**: Check **Download as .m4b** to automatically combine parts and convert to `.m4b` in a single pass, and choose **M4B Quality** (`Auto`, `64k`, `96k`, `128k`, `192k`, `256k`). In `Auto` mode, source bitrate is probed via `ffprobe` and matched within `64k`–`192k`.
- **Resumable downloads**: Enable **Resume partial** to keep downloaded segments and resume partial files via HTTP `Range` requests after interruption.
- **Credential management**: Save per-service logins/cookie paths in `audiobook-dl.toml` with passwords stored in the OS credential store.
- **FFmpeg folder picker**: Click **Locate FFmpeg...** if `ffmpeg`/`ffprobe` is not on your `PATH`; the path is saved in `audiobook-dl.toml`.

## Authentication

### Cookies
audiobook-dl uses Netscape cookie files for authentication on several services. You can use [this](https://github.com/rotemdan/ExportCookies) browser extension to export cookies.

Cookies can be placed in the current directory as `cookies.txt` or passed with `-c` / `--cookies`.

### Login
[Some sources](./supported_sites.md) support authentication through username and password (and sometimes library). Use `--username`, `--password`, and `--library`, enter them interactively when prompted, or save them via the GUI / configuration file.

## Downloading audiobooks (CLI)
```shell
audiobook-dl -c <cookie file> <url>
```
To combine multi-part streams into a single chaptered `.m4b`:
```shell
audiobook-dl --combine -f m4b <url>
```
**Most sites require you to provide the listening page, not just the book information page.**

## Arguments

| Argument | Description |
|---|---|
| `urls` | One or more book or series URLs to download |
| `-v`, `--version` | Show program version and exit |
| `-c`, `--cookies` | Path to a Netscape cookie file |
| `--combine` | Combine output files into a single file (requires `ffmpeg`) |
| `-o`, `--output` | Output path template (default: `{title}`) |
| `--remove-chars` | List of characters to remove from metadata values in output paths |
| `-f`, `--output-format` | Output file format (e.g. `m4b`, `mp3`, `m4a`, `flac`, `opus`) |
| `--audio-bitrate`, `--m4b-bitrate` | Target AAC bitrate when converting to `.m4b`/`.m4a` (`auto`, `64k`, `128k`, `192k`, etc.) |
| `--resume` | Resume interrupted downloads using existing parts and HTTP `Range` requests |
| `--skip-downloaded` | Skip books whose output file or directory already exists |
| `--no-chapters` | Don't include chapters in output file |
| `--cover` | Download only the cover image |
| `--write-json-metadata` | Write metadata to a separate `.json` file |
| `--print-output` | Print resolved output locations instead of downloading |
| `--input-file` | File containing one URL per line |
| `--username` | Username for source (when using login) |
| `--password` | Password for source (when using login) |
| `--library` | Specific library on service (when required by login) |
| `--database_directory` | Directory for sources to store database state |
| `--config` | Alternative location of `audiobook-dl.toml` |
| `-d`, `--debug` | Enable debug logging |
| `-q`, `--quiet` | Suppress non-essential output |
| `--verbose-ffmpeg` | Show `ffmpeg` output in terminal |

## Output
By default, audiobook-dl saves audiobooks to `{title}` relative to the current directory. This can be customized with `-o` / `--output` using metadata fields:
- `title`
- `author`
- `series`
- `series_order`
- `narrator`
- `genre`
- `publisher`
- `isbn`
- `release_date`

Not all fields are available for every audiobook. Metadata values are sanitized for filesystem safety (removing invalid characters and preventing path traversal).

The output container can be changed with `-f` / `--output-format` (for example `-f m4b`). When converting non-AAC audio to `.m4b`/`.m4a`, `--audio-bitrate` defaults to `auto` (probing source bitrate with `ffprobe`, clamped to `64k`–`192k`).

## Configuration
audiobook-dl can be configured via `audiobook-dl.toml` at:
- Windows: `%LOCALAPPDATA%\jo1gi\audiobook-dl\audiobook-dl.toml`
- macOS: `~/Library/Application Support/audiobook-dl/audiobook-dl.toml`
- Linux: `$XDG_CONFIG_DIR/audiobook-dl/audiobook-dl.toml` (defaults to `~/.config/audiobook-dl/audiobook-dl.toml`)

In portable Windows `.exe` mode, placing a `.portable` marker file or `audiobook-dl.toml` beside `audiobook-dl.exe` keeps `audiobook-dl.toml` and `db/` in that same folder instead of `%LOCALAPPDATA%`.

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

### Authentication
Non-secret source settings can be stored in `audiobook-dl.toml`:
```toml
[sources.yourcloudlibrary]
username = "yourusername"
library = "hometown"
```

Cookie files can be specified per source (relative paths are resolved against the configuration directory):
```toml
[sources.everand]
cookie_file = "./everand_cookies.txt"
```

Passwords saved from the GUI are stored in the OS credential store (Windows DPAPI / system keyring) rather than plaintext in `audiobook-dl.toml`. Legacy `password = "..."` entries in `audiobook-dl.toml` are automatically migrated to the OS keystore and scrubbed from the file on load.

## Python Library API
`audiobookdl` also exposes a programmatic client (`Downloader`, `DownloadOptions`, `DownloadSummary`, `DownloadEvent`):
```python
from audiobookdl import Downloader, DownloadOptions

options = DownloadOptions(combine=True, output_format="m4b", resume=True)
downloader = Downloader(options=options, on_event=lambda ev: print(ev.kind, ev.data))
summary = downloader.download("https://librivox.org/...")
```

## Portable Windows Executable
You can build a standalone Windows executable (`dist/audiobook-dl.exe`) using PyInstaller:
```shell
pip install ".[build]"
python scripts/build_exe.py
```

Useful build flags:
- `--onedir`: Build a fast-startup portable folder (`dist/audiobook-dl/`) instead of a single-file `--onefile` binary.
- `--copy-ffmpeg`: Copy `ffmpeg` and `ffprobe` from `PATH` alongside the built executable.
- `--bundle-ffmpeg`: Embed `ffmpeg` and `ffprobe` inside the executable bundle.
- `--no-verify`: Skip the post-build `--self-test` smoke check (enabled by default).

## Contributions
Issues, bug-reports, pull requests, or ideas for features and improvements are **very welcome**.


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

Additional CLI subcommands:
- `audiobook-dl library list|tag|hide|unhide|trash|restore`: Query and manage persistent library items, tags, hidden items, and trash in `downloads.db`.
- `audiobook-dl scan <paths...> [--verify-missing] [--json]`: Discover existing local audiobooks and reconcile `downloads.db`.
- `audiobook-dl export <output.{csv,json,xlsx}> [--query ...]`: Export the library catalog/ledger.
- `audiobook-dl sync [--service ...] [--profile ...] [--watch] [--interval 3600] [--scan-only]`: Poll configured accounts and download new pending titles.

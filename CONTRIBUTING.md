# Contributing to audiobook-dl

Thank you for contributing! `audiobook-dl` focuses on a reliable Tkinter Desktop GUI, shared core download/FFmpeg/library pipeline, and source adapters.

## Development Setup

```shell
git clone https://github.com/jo1gi/audiobook-dl.git
cd audiobook-dl
python -m venv .venv
```

Activate the environment and install editable dev dependencies:

- **Windows (PowerShell)**:
  ```powershell
  .venv\Scripts\Activate.ps1
  python -m pip install -e ".[dev,export,impersonate,dnd]"
  ```
- **macOS / Linux**:
  ```shell
  source .venv/bin/activate
  python -m pip install -e ".[dev,export,impersonate,dnd]"
  ```

Ensure `ffmpeg` and `ffprobe` are available on `PATH` for media merging, chapter detection, and integration tests.

## Launching the Desktop GUI Locally

```shell
python -m audiobookdl
# or via installed entry point:
audiobook-dl
```

## Verification Checks

Run all checks locally before opening a pull request:

```shell
# Lint across package, tests, and build scripts
ruff check audiobookdl tests scripts

# Static type checking (package + build script)
mypy audiobookdl scripts

# Fast unit & integration suite (no external network required)
pytest -m "not e2e" -q

# End-to-end Desktop GUI & media harness tests (requires FFmpeg and display / Xvfb)
pytest -m e2e -v
```

See [docs/testing.md](./docs/testing.md) for marker descriptions (`gui`, `ffmpeg`, `e2e`, `real_integrity`, etc.) and Linux `xvfb-run` tips.

## Architecture & Coding Guidelines

- Options belong declaratively in [audiobookdl/options_schema.py](./audiobookdl/options_schema.py) (`OPTION_REGISTRY` / `CONFIG_OPTION_SPECS`), avoiding manual option plumbing across layers. Keep [docs/configuration.md](./docs/configuration.md) synchronized (`tests/test_docs_sync.py` checks this automatically).
- Source adapters in `audiobookdl/sources/` should stay pure HTTP/metadata adapters returning `Audiobook` / `Series` objects, reusing `Source` base utilities (`get_or_create_device_id`, `fetch_cover_safe`, `get_stream_files`, etc.).
- Every GUI workflow change should handle loading, empty, error, and success states without blocking the Tk main thread (`MainThreadCallbackQueue` + bounded background workers in [audiobookdl/gui/app.py](./audiobookdl/gui/app.py) / [audiobookdl/gui/controller.py](./audiobookdl/gui/controller.py)).
- Avoid storing plaintext secrets in TOML config files; route credentials through `get_credential_store()`.

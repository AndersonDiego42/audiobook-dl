# End-to-End (E2E) Test Suite

This directory provides the reproducible end-to-end testing foundation for `audiobook-dl`, covering real CLI subprocess execution, desktop Tkinter GUI lifecycles, FFmpeg media processing, credential storage, and HTTP/HLS interactions against an isolated local server.

## Running Tests

Run the full test suite (unit, integration, and E2E):
```shell
python -m pytest -q
```

Run only the E2E suite:
```shell
python -m pytest -m e2e -v
```

Run only non-E2E unit/integration tests:
```shell
python -m pytest -m "not e2e" -q
```

On headless Linux systems, wrap GUI tests with `xvfb-run`:
```shell
xvfb-run -a python -m pytest -m e2e -v
```

## Environment Configuration

| Variable | Default | Description |
|---|---|---|
| `AUDIOBOOK_DL_E2E_TARGET` | `module` | `module` runs `python -m audiobookdl`; `executable` targets built binaries |
| `AUDIOBOOK_DL_E2E_CLI_BIN` | _(unset)_ | Path to CLI executable when `AUDIOBOOK_DL_E2E_TARGET=executable` |
| `AUDIOBOOK_DL_E2E_GUI_BIN` | _(unset)_ | Path to GUI executable for `--self-test` when `AUDIOBOOK_DL_E2E_TARGET=executable` |
| `AUDIOBOOK_DL_E2E_FFMPEG_DIR` | _(unset)_ | Optional explicit directory containing `ffmpeg` and `ffprobe` |
| `AUDIOBOOK_DL_E2E_TIMEOUT` | `30.0` | Timeout in seconds for subprocesses and GUI state polling |
| `AUDIOBOOK_DL_E2E_SERVER_HOST` | `127.0.0.1` | Bind host for `LocalAudiobookServer` |
| `AUDIOBOOK_DL_E2E_ARTIFACTS_DIR` | _(unset)_ | Directory to write per-test diagnostic bundles on failure |
| `AUDIOBOOK_DL_E2E_REQUIRE_FFMPEG` | `0` | Fail instead of skipping when `ffmpeg`/`ffprobe` are unavailable |
| `AUDIOBOOK_DL_E2E_REQUIRE_GUI` | `0` | Fail instead of skipping when Tkinter display is unavailable |
| `AUDIOBOOK_DL_E2E_HEADLESS` | auto | Override headless environment detection (`1`/`0`) |

## Architecture & Guarantees

- **Isolation**: Each test runs in an isolated `tmp_path` workspace (`work/`, `output/`, `config/`, `db/`, `home/` with Windows `AppData` and XDG directories), preventing writes to host user configuration or repository directories. Global `audiobookdl.logging` state, `ffmpeg_dir`, and `os.environ` are reset after every test.
- **Readiness Checks**: `LocalAudiobookServer` polls `GET /__health` over TCP before tests begin; `CliRunner` verifies `--version` responsiveness once per session; `GuiHarness` validates Tkinter initialization before interacting with `AudiobookDLApp`; `require_ffmpeg` verifies executable health.
- **Deterministic Data**: `MediaFixtureFactory` generates and session-caches real MP3/AAC-TS audio and JPEG covers via FFmpeg and Pillow without committing binary fixtures or hitting external networks.
- **Failure Diagnostics**: On any E2E failure, `pytest_runtest_makereport` attaches captured subprocess `stdout`/`stderr`, HTTP request/response logs (with auth headers redacted), GUI state snapshots, and workspace file trees to the pytest report, and writes artifact bundles when `AUDIOBOOK_DL_E2E_ARTIFACTS_DIR` is set.

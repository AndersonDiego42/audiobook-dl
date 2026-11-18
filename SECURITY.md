# Security Policy

## Supported Versions

Security fixes target the latest development branch (`master`) and the most recent tagged release, running on Python 3.10+.

## Reporting a Vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities (such as credential leaks, command injection paths, or unsafe file/path handling).

Instead, report vulnerabilities privately to the maintainer:
- Matrix: `jo1gi:matrix.org`
- XMPP: `verdantislet@movim.eu`
- Email: `mail@joakimholm.xyz`

Please include a minimal reproduction, affected platform/version, and impact summary.

## Security Design Notes

- **Credentials & API Keys**: Service passwords, Audiobookshelf API tokens, and custom enrichment keys are persisted via Windows DPAPI (`credentials.dpapi`) or the OS keyring (`credentials.keyring.json`). Plaintext credentials in `audiobook-dl.toml` are automatically migrated and scrubbed on load unless the user explicitly opts into `AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS=1` in a headless container without a keyring.
- **Session Cookies & Device IDs**: Cached Netscape cookie jars and per-device identifiers under `<database_directory>/<service>/` are written atomically with restricted owner-only permissions (`0o600` on POSIX, owner-restricted DACL on Windows).
- **HTTP Transport Warnings**: Sending `Bearer` credentials over unencrypted non-loopback `http://` endpoints emits explicit security warnings for Audiobookshelf and custom enrichment providers.
- **`post_exec` Hook Execution**: Use argument-array hooks (`post_exec = ["cmd", "arg"]` via `shell=False`) and read `AUDIOBOOK_DL_*` environment variables rather than interpolating untrusted titles into shell commands. String-based shell commands require explicit `allow_shell=True` opt-in in `run_post_exec_hook`.

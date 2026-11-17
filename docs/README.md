# audiobook-dl Documentation

Technical documentation for users, operators, and contributors working on `audiobook-dl`.

## Guides

| Document | Audience | Contents |
|---|---|---|
| [Root README](../README.md) | End users & operators | Installation, CLI flags, output templates, TOML configuration, environment variables, subcommands, Docker, and Python API quickstart |
| [Supported Sites & Plugins](./supported_sites.md) | End users & plugin authors | Service capability matrix (cookies, login, search, library sync, ebooks), service key aliases, and `audiobookdl.sources` plugin entry points |
| [System Architecture](./architecture.md) | Contributors & maintainers | Layered package structure, `OPTION_REGISTRY`, `Downloader` / `JobContext` isolation, source adapters, credential backends, and shared `GuiController` |
| [Runtime & Build Workflows](./workflows.md) | Contributors & operators | End-to-end download sequence, offline `pack` flow, headless `sync` & `serve` flows, CI checks, and PyInstaller/Docker release pipelines |
| [Testing Guide](./testing.md) | Contributors & QA | Unit, integration, and E2E test suites, `pytest` markers, `LocalAudiobookServer` harness, E2E environment variables, and linting/type-checking commands |
| [GUI Technical Audit & Roadmap](./gui_audit_and_roadmap.md) | Contributors & maintainers | Prioritized technical analysis of desktop/web GUI bugs, performance bottlenecks, architectural debt, UX enhancements, and implementation steps |

# audiobook-dl Documentation

Technical documentation for users, operators, and contributors working on `audiobook-dl`.

## Guides

| Document | Audience | Contents |
|---|---|---|
| [Root README](../README.md) | End users & operators | Installation, Desktop GUI features, output templates, TOML configuration, environment variables, portable build, and Python API quickstart |
| [Supported Sites & Plugins](./supported_sites.md) | End users & plugin authors | Service capability matrix (cookies, login, search, library sync, ebooks), service key aliases, and `audiobookdl.sources` plugin entry points |
| [System Architecture](./architecture.md) | Contributors & maintainers | Layered package structure, `OPTION_REGISTRY`, `Downloader` / `JobContext` isolation, source adapters, credential backends, and `GuiController` |
| [Runtime & Build Workflows](./workflows.md) | Contributors & operators | End-to-end download sequence, offline `pack` flow, GUI library sync flow, CI checks, and PyInstaller release pipeline |
| [Testing Guide](./testing.md) | Contributors & QA | Unit, integration, and E2E GUI test suites, `pytest` markers, `LocalAudiobookServer` harness, E2E environment variables, and linting/type-checking commands |

# audiobook-dl Documentation

Technical documentation for users, operators, and contributors working on `audiobook-dl`.

## Guides

| Document | Audience | Contents |
|---|---|---|
| [Root README](../README.md) | End users & operators | Installation, Desktop GUI features, output templates, TOML configuration, environment variables, portable build, and Python API quickstart |
| [Configuration Reference](./configuration.md) | End users & operators | Complete schema-backed TOML reference (`CONFIG_OPTION_SPECS`, `[gui]`, `[sources]`, `[audiobookshelf]`, `[enrichment.custom]`) |
| [Supported Sites & Capabilities](./supported_sites.md) | End users & plugin authors | Service capability matrix (cookies, login, search, library sync, ebooks) and service key aliases |
| [Plugin SDK Guide](./plugins.md) | Plugin authors & integrators | Entry-point SDK for `audiobookdl.sources` (`PLUGIN_API_VERSION = 1`, `Source`, `CatalogBook`) and `audiobookdl.enrichment` providers |
| [System Architecture](./architecture.md) | Contributors & maintainers | Layered package structure, `OPTION_REGISTRY`, `Downloader` / `JobContext` isolation, source adapters, credential backends, and `GuiController` |
| [Runtime & Build Workflows](./workflows.md) | Contributors & operators | End-to-end download sequence, offline `pack` flow, GUI library sync flow, CI checks, and PyInstaller release pipeline |
| [Testing Guide](./testing.md) | Contributors & QA | Unit, integration, and E2E GUI test suites, `pytest` markers, `LocalAudiobookServer` harness, E2E environment variables, and linting/type-checking commands |
| [Technical Audit & Improvement Roadmap](./technical-audit.md) | Contributors & maintainers | Prioritized 70-item codebase audit covering Desktop GUI reliability, performance, accessibility, CLI remains cleanup, security, core API/concurrency, testing/CI, and feature roadmap |
| [Contributing Guide](../CONTRIBUTING.md) | Contributors | Local environment setup, GUI launch, lint/type/test checks, and schema/UI conventions |
| [Security Policy](../SECURITY.md) | Users & security reporters | Vulnerability reporting, DPAPI/keyring credential design, cookie permissions, and `post_exec` safety |
| [Changelog](../CHANGELOG.md) | Users & maintainers | Notable feature, pipeline, and security changes |

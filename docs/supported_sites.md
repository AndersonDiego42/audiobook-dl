# Supported Sites

| Site             | Cookies | Login | Search | Library Sync | Ebooks | Notes                                                               |
|:-----------------|:-------:|:-----:|:------:|:------------:|:------:|:--------------------------------------------------------------------|
| Archive.org      |    ✗    |   ✗   |   ✓    |      ✗       |   ✗    | Internet Archive audio catalog & metadata API                       |
| audiobooks.com   |    ✓    |   ✗   |   ✗    |      ✓       |   ✗    | Supports player, audiobook, and library URLs                        |
| Blinkist         |    ✓    |   ✗   |   ✓    |      ✓       |   ✗    | Supports `/books/` and `/nc/reader/` URLs                           |
| BookBeat         |    ✗    |   ✓   |   ✓    |      ✓       |   ✓    | Works with saved books or catalog URLs; supports audiobook/ebook    |
| Chirp            |    ✓    |   ✗   |   ✗    |      ✓       |   ✗    | Supports player and catalog URLs                                    |
| eReolen          |    ✓    |   ✓   |   ✗    |      ✓       |   ✗    | Danish public library portal; HLS streams with chapter extraction   |
| Everand          |    ✓    |   ✗   |   ✓    |      ✓       |   ✗    | Formerly Scribd                                                     |
| Libro.fm         |    ✓    |   ✓   |   ✓    |      ✓       |   ✗    | DRM-free `.m4b` / split `.mp3`; search filters purchased library    |
| Librivox         |    ✗    |   ✗   |   ✓    |      ✗       |   ✗    | Public domain catalog (no user account or personal library)         |
| Nextory          |    ✗    |   ✓   |   ✓    |      ✓       |   ✗    | Multi-profile selection (`--library`), locale routing, catalog URLs |
| O'Reilly         |    ✓    |   ✗   |   ✓    |      ✓       |   ✓    | Audiobooks, video courses (audio or `.mp4`/`.mkv`), and EPUB assembly |
| Overdrive        |    ✓    |   ✗   |   ✗    |      ✓       |   ✗    | Supports `listen.overdrive.com` and Libby loan/shelf URLs           |
| Podimo           |    ✗    |   ✓   |   ✓    |      ✓       |   ✗    | Audiobooks & podcast series; GraphQL catalog search & library sync  |
| RSS              |    ✗    |   ✗   |   ✗    |      ✗       |   ✗    | Generic RSS 2.0 / Atom podcast & serialized audiobook `.xml` feeds  |
| Saxo             |    ✗    |   ✓   |   ✓    |      ✓       |   ✗    | AES-encrypted streams with searchable premium catalog               |
| Storytel         |    ✗    |   ✓   |   ✓    |      ✓       |   ✓    | Full `--media-type` audiobook/ebook/both and bookshelf sync         |
| YourCloudLibrary |    ✓    |   ✓   |   ✗    |      ✓       |   ✗    | Findaway v4 streams + library shelf listing                         |

## Service Keys & Aliases

When passing `--service <name>` on the CLI or configuring `[sources.<name>]` in `audiobook-dl.toml`, the following aliases are automatically normalized to their canonical service key:

- `internet archive` -> `archive.org`
- `audiobooksdotcom` -> `audiobooks.com`
- `scribd` -> `everand`
- `librofm` -> `libro.fm`
- `libby` -> `overdrive`
- `mofibo` -> `storytel`
- `o'reilly` / `oreillylearning` -> `oreilly`
- `cloudlibrary` -> `yourcloudlibrary`

## Third-Party Source Plugins

External Python packages can register custom `Source` subclasses via standard `importlib.metadata` entry points under the `audiobookdl.sources` group in `pyproject.toml`:

```toml
[project.entry-points."audiobookdl.sources"]
my_service = "my_package.source:MyServiceSource"
```

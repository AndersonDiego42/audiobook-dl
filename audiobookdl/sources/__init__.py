from .source import Source

from ..exceptions import NoSourceFound
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import functools
import importlib
import importlib.metadata
import re
import sys
import threading
from typing import Any, cast


@dataclass(frozen=True)
class SourceDescriptor:
    """Lightweight static metadata for a built-in `Source` allowing lazy module import."""

    key: str
    module: str
    class_name: str
    names: tuple[str, ...]
    match: tuple[str, ...]
    auth_methods: tuple[str, ...] = ()
    login_fields: tuple[str, ...] = ("username", "password")
    supports_search: bool = False
    supports_library: bool = False

    def load(self) -> type[Source]:
        mod = importlib.import_module(self.module)
        cls = getattr(mod, self.class_name)
        return cast(type[Source], cls)


_BUILTIN_SOURCE_DESCRIPTORS: tuple[SourceDescriptor, ...] = (
    SourceDescriptor(
        key="archive.org",
        module="audiobookdl.sources.archiveorg",
        class_name="ArchiveOrgSource",
        names=("Archive.org", "Internet Archive"),
        match=(r"https?://(?:www\.)?archive\.org/(?:details|metadata|download)/([^/?#]+)",),
        auth_methods=(),
        supports_search=True,
        supports_library=False,
    ),
    SourceDescriptor(
        key="audiobooks.com",
        module="audiobookdl.sources.audiobooksdotcom",
        class_name="AudiobooksdotcomSource",
        names=("audiobooks.com",),
        match=(
            r"https://www.audiobooks.com/book/stream/\d+(/\d+)?/?",
            r"https?://(www\.)?audiobooks\.com/audiobook/.+",
            r"https?://(www\.)?audiobooks\.com/browse/library.*",
        ),
        auth_methods=("cookies",),
        supports_search=False,
        supports_library=True,
    ),
    SourceDescriptor(
        key="blinkist",
        module="audiobookdl.sources.blinkist",
        class_name="BlinkistSource",
        names=("Blinkist",),
        match=(r"https?://(www\.)?blinkist\.com/([a-z]{2}(?:-[a-z]{2})?/)?(nc/reader|books)/.+",),
        auth_methods=("cookies",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="bookbeat",
        module="audiobookdl.sources.bookbeat",
        class_name="BookBeatSource",
        names=("BookBeat",),
        match=(r"https?://(?:www\.)?bookbeat\..+",),
        auth_methods=("login",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="chirp",
        module="audiobookdl.sources.chirp",
        class_name="ChirpSource",
        names=("Chirp",),
        match=(
            r"https?://(www\.)?chirpbooks\.com/player/\d+",
            r"https?://(www\.)?chirpbooks\.com/audiobooks/[^/?#]+",
        ),
        auth_methods=("cookies",),
        supports_search=False,
        supports_library=True,
    ),
    SourceDescriptor(
        key="ereolen",
        module="audiobookdl.sources.ereolen",
        class_name="EreolenSource",
        names=("eReolen",),
        match=(r"https://(www\.)?ereolen\.dk/work/work-of:.+",),
        auth_methods=("cookies", "login"),
        supports_search=False,
        supports_library=True,
    ),
    SourceDescriptor(
        key="libro.fm",
        module="audiobookdl.sources.librofm",
        class_name="LibrofmSource",
        names=("Libro.fm", "Librofm"),
        match=(r"https?://(?:www\.)?libro\.fm/audiobooks/([^/?#]+)",),
        auth_methods=("login", "cookies"),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="librivox",
        module="audiobookdl.sources.librivox",
        class_name="LibrivoxSource",
        names=("Librivox",),
        match=(r"https?://(?:www\.)?librivox\.org/.+",),
        auth_methods=(),
        supports_search=True,
        supports_library=False,
    ),
    SourceDescriptor(
        key="nextory",
        module="audiobookdl.sources.nextory",
        class_name="NextorySource",
        names=("Nextory",),
        match=(r"https?://((www|catalog-\w\w)\.)?nextory.+",),
        auth_methods=("login",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="oreilly",
        module="audiobookdl.sources.oreilly",
        class_name="OReillySource",
        names=("OReilly", "O'Reilly"),
        match=(
            r"https?://(?:www\.)?learning\.oreilly\.com/library/view/[^/]+/[^/?#]+",
            r"https?://(?:www\.)?learning\.oreilly\.com/audiobooks/(?:[^/]+/)?[^/?#]+",
            r"https?://(?:www\.)?learning\.oreilly\.com/videos/(?:[^/]+/)?[^/?#]+",
            r"https?://(?:www\.)?learning\.oreilly\.com/course/(?:[^/]+/)?[^/?#]+",
            r"https?://(?:www\.)?learning\.oreilly\.com/api/v2/epubs/urn:orm:[^/?#]+",
            r"https?://(?:www\.)?learning\.oreilly\.com/api/v2/metadata/urn:orm:[^/?#]+",
        ),
        auth_methods=("cookies",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="overdrive",
        module="audiobookdl.sources.overdrive",
        class_name="OverdriveSource",
        names=("Overdrive", "Libby"),
        match=(
            r"https?://.+\.listen\.overdrive\.com.*",
            r"https?://(www\.)?libbyapp\.com/(open/loan|shelf)/.*",
        ),
        auth_methods=("cookies",),
        supports_search=False,
        supports_library=True,
    ),
    SourceDescriptor(
        key="podimo",
        module="audiobookdl.sources.podimo",
        class_name="PodimoSource",
        names=("Podimo",),
        match=(
            r"https://open.podimo.com/audiobook/[^/]+",
            r"https://open.podimo.com/podcast/[^/]+",
            r"https://share.podimo.com/s/[^/]+",
        ),
        auth_methods=("login",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="saxo",
        module="audiobookdl.sources.saxo",
        class_name="SaxoSource",
        names=("Saxo",),
        match=(r"https?://(?:www\.)?saxo\.(?:com|dk)/[^/]+/.+",),
        auth_methods=("login",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="everand",
        module="audiobookdl.sources.everand",
        class_name="EverandSource",
        names=("Everand", "Scribd"),
        match=(
            r"https?://(www\.)?(scribd|everand)\.com/listen/\d+",
            r"https?://(www\.)?(scribd|everand)\.com/audiobook/\d+(/.*)?",
            r"https?://(www\.)?(scribd|everand)\.com/series/\d+",
            r"https?://(www\.)?(scribd|everand)\.com/book/\d+(/.*)?",
        ),
        auth_methods=("cookies",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="storytel",
        module="audiobookdl.sources.storytel",
        class_name="StorytelSource",
        names=("Storytel", "Mofibo"),
        match=(
            r"https?://(?:www\.)?(?:storytel|mofibo)\.com/(?:(?P<language>\w+)(?:/(?P<language2>\w+))?/)?(?P<list_type>(?:books|series|authors|narrators|publishers|categories))/.+",
            r"https?://(?:www\.)?(?:storytel|mofibo)\.com/\w+(?:/\w+)?/(?:want-to-read|bookshelf)/?$",
        ),
        auth_methods=("login",),
        supports_search=True,
        supports_library=True,
    ),
    SourceDescriptor(
        key="yourcloudlibrary",
        module="audiobookdl.sources.yourcloudlibrary",
        class_name="YourCloudLibrarySource",
        names=("YourCloudLibrary",),
        match=(
            r"https?://audio\.yourcloudlibrary\.com/listen/.+",
            r"https?://ebook\.yourcloudlibrary\.com/library/[^/]+/detail/.+",
            r"https?://ebook\.yourcloudlibrary\.com/library/[^/]+/(audiobook|title)/.+",
        ),
        auth_methods=("cookies", "login"),
        login_fields=("username", "password", "library"),
        supports_search=False,
        supports_library=True,
    ),
    SourceDescriptor(
        key="rss",
        module="audiobookdl.sources.rss",
        class_name="RssSource",
        names=("RSS",),
        match=(
            r"https?://[^?#]+\.(?:rss|xml|atom)(?:[?#].*)?$",
            r"https?://[^?#]+/(?:feed|rss|atom)/?(?:[?#].*)?$",
            r"https?://[^/]+/[^?#]*[?&](?:format|alt|feed)=(?:rss|atom)(?:&|#|$)",
            r"https?://(?:[^/]+\.)?substack\.com/feed/?(?:[?#].*)?$",
            r"https?://feeds\.feedburner\.com/[^?#]+",
            r"https?://feeds\.libsyn\.com/[^?#]+",
            r"https?://feeds\.(?:megaphone\.fm|simplecast\.com|buzzsprout\.com|transistor\.fm|captivate\.fm|acast\.com|podbean\.com)/[^?#]+",
        ),
        auth_methods=(),
        supports_search=False,
        supports_library=False,
    ),
    SourceDescriptor(
        key="youtubemusic",
        module="audiobookdl.sources.youtubemusic",
        class_name="YouTubeMusicSource",
        names=("YouTubeMusic", "YouTube Music"),
        match=(
            r"https?://music\.youtube\.com/watch\?(?:[^#]*&)?v=[^&#]+",
            r"https?://music\.youtube\.com/playlist\?(?:[^#]*&)?list=[^&#]+",
            r"https?://music\.youtube\.com/browse/(?:MPREb_|VL|MPSP)[^/?#]+",
            r"https?://music\.youtube\.com/podcast/[^/?#]+",
        ),
        auth_methods=("cookies",),
        supports_search=False,
        supports_library=False,
    ),
)

_DESCRIPTORS_BY_ALIAS: dict[str, SourceDescriptor] = {
    alias.lower(): desc
    for desc in _BUILTIN_SOURCE_DESCRIPTORS
    for alias in desc.names
}
_CLASS_EXPORTS: dict[str, SourceDescriptor] = {
    desc.class_name: desc for desc in _BUILTIN_SOURCE_DESCRIPTORS
}


@functools.lru_cache(maxsize=1)
def _compiled_descriptor_patterns() -> tuple[tuple[SourceDescriptor, tuple[re.Pattern[str], ...]], ...]:
    return tuple(
        (desc, tuple(re.compile(pat) for pat in desc.match))
        for desc in _BUILTIN_SOURCE_DESCRIPTORS
    )


@functools.lru_cache(maxsize=1)
def _ereolen_static_patterns() -> tuple[re.Pattern[str], ...]:
    from audiobookdl import utils

    domains = [
        line.strip()
        for line in utils.read_asset_file("assets/sources/ereolen/libraries.txt").splitlines()
        if line.strip()
    ]
    if not domains:
        return ()
    return (
        re.compile(
            rf"https://(www\.)?({'|'.join(re.escape(d) for d in domains)})/work/work-of:.+"
        ),
    )


@dataclass(frozen=True)
class ServiceInfo:
    """Metadata describing a supported audiobook service."""
    key: str
    label: str
    auth_methods: tuple[str, ...]
    login_fields: tuple[str, ...]
    supports_search: bool = False
    supports_library: bool = False

    @property
    def supports_oauth(self) -> bool:
        """Return `True` when this service declares OAuth authentication."""
        return "oauth" in self.auth_methods


def normalize_descriptor_source_key(raw_key: str) -> str:
    """Resolve canonical source key from static descriptors or plugins without importing built-ins."""
    lowered = raw_key.strip().lower()
    desc = _DESCRIPTORS_BY_ALIAS.get(lowered)
    if desc is not None:
        return desc.key
    for cls in _discover_plugin_sources():
        if cls.names:
            canonical = cls.names[0].lower()
            for alias in cls.names:
                if alias.lower() == lowered:
                    return canonical
    return lowered


def get_available_services() -> list[ServiceInfo]:
    """Return sorted metadata for all supported audiobook services without eager source imports."""
    services = [
        ServiceInfo(
            key=desc.key,
            label=" / ".join(desc.names),
            auth_methods=desc.auth_methods,
            login_fields=desc.login_fields,
            supports_search=desc.supports_search,
            supports_library=desc.supports_library,
        )
        for desc in _BUILTIN_SOURCE_DESCRIPTORS
    ]
    seen_keys = {svc.key for svc in services}
    for cls in _discover_plugin_sources():
        if not cls.names:
            continue
        key = cls.names[0].lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        services.append(
            ServiceInfo(
                key=key,
                label=" / ".join(cls.names),
                auth_methods=tuple(cls._authentication_methods),
                login_fields=tuple(cls.login_data),
                supports_search=bool(getattr(cls, "supports_search", False)),
                supports_library=bool(getattr(cls, "supports_library", False)),
            )
        )
    return sorted(services, key=lambda s: s.label.lower())


def find_source_by_name(service_name: str) -> type[Source]:
    """Resolve a service class by canonical name or configured alias."""
    from audiobookdl.config import normalize_source_key

    target = normalize_source_key(service_name)
    if get_source_classes is _DEFAULT_GET_SOURCE_CLASSES:
        desc = _DESCRIPTORS_BY_ALIAS.get(target)
        if desc is not None:
            return desc.load()
        for cls in _discover_plugin_sources():
            for name in cls.names:
                if name.lower() == target:
                    return cls
        raise NoSourceFound

    for cls in get_source_classes():
        for name in cls.names:
            if name.lower() == target:
                return cls
    raise NoSourceFound


def detect_service_key(url: str) -> str | None:
    """Return the canonical service key matching `url`, or `None` if unrecognized."""
    candidate = url.strip()
    if not candidate:
        return None
    if not (candidate.startswith("http://") or candidate.startswith("https://")):
        candidate = f"https://{candidate}"
    if get_source_classes is _DEFAULT_GET_SOURCE_CLASSES:
        for desc, patterns in _compiled_descriptor_patterns():
            for pat in patterns:
                if pat.match(candidate) is not None:
                    return desc.key
        if _EREOLEN_WORK_URL_RE.match(candidate):
            for pat in _ereolen_static_patterns():
                if pat.match(candidate) is not None:
                    return "ereolen"
        try:
            source_cls = find_compatible_source(candidate)
        except NoSourceFound:
            return None
        return source_cls.names[0].lower() if source_cls.names else None
    try:
        source_cls = find_compatible_source(candidate)
    except NoSourceFound:
        return None
    return source_cls.names[0].lower() if source_cls.names else None


def resolve_service_label(
    url: str,
    services_by_key: Mapping[str, ServiceInfo] | None = None,
) -> str:
    """Return a human-readable service label for `url`, or `'Unknown'`."""
    key = detect_service_key(url)
    if key and services_by_key and key in services_by_key:
        return services_by_key[key].label
    return key or "Unknown"


_EREOLEN_REFRESH_LOCK = threading.Lock()
_EREOLEN_REFRESHED = False
_EREOLEN_WORK_URL_RE = re.compile(r"^https?://[^/]+\.(?:dk|fo|gl)/work/work-of:", re.IGNORECASE)


def _refresh_ereolen_domains_once() -> None:
    """Refresh dynamic Danish library domains on first unrecognized `.dk`/`.fo`/`.gl` work URL."""
    global _EREOLEN_REFRESHED
    with _EREOLEN_REFRESH_LOCK:
        if _EREOLEN_REFRESHED:
            return
        _EREOLEN_REFRESHED = True
        try:
            from .ereolen import EreolenSource

            EreolenSource.refresh_library_domains()
        except Exception:
            pass


def find_compatible_source(url: str) -> type[Source]:
    """Finds the first source that supports the given url"""
    if get_source_classes is not _DEFAULT_GET_SOURCE_CLASSES:
        for source in get_source_classes():
            for m in source.match:
                if re.match(m, url) is not None:
                    return source
        if _EREOLEN_WORK_URL_RE.match(url):
            from .ereolen import EreolenSource

            _refresh_ereolen_domains_once()
            for m in EreolenSource.match:
                if re.match(m, url) is not None:
                    return EreolenSource
        raise NoSourceFound

    for desc, patterns in _compiled_descriptor_patterns():
        for pat in patterns:
            if pat.match(url) is not None:
                return desc.load()

    if _EREOLEN_WORK_URL_RE.match(url):
        ereolen_mod = sys.modules.get("audiobookdl.sources.ereolen")
        if ereolen_mod is not None:
            ereolen_cls = cast(type[Source], ereolen_mod.EreolenSource)
            for m in ereolen_cls.match:
                if re.match(m, url) is not None:
                    return ereolen_cls
        else:
            for pat in _ereolen_static_patterns():
                if pat.match(url) is not None:
                    return _DESCRIPTORS_BY_ALIAS["ereolen"].load()

    for plugin_cls in _discover_plugin_sources():
        for m in plugin_cls.match:
            if re.match(m, url) is not None:
                return plugin_cls

    if _EREOLEN_WORK_URL_RE.match(url):
        from .ereolen import EreolenSource

        _refresh_ereolen_domains_once()
        for m in EreolenSource.match:
            if re.match(m, url) is not None:
                return EreolenSource
    raise NoSourceFound


PLUGIN_API_VERSION = 1


def reset_plugin_sources_cache() -> None:
    """Clear cached third-party entry-point source discovery results."""
    _discover_plugin_sources.cache_clear()


@functools.lru_cache(maxsize=1)
def _discover_plugin_sources() -> list[type[Source]]:
    """Discover third-party `Source` subclasses via the `audiobookdl.sources` entry point group."""
    from audiobookdl import logging

    discovered: list[type[Source]] = []
    try:
        eps = importlib.metadata.entry_points(group="audiobookdl.sources")
    except Exception as exc:
        logging.debug(f"Failed querying source entry points: {exc}")
        return discovered

    known_names = {
        str(name).lower()
        for desc in _BUILTIN_SOURCE_DESCRIPTORS
        for name in desc.names
    }
    for ep in eps:
        ep_name = getattr(ep, "name", ep)
        try:
            cls = ep.load()
        except Exception as exc:
            logging.debug(f"Failed loading source plugin '{ep_name}': {exc}")
            continue
        if not (
            isinstance(cls, type)
            and issubclass(cls, Source)
            and cls is not Source
            and getattr(cls, "names", None)
            and getattr(cls, "match", None)
        ):
            logging.debug(f"Ignoring invalid source plugin entry point '{ep_name}': {cls!r}")
            continue
        api_version = getattr(cls, "plugin_api_version", PLUGIN_API_VERSION)
        if api_version != PLUGIN_API_VERSION:
            logging.debug(
                f"Ignoring source plugin '{ep_name}' with incompatible plugin_api_version={api_version!r} (expected {PLUGIN_API_VERSION})"
            )
            continue
        plugin_names = [str(name).strip().lower() for name in cls.names if str(name).strip()]
        if not plugin_names:
            logging.debug(f"Ignoring source plugin '{ep_name}' with empty service names")
            continue
        collisions = [name for name in plugin_names if name in known_names]
        if collisions:
            logging.debug(
                f"Ignoring source plugin '{ep_name}' due to service name collision: {collisions}"
            )
            continue
        known_names.update(plugin_names)
        discovered.append(cls)
    return discovered


def get_source_classes() -> list[type[Source]]:
    """Returns a list of all built-in and entry-point plugin sources."""
    classes: list[type[Source]] = [desc.load() for desc in _BUILTIN_SOURCE_DESCRIPTORS]
    seen = set(classes)
    for plugin_cls in _discover_plugin_sources():
        if plugin_cls not in seen:
            seen.add(plugin_cls)
            classes.append(plugin_cls)
    return classes


_DEFAULT_GET_SOURCE_CLASSES = get_source_classes


def get_source_names() -> Iterable[str]:
    """
    Returns the names of all sources available
    There are sometimes multiple names for the same source
    """
    results: list[str] = []
    for desc in _BUILTIN_SOURCE_DESCRIPTORS:
        for source_name in desc.names:
            results.append(source_name)
    for source in _discover_plugin_sources():
        for source_name in source.names:
            results.append(source_name)
    return sorted(results, key=lambda x: x.lower())


def __getattr__(name: str) -> Any:
    if name == "_BUILTIN_SOURCES":
        return tuple(desc.load() for desc in _BUILTIN_SOURCE_DESCRIPTORS)
    desc = _CLASS_EXPORTS.get(name)
    if desc is not None:
        return desc.load()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

from .source import Source

from .audiobooksdotcom import AudiobooksdotcomSource
from .blinkist import BlinkistSource
from .bookbeat import BookBeatSource
from .chirp import ChirpSource
from .ereolen import EreolenSource
from .librivox import LibrivoxSource
from .nextory import NextorySource
from .overdrive import OverdriveSource
from .podimo import PodimoSource
from .saxo import SaxoSource
from .everand import EverandSource
from .storytel import StorytelSource
from .yourcloudlibrary import YourCloudLibrarySource

from ..exceptions import NoSourceFound
from dataclasses import dataclass
import importlib.metadata
import re
from typing import Iterable, List, Optional, Tuple, Type


@dataclass(frozen=True)
class ServiceInfo:
    """Metadata describing a supported audiobook service."""
    key: str
    label: str
    auth_methods: Tuple[str, ...]
    login_fields: Tuple[str, ...]
    supports_search: bool = False
    supports_library: bool = False


def get_available_services() -> List[ServiceInfo]:
    """Return sorted metadata for all supported audiobook services."""
    services = [
        ServiceInfo(
            key=cls.names[0].lower(),
            label=" / ".join(cls.names),
            auth_methods=tuple(cls._authentication_methods),
            login_fields=tuple(cls.login_data),
            supports_search=bool(getattr(cls, "supports_search", False)),
            supports_library=bool(getattr(cls, "supports_library", False)),
        )
        for cls in get_source_classes()
        if cls.names
    ]
    return sorted(services, key=lambda s: s.label.lower())


def find_source_by_name(service_name: str) -> Type[Source]:
    """Resolve a service class by canonical name or configured alias."""
    from audiobookdl.config import normalize_source_key

    target = normalize_source_key(service_name)
    for cls in get_source_classes():
        for name in cls.names:
            if name.lower() == target:
                return cls
    raise NoSourceFound


def detect_service_key(url: str) -> Optional[str]:
    """Return the canonical service key matching `url`, or `None` if unrecognized."""
    candidate = url.strip()
    if not candidate:
        return None
    if not (candidate.startswith("http://") or candidate.startswith("https://")):
        candidate = f"https://{candidate}"
    try:
        source_cls = find_compatible_source(candidate)
    except NoSourceFound:
        return None
    return source_cls.names[0].lower() if source_cls.names else None

def find_compatible_source(url: str) -> Type[Source]:
    """Finds the first source that supports the given url"""
    sources = get_source_classes()
    for source in sources:
        for n, m in enumerate(source.match):
            if not re.match(m, url) is None:
                return source
    raise NoSourceFound


_BUILTIN_SOURCES: Tuple[Type[Source], ...] = (
    AudiobooksdotcomSource,
    BlinkistSource,
    BookBeatSource,
    ChirpSource,
    EreolenSource,
    LibrivoxSource,
    NextorySource,
    OverdriveSource,
    PodimoSource,
    SaxoSource,
    EverandSource,
    StorytelSource,
    YourCloudLibrarySource,
)


def _discover_plugin_sources() -> List[Type[Source]]:
    """Discover third-party `Source` subclasses via the `audiobookdl.sources` entry point group."""
    from audiobookdl import logging

    discovered: List[Type[Source]] = []
    try:
        eps = importlib.metadata.entry_points(group="audiobookdl.sources")
    except Exception as exc:
        logging.debug(f"Failed querying source entry points: {exc}")
        return discovered

    for ep in eps:
        try:
            cls = ep.load()
        except Exception as exc:
            logging.debug(f"Failed loading source plugin '{getattr(ep, 'name', ep)}': {exc}")
            continue
        if (
            isinstance(cls, type)
            and issubclass(cls, Source)
            and cls is not Source
            and getattr(cls, "names", None)
            and getattr(cls, "match", None)
        ):
            discovered.append(cls)
        else:
            logging.debug(f"Ignoring invalid source plugin entry point '{getattr(ep, 'name', ep)}': {cls!r}")
    return discovered


def get_source_classes() -> List[Type[Source]]:
    """Returns a list of all built-in and entry-point plugin sources."""
    classes = list(_BUILTIN_SOURCES)
    seen = set(classes)
    for plugin_cls in _discover_plugin_sources():
        if plugin_cls not in seen:
            seen.add(plugin_cls)
            classes.append(plugin_cls)
    return classes


def get_source_names() -> Iterable[str]:
    """
    Returns the names of all sources available
    There are sometimes multiple names for the same source
    """
    results: List[str] = []
    for source in get_source_classes():
        for source_name in source.names:
            results.append(source_name)
    return sorted(results, key=lambda x: x.lower())

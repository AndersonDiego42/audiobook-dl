from dataclasses import dataclass, field
import os
import sys
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import requests

from . import __version__, args, logging, output
from .args import DownloadOptions
from .config import Config, SourceConfig, load_config
from .options_schema import MERGEABLE_OPTION_SPECS
from .exceptions import (
    AudiobookDLException,
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotReleased,
    DownloadCancelled,
    GenericAudiobookDLException,
    NoSourceFound,
)
from .output.download import download
from .sources import find_compatible_source, find_source_by_name
from .sources.source import CatalogBook, Source
from .utils import set_ffmpeg_dir
from .utils.archive import DownloadArchive, default_archive_path, make_book_key
from .utils.audiobook import Audiobook, BookId, Result, Series, is_ebook
from .utils.enrichment import (
    EnrichmentCandidate,
    EnrichmentResult,
    enrich_audiobook_metadata,
    parse_provider_list,
    resolve_redirect_url,
)


@dataclass
class DownloadSummary:
    """Outcome summary returned by `Downloader.download` and `run_downloads`."""

    completed: int = 0
    failed: int = 0
    cancelled: bool = False
    books_completed: int = field(default=0, compare=False)
    books_skipped: int = field(default=0, compare=False)
    books_failed: int = field(default=0, compare=False)
    book_errors: List[Tuple[str, str]] = field(default_factory=list, compare=False)


@dataclass(frozen=True)
class DownloadEvent:
    """Structured log or progress event emitted by `Downloader`."""

    kind: str
    data: Dict[str, Any]


EventCallback = Callable[[DownloadEvent], None]


class _EventLogSink:
    """`LogSink` implementation forwarding structured events to an `on_event` callback."""

    def __init__(self, on_event: EventCallback) -> None:
        self._on_event = on_event

    def _emit(self, kind: str, **data: Any) -> None:
        self._on_event(DownloadEvent(kind=kind, data=data))

    def on_log(self, level: str, message: str) -> None:
        self._emit("log", level=level, message=message)

    def on_progress_start(self, description: str, total: float) -> None:
        self._emit("progress_start", description=description, total=total)

    def on_progress_advance(self, completed: float, total: float) -> None:
        self._emit("progress_advance", completed=completed, total=total)

    def on_progress_metrics(
        self,
        completed: float,
        total: float,
        bytes_downloaded: int,
        speed_bps: float,
        elapsed_seconds: float,
        eta_seconds: Optional[float],
    ) -> None:
        self._emit(
            "progress_metrics",
            completed=completed,
            total=total,
            bytes_downloaded=bytes_downloaded,
            speed_bps=speed_bps,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta_seconds,
        )

    def on_item_progress(self, current: int, total: int, title: str) -> None:
        self._emit("item_progress", current=current, total=total, title=title)

    def on_progress_finish(self) -> None:
        self._emit("progress_finish")


def apply_runtime_options(
    options: DownloadOptions,
    config: Config,
) -> logging.JobContext:
    """Merge config defaults into `options` and build an isolated `JobContext`."""
    explicit = getattr(options, "_explicit_fields", frozenset())
    preset_requested = "output_template" not in explicit and bool(getattr(options, "output_preset", None))
    if preset_requested:
        preset_tpl = output.OUTPUT_TEMPLATE_PRESETS.get(options.output_preset or "")
        if preset_tpl:
            options.output_template = preset_tpl

    for spec in MERGEABLE_OPTION_SPECS:
        if spec.name == "output_template" and preset_requested:
            continue
        if spec.name in explicit:
            continue
        cfg_key = spec.config_key or spec.name
        cfg_value = getattr(config, cfg_key, None)
        if spec.merge_require_truthy:
            if not cfg_value:
                continue
        elif cfg_value is None:
            continue
        if spec.merge_only_if_default and getattr(options, spec.name) != spec.resolve_default():
            continue
        setattr(
            options,
            spec.name,
            spec.merge_cast(cfg_value) if spec.merge_cast is not None else cfg_value,
        )

    if config.ffmpeg_dir:
        set_ffmpeg_dir(config.ffmpeg_dir)
    ffmpeg_out = options.ffmpeg_output or options.debug
    return logging.JobContext(
        debug=options.debug,
        quiet=options.quiet,
        ffmpeg_output=ffmpeg_out,
    )


def _pause_between_books(delay: float, cancel_event: Optional[threading.Event]) -> None:
    if delay <= 0:
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise DownloadCancelled
    else:
        time.sleep(delay)


class Downloader:
    """
    Programmatic client for authenticating, fetching metadata, and downloading audiobooks.

    All operations run inside an isolated `logging.JobContext`, leaving global
    `audiobookdl.logging` flags and sinks untouched.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        options: Optional[DownloadOptions] = None,
        *,
        on_event: Optional[EventCallback] = None,
        sink: Optional[logging.LogSink] = None,
        interaction: Optional[logging.InteractionHandler] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if sink is not None and on_event is not None:
            raise ValueError("Specify either 'sink' or 'on_event', not both")
        self.options: DownloadOptions = (
            options if options is not None else args.build_default_options()
        )
        self.config: Config = (
            config if config is not None else load_config(self.options.config_location)
        )
        self.interaction: logging.InteractionHandler = (
            interaction if interaction is not None else logging.CliInteractionHandler()
        )
        self.cancel_event: Optional[threading.Event] = cancel_event
        if sink is not None:
            self.sink: Optional[logging.LogSink] = sink
        elif on_event is not None:
            self.sink = _EventLogSink(on_event)
        else:
            self.sink = None
        self._http_session = requests.Session()
        self._http_session_finalizer = weakref.finalize(self, self._http_session.close)

    def close(self) -> None:
        """Close the shared HTTP session held by this `Downloader`."""
        self._http_session_finalizer()

    def __enter__(self) -> "Downloader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _build_context(self) -> logging.JobContext:
        ctx = apply_runtime_options(self.options, self.config)
        ctx.interaction = self.interaction
        ctx.cancel_event = self.cancel_event
        ctx.sink = self.sink if self.sink is not None else logging.get_ui_sink()
        return ctx

    def prepare(self, url: str) -> Tuple[Source, Result]:
        """Resolve source for `url`, authenticate if needed, and fetch `Result`."""
        with logging.job_context(self._build_context()):
            return prepare_source_result(
                url,
                self.options,
                self.config,
                interaction=self.interaction,
                cancel_event=self.cancel_event,
                session=self._http_session,
            )

    def fetch_metadata(self, url: str) -> Result:
        """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
        _, result = self.prepare(url)
        return result

    def download_result(
        self,
        source: Source,
        result: Result,
        *,
        selected_indices: Optional[Sequence[int]] = None,
    ) -> None:
        """Download an already-resolved `Audiobook` or `Series` `Result`."""
        with logging.job_context(self._build_context()):
            download_result(
                source,
                result,
                self.options,
                interaction=self.interaction,
                cancel_event=self.cancel_event,
                selected_indices=selected_indices,
            )

    def download(
        self,
        urls: Union[str, Sequence[str]],
        *,
        selected_indices: Optional[Sequence[int]] = None,
        prefetched: Optional[Tuple[Source, Result]] = None,
    ) -> DownloadSummary:
        """Download one URL or a sequence of URLs and return a `DownloadSummary`."""
        url_list: Sequence[str] = [urls] if isinstance(urls, str) else urls
        ctx = self._build_context()
        summary = DownloadSummary()
        with logging.job_context(ctx):
            logging.debug(f"audiobook-dl {__version__}", remove_styling=True)
            logging.debug(f"python {sys.version}", remove_styling=True)
            for index, url in enumerate(url_list):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    summary.cancelled = True
                    break
                if index > 0 and self.options.book_delay > 0:
                    try:
                        _pause_between_books(self.options.book_delay, self.cancel_event)
                    except DownloadCancelled:
                        summary.cancelled = True
                        logging.log("Download cancelled")
                        break
                try:
                    extra_kwargs: Dict[str, Any] = {}
                    if index == 0:
                        if selected_indices is not None:
                            extra_kwargs["selected_indices"] = selected_indices
                        if prefetched is not None:
                            extra_kwargs["prefetched"] = prefetched
                    before_failed = summary.books_failed
                    before_completed = summary.books_completed
                    before_skipped = summary.books_skipped
                    process_url(
                        url,
                        self.options,
                        self.config,
                        interaction=self.interaction,
                        cancel_event=self.cancel_event,
                        summary=summary,
                        session=self._http_session,
                        **extra_kwargs,
                    )
                    if (
                        summary.books_failed > before_failed
                        and summary.books_completed == before_completed
                        and summary.books_skipped == before_skipped
                    ):
                        summary.failed += 1
                    else:
                        summary.completed += 1
                except (DownloadCancelled, KeyboardInterrupt):
                    summary.cancelled = True
                    logging.log("Download cancelled")
                    break
                except AudiobookDLException as e:
                    summary.failed += 1
                    summary.book_errors.append((url, e.error_description))
                    e.print()
                    if logging.is_debug_mode():
                        logging.print_traceback()
        return summary


def run_downloads(
    urls: Sequence[str],
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    sink: Optional[logging.LogSink] = None,
    selected_indices: Optional[Sequence[int]] = None,
    prefetched: Optional[Tuple[Source, Result]] = None,
) -> DownloadSummary:
    """Run audiobook downloads for `urls` with isolated per-job context."""
    return Downloader(
        config=config,
        options=options,
        sink=sink,
        interaction=interaction,
        cancel_event=cancel_event,
    ).download(
        urls,
        selected_indices=selected_indices,
        prefetched=prefetched,
    )


def prepare_source_result(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    session: Optional[requests.Session] = None,
) -> Tuple[Source, Result]:
    """
    Resolve source for `url`, authenticate if required, and fetch `Result`
    without downloading audio files.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"
    logging.log("Finding compatible source")
    try:
        source_class = find_compatible_source(url)
    except NoSourceFound:
        resolved_url = resolve_redirect_url(
            url,
            session=session,
            proxy=getattr(options, "proxy", None),
            timeout=getattr(options, "timeout", None) or 10.0,
        )
        if resolved_url == url:
            raise
        logging.debug(f"Resolved short URL '{url}' -> '{resolved_url}'")
        url = resolved_url
        source_class = find_compatible_source(url)
    source = source_class(options)
    handler = interaction or logging.CliInteractionHandler()
    if source.requires_authentication and not source.authenticated:
        authenticate(url, source, options, config, interaction=handler)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled
    logging.debug(f"Downloading result of [underline]{url}")
    result = source.download(url)
    return source, result


def fetch_metadata(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Result:
    """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
    _, result = prepare_source_result(
        url,
        options,
        config,
        interaction=interaction,
        cancel_event=cancel_event,
    )
    return result


def print_result_info(
    source: Source,
    result: Result,
    options: DownloadOptions,
    *,
    selected_indices: Optional[Sequence[int]] = None,
) -> None:
    """Display parsed metadata and resolved output path(s) without downloading media."""
    logging.log(f"[magenta]{source.name}[/] info:")
    if isinstance(result, Audiobook):
        if not result.metadata.source:
            result.metadata.source = source.name
        location = output.gen_output_location(
            options.output_template,
            result.metadata,
            options.remove_chars,
        )
        authors = ", ".join(result.metadata.authors) or "Unknown"
        narrators = ", ".join(result.metadata.narrators)
        raw_ext = result.files[0].ext if result.files else "unknown"
        fmt = raw_ext if is_ebook(result) else (options.output_format or raw_ext)
        logging.log(f"  Title:    {result.title}")
        logging.log(f"  Authors:  {authors}")
        if narrators:
            logging.log(f"  Narrator: {narrators}")
        if result.metadata.series:
            order = f" #{result.metadata.series_order}" if result.metadata.series_order else ""
            logging.log(f"  Series:   {result.metadata.series}{order}")
        if result.metadata.isbn:
            logging.log(f"  ISBN:     {result.metadata.isbn}")
        if result.metadata.asin:
            logging.log(f"  ASIN:     {result.metadata.asin}")
        logging.log(f"  Parts:    {len(result.files)} | Chapters: {len(result.chapters)} | Format: {fmt}")
        logging.log(f"  Output:   {location}")
    elif isinstance(result, Series):
        indices = (
            list(selected_indices)
            if selected_indices is not None
            else list(range(len(result.books)))
        )
        logging.log(f"  Series: {result.title} ({len(result.books)} books, {len(indices)} selected)")
        for idx in indices:
            if 0 <= idx < len(result.books):
                entry = result.books[idx]
                label = entry.title if isinstance(entry, Audiobook) else str(entry)
                logging.log(f"    [{idx + 1}] {label}")


def download_result(
    source: Source,
    result: Result,
    options: DownloadOptions,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    selected_indices: Optional[Sequence[int]] = None,
    summary: Optional[DownloadSummary] = None,
) -> None:
    """Download (or inspect when `options.info_only`) a resolved `Audiobook` or `Series` `Result`."""
    handler = interaction or logging.CliInteractionHandler()
    if (
        isinstance(result, Series)
        and selected_indices is None
        and getattr(options, "series_items", None)
    ):
        try:
            selected_indices = args.parse_series_selection(
                options.series_items or "",
                len(result.books),
            )
        except ValueError as exc:
            raise GenericAudiobookDLException(str(exc)) from exc

    if getattr(options, "info_only", False):
        if isinstance(result, Audiobook) and getattr(options, "enrich_metadata", False):
            _enrich_audiobook(source, result, options, interaction=handler)
        print_result_info(source, result, options, selected_indices=selected_indices)
        return

    logging.log("")  # Empty line
    if isinstance(result, Audiobook):
        logging.emit_item_progress(1, 1, result.title)
        logging.log(f"Downloading [blue]{result.title}[/] from [magenta]{source.name}[/]")
        processed = process_audiobook(
            source,
            result,
            options,
            interaction=handler,
            cancel_event=cancel_event,
        )
        if summary is not None:
            if processed is False:
                summary.books_skipped += 1
            else:
                summary.books_completed += 1
    elif isinstance(result, Series):
        if selected_indices is None:
            books_to_process: List[Union[BookId[Any], Audiobook]] = list(result.books)
        else:
            seen_idx = set()
            books_to_process = []
            for idx in selected_indices:
                if 0 <= idx < len(result.books) and idx not in seen_idx:
                    seen_idx.add(idx)
                    books_to_process.append(result.books[idx])
        count = len(books_to_process)
        if count == 0:
            logging.log(f"No books selected from [blue]{result.title}[/]")
            return
        logging.emit_item_progress(0, count, result.title)
        logging.log(
            f"Downloading [yellow not bold]{count}[/] books in [blue]{result.title}[/] from [magenta]{source.name}[/]"
        )
        done_count = 0
        skip_count = 0
        fail_count = 0
        errors: List[Tuple[str, str]] = []
        for index, book in enumerate(books_to_process, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            if index > 1 and getattr(options, "book_delay", 0.0) > 0:
                _pause_between_books(options.book_delay, cancel_event)
            logging.emit_item_progress(index, count, getattr(book, "title", None) or result.title)
            try:
                resolved = book if isinstance(book, Audiobook) else source.download_from_id(book.id)
                audiobooks = _flatten_resolved_books(source, resolved)
                for audiobook in audiobooks:
                    logging.emit_item_progress(index, count, audiobook.title)
                    processed = process_audiobook(
                        source,
                        audiobook,
                        options,
                        interaction=handler,
                        cancel_event=cancel_event,
                    )
                    if processed is False:
                        skip_count += 1
                    else:
                        done_count += 1
            except BookNotReleased:
                skip_count += 1
                logging.log(f"Skipped [blue]{book}[/] (not released)")
                continue
            except BookHasNoAudiobook:
                skip_count += 1
                logging.log(f"Skipped [blue]{book}[/] (no audiobook available)")
                continue
            except BookHasNoEbook:
                skip_count += 1
                logging.log(f"Skipped [blue]{book}[/] (no ebook available)")
                continue
            except AudiobookDLException as e:
                if isinstance(e, DownloadCancelled):
                    raise
                # Don't let a single broken book take down a 200+ book run.
                # Surface the underlying error and move on.
                fail_count += 1
                detail = str(e).strip() or e.error_description
                errors.append((str(book), detail))
                logging.log(f"Skipped [blue]{book}[/] ({detail})")
                if logging.is_debug_mode():
                    logging.print_traceback()
                continue
        if summary is not None:
            summary.books_completed += done_count
            summary.books_skipped += skip_count
            summary.books_failed += fail_count
            summary.book_errors.extend(errors)
        if fail_count > 0 or skip_count > 0:
            logging.log(
                f"Series summary: {done_count} completed, {skip_count} skipped, {fail_count} failed"
            )


def process_url(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    selected_indices: Optional[Sequence[int]] = None,
    prefetched: Optional[Tuple[Source, Result]] = None,
    summary: Optional[DownloadSummary] = None,
    session: Optional[requests.Session] = None,
) -> None:
    """
    Process `url` based on `options` and `config`.
    Will by default download the audiobook the URL is pointing to.
    """
    if prefetched is not None:
        source, result = prefetched
    else:
        source, result = prepare_source_result(
            url,
            options,
            config,
            interaction=interaction,
            cancel_event=cancel_event,
            session=session,
        )
    download_result(
        source,
        result,
        options,
        interaction=interaction,
        cancel_event=cancel_event,
        selected_indices=selected_indices,
        summary=summary,
    )


def get_cookie_path(options: DownloadOptions, config: Optional[SourceConfig]) -> Optional[str]:
    """
    Find path to cookie file. The cookie files are looked for in CLI arguments
    and in the current directory.
    """
    if options.cookie_file is not None and os.path.exists(options.cookie_file):
        return options.cookie_file
    if config is not None and config.cookie_file is not None and os.path.exists(config.cookie_file):
        return config.cookie_file
    if os.path.exists("./cookies.txt"):
        return "./cookies.txt"
    return None


def get_or_ask(
    attr: str,
    hidden: bool,
    options: DownloadOptions,
    config: Optional[SourceConfig],
    interaction: Optional[logging.InteractionHandler] = None,
) -> str:
    """
    Check for `attr` in CLI options and config options.
    Ask the user for the value if it is not found.
    """
    config_value = getattr(config, attr, None)
    value: Optional[str] = getattr(options, attr, None) or config_value
    if value is None:
        handler = interaction or logging.CliInteractionHandler()
        return handler.ask_input(attr.capitalize(), hidden=hidden)
    return value


def login(
    url: str,
    source: Source,
    options: DownloadOptions,
    config: Optional[SourceConfig],
    interaction: Optional[logging.InteractionHandler] = None,
) -> None:
    """Login to `source`."""
    login_data = {}
    for name in source.login_data:
        hidden = name == "password"
        login_data[name] = get_or_ask(name, hidden, options, config, interaction=interaction)
    source.login(url, **login_data)


def authenticate(
    url: str,
    source: Source,
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
) -> None:
    """Authenticate `source` using cookie file and/or login credentials."""
    logging.log(f"Authenticating with [magenta]{source.name}[/]")
    source_config = config.sources.get(source.name)
    if source_config is None:
        for alias in getattr(source, "names", ()):
            source_config = config.sources.get(alias.lower())
            if source_config is not None:
                break
    cookie_path = get_cookie_path(options, source_config)
    if cookie_path is not None:
        source.load_cookie_file(cookie_path)
    elif (
        getattr(source, "supports_cookies", False)
        and hasattr(source, "cached_cookie_path")
        and os.path.exists(source.cached_cookie_path())
    ):
        source.load_cookie_file(source.cached_cookie_path())
    if getattr(source, "supports_login", False) and not source.authenticated:
        login(url, source, options, source_config, interaction=interaction)


def _init_service_source(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: Optional[logging.InteractionHandler] = None,
    url: str = "",
) -> Source:
    """Instantiate `service_name` and authenticate when required."""
    source_class = find_source_by_name(service_name)
    source = source_class(options)
    handler = interaction or logging.CliInteractionHandler()
    if source.requires_authentication and not source.authenticated:
        authenticate(url, source, options, config, interaction=handler)
    return source


def verify_service_auth(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: Optional[logging.InteractionHandler] = None,
    persist_cookies: bool = True,
) -> bool:
    """Authenticate against `service_name` and optionally cache session cookies."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        ok = source.verify_auth()
        if ok and persist_cookies and source.supports_cookies and len(source._session.cookies) > 0:
            source.save_cookie_file()
        return ok


def search_service(
    service_name: str,
    query: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: Optional[logging.InteractionHandler] = None,
) -> List[CatalogBook]:
    """Search `service_name` catalog for `query`."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        return source.search(query)


def list_service_library(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: Optional[logging.InteractionHandler] = None,
) -> List[CatalogBook]:
    """List books from `service_name`'s user library/bookshelf."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        return source.list_library()


def _flatten_resolved_books(source: Source, resolved: Result) -> List[Audiobook]:
    """Expand a `Result` (`Audiobook` or `Series`) into concrete `Audiobook` items."""
    if isinstance(resolved, Audiobook):
        return [resolved]
    items: List[Audiobook] = []
    for entry in resolved.books:
        if isinstance(entry, Audiobook):
            items.append(entry)
        else:
            nested = source.download_from_id(entry.id)
            items.extend(_flatten_resolved_books(source, nested))
    return items


def audiobook_from_series(
    source: Source,
    book: Union[BookId[Any], Audiobook],
) -> Audiobook:
    """Make an `Audiobook` object from a book entry in a `Series`."""
    if isinstance(book, Audiobook):
        return book
    resolved = source.download_from_id(book.id)
    if isinstance(resolved, Audiobook):
        return resolved
    items = _flatten_resolved_books(source, resolved)
    if not items:
        raise BookHasNoAudiobook
    return items[0]


def _format_candidate_choice(candidate: EnrichmentCandidate) -> str:
    """Format an `EnrichmentCandidate` as a concise single-line selection option."""
    pct = round(candidate.score * 100)
    parts = [f"[{candidate.provider} {pct}%] {candidate.title}"]
    if candidate.authors:
        parts.append(f"by {', '.join(candidate.authors)}")
    if candidate.narrators:
        parts.append(f"(narr. {', '.join(candidate.narrators)})")
    if candidate.release_date:
        parts.append(f"[{candidate.release_date.year}]")
    extras = []
    if candidate.asin:
        extras.append(f"ASIN:{candidate.asin}")
    if candidate.isbn:
        extras.append(f"ISBN:{candidate.isbn}")
    if candidate.cover_url:
        extras.append("cover")
    if extras:
        parts.append("{" + ", ".join(extras) + "}")
    return " ".join(parts)


def _enrich_audiobook(
    source: Optional[Source],
    audiobook: Audiobook,
    options: DownloadOptions,
    *,
    interaction: Optional[logging.InteractionHandler] = None,
) -> Optional[EnrichmentResult]:
    """Run multi-provider metadata enrichment for `audiobook` according to `options`."""
    raw_providers = getattr(options, "enrich_providers", None)
    try:
        providers = parse_provider_list(raw_providers) if raw_providers else None
    except ValueError as exc:
        raise GenericAudiobookDLException(str(exc)) from exc

    interactive = bool(getattr(options, "enrich_interactive", False))
    selector = None
    if interactive:
        handler = interaction or logging.CliInteractionHandler()
        choose_fn = getattr(handler, "choose_index", None)
        if choose_fn is not None:
            def _select(provider_name: str, candidates: Sequence[EnrichmentCandidate]) -> Optional[int]:
                labels = [_format_candidate_choice(c) for c in candidates]
                return choose_fn(
                    f"Select {provider_name} match for '{audiobook.title}'",
                    labels,
                    default=0 if candidates[0].score >= 0.65 else None,
                )
            selector = _select

    result = enrich_audiobook_metadata(
        audiobook,
        providers=providers,
        session=getattr(source, "_session", None),
        proxy=getattr(options, "proxy", None),
        timeout=getattr(options, "timeout", None) or 10.0,
        database_directory=getattr(options, "database_directory", None),
        use_cache=not bool(getattr(options, "no_enrich_cache", False)),
        interactive=interactive,
        selector=selector,
        include_chapters=not bool(getattr(options, "no_chapters", False)),
    )
    if result.updated:
        details = list(result.fields_updated)
        if result.cover_updated:
            details.append("cover")
        if result.chapters_updated:
            details.append(f"chapters({len(audiobook.chapters)})")
        logging.book_update(
            f"Enriched metadata ({', '.join(result.providers_used)}): {', '.join(details)}"
        )
    return result


def process_audiobook(
    source: Source,
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Operate on `audiobook` based on `options`. Return False if skipped via archive."""
    if not audiobook.metadata.source and source is not None:
        audiobook.metadata.source = source.name
    if options.print_output:
        print_output(audiobook, options)
        return True
    if options.cover:
        download_cover(audiobook, options)
        return True

    source_name = (
        source.name
        if source is not None
        else (audiobook.metadata.source or "unknown")
    )
    book_key = make_book_key(source_name, audiobook)
    explicit_archive = getattr(options, "download_archive", None)
    ledger_path = explicit_archive or (
        default_archive_path(options.database_directory)
        if getattr(options, "database_directory", None)
        else None
    )
    archive: Optional[DownloadArchive] = None
    if explicit_archive:
        archive = DownloadArchive(explicit_archive)
        if archive.contains(source_name, book_key):
            logging.log(
                f"Skipping [blue]{audiobook.title}[/], already recorded in download archive."
            )
            return False

    if getattr(options, "enrich_metadata", False):
        _enrich_audiobook(source, audiobook, options, interaction=interaction)

    download(audiobook, options, interaction=interaction, cancel_event=cancel_event)
    if source is not None:
        source.on_download_complete(audiobook)

    if ledger_path:
        target_archive = archive if archive is not None else DownloadArchive(ledger_path)
        location = output.gen_output_location(
            options.output_template,
            audiobook.metadata,
            options.remove_chars,
        )
        raw_ext = audiobook.files[0].ext if audiobook.files else None
        fmt = raw_ext if is_ebook(audiobook) else (options.output_format or raw_ext)
        target_archive.record(
            source_name,
            book_key,
            title=audiobook.title,
            author=audiobook.metadata.author,
            url=audiobook.metadata.scrape_url,
            output_path=location,
            audio_format=fmt,
        )
    return True


def print_output(audiobook: Audiobook, options: DownloadOptions) -> None:
    """Print resolved output location."""
    metadata = audiobook.metadata
    location = output.gen_output_location(options.output_template, metadata, options.remove_chars)
    print(location)


def download_cover(audiobook: Audiobook, options: DownloadOptions) -> None:
    """Download audiobook cover to the resolved output location."""
    cover = audiobook.cover
    if not cover:
        return
    location = output.gen_output_location(
        options.output_template,
        audiobook.metadata,
        options.remove_chars,
    )
    ext = cover.extension.lstrip(".")
    if not location:
        cover_path = f"cover.{ext}"
    elif location.endswith(("/", "\\")) or os.path.isdir(location):
        cover_path = os.path.join(location, f"cover.{ext}")
    else:
        cover_path = f"{location}.{ext}"
    parent = os.path.dirname(cover_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(cover_path, "wb") as f:
        f.write(cover.image)

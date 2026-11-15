from dataclasses import dataclass
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import __version__, args, logging, output
from .args import DownloadOptions
from .config import Config, SourceConfig, load_config
from .exceptions import AudiobookDLException, BookHasNoAudiobook, BookNotReleased, DownloadCancelled
from .output.download import download
from .sources import find_compatible_source
from .sources.source import Source
from .utils import set_ffmpeg_dir
from .utils.audiobook import Audiobook, BookId, Result, Series


@dataclass
class DownloadSummary:
    """Outcome summary returned by `Downloader.download` and `run_downloads`."""

    completed: int = 0
    failed: int = 0
    cancelled: bool = False


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
    options.output_template = options.output_template or config.output_template or "{title}"
    options.database_directory = (
        options.database_directory
        or config.database_directory
        or args.default_database_directory()
    )
    options.skip_downloaded = bool(options.skip_downloaded or config.skip_downloaded)
    options.resume = bool(options.resume or config.resume)
    if options.audio_bitrate is None and config.audio_bitrate:
        options.audio_bitrate = config.audio_bitrate
    if config.ffmpeg_dir:
        set_ffmpeg_dir(config.ffmpeg_dir)
    ffmpeg_out = options.ffmpeg_output or options.debug
    return logging.JobContext(
        debug=options.debug,
        quiet=options.quiet,
        ffmpeg_output=ffmpeg_out,
    )


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
                try:
                    extra_kwargs: Dict[str, Any] = {}
                    if index == 0:
                        if selected_indices is not None:
                            extra_kwargs["selected_indices"] = selected_indices
                        if prefetched is not None:
                            extra_kwargs["prefetched"] = prefetched
                    process_url(
                        url,
                        self.options,
                        self.config,
                        interaction=self.interaction,
                        cancel_event=self.cancel_event,
                        **extra_kwargs,
                    )
                    summary.completed += 1
                except (DownloadCancelled, KeyboardInterrupt):
                    summary.cancelled = True
                    logging.log("Download cancelled")
                    break
                except AudiobookDLException as e:
                    summary.failed += 1
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
) -> Tuple[Source, Result]:
    """
    Resolve source for `url`, authenticate if required, and fetch `Result`
    without downloading audio files.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"
    logging.log("Finding compatible source")
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


def download_result(
    source: Source,
    result: Result,
    options: DownloadOptions,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    selected_indices: Optional[Sequence[int]] = None,
) -> None:
    """Download an already-resolved `Audiobook` or `Series` `Result`."""
    handler = interaction or logging.CliInteractionHandler()
    logging.log("")  # Empty line
    if isinstance(result, Audiobook):
        logging.emit_item_progress(1, 1, result.title)
        logging.log(f"Downloading [blue]{result.title}[/] from [magenta]{source.name}[/]")
        process_audiobook(source, result, options, interaction=handler, cancel_event=cancel_event)
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
        for index, book in enumerate(books_to_process, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            logging.emit_item_progress(index, count, getattr(book, "title", None) or result.title)
            try:
                audiobook = audiobook_from_series(source, book)
                logging.emit_item_progress(index, count, audiobook.title)
                process_audiobook(source, audiobook, options, interaction=handler, cancel_event=cancel_event)
            except BookNotReleased:
                logging.log(f"Skipped [blue]{book}[/] (not released)")
                continue
            except BookHasNoAudiobook:
                logging.log(f"Skipped [blue]{book}[/] (no audiobook available)")
                continue
            except AudiobookDLException as e:
                if isinstance(e, DownloadCancelled):
                    raise
                # Don't let a single broken book take down a 200+ book run.
                # Surface the underlying error and move on.
                logging.log(f"Skipped [blue]{book}[/] ({e.error_description})")
                if logging.is_debug_mode():
                    logging.print_traceback()
                continue


def process_url(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
    selected_indices: Optional[Sequence[int]] = None,
    prefetched: Optional[Tuple[Source, Result]] = None,
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
        )
    download_result(
        source,
        result,
        options,
        interaction=interaction,
        cancel_event=cancel_event,
        selected_indices=selected_indices,
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
    cookie_path = get_cookie_path(options, source_config)
    if cookie_path is not None:
        source.load_cookie_file(cookie_path)
    if source.supports_login and not source.authenticated:
        login(url, source, options, source_config, interaction=interaction)


def audiobook_from_series(
    source: Source,
    book: Union[BookId[Any], Audiobook],
) -> Audiobook:
    """Make an `Audiobook` object from a book entry in a `Series`."""
    if isinstance(book, Audiobook):
        return book
    return source.download_from_id(book.id)


def process_audiobook(
    source: Source,
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Operate on `audiobook` based on `options`."""
    if options.print_output:
        print_output(audiobook, options)
    elif options.cover:
        download_cover(audiobook, options)
    else:
        download(audiobook, options, interaction=interaction, cancel_event=cancel_event)
        source.on_download_complete(audiobook)


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

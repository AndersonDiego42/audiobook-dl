from audiobookdl import Source, logging, args, output, __version__
from .exceptions import AudiobookDLException, BookHasNoAudiobook, BookNotReleased, DownloadCancelled
from .utils.audiobook import Audiobook, Series
from .output.download import download
from .sources import find_compatible_source
from .config import load_config, Config, SourceConfig

from dataclasses import dataclass
import os
import sys
import threading
from typing import Optional, Sequence


@dataclass
class DownloadSummary:
    """Outcome summary returned by `run_downloads`."""
    completed: int = 0
    failed: int = 0
    cancelled: bool = False


def apply_runtime_options(options, config: Config) -> None:
    """Merge config defaults into options and configure logging flags."""
    options.output_template = options.output_template or config.output_template
    options.database_directory = options.database_directory or config.database_directory
    options.skip_downloaded = options.skip_downloaded or config.skip_downloaded
    logging.debug_mode = options.debug
    logging.quiet_mode = options.quiet
    logging.ffmpeg_output = options.ffmpeg_output or options.debug
    logging.debug(f"audiobook-dl {__version__}", remove_styling=True)
    logging.debug(f"python {sys.version}", remove_styling=True)


def run_downloads(
    urls: Sequence[str],
    options,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadSummary:
    """Run audiobook downloads for `urls` with shared options and interaction handling."""
    apply_runtime_options(options, config)
    handler = interaction or logging.CliInteractionHandler()
    summary = DownloadSummary()
    for url in urls:
        if cancel_event is not None and cancel_event.is_set():
            summary.cancelled = True
            break
        try:
            process_url(
                url,
                options,
                config,
                interaction=handler,
                cancel_event=cancel_event,
            )
            summary.completed += 1
        except DownloadCancelled:
            summary.cancelled = True
            logging.log("Download cancelled")
            break
        except AudiobookDLException as e:
            summary.failed += 1
            e.print()
            if logging.debug_mode:
                logging.print_traceback()
    return summary


def main() -> None:
    # Parsing arguments
    options = args.parse_arguments()
    config = load_config(options.config_location)
    urls = args.get_urls(options)
    if not urls:
        apply_runtime_options(options, config)
        logging.simple_help()
        exit()
    run_downloads(urls, options, config)


def process_url(
    url: str,
    options,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """
    Process url based on cli options.
    Will by default download the audiobook the url is pointing to.

    :param url: Url to process
    :param options: Cli options
    :param config: Configuration file options
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
    # Running program
    logging.debug(f"Downloading result of [underline]{url}")
    result = source.download(url)
    logging.log("") # Empty line
    if isinstance(result, Audiobook):
        logging.log(f"Downloading [blue]{result.title}[/] from [magenta]{source.name}[/]")
        process_audiobook(source, result, options, interaction=handler, cancel_event=cancel_event)
    elif isinstance(result, Series):
        count = len(result.books)
        logging.log(
            f"Downloading [yellow not bold]{count}[/] books in [blue]{result.title}[/] from [magenta]{source.name}[/]")
        for book in result.books:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            try:
                audiobook = audiobook_from_series(source, book)
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
                if logging.debug_mode:
                    logging.print_traceback()
                continue


def get_cookie_path(options, config: Optional[SourceConfig]) -> Optional[str]:
    """
    Find path to cookie file. The cookie files a looked for in cli arguments
    and in the current directory.

    :param options: Cli options
    :param config: Configuration for source
    :returns: Path to cookie file
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
    options,
    config: Optional[SourceConfig],
    interaction: Optional[logging.InteractionHandler] = None,
) -> str:
    """
    Check for `attr` in cli options and config options.
    Ask the user for the value if it is not found.

    :param attr: Attribute to search for
    :param hidden: Should the user input be hidden (Used for passwords)
    :param options: Cli options
    :param config: Configuration for source
    :returns: `attr` value from either cli options, config options, or user input
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
    options,
    config: Optional[SourceConfig],
    interaction: Optional[logging.InteractionHandler] = None,
):
    """
    Login to source

    :param url: Url for book
    :param source: Source the user is trying to login to
    :param options: Cli options
    :param config: Config file options
    """
    login_data = {}
    for name in source.login_data:
        hidden = name == "password"
        login_data[name] = get_or_ask(name, hidden, options, config, interaction=interaction)
    source.login(url, **login_data)


def authenticate(
    url: str,
    source: Source,
    options,
    config: Config,
    interaction: Optional[logging.InteractionHandler] = None,
):
    """
    Authenticate source

    :param url: Url of book the user is trying to download
    :param source: Source the book should be downloaded from
    :param options: Cli options
    :param config: Config file options
    """
    logging.log(f"Authenticating with [magenta]{source.name}[/]")
    source_config = config.sources.get(source.name)
    # Load cookie file
    cookie_path = get_cookie_path(options, source_config)
    if cookie_path is not None:
        source.load_cookie_file(cookie_path)
    # Authenticating with username and password
    if source.supports_login and not source.authenticated:
        login(url, source, options, source_config, interaction=interaction)


def audiobook_from_series(source: Source, book) -> Audiobook:
    """
    Make an audiobook object from book result in series

    :param source: Source book originates from
    :param book: Audiobook metadata or book id
    :returns: Audiobook
    """
    if isinstance(book, Audiobook):
        return book
    return source.download_from_id(book.id)


def process_audiobook(
    source: Source,
    audiobook: Audiobook,
    options,
    interaction: Optional[logging.InteractionHandler] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """
    Operate on audiobook based on cli arguments

    :param audiobook: Audiobook to operate on
    :param options: Cli options
    :returns: Nothing
    """
    if options.print_output:
        print_output(audiobook, options)
    elif options.cover:
        download_cover(audiobook)
    else:
        download(audiobook, options, interaction=interaction, cancel_event=cancel_event)
        source.on_download_complete(audiobook)



def print_output(audiobook: Audiobook, options) -> None:
    """Prints output location"""
    metadata = audiobook.metadata
    location = output.gen_output_location(options.output_template, metadata, options.remove_chars)
    print(location)


def download_cover(audiobook: Audiobook) -> None:
    """
    Download audiobook cover

    :param audiobook: Audiobook with cover
    """
    cover = audiobook.cover
    if cover:
        with open(f"cover.{cover.extension}", "wb") as f:
            f.write(cover.image)


if __name__ == "__main__":
    main()

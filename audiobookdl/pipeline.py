from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import subprocess
import sys
import threading
import time
import weakref
from typing import Any
from collections.abc import Callable, Iterator, Sequence

import requests

from . import __version__, logging, output
from .config import (
    DEFAULT_PROFILE,
    Config,
    SourceConfig,
    get_source_config,
    load_config,
    normalize_profile_name,
    resolve_effective_source_profile,
)
from .options_schema import (
    MERGEABLE_OPTION_SPECS,
    DownloadOptions,
    build_default_options,
    parse_series_selection,
)
from .exceptions import (
    AudiobookDLException,
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotReleased,
    DownloadCancelled,
    GenericAudiobookDLException,
    NoSourceFound,
    UserNotAuthorized,
)
from .output.download import download
from .sources import find_compatible_source, find_source_by_name
from .sources.source import CatalogBook, Source, bind_pooled_session
from .utils import subprocess_kwargs
from .utils.http import create_configured_http_session
from .utils.archive import DownloadArchive, default_archive_path, make_book_key
from .utils.audiobook import (
    Audiobook,
    BookId,
    CENCDecryption,
    Result,
    Series,
    parse_decryption_key_spec,
    resolve_audiobook_output_format,
)
from .utils.enrichment import (
    EnrichmentCandidate,
    EnrichmentResult,
    enrich_audiobook_metadata,
    resolve_provider_list,
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
    book_errors: list[tuple[str, str]] = field(default_factory=list, compare=False)
    outputs: list[dict[str, Any]] = field(default_factory=list, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary fields into a JSON-compatible dictionary."""
        return {
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "books_completed": self.books_completed,
            "books_skipped": self.books_skipped,
            "books_failed": self.books_failed,
            "outputs": list(self.outputs),
            "errors": [{"target": t, "error": e} for t, e in self.book_errors],
        }


DEFAULT_POST_EXEC_TIMEOUT = 300.0


def run_post_exec_hook(
    command: str | Sequence[str] | None,
    *,
    output_path: str,
    metadata: Any,
    audio_format: str | None,
    timeout: float | None = DEFAULT_POST_EXEC_TIMEOUT,
    allow_shell: bool = True,
) -> int | None:
    """
    Execute user-configured `post_exec` command with `AUDIOBOOK_DL_*` environment variables.

    Supports both argv sequences (`shell=False`) and shell command strings (`shell=True`
    when `allow_shell=True`). Uses a bounded timeout (`DEFAULT_POST_EXEC_TIMEOUT` = 300s),
    suppresses Windows console windows, captures stdout/stderr into the application log,
    and logs failures without failing an already-completed audiobook download.
    """
    use_shell = False
    subproc_cmd: str | list[str]
    display_cmd: str
    if command is None:
        return None
    if isinstance(command, str):
        stripped = command.strip()
        if not stripped:
            return None
        if not allow_shell:
            logging.log(
                f"[yellow]Warning:[/yellow] Skipping shell post_exec command without shell opt-in: {stripped}"
            )
            return None
        use_shell = True
        subproc_cmd = stripped
        display_cmd = stripped
    else:
        argv = [str(part) for part in command if str(part)]
        if not argv:
            return None
        subproc_cmd = argv
        display_cmd = " ".join(argv)

    env = os.environ.copy()
    env.update(
        {
            "AUDIOBOOK_DL_PATH": str(output_path or ""),
            "AUDIOBOOK_DL_TITLE": str(getattr(metadata, "title", "") or ""),
            "AUDIOBOOK_DL_AUTHOR": str(getattr(metadata, "author", "") or ""),
            "AUDIOBOOK_DL_SOURCE": str(getattr(metadata, "source", "") or ""),
            "AUDIOBOOK_DL_FORMAT": str(audio_format or ""),
        }
    )
    try:
        completed = subprocess.run(
            subproc_cmd,
            shell=use_shell,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            **subprocess_kwargs(capture_output=True),
        )
    except subprocess.TimeoutExpired as exc:
        logging.error(
            f"post_exec hook timed out after {timeout}s ({display_cmd})"
            if timeout is not None
            else f"post_exec hook timed out ({display_cmd})"
        )
        stderr_raw = getattr(exc, "stderr", None)
        if stderr_raw:
            stderr_txt = (
                stderr_raw.decode("utf-8", errors="replace")
                if isinstance(stderr_raw, bytes)
                else str(stderr_raw)
            ).strip()
            if stderr_txt:
                logging.debug(f"post_exec stderr before timeout: {stderr_txt}")
        return None
    except OSError as exc:
        logging.error(f"post_exec hook failed to start ({display_cmd}): {exc}")
        return None

    stdout_txt = (getattr(completed, "stdout", None) or "").strip()
    stderr_txt = (getattr(completed, "stderr", None) or "").strip()
    if stdout_txt:
        logging.log(f"post_exec stdout: {stdout_txt}")
    if completed.returncode != 0:
        detail = f" — {stderr_txt}" if stderr_txt else ""
        logging.error(f"post_exec hook exited with code {completed.returncode}: {display_cmd}{detail}")
    elif stderr_txt:
        logging.debug(f"post_exec stderr: {stderr_txt}")
    return completed.returncode


@dataclass(frozen=True)
class DownloadEvent:
    """Structured log or progress event emitted by `Downloader`."""

    kind: str
    data: dict[str, Any]


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
        eta_seconds: float | None,
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
    explicit: frozenset[str] = getattr(options, "_explicit_fields", frozenset())
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

    if not getattr(options, "output_template", None):
        options.output_template = "{title}"

    ffmpeg_dir = config.ffmpeg_dir.strip() or None if config.ffmpeg_dir else None
    ffmpeg_out = options.ffmpeg_output or options.debug
    return logging.JobContext(
        debug=options.debug,
        quiet=options.quiet,
        ffmpeg_output=ffmpeg_out,
        ffmpeg_dir=ffmpeg_dir,
    )


def _pause_between_books(delay: float, cancel_event: threading.Event | None) -> None:
    if delay <= 0:
        return
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise DownloadCancelled
    else:
        time.sleep(delay)


SessionPoolKey = tuple[str, str, str, str, str]
SessionPool = dict[SessionPoolKey, Any]
_ACTIVE_SESSION_POOL: ContextVar[SessionPool | None] = ContextVar(
    "audiobookdl_active_session_pool",
    default=None,
)


@contextmanager
def session_pool_scope(pool: SessionPool | None) -> Iterator[None]:
    """Bind a per-run HTTP session pool for `Downloader` / `prepare_source_result` calls."""
    if pool is None:
        yield
        return
    token = _ACTIVE_SESSION_POOL.set(pool)
    try:
        yield
    finally:
        _ACTIVE_SESSION_POOL.reset(token)


def close_session_pool(pool: SessionPool | None) -> None:
    """Close and clear all pooled HTTP sessions without raising."""
    if not pool:
        return
    for session in list(pool.values()):
        close_fn = getattr(session, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    pool.clear()


def _close_downloader_sessions(
    http_session: Any,
    session_pool: SessionPool,
) -> None:
    close_fn = getattr(http_session, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass
    close_session_pool(session_pool)


def _resolve_pool_key(
    source_class: type[Source[Any]],
    options: DownloadOptions,
    config: Config,
) -> SessionPoolKey:
    names = getattr(source_class, "names", ())
    service_key = names[0].lower() if names else source_class.__name__.lower()
    explicit_inline_creds = bool(
        getattr(options, "username", None)
        or getattr(options, "password", None)
        or getattr(options, "cookie_file", None)
    )
    requested_profile = getattr(options, "profile", None)
    if explicit_inline_creds and not (requested_profile and str(requested_profile).strip()):
        eff_profile = DEFAULT_PROFILE
    else:
        eff_profile = resolve_effective_source_profile(
            config,
            service_key,
            requested_profile,
            aliases=names,
        )
    impersonate = (getattr(options, "impersonate", None) or "").strip()
    proxy = (getattr(options, "proxy", None) or getattr(config, "proxy", None) or "").strip()
    inline_scope = (
        f"{getattr(options, 'username', None) or ''}|{getattr(options, 'cookie_file', None) or ''}"
        if explicit_inline_creds
        else ""
    )
    return (service_key, eff_profile, impersonate, proxy, inline_scope)


class Downloader:
    """
    Programmatic client for authenticating, fetching metadata, and downloading audiobooks.

    All operations run inside an isolated `logging.JobContext`, leaving global
    `audiobookdl.logging` flags and sinks untouched.
    """

    def __init__(
        self,
        config: Config | None = None,
        options: DownloadOptions | None = None,
        *,
        on_event: EventCallback | None = None,
        sink: logging.LogSink | None = None,
        interaction: logging.InteractionHandler | None = None,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
    ) -> None:
        if sink is not None and on_event is not None:
            raise ValueError("Specify either 'sink' or 'on_event', not both")
        self.options: DownloadOptions = (
            options if options is not None else build_default_options()
        )
        self.config: Config = (
            config if config is not None else load_config(self.options.config_location)
        )
        self.interaction: logging.InteractionHandler = (
            interaction if interaction is not None else logging.NonInteractiveInteractionHandler()
        )
        self.cancel_event: threading.Event | None = cancel_event
        self.pause_event: threading.Event | None = pause_event
        if sink is not None:
            self.sink: logging.LogSink | None = sink
        elif on_event is not None:
            self.sink = _EventLogSink(on_event)
        else:
            self.sink = None
        proxy = getattr(self.options, "proxy", None) or getattr(self.config, "proxy", None)
        self._http_session = create_configured_http_session(self.options, proxy=proxy)
        self._owned_session_pool: SessionPool = {}
        self._http_session_finalizer = weakref.finalize(
            self,
            _close_downloader_sessions,
            self._http_session,
            self._owned_session_pool,
        )

    def _effective_session_pool(self) -> SessionPool:
        active = _ACTIVE_SESSION_POOL.get()
        return active if active is not None else self._owned_session_pool

    def close(self) -> None:
        """Close the shared HTTP session and any pooled source sessions held by this `Downloader`."""
        self._http_session_finalizer()

    def __enter__(self) -> "Downloader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _build_context(self) -> logging.JobContext:
        ctx = apply_runtime_options(self.options, self.config)
        ctx.interaction = self.interaction
        ctx.cancel_event = self.cancel_event
        ctx.pause_event = self.pause_event
        ctx.sink = self.sink if self.sink is not None else logging.get_ui_sink()
        return ctx

    def prepare(self, url: str) -> tuple[Source, Result]:
        """Resolve source for `url`, authenticate if needed, and fetch `Result`."""
        with logging.job_context(self._build_context()):
            return prepare_source_result(
                url,
                self.options,
                self.config,
                interaction=self.interaction,
                cancel_event=self.cancel_event,
                session=self._http_session,
                session_pool=self._effective_session_pool(),
            )

    def fetch_metadata(self, url: str) -> Result:
        """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
        source, result = self.prepare(url)
        close_fn = getattr(source, "close", None)
        if callable(close_fn):
            close_fn()
        return result

    def download_result(
        self,
        source: Source,
        result: Result,
        *,
        selected_indices: Sequence[int] | None = None,
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
                config=self.config,
            )

    def download(
        self,
        urls: str | Sequence[str],
        *,
        selected_indices: Sequence[int] | None = None,
        prefetched: tuple[Source, Result] | None = None,
    ) -> DownloadSummary:
        """Download one URL or a sequence of URLs and return a `DownloadSummary`."""
        url_list: Sequence[str] = [urls] if isinstance(urls, str) else urls
        ctx = self._build_context()
        summary = DownloadSummary()
        with logging.job_context(ctx):
            logging.debug(f"audiobook-dl {__version__}")
            logging.debug(f"python {sys.version}")
            for index, url in enumerate(url_list):
                try:
                    logging.wait_if_paused(
                        cancel_event=self.cancel_event,
                        pause_event=self.pause_event,
                    )
                except DownloadCancelled:
                    summary.cancelled = True
                    logging.log("Download cancelled")
                    break
                if index > 0 and self.options.book_delay > 0:
                    try:
                        _pause_between_books(self.options.book_delay, self.cancel_event)
                    except DownloadCancelled:
                        summary.cancelled = True
                        logging.log("Download cancelled")
                        break
                try:
                    extra_kwargs: dict[str, Any] = {}
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
                        session_pool=self._effective_session_pool(),
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
                    detail = str(e).strip() or e.error_description
                    summary.book_errors.append((url, detail))
                    e.print()
                    if logging.is_debug_mode():
                        logging.print_traceback()
        return summary


def run_downloads(
    urls: Sequence[str],
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    sink: logging.LogSink | None = None,
    selected_indices: Sequence[int] | None = None,
    prefetched: tuple[Source, Result] | None = None,
) -> DownloadSummary:
    """Run audiobook downloads for `urls` with isolated per-job context."""
    with Downloader(
        config=config,
        options=options,
        sink=sink,
        interaction=interaction,
        cancel_event=cancel_event,
        pause_event=pause_event,
    ) as downloader:
        return downloader.download(
            urls,
            selected_indices=selected_indices,
            prefetched=prefetched,
        )


def prepare_source_result(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    session: requests.Session | None = None,
    session_pool: SessionPool | None = None,
) -> tuple[Source, Result]:
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
    active_pool = session_pool if session_pool is not None else _ACTIVE_SESSION_POOL.get()
    pool_key = _resolve_pool_key(source_class, options, config) if active_pool is not None else None
    pooled_session = active_pool.get(pool_key) if (active_pool is not None and pool_key is not None) else None
    with bind_pooled_session(pooled_session):
        source = source_class(options)
    try:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        if source.requires_authentication and not source.authenticated:
            authenticate(url, source, options, config, interaction=handler)
        if (
            active_pool is not None
            and pool_key is not None
            and pooled_session is None
            and hasattr(source, "_session")
            and (not source.requires_authentication or source.authenticated)
        ):
            active_pool[pool_key] = source._session
            release_fn = getattr(source, "release_session_ownership", None)
            if callable(release_fn):
                release_fn()
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled
        logging.debug(f"Downloading result of [underline]{url}")
        try:
            result = source.download(url)
        except UserNotAuthorized:
            if not (
                getattr(source, "_loaded_cached_cookies", False)
                and getattr(source, "supports_login", False)
            ):
                raise
            logging.debug(
                f"Cached session cookies for {source.name} were rejected; clearing stale jar and re-authenticating"
            )
            cached_path_fn = getattr(source, "cached_cookie_path", None)
            if callable(cached_path_fn):
                try:
                    os.remove(cached_path_fn())
                except OSError:
                    pass
            reset_fn = getattr(source, "reset_authentication", None)
            if callable(reset_fn):
                reset_fn(clear_cookies=True)
            else:
                source._loaded_cached_cookies = False
            authenticate(
                url,
                source,
                options,
                config,
                interaction=handler,
                use_cached_cookies=False,
            )
            if (
                active_pool is not None
                and pool_key is not None
                and hasattr(source, "_session")
                and (not source.requires_authentication or source.authenticated)
            ):
                active_pool[pool_key] = source._session
                release_fn = getattr(source, "release_session_ownership", None)
                if callable(release_fn):
                    release_fn()
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled from None
            result = source.download(url)
        return source, result
    except BaseException:
        _close_source(source)
        raise


def fetch_metadata(
    url: str,
    options: DownloadOptions,
    config: Config,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
) -> Result:
    """Fetch `Audiobook` or `Series` metadata for `url` without downloading audio files."""
    source, result = prepare_source_result(
        url,
        options,
        config,
        interaction=interaction,
        cancel_event=cancel_event,
    )
    _close_source(source)
    return result


def _audiobook_output_entry(audiobook: Audiobook, options: DownloadOptions) -> dict[str, Any]:
    location = output.gen_output_location(
        options.output_template,
        audiobook.metadata,
        options.remove_chars,
    )
    fmt = resolve_audiobook_output_format(audiobook, options.output_format)
    candidate_file = f"{location}.{fmt}" if fmt else ""
    resolved_path = candidate_file if (candidate_file and os.path.isfile(candidate_file)) else location
    return {
        "title": audiobook.title,
        "authors": list(audiobook.metadata.authors),
        "source": audiobook.metadata.source,
        "format": fmt,
        "output_path": resolved_path,
        "parts": len(audiobook.files),
        "chapters": len(audiobook.chapters),
    }


def download_result(
    source: Source,
    result: Result,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    selected_indices: Sequence[int] | None = None,
    summary: DownloadSummary | None = None,
    config: Config | None = None,
) -> None:
    """Download a resolved `Audiobook` or `Series` `Result`."""
    handler = interaction or logging.NonInteractiveInteractionHandler()
    if (
        isinstance(result, Series)
        and selected_indices is None
        and getattr(options, "series_items", None)
    ):
        try:
            selected_indices = parse_series_selection(
                options.series_items or "",
                len(result.books),
            )
        except ValueError as exc:
            raise GenericAudiobookDLException(str(exc)) from exc

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
            config=config,
        )
        if summary is not None:
            if processed is False:
                summary.books_skipped += 1
            else:
                summary.books_completed += 1
                summary.outputs.append(_audiobook_output_entry(result, options))
    elif isinstance(result, Series):
        if selected_indices is None:
            books_to_process: list[BookId[Any] | Audiobook] = list(result.books)
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
        errors: list[tuple[str, str]] = []
        for index, book in enumerate(books_to_process, start=1):
            logging.wait_if_paused(cancel_event=cancel_event)
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
                        config=config,
                    )
                    if processed is False:
                        skip_count += 1
                    else:
                        done_count += 1
                        if summary is not None:
                            summary.outputs.append(_audiobook_output_entry(audiobook, options))
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
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    selected_indices: Sequence[int] | None = None,
    prefetched: tuple[Source, Result] | None = None,
    summary: DownloadSummary | None = None,
    session: requests.Session | None = None,
    session_pool: SessionPool | None = None,
) -> None:
    """
    Process `url` based on `options` and `config`.
    Will by default download the audiobook the URL is pointing to.
    """
    owns_source = prefetched is None
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
            session_pool=session_pool,
        )
    try:
        download_result(
            source,
            result,
            options,
            interaction=interaction,
            cancel_event=cancel_event,
            selected_indices=selected_indices,
            summary=summary,
            config=config,
        )
    finally:
        if owns_source:
            _close_source(source)


def get_cookie_path(options: DownloadOptions, config: SourceConfig | None) -> str | None:
    """
    Find path to cookie file from `options.cookie_file`, source config, or `./cookies.txt`.
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
    config: SourceConfig | None,
    interaction: logging.InteractionHandler | None = None,
) -> str:
    """
    Check for `attr` in `options` and source config options.
    Ask the user for the value if it is not found.
    """
    config_value = getattr(config, attr, None)
    value: str | None = getattr(options, attr, None) or config_value
    if value is None:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        return handler.ask_input(attr.capitalize(), hidden=hidden)
    return value


def login(
    url: str,
    source: Source,
    options: DownloadOptions,
    config: SourceConfig | None,
    interaction: logging.InteractionHandler | None = None,
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
    interaction: logging.InteractionHandler | None = None,
    *,
    use_cached_cookies: bool = True,
) -> None:
    """Authenticate `source` using cookie file and/or login credentials."""
    explicit_inline_creds = bool(
        getattr(options, "username", None)
        or getattr(options, "password", None)
        or getattr(options, "cookie_file", None)
    )
    requested_profile = getattr(options, "profile", None)
    if explicit_inline_creds and not (requested_profile and str(requested_profile).strip()):
        req_profile = DEFAULT_PROFILE
    else:
        req_profile = resolve_effective_source_profile(
            config,
            source.name,
            requested_profile,
            aliases=getattr(source, "names", ()),
        )
    profile_label = f" ({req_profile})" if req_profile != DEFAULT_PROFILE else ""
    logging.log(f"Authenticating with [magenta]{source.name}{profile_label}[/]")
    source_config = get_source_config(
        config,
        source.name,
        req_profile,
        aliases=getattr(source, "names", ()),
    )
    if source_config is not None and getattr(source_config, "locale", None):
        source._configured_locale = source_config.locale  # type: ignore[attr-defined]
    if (
        req_profile != DEFAULT_PROFILE
        and source_config is None
        and not getattr(options, "username", None)
        and not getattr(options, "password", None)
        and not getattr(options, "cookie_file", None)
    ):
        logging.log(
            f"Warning: profile '{req_profile}' not found in config for {source.name}"
        )
    source._loaded_cached_cookies = False
    cookie_path = get_cookie_path(options, source_config)
    if cookie_path is not None:
        source.load_cookie_file(cookie_path)
    elif (
        use_cached_cookies
        and getattr(source, "supports_cookies", False)
        and hasattr(source, "cached_cookie_path")
        and os.path.exists(source.cached_cookie_path())
    ):
        source.load_cookie_file(source.cached_cookie_path())
        source._loaded_cached_cookies = bool(source.authenticated)
    if getattr(source, "supports_login", False) and not source.authenticated:
        login(url, source, options, source_config, interaction=interaction)
        source._loaded_cached_cookies = False
        if (
            getattr(source, "supports_cookies", False)
            and hasattr(source, "save_cookie_file")
            and getattr(options, "database_directory", None)
            and len(source._session.cookies) > 0
        ):
            try:
                source.save_cookie_file()
            except OSError as exc:
                logging.debug(f"Could not persist session cookies for {source.name}: {exc}")


def _init_service_source(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    url: str = "",
) -> Source:
    """Instantiate `service_name` and authenticate when required."""
    source_class = find_source_by_name(service_name)
    source = source_class(options)
    handler = interaction or logging.NonInteractiveInteractionHandler()
    if source.requires_authentication and not source.authenticated:
        authenticate(url, source, options, config, interaction=handler)
    return source


def _close_source(source: Any) -> None:
    close_fn = getattr(source, "close", None)
    if callable(close_fn):
        close_fn()


def verify_service_auth(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
    persist_cookies: bool = True,
) -> bool:
    """Authenticate against `service_name` and optionally cache session cookies."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        try:
            ok = source.verify_auth()
            if ok and persist_cookies and source.supports_cookies and len(source._session.cookies) > 0:
                source.save_cookie_file()
            return ok
        finally:
            _close_source(source)


def search_service(
    service_name: str,
    query: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
) -> list[CatalogBook]:
    """Search `service_name` catalog for `query`."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        try:
            return source.search(query)
        finally:
            _close_source(source)


def list_service_library(
    service_name: str,
    options: DownloadOptions,
    config: Config,
    *,
    interaction: logging.InteractionHandler | None = None,
) -> list[CatalogBook]:
    """List books from `service_name`'s user library/bookshelf."""
    with logging.job_context(apply_runtime_options(options, config)):
        source = _init_service_source(service_name, options, config, interaction=interaction)
        try:
            return source.list_library()
        finally:
            _close_source(source)


def _flatten_resolved_books(source: Source, resolved: Result) -> list[Audiobook]:
    """Expand a `Result` (`Audiobook` or `Series`) into concrete `Audiobook` items."""
    if isinstance(resolved, Audiobook):
        return [resolved]
    items: list[Audiobook] = []
    for entry in resolved.books:
        if isinstance(entry, Audiobook):
            items.append(entry)
        else:
            nested = source.download_from_id(entry.id)
            items.extend(_flatten_resolved_books(source, nested))
    return items


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


def enrich_audiobook_with_options(
    audiobook: Audiobook,
    options: DownloadOptions,
    *,
    source: Source | None = None,
    interaction: logging.InteractionHandler | None = None,
    config: Config | None = None,
) -> EnrichmentResult | None:
    """Run multi-provider metadata enrichment for `audiobook` according to `options`."""
    resolved_cfg = config
    if resolved_cfg is None and getattr(options, "config_location", None):
        try:
            resolved_cfg = load_config(options.config_location)
        except Exception:
            resolved_cfg = None
    custom_providers = (
        resolved_cfg.custom_metadata_providers if resolved_cfg is not None else None
    )
    raw_providers = getattr(options, "enrich_providers", None)
    try:
        providers = resolve_provider_list(raw_providers, custom_providers=custom_providers)
    except ValueError as exc:
        raise GenericAudiobookDLException(str(exc)) from exc

    interactive = bool(getattr(options, "enrich_interactive", False))
    selector = None
    if interactive:
        handler = interaction or logging.NonInteractiveInteractionHandler()
        choose_fn = getattr(handler, "choose_index", None)
        if choose_fn is not None:
            def _select(provider_name: str, candidates: Sequence[EnrichmentCandidate]) -> int | None:
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
        custom_providers=custom_providers,
        session=getattr(source, "_session", None),
        proxy=getattr(options, "proxy", None),
        timeout=getattr(options, "timeout", None) or 10.0,
        database_directory=getattr(options, "database_directory", None),
        use_cache=not bool(getattr(options, "no_enrich_cache", False)),
        interactive=interactive,
        selector=selector,
        include_chapters=not bool(getattr(options, "no_chapters", False)),
        override_chapters=bool(getattr(options, "override_chapters", False)),
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


_CENC_CONTAINER_EXTENSIONS = frozenset({"mp4", "m4a", "m4b", "m4s", "m4v", "mov"})


def _apply_user_decryption_key(audiobook: Audiobook, options: DownloadOptions) -> None:
    """Apply user-supplied `options.decryption_key` to CENC files as fallback/override."""
    raw_key = getattr(options, "decryption_key", None)
    if not isinstance(raw_key, str) or not raw_key.strip():
        return
    default_key, key_map = parse_decryption_key_spec(raw_key)
    for file in audiobook.files:
        if isinstance(file.encryption_method, CENCDecryption):
            if default_key is not None:
                file.encryption_method.key = default_key
            if key_map:
                file.encryption_method.key_map.update(key_map)
        elif (
            file.encryption_method is None
            and file.encryption_resolver is None
            and file.ext.lstrip(".").lower() in _CENC_CONTAINER_EXTENSIONS
        ):
            file.encryption_method = CENCDecryption(key=default_key, key_map=dict(key_map))


def process_audiobook(
    source: Source,
    audiobook: Audiobook,
    options: DownloadOptions,
    interaction: logging.InteractionHandler | None = None,
    cancel_event: threading.Event | None = None,
    config: Config | None = None,
) -> bool:
    """Operate on `audiobook` based on `options`. Return False if skipped via archive."""
    if not audiobook.metadata.source and source is not None:
        audiobook.metadata.source = source.name
    if options.cover:
        download_cover(audiobook, options)
        return True

    source_name = (
        source.name
        if source is not None
        else (audiobook.metadata.source or "unknown")
    )
    book_key = make_book_key(source_name, audiobook)
    use_archive = not bool(getattr(options, "no_download_archive", False))
    explicit_archive = getattr(options, "download_archive", None) if use_archive else None
    ledger_path = (
        explicit_archive
        or (
            default_archive_path(options.database_directory)
            if use_archive and getattr(options, "database_directory", None)
            else None
        )
    )
    archive: DownloadArchive | None = None
    check_archive = bool(use_archive and (explicit_archive or (options.skip_downloaded and ledger_path)))
    try:
        if check_archive and ledger_path:
            archive = DownloadArchive(ledger_path, persistent=True)
            if archive.contains(source_name, book_key):
                logging.log(
                    f"Skipping [blue]{audiobook.title}[/], already recorded in download archive."
                )
                return False
            dup = archive.find_cross_source_duplicate(
                source_name,
                book_key,
                title=audiobook.title,
                author=audiobook.metadata.author,
                isbn=audiobook.metadata.isbn,
                asin=audiobook.metadata.asin,
            )
            if dup is not None:
                dup_label = dup.get("output_path") or dup.get("title") or dup.get("source")
                if options.skip_downloaded:
                    logging.log(
                        f"Skipping [blue]{audiobook.title}[/], already archived from {dup['source']} ({dup_label})."
                    )
                    return False
                logging.log(
                    f"[yellow]Warning:[/yellow] [blue]{audiobook.title}[/] matches existing archive entry from {dup['source']} ({dup_label})."
                )

        enrich_result = None
        if getattr(options, "enrich_metadata", False):
            enrich_result = enrich_audiobook_with_options(
                audiobook,
                options,
                source=source,
                interaction=interaction,
                config=config,
            )

        logging.wait_if_paused(cancel_event=cancel_event)
        _apply_user_decryption_key(audiobook, options)
        if enrich_result is not None and enrich_result.chapters_updated:
            download(
                audiobook,
                options,
                interaction=interaction,
                cancel_event=cancel_event,
                chapters_already_overridden=True,
            )
        else:
            download(audiobook, options, interaction=interaction, cancel_event=cancel_event)
        if source is not None:
            source.on_download_complete(audiobook)

        location = output.gen_output_location(
            options.output_template,
            audiobook.metadata,
            options.remove_chars,
        )
        fmt = resolve_audiobook_output_format(audiobook, options.output_format)
        candidate_file = f"{location}.{fmt}" if fmt else ""
        resolved_output = candidate_file if (candidate_file and os.path.isfile(candidate_file)) else location

        if ledger_path:
            if archive is None:
                archive = DownloadArchive(ledger_path, persistent=True)
            active_profile = normalize_profile_name(getattr(options, "profile", None))
            archive.record(
                source_name,
                book_key,
                title=audiobook.title,
                author=audiobook.metadata.author,
                url=audiobook.metadata.scrape_url,
                output_path=resolved_output,
                audio_format=fmt,
                profile=active_profile,
                status="downloaded",
                series=audiobook.metadata.series,
                series_order=audiobook.metadata.series_order,
                narrator=audiobook.metadata.narrator,
                isbn=audiobook.metadata.isbn,
                asin=audiobook.metadata.asin,
            )
    finally:
        if archive is not None:
            archive.close()

    force_abs = bool(getattr(options, "upload_to_abs", False))
    abs_cfg = config.audiobookshelf if config is not None else None
    if abs_cfg is None and force_abs:
        abs_cfg = load_config(getattr(options, "config_location", None)).audiobookshelf
    if abs_cfg is not None:
        try:
            from audiobookdl.integrations.audiobookshelf import sync_book_to_audiobookshelf

            if sync_book_to_audiobookshelf(
                resolved_output,
                audiobook.metadata,
                abs_cfg,
                force_upload=force_abs,
                session=getattr(audiobook, "session", None),
            ):
                logging.log(f"Synced [blue]{audiobook.title}[/] to Audiobookshelf")
        except Exception as exc:
            logging.error(f"Audiobookshelf sync failed for '{audiobook.title}': {exc}")
    run_post_exec_hook(
        getattr(options, "post_exec", None),
        output_path=resolved_output,
        metadata=audiobook.metadata,
        audio_format=fmt,
    )
    if source is not None:
        clear_fn = getattr(source, "clear_page_cache", None)
        if callable(clear_fn):
            clear_fn()
    return True


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

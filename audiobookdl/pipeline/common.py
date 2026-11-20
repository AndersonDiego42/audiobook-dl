from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace as dc_replace
import os
import subprocess
import threading
import time
from typing import Any, TypedDict, cast
from collections.abc import Callable, Iterator, Sequence


from .. import logging
from ..config import (
    DEFAULT_PROFILE,
    Config,
    resolve_effective_source_profile,
)
from ..options_schema import (
    MERGEABLE_OPTION_SPECS,
    DownloadOptions,
)
from ..exceptions import (
    AudiobookDLException,
    BookHasNoAudiobook,
    BookHasNoEbook,
    BookNotFound,
    BookNotReleased,
    DownloadCancelled,
    MissingBookAccess,
    NoFilesFound,
)
from ..sources.source import Source
from ..utils.concurrency import run_cancellable_subprocess


UNAVAILABLE_BOOK_EXCEPTIONS: tuple[type[AudiobookDLException], ...] = (
    MissingBookAccess,
    BookNotFound,
    BookNotReleased,
    BookHasNoAudiobook,
    BookHasNoEbook,
    NoFilesFound,
)


def _describe_unavailable_reason(exc: AudiobookDLException) -> str:
    """Return a concise human-readable reason for skipping an unavailable book."""
    if isinstance(exc, BookNotReleased):
        return "not released"
    if isinstance(exc, BookHasNoAudiobook):
        return "no audiobook available"
    if isinstance(exc, BookHasNoEbook):
        return "no ebook available"
    if isinstance(exc, MissingBookAccess):
        return "inaccessible"
    if isinstance(exc, BookNotFound):
        return "not found"
    if isinstance(exc, NoFilesFound):
        return "no files available"
    return exc.error_description.replace("_", " ")


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
    cancel_event: threading.Event | None = None,
) -> int | None:
    """
    Execute user-configured `post_exec` command with `AUDIOBOOK_DL_*` environment variables.

    Supports both argv sequences (`shell=False`) and shell command strings (`shell=True`
    when `allow_shell=True`). Uses a bounded timeout (`DEFAULT_POST_EXEC_TIMEOUT` = 300s),
    suppresses Windows console windows, captures stdout/stderr into the application log,
    and logs failures without failing an already-completed audiobook download.
    """
    subproc_cmd: str | list[str]
    display_cmd: str
    if command is None:
        return None
    if isinstance(command, str):
        stripped = command.strip()
        if not stripped:
            return None
        if not allow_shell:
            logging.warning(
                f"Warning: Skipping shell post_exec command without shell opt-in: {stripped}"
            )
            return None
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
    if cancel_event is None:
        ctx = logging.get_job_context()
        cancel_event = ctx.cancel_event if ctx is not None else None
    try:
        if isinstance(subproc_cmd, str):
            shell_cmd = ["cmd.exe", "/c", subproc_cmd] if os.name == "nt" else ["/bin/sh", "-c", subproc_cmd]
            completed = run_cancellable_subprocess(
                shell_cmd,
                cancel_event=cancel_event,
                timeout=timeout,
                capture_stdout=True,
                env=env,
            )
        else:
            completed = run_cancellable_subprocess(
                subproc_cmd,
                cancel_event=cancel_event,
                timeout=timeout,
                capture_stdout=True,
                env=env,
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

    def _to_text(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace").strip()
        return str(raw).strip()

    stdout_txt = _to_text(getattr(completed, "stdout", None))
    stderr_txt = _to_text(getattr(completed, "stderr", None))
    if stdout_txt:
        logging.log(f"post_exec stdout: {stdout_txt}")
    if completed.returncode != 0:
        detail = f" — {stderr_txt}" if stderr_txt else ""
        logging.error(f"post_exec hook exited with code {completed.returncode}: {display_cmd}{detail}")
    elif stderr_txt:
        logging.debug(f"post_exec stderr: {stderr_txt}")
    return completed.returncode


class _LogEventData(TypedDict):
    level: str
    message: str


class _ProgressStartEventData(TypedDict):
    description: str
    total: float


class _ProgressAdvanceEventData(TypedDict):
    completed: float
    total: float


class _ProgressMetricsEventData(TypedDict):
    completed: float
    total: float
    bytes_downloaded: int
    speed_bps: float
    elapsed_seconds: float
    eta_seconds: float | None


class _ItemProgressEventData(TypedDict):
    current: int
    total: int
    title: str


class _DownloadEstimateEventData(TypedDict):
    estimated_bytes: int | None
    is_exact: bool
    known_files: int
    total_files: int


class _ProgressFinishEventData(TypedDict, total=False):
    pass


DownloadEventData = (
    _LogEventData
    | _ProgressStartEventData
    | _ProgressAdvanceEventData
    | _ProgressMetricsEventData
    | _ItemProgressEventData
    | _DownloadEstimateEventData
    | _ProgressFinishEventData
)


@dataclass(frozen=True)
class DownloadEvent:
    """Structured log or progress event emitted by `Downloader`.

    `kind` is one of ``"log"``, ``"progress_start"``, ``"progress_advance"``,
    ``"progress_metrics"``, ``"item_progress"``, ``"download_estimate"``, or
    ``"progress_finish"``; `data` carries the kind-specific payload.
    """

    kind: str
    data: DownloadEventData


EventCallback = Callable[[DownloadEvent], None]


class _EventLogSink:
    """`LogSink` implementation forwarding structured events to an `on_event` callback."""

    def __init__(self, on_event: EventCallback) -> None:
        self._on_event = on_event

    def _emit(self, kind: str, **data: Any) -> None:
        self._on_event(DownloadEvent(kind=kind, data=cast(DownloadEventData, data)))

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

    def on_download_estimate(
        self,
        estimated_bytes: int | None,
        is_exact: bool,
        known_files: int,
        total_files: int,
    ) -> None:
        self._emit(
            "download_estimate",
            estimated_bytes=estimated_bytes,
            is_exact=is_exact,
            known_files=known_files,
            total_files=total_files,
        )

    def on_progress_finish(self) -> None:
        self._emit("progress_finish")


def resolve_runtime_options(
    options: DownloadOptions,
    config: Config,
) -> DownloadOptions:
    """Return a new `DownloadOptions` with config defaults merged in.

    Pure: the input `options` instance is never mutated. `_explicit_fields`
    (an `init=False` field) is copied onto the resolved instance so explicit
    CLI/GUI overrides keep winning over `audiobook-dl.toml` values.
    """
    resolved = dc_replace(options)
    resolved._explicit_fields = frozenset(getattr(options, "_explicit_fields", frozenset()))
    explicit: frozenset[str] = resolved._explicit_fields

    for spec in MERGEABLE_OPTION_SPECS:
        if spec.name in explicit:
            continue
        cfg_key = spec.config_key or spec.name
        cfg_value = getattr(config, cfg_key, None)
        if spec.merge_require_truthy:
            if not cfg_value:
                continue
        elif cfg_value is None:
            continue
        if spec.merge_only_if_default and getattr(resolved, spec.name) != spec.resolve_default():
            continue
        setattr(
            resolved,
            spec.name,
            spec.merge_cast(cfg_value) if spec.merge_cast is not None else cfg_value,
        )

    if not getattr(resolved, "output_template", None):
        resolved.output_template = "{title}"
    return resolved


def apply_runtime_options(
    options: DownloadOptions,
    config: Config,
) -> logging.JobContext:
    """Build an isolated `JobContext` from config-merged runtime options."""
    resolved = resolve_runtime_options(options, config)
    ffmpeg_dir = config.ffmpeg_dir.strip() or None if config.ffmpeg_dir else None
    return logging.JobContext(
        debug=resolved.debug,
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



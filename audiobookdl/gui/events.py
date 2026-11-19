"""Typed immutable controller-to-UI event messages dispatched via `GuiController.event_queue`."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class LogEvent:
    level: str
    message: str
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogBatchEvent:
    entries: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BatchItemEvent:
    run_id: int
    item_index: int
    item_total: int
    target_url: str


@dataclass(frozen=True, slots=True)
class JobStatusEvent:
    job_id: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class JobMetaEvent:
    job_id: str
    title: str = ""
    output_path: str = ""


@dataclass(frozen=True, slots=True)
class ItemProgressEvent:
    run_id: int
    current: int
    total: int
    title: str


@dataclass(frozen=True, slots=True)
class ProgressStartEvent:
    description: str
    total: float
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProgressAdvanceEvent:
    completed: float
    total: float
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProgressMetricsEvent:
    completed: float
    total: float
    bytes_downloaded: int
    speed_bps: float
    elapsed_seconds: float
    eta_seconds: float | None
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProgressFinishEvent:
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadEstimateEvent:
    estimated_bytes: int | None
    is_exact: bool
    known_files: int
    total_files: int
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class PromptInputEvent:
    prompt: str
    hidden: bool
    holder: dict[str, Any]
    done: threading.Event


@dataclass(frozen=True, slots=True)
class PromptConfirmEvent:
    message: str
    holder: dict[str, Any]
    done: threading.Event


@dataclass(frozen=True, slots=True)
class PromptChoiceEvent:
    title: str
    choices: tuple[str, ...]
    default_index: int | None
    holder: dict[str, Any]
    done: threading.Event


@dataclass(frozen=True, slots=True)
class PreviewDoneEvent:
    url: str
    prepared: tuple[Any, Any] | None
    error: Exception | None


@dataclass(frozen=True, slots=True)
class AuthVerifiedEvent:
    service_label: str
    ok: bool
    error: Exception | None


@dataclass(frozen=True, slots=True)
class AccountsVerificationItemEvent:
    run_id: int
    service_key: str
    profile: str
    ok: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class AccountsVerificationDoneEvent:
    run_id: int
    valid_count: int
    checked_count: int
    total_count: int
    cancelled: bool


@dataclass(frozen=True, slots=True)
class OAuthConnectedEvent:
    service_key: str
    service_label: str
    profile: str
    ok: bool
    error: Exception | None


@dataclass(frozen=True, slots=True)
class OAuthDisconnectedEvent:
    service_key: str
    service_label: str
    profile: str
    revoked: bool
    error: Exception | None


@dataclass(frozen=True, slots=True)
class CatalogLoadedEvent:
    request_id: int
    target_tab: Any
    payload: Any
    error: Exception | None


@dataclass(frozen=True, slots=True)
class FfmpegInstallDoneEvent:
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class UpdateCheckedEvent:
    info: Any
    interactive: bool


@dataclass(frozen=True, slots=True)
class SyncDoneEvent:
    cycle: Any
    error: Exception | None


@dataclass(frozen=True, slots=True)
class SyncEnqueueReadyEvent:
    pending_groups: Any
    stats: dict[str, Any]
    error: Exception | None


@dataclass(frozen=True, slots=True)
class AbsUploadDoneEvent:
    title: str
    ok: bool
    error_message: str | None


@dataclass(frozen=True, slots=True)
class RunDoneEvent:
    run_id: int
    summary: Any
    error: Exception | None


ControllerEvent: TypeAlias = (
    LogEvent
    | LogBatchEvent
    | BatchItemEvent
    | JobStatusEvent
    | JobMetaEvent
    | ItemProgressEvent
    | ProgressStartEvent
    | ProgressAdvanceEvent
    | ProgressMetricsEvent
    | ProgressFinishEvent
    | DownloadEstimateEvent
    | PromptInputEvent
    | PromptConfirmEvent
    | PromptChoiceEvent
    | PreviewDoneEvent
    | AuthVerifiedEvent
    | AccountsVerificationItemEvent
    | AccountsVerificationDoneEvent
    | OAuthConnectedEvent
    | OAuthDisconnectedEvent
    | CatalogLoadedEvent
    | FfmpegInstallDoneEvent
    | UpdateCheckedEvent
    | SyncDoneEvent
    | SyncEnqueueReadyEvent
    | AbsUploadDoneEvent
    | RunDoneEvent
)

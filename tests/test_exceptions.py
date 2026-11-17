"""Tests verifying every `AudiobookDLException` subclass and error template renders cleanly."""

from __future__ import annotations

from pathlib import Path

import pytest

from audiobookdl import exceptions, logging
from audiobookdl.integrations.audiobookshelf import AudiobookshelfError


class _RecordingSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def on_log(self, level: str, message: str) -> None:
        self.messages.append((level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        pass

    def on_progress_advance(self, completed: float, total: float) -> None:
        pass

    def on_progress_finish(self) -> None:
        pass


def _all_exception_subclasses() -> list[type[exceptions.AudiobookDLException]]:
    discovered: list[type[exceptions.AudiobookDLException]] = []
    stack = list(exceptions.AudiobookDLException.__subclasses__())
    while stack:
        cls = stack.pop(0)
        discovered.append(cls)
        stack.extend(cls.__subclasses__())
    return sorted(discovered, key=lambda c: c.__name__)


_SUBCLASS_CASES: list[tuple[type[exceptions.AudiobookDLException], dict[str, object]]] = [
    (AudiobookshelfError, {"message": "Connection refused"}),
    (exceptions.BookHasNoAudiobook, {}),
    (exceptions.BookHasNoEbook, {}),
    (exceptions.BookNotFound, {}),
    (exceptions.BookNotReleased, {}),
    (exceptions.CloudflareBlocked, {}),
    (exceptions.ConfigNotFound, {}),
    (exceptions.DataNotPresent, {}),
    (exceptions.DownloadCancelled, {}),
    (
        exceptions.DownloadError,
        {
            "url": "https://example.com/audio.mp3",
            "status_code": 403,
            "content_type": "text/html",
            "expected_status_code": 200,
            "expected_content_type": "audio/mpeg",
            "body": "Forbidden",
        },
    ),
    (exceptions.FailedCombining, {}),
    (
        exceptions.GenericAudiobookDLException,
        {"heading": "Something failed", "body": "More details here"},
    ),
    (exceptions.MissingBookAccess, {}),
    (exceptions.MissingDependency, {"dependency": "ffmpeg"}),
    (exceptions.NoFilesFound, {}),
    (exceptions.NoSourceFound, {}),
    (exceptions.RequestError, {}),
    (exceptions.UserNotAuthorized, {}),
]


def test_exception_test_matrix_covers_all_subclasses():
    tested = {cls for cls, _ in _SUBCLASS_CASES}
    actual = set(_all_exception_subclasses())
    assert tested == actual


def test_error_template_inventory_covers_all_18_assets():
    errors_dir = Path(exceptions.__file__).resolve().parent / "assets" / "errors"
    actual_templates = {p.stem for p in errors_dir.glob("*.txt")}
    subclass_templates = {
        cls.error_description
        for cls, _ in _SUBCLASS_CASES
        if cls is not exceptions.DownloadCancelled
    }
    extra_templates = {"chapters_add", "nextory_want_to_read"}
    assert len(actual_templates) == 18
    assert subclass_templates | extra_templates == actual_templates


@pytest.mark.parametrize("exc_cls,kwargs", _SUBCLASS_CASES, ids=lambda v: v.__name__ if isinstance(v, type) else "kwargs")
def test_audiobook_dl_exception_print_renders_cleanly(exc_cls, kwargs):
    sink = _RecordingSink()
    exc = exc_cls(**kwargs)
    with logging.job_context(logging.JobContext(sink=sink)):
        exc.print()

    assert len(sink.messages) >= 1
    rendered = "\n".join(msg for _lvl, msg in sink.messages)
    assert rendered.strip() != ""
    if exc_cls is exceptions.NoSourceFound:
        assert "Librivox" in rendered
    elif exc_cls is exceptions.DownloadCancelled:
        assert "Download cancelled" in rendered
    for val in kwargs.values():
        assert str(val) in rendered


def test_non_subclass_error_templates_render_cleanly():
    sink = _RecordingSink()
    with logging.job_context(logging.JobContext(sink=sink)):
        exceptions.DataNotPresent(error_description="nextory_want_to_read").print()
        logging.print_error_file("chapters_add", filetype="m4b")

    assert len(sink.messages) == 2
    assert "Nextory" in sink.messages[0][1]
    assert "m4b" in sink.messages[1][1]


@pytest.mark.parametrize("exc_cls,kwargs", _SUBCLASS_CASES, ids=lambda v: v.__name__ if isinstance(v, type) else "kwargs")
def test_audiobook_dl_exception_str_is_non_empty_plain_text(exc_cls, kwargs):
    exc = exc_cls(**kwargs)
    text = str(exc).strip()
    assert text != ""
    assert "[red]" not in text

    # Subclasses using base `**kwargs` accept `detail=...` cleanly for GUI dialogs.
    if exc_cls not in (AudiobookshelfError, exceptions.GenericAudiobookDLException):
        bare = exc_cls(detail="HTTP 401 from https://api.storytel.net/libraries/bookshelf")
        bare_text = str(bare).strip()
        assert bare_text != ""
        assert "HTTP 401 from https://api.storytel.net/libraries/bookshelf" in bare_text

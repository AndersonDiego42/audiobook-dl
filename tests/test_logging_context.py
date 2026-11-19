import threading
from unittest.mock import patch

import pytest

from audiobookdl import logging
from audiobookdl.options_schema import build_default_options
from audiobookdl.pipeline import run_downloads
from audiobookdl.config import Config
from audiobookdl.utils.concurrency import run_cancellable_pool


class _CaptureSink:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.logs: list[tuple[str, str]] = []
        self.starts: list[str] = []
        self.advances: list[tuple[float, float]] = []
        self.finished = 0

    def on_log(self, level: str, message: str) -> None:
        with self.lock:
            self.logs.append((level, message))

    def on_progress_start(self, description: str, total: float) -> None:
        with self.lock:
            self.starts.append(description)

    def on_progress_advance(self, completed: float, total: float) -> None:
        with self.lock:
            self.advances.append((completed, total))

    def on_progress_finish(self) -> None:
        with self.lock:
            self.finished += 1


@pytest.mark.no_worker_cap
def test_concurrent_run_downloads_isolate_logging_sinks_and_flags():
    sink_a = _CaptureSink()
    sink_b = _CaptureSink()
    barrier = threading.Barrier(2)

    def fake_process_url(url, options, config, interaction=None, cancel_event=None, summary=None, **_kwargs):
        barrier.wait(timeout=2.0)
        logging.log(f"start {url}")
        logging.debug(f"dbg {url}")
        with logging.progress() as prog:
            task = prog.add_task(f"task {url}", total=2.0)
            run_cancellable_pool(
                lambda idx: (
                    logging.debug(f"worker-{idx} {url}"),
                    prog.advance(task, 1.0),
                ),
                [0, 1],
                max_workers=2,
            )

    cfg = Config(sources={}, output_template=None, database_directory=None, skip_downloaded=None)
    opts_a = build_default_options(debug=True)
    opts_b = build_default_options(debug=False)

    results = {}

    def run_job(name, url, opts, sink):
        results[name] = run_downloads([url], opts, cfg, sink=sink)

    with patch("audiobookdl.pipeline.process_url", side_effect=fake_process_url):
        t_a = threading.Thread(target=run_job, args=("a", "https://a.example/1", opts_a, sink_a))
        t_b = threading.Thread(target=run_job, args=("b", "https://b.example/2", opts_b, sink_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

    assert results["a"].completed == 1
    assert results["b"].completed == 1

    # Job A had debug=True: receives INFO + DEBUG (including worker threads)
    a_messages = [msg for _lvl, msg in sink_a.logs]
    assert any("start https://a.example/1" in m for m in a_messages)
    assert any("dbg https://a.example/1" in m for m in a_messages)
    assert any("worker-0 https://a.example/1" in m for m in a_messages)
    assert not any("b.example" in m for m in a_messages)
    assert sink_a.starts == ["task https://a.example/1"]
    assert sink_a.finished == 1

    # Job B had debug=False: receives INFO only, no DEBUG logs, and no Job A leaks
    b_levels = {lvl for lvl, _msg in sink_b.logs}
    b_messages = [msg for _lvl, msg in sink_b.logs]
    assert b_levels == {"INFO"}
    assert any("start https://b.example/2" in m for m in b_messages)
    assert not any("dbg https://b.example/2" in m for m in b_messages)
    assert not any("a.example" in m for m in b_messages)
    assert sink_b.starts == ["task https://b.example/2"]
    assert sink_b.finished == 1
    assert logging.get_job_context() is None


def test_job_context_isolates_debug_and_quiet_flags():
    observed = {}
    barrier = threading.Barrier(2)

    def worker(name: str, debug_flag: bool, quiet_flag: bool) -> None:
        with logging.job_context(logging.JobContext(debug=debug_flag, quiet=quiet_flag)):
            barrier.wait(timeout=2.0)
            observed[name] = (logging.is_debug_mode(), logging.is_quiet_mode())

    t1 = threading.Thread(target=worker, args=("debug", True, False))
    t2 = threading.Thread(target=worker, args=("quiet", False, True))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert observed == {"debug": (True, False), "quiet": (False, True)}


def test_progress_adapter_tracks_bytes_and_speed():
    with logging.progress() as prog:
        task = prog.add_task("Downloading", total=2.0)
        assert prog.get_byte_metrics(task) is None
        prog.record_bytes(task, 2048)
        stats = prog.get_byte_metrics(task)
        assert stats is not None
        assert stats[0] == 2048
        assert stats[1] > 0.0


def test_wait_if_paused_blocks_until_resumed_or_cancelled():
    from audiobookdl.exceptions import DownloadCancelled

    pause_ev = threading.Event()
    pause_ev.clear()  # paused
    cancel_ev = threading.Event()

    resumed_at: list[float] = []
    started = threading.Event()

    def _worker() -> None:
        with logging.job_context(logging.JobContext(cancel_event=cancel_ev, pause_event=pause_ev)):
            started.set()
            logging.wait_if_paused()
            resumed_at.append(1.0)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    assert started.wait(timeout=1.0)
    t.join(timeout=0.05)
    assert t.is_alive()  # still paused
    pause_ev.set()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert resumed_at == [1.0]

    # Cancelling while paused raises DownloadCancelled
    pause_ev.clear()
    cancel_ev.set()
    try:
        with logging.job_context(logging.JobContext(cancel_event=cancel_ev, pause_event=pause_ev)):
            logging.wait_if_paused()
        raise AssertionError("Expected DownloadCancelled")
    except DownloadCancelled:
        pass


def test_series_download_pauses_between_books(tmp_path):
    from types import SimpleNamespace
    from audiobookdl.config import Config
    from audiobookdl.pipeline import Downloader
    from audiobookdl.utils.audiobook import BookId, Series

    pause_ev = threading.Event()
    pause_ev.set()
    cancel_ev = threading.Event()

    b1 = BookId(id="1", title="Book 1")
    b2 = BookId(id="2", title="Book 2")
    series = Series(title="Paused Series", books=[b1, b2])
    processed: list[str] = []
    first_done = threading.Event()

    class _FakeSource:
        name = "FakeSource"
        names = ["FakeSource"]

        def download_from_id(self, bid: str):
            return bid

    with patch("audiobookdl.pipeline._flatten_resolved_books", side_effect=lambda _s, b: [SimpleNamespace(title=f"T{b}")]), patch(
        "audiobookdl.pipeline.process_audiobook",
        side_effect=lambda _src, ab, *_a, **_kw: (
            processed.append(ab.title),
            pause_ev.clear() if len(processed) == 1 else None,
            first_done.set() if len(processed) == 1 else None,
            True,
        )[-1],
    ):
        dl = Downloader(
            config=Config(sources={}, output_template=str(tmp_path / "{title}"), database_directory=None, skip_downloaded=False),
            options=build_default_options(),
            cancel_event=cancel_ev,
            pause_event=pause_ev,
        )
        t = threading.Thread(target=lambda: dl.download_result(_FakeSource(), series), daemon=True)  # type: ignore[arg-type]
        t.start()
        assert first_done.wait(timeout=2.0)
        t.join(timeout=0.15)
        assert t.is_alive()
        assert processed == ["T1"]
        pause_ev.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert processed == ["T1", "T2"]


def test_logging_warning_emits_to_sink_and_stdlib_logger(caplog):
    import logging as stdlib_logging

    assert not hasattr(logging, "strip_markup")
    assert not hasattr(logging, "escape_markup")
    assert not hasattr(logging, "print_error_file")
    assert not hasattr(logging, "print_traceback")

    sink = _CaptureSink()
    with logging.job_context(logging.JobContext(quiet=True, sink=sink)):
        logging.warning("Warning: existing file failed integrity check")
        logging.book_update("Adding metadata")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.log_traceback()

    assert ("WARNING", "Warning: existing file failed integrity check") in sink.logs
    assert any(lvl == "ERROR" and "ValueError: boom" in msg for lvl, msg in sink.logs)

    with logging.job_context(logging.JobContext(quiet=False, sink=sink)):
        logging.book_update("Adding metadata")
    assert ("INFO", "Adding metadata") in sink.logs

    with caplog.at_level(stdlib_logging.WARNING, logger="audiobookdl"):
        logging.warning("Warning: disk nearly full")
        logging.warning("Plain warning message")
        try:
            raise RuntimeError("headless boom")
        except RuntimeError:
            logging.log_traceback()

    warning_msgs = [r.message for r in caplog.records if r.levelno == stdlib_logging.WARNING]
    error_msgs = [r.message for r in caplog.records if r.levelno == stdlib_logging.ERROR]
    assert "Warning: disk nearly full" in warning_msgs
    assert "Plain warning message" in warning_msgs
    assert any("RuntimeError: headless boom" in msg for msg in error_msgs)


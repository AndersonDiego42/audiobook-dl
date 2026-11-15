import threading
from unittest.mock import patch

from audiobookdl import args, logging
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


def test_concurrent_run_downloads_isolate_logging_sinks_and_flags():
    sink_a = _CaptureSink()
    sink_b = _CaptureSink()
    barrier = threading.Barrier(2)

    def fake_process_url(url, options, config, interaction=None, cancel_event=None, summary=None):
        barrier.wait(timeout=2.0)
        logging.log(f"start {url}")
        logging.debug(f"dbg {url}")
        with logging.progress([]) as prog:
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
    opts_a = args.build_default_options(debug=True, quiet=False)
    opts_b = args.build_default_options(debug=False, quiet=True)

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

    # Job A had debug=True and quiet=False: receives INFO + DEBUG (including worker threads)
    a_messages = [msg for _lvl, msg in sink_a.logs]
    assert any("start https://a.example/1" in m for m in a_messages)
    assert any("worker-0 https://a.example/1" in m for m in a_messages)
    assert not any("b.example" in m for m in a_messages)
    assert sink_a.starts == ["task https://a.example/1"]
    assert sink_a.finished == 1

    # Job B had debug=False and quiet=True: receives no INFO/DEBUG logs and no Job A leaks
    assert sink_b.logs == []
    assert sink_b.starts == ["task https://b.example/2"]
    assert sink_b.finished == 1
    assert logging.get_job_context() is None


def test_job_context_isolates_ffmpeg_output_flag():
    observed = {}
    barrier = threading.Barrier(2)

    def worker(name: str, ffmpeg_flag: bool) -> None:
        with logging.job_context(logging.JobContext(ffmpeg_output=ffmpeg_flag)):
            barrier.wait(timeout=2.0)
            observed[name] = logging.is_ffmpeg_output()

    t1 = threading.Thread(target=worker, args=("verbose", True))
    t2 = threading.Thread(target=worker, args=("quiet", False))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert observed == {"verbose": True, "quiet": False}

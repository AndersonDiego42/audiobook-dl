"""Integration tests for `audiobook-dl serve` headless Web UI and JSON API."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from audiobookdl import args
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.web import create_web_server


def _http_json(url: str, *, method: str = "GET", payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_parse_serve_arguments_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_DL_WEB_TOKEN", "env-secret")
    opts = args.parse_serve_arguments(["--host", "0.0.0.0", "--port", "9090"])
    assert opts.host == "0.0.0.0"
    assert opts.port == 9090
    assert opts.token == "env-secret"


def test_web_ui_server_endpoints_auth_and_download_queue(tmp_path, monkeypatch):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_dir = (tmp_path / "db").as_posix()
    cfg_path.write_text(f'database_directory = "{db_dir}"\n', encoding="utf-8")

    monkeypatch.setattr(
        "audiobookdl.gui.controller.run_downloads",
        lambda *_a, **_kw: DownloadSummary(completed=1, failed=0, cancelled=False),
    )

    serve_opts = args.ServeOptions(
        host="127.0.0.1",
        port=0,
        output_dir=str(tmp_path / "out"),
        token="top-secret",
        config_location=str(cfg_path),
    )
    server = create_web_server(serve_opts)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    auth_hdr = {"Authorization": "Bearer top-secret"}

    try:
        # 1. Dashboard HTML and public health check
        with urllib.request.urlopen(f"{base}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")
            assert "audiobook-dl Web UI" in html
            assert "API Token" in html

        status, health = _http_json(f"{base}/api/health")
        assert status == 200 and health["status"] == "ok"

        # 2. Protected API rejects unauthenticated calls
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/status")
        assert exc_info.value.code == 401

        # 3. Services list includes RSS, Archive.org, and Libro.fm
        status, services = _http_json(f"{base}/api/services", headers=auth_hdr)
        assert status == 200
        keys = {s["key"] for s in services}
        assert {"archive.org", "libro.fm", "rss"} <= keys

        # 4. Submit download job -> completes via controller
        status, queued = _http_json(
            f"{base}/api/download",
            method="POST",
            payload={"urls": ["https://librivox.org/demo-book"]},
            headers=auth_hdr,
        )
        assert status == 202 and queued["queued"] == 1

        deadline = threading.Event()
        final_status = None
        for _ in range(50):
            _, snap = _http_json(f"{base}/api/status", headers=auth_hdr)
            if not snap["running"] and snap["queue"] and snap["queue"][0]["status"] == "Completed":
                final_status = snap
                break
            deadline.wait(0.05)
        assert final_status is not None

        # 5. Pause & archive ledger endpoints respond cleanly
        _, pause_res = _http_json(f"{base}/api/pause", method="POST", payload={}, headers=auth_hdr)
        assert "paused" in pause_res
        _, lib_entries = _http_json(f"{base}/api/library?limit=10", headers=auth_hdr)
        assert isinstance(lib_entries, list)
    finally:
        server.shutdown()
        server.dashboard_state.shutdown()  # type: ignore[attr-defined]
        server.server_close()
        thread.join(timeout=3.0)

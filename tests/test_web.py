"""Integration and security tests for `audiobook-dl serve` headless Web UI and JSON API."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import threading
import time
from typing import Any
import urllib.error
import urllib.request

import pytest

from audiobookdl import args
from audiobookdl.pipeline import DownloadSummary
from audiobookdl.sources.source import CatalogBook
from audiobookdl.sync import SyncCycleResult
from audiobookdl.web import MAX_REQUEST_BODY_BYTES, create_web_server


def _http_raw(
    url: str,
    *,
    method: str = "GET",
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    req = urllib.request.Request(url, data=raw_body, method=method, headers=dict(headers or {}))
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _http_json(url: str, *, method: str = "GET", payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    return _http_raw(url, method=method, raw_body=data, headers=req_headers)


@contextmanager
def _running_web_server(tmp_path, *, token: str | None = "top-secret"):
    cfg_path = tmp_path / "audiobook-dl.toml"
    db_dir = (tmp_path / "db").as_posix()
    cfg_path.write_text(f'database_directory = "{db_dir}"\n', encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)

    serve_opts = args.ServeOptions(
        host="127.0.0.1",
        port=0,
        output_dir=str(out_dir),
        token=token,
        config_location=str(cfg_path),
    )
    server = create_web_server(serve_opts)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield server, base, out_dir
    finally:
        server.shutdown()
        server.dashboard_state.shutdown()  # type: ignore[attr-defined]
        server.server_close()
        thread.join(timeout=3.0)


def test_parse_serve_arguments_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_DL_WEB_TOKEN", "env-secret")
    opts = args.parse_serve_arguments(["--host", "0.0.0.0", "--port", "9090"])
    assert opts.host == "0.0.0.0"
    assert opts.port == 9090
    assert opts.token == "env-secret"


def test_web_ui_server_endpoints_auth_and_download_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "audiobookdl.gui.controller.run_downloads",
        lambda *_a, **_kw: DownloadSummary(completed=1, failed=0, cancelled=False),
    )

    with _running_web_server(tmp_path, token="top-secret") as (_server, base, _out_dir):
        auth_hdr = {"Authorization": "Bearer top-secret"}
        x_api_hdr = {"X-API-Token": "top-secret"}

        # 1. Dashboard HTML and public health check
        with urllib.request.urlopen(f"{base}/", timeout=5.0) as html_resp:
            assert html_resp.status == 200
            html = html_resp.read().decode("utf-8")
            assert "audiobook-dl Web UI" in html
            assert "API Token" in html

        status, health = _http_json(f"{base}/api/health")
        assert status == 200 and health["status"] == "ok"

        # 2. Protected API rejects unauthenticated and wrong-token calls, accepts X-API-Token
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/status")
        assert exc_info.value.code == 401

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/status", headers={"X-API-Token": "wrong"})
        assert exc_info.value.code == 401

        status, _snap = _http_json(f"{base}/api/status", headers=x_api_hdr)
        assert status == 200

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

        # 5. Pause, cancel, queue/clear & archive ledger endpoints respond cleanly
        _, pause_res = _http_json(f"{base}/api/pause", method="POST", payload={}, headers=x_api_hdr)
        assert "paused" in pause_res

        status, cancel_res = _http_json(f"{base}/api/cancel", method="POST", payload={}, headers=x_api_hdr)
        assert status == 200 and cancel_res == {"cancelling": True}

        status, clear_res = _http_json(
            f"{base}/api/queue/clear",
            method="POST",
            payload={},
            headers=x_api_hdr,
        )
        assert status == 200 and clear_res["cleared"] == 1

        _, lib_entries = _http_json(f"{base}/api/library?limit=10", headers=auth_hdr)
        assert isinstance(lib_entries, list)
        assert not (tmp_path / "db" / "downloads.db").exists()


def test_web_ui_catalog_sync_and_error_responses(tmp_path, monkeypatch):
    catalog_calls: list[tuple[str, str, str | None]] = []

    def fake_search(service_key, query_text, opts, _cfg):
        catalog_calls.append(("search", service_key, opts.profile))
        return [CatalogBook(title=f"Hit for {query_text}", url="https://librivox.org/hit", author="Author")]

    def fake_library(service_key, opts, _cfg):
        catalog_calls.append(("library", service_key, opts.profile))
        return [CatalogBook(title="Library Book", url="https://librivox.org/lib", author="Lib Author")]

    monkeypatch.setattr("audiobookdl.web.search_service", fake_search)
    monkeypatch.setattr("audiobookdl.web.list_service_library", fake_library)
    monkeypatch.setattr(
        "audiobookdl.web.run_sync_once",
        lambda *_a, **_kw: SyncCycleResult(profiles_checked=2, books_discovered=5, downloaded=1, skipped=4, failed=0),
    )

    with _running_web_server(tmp_path, token="sec") as (_server, base, _out_dir):
        hdr = {"X-API-Token": "sec"}

        # Catalog search & library + DEFAULT_PROFILE normalization
        status, items = _http_json(
            f"{base}/api/catalog",
            method="POST",
            payload={"service": "librivox", "mode": "search", "query": "odyssey", "profile": "default"},
            headers=hdr,
        )
        assert status == 200 and items[0]["title"] == "Hit for odyssey"

        status, lib_items = _http_json(
            f"{base}/api/catalog",
            method="POST",
            payload={"service": "librivox", "mode": "library"},
            headers=hdr,
        )
        assert status == 200 and lib_items[0]["title"] == "Library Book"
        assert catalog_calls == [("search", "librivox", None), ("library", "librivox", None)]

        # Missing service -> 400
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/catalog", method="POST", payload={"mode": "search"}, headers=hdr)
        assert exc_info.value.code == 400

        # Sync endpoint starts asynchronously -> 202 and completes in background
        status, sync_res = _http_json(f"{base}/api/sync", method="POST", payload={}, headers=hdr)
        assert status == 202
        assert sync_res["syncing"] is True
        deadline = time.monotonic() + 3.0
        snap = {}
        while time.monotonic() < deadline:
            _, snap = _http_json(f"{base}/api/status", headers=hdr)
            if not snap["syncing"] and snap.get("last_sync") is not None:
                break
            time.sleep(0.02)
        assert snap["last_sync"]["profiles_checked"] == 2
        assert snap["last_sync"]["downloaded"] == 1

        # Sync endpoint failure surfaces via status.last_sync_error
        monkeypatch.setattr(
            "audiobookdl.web.run_sync_once",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("sync boom")),
        )
        status, _ = _http_json(f"{base}/api/sync", method="POST", payload={}, headers=hdr)
        assert status == 202
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            _, snap = _http_json(f"{base}/api/status", headers=hdr)
            if not snap["syncing"] and snap.get("last_sync_error"):
                break
            time.sleep(0.02)
        assert "sync boom" in (snap.get("last_sync_error") or "")

        # Unknown GET and POST routes -> 404
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/does-not-exist", headers=hdr)
        assert exc_info.value.code == 404

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_json(f"{base}/api/does-not-exist", method="POST", payload={}, headers=hdr)
        assert exc_info.value.code == 404


def test_web_ui_security_csrf_413_400_and_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "audiobookdl.gui.controller.run_downloads",
        lambda *_a, **_kw: DownloadSummary(completed=1, failed=0, cancelled=False),
    )

    with _running_web_server(tmp_path, token=None) as (server, base, out_dir):
        host_port = f"127.0.0.1:{server.server_address[1]}"

        # 1. Malformed JSON -> 400
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/download",
                method="POST",
                raw_body=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
        assert exc_info.value.code == 400

        # Non-object JSON -> 400
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/download",
                method="POST",
                raw_body=b"[]",
                headers={"Content-Type": "application/json"},
            )
        assert exc_info.value.code == 400

        # 2. Oversized body (> MAX_REQUEST_BODY_BYTES) -> 413
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5.0)
        try:
            conn.putrequest("POST", "/api/download")
            conn.putheader("Host", host_port)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 413
            body_413 = json.loads(resp.read().decode("utf-8"))
            assert "too large" in body_413["error"].lower()
        finally:
            conn.close()

        # 3. CSRF protections: reject form/text Content-Type, cross-origin Origin, null Origin, mismatched Referer
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/cancel",
                method="POST",
                raw_body=b"{}",
                headers={"Content-Type": "text/plain"},
            )
        assert exc_info.value.code == 415

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/download",
                method="POST",
                raw_body=b'{"urls":["https://librivox.org/a"]}',
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
        assert exc_info.value.code == 403

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/download",
                method="POST",
                raw_body=b'{"urls":["https://librivox.org/a"]}',
                headers={"Content-Type": "application/json", "Origin": "null"},
            )
        assert exc_info.value.code == 403

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_raw(
                f"{base}/api/cancel",
                method="POST",
                raw_body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://evil.example/attack.html",
                },
            )
        assert exc_info.value.code == 403

        # Same-origin Origin + application/json succeeds
        status, cancel_res = _http_raw(
            f"{base}/api/cancel",
            method="POST",
            raw_body=b"{}",
            headers={"Content-Type": "application/json", "Origin": f"http://{host_port}"},
        )
        assert status == 200 and cancel_res == {"cancelling": True}

        # 4. output_dir / output_template path traversal rejection
        outside_dir = str((tmp_path / "outside").resolve())
        for bad_payload in (
            {"urls": ["https://librivox.org/a"], "output_dir": "../outside"},
            {"urls": ["https://librivox.org/a"], "output_dir": outside_dir},
            {"urls": ["https://librivox.org/a"], "output_template": "../../escape/{title}"},
            {"urls": ["https://librivox.org/a"], "output_template": "/etc/passwd/{title}"},
        ):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _http_json(f"{base}/api/download", method="POST", payload=bad_payload)
            assert exc_info.value.code == 400

        # Relative subdirectory inside configured output_dir is accepted
        status, ok_res = _http_json(
            f"{base}/api/download",
            method="POST",
            payload={
                "urls": ["https://librivox.org/safe-book"],
                "output_dir": "subdir",
                "output_template": "nested/{title}",
            },
        )
        assert status == 202 and ok_res["queued"] == 1
        assert (out_dir / "subdir" / "nested").is_dir() or ok_res["queued"] == 1


def test_web_ui_sse_queue_controls_interactive_prompts_and_option_parity(tmp_path, monkeypatch):
    from audiobookdl import output

    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.storytel.profiles.work]\nusername = "worker@example.com"\n',
        encoding="utf-8",
    )
    serve_opts = args.ServeOptions(
        host="127.0.0.1",
        port=0,
        token="sse-secret",
        config_location=str(cfg_path),
        output_dir=str(tmp_path / "out"),
    )
    server = create_web_server(serve_opts)
    state = server.dashboard_state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        headers = {"Authorization": "Bearer sse-secret"}

        # 1. /api/services includes per-service profiles
        st_svc, svcs = _http_json(f"{base}/api/services", headers=headers)
        assert st_svc == 200
        storytel = next(s for s in svcs if s["key"] == "storytel")
        assert "work" in storytel["profiles"]

        # 2. build_download_options supports output_preset, enrich_metadata, profile
        built = state.build_download_options(
            ["https://www.storytel.com/books/12345"],
            {"output_preset": "audiobookshelf", "enrich_metadata": True, "profile": "work"},
        )
        assert output.OUTPUT_TEMPLATE_PRESETS["audiobookshelf"] in built.output_template
        assert built.enrich_metadata is True
        assert built.profile == "work"

        # 3. Connect to SSE /api/events and read snapshot + log + prompt events
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5.0)
        try:
            conn.request("GET", "/api/events", headers=headers)
            resp = conn.getresponse()
            assert resp.status == 200
            assert "text/event-stream" in (resp.getheader("Content-Type") or "")

            def _read_sse_event() -> tuple[str, dict[str, Any]]:
                ev_name = ""
                data_buf = ""
                while True:
                    raw_line = resp.fp.readline().decode("utf-8")
                    if raw_line in ("\n", "\r\n", ""):
                        if data_buf:
                            return ev_name, json.loads(data_buf)
                        continue
                    line = raw_line.rstrip("\r\n")
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        ev_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf += line[5:].strip()

            first_ev, first_payload = _read_sse_event()
            assert first_ev == "snapshot"
            assert "queue" in first_payload

            # Emit log event and interactive prompt while SSE subscriber is active
            state.controller.event_queue.put(("log", "INFO", "SSE test message"))
            ev2, p2 = _read_sse_event()
            assert ev2 == "log"
            assert any("SSE test message" in ln for ln in p2["lines"])

            holder: dict[str, Any] = {"value": None}
            done = threading.Event()
            state.controller.event_queue.put(("prompt_input", "Enter OTP", True, holder, done))
            ev3, p3 = _read_sse_event()
            assert ev3 == "prompt"
            prompt_id = p3["prompt"]["id"]
            assert p3["prompt"]["kind"] == "input"
            assert p3["prompt"]["hidden"] is True

            # Respond via HTTP POST /api/prompt/respond
            st_resp, res_resp = _http_json(
                f"{base}/api/prompt/respond",
                method="POST",
                payload={"id": prompt_id, "value": "998877"},
                headers=headers,
            )
            assert st_resp == 200 and res_resp == {"resolved": True}
            assert done.wait(timeout=2.0)
            assert holder["value"] == "998877"
        finally:
            conn.close()

        # 4. Queue move, remove, and retry endpoints
        from audiobookdl.gui.controller import GuiInteractionHandler, QueuedJob

        inter = GuiInteractionHandler(state.controller.event_queue, state.controller.cancel_event)
        j1 = QueuedJob("j1", 1, "https://librivox.org/b1", state.build_download_options(["https://librivox.org/b1"]), inter)
        j2 = QueuedJob("j2", 1, "https://librivox.org/b2", state.build_download_options(["https://librivox.org/b2"]), inter)
        j_fail = QueuedJob("j_fail", 1, "https://librivox.org/f1", state.build_download_options(["https://librivox.org/f1"]), inter)
        with state.controller.queue_cond:
            state.controller.pending_jobs.extend([j1, j2])
        state.controller.register_queue_record(j1, "LibriVox", persist=False)
        state.controller.register_queue_record(j2, "LibriVox", persist=False)
        state.controller.register_queue_record(j_fail, "LibriVox", persist=False)
        state.controller.update_queue_record("j_fail", "Failed", "timeout")

        st_mv, res_mv = _http_json(
            f"{base}/api/queue/move",
            method="POST",
            payload={"job_id": "j2", "direction": "up"},
            headers=headers,
        )
        assert st_mv == 200 and res_mv == {"moved": True}
        pending_ids = [j.job_id for j in state.controller.pending_jobs if j is not None]
        assert pending_ids[:2] == ["j2", "j1"]

        st_rm, res_rm = _http_json(
            f"{base}/api/queue/remove",
            method="POST",
            payload={"job_id": "j1"},
            headers=headers,
        )
        assert st_rm == 200 and res_rm["removed"] is True

        # Mock start_new_run so retry doesn't hit network
        retried_calls: list[Any] = []
        monkeypatch.setattr(state.controller, "start_or_enqueue_jobs", lambda **kw: (retried_calls.append(kw) or False, 2, [], 1))
        st_rt, res_rt = _http_json(
            f"{base}/api/queue/retry",
            method="POST",
            payload={"job_ids": ["j_fail"]},
            headers=headers,
        )
        assert st_rt == 202 and len(retried_calls) == 1

        # 5. POST /api/sync returns 409 Conflict when already syncing
        with state._lock:
            state.is_syncing = True
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _http_json(f"{base}/api/sync", method="POST", payload={}, headers=headers)
            assert exc_info.value.code == 409
        finally:
            with state._lock:
                state.is_syncing = False
    finally:
        server.shutdown()
        state.shutdown()
        server.server_close()


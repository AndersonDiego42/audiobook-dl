"""Lightweight headless HTTP Web UI dashboard & JSON API powered by `GuiController`."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from audiobookdl import __version__, args
from audiobookdl.config import DEFAULT_PROFILE
from audiobookdl.gui.controller import GuiController
from audiobookdl.pipeline import list_service_library, search_service
from audiobookdl.sources import detect_service_key, get_available_services
from audiobookdl.sync import run_sync_once
from audiobookdl.utils.archive import DownloadArchive, default_archive_path

MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_WEB_LOG_LINES = 400


class WebDashboardState:
    """Thread-safe headless state bridge around `GuiController` for HTTP/JSON handlers."""

    def __init__(self, serve_options: args.ServeOptions) -> None:
        self.options = serve_options
        self.controller = GuiController(serve_options.config_location)
        self.token: Optional[str] = serve_options.token
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self.is_running = False
        self.status_text = "Ready"
        self.stage_text = ""
        self.progress_pct = 0.0
        self.speed_bps = 0.0
        self.eta_seconds: Optional[float] = None
        self.logs: Deque[str] = deque(maxlen=MAX_WEB_LOG_LINES)
        self._services = get_available_services()
        self._services_by_key = {s.key: s for s in self._services}

        # Restore persisted unresolved queue entries so status reflects prior state.
        self.controller.load_saved_queue_records()
        self._pump_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._pump_thread.start()

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self.controller.drain_events(self._handle_event, max_batch=200)
            self._stop_event.wait(0.02 if processed >= 200 else 0.1)

    def _handle_event(self, item: tuple) -> None:
        kind = item[0]
        with self._lock:
            if kind == "log":
                _, level, message = item
                prefix = f"[{level}] " if level in ("DEBUG", "ERROR") else ""
                for line in f"{prefix}{message}".splitlines():
                    self.logs.append(line)
                if level == "INFO" and message.strip():
                    self.status_text = message.strip().splitlines()[0][:100]
            elif kind == "batch_item":
                self.stage_text = "Fetching metadata"
            elif kind == "job_status":
                _, job_id, status_val, detail = item
                self.controller.update_queue_record(job_id, status_val, detail)
            elif kind == "progress_start":
                _, description, _total = item
                self.progress_pct = 0.0
                self.speed_bps = 0.0
                self.eta_seconds = None
                self.stage_text = "Downloading"
                self.status_text = str(description)[:100]
            elif kind == "progress_advance":
                _, completed, total = item
                pct = (completed / total * 100.0) if total > 0 else 0.0
                self.progress_pct = min(100.0, max(0.0, pct))
            elif kind == "progress_metrics":
                _, completed, total, _bytes_dl, speed_bps, _elapsed, eta = item
                pct = (completed / total * 100.0) if total > 0 else 0.0
                self.progress_pct = min(100.0, max(0.0, pct))
                self.speed_bps = float(speed_bps or 0.0)
                self.eta_seconds = eta
            elif kind == "progress_finish":
                self.progress_pct = 100.0
                self.eta_seconds = 0.0
            elif kind in ("prompt_input", "prompt_confirm", "prompt_choice"):
                # Headless web server has no synchronous modal dialog; unblock worker cleanly.
                holder, done = item[-2], item[-1]
                holder["value"] = False if kind == "prompt_confirm" else None
                done.set()
            elif kind == "done":
                _, _run_id, summary, exc = item
                self.is_running = False
                self.stage_text = ""
                self.controller.clear_seen_urls()
                if exc is not None:
                    self.progress_pct = 0.0
                    self.status_text = f"Error: {exc}"
                elif summary is None or summary.cancelled:
                    self.progress_pct = 0.0
                    self.status_text = "Cancelled"
                elif summary.failed > 0 and summary.completed == 0:
                    self.progress_pct = 0.0
                    self.status_text = "Failed"
                elif summary.failed > 0:
                    self.progress_pct = 100.0
                    self.status_text = f"Done ({summary.completed} ok, {summary.failed} failed)"
                else:
                    self.progress_pct = 100.0
                    self.status_text = "Completed"

    def shutdown(self) -> None:
        self._stop_event.set()
        self.controller.shutdown(is_running=self.is_running, is_previewing=False)
        if self._pump_thread.is_alive():
            self._pump_thread.join(timeout=2.0)

    def archive_path(self) -> str:
        cfg = self.controller.config_snapshot()
        if cfg.download_archive:
            return cfg.download_archive
        db_dir = cfg.database_directory or args.default_database_directory()
        return default_archive_path(db_dir)

    def build_download_options(
        self,
        urls: Sequence[str],
        overrides: Optional[Dict[str, Any]] = None,
    ) -> args.DownloadOptions:
        cfg = self.controller.config_snapshot()
        raw_ov = overrides or {}
        base_dir = str(
            raw_ov.get("output_dir")
            or self.options.output_dir
            or cfg.gui.output_dir
            or os.getcwd()
        ).strip()
        tpl = str(
            raw_ov.get("output_template")
            or self.options.output_template
            or cfg.gui.output_template
            or cfg.output_template
            or "{title}"
        ).strip()
        output_template = os.path.join(base_dir, tpl) if base_dir else tpl
        fmt = raw_ov.get("output_format") or self.options.output_format or cfg.output_format
        combine = (
            bool(raw_ov["combine"])
            if "combine" in raw_ov and raw_ov["combine"] is not None
            else (cfg.combine if cfg.combine is not None else (fmt == "m4b"))
        )
        return args.build_default_options(
            urls=list(urls),
            output_template=output_template,
            output_format=fmt,
            combine=combine,
            download_archive=self.archive_path(),
            config_location=self.options.config_location,
            profile=(str(raw_ov["profile"]).strip() or None) if raw_ov.get("profile") else None,
        )

    def service_label_for_url(self, url: str) -> str:
        key = detect_service_key(url)
        if key and key in self._services_by_key:
            return self._services_by_key[key].label
        return key or "Unknown"

    def queue_urls(self, urls: Sequence[str], payload: Dict[str, Any]) -> Tuple[int, int]:
        unique_urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        unique_urls = list(dict.fromkeys(unique_urls))
        if not unique_urls:
            raise ValueError("At least one valid URL is required")

        # Hold `_lock` while starting/enqueuing and registering records so `_event_loop`
        # cannot process `job_status`/`done` events before the records exist in `queue_records`.
        with self._lock:
            if self.is_running:
                has_run, added, new_total = self.controller.enqueue_additional_urls(
                    unique_urls,
                    lambda u: self.build_download_options([u], payload),
                )
                if has_run:
                    for job in added:
                        self.controller.register_queue_record(job, self.service_label_for_url(job.url))
                    return len(added), new_total

            job_specs = [
                (u, self.build_download_options([u], payload), None, None)
                for u in unique_urls
            ]
            _run_id, queued_jobs = self.controller.start_new_run(job_specs)
            self.is_running = True
            self.status_text = "Starting..."
            self.stage_text = "Preparing"
            self.progress_pct = 0.0
            for job in queued_jobs:
                self.controller.register_queue_record(job, self.service_label_for_url(job.url))
            return len(queued_jobs), len(queued_jobs)

    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            with self.controller.queue_lock:
                records = [dict(rec) for rec in self.controller.queue_records.values()]
            return {
                "version": __version__,
                "running": self.is_running,
                "paused": self.controller.is_paused(),
                "status": self.status_text,
                "stage": self.stage_text,
                "progress_pct": round(self.progress_pct, 1),
                "speed_bps": self.speed_bps,
                "eta_seconds": self.eta_seconds,
                "queue": records,
                "logs": list(self.logs),
            }


def _render_dashboard_html(requires_token: bool) -> str:
    token_banner = (
        '<div class="card"><label for="api-token">API Token:</label> '
        '<input id="api-token" type="password" placeholder="Enter bearer token">'
        '<button type="button" onclick="saveToken()">Unlock</button></div>'
        if requires_token
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>audiobook-dl Web Dashboard v{__version__}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f172a; --card:#1e293b; --border:#334155; --fg:#f8fafc; --muted:#94a3b8; --accent:#38bdf8; --ok:#22c55e; --err:#ef4444; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--fg); padding:20px; line-height:1.4; }}
    .wrap {{ max-width:1040px; margin:0 auto; display:grid; gap:16px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
    h1,h2 {{ margin:0 0 10px 0; font-weight:600; }}
    h1 {{ font-size:1.35rem; display:flex; justify-content:space-between; align-items:center; }}
    h2 {{ font-size:1.05rem; }}
    textarea,input,select {{ background:#0b1220; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:8px 10px; font:inherit; }}
    textarea {{ width:100%; min-height:76px; resize:vertical; }}
    .row {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px; }}
    button {{ background:var(--accent); color:#04111d; border:none; border-radius:6px; padding:8px 14px; font-weight:600; cursor:pointer; }}
    button.secondary {{ background:#334155; color:var(--fg); }}
    button.danger {{ background:var(--err); color:#fff; }}
    progress {{ width:100%; height:14px; margin-top:8px; }}
    .muted {{ color:var(--muted); font-size:0.9rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.9rem; margin-top:8px; }}
    th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }}
    pre {{ background:#0b1220; border:1px solid var(--border); border-radius:6px; padding:10px; max-height:200px; overflow:auto; font-size:0.82rem; margin:8px 0 0; }}
    .msg {{ margin-top:8px; font-size:0.9rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1><span>audiobook-dl Web UI <small class="muted">v{__version__}</small></span>
      <button type="button" class="secondary" onclick="triggerSync()">Sync Libraries</button></h1>
    {token_banner}
    <div class="card">
      <h2>Queue Download</h2>
      <label for="urls-input" class="muted">Audiobook, series, archive.org, or RSS feed URLs (one per line):</label>
      <textarea id="urls-input" placeholder="https://librivox.org/..."></textarea>
      <div class="row">
        <label for="fmt-select">Format:</label>
        <select id="fmt-select"><option value="">Original</option><option value="m4b" selected>.m4b</option><option value="mp3">.mp3</option></select>
        <label><input type="checkbox" id="combine-cb" checked> Combine parts</label>
        <button type="button" onclick="submitUrls()">Download / Queue</button>
        <button type="button" class="secondary" id="pause-btn" onclick="togglePause()">Pause</button>
        <button type="button" class="danger" onclick="cancelRun()">Cancel</button>
      </div>
      <div id="submit-msg" class="msg muted" aria-live="polite"></div>
    </div>
    <div class="card">
      <h2>Status &amp; Progress</h2>
      <div id="status-line">Ready</div>
      <div id="stage-line" class="muted"></div>
      <progress id="prog-bar" max="100" value="0"></progress>
      <table><thead><tr><th>Service</th><th>URL / Book</th><th>Status</th><th>Detail</th></tr></thead><tbody id="queue-body"></tbody></table>
      <pre id="logs-box"></pre>
    </div>
    <div class="card">
      <h2>Catalog Search &amp; Library</h2>
      <div class="row">
        <select id="svc-select" aria-label="Service"></select>
        <input id="cat-query" type="text" placeholder="Search query (optional for library)">
        <button type="button" onclick="browseCatalog('search')">Search</button>
        <button type="button" class="secondary" onclick="browseCatalog('library')">List Library</button>
      </div>
      <table><thead><tr><th>Title</th><th>Author</th><th>Action</th></tr></thead><tbody id="cat-body"></tbody></table>
    </div>
    <div class="card">
      <h2>Downloaded Archive <button type="button" class="secondary" onclick="refreshLibrary()">Refresh</button></h2>
      <table><thead><tr><th>Service</th><th>Title</th><th>Author</th><th>Status</th></tr></thead><tbody id="lib-body"></tbody></table>
    </div>
  </div>
  <script>
    function authHeaders() {{
      const tok = sessionStorage.getItem('abdl_token') || '';
      return tok ? {{ 'Authorization': 'Bearer ' + tok }} : {{}};
    }}
    function saveToken() {{
      const el = document.getElementById('api-token');
      if (el) sessionStorage.setItem('abdl_token', el.value.trim());
      refreshAll();
    }}
    async function apiFetch(url, opts = {{}}) {{
      opts.headers = Object.assign({{'Content-Type': 'application/json'}}, authHeaders(), opts.headers || {{}});
      const res = await fetch(url, opts);
      const data = await res.json().catch(() => ({{}}));
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }}
    async function submitUrls() {{
      const raw = document.getElementById('urls-input').value;
      const urls = raw.split(/\\s+/).map(s => s.trim()).filter(Boolean);
      const msg = document.getElementById('submit-msg');
      if (!urls.length) {{ msg.textContent = 'Please enter at least one URL.'; return; }}
      try {{
        const data = await apiFetch('/api/download', {{
          method: 'POST',
          body: JSON.stringify({{
            urls,
            output_format: document.getElementById('fmt-select').value || null,
            combine: document.getElementById('combine-cb').checked,
          }}),
        }});
        msg.textContent = 'Queued ' + data.queued + ' job(s) (total ' + data.total + ').';
        refreshStatus();
      }} catch (e) {{ msg.textContent = 'Error: ' + e.message; }}
    }}
    async function togglePause() {{ await apiFetch('/api/pause', {{method: 'POST'}}); refreshStatus(); }}
    async function cancelRun() {{ await apiFetch('/api/cancel', {{method: 'POST'}}); refreshStatus(); }}
    async function triggerSync() {{
      const msg = document.getElementById('submit-msg');
      msg.textContent = 'Running library sync...';
      try {{
        const res = await apiFetch('/api/sync', {{method: 'POST', body: '{{}}'}});
        msg.textContent = 'Sync done: ' + JSON.stringify(res);
        refreshLibrary();
      }} catch (e) {{ msg.textContent = 'Sync failed: ' + e.message; }}
    }}
    async function loadServices() {{
      try {{
        const svcs = await apiFetch('/api/services');
        const sel = document.getElementById('svc-select');
        sel.innerHTML = svcs.map(s => `<option value="${{s.key}}">${{s.label}}</option>`).join('');
      }} catch (_) {{}}
    }}
    async function browseCatalog(mode) {{
      const svc = document.getElementById('svc-select').value;
      const query = document.getElementById('cat-query').value;
      const body = document.getElementById('cat-body');
      body.innerHTML = '<tr><td colspan="3" class="muted">Loading...</td></tr>';
      try {{
        const items = await apiFetch('/api/catalog', {{method: 'POST', body: JSON.stringify({{service: svc, mode, query}})}});
        if (!items.length) {{ body.innerHTML = '<tr><td colspan="3" class="muted">No items found.</td></tr>'; return; }}
        body.innerHTML = items.map(b => `<tr><td>${{escapeHtml(b.title)}}</td><td>${{escapeHtml(b.author || '')}}</td><td><button type="button" onclick="queueSingle('${{escapeAttr(b.url)}}')">Queue</button></td></tr>`).join('');
      }} catch (e) {{ body.innerHTML = `<tr><td colspan="3" class="muted">Error: ${{escapeHtml(e.message)}}</td></tr>`; }}
    }}
    async function queueSingle(url) {{
      await apiFetch('/api/download', {{method: 'POST', body: JSON.stringify({{urls: [url], output_format: document.getElementById('fmt-select').value || null, combine: document.getElementById('combine-cb').checked}})}});
      refreshStatus();
    }}
    async function refreshStatus() {{
      try {{
        const s = await apiFetch('/api/status');
        document.getElementById('status-line').textContent = s.status + (s.running ? ` (${{s.progress_pct}}%)` : '');
        document.getElementById('stage-line').textContent = s.stage ? ('Stage: ' + s.stage) : '';
        document.getElementById('prog-bar').value = s.progress_pct || 0;
        document.getElementById('pause-btn').textContent = s.paused ? 'Resume' : 'Pause';
        const qBody = document.getElementById('queue-body');
        qBody.innerHTML = s.queue.length
          ? s.queue.map(q => `<tr><td>${{escapeHtml(q.service)}}</td><td>${{escapeHtml(q.url)}}</td><td>${{escapeHtml(q.status)}}</td><td>${{escapeHtml(q.detail || '')}}</td></tr>`).join('')
          : '<tr><td colspan="4" class="muted">Queue is empty.</td></tr>';
        document.getElementById('logs-box').textContent = (s.logs || []).join('\\n');
      }} catch (_) {{}}
    }}
    async function refreshLibrary() {{
      try {{
        const items = await apiFetch('/api/library?limit=50');
        const body = document.getElementById('lib-body');
        body.innerHTML = items.length
          ? items.map(e => `<tr><td>${{escapeHtml(e.source)}}</td><td>${{escapeHtml(e.title)}}</td><td>${{escapeHtml(e.author || '')}}</td><td>${{escapeHtml(e.status)}}</td></tr>`).join('')
          : '<tr><td colspan="4" class="muted">No archive entries recorded.</td></tr>';
      }} catch (_) {{}}
    }}
    function escapeHtml(s) {{ return String(s).replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[c])); }}
    function escapeAttr(s) {{ return escapeHtml(s); }}
    function refreshAll() {{ loadServices(); refreshStatus(); refreshLibrary(); }}
    refreshAll();
    setInterval(refreshStatus, 1500);
  </script>
</body>
</html>"""


class _WebRequestHandler(BaseHTTPRequestHandler):
    """HTTP request router for the headless Web UI & JSON endpoints."""

    server_version = f"audiobook-dl/{__version__}"

    @property
    def state(self) -> WebDashboardState:
        return self.server.dashboard_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy per-poll stdout/stderr HTTP logs on headless terminals.
        return

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        expected = self.state.token
        if not expected:
            return True
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
            if hmac.compare_digest(provided, expected):
                return True
        alt = (self.headers.get("X-API-Token") or "").strip()
        return bool(alt and hmac.compare_digest(alt, expected))

    def _read_json_body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length") or "0"
        if not raw_len.strip().isdigit():
            raise ValueError("Invalid Content-Length header")
        length = int(raw_len.strip())
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("Request payload too large")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(HTTPStatus.OK, _render_dashboard_html(bool(self.state.token)))
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "version": __version__})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        if path == "/api/status":
            self._send_json(HTTPStatus.OK, self.state.status_snapshot())
            return
        if path == "/api/services":
            self._send_json(HTTPStatus.OK, [asdict(s) for s in get_available_services()])
            return
        if path == "/api/library":
            qs = parse_qs(parsed.query)
            query_text = (qs.get("q") or [""])[0]
            raw_limit = (qs.get("limit") or ["100"])[0]
            limit = int(raw_limit) if raw_limit.isdigit() else 100
            entries = DownloadArchive(self.state.archive_path()).list_entries(
                query=query_text,
                limit=max(1, min(500, limit)),
            )
            self._send_json(HTTPStatus.OK, [asdict(e) for e in entries])
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/download":
            try:
                urls = body.get("urls")
                if isinstance(urls, str):
                    urls = [urls]
                if not isinstance(urls, list):
                    raise ValueError("'urls' must be a list of URL strings")
                queued, total = self.state.queue_urls(urls, body)
                self._send_json(HTTPStatus.ACCEPTED, {"queued": queued, "total": total})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/pause":
            paused = self.state.controller.toggle_pause()
            self._send_json(HTTPStatus.OK, {"paused": paused})
            return

        if path == "/api/cancel":
            self.state.controller.cancel_active_run()
            self._send_json(HTTPStatus.OK, {"cancelling": True})
            return

        if path == "/api/queue/clear":
            cleared = self.state.controller.clear_completed_records(
                list(self.state.controller.queue_records.keys())
            )
            self._send_json(HTTPStatus.OK, {"cleared": len(cleared)})
            return

        if path == "/api/catalog":
            service_key = str(body.get("service") or "").strip()
            mode = str(body.get("mode") or "search").strip().lower()
            query_text = str(body.get("query") or "")
            if not service_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'service' is required"})
                return
            cfg = self.state.controller.config_snapshot()
            opts = self.state.build_download_options([], body)
            if body.get("profile") == DEFAULT_PROFILE:
                opts.profile = None
            try:
                if mode == "library":
                    books = list_service_library(service_key, opts, cfg)
                else:
                    books = search_service(service_key, query_text, opts, cfg)
                self._send_json(HTTPStatus.OK, [asdict(b) for b in books])
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/sync":
            cfg = self.state.controller.config_snapshot()
            sync_opts = args.SyncOptions(
                output_template=self.state.options.output_template,
                output_format=self.state.options.output_format,
                config_location=self.state.options.config_location,
            )
            try:
                summary = run_sync_once(sync_opts, config=cfg)
                self._send_json(HTTPStatus.OK, summary.to_dict())
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})


def create_web_server(serve_options: args.ServeOptions) -> ThreadingHTTPServer:
    """Create and bind a headless Web UI `ThreadingHTTPServer`."""
    state = WebDashboardState(serve_options)
    server = ThreadingHTTPServer((serve_options.host, serve_options.port), _WebRequestHandler)
    server.dashboard_state = state  # type: ignore[attr-defined]
    return server


def serve_web_ui(serve_options: args.ServeOptions) -> int:
    """Run the headless Web UI HTTP server until interrupted."""
    server = create_web_server(serve_options)
    bound_host, bound_port = server.server_address[:2]
    print(f"audiobook-dl Web UI listening on http://{bound_host}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.dashboard_state.shutdown()  # type: ignore[attr-defined]
        server.server_close()
    return 0

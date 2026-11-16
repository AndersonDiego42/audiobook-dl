"""Local deterministic HTTP origin and proxy server for E2E tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from tests.e2e.diagnostics import E2EDiagnosticRecorder


_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


@dataclass(frozen=True)
class RouteRequest:
    """Normalized incoming HTTP request passed to dynamic route handlers."""

    method: str
    raw_path: str
    host: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class RouteResponse:
    """HTTP response descriptor returned by static or dynamic routes."""

    body: bytes
    content_type: str = "application/octet-stream"
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    support_range: bool = True


RouteHandler = Callable[[RouteRequest], RouteResponse]


@dataclass(frozen=True)
class RecordedRequest:
    """Thread-safe snapshot of a handled HTTP request."""

    method: str
    raw_path: str
    host: str
    path: str
    status_code: int
    headers: dict[str, str]
    body: bytes


class LocalAudiobookServer:
    """
    Thread-safe local HTTP server supporting both direct origin URLs and HTTP proxy requests.

    Routes can be keyed by `(method, host, path)` or `(method, None, path)`.
    Static byte routes automatically honor HTTP `Range` requests (`206` / `416`)
    for deterministic `--resume` testing.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        diagnostics: E2EDiagnosticRecorder | None = None,
    ) -> None:
        self.host = host
        self._diagnostics = diagnostics
        self._lock = threading.Lock()
        self._routes: dict[tuple[str, str | None, str], RouteHandler] = {}
        self._recorded: list[RecordedRequest] = []

        parent = self

        class _RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parent._dispatch(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                parent._dispatch(self, "POST")

            def do_HEAD(self) -> None:  # noqa: N802
                parent._dispatch(self, "HEAD")

        self._httpd = ThreadingHTTPServer((host, port), _RequestHandler)
        self.port: int = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="e2e-local-http-server",
            daemon=True,
        )

    @property
    def url(self) -> str:
        """Base HTTP URL of the running server."""
        return f"http://{self.host}:{self.port}"

    def start(self, ready_timeout: float = 5.0) -> LocalAudiobookServer:
        """Start serving in the background and block until `/__health` responds 200."""
        self._thread.start()
        self.wait_until_ready(timeout=ready_timeout)
        return self

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Poll the server's `/__health` endpoint over TCP until healthy or timeout expires."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            conn: http.client.HTTPConnection | None = None
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=1.0)
                conn.request("GET", "/__health")
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 200 and b"ok" in body:
                    return
            except OSError as exc:
                last_error = exc
            finally:
                if conn is not None:
                    conn.close()
            time.sleep(0.02)
        raise TimeoutError(
            f"LocalAudiobookServer at {self.url} failed readiness probe within {timeout}s: {last_error}"
        )

    def close(self) -> None:
        """Shut down the server socket and join the background thread."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> LocalAudiobookServer:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def add_route(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        host: str | None = None,
        support_range: bool = True,
    ) -> None:
        """Register a deterministic static byte response."""
        response = RouteResponse(
            body=body,
            content_type=content_type,
            status_code=status_code,
            headers=dict(headers or {}),
            support_range=support_range,
        )
        self.add_handler(method, path, lambda _req: response, host=host)

    def add_json(
        self,
        path: str,
        payload: object,
        *,
        status_code: int = 200,
        method: str = "GET",
        host: str | None = None,
    ) -> None:
        """Register a static JSON response."""
        self.add_route(
            path,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            status_code=status_code,
            method=method,
            host=host,
            support_range=False,
        )

    def add_handler(
        self,
        method: str,
        path: str,
        handler: RouteHandler,
        *,
        host: str | None = None,
    ) -> None:
        """Register a dynamic handler callable for `(method, host, path)`."""
        with self._lock:
            self._routes[(method.upper(), host.lower() if host else None, path)] = handler

    def clear_routes(self) -> None:
        """Remove all registered routes and clear recorded request history."""
        with self._lock:
            self._routes.clear()
            self._recorded.clear()

    def recorded_requests(self) -> list[RecordedRequest]:
        """Return a snapshot list of all requests handled so far."""
        with self._lock:
            return list(self._recorded)

    def _lookup_handler(self, method: str, host: str, path: str) -> RouteHandler | None:
        with self._lock:
            exact = self._routes.get((method, host.lower(), path))
            if exact is not None:
                return exact
            return self._routes.get((method, None, path))

    def _record(
        self,
        req: RouteRequest,
        status_code: int,
        bytes_sent: int,
    ) -> None:
        rec = RecordedRequest(
            method=req.method,
            raw_path=req.raw_path,
            host=req.host,
            path=req.path,
            status_code=status_code,
            headers=dict(req.headers),
            body=req.body,
        )
        with self._lock:
            self._recorded.append(rec)
        if self._diagnostics is not None and req.path != "/__health":
            self._diagnostics.record_http_exchange(
                method=req.method,
                raw_path=req.raw_path,
                normalized_path=req.path,
                status_code=status_code,
                bytes_sent=bytes_sent,
                headers=req.headers,
            )

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlsplit(handler.path)
        req_headers = {k: v for k, v in handler.headers.items()}
        host_header = req_headers.get("Host", "")
        host = (parsed.hostname or host_header.split(":", 1)[0] or self.host).lower()
        path = parsed.path or "/"

        content_length = int(req_headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(content_length) if content_length > 0 else b""
        req = RouteRequest(
            method=method.upper(),
            raw_path=handler.path,
            host=host,
            path=path,
            query=parsed.query,
            headers=req_headers,
            body=body,
        )

        if path == "/__health":
            payload = b'{"status":"ok"}'
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            if method != "HEAD":
                handler.wfile.write(payload)
            return

        route_fn = self._lookup_handler(method.upper(), host, path)
        if route_fn is None:
            not_found = b"not found"
            handler.send_response(404)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(not_found)))
            handler.end_headers()
            if method != "HEAD":
                handler.wfile.write(not_found)
            self._record(req, 404, len(not_found))
            return

        resp = route_fn(req)
        payload = resp.body
        status = resp.status_code
        out_headers = dict(resp.headers)

        range_header = req_headers.get("Range") or req_headers.get("range")
        if resp.support_range and status == 200 and range_header:
            match = _RANGE_RE.fullmatch(range_header.strip())
            if match:
                total = len(payload)
                start = int(match.group(1))
                end_raw = match.group(2)
                end = int(end_raw) if end_raw else (total - 1)
                if start >= total or start > end:
                    status = 416
                    out_headers["Content-Range"] = f"bytes */{total}"
                    payload = b""
                else:
                    end = min(end, total - 1)
                    payload = payload[start : end + 1]
                    status = 206
                    out_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
                    out_headers["Accept-Ranges"] = "bytes"

        handler.send_response(status)
        handler.send_header("Content-Type", resp.content_type)
        handler.send_header("Content-Length", str(len(payload)))
        for h_key, h_val in out_headers.items():
            handler.send_header(h_key, h_val)
        handler.end_headers()
        bytes_written = 0
        if method != "HEAD" and payload:
            handler.wfile.write(payload)
            bytes_written = len(payload)
        self._record(req, status, bytes_written)

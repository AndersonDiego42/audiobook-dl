"""OAuth 2.0 authentication helpers (PKCE loopback, Device Authorization, and token refresh)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests

from audiobookdl import logging
from audiobookdl.exceptions import DownloadCancelled, RequestError, UserNotAuthorized
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    compute_retry_delay,
    create_configured_http_session,
    is_transient_network_error,
    sleep_or_cancel,
    warn_if_insecure_token_url,
)


_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Endpoint and client metadata describing an OAuth 2.0 provider."""

    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str | None = None
    scopes: tuple[str, ...] = ()
    use_pkce: bool = True
    device_authorization_url: str | None = None
    revoke_url: str | None = None
    auth_scheme: str = "Bearer"
    callback_host: str = "127.0.0.1"
    callback_path: str = "/callback"
    extra_auth_params: Mapping[str, str] = field(default_factory=dict)
    extra_token_params: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 180.0

    def with_overrides(
        self,
        *,
        client_id: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
    ) -> OAuthProviderConfig:
        """Return a copy overriding `client_id` or `scopes` when non-empty values are supplied."""
        resolved_client_id = client_id.strip() if isinstance(client_id, str) and client_id.strip() else self.client_id
        resolved_scopes = (
            tuple(s.strip() for s in scopes if isinstance(s, str) and s.strip())
            if scopes is not None
            else self.scopes
        )
        if resolved_client_id == self.client_id and resolved_scopes == self.scopes:
            return self
        return OAuthProviderConfig(
            authorization_url=self.authorization_url,
            token_url=self.token_url,
            client_id=resolved_client_id,
            client_secret=self.client_secret,
            scopes=resolved_scopes,
            use_pkce=self.use_pkce,
            device_authorization_url=self.device_authorization_url,
            revoke_url=self.revoke_url,
            auth_scheme=self.auth_scheme,
            callback_host=self.callback_host,
            callback_path=self.callback_path,
            extra_auth_params=dict(self.extra_auth_params),
            extra_token_params=dict(self.extra_token_params),
            timeout_seconds=self.timeout_seconds,
        )


@dataclass
class OAuthTokenBundle:
    """Serialized OAuth 2.0 token response persisted per `(service, profile)`."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None
    scope: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def can_refresh(self) -> bool:
        """Return `True` when a non-empty refresh token is available."""
        return bool(self.refresh_token and self.refresh_token.strip())

    def is_expired(
        self,
        leeway_seconds: float = 60.0,
        *,
        now: float | None = None,
    ) -> bool:
        """Return `True` when `expires_at` is set and falls within `leeway_seconds` of `now`."""
        if self.expires_at is None:
            return False
        current = time.time() if now is None else float(now)
        return (self.expires_at - max(0.0, float(leeway_seconds))) <= current

    def authorization_header(self, default_scheme: str = "Bearer") -> str:
        """Return a formatted HTTP `Authorization` header value."""
        raw_scheme = (self.token_type or default_scheme or "Bearer").strip()
        scheme = "Bearer" if raw_scheme.lower() == "bearer" else raw_scheme
        return f"{scheme} {self.access_token}"

    def to_json(self) -> str:
        """Serialize token bundle to compact JSON for storage in `CredentialStore`."""
        payload: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "extra": self.extra,
        }
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> OAuthTokenBundle | None:
        """Parse serialized token bundle JSON, returning `None` if malformed."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return None
        refresh_token = data.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            refresh_token = str(refresh_token)
        token_type = data.get("token_type")
        if not isinstance(token_type, str) or not token_type.strip():
            token_type = "Bearer"
        raw_exp = data.get("expires_at")
        expires_at: float | None = None
        if isinstance(raw_exp, (int, float)) and not isinstance(raw_exp, bool):
            expires_at = float(raw_exp)
        scope = data.get("scope")
        if scope is not None and not isinstance(scope, str):
            scope = str(scope)
        extra = data.get("extra")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token if (refresh_token and refresh_token.strip()) else None,
            token_type=token_type,
            expires_at=expires_at,
            scope=scope,
            extra=dict(extra) if isinstance(extra, dict) else {},
        )

    @classmethod
    def from_token_response(
        cls,
        payload: Mapping[str, Any],
        *,
        existing_refresh_token: str | None = None,
        now: float | None = None,
    ) -> OAuthTokenBundle:
        """Construct an `OAuthTokenBundle` from an OAuth 2.0 JSON token response."""
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            error_msg = _format_oauth_error(payload) or "OAuth token response missing access_token"
            raise UserNotAuthorized(detail=error_msg)

        raw_refresh = payload.get("refresh_token")
        refresh_token = (
            raw_refresh.strip()
            if isinstance(raw_refresh, str) and raw_refresh.strip()
            else existing_refresh_token
        )

        raw_type = payload.get("token_type")
        token_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "Bearer"

        expires_at: float | None = None
        raw_expires_in = payload.get("expires_in")
        if isinstance(raw_expires_in, str) and raw_expires_in.strip().isdigit():
            raw_expires_in = int(raw_expires_in.strip())
        if isinstance(raw_expires_in, (int, float)) and not isinstance(raw_expires_in, bool):
            base = time.time() if now is None else float(now)
            expires_at = base + max(0.0, float(raw_expires_in))
        elif isinstance(payload.get("expires_at"), (int, float)) and not isinstance(payload.get("expires_at"), bool):
            expires_at = float(payload["expires_at"])

        raw_scope = payload.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) and raw_scope.strip() else None

        known = {"access_token", "refresh_token", "token_type", "expires_in", "expires_at", "scope"}
        extra = {str(k): v for k, v in payload.items() if str(k) not in known}
        return cls(
            access_token=access_token.strip(),
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scope=scope,
            extra=extra,
        )


def _format_oauth_error(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    desc = payload.get("error_description")
    if isinstance(error, str) and error.strip():
        if isinstance(desc, str) and desc.strip():
            return f"{error.strip()}: {desc.strip()}"
        return error.strip()
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return None


def generate_code_verifier(length: int = 64) -> str:
    """Generate an RFC 7636 PKCE `code_verifier` (43-128 URL-safe characters)."""
    clamped = max(43, min(128, int(length)))
    token = secrets.token_urlsafe(clamped)
    return token[:clamped]


def build_code_challenge(verifier: str) -> str:
    """Compute the RFC 7636 S256 `code_challenge` for `verifier`."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    provider: OAuthProviderConfig,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str | None = None,
) -> str:
    """Construct the provider authorization URL with query parameters."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if provider.scopes:
        params["scope"] = " ".join(provider.scopes)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    for k, v in provider.extra_auth_params.items():
        if k and v is not None:
            params[str(k)] = str(v)
    separator = "&" if "?" in provider.authorization_url else "?"
    return f"{provider.authorization_url}{separator}{urlencode(params)}"


def extract_callback_code(raw_input: str, expected_state: str | None = None) -> str:
    """
    Extract an OAuth authorization `code` from a full redirect URL or raw code string.

    Validates CSRF `state` when a query string is present and raises `UserNotAuthorized`
    if the provider returned an OAuth error or `state` does not match `expected_state`.
    """
    text = (raw_input or "").strip()
    if not text:
        raise UserNotAuthorized(detail="Authorization code was empty")

    if "://" in text or text.startswith("?") or "code=" in text or "error=" in text:
        parsed = urlparse(text if "://" in text or text.startswith("?") else f"?{text.lstrip('?')}")
        qs = parse_qs(parsed.query, keep_blank_values=False)
        if "error" in qs:
            err = qs["error"][0]
            desc = qs.get("error_description", [""])[0]
            msg = f"OAuth authorization denied ({err}: {desc})" if desc else f"OAuth authorization denied ({err})"
            raise UserNotAuthorized(detail=msg)
        if expected_state is not None:
            got_states = qs.get("state") or []
            got_state = got_states[0] if got_states else None
            if got_state != expected_state:
                raise UserNotAuthorized(detail="OAuth state mismatch (CSRF check failed)")
        codes = qs.get("code") or []
        code = codes[0] if codes else None
        if not code or not code.strip():
            raise UserNotAuthorized(detail="OAuth callback URL did not include an authorization code")
        return code.strip()

    return text


class _LoopbackCallbackState:
    def __init__(self, expected_path: str, expected_state: str) -> None:
        self.expected_path = expected_path if expected_path.startswith("/") else f"/{expected_path}"
        self.expected_state = expected_state
        self.done = threading.Event()
        self.code: str | None = None
        self.error: str | None = None


class _LoopbackCallbackServer:
    """Single-use local HTTP server listening on loopback for the OAuth redirect callback."""

    def __init__(self, host: str, path: str, expected_state: str) -> None:
        self.state = _LoopbackCallbackState(path, expected_state)
        state_ref = self.state

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != state_ref.expected_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    code = extract_callback_code(
                        f"http://127.0.0.1{self.path}",
                        expected_state=state_ref.expected_state,
                    )
                    state_ref.code = code
                    status_code = 200
                    title = "Authorization Complete"
                    body = "Signed in successfully. You can close this browser tab and return to audiobook-dl."
                except UserNotAuthorized as exc:
                    state_ref.error = str(getattr(exc, "detail", None) or exc or "Authorization denied")
                    status_code = 400
                    title = "Authorization Failed"
                    body = state_ref.error
                finally:
                    state_ref.done.set()

                doc = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    f"<title>{html.escape(title)}</title></head>"
                    "<body style='font-family:sans-serif;max-width:520px;margin:48px auto;padding:0 16px;'>"
                    f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p>"
                    "</body></html>"
                ).encode()
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(doc)))
                self.end_headers()
                self.wfile.write(doc)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = HTTPServer((host, 0), _Handler)
        self._server.timeout = 0.25
        self._port = int(self._server.server_address[1])
        self._host = host
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._host}:{self._port}{self.state.expected_path}"

    def _serve_loop(self) -> None:
        while not self._stop.is_set() and not self.state.done.is_set():
            self._server.handle_request()

    def start(self) -> None:
        self._thread.start()

    def wait_for_code(
        self,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while not self.state.done.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.state.done.wait(min(0.25, remaining))
        if self.state.error:
            raise UserNotAuthorized(detail=self.state.error)
        return self.state.code

    def close(self) -> None:
        self._stop.set()
        self.state.done.set()
        try:
            self._server.server_close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


def _post_oauth_form(
    url: str,
    data: dict[str, str],
    *,
    session: Any | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST form-encoded OAuth payload with retries on transient network/5xx errors."""
    warn_if_insecure_token_url(url, "OAuth")
    retries = DEFAULT_MAX_RETRIES if max_retries is None else max(0, int(max_retries))
    owned_session = session is None
    http = session if session is not None else create_configured_http_session()
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    try:
        for attempt in range(retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled
            try:
                resp = http.post(
                    url,
                    data=data,
                    headers=req_headers,
                    timeout=DEFAULT_HTTP_TIMEOUT,
                )
            except DownloadCancelled:
                raise
            except Exception as exc:
                if is_transient_network_error(exc) and attempt < retries:
                    delay = compute_retry_delay(attempt)
                    logging.debug(
                        f"OAuth request to {url} failed ({exc}); retrying in {delay:.2f}s"
                    )
                    sleep_or_cancel(delay, cancel_event)
                    continue
                raise RequestError(detail=f"OAuth request failed: {exc}") from exc

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < retries:
                delay = compute_retry_delay(
                    attempt,
                    retry_after=resp.headers.get("Retry-After") if hasattr(resp, "headers") else None,
                )
                logging.debug(
                    f"OAuth endpoint {url} returned HTTP {resp.status_code}; retrying in {delay:.2f}s"
                )
                sleep_or_cancel(delay, cancel_event)
                continue

            payload: dict[str, Any] = {}
            try:
                raw_json = resp.json()
                if isinstance(raw_json, dict):
                    payload = raw_json
            except Exception:
                pass
            return int(resp.status_code), payload
    finally:
        if owned_session and hasattr(http, "close"):
            http.close()
    raise RequestError(detail=f"OAuth request exhausted retries for {url}")


def exchange_authorization_code(
    provider: OAuthProviderConfig,
    code: str,
    redirect_uri: str,
    *,
    code_verifier: str | None = None,
    session: Any | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int | None = None,
) -> OAuthTokenBundle:
    """Exchange an authorization `code` at `provider.token_url` for an `OAuthTokenBundle`."""
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        form["client_secret"] = provider.client_secret
    if code_verifier:
        form["code_verifier"] = code_verifier
    for k, v in provider.extra_token_params.items():
        if k and v is not None:
            form[str(k)] = str(v)

    status, payload = _post_oauth_form(
        provider.token_url,
        form,
        session=session,
        cancel_event=cancel_event,
        max_retries=max_retries,
    )
    if status in (400, 401, 403):
        msg = _format_oauth_error(payload) or f"OAuth token exchange rejected (HTTP {status})"
        raise UserNotAuthorized(detail=msg)
    if status != 200:
        raise RequestError(detail=f"OAuth token exchange failed (HTTP {status})")
    return OAuthTokenBundle.from_token_response(payload)


def refresh_oauth_token(
    provider: OAuthProviderConfig,
    token: OAuthTokenBundle,
    *,
    session: Any | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int | None = None,
) -> OAuthTokenBundle:
    """Refresh an expired `OAuthTokenBundle` using its `refresh_token`."""
    if not token.can_refresh or not token.refresh_token:
        raise UserNotAuthorized(detail="OAuth session expired and no refresh token is available")

    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        form["client_secret"] = provider.client_secret
    for k, v in provider.extra_token_params.items():
        if k and v is not None:
            form[str(k)] = str(v)

    status, payload = _post_oauth_form(
        provider.token_url,
        form,
        session=session,
        cancel_event=cancel_event,
        max_retries=max_retries,
    )
    if status in (400, 401, 403):
        msg = _format_oauth_error(payload) or f"OAuth token refresh rejected (HTTP {status})"
        raise UserNotAuthorized(detail=msg)
    if status != 200:
        raise RequestError(detail=f"OAuth token refresh failed (HTTP {status})")
    return OAuthTokenBundle.from_token_response(
        payload,
        existing_refresh_token=token.refresh_token,
    )


def revoke_oauth_token(
    provider: OAuthProviderConfig,
    token: OAuthTokenBundle,
    *,
    session: Any | None = None,
) -> bool:
    """Best-effort RFC 7009 token revocation when `provider.revoke_url` is configured."""
    if not provider.revoke_url:
        return False
    secret_to_revoke = token.refresh_token or token.access_token
    if not secret_to_revoke:
        return False
    form: dict[str, str] = {
        "token": secret_to_revoke,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        form["client_secret"] = provider.client_secret
    try:
        status, _ = _post_oauth_form(
            provider.revoke_url,
            form,
            session=session,
            max_retries=0,
        )
        return 200 <= status < 300
    except (RequestError, UserNotAuthorized, requests.RequestException) as exc:
        logging.debug(f"Ignoring OAuth token revocation error: {exc}")
        return False


def run_pkce_authorization_code_flow(
    provider: OAuthProviderConfig,
    *,
    interaction: logging.InteractionHandler | None = None,
    open_browser: bool = True,
    browser_opener: Callable[[str], Any] = webbrowser.open,
    session: Any | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int | None = None,
) -> OAuthTokenBundle:
    """
    Execute an OAuth 2.0 Authorization Code flow (with optional PKCE).

    Starts an ephemeral loopback HTTP listener to receive the redirect callback automatically
    after opening `browser_opener(auth_url)`. If loopback binding or browser opening is skipped/
    unavailable, prompts via `interaction.ask_input` for the pasted callback URL or code.
    """
    state = secrets.token_urlsafe(24)
    code_verifier = generate_code_verifier() if provider.use_pkce else None
    code_challenge = build_code_challenge(code_verifier) if code_verifier else None

    server: _LoopbackCallbackServer | None = None
    try:
        try:
            server = _LoopbackCallbackServer(
                provider.callback_host,
                provider.callback_path,
                state,
            )
            server.start()
            redirect_uri = server.redirect_uri
        except OSError as exc:
            logging.debug(f"Could not start OAuth loopback listener: {exc}")
            redirect_uri = f"http://{provider.callback_host}:8080{provider.callback_path}"

        auth_url = build_authorization_url(
            provider,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
        )
        logging.log(f"Open this URL in your browser to authorize:\n{auth_url}")

        browser_opened = False
        if open_browser:
            try:
                browser_opened = bool(browser_opener(auth_url))
            except Exception as exc:
                logging.debug(f"Browser open failed for OAuth URL: {exc}")

        code: str | None = None
        if server is not None and browser_opened:
            code = server.wait_for_code(provider.timeout_seconds, cancel_event=cancel_event)

        if code is None:
            if interaction is None:
                raise UserNotAuthorized(
                    detail="OAuth authorization did not complete via browser callback and no interactive prompt is active"
                )
            pasted = interaction.ask_input(
                f"Open {auth_url} and paste the redirect URL or authorization code"
            )
            code = extract_callback_code(pasted, expected_state=state)

        return exchange_authorization_code(
            provider,
            code,
            redirect_uri,
            code_verifier=code_verifier,
            session=session,
            cancel_event=cancel_event,
            max_retries=max_retries,
        )
    finally:
        if server is not None:
            server.close()


def run_device_authorization_flow(
    provider: OAuthProviderConfig,
    *,
    session: Any | None = None,
    cancel_event: threading.Event | None = None,
    max_retries: int | None = None,
) -> OAuthTokenBundle:
    """Execute an RFC 8628 OAuth 2.0 Device Authorization Grant flow."""
    if not provider.device_authorization_url:
        raise ValueError("device_authorization_url is required for device authorization flow")

    init_form: dict[str, str] = {"client_id": provider.client_id}
    if provider.scopes:
        init_form["scope"] = " ".join(provider.scopes)

    status, init_payload = _post_oauth_form(
        provider.device_authorization_url,
        init_form,
        session=session,
        cancel_event=cancel_event,
        max_retries=max_retries,
    )
    if status != 200 or not isinstance(init_payload.get("device_code"), str):
        msg = _format_oauth_error(init_payload) or f"OAuth device authorization failed (HTTP {status})"
        raise UserNotAuthorized(detail=msg)

    device_code = str(init_payload["device_code"])
    user_code = str(init_payload.get("user_code") or "")
    verify_complete = init_payload.get("verification_uri_complete") or init_payload.get("verification_url_complete")
    verify_uri = verify_complete or init_payload.get("verification_uri") or init_payload.get("verification_url") or ""
    interval = float(init_payload.get("interval") or 5)
    expires_in = float(init_payload.get("expires_in") or provider.timeout_seconds)
    deadline = time.monotonic() + min(float(provider.timeout_seconds), max(10.0, expires_in))

    logging.log(
        f"Authorize device at {verify_uri}" + (f" with code: {user_code}" if user_code else "")
    )

    poll_form: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        poll_form["client_secret"] = provider.client_secret

    while time.monotonic() < deadline:
        sleep_or_cancel(interval, cancel_event)
        poll_status, poll_payload = _post_oauth_form(
            provider.token_url,
            poll_form,
            session=session,
            cancel_event=cancel_event,
            max_retries=max_retries,
        )
        if poll_status == 200 and isinstance(poll_payload.get("access_token"), str):
            return OAuthTokenBundle.from_token_response(poll_payload)

        err = str(poll_payload.get("error") or "").strip().lower()
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5.0
            continue
        if err in ("expired_token", "access_denied"):
            raise UserNotAuthorized(detail=_format_oauth_error(poll_payload) or err)
        if poll_status in (400, 401, 403):
            raise UserNotAuthorized(detail=_format_oauth_error(poll_payload) or f"Device auth rejected (HTTP {poll_status})")
        raise RequestError(detail=f"Unexpected device authorization poll HTTP {poll_status}")

    raise UserNotAuthorized(detail="OAuth device authorization timed out")

"""Unit and integration tests for OAuth 2.0 flows, token persistence, and source lifecycle."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata
from audiobookdl.config import (
    DEFAULT_PROFILE,
    delete_source_oauth_token,
    load_config,
    load_source_oauth_token,
    save_source_credentials,
    save_source_oauth_token,
)
from audiobookdl.credentials import (
    KeyringCredentialStore,
    PlaintextFallbackCredentialStore,
    get_credential_store,
)
from audiobookdl.exceptions import DownloadCancelled, UserNotAuthorized
from audiobookdl.options_schema import build_default_options
from audiobookdl.pipeline import authenticate, authorize_service_oauth, disconnect_service_oauth, prepare_source_result
from audiobookdl.sources.librofm import LibrofmSource
from audiobookdl.sources.saxo import SaxoSource
from audiobookdl.sources.source import Source
from audiobookdl.utils.oauth import (
    OAuthProviderConfig,
    OAuthTokenBundle,
    build_authorization_url,
    build_code_challenge,
    extract_callback_code,
    generate_code_verifier,
    refresh_oauth_token,
    run_device_authorization_flow,
    run_pkce_authorization_code_flow,
)


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingSession:
    def __init__(self, post_fn) -> None:
        self._post_fn = post_fn
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, data=None, headers=None, timeout=None):
        self.calls.append((url, dict(data or {})))
        return self._post_fn(url, data=data, headers=headers, timeout=timeout)

    def close(self) -> None:
        pass


class _PromptHandler:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def ask_input(self, prompt: str, hidden: bool = False) -> str:
        self.prompts.append(prompt)
        return self.reply

    def confirm(self, message: str) -> bool:
        return True

    def choose_index(self, title: str, choices, default=None):
        return default


def test_pkce_helpers_and_callback_parsing():
    verifier = generate_code_verifier(64)
    assert len(verifier) == 64
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert build_code_challenge(verifier) == expected_challenge

    provider = OAuthProviderConfig(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="client-123",
        scopes=("library.read", "offline_access"),
        extra_auth_params={"audience": "books"},
    )
    auth_url = build_authorization_url(
        provider,
        redirect_uri="http://127.0.0.1:9000/callback",
        state="st-xyz",
        code_challenge=expected_challenge,
    )
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["client_id"] == ["client-123"]
    assert qs["state"] == ["st-xyz"]
    assert qs["scope"] == ["library.read offline_access"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["audience"] == ["books"]

    assert (
        extract_callback_code(
            "http://127.0.0.1:9000/callback?code=auth-code-1&state=st-xyz",
            expected_state="st-xyz",
        )
        == "auth-code-1"
    )
    assert extract_callback_code("raw-code-2", expected_state="st-xyz") == "raw-code-2"

    with pytest.raises(UserNotAuthorized, match="state mismatch"):
        extract_callback_code(
            "http://127.0.0.1:9000/callback?code=auth-code-1&state=wrong",
            expected_state="st-xyz",
        )
    with pytest.raises(UserNotAuthorized, match="access_denied"):
        extract_callback_code(
            "http://127.0.0.1:9000/callback?error=access_denied&error_description=User+cancelled",
            expected_state="st-xyz",
        )


def test_token_bundle_expiry_serialization_and_refresh_preservation():
    now = 1_700_000_000.0
    bundle = OAuthTokenBundle.from_token_response(
        {
            "access_token": "tok-1",
            "refresh_token": "ref-1",
            "token_type": "bearer",
            "expires_in": 3600,
            "scope": "read",
            "id": "u-9",
        },
        now=now,
    )
    assert bundle.authorization_header() == "Bearer tok-1"
    assert bundle.can_refresh is True
    assert not bundle.is_expired(leeway_seconds=60, now=now + 3500)
    assert bundle.is_expired(leeway_seconds=60, now=now + 3550)

    restored = OAuthTokenBundle.from_json(bundle.to_json())
    assert restored is not None
    assert restored.access_token == "tok-1"
    assert restored.refresh_token == "ref-1"
    assert restored.extra["id"] == "u-9"

    # Refresh preserving original refresh_token when server omits rotated token
    provider = OAuthProviderConfig(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="client-123",
    )
    sess = _RecordingSession(
        lambda url, data=None, headers=None, timeout=None: _DummyResponse(
            200,
            {"access_token": "tok-2", "expires_in": 7200},
        )
    )
    refreshed = refresh_oauth_token(provider, bundle, session=sess)
    assert refreshed.access_token == "tok-2"
    assert refreshed.refresh_token == "ref-1"
    assert sess.calls[0][1]["grant_type"] == "refresh_token"


def test_pkce_loopback_callback_and_manual_fallback():
    provider = OAuthProviderConfig(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="client-pkce",
        timeout_seconds=10.0,
    )
    seen_verifiers: list[str] = []

    def _token_post(url, data=None, headers=None, timeout=None):
        seen_verifiers.append(data["code_verifier"])
        return _DummyResponse(
            200,
            {"access_token": f"access-for-{data['code']}", "refresh_token": "ref-pkce", "expires_in": 300},
        )

    def _fake_browser_open(auth_url: str) -> bool:
        def _send_callback() -> None:
            parsed = parse_qs(urlparse(auth_url).query)
            redirect_uri = parsed["redirect_uri"][0]
            state = parsed["state"][0]
            resp = requests.get(f"{redirect_uri}?code=loopback-ok&state={state}", timeout=5)
            assert resp.status_code == 200

        threading.Thread(target=_send_callback, daemon=True).start()
        return True

    sess = _RecordingSession(_token_post)
    token = run_pkce_authorization_code_flow(
        provider,
        browser_opener=_fake_browser_open,
        session=sess,
    )
    assert token.access_token == "access-for-loopback-ok"
    assert token.refresh_token == "ref-pkce"
    assert len(seen_verifiers) == 1 and len(seen_verifiers[0]) >= 43

    # Manual interaction fallback when open_browser=False
    prompt = _PromptHandler("manual-code-99")
    manual_token = run_pkce_authorization_code_flow(
        provider,
        interaction=prompt,
        open_browser=False,
        session=sess,
    )
    assert manual_token.access_token == "access-for-manual-code-99"
    assert len(prompt.prompts) == 1


def test_pkce_flow_respects_cancellation():
    provider = OAuthProviderConfig(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="client-cancel",
        timeout_seconds=10.0,
    )
    cancel_ev = threading.Event()
    cancel_ev.set()
    with pytest.raises(DownloadCancelled):
        run_pkce_authorization_code_flow(
            provider,
            browser_opener=lambda _u: True,
            cancel_event=cancel_ev,
        )


def test_device_authorization_flow_polls_until_granted():
    provider = OAuthProviderConfig(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        device_authorization_url="https://auth.example.com/device",
        client_id="dev-client",
        scopes=("read",),
        timeout_seconds=10.0,
    )
    poll_count = 0

    def _device_post(url, data=None, headers=None, timeout=None):
        nonlocal poll_count
        if url == provider.device_authorization_url:
            return _DummyResponse(
                200,
                {
                    "device_code": "dev-code-1",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.example.com/activate",
                    "interval": 0.01,
                    "expires_in": 60,
                },
            )
        poll_count += 1
        if poll_count == 1:
            return _DummyResponse(400, {"error": "authorization_pending"})
        return _DummyResponse(200, {"access_token": "dev-access", "refresh_token": "dev-refresh"})

    sess = _RecordingSession(_device_post)
    token = run_device_authorization_flow(provider, session=sess)
    assert token.access_token == "dev-access"
    assert token.refresh_token == "dev-refresh"
    assert poll_count == 2


def test_oauth_vault_isolation_across_stores_and_profiles(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text('[sources.myservice.profiles.work]\nlocale = "sv"\n', encoding="utf-8")

    # 1. Plaintext fallback store
    plain_store = PlaintextFallbackCredentialStore(allow_plaintext=True, vault_path=str(tmp_path / "plain.json"))
    plain_store.set_password("myservice:work", "alice", "pw1")
    tok_work = OAuthTokenBundle(access_token="acc-work", refresh_token="ref-work")
    save_source_oauth_token("myservice", tok_work, profile="work", overwrite=str(cfg_path), credential_store=plain_store)

    assert plain_store.stored_services() == {"myservice:work": "alice"}
    assert plain_store.stored_oauth_profiles() == {"myservice:work"}
    loaded_work = load_source_oauth_token("myservice", profile="work", overwrite=str(cfg_path), credential_store=plain_store)
    assert loaded_work is not None and loaded_work.access_token == "acc-work"
    assert load_source_oauth_token("myservice", profile=DEFAULT_PROFILE, overwrite=str(cfg_path), credential_store=plain_store) is None

    # 2. Keyring store with fake keyring backend
    class _MemKeyring:
        def __init__(self) -> None:
            self.data: dict[tuple[str, str], str] = {}

        def get_password(self, service, account):
            return self.data.get((service, account))

        def set_password(self, service, account, password):
            self.data[(service, account)] = password

        def delete_password(self, service, account):
            self.data.pop((service, account), None)

    kr = KeyringCredentialStore(_MemKeyring(), index_path=str(tmp_path / "keyring.json"))
    save_source_oauth_token("myservice", tok_work, profile="work", overwrite=str(cfg_path), credential_store=kr)
    assert kr.stored_services() == {}
    assert kr.stored_oauth_profiles() == {"myservice:work"}
    assert load_source_oauth_token("myservice", profile="work", overwrite=str(cfg_path), credential_store=kr).access_token == "acc-work"

    # 3. Default platform credential store hydrates SourceConfig.oauth_token_present cleanly
    plat_store = get_credential_store(str(cfg_path), allow_plaintext=True)
    save_source_oauth_token("myservice", tok_work, profile="work", overwrite=str(cfg_path), credential_store=plat_store)
    cfg = load_config(str(cfg_path), credential_store=plat_store)
    assert cfg.sources["myservice:work"].oauth_token_present is True
    assert cfg.sources["myservice:work"].locale == "sv"

    delete_source_oauth_token("myservice", profile="work", overwrite=str(cfg_path), credential_store=plat_store)
    assert load_source_oauth_token("myservice", profile="work", overwrite=str(cfg_path), credential_store=plat_store) is None


class _FakeOAuthSource(Source[str]):
    names = ["FakeOAuth"]
    match = [r"https?://fakeoauth\.example\.com/book/\d+"]
    _authentication_methods = ["oauth"]
    oauth_provider = OAuthProviderConfig(
        authorization_url="https://fakeoauth.example.com/authorize",
        token_url="https://fakeoauth.example.com/token",
        revoke_url="https://fakeoauth.example.com/revoke",
        client_id="default-client",
    )

    auth_runs = 0
    refresh_calls = 0

    def oauth_authorize(self, url="", *, interaction=None, cancel_event=None, open_browser=True):
        type(self).auth_runs += 1
        token = OAuthTokenBundle(
            access_token=f"fresh-tok-{type(self).auth_runs}",
            refresh_token="refresh-1",
            expires_at=time.time() + 3600,
        )
        self.apply_oauth_token(token)
        return token

    def refresh_oauth_token_if_needed(self, *, force: bool = False):
        if self._oauth_token is None:
            return None
        if not force and not self._oauth_token.is_expired():
            return self._oauth_token
        type(self).refresh_calls += 1
        refreshed = OAuthTokenBundle(
            access_token=f"refreshed-tok-{type(self).refresh_calls}",
            refresh_token="refresh-2",
            expires_at=time.time() + 3600,
        )
        self.apply_oauth_token(refreshed)
        return refreshed

    def download(self, url: str) -> Audiobook:
        if self._oauth_token and self._oauth_token.access_token == "stale-access":
            raise UserNotAuthorized(detail="Access token expired on server")
        return Audiobook(
            session=self._session,
            files=[AudiobookFile(url="https://cdn.example.com/1.mp3", ext="mp3")],
            metadata=AudiobookMetadata(title="OAuth Book", authors=["A"]),
        )


def test_pipeline_oauth_lifecycle_authorize_reuse_refresh_and_retry(tmp_path, monkeypatch):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        '[sources.fakeoauth.profiles.work]\noauth_client_id = "custom-work-id"\noauth_scopes = ["read", "dl"]\n',
        encoding="utf-8",
    )
    _FakeOAuthSource.auth_runs = 0
    _FakeOAuthSource.refresh_calls = 0
    monkeypatch.setattr("audiobookdl.pipeline.jobs.find_compatible_source", lambda _u: _FakeOAuthSource)
    monkeypatch.setattr("audiobookdl.pipeline.auth.find_source_by_name", lambda _n: _FakeOAuthSource)

    cfg = load_config(str(cfg_path))
    opts = build_default_options(config_location=str(cfg_path), profile="work", database_directory=str(tmp_path / "db"))

    # 1. First download triggers oauth_authorize & saves token for "work" profile
    src1, book1 = prepare_source_result("https://fakeoauth.example.com/book/1", opts, cfg)
    assert book1.metadata.title == "OAuth Book"
    assert _FakeOAuthSource.auth_runs == 1
    assert src1.get_oauth_provider_config().client_id == "custom-work-id"
    assert src1.get_oauth_provider_config().scopes == ("read", "dl")
    saved = load_source_oauth_token("fakeoauth", profile="work", overwrite=str(cfg_path))
    assert saved is not None and saved.access_token == "fresh-tok-1"

    # 2. Second download reuses persisted token without re-authorizing
    cfg2 = load_config(str(cfg_path))
    src2, _ = prepare_source_result("https://fakeoauth.example.com/book/1", opts, cfg2)
    assert _FakeOAuthSource.auth_runs == 1
    assert src2._session.headers["Authorization"] == "Bearer fresh-tok-1"

    # 3. Stale token triggering 401 -> UserNotAuthorized refreshes transparently & retries once
    save_source_oauth_token(
        "fakeoauth",
        OAuthTokenBundle(access_token="stale-access", refresh_token="refresh-1", expires_at=time.time() + 3600),
        profile="work",
        overwrite=str(cfg_path),
    )
    cfg3 = load_config(str(cfg_path))
    src3, book3 = prepare_source_result("https://fakeoauth.example.com/book/1", opts, cfg3)
    assert book3.metadata.title == "OAuth Book"
    assert _FakeOAuthSource.refresh_calls == 1
    after_retry = load_source_oauth_token("fakeoauth", profile="work", overwrite=str(cfg_path))
    assert after_retry is not None and after_retry.access_token == "refreshed-tok-1"

    # 4. GUI helper authorize_service_oauth + disconnect_service_oauth
    monkeypatch.setattr("requests.Session.post", lambda *a, **kw: _DummyResponse(200, {}))
    authorize_service_oauth("fakeoauth", opts, cfg3, profile="default", open_browser=False)
    assert load_source_oauth_token("fakeoauth", profile="default", overwrite=str(cfg_path)) is not None
    revoked = disconnect_service_oauth("fakeoauth", opts, load_config(str(cfg_path)), profile="default")
    assert revoked is True
    assert load_source_oauth_token("fakeoauth", profile="default", overwrite=str(cfg_path)) is None


def test_librofm_and_saxo_persist_and_reuse_oauth_tokens(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    save_source_credentials("librofm", username="alice", password="pw1", overwrite=str(cfg_path))
    save_source_credentials("saxo", username="bob", password="pw2", overwrite=str(cfg_path))
    cfg = load_config(str(cfg_path))

    # Libro.fm password grant issues token persisted for subsequent run
    libro_opts = build_default_options(config_location=str(cfg_path), database_directory=str(tmp_path / "db"))
    libro_src = LibrofmSource(libro_opts)
    login_calls = 0

    def _libro_request(method, url, **kwargs):
        nonlocal login_calls
        login_calls += 1
        return _DummyResponse(200, {"access_token": "lib-tok-1", "refresh_token": "lib-ref-1", "expires_in": 3600})

    libro_src.request = _libro_request  # type: ignore[assignment]
    authenticate("https://libro.fm/audiobooks/1", libro_src, libro_opts, cfg)
    assert login_calls == 1
    stored_lib = load_source_oauth_token("librofm", overwrite=str(cfg_path))
    assert stored_lib is not None and stored_lib.access_token == "lib-tok-1"

    libro_src2 = LibrofmSource(libro_opts)
    libro_src2.request = _libro_request  # type: ignore[assignment]
    authenticate("https://libro.fm/audiobooks/1", libro_src2, libro_opts, load_config(str(cfg_path)))
    assert login_calls == 1
    assert libro_src2._session.headers["Authorization"] == "Bearer lib-tok-1"

    # Saxo password grant preserves extra["id"] alongside bearer_token
    saxo_src = SaxoSource(libro_opts)
    saxo_calls = 0

    def _saxo_post_json(url, **kwargs):
        nonlocal saxo_calls
        saxo_calls += 1
        return {"access_token": "sax-tok-1", "id": "sax-user-7", "expires_in": 3600}

    saxo_src.post_json = _saxo_post_json  # type: ignore[assignment]
    authenticate("https://www.saxo.com/dk/test", saxo_src, libro_opts, cfg)
    assert saxo_calls == 1
    saxo_src2 = SaxoSource(libro_opts)
    saxo_src2.post_json = _saxo_post_json  # type: ignore[assignment]
    authenticate("https://www.saxo.com/dk/test", saxo_src2, libro_opts, load_config(str(cfg_path)))
    assert saxo_calls == 1
    assert saxo_src2.bearer_token == "sax-tok-1"
    assert saxo_src2.user_id == "sax-user-7"

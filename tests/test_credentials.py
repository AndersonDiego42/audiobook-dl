import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from audiobookdl import logging
from audiobookdl.config import load_config, read_config, save_source_credentials
from audiobookdl.credentials import (
    DpapiCredentialStore,
    KeyringCredentialStore,
    PLAINTEXT_OPT_IN_ENV,
    PlaintextFallbackCredentialStore,
    get_credential_store,
    get_vault_path,
)


def test_passwords_are_not_persisted_in_plaintext_toml(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    secret = 'p@ss"w\\ord\t$123-café'

    save_source_credentials(
        "storytel",
        username="reader@example.com",
        password=secret,
        overwrite=str(cfg_path),
    )

    toml_text = cfg_path.read_text(encoding="utf-8")
    assert secret not in toml_text
    assert "password" not in read_config(str(cfg_path))["sources"]["storytel"]
    if os.name == "nt":
        assert os.path.exists(get_vault_path(str(cfg_path)))

    loaded = load_config(str(cfg_path))
    assert loaded.sources["storytel"].username == "reader@example.com"
    assert loaded.sources["storytel"].password == secret

    # Changing username removes old username's stored secret and clears on forget
    save_source_credentials("storytel", overwrite=str(cfg_path))
    reloaded = load_config(str(cfg_path))
    assert "storytel" not in reloaded.sources


def test_password_only_credential_round_trip(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    save_source_credentials("storytel", password="only-password", overwrite=str(cfg_path))

    loaded = load_config(str(cfg_path))
    assert loaded.sources["storytel"].username is None
    assert loaded.sources["storytel"].password == "only-password"

    save_source_credentials("storytel", overwrite=str(cfg_path))
    assert "storytel" not in load_config(str(cfg_path)).sources


def test_legacy_plaintext_password_is_migrated_and_scrubbed(tmp_path):
    cfg_path = tmp_path / "audiobook-dl.toml"
    cfg_path.write_text(
        'output_template = "{title}"\n\n'
        '[sources.storytel]\n'
        'username = "alice@example.com"\n'
        'password = "legacy-plaintext-pw"\n',
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    assert loaded.output_template == "{title}"
    assert loaded.sources["storytel"].username == "alice@example.com"
    assert loaded.sources["storytel"].password == "legacy-plaintext-pw"

    # Plaintext password must be scrubbed from TOML and still loadable afterward
    scrubbed_text = cfg_path.read_text(encoding="utf-8")
    assert "legacy-plaintext-pw" not in scrubbed_text
    assert "password" not in read_config(str(cfg_path))["sources"]["storytel"]

    reloaded = load_config(str(cfg_path))
    assert reloaded.sources["storytel"].password == "legacy-plaintext-pw"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_vault_binds_secret_to_service_and_username(tmp_path):
    vault = get_vault_path(str(tmp_path / "audiobook-dl.toml"))
    store = DpapiCredentialStore(vault)
    store.set_password("bookbeat", "bob@example.com", "top-secret")

    assert store.get_password("bookbeat", "bob@example.com") == "top-secret"
    assert store.get_password("bookbeat", "other@example.com") is None
    assert store.get_password("storytel", "bob@example.com") is None


def test_keyring_credential_store_and_plaintext_fallback(tmp_path, monkeypatch):
    backing = {}

    class _FakeKeyring:
        def get_keyring(self):
            return SimpleNamespace(priority=10)

        def get_password(self, service, account):
            return backing.get((service, account))

        def set_password(self, service, account, password):
            backing[(service, account)] = password

        def delete_password(self, service, account):
            backing.pop((service, account), None)

    kr_store = KeyringCredentialStore(_FakeKeyring())
    cfg_path = tmp_path / "audiobook-dl.toml"

    save_source_credentials(
        "podimo",
        username="u@example.com",
        password="kr-password",
        overwrite=str(cfg_path),
        credential_store=kr_store,
    )
    assert "kr-password" not in cfg_path.read_text(encoding="utf-8")
    assert backing == {("audiobook-dl", "podimo:u@example.com"): "kr-password"}

    loaded = load_config(str(cfg_path), credential_store=kr_store)
    assert loaded.sources["podimo"].password == "kr-password"

    # Non-Windows fallback without keyring refuses plaintext unless opted in
    monkeypatch.setattr("audiobookdl.credentials.os.name", "posix")
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    monkeypatch.delenv(PLAINTEXT_OPT_IN_ENV, raising=False)

    logged = []
    monkeypatch.setattr(logging, "log", logged.append)

    fallback = get_credential_store(str(cfg_path))
    assert isinstance(fallback, PlaintextFallbackCredentialStore)
    assert fallback.allow_plaintext is False

    save_source_credentials(
        "nextory",
        username="n@example.com",
        password="should-not-write",
        overwrite=str(cfg_path),
        credential_store=fallback,
    )
    assert "should-not-write" not in cfg_path.read_text(encoding="utf-8")
    assert any("not persisted" in msg for msg in logged)

    monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV, "1")
    opt_in_store = get_credential_store(str(cfg_path))
    assert isinstance(opt_in_store, PlaintextFallbackCredentialStore)
    assert opt_in_store.allow_plaintext is True
    save_source_credentials(
        "nextory",
        username="n@example.com",
        password="opted-in-pw",
        overwrite=str(cfg_path),
        credential_store=opt_in_store,
    )
    assert read_config(str(cfg_path))["sources"]["nextory"]["password"] == "opted-in-pw"


def test_concurrent_config_writes_do_not_corrupt_or_drop_entries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cfg_path = tmp_path / "audiobook-dl.toml"

    def write_service(idx: int) -> None:
        save_source_credentials(
            f"svc_{idx}",
            username=f"user_{idx}",
            overwrite=str(cfg_path),
            credential_store=PlaintextFallbackCredentialStore(allow_plaintext=False),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_service, range(16)))

    parsed = read_config(str(cfg_path))
    assert len(parsed["sources"]) == 16
    for idx in range(16):
        assert parsed["sources"][f"svc_{idx}"]["username"] == f"user_{idx}"


def test_keyring_store_index_tracks_stored_services_across_profiles(tmp_path):
    backing = {}

    class _FakeKeyring:
        def get_password(self, service, account):
            return backing.get((service, account))

        def set_password(self, service, account, password):
            backing[(service, account)] = password

        def delete_password(self, service, account):
            backing.pop((service, account), None)

    index_path = str(tmp_path / "credentials.keyring.json")
    store = KeyringCredentialStore(_FakeKeyring(), index_path=index_path)
    store.set_password("storytel", "main@example.com", "pw-main")
    store.set_password("storytel:family", "fam@example.com", "pw-fam")

    assert store.stored_services() == {
        "storytel": "main@example.com",
        "storytel:family": "fam@example.com",
    }
    store.delete_password("storytel:family", "fam@example.com")
    assert store.stored_services() == {"storytel": "main@example.com"}


def test_get_credential_store_accepts_plaintext_env_alias(tmp_path, monkeypatch):
    cfg_path = tmp_path / "audiobook-dl.toml"
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv(PLAINTEXT_OPT_IN_ENV, raising=False)
    monkeypatch.setenv("AUDIOBOOKDL_ALLOW_PLAINTEXT_CREDENTIALS", "1")
    with patch("audiobookdl.credentials._is_usable_keyring_backend", return_value=False):
        store = get_credential_store(str(cfg_path))
    assert isinstance(store, PlaintextFallbackCredentialStore)
    assert store.allow_plaintext is True

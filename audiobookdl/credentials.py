from abc import ABC, abstractmethod
import base64
import ctypes
from ctypes import wintypes
import json
import os
from typing import Any

from audiobookdl import logging
from audiobookdl.utils.fs import atomic_write_text, file_lock


PLAINTEXT_OPT_IN_ENV = "AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS"
_PLAINTEXT_OPT_IN_ENV_ALIASES = (
    PLAINTEXT_OPT_IN_ENV,
    "AUDIOBOOKDL_ALLOW_PLAINTEXT_CREDENTIALS",
)
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_OAUTH_KEY_SUFFIX = "#oauth"
_OAUTH_USERNAME = "__oauth_token__"


def make_oauth_vault_key(profile_key: str) -> str:
    """Return the `CredentialStore` service key used to store OAuth tokens for `profile_key`."""
    return f"{profile_key}{_OAUTH_KEY_SUFFIX}"


def is_oauth_vault_key(service_key: str) -> bool:
    """Return `True` if `service_key` belongs to an OAuth token bundle slot."""
    return isinstance(service_key, str) and service_key.endswith(_OAUTH_KEY_SUFFIX)


def strip_oauth_vault_key(service_key: str) -> str:
    """Return the underlying `profile_key` for an OAuth vault key."""
    if is_oauth_vault_key(service_key):
        return service_key[: -len(_OAUTH_KEY_SUFFIX)]
    return service_key


class CredentialStore(ABC):
    """Abstract storage backend for service passwords and OAuth token bundles."""

    is_secure: bool = True

    @abstractmethod
    def get_password(self, service_key: str, username: str | None) -> str | None:
        """Retrieve stored password for `(service_key, username)`, or `None`."""

    @abstractmethod
    def set_password(
        self, service_key: str, username: str | None, password: str
    ) -> None:
        """Persist `password` for `(service_key, username)`."""

    @abstractmethod
    def delete_password(self, service_key: str, username: str | None) -> None:
        """Remove any stored password for `(service_key, username)`."""

    def stored_services(self) -> dict[str, str | None]:
        """Return `{service_key: username}` for password entries known to this store."""
        return {}

    def get_oauth_token(self, profile_key: str) -> str | None:
        """Retrieve serialized OAuth token JSON for `profile_key`, or `None`."""
        return self.get_password(make_oauth_vault_key(profile_key), _OAUTH_USERNAME)

    def set_oauth_token(self, profile_key: str, token_json: str) -> None:
        """Persist serialized OAuth token JSON for `profile_key`."""
        self.set_password(make_oauth_vault_key(profile_key), _OAUTH_USERNAME, token_json)

    def delete_oauth_token(self, profile_key: str) -> None:
        """Remove any stored OAuth token bundle for `profile_key`."""
        self.delete_password(make_oauth_vault_key(profile_key), _OAUTH_USERNAME)

    def stored_oauth_profiles(self) -> set[str]:
        """Return profile keys that have a persisted OAuth token bundle in this store."""
        return set()


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array | None]:
    if not data:
        return _DataBlob(0, None), None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), buf), buf


def _dpapi_entropy(service_key: str, username: str | None) -> bytes:
    return f"audiobook-dl\0{service_key}\0{username or ''}".encode()


def _dpapi_protect(plaintext: bytes, entropy: bytes) -> bytes:
    windll: Any = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("Windows DPAPI is unavailable on this platform")
    crypt32 = windll.crypt32
    kernel32 = windll.kernel32
    win_error: Any = getattr(ctypes, "WinError", OSError)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_blob, _in_buf = _make_blob(plaintext)
    entropy_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "audiobook-dl",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise win_error()
    try:
        return bytes(
            ctypes.cast(
                out_blob.pbData,
                ctypes.POINTER(ctypes.c_ubyte * out_blob.cbData),
            ).contents
        )
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(ciphertext: bytes, entropy: bytes) -> bytes:
    windll: Any = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("Windows DPAPI is unavailable on this platform")
    crypt32 = windll.crypt32
    kernel32 = windll.kernel32
    win_error: Any = getattr(ctypes, "WinError", OSError)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_blob, _in_buf = _make_blob(ciphertext)
    entropy_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise win_error()
    try:
        return bytes(
            ctypes.cast(
                out_blob.pbData,
                ctypes.POINTER(ctypes.c_ubyte * out_blob.cbData),
            ).contents
        )
    finally:
        kernel32.LocalFree(out_blob.pbData)


class DpapiCredentialStore(CredentialStore):
    """Windows DPAPI-encrypted credential vault stored alongside `audiobook-dl.toml`."""

    is_secure = True

    def __init__(self, vault_path: str) -> None:
        self.vault_path = vault_path

    def _read_entries(self) -> dict[str, dict[str, Any]]:
        if not os.path.exists(self.vault_path):
            return {}
        try:
            with open(self.vault_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries")
            if isinstance(entries, dict):
                return entries
        except (OSError, ValueError, TypeError) as exc:
            logging.debug(f"Failed to read DPAPI credential vault '{self.vault_path}': {exc}")
        return {}

    def _write_entries(self, entries: dict[str, dict[str, Any]]) -> None:
        # Write an explicit empty vault atomically rather than unlinking so concurrent
        # readers inspecting/locking `self.vault_path` do not observe transient ENOENT.
        payload = {"version": 1, "entries": entries}
        atomic_write_text(
            self.vault_path,
            json.dumps(payload, indent=2),
            private=True,
        )

    def get_password(self, service_key: str, username: str | None) -> str | None:
        with file_lock(self.vault_path):
            entries = self._read_entries()
        entry = entries.get(service_key)
        if not isinstance(entry, dict):
            return None
        stored_user = entry.get("username")
        if stored_user != username:
            return None
        encoded = entry.get("ciphertext")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            encrypted = base64.b64decode(encoded.encode("ascii"))
            decrypted = _dpapi_unprotect(encrypted, _dpapi_entropy(service_key, username))
            return decrypted.decode("utf-8")
        except Exception as exc:
            logging.debug(f"Failed to decrypt stored password for '{service_key}': {exc}")
            return None

    def set_password(
        self, service_key: str, username: str | None, password: str
    ) -> None:
        encrypted = _dpapi_protect(
            password.encode("utf-8"),
            _dpapi_entropy(service_key, username),
        )
        with file_lock(self.vault_path):
            entries = self._read_entries()
            entries[service_key] = {
                "username": username,
                "ciphertext": base64.b64encode(encrypted).decode("ascii"),
            }
            self._write_entries(entries)

    def delete_password(self, service_key: str, username: str | None) -> None:
        with file_lock(self.vault_path):
            entries = self._read_entries()
            entry = entries.get(service_key)
            if not isinstance(entry, dict):
                return
            if username is None or entry.get("username") == username:
                entries.pop(service_key, None)
                self._write_entries(entries)

    def stored_services(self) -> dict[str, str | None]:
        with file_lock(self.vault_path):
            entries = self._read_entries()
        return {
            service_key: entry.get("username")
            for service_key, entry in entries.items()
            if isinstance(entry, dict) and not is_oauth_vault_key(service_key)
        }

    def stored_oauth_profiles(self) -> set[str]:
        with file_lock(self.vault_path):
            entries = self._read_entries()
        return {
            strip_oauth_vault_key(service_key)
            for service_key, entry in entries.items()
            if isinstance(entry, dict) and is_oauth_vault_key(service_key)
        }


class KeyringCredentialStore(CredentialStore):
    """Cross-platform OS keyring store backed by the `keyring` package."""

    is_secure = True
    _SERVICE_NAME = "audiobook-dl"

    def __init__(self, keyring_module: Any, index_path: str | None = None) -> None:
        self._keyring = keyring_module
        self.index_path = index_path

    @staticmethod
    def _account_key(service_key: str, username: str | None) -> str:
        return f"{service_key}:{username or ''}"

    def _read_index(self) -> dict[str, str | None]:
        if not self.index_path or not os.path.exists(self.index_path):
            return {}
        try:
            with open(self.index_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries")
            if isinstance(entries, dict):
                return {
                    str(k): (v if isinstance(v, str) or v is None else str(v))
                    for k, v in entries.items()
                }
        except (OSError, ValueError, TypeError) as exc:
            logging.debug(f"Failed to read keyring index '{self.index_path}': {exc}")
        return {}

    def _write_index(self, entries: dict[str, str | None]) -> None:
        if not self.index_path:
            return
        if not entries:
            if os.path.exists(self.index_path):
                os.remove(self.index_path)
            return
        atomic_write_text(
            self.index_path,
            json.dumps({"version": 1, "entries": entries}, indent=2),
            private=True,
        )

    def get_password(self, service_key: str, username: str | None) -> str | None:
        try:
            return self._keyring.get_password(
                self._SERVICE_NAME,
                self._account_key(service_key, username),
            )
        except Exception as exc:
            logging.debug(f"Keyring lookup failed for '{service_key}': {exc}")
            return None

    def set_password(
        self, service_key: str, username: str | None, password: str
    ) -> None:
        self._keyring.set_password(
            self._SERVICE_NAME,
            self._account_key(service_key, username),
            password,
        )
        if self.index_path:
            with file_lock(self.index_path):
                entries = self._read_index()
                entries[service_key] = username
                self._write_index(entries)

    def delete_password(self, service_key: str, username: str | None) -> None:
        try:
            self._keyring.delete_password(
                self._SERVICE_NAME,
                self._account_key(service_key, username),
            )
        except Exception as exc:
            logging.debug(f"Keyring delete ignored for '{service_key}': {exc}")
        if self.index_path:
            with file_lock(self.index_path):
                entries = self._read_index()
                if service_key in entries and (
                    username is None or entries.get(service_key) == username
                ):
                    entries.pop(service_key, None)
                    self._write_index(entries)

    def stored_services(self) -> dict[str, str | None]:
        if not self.index_path:
            return {}
        with file_lock(self.index_path):
            entries = self._read_index()
        active: dict[str, str | None] = {}
        for service_key, username in entries.items():
            if not is_oauth_vault_key(service_key) and self.get_password(service_key, username) is not None:
                active[service_key] = username
        return active

    def stored_oauth_profiles(self) -> set[str]:
        if not self.index_path:
            return set()
        with file_lock(self.index_path):
            entries = self._read_index()
        return {
            strip_oauth_vault_key(service_key)
            for service_key, username in entries.items()
            if is_oauth_vault_key(service_key) and self.get_password(service_key, username) is not None
        }


class PlaintextFallbackCredentialStore(CredentialStore):
    """Fallback used when no OS keystore is available."""

    is_secure = False

    def __init__(
        self,
        allow_plaintext: bool = False,
        vault_path: str | None = None,
    ) -> None:
        self.allow_plaintext = allow_plaintext
        self.vault_path = vault_path
        self._entries: dict[str, dict[str, str | None]] = {}

    def _read_vault(self) -> dict[str, dict[str, str | None]]:
        if not self.vault_path or not os.path.exists(self.vault_path):
            return dict(self._entries)
        try:
            with open(self.vault_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries")
            if isinstance(entries, dict):
                return entries
        except (OSError, ValueError, TypeError) as exc:
            logging.debug(f"Failed to read plaintext fallback vault '{self.vault_path}': {exc}")
        return {}

    def _write_vault(self, entries: dict[str, dict[str, str | None]]) -> None:
        self._entries = dict(entries)
        if not self.vault_path:
            return
        payload = {"version": 1, "entries": entries}
        atomic_write_text(
            self.vault_path,
            json.dumps(payload, indent=2),
            private=True,
        )

    def get_password(self, service_key: str, username: str | None) -> str | None:
        if not self.allow_plaintext:
            return None
        if self.vault_path:
            with file_lock(self.vault_path):
                entries = self._read_vault()
        else:
            entries = self._entries
        entry = entries.get(service_key)
        if not isinstance(entry, dict):
            return None
        if entry.get("username") != username:
            return None
        password = entry.get("password")
        return password if isinstance(password, str) else None

    def set_password(
        self, service_key: str, username: str | None, password: str
    ) -> None:
        if self.allow_plaintext:
            logging.log(
                f"Warning: No OS keyring backend available; saving password for "
                f"'{service_key}' in plaintext."
            )
            record = {"username": username, "password": password}
            if self.vault_path:
                with file_lock(self.vault_path):
                    entries = self._read_vault()
                    entries[service_key] = record
                    self._write_vault(entries)
            else:
                self._entries[service_key] = record
        else:
            logging.log(
                f"Warning: No OS keyring backend available; password for '{service_key}' "
                f"was not persisted. Set {PLAINTEXT_OPT_IN_ENV}=1 to allow plaintext storage."
            )

    def delete_password(self, service_key: str, username: str | None) -> None:
        if self.vault_path:
            with file_lock(self.vault_path):
                entries = self._read_vault()
                entry = entries.get(service_key)
                if isinstance(entry, dict) and (
                    username is None or entry.get("username") == username
                ):
                    entries.pop(service_key, None)
                    self._write_vault(entries)
        else:
            entry = self._entries.get(service_key)
            if isinstance(entry, dict) and (
                username is None or entry.get("username") == username
            ):
                self._entries.pop(service_key, None)

    def stored_services(self) -> dict[str, str | None]:
        if not self.allow_plaintext:
            return {}
        if self.vault_path:
            with file_lock(self.vault_path):
                entries = self._read_vault()
        else:
            entries = self._entries
        return {
            service_key: entry.get("username")
            for service_key, entry in entries.items()
            if isinstance(entry, dict) and not is_oauth_vault_key(service_key)
        }

    def stored_oauth_profiles(self) -> set[str]:
        if not self.allow_plaintext:
            return set()
        if self.vault_path:
            with file_lock(self.vault_path):
                entries = self._read_vault()
        else:
            entries = self._entries
        return {
            strip_oauth_vault_key(service_key)
            for service_key, entry in entries.items()
            if isinstance(entry, dict) and is_oauth_vault_key(service_key)
        }


def _is_usable_keyring_backend(keyring_module: Any) -> bool:
    try:
        backend = keyring_module.get_keyring()
    except Exception:
        return False
    backend_mod = type(backend).__module__.lower()
    if "fail" in backend_mod or "null" in backend_mod:
        return False
    return getattr(backend, "priority", 0) > 0


def get_vault_path(config_location: str) -> str:
    """Return path to the DPAPI vault file beside `config_location`."""
    config_dir = os.path.dirname(os.path.abspath(config_location))
    return os.path.join(config_dir, "credentials.dpapi")


def get_keyring_index_path(config_location: str) -> str:
    """Return path to the non-secret keyring service index beside `config_location`."""
    config_dir = os.path.dirname(os.path.abspath(config_location))
    return os.path.join(config_dir, "credentials.keyring.json")


def get_credential_store(
    config_location: str,
    allow_plaintext: bool | None = None,
) -> CredentialStore:
    """Select the primary OS-backed `CredentialStore` for the current platform."""
    if os.name == "nt":
        return DpapiCredentialStore(get_vault_path(config_location))
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None and _is_usable_keyring_backend(keyring):
        return KeyringCredentialStore(
            keyring,
            index_path=get_keyring_index_path(config_location),
        )
    if allow_plaintext is None:
        allow_plaintext = any(
            os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes")
            for env_name in _PLAINTEXT_OPT_IN_ENV_ALIASES
        )
    return PlaintextFallbackCredentialStore(allow_plaintext=allow_plaintext)

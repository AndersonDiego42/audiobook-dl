"""Cross-platform browser cookie discovery, extraction, and Netscape export helpers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from platformdirs import user_data_dir

from audiobookdl.utils.fs import atomic_write_text


BROWSER_LABELS: Tuple[Tuple[str, str], ...] = (
    ("firefox", "Firefox"),
    ("edge", "Microsoft Edge"),
    ("chrome", "Google Chrome"),
    ("brave", "Brave"),
    ("chromium", "Chromium"),
    ("opera", "Opera"),
)

SERVICE_COOKIE_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "audiobooks.com": ("audiobooks.com",),
    "blinkist": ("blinkist.com",),
    "chirp": ("chirpbooks.com",),
    "ereolen": ("ereolen.dk",),
    "everand": ("everand.com", "scribd.com"),
    "overdrive": ("overdrive.com", "listen.overdrive.com", "libbyapp.com"),
    "yourcloudlibrary": ("yourcloudlibrary.com",),
}

_NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.haxx.se/rfc/cookie_spec.html\n"
    "# This is a generated file! Do not edit.\n\n"
)

_CHROMIUM_EPOCH_OFFSET_SECONDS = 11644473600


class BrowserCookieError(RuntimeError):
    """Raised when browser cookies cannot be discovered, read, or decrypted."""


@dataclass
class CookieImportResult:
    """Result returned by extracting browser cookies for a service."""

    browser: str
    domains: Tuple[str, ...]
    cookies: List[http.cookiejar.Cookie]
    total_scanned: int
    profile_used: str
    warnings: List[str] = field(default_factory=list)


def default_imported_cookie_path(service_key: str) -> str:
    """Return a per-service default file path for imported Netscape cookies."""
    safe_key = re.sub(r"[^a-zA-Z0-9._-]+", "_", (service_key or "cookies").strip().lower()).strip("_")
    if not safe_key:
        safe_key = "cookies"
    return os.path.join(user_data_dir("audiobook-dl"), "cookies", f"{safe_key}.txt")


def default_domains_for_service(service_key: str) -> List[str]:
    """Return suggested cookie domain filters for `service_key`."""
    normalized = (service_key or "").strip().lower()
    if normalized in SERVICE_COOKIE_DOMAINS:
        return list(SERVICE_COOKIE_DOMAINS[normalized])

    from audiobookdl.sources import find_source_by_name

    source_cls = find_source_by_name(normalized)
    if source_cls is None:
        return []
    domains: List[str] = []
    seen = set()
    for pattern in getattr(source_cls, "match", ()):
        # Extract literal hostnames from simple URL match patterns.
        cleaned = (
            pattern.replace("\\.", ".")
            .replace("\\-", "-")
            .replace("https?://", "https://")
        )
        for match in re.findall(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", cleaned):
            host = match.lower().lstrip(".")
            if host.startswith("www."):
                host = host[4:]
            if host and host not in seen:
                seen.add(host)
                domains.append(host)
            if len(domains) >= 6:
                return domains
    return domains


def parse_domain_filter(text: str) -> List[str]:
    """Parse user-entered domain filter text into normalized bare hostnames."""
    if not text or not text.strip():
        return []
    domains: List[str] = []
    seen = set()
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        candidate = token.strip()
        if "://" in candidate:
            parsed = urlparse(candidate)
            candidate = parsed.hostname or candidate
        else:
            candidate = candidate.split("/")[0].split(":")[0]
        candidate = candidate.lstrip(".").lower()
        if candidate and candidate not in seen:
            seen.add(candidate)
            domains.append(candidate)
    return domains


def cookie_matches_domains(cookie_domain: str, target_domains: Sequence[str]) -> bool:
    """Return True when `cookie_domain` matches any domain suffix in `target_domains`."""
    if not target_domains:
        return True
    host = (cookie_domain or "").strip().lstrip(".").lower()
    if not host:
        return False
    for raw_target in target_domains:
        target = raw_target.strip().lstrip(".").lower()
        if not target:
            continue
        if host == target or host.endswith(f".{target}"):
            return True
    return False


def _build_cookie(
    *,
    domain: str,
    name: str,
    value: str,
    path: str = "/",
    expires: Optional[int] = None,
    secure: bool = False,
    http_only: bool = False,
) -> http.cookiejar.Cookie:
    """Construct a standard `http.cookiejar.Cookie` ready for Netscape serialization."""
    domain_clean = domain.strip()
    initial_dot = domain_clean.startswith(".")
    rest: Dict[str, Optional[str]] = {"HttpOnly": None} if http_only else {}
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain_clean,
        domain_specified=True,
        domain_initial_dot=initial_dot,
        path=path or "/",
        path_specified=True,
        secure=bool(secure),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest=rest,
        rfc2109=False,
    )


def cookies_to_netscape_text(cookies: Sequence[http.cookiejar.Cookie]) -> str:
    """Serialize `cookies` to standard Netscape `cookies.txt` format."""
    lines = [_NETSCAPE_HEADER.rstrip("\n")]
    for cookie in cookies:
        domain = cookie.domain or ""
        if cookie.http_only if hasattr(cookie, "http_only") else ("HttpOnly" in getattr(cookie, "_rest", {})):
            domain_field = f"#HttpOnly_{domain}"
        else:
            domain_field = domain
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = cookie.path or "/"
        secure = "TRUE" if cookie.secure else "FALSE"
        expires = int(cookie.expires) if cookie.expires is not None else 0
        lines.append(
            "\t".join(
                (
                    domain_field,
                    include_subdomains,
                    path,
                    secure,
                    str(max(0, expires)),
                    cookie.name,
                    cookie.value or "",
                )
            )
        )
    return "\n".join(lines) + "\n"


def export_cookies_netscape(cookies: Sequence[http.cookiejar.Cookie], target_path: str) -> str:
    """Atomically write `cookies` to `target_path` in Netscape format with restricted permissions."""
    content = cookies_to_netscape_text(cookies)
    return atomic_write_text(target_path, content, encoding="utf-8", private=True)


def _copy_sqlite_to_temp(db_path: Path, tmp_dir: Path) -> Path:
    """Copy `db_path` and optional SQLite WAL/SHM sidecars into `tmp_dir`."""
    dest = tmp_dir / db_path.name
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        if sidecar.is_file():
            try:
                shutil.copy2(sidecar, tmp_dir / sidecar.name)
            except OSError:
                pass
    return dest


def _firefox_profile_roots() -> List[Path]:
    home = Path.home()
    roots: List[Path] = []
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "Mozilla" / "Firefox" / "Profiles")
    elif sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Firefox" / "Profiles")
    else:
        roots.append(home / ".mozilla" / "firefox")
        roots.append(home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox")
        roots.append(home / "snap" / "firefox" / "common" / ".mozilla" / "firefox")
    return roots


def discover_firefox_cookie_dbs(
    profile_roots: Optional[Sequence[Path]] = None,
) -> List[Tuple[str, Path]]:
    """Return `(profile_name, cookies.sqlite)` pairs found for Firefox."""
    roots = list(profile_roots) if profile_roots is not None else _firefox_profile_roots()
    found: List[Tuple[str, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates = sorted(root.iterdir(), key=lambda p: ("default-release" not in p.name, p.name))
        for entry in candidates:
            if not entry.is_dir():
                continue
            cookie_db = entry / "cookies.sqlite"
            if cookie_db.is_file():
                found.append((entry.name, cookie_db))
    return found


def _chromium_user_data_roots(browser: str) -> List[Path]:
    home = Path.home()
    browser = browser.lower()
    roots: List[Path] = []
    if sys.platform.startswith("win"):
        local_appdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        if local_appdata:
            local = Path(local_appdata)
            if browser == "chrome":
                roots.append(local / "Google" / "Chrome" / "User Data")
            elif browser == "edge":
                roots.append(local / "Microsoft" / "Edge" / "User Data")
            elif browser == "brave":
                roots.append(local / "BraveSoftware" / "Brave-Browser" / "User Data")
            elif browser == "chromium":
                roots.append(local / "Chromium" / "User Data")
        if appdata and browser == "opera":
            app = Path(appdata)
            roots.append(app / "Opera Software" / "Opera Stable")
            roots.append(app / "Opera Software" / "Opera GX Stable")
    elif sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        mapping = {
            "chrome": app_support / "Google" / "Chrome",
            "edge": app_support / "Microsoft Edge",
            "brave": app_support / "BraveSoftware" / "Brave-Browser",
            "chromium": app_support / "Chromium",
            "opera": app_support / "com.operasoftware.Opera",
        }
        if browser in mapping:
            roots.append(mapping[browser])
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        mapping = {
            "chrome": config_home / "google-chrome",
            "edge": config_home / "microsoft-edge",
            "brave": config_home / "BraveSoftware" / "Brave-Browser",
            "chromium": config_home / "chromium",
            "opera": config_home / "opera",
        }
        if browser in mapping:
            roots.append(mapping[browser])
    return roots


def discover_chromium_cookie_dbs(
    browser: str,
    user_data_roots: Optional[Sequence[Path]] = None,
) -> List[Tuple[str, Path, Optional[Path]]]:
    """Return `(profile_name, cookie_db_path, local_state_path)` tuples for Chromium browsers."""
    roots = list(user_data_roots) if user_data_roots is not None else _chromium_user_data_roots(browser)
    found: List[Tuple[str, Path, Optional[Path]]] = []
    for root in roots:
        if not root.is_dir():
            continue
        local_state = root / "Local State"
        local_state_path = local_state if local_state.is_file() else None

        profile_dirs: List[Path] = []
        # Opera keeps Network/Cookies directly in user data root.
        for direct_rel in (Path("Network") / "Cookies", Path("Cookies")):
            direct_db = root / direct_rel
            if direct_db.is_file():
                found.append((root.name, direct_db, local_state_path))
                break

        default_dir = root / "Default"
        if default_dir.is_dir():
            profile_dirs.append(default_dir)
        other_profiles = sorted(
            p for p in root.iterdir() if p.is_dir() and p.name.startswith("Profile ")
        )
        profile_dirs.extend(other_profiles)

        for profile_dir in profile_dirs:
            for rel in (Path("Network") / "Cookies", Path("Cookies")):
                cookie_db = profile_dir / rel
                if cookie_db.is_file():
                    found.append((profile_dir.name, cookie_db, local_state_path))
                    break
    return found


def detect_available_browsers() -> Dict[str, bool]:
    """Return mapping of browser key -> whether local cookie profiles were found."""
    available: Dict[str, bool] = {}
    for key, _label in BROWSER_LABELS:
        if key == "firefox":
            available[key] = len(discover_firefox_cookie_dbs()) > 0
        else:
            available[key] = len(discover_chromium_cookie_dbs(key)) > 0
    return available


def read_firefox_cookies(
    db_path: Path,
    target_domains: Sequence[str],
) -> Tuple[List[http.cookiejar.Cookie], int]:
    """Read and filter cookies from a Firefox `cookies.sqlite` file."""
    with tempfile.TemporaryDirectory(prefix="abdl-ff-") as tmp:
        copied = _copy_sqlite_to_temp(db_path, Path(tmp))
        conn = sqlite3.connect(str(copied))
        try:
            rows = conn.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies"
            ).fetchall()
        finally:
            conn.close()

    matched: List[http.cookiejar.Cookie] = []
    for host, name, value, path, expiry, is_secure, is_httponly in rows:
        host_str = str(host or "")
        if not cookie_matches_domains(host_str, target_domains):
            continue
        exp_val = int(expiry) if expiry is not None else None
        matched.append(
            _build_cookie(
                domain=host_str,
                name=str(name or ""),
                value=str(value or ""),
                path=str(path or "/"),
                expires=exp_val,
                secure=bool(is_secure),
                http_only=bool(is_httponly),
            )
        )
    return matched, len(rows)


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes:
    """Decrypt Windows DPAPI-protected bytes via `crypt32.CryptUnprotectData`."""
    if not sys.platform.startswith("win"):
        raise BrowserCookieError("DPAPI decryption is only available on Windows.")
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    in_buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
    blob_in = _DataBlob(len(encrypted_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise BrowserCookieError("Windows DPAPI decryption failed for browser key.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _load_chromium_master_key(local_state_path: Optional[Path]) -> Optional[bytes]:
    if local_state_path is None or not local_state_path.is_file():
        return None
    try:
        payload = json.loads(local_state_path.read_text(encoding="utf-8"))
        enc_key_b64 = payload.get("os_crypt", {}).get("encrypted_key")
        if not enc_key_b64:
            return None
        raw = base64.b64decode(enc_key_b64)
        if raw.startswith(b"DPAPI"):
            return _dpapi_unprotect(raw[5:])
    except BrowserCookieError:
        raise
    except Exception as exc:
        raise BrowserCookieError(f"Failed to read Chromium encryption key: {exc}") from exc
    return None


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    try:
        from Cryptodome.Cipher import AES

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(key).decrypt(nonce, ciphertext + tag, None)


def _decrypt_chromium_value(
    host_key: str,
    plain_value: str,
    encrypted_value: Optional[bytes],
    *,
    master_key: Optional[bytes],
    db_version: int,
    dpapi_decrypt: Callable[[bytes], bytes] = _dpapi_unprotect,
) -> str:
    """Decode plaintext or decrypt Chromium `encrypted_value` cookie payload."""
    if plain_value:
        return plain_value
    if not encrypted_value:
        return ""
    if isinstance(encrypted_value, memoryview):
        encrypted_value = encrypted_value.tobytes()

    if encrypted_value.startswith((b"v10", b"v11")):
        if master_key is None:
            raise BrowserCookieError(
                "Browser cookies are encrypted with an OS keyring/Local State key that is unavailable."
            )
        if len(encrypted_value) < 3 + 12 + 16:
            raise BrowserCookieError("Encrypted Chromium cookie payload is truncated.")
        nonce = encrypted_value[3:15]
        body = encrypted_value[15:-16]
        tag = encrypted_value[-16:]
        decrypted = _aes_gcm_decrypt(master_key, nonce, body, tag)
        # Chromium DB schema >= 24 prepends a 32-byte SHA-256 hash of host_key.
        if db_version >= 24 and len(decrypted) >= 32:
            expected_hash = hashlib.sha256(host_key.encode("utf-8")).digest()
            if decrypted[:32] == expected_hash:
                decrypted = decrypted[32:]
        return decrypted.decode("utf-8")

    if encrypted_value.startswith(b"v20"):
        raise BrowserCookieError(
            "Chrome/Edge uses App-Bound Encryption (v20) for these cookies. "
            "Try Firefox, or export a Netscape cookies.txt file from your browser."
        )

    if sys.platform.startswith("win"):
        return dpapi_decrypt(encrypted_value).decode("utf-8")

    raise BrowserCookieError("Unsupported Chromium cookie encryption format.")


def _chromium_expires_to_unix(expires_utc: Optional[int]) -> Optional[int]:
    if not expires_utc:
        return None
    try:
        unix_ts = int(int(expires_utc) / 1_000_000 - _CHROMIUM_EPOCH_OFFSET_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return None
    return unix_ts if unix_ts > 0 else None


def read_chromium_cookies(
    db_path: Path,
    local_state_path: Optional[Path],
    target_domains: Sequence[str],
    *,
    master_key: Optional[bytes] = None,
) -> Tuple[List[http.cookiejar.Cookie], int, List[str]]:
    """Read and filter cookies from a Chromium `Cookies` SQLite database."""
    try:
        with tempfile.TemporaryDirectory(prefix="abdl-cr-") as tmp:
            copied = _copy_sqlite_to_temp(db_path, Path(tmp))
            conn = sqlite3.connect(str(copied))
            try:
                db_version = 0
                try:
                    ver_row = conn.execute(
                        "SELECT value FROM meta WHERE key = 'version'"
                    ).fetchone()
                    if ver_row is not None:
                        db_version = int(ver_row[0])
                except sqlite3.Error:
                    db_version = 0
                rows = conn.execute(
                    "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies"
                ).fetchall()
            finally:
                conn.close()
    except PermissionError as exc:
        raise BrowserCookieError(
            f"Cookie database is locked by the running browser ({db_path.name}). "
            "Close the browser completely and scan again, or try Firefox."
        ) from exc
    except OSError as exc:
        raise BrowserCookieError(f"Could not read browser cookie database: {exc}") from exc

    resolved_key = master_key if master_key is not None else _load_chromium_master_key(local_state_path)
    matched: List[http.cookiejar.Cookie] = []
    warnings: List[str] = []
    app_bound_error: Optional[BrowserCookieError] = None

    for host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly in rows:
        host_str = str(host_key or "")
        if not cookie_matches_domains(host_str, target_domains):
            continue
        try:
            decoded_value = _decrypt_chromium_value(
                host_str,
                str(value or ""),
                encrypted_value,
                master_key=resolved_key,
                db_version=db_version,
            )
        except BrowserCookieError as exc:
            if app_bound_error is None:
                app_bound_error = exc
            if "App-Bound Encryption" in str(exc):
                continue
            raise
        matched.append(
            _build_cookie(
                domain=host_str,
                name=str(name or ""),
                value=decoded_value,
                path=str(path or "/"),
                expires=_chromium_expires_to_unix(expires_utc),
                secure=bool(is_secure),
                http_only=bool(is_httponly),
            )
        )

    if app_bound_error is not None:
        if not matched:
            raise app_bound_error
        warnings.append(str(app_bound_error))

    return matched, len(rows), warnings


def _try_browser_cookie3(browser: str, target_domains: Sequence[str]) -> Optional[List[http.cookiejar.Cookie]]:
    """Use optional `browser_cookie3` package when installed."""
    try:
        import browser_cookie3  # type: ignore[import-not-found]
    except ImportError:
        return None
    loader = getattr(browser_cookie3, browser.lower(), None)
    if loader is None:
        return None
    try:
        jar = loader()
    except Exception:
        return None
    matched = [c for c in jar if cookie_matches_domains(c.domain or "", target_domains)]
    return matched


def extract_browser_cookies(
    browser: str,
    target_domains: Sequence[str],
    *,
    db_path: Optional[Path] = None,
    local_state_path: Optional[Path] = None,
) -> CookieImportResult:
    """
    Extract cookies for `target_domains` from `browser`.

    Tries native profile SQLite readers first (Firefox, Chrome, Edge, Brave, Chromium, Opera),
    with optional `browser_cookie3` fallback when native extraction cannot access encrypted stores.
    """
    browser_key = (browser or "firefox").strip().lower()
    domains_tuple = tuple(target_domains)
    warnings: List[str] = []

    if browser_key == "firefox":
        if db_path is not None:
            cookies, scanned = read_firefox_cookies(db_path, domains_tuple)
            return CookieImportResult(
                browser=browser_key,
                domains=domains_tuple,
                cookies=cookies,
                total_scanned=scanned,
                profile_used=db_path.parent.name,
                warnings=warnings,
            )
        profiles = discover_firefox_cookie_dbs()
        if not profiles:
            raise BrowserCookieError("No Firefox profile with cookies was found on this system.")
        all_cookies: List[http.cookiejar.Cookie] = []
        total_scanned = 0
        used_profiles: List[str] = []
        for profile_name, path in profiles:
            cookies, scanned = read_firefox_cookies(path, domains_tuple)
            total_scanned += scanned
            if cookies or not used_profiles:
                used_profiles.append(profile_name)
            all_cookies.extend(cookies)
        return CookieImportResult(
            browser=browser_key,
            domains=domains_tuple,
            cookies=all_cookies,
            total_scanned=total_scanned,
            profile_used=", ".join(used_profiles),
            warnings=warnings,
        )

    if db_path is not None:
        cookies, scanned, read_warnings = read_chromium_cookies(db_path, local_state_path, domains_tuple)
        return CookieImportResult(
            browser=browser_key,
            domains=domains_tuple,
            cookies=cookies,
            total_scanned=scanned,
            profile_used=db_path.parent.name,
            warnings=read_warnings,
        )

    chromium_profiles = discover_chromium_cookie_dbs(browser_key)
    if not chromium_profiles:
        bc3 = _try_browser_cookie3(browser_key, domains_tuple)
        if bc3 is not None:
            return CookieImportResult(
                browser=browser_key,
                domains=domains_tuple,
                cookies=bc3,
                total_scanned=len(bc3),
                profile_used="browser_cookie3",
                warnings=warnings,
            )
        raise BrowserCookieError(f"No {browser_key.title()} cookie profile was found on this system.")

    all_cookies = []
    total_scanned = 0
    used_profiles = []
    last_error: Optional[BrowserCookieError] = None
    for profile_name, path, state_path in chromium_profiles:
        try:
            cookies, scanned, read_warnings = read_chromium_cookies(path, state_path, domains_tuple)
            total_scanned += scanned
            warnings.extend(read_warnings)
            if cookies or not used_profiles:
                used_profiles.append(profile_name)
            all_cookies.extend(cookies)
        except BrowserCookieError as exc:
            last_error = exc

    if not all_cookies and last_error is not None:
        bc3 = _try_browser_cookie3(browser_key, domains_tuple)
        if bc3 is not None:
            return CookieImportResult(
                browser=browser_key,
                domains=domains_tuple,
                cookies=bc3,
                total_scanned=len(bc3),
                profile_used="browser_cookie3",
                warnings=warnings,
            )
        raise last_error

    return CookieImportResult(
        browser=browser_key,
        domains=domains_tuple,
        cookies=all_cookies,
        total_scanned=total_scanned,
        profile_used=", ".join(used_profiles) if used_profiles else chromium_profiles[0][0],
        warnings=warnings,
    )

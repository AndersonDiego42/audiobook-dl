import hashlib
import http.cookiejar
import os
import sqlite3
import time

from Cryptodome.Cipher import AES
import pytest

from audiobookdl.utils.browser_cookies import (
    BrowserCookieError,
    cookie_matches_domains,
    default_domains_for_service,
    discover_chromium_cookie_dbs,
    discover_firefox_cookie_dbs,
    export_cookies_netscape,
    extract_browser_cookies,
    parse_domain_filter,
    read_chromium_cookies,
)


def _create_firefox_db(db_path, rows):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE moz_cookies (
                id INTEGER PRIMARY KEY,
                host TEXT,
                name TEXT,
                value TEXT,
                path TEXT,
                expiry INTEGER,
                isSecure INTEGER,
                isHttpOnly INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO moz_cookies (host, name, value, path, expiry, isSecure, isHttpOnly) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _encrypt_chromium_v10(key: bytes, host_key: str, value: str, *, include_host_hash: bool) -> bytes:
    nonce = b"123456789012"
    payload = value.encode("utf-8")
    if include_host_hash:
        payload = hashlib.sha256(host_key.encode("utf-8")).digest() + payload
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(payload)
    return b"v10" + nonce + ciphertext + tag


def test_domain_helpers_and_service_defaults():
    assert "everand.com" in default_domains_for_service("everand")
    assert "scribd.com" in default_domains_for_service("everand")
    assert "libbyapp.com" in default_domains_for_service("overdrive")

    parsed = parse_domain_filter("https://www.everand.com/listen/123, scribd.com; .ereolen.dk")
    assert parsed == ["www.everand.com", "scribd.com", "ereolen.dk"]

    assert cookie_matches_domains(".everand.com", ["everand.com"]) is True
    assert cookie_matches_domains("api.scribd.com", ["everand.com", "scribd.com"]) is True
    assert cookie_matches_domains("example.com", ["everand.com"]) is False


def test_firefox_cookie_discovery_read_and_netscape_roundtrip(tmp_path):
    profile_root = tmp_path / "Firefox" / "Profiles"
    p1 = profile_root / "abcd.default-release"
    p1.mkdir(parents=True)
    db_path = p1 / "cookies.sqlite"
    future_exp = int(time.time()) + 3600
    _create_firefox_db(
        db_path,
        [
            (".everand.com", "session_id", "secret123", "/", future_exp, 1, 1),
            ("audio.scribd.com", "prefs", "dark", "/listen", future_exp, 0, 0),
            (".google.com", "NID", "ignored", "/", future_exp, 1, 1),
        ],
    )

    discovered = discover_firefox_cookie_dbs([profile_root])
    assert discovered == [("abcd.default-release", db_path)]

    result = extract_browser_cookies("firefox", ["everand.com", "scribd.com"], db_path=db_path)
    assert result.total_scanned == 3
    assert [c.name for c in result.cookies] == ["session_id", "prefs"]

    out_txt = tmp_path / "cookies" / "everand.txt"
    written = export_cookies_netscape(result.cookies, str(out_txt))
    assert os.path.isfile(written)

    jar = http.cookiejar.MozillaCookieJar()
    jar.load(written, ignore_discard=True, ignore_expires=True)
    loaded = {c.name: c for c in jar}
    assert loaded["session_id"].value == "secret123"
    assert loaded["session_id"].secure is True
    assert loaded["prefs"].value == "dark"


def test_chromium_cookie_decryption_v24_hash_and_errors(tmp_path, monkeypatch):
    user_data = tmp_path / "Chrome" / "User Data"
    net_dir = user_data / "Default" / "Network"
    net_dir.mkdir(parents=True)
    db_path = net_dir / "Cookies"

    key = b"\x01" * 32
    enc_val = _encrypt_chromium_v10(key, ".overdrive.com", "tok_xyz", include_host_hash=True)
    chrome_exp = 13500000000000000  # > unix 1970

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE meta (key LONGVARCHAR PRIMARY KEY, value LONGVARCHAR)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', '24')")
        conn.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                path TEXT,
                expires_utc INTEGER,
                is_secure INTEGER,
                is_httponly INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (".overdrive.com", "od_sess", "", enc_val, "/", chrome_exp, 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    discovered = discover_chromium_cookie_dbs("chrome", [user_data])
    assert len(discovered) == 1
    assert discovered[0][1] == db_path

    cookies, scanned, warnings = read_chromium_cookies(
        db_path, None, ["overdrive.com"], master_key=key
    )
    assert scanned == 1
    assert warnings == []
    assert len(cookies) == 1
    assert cookies[0].name == "od_sess"
    assert cookies[0].value == "tok_xyz"
    assert cookies[0].expires is not None and cookies[0].expires > 0

    # App-Bound Encryption (v20) surfaces clear actionable error when no cookies decrypt
    v20_db = tmp_path / "v20_cookies"
    conn = sqlite3.connect(str(v20_db))
    try:
        conn.execute("CREATE TABLE meta (key LONGVARCHAR PRIMARY KEY, value LONGVARCHAR)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', '24')")
        conn.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                path TEXT,
                expires_utc INTEGER,
                is_secure INTEGER,
                is_httponly INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (".overdrive.com", "od_v20", "", b"v20" + b"\x00" * 40, "/", chrome_exp, 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(BrowserCookieError, match="App-Bound Encryption"):
        read_chromium_cookies(v20_db, None, ["overdrive.com"], master_key=key)

    # PermissionError when browser locks SQLite DB gives clear message
    monkeypatch.setattr(
        "audiobookdl.utils.browser_cookies._copy_sqlite_to_temp",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("locked")),
    )
    with pytest.raises(BrowserCookieError, match="locked by the running browser"):
        read_chromium_cookies(db_path, None, ["overdrive.com"], master_key=key)

"""Tests for DASH MPD ContentProtection parsing, `drm_inspect`, and the GUI DRM inspect dialog."""

from __future__ import annotations

import base64
import struct
import subprocess
import time
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from audiobookdl.exceptions import GenericAudiobookDLException, UserNotAuthorized
from audiobookdl.utils import find_executable
from audiobookdl.utils.drm_inspect import (
    format_report,
    inspect_bytes,
    inspect_file,
    inspect_url,
)
from audiobookdl.utils.pssh import (
    PLAYREADY_SYSTEM_ID,
    WIDEVINE_SYSTEM_ID,
    PsshParseError,
    parse_mpd_content_protection,
)


def _pssh_box(system_id: UUID, data: bytes = b"") -> bytes:
    body = struct.pack(">B3s", 0, b"\x00\x00\x00") + system_id.bytes + struct.pack(">I", len(data)) + data
    return struct.pack(">I4s", 8 + len(body), b"pssh") + body


def _playready_pro(kid: bytes) -> bytes:
    b64_guid = base64.b64encode(UUID(bytes=kid).bytes_le).decode("ascii")
    xml_bytes = f'<WRMHEADER version="4.0.0.0"><DATA><KID>{b64_guid}</KID></DATA></WRMHEADER>'.encode("utf-16-le")
    rec = struct.pack("<HH", 0x0001, len(xml_bytes)) + xml_bytes
    return struct.pack("<IH", 6 + len(rec), 1) + rec


def test_parse_mpd_content_protection_widevine_playready_and_errors():
    kid_a = bytes.fromhex("11223344556677889900aabbccddeeff")
    kid_b = bytes.fromhex("ffeeddccbbaa00998877665544332211")
    wv_b64 = base64.b64encode(_pssh_box(WIDEVINE_SYSTEM_ID, b"\x12\x10" + kid_a)).decode("ascii")
    pr_b64 = base64.b64encode(_pssh_box(PLAYREADY_SYSTEM_ID, _playready_pro(kid_b))).decode("ascii")

    mpd = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013">'
        "<Period>"
        '<AdaptationSet mimeType="audio/mp4">'
        f'<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc" cenc:default_KID="{UUID(bytes=kid_a)}"/>'
        f'<ContentProtection schemeIdUri="urn:uuid:{WIDEVINE_SYSTEM_ID}">'
        f"<cenc:pssh>{wv_b64}</cenc:pssh>"
        "</ContentProtection>"
        "</AdaptationSet>"
        '<AdaptationSet mimeType="video/mp4">'
        "<Representation>"
        f'<ContentProtection schemeIdUri="urn:uuid:{PLAYREADY_SYSTEM_ID}">'
        f"<cenc:pssh>{pr_b64}</cenc:pssh>"
        "</ContentProtection>"
        "</Representation>"
        "</AdaptationSet>"
        "</Period>"
        "</MPD>"
    ).encode()

    cps = parse_mpd_content_protection(mpd)
    assert len(cps) == 3
    assert cps[0].default_kid == kid_a
    assert cps[1].system_name == "Widevine"
    assert cps[1].pssh_boxes[0].key_ids == (kid_a,)
    assert cps[2].system_name == "PlayReady"
    assert cps[2].pssh_boxes[0].key_ids == (kid_b,)

    # Malformed base64 cenc:pssh
    bad_pssh_mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013">'
        "<Period><AdaptationSet>"
        f'<ContentProtection schemeIdUri="urn:uuid:{WIDEVINE_SYSTEM_ID}"><cenc:pssh>!!!bad!!!</cenc:pssh></ContentProtection>'
        "</AdaptationSet></Period></MPD>"
    ).encode()
    with pytest.raises(PsshParseError):
        parse_mpd_content_protection(bad_pssh_mpd)

    # Disallowed DOCTYPE
    doctype_mpd = b'<!DOCTYPE MPD [<!ENTITY x "y">]><MPD></MPD>'
    with pytest.raises(PsshParseError):
        parse_mpd_content_protection(doctype_mpd)


def test_inspect_bytes_sniffing_isobmff_hls_dash_and_unknown():
    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wv_box = _pssh_box(WIDEVINE_SYSTEM_ID, b"\x12\x10" + kid)

    # ISO BMFF
    iso = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00" + struct.pack(">I4s", 8 + len(wv_box), b"moov") + wv_box
    r_iso = inspect_bytes(iso, source_label="init.mp4")
    assert r_iso.container == "isobmff"
    assert r_iso.systems == ("Widevine",)
    assert r_iso.key_ids == (kid,)
    assert r_iso.encrypted is True
    assert "Widevine" in format_report(r_iso)
    assert kid.hex() in format_report(r_iso)

    # HLS
    wv_b64 = base64.b64encode(wv_box).decode("ascii")
    hls = (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,{wv_b64}",'
        f'KEYFORMAT="urn:uuid:{WIDEVINE_SYSTEM_ID}"\n'
        "#EXTINF:4.0,\nseg0.m4s\n"
    ).encode()
    r_hls = inspect_bytes(hls, source_label="https://cdn.example/p.m3u8")
    assert r_hls.container == "hls"
    assert r_hls.systems == ("Widevine",)
    assert r_hls.key_ids == (kid,)
    assert r_hls.encrypted is True

    # DASH MPD
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013">'
        "<Period><AdaptationSet>"
        f'<ContentProtection schemeIdUri="urn:uuid:{WIDEVINE_SYSTEM_ID}" cenc:default_KID="{UUID(bytes=kid)}">'
        f"<cenc:pssh>{wv_b64}</cenc:pssh>"
        "</ContentProtection></AdaptationSet></Period></MPD>"
    ).encode()
    r_dash = inspect_bytes(mpd, source_label="manifest.mpd")
    assert r_dash.container == "dash"
    assert r_dash.systems == ("Widevine",)
    assert r_dash.key_ids == (kid,)
    assert r_dash.encrypted is True

    # Unknown
    r_unk = inspect_bytes(b"plain non-media bytes", source_label="notes.txt")
    assert r_unk.container == "unknown"
    assert r_unk.encrypted is False
    assert r_unk.key_ids == ()


@pytest.mark.ffmpeg
def test_inspect_file_on_real_ffmpeg_cenc_mp4(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    key_hex = "00112233445566778899aabbccddeeff"
    kid_hex = "11223344556677889900aabbccddeeff"
    clear_mp4 = tmp_path / "clear.mp4"
    enc_mp4 = tmp_path / "enc.mp4"

    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", "-c:a", "aac", str(clear_mp4)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(clear_mp4),
            "-c",
            "copy",
            "-encryption_scheme",
            "cenc-aes-ctr",
            "-encryption_key",
            key_hex,
            "-encryption_kid",
            kid_hex,
            str(enc_mp4),
        ],
        check=True,
        capture_output=True,
    )

    report = inspect_file(str(enc_mp4))
    assert report.container == "isobmff"
    assert report.encrypted is True
    assert report.key_ids == (bytes.fromhex(kid_hex),)

    with pytest.raises(GenericAudiobookDLException, match="File not found"):
        inspect_file(str(tmp_path / "missing.mp4"))


def test_inspect_url_byte_cap_hls_map_and_403_handling():
    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wv_box = _pssh_box(WIDEVINE_SYSTEM_ID, b"\x12\x10" + kid)
    mpd_bytes = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013">'
        "<Period><AdaptationSet>"
        f'<ContentProtection schemeIdUri="urn:uuid:{WIDEVINE_SYSTEM_ID}" cenc:default_KID="{UUID(bytes=kid)}"/>'
        "</AdaptationSet></Period></MPD>"
    ).encode()

    # 1. Byte cap truncates streaming read cleanly
    def _make_resp(status_code: int, chunks: list[bytes], url: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.url = url
        resp.headers = {"Content-Type": "application/dash+xml"}
        resp.text = ""
        resp.iter_content.return_value = iter(chunks)
        return resp

    session = MagicMock()
    session.get.return_value = _make_resp(200, [mpd_bytes[:30], mpd_bytes[30:], b"X" * 500], "https://cdn.example/m.mpd")
    report = inspect_url("https://cdn.example/m.mpd", session=session, max_bytes=len(mpd_bytes))
    assert report.container == "dash"
    assert report.key_ids == (kid,)

    # 2. HTTP 403 maps to UserNotAuthorized
    session.get.return_value = _make_resp(403, [b"Forbidden"], "https://cdn.example/secret.mpd")
    with pytest.raises(UserNotAuthorized):
        inspect_url("https://cdn.example/secret.mpd", session=session)

    # 3. HLS fetches #EXT-X-MAP init segment and merges its PSSH KID
    init_mp4 = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00" + struct.pack(">I4s", 8 + len(wv_box), b"moov") + wv_box
    hls_bytes = (
        b"#EXTM3U\n"
        b'#EXT-X-MAP:URI="init.mp4"\n'
        b'#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="https://lic.example/wv",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"\n'
        b"#EXTINF:5.0,\nseg0.m4s\n"
    )

    def _hls_get(url: str, **_kw):
        if url.endswith("init.mp4"):
            return _make_resp(200, [init_mp4], url)
        return _make_resp(200, [hls_bytes], url)

    session.get.side_effect = _hls_get
    r_hls = inspect_url("https://cdn.example/stream.m3u8", session=session)
    assert r_hls.container == "hls"
    assert r_hls.key_ids == (kid,)
    assert "Widevine" in r_hls.systems


@pytest.mark.gui
def test_gui_drm_inspect_dialog_states_and_copy_kids(tmp_path):
    import tkinter as tk
    from audiobookdl.gui.app import AudiobookDLApp
    from audiobookdl.gui.pages.drm_inspect_dialog import DrmInspectDialog

    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError as exc:
        pytest.skip(f"Tkinter display unavailable: {exc}")

    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wv_box = _pssh_box(WIDEVINE_SYSTEM_ID, b"\x12\x10" + kid)
    mp4_file = tmp_path / "sample.mp4"
    mp4_file.write_bytes(
        struct.pack(">I4s", 16, b"ftyp")
        + b"isom\x00\x00\x02\x00"
        + struct.pack(">I4s", 8 + len(wv_box), b"moov")
        + wv_box
    )

    try:
        # 1. Verify OptionsPanel Inspect... button opens DrmInspectDialog
        app = AudiobookDLApp(root, config_location=str(tmp_path / "cfg.toml"))
        dl_page = app.shell.get_page("download")
        assert dl_page is not None
        assert dl_page.options_panel._inspect_drm_btn.cget("text") == "Inspect..."

        dlg = DrmInspectDialog(root)
        # Empty state
        assert dlg.ui_state == "empty"
        assert str(dlg.copy_kids_btn.cget("state")) == "disabled"

        # Loading + Error state (missing file)
        dlg.target_var.set(str(tmp_path / "does_not_exist.mp4"))
        dlg.inspect_target()
        assert dlg.ui_state == "loading"
        deadline = time.monotonic() + 3.0
        while dlg.ui_state == "loading" and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert dlg.ui_state == "error"
        assert "not found" in dlg.status_var.get().lower()

        # Success state + Copy KIDs
        dlg.target_var.set(str(mp4_file))
        dlg.inspect_target()
        assert dlg.ui_state == "loading"
        deadline = time.monotonic() + 3.0
        while dlg.ui_state == "loading" and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert dlg.ui_state == "success"
        assert dlg.last_report is not None
        assert dlg.last_report.key_ids == (kid,)
        assert str(dlg.copy_kids_btn.cget("state")) == "normal"

        dlg._copy_kids()
        assert dlg.clipboard_get() == kid.hex()
        dlg.destroy()
    finally:
        root.destroy()

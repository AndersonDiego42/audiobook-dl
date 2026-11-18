"""Tests for ISO BMFF PSSH, `tenc`, and HLS DRM key-data parsing."""

from __future__ import annotations

import base64
import struct
from uuid import UUID

import pytest

from audiobookdl.utils.pssh import (
    CLEARKEY_SYSTEM_ID,
    FAIRPLAY_SYSTEM_ID,
    PLAYREADY_SYSTEM_ID,
    WIDEVINE_SYSTEM_ID,
    PsshParseError,
    extract_cenc_kids,
    parse_hls_key_data,
    parse_pssh_box,
    parse_pssh_boxes,
    parse_tenc_default_kids,
    read_mp4_init_boxes,
)


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _pssh_box(
    system_id: UUID,
    *,
    version: int = 0,
    key_ids: tuple[bytes, ...] = (),
    data: bytes = b"",
    large_size: bool = False,
) -> bytes:
    body = bytearray()
    body.extend(struct.pack(">B3s", version, b"\x00\x00\x00"))
    body.extend(system_id.bytes)
    if version == 1:
        body.extend(struct.pack(">I", len(key_ids)))
        for kid in key_ids:
            body.extend(kid)
    body.extend(struct.pack(">I", len(data)))
    body.extend(data)
    if large_size:
        return struct.pack(">I4sQ", 1, b"pssh", 16 + len(body)) + bytes(body)
    return _box(b"pssh", bytes(body))


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            break
    return bytes(out)


def _pb_bytes(field_num: int, value: bytes) -> bytes:
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(value)) + value


def _pb_varint(field_num: int, value: int) -> bytes:
    return _encode_varint((field_num << 3) | 0) + _encode_varint(value)


def _playready_pro(wrmheader_xml: str) -> bytes:
    xml_bytes = wrmheader_xml.encode("utf-16-le")
    record = struct.pack("<HH", 0x0001, len(xml_bytes)) + xml_bytes
    total_len = 6 + len(record)
    return struct.pack("<IH", total_len, 1) + record


def _build_tenc_mp4(
    kid: bytes,
    *,
    sample_entry_type: bytes = b"enca",
    is_protected: int = 1,
    audio_entry_version: int = 0,
) -> bytes:
    tenc_payload = (
        struct.pack(">B3sBBBB", 0, b"\x00\x00\x00", 0, 0, is_protected, 8) + kid
    )
    tenc = _box(b"tenc", tenc_payload)
    schi = _box(b"schi", tenc)
    sinf = _box(b"sinf", _box(b"frma", b"mp4a") + schi)
    if sample_entry_type == b"encv":
        sample_fixed = b"\x00" * 78
    else:
        extra = {0: 0, 1: 16, 2: 36}[audio_entry_version]
        sample_fixed = (
            b"\x00" * 6
            + struct.pack(">HH", 1, audio_entry_version)
            + b"\x00" * (18 + extra)
        )
    sample_entry = _box(sample_entry_type, sample_fixed + sinf)
    stsd = _box(b"stsd", struct.pack(">B3sI", 0, b"\x00\x00\x00", 1) + sample_entry)
    stbl = _box(b"stbl", stsd)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 16) + trak)
    return _box(b"ftyp", b"isom\x00\x00\x02\x00") + moov


def test_parse_widevine_v0_and_v1_pssh_boxes():
    kid1 = bytes.fromhex("11223344556677889900aabbccddeeff")
    kid2 = bytes.fromhex("ffeeddccbbaa00998877665544332211")
    wv_proto = (
        _pb_varint(1, 1)  # algorithm = AESCTR
        + _pb_bytes(2, kid1)
        + _pb_bytes(2, kid2)
        + _pb_bytes(4, b"book-content-id")
    )

    v0_raw = _pssh_box(WIDEVINE_SYSTEM_ID, version=0, data=wv_proto)
    v0 = parse_pssh_box(v0_raw)
    assert v0.system_id == WIDEVINE_SYSTEM_ID
    assert v0.system_name == "Widevine"
    assert v0.version == 0
    assert v0.key_ids == (kid1, kid2)
    assert v0.data == wv_proto

    # v1 merges header KIDs with protobuf KIDs without duplicates, and supports 64-bit largesize
    v1_raw = _pssh_box(
        WIDEVINE_SYSTEM_ID,
        version=1,
        key_ids=(kid1,),
        data=_pb_bytes(2, kid1) + _pb_bytes(2, kid2),
        large_size=True,
    )
    v1 = parse_pssh_box(v1_raw)
    assert v1.version == 1
    assert v1.key_ids == (kid1, kid2)


@pytest.mark.parametrize(
    ("xml_template", "expected_hex_list"),
    [
        (
            '<WRMHEADER version="4.0.0.0"><DATA><KID>{b64_1}</KID></DATA></WRMHEADER>',
            ["11223344556677889900aabbccddeeff"],
        ),
        (
            '<WRMHEADER version="4.1.0.0"><DATA><PROTECTINFO><KID VALUE="{b64_1}" ALGID="AESCTR"/></PROTECTINFO></DATA></WRMHEADER>',
            ["11223344556677889900aabbccddeeff"],
        ),
        (
            '<WRMHEADER version="4.3.0.0"><DATA><PROTECTINFO><KIDS>'
            '<KID ALGID="AESCTR" VALUE="{b64_1}"/>'
            '<KID ALGID="CENC" VALUE="{b64_2}"/>'
            "</KIDS></PROTECTINFO></DATA></WRMHEADER>",
            ["11223344556677889900aabbccddeeff", "ffeeddccbbaa00998877665544332211"],
        ),
    ],
)
def test_parse_playready_wrmheader_versions_and_guid_swap(xml_template, expected_hex_list):
    kid1 = bytes.fromhex("11223344556677889900aabbccddeeff")
    kid2 = bytes.fromhex("ffeeddccbbaa00998877665544332211")
    b64_1 = base64.b64encode(UUID(bytes=kid1).bytes_le).decode("ascii")
    b64_2 = base64.b64encode(UUID(bytes=kid2).bytes_le).decode("ascii")
    pro = _playready_pro(xml_template.format(b64_1=b64_1, b64_2=b64_2))

    parsed = parse_pssh_box(_pssh_box(PLAYREADY_SYSTEM_ID, version=0, data=pro))
    assert parsed.system_name == "PlayReady"
    assert parsed.key_ids == tuple(bytes.fromhex(h) for h in expected_hex_list)


def test_parse_clearkey_fairplay_unknown_and_moov_scan():
    kid = bytes.fromhex("00112233445566778899aabbccddeeff")
    unknown_id = UUID("12345678-1234-5678-1234-567812345678")

    clearkey_box = _pssh_box(CLEARKEY_SYSTEM_ID, version=1, key_ids=(kid,))
    fairplay_box = _pssh_box(FAIRPLAY_SYSTEM_ID, version=0, data=b"fairplay-init")
    unknown_box = _pssh_box(unknown_id, version=1, key_ids=(kid,))

    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 16) + clearkey_box + unknown_box)
    moof = _box(b"moof", _box(b"mfhd", b"\x00" * 8) + fairplay_box)
    eof_mdat = struct.pack(">I4s", 0, b"mdat") + b"\xaa\xbb\xcc"
    stream = _box(b"ftyp", b"isom\x00\x00\x02\x00") + moov + moof + eof_mdat

    boxes = parse_pssh_boxes(stream)
    assert [b.system_name for b in boxes] == ["ClearKey", str(unknown_id), "FairPlay"]
    assert boxes[0].key_ids == (kid,)
    assert boxes[1].key_ids == (kid,)
    assert boxes[2].data == b"fairplay-init"


def test_parse_tenc_default_kids_audio_and_video():
    kid_a = bytes.fromhex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    kid_v = bytes.fromhex("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    mp4_a0 = _build_tenc_mp4(kid_a, sample_entry_type=b"enca", audio_entry_version=0)
    mp4_a1 = _build_tenc_mp4(kid_a, sample_entry_type=b"enca", audio_entry_version=1)
    mp4_v = _build_tenc_mp4(kid_v, sample_entry_type=b"encv")
    mp4_clear = _build_tenc_mp4(kid_a, is_protected=0)

    assert parse_tenc_default_kids(mp4_a0) == (kid_a,)
    assert parse_tenc_default_kids(mp4_a1) == (kid_a,)
    assert parse_tenc_default_kids(mp4_v) == (kid_v,)
    assert parse_tenc_default_kids(mp4_clear) == ()
    assert extract_cenc_kids(mp4_a0) == (kid_a,)


@pytest.mark.parametrize(
    "bad_box",
    [
        b"",
        b"\x00\x00",
        _box(b"moov", b""),  # not a pssh box
        struct.pack(">I4s", 0, b"pssh") + b"\x00" * 32,  # zero-size pssh box
        struct.pack(">I4s", 4, b"pssh") + b"\x00" * 32,  # size < header
        struct.pack(">I4s", 999, b"pssh") + b"\x00" * 32,  # oversized / truncated
        _pssh_box(WIDEVINE_SYSTEM_ID, version=2),  # unsupported version
        _pssh_box(WIDEVINE_SYSTEM_ID, version=0, data=b"x") + b"\x00",  # trailing bytes
        # Huge v1 kid_count must be rejected cleanly via bounds check
        _box(
            b"pssh",
            struct.pack(">B3s", 1, b"\x00\x00\x00")
            + WIDEVINE_SYSTEM_ID.bytes
            + struct.pack(">II", 0xFFFFFFFF, 0),
        ),
        # Truncated Widevine protobuf
        _pssh_box(WIDEVINE_SYSTEM_ID, version=0, data=b"\x12\x10\x01\x02"),
        # Truncated PlayReady PRO
        _pssh_box(PLAYREADY_SYSTEM_ID, version=0, data=b"\x10\x00\x00\x00\x01"),
        # Disallowed XML doctype in PlayReady
        _pssh_box(
            PLAYREADY_SYSTEM_ID,
            version=0,
            data=_playready_pro('<!DOCTYPE foo><WRMHEADER version="4.0.0.0"></WRMHEADER>'),
        ),
    ],
)
def test_malformed_pssh_boxes_raise_parse_error(bad_box):
    with pytest.raises(PsshParseError):
        parse_pssh_box(bad_box)


def test_parse_pssh_boxes_and_tenc_reject_malformed_containers():
    with pytest.raises(PsshParseError):
        parse_pssh_boxes(struct.pack(">I4s", 0, b"free") + _pssh_box(CLEARKEY_SYSTEM_ID))
    with pytest.raises(PsshParseError):
        parse_pssh_boxes(struct.pack(">I4s", 100, b"moov") + b"\x00" * 10)
    truncated_tenc_mp4 = _box(
        b"moov",
        _box(
            b"trak",
            _box(
                b"mdia",
                _box(
                    b"minf",
                    _box(
                        b"stbl",
                        _box(
                            b"stsd",
                            struct.pack(">B3sI", 0, b"\x00\x00\x00", 1)
                            + _box(b"enca", b"\x00" * 28 + _box(b"sinf", _box(b"schi", _box(b"tenc", b"\x00" * 8)))),
                        ),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(PsshParseError):
        parse_tenc_default_kids(truncated_tenc_mp4)


def test_parse_hls_key_data_variants_and_errors():
    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wv_proto = _pb_bytes(2, kid)
    wv_pssh = _pssh_box(WIDEVINE_SYSTEM_ID, version=0, data=wv_proto)
    pr_pro = _playready_pro(
        f'<WRMHEADER version="4.0.0.0"><DATA><KID>{base64.b64encode(UUID(bytes=kid).bytes_le).decode()}</KID></DATA></WRMHEADER>'
    )
    pr_pssh = _pssh_box(PLAYREADY_SYSTEM_ID, version=0, data=pr_pro)

    # Non-data URIs return empty list
    assert parse_hls_key_data("skd://fairplay") == []
    assert parse_hls_key_data("https://example.com/license") == []

    # Single and concatenated PSSH boxes
    b64_multi = base64.b64encode(wv_pssh + pr_pssh).decode("ascii")
    multi = parse_hls_key_data(f"data:application/octet-stream;base64,{b64_multi}")
    assert [b.system_name for b in multi] == ["Widevine", "PlayReady"]
    assert all(b.key_ids == (kid,) for b in multi)

    # Raw Widevine protobuf (with and without key_format)
    b64_wv_raw = base64.b64encode(wv_proto).decode("ascii")
    assert parse_hls_key_data(f"data:text/plain;base64,{b64_wv_raw}")[0].key_ids == (kid,)
    assert (
        parse_hls_key_data(
            f"data:text/plain;base64,{b64_wv_raw}",
            key_format=f"urn:uuid:{WIDEVINE_SYSTEM_ID}",
        )[0].system_name
        == "Widevine"
    )

    # Raw PlayReady PRO & raw WRMHEADER XML
    b64_pr_raw = base64.b64encode(pr_pro).decode("ascii")
    assert parse_hls_key_data(f"data:text/plain;base64,{b64_pr_raw}")[0].key_ids == (kid,)

    wrm_xml = (
        f'<WRMHEADER version="4.0.0.0"><DATA><KID>{base64.b64encode(UUID(bytes=kid).bytes_le).decode()}</KID></DATA></WRMHEADER>'
    ).encode("utf-16-le")
    b64_wrm = base64.b64encode(wrm_xml).decode("ascii")
    assert parse_hls_key_data(f"data:text/plain;base64,{b64_wrm}")[0].key_ids == (kid,)

    # Invalid data: URIs raise PsshParseError
    for bad_uri in (
        "data:text/plain;base64",
        "data:text/plain;base64,!!!not-base64!!!",
        f"data:text/plain;base64,{base64.b64encode(b'not-a-pssh').decode('ascii')}",
    ):
        with pytest.raises(PsshParseError):
            parse_hls_key_data(bad_uri)


def test_read_mp4_init_boxes_skips_mdat_and_reads_moov_at_end(tmp_path):
    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    pssh_box = _pssh_box(WIDEVINE_SYSTEM_ID, version=0, data=_pb_bytes(2, kid))
    moov_box = _box(b"moov", pssh_box)
    mp4_path = tmp_path / "moov_at_end.mp4"
    mp4_path.write_bytes(
        _box(b"ftyp", b"isom\x00\x00\x02\x00")
        + _box(b"mdat", b"\x00" * 1024)
        + moov_box
    )

    extracted = read_mp4_init_boxes(str(mp4_path))
    assert extracted == moov_box
    assert parse_pssh_boxes(extracted)[0].key_ids == (kid,)

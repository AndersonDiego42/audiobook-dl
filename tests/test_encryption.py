import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES, ChaCha20, ChaCha20_Poly1305
from Crypto.Util.Padding import pad
import pytest

from audiobookdl import Audiobook, AudiobookFile, AudiobookMetadata
from audiobookdl.exceptions import GenericAudiobookDLException, MissingDependency
from audiobookdl.options_schema import OPTIONS_BY_NAME, PERSISTED_GUI_OPTION_SPECS, build_default_options
from audiobookdl.output.download import download_file
from audiobookdl.output.encryption import (
    AESCBCStreamDecryptor,
    AESCTRStreamDecryptor,
    AESGCMStreamDecryptor,
    ChaCha20StreamDecryptor,
    DECRYPT_CHUNK_SIZE,
    TS_PACKET_SIZE,
    create_stream_decryptor,
    decrypt_file,
    decrypt_mpegts_sample_aes,
)
from audiobookdl.pipeline import _apply_user_decryption_key
from audiobookdl.sources.source import Source
from audiobookdl.utils import AudioProbeInfo, find_executable
from audiobookdl.utils.audiobook import (
    AESCBCEncryption,
    AESCTREncryption,
    AESGCMEncryption,
    CENCDecryption,
    ChaCha20Encryption,
    SampleAESEncryption,
    parse_decryption_key_spec,
)


def _mock_stream_response(chunks: list[bytes], *, headers: dict[str, str] | None = None, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.iter_content.return_value = iter(chunks)
    resp.content = b"".join(chunks)
    return resp


def _build_ts_packet(pid: int, payload: bytes, *, pusi: bool = False) -> bytes:
    """Build a single 188-byte MPEG-TS packet with an optional adaptation field for padding."""
    header = bytes(
        [
            0x47,
            (0x40 if pusi else 0x00) | ((pid >> 8) & 0x1F),
            pid & 0xFF,
            0x10,
        ]
    )
    max_payload = TS_PACKET_SIZE - 4
    if len(payload) == max_payload:
        return header + payload
    if len(payload) > max_payload:
        raise ValueError("Payload exceeds single TS packet capacity")
    pad_len = max_payload - len(payload)
    if pad_len == 1:
        # AFC=3 with adaptation_field_length=0 occupies 1 byte
        return bytes([header[0], header[1], header[2], 0x30, 0x00]) + payload
    af = bytes([pad_len - 1, 0x00]) + (b"\xFF" * (pad_len - 2))
    return bytes([header[0], header[1], header[2], 0x30]) + af + payload


def _build_sample_aes_ts(adts_frames: list[bytes], key: bytes, iv: bytes) -> tuple[bytes, bytes]:
    """Return `(encrypted_ts_bytes, plaintext_ts_bytes)` containing PAT, PMT, and PES-wrapped ADTS AAC."""
    pmt_pid = 0x0100
    audio_pid = 0x0101

    pat_body = bytes(
        [
            0x00,  # pointer_field
            0x00,  # table_id PAT
            0xB0,
            0x0D,  # section_length = 13
            0x00,
            0x01,  # transport_stream_id
            0xC1,
            0x00,
            0x00,
            0x00,
            0x01,  # program_number = 1
            0xE0 | ((pmt_pid >> 8) & 0x1F),
            pmt_pid & 0xFF,
            0x00,
            0x00,
            0x00,
            0x00,  # CRC32 placeholder
        ]
    )
    pmt_body = bytes(
        [
            0x00,  # pointer_field
            0x02,  # table_id PMT
            0xB0,
            0x12,  # section_length = 18
            0x00,
            0x01,  # program_number = 1
            0xC1,
            0x00,
            0x00,
            0xE0 | ((audio_pid >> 8) & 0x1F),
            audio_pid & 0xFF,  # PCR PID
            0xF0,
            0x00,  # program_info_length = 0
            0x0F,  # stream_type ADTS AAC
            0xE0 | ((audio_pid >> 8) & 0x1F),
            audio_pid & 0xFF,
            0xF0,
            0x00,  # ES_info_length = 0
            0x00,
            0x00,
            0x00,
            0x00,  # CRC32 placeholder
        ]
    )

    plain_es = b"".join(adts_frames)
    enc_frames: list[bytes] = []
    for frame in adts_frames:
        # ADTS 7-byte header + 16 clear payload bytes, then complete 16-byte CBC blocks resetting IV per frame
        clear_prefix = frame[: 7 + 16]
        body = frame[7 + 16 :]
        usable = (len(body) // 16) * 16
        if usable:
            enc_part = AES.new(key, AES.MODE_CBC, iv).encrypt(body[:usable])
        else:
            enc_part = b""
        enc_frames.append(clear_prefix + enc_part + body[usable:])
    enc_es = b"".join(enc_frames)

    pes_header = bytes([0x00, 0x00, 0x01, 0xC0, 0x00, 0x00, 0x80, 0x00, 0x00])

    def _pack_pes(es: bytes) -> bytes:
        pes = pes_header + es
        packets = [
            _build_ts_packet(0x0000, pat_body, pusi=True),
            _build_ts_packet(pmt_pid, pmt_body, pusi=True),
        ]
        chunk_max = TS_PACKET_SIZE - 4
        for offset in range(0, len(pes), chunk_max):
            packets.append(
                _build_ts_packet(audio_pid, pes[offset : offset + chunk_max], pusi=(offset == 0))
            )
        return b"".join(packets)

    return _pack_pes(enc_es), _pack_pes(plain_es)


def _make_adts_frame(payload: bytes) -> bytes:
    frame_len = 7 + len(payload)
    header = bytes(
        [
            0xFF,
            0xF1,  # MPEG-4, protection_absent=1 (7-byte header)
            0x50,
            0x80 | ((frame_len >> 11) & 0x03),
            (frame_len >> 3) & 0xFF,
            ((frame_len & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
    return header + payload


def test_decrypt_file_streams_across_multiple_64k_chunks(tmp_path):
    assert DECRYPT_CHUNK_SIZE == 64 * 1024
    assert DECRYPT_CHUNK_SIZE % AES.block_size == 0

    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = os.urandom(DECRYPT_CHUNK_SIZE * 2 + 73)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    target = tmp_path / "segment.ts.tmp"
    target.write_bytes(ciphertext)

    decrypt_file(str(target), AESCBCEncryption(key=key, iv=iv))

    assert target.read_bytes() == plaintext
    assert not (tmp_path / "segment.ts.tmp.dec.tmp").exists()


def test_decrypt_file_handles_empty_file(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    target = tmp_path / "empty.mp3"
    target.write_bytes(b"")

    decrypt_file(str(target), AESCBCEncryption(key=key, iv=iv))

    assert target.read_bytes() == b""
    assert not (tmp_path / "empty.mp3.dec.tmp").exists()


def test_decrypt_file_preserves_original_and_cleans_tmp_on_error(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    bad_payload = b"short-not-16-bytes"
    target = tmp_path / "broken.mp3"
    target.write_bytes(bad_payload)

    with pytest.raises(ValueError):
        decrypt_file(str(target), AESCBCEncryption(key=key, iv=iv))

    assert target.read_bytes() == bad_payload
    assert not (tmp_path / "broken.mp3.dec.tmp").exists()


def test_decrypt_file_with_output_path_preserves_source_ciphertext(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = os.urandom(16 * 10)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    src_tmp = tmp_path / "segment.ts.tmp"
    dst_file = tmp_path / "segment.ts"
    src_tmp.write_bytes(ciphertext)

    decrypt_file(str(src_tmp), AESCBCEncryption(key=key, iv=iv), output_path=str(dst_file))

    assert dst_file.read_bytes() == plaintext
    assert src_tmp.read_bytes() == ciphertext
    assert not (tmp_path / "segment.ts.dec.tmp").exists()


@pytest.mark.parametrize("payload_len", [0, 1, 15, 16, 17, 32, 47])
@pytest.mark.parametrize("chunk_size", [1, 7, 16, 17, 64])
def test_aes_stream_decryptor_unpads_pkcs7_across_arbitrary_chunks(payload_len, chunk_size):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = os.urandom(payload_len)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))

    decryptor = AESCBCStreamDecryptor(key, iv)
    out = bytearray()
    for idx in range(0, len(ciphertext), chunk_size):
        out.extend(decryptor.decrypt_chunk(ciphertext[idx : idx + chunk_size]))
    out.extend(decryptor.finalize())

    assert bytes(out) == plaintext


def test_aes_cbc_nopad_supports_256bit_key_and_rejects_unaligned(tmp_path):
    key = os.urandom(32)
    iv = os.urandom(16)
    plaintext = os.urandom(64)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)

    target = tmp_path / "nopad.bin"
    target.write_bytes(ciphertext)
    decrypt_file(str(target), AESCBCEncryption(key=key, iv=iv, padding="none"))
    assert target.read_bytes() == plaintext

    dec = AESCBCStreamDecryptor(key, iv, padding="none")
    dec.decrypt_chunk(ciphertext + b"\x00")
    with pytest.raises(ValueError, match="multiple of 16 bytes"):
        dec.finalize()


def test_aes_stream_decryptor_rejects_unaligned_tail_and_bad_padding():
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"

    for bad_len in (5, 21):
        decryptor = AESCBCStreamDecryptor(key, iv)
        decryptor.decrypt_chunk(b"x" * bad_len)
        with pytest.raises(ValueError, match="multiple of 16 bytes"):
            decryptor.finalize()
        assert decryptor.finalize() == b""

    unpadded = b"0123456789abcdef" * 2
    bad_ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(unpadded)
    decryptor = AESCBCStreamDecryptor(key, iv)
    decryptor.decrypt_chunk(bad_ciphertext)
    with pytest.raises(ValueError):
        decryptor.finalize()


def test_decrypt_file_preserves_original_on_invalid_pkcs7_padding(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    bad_ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(b"0123456789abcdef")
    target = tmp_path / "bad_padding.mp3"
    target.write_bytes(bad_ciphertext)

    with pytest.raises(ValueError):
        decrypt_file(str(target), AESCBCEncryption(key=key, iv=iv))

    assert target.read_bytes() == bad_ciphertext
    assert not (tmp_path / "bad_padding.mp3.dec.tmp").exists()


@pytest.mark.parametrize("nonce_len", [8, 12, 16])
@pytest.mark.parametrize("chunk_size", [1, 13, 64])
def test_aes_ctr_stream_decryptor(nonce_len, chunk_size, tmp_path):
    key = os.urandom(32)
    nonce = os.urandom(nonce_len)
    plaintext = os.urandom(103)
    if nonce_len == 16:
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=nonce)
    else:
        cipher = AES.new(key, AES.MODE_CTR, nonce=nonce)
    ciphertext = cipher.encrypt(plaintext)

    decryptor = AESCTRStreamDecryptor(key, nonce)
    out = bytearray()
    for i in range(0, len(ciphertext), chunk_size):
        out.extend(decryptor.decrypt_chunk(ciphertext[i : i + chunk_size]))
    out.extend(decryptor.finalize())
    assert bytes(out) == plaintext

    target = tmp_path / "ctr.bin"
    target.write_bytes(ciphertext)
    decrypt_file(str(target), AESCTREncryption(key=key, nonce=nonce))
    assert target.read_bytes() == plaintext


@pytest.mark.parametrize("explicit_tag", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 7, 16, 64])
def test_aes_gcm_stream_decryptor_and_tamper_detection(explicit_tag, chunk_size, tmp_path):
    key = os.urandom(32)
    nonce = os.urandom(12)
    aad = b"segment-aad"
    plaintext = os.urandom(91)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)

    payload = ciphertext if explicit_tag else (ciphertext + tag)
    enc = AESGCMEncryption(key=key, nonce=nonce, tag=tag if explicit_tag else None, aad=aad)
    decryptor = create_stream_decryptor(enc)
    assert isinstance(decryptor, AESGCMStreamDecryptor)

    out = bytearray()
    for i in range(0, len(payload), chunk_size):
        out.extend(decryptor.decrypt_chunk(payload[i : i + chunk_size]))
    out.extend(decryptor.finalize())
    assert bytes(out) == plaintext

    # Tampered tag leaves original file untouched
    tampered = bytearray(payload)
    tampered[-1] ^= 0xFF
    target = tmp_path / "gcm.bin"
    target.write_bytes(bytes(tampered))
    with pytest.raises(ValueError):
        decrypt_file(str(target), enc)
    assert target.read_bytes() == bytes(tampered)
    assert not (tmp_path / "gcm.bin.dec.tmp").exists()


@pytest.mark.parametrize("poly1305", [False, True])
@pytest.mark.parametrize("nonce_len", [8, 12, 24])
def test_chacha20_stream_and_poly1305_decryptor(poly1305, nonce_len, tmp_path):
    key = os.urandom(32)
    nonce = os.urandom(nonce_len)
    plaintext = os.urandom(117)
    aad = b"book-aad" if poly1305 else b""

    if poly1305:
        cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
        cipher.update(aad)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        payload = ciphertext + tag
    else:
        ciphertext = ChaCha20.new(key=key, nonce=nonce).encrypt(plaintext)
        payload = ciphertext

    enc = ChaCha20Encryption(key=key, nonce=nonce, aad=aad, poly1305=poly1305)
    decryptor = create_stream_decryptor(enc)
    assert isinstance(decryptor, ChaCha20StreamDecryptor)
    out = decryptor.decrypt_chunk(payload[:23]) + decryptor.decrypt_chunk(payload[23:]) + decryptor.finalize()
    assert out == plaintext

    target = tmp_path / "chacha.bin"
    target.write_bytes(payload)
    decrypt_file(str(target), enc)
    assert target.read_bytes() == plaintext

    if poly1305:
        bad = bytearray(payload)
        bad[0] ^= 0x01
        target.write_bytes(bytes(bad))
        with pytest.raises(ValueError):
            decrypt_file(str(target), enc)
        assert target.read_bytes() == bytes(bad)


def test_decrypt_mpegts_sample_aes_adts_aac(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    frame1 = _make_adts_frame(os.urandom(16 + 32 + 5))  # 2 encrypted blocks + 5-byte clear tail
    frame2 = _make_adts_frame(os.urandom(12))  # shorter than 16 payload bytes -> entirely clear
    frame3 = _make_adts_frame(os.urandom(16 + 48 + 3))
    enc_ts, expected_plain_ts = _build_sample_aes_ts([frame1, frame2, frame3], key, iv)

    assert enc_ts != expected_plain_ts
    assert decrypt_mpegts_sample_aes(enc_ts, key, iv) == expected_plain_ts

    ts_file = tmp_path / "sample.ts"
    ts_file.write_bytes(enc_ts)
    decrypt_file(str(ts_file), SampleAESEncryption(key=key, iv=iv))
    assert ts_file.read_bytes() == expected_plain_ts

    with pytest.raises(ValueError, match="multiple of 188 bytes"):
        decrypt_mpegts_sample_aes(b"not-188-aligned", key, iv)


def test_decrypt_file_cenc_invokes_ffmpeg_and_handles_init_prefix(tmp_path):
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    init_bytes = b"ftypmoov_init"
    seg_bytes = b"moofmdat_enc"
    src_path = tmp_path / "seg.m4s.tmp"
    dst_path = tmp_path / "seg.m4s"
    src_path.write_bytes(seg_bytes)

    captured: dict[str, object] = {}

    def _fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        in_path = cmd[cmd.index("-i") + 1]
        captured["in_bytes"] = open(in_path, "rb").read()
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"decrypted-mp4")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with (
        patch("audiobookdl.output.encryption.find_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.encryption.subprocess.run", side_effect=_fake_run),
    ):
        decrypt_file(
            str(src_path),
            CENCDecryption(key=key, key_map={kid: key}),
            output_path=str(dst_path),
            prefix=init_bytes,
        )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "-decryption_key" in cmd
    assert key.hex() in cmd
    assert "-decryption_keys" in cmd
    assert f"{kid.hex()}={key.hex()}" in cmd
    assert captured["in_bytes"] == init_bytes + seg_bytes
    assert dst_path.read_bytes() == b"decrypted-mp4"
    assert not (tmp_path / "seg.m4s.cenc_in.tmp").exists()
    assert not (tmp_path / "seg.m4s.dec.tmp").exists()

    with patch("audiobookdl.output.encryption.find_executable", return_value=None):
        with pytest.raises(MissingDependency):
            decrypt_file(str(src_path), CENCDecryption(key=key))


@pytest.mark.ffmpeg
def test_decrypt_file_cenc_real_ffmpeg_roundtrip(tmp_path):
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    key_hex = "00112233445566778899aabbccddeeff"
    kid_hex = "11223344556677889900aabbccddeeff"
    clear_mp4 = tmp_path / "clear.mp4"
    enc_mp4 = tmp_path / "enc.mp4"
    dec_mp4 = tmp_path / "dec.mp4"

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

    decrypt_file(
        str(enc_mp4),
        CENCDecryption(key=bytes.fromhex(key_hex), kid=bytes.fromhex(kid_hex)),
        output_path=str(dec_mp4),
    )
    assert dec_mp4.exists()
    assert dec_mp4.stat().st_size > 0


def test_download_file_streams_aead_and_blocks_tampered_commit(tmp_path, monkeypatch):
    from audiobookdl.output import output as output_mod

    monkeypatch.setattr(output_mod, "_probe_audio_info", lambda _p: AudioProbeInfo(duration=1.0, codec_name="aac"))

    key = os.urandom(32)
    nonce = os.urandom(12)
    plaintext = b"authenticated audio stream payload" * 10
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    payload = ciphertext + tag

    session = MagicMock()
    session.get.return_value = _mock_stream_response(
        [payload[:19], payload[19:77], payload[77:]],
        headers={"Content-Length": str(len(payload))},
    )
    book = Audiobook(
        session=session,
        metadata=AudiobookMetadata(title="GCM Book"),
        files=[AudiobookFile(url="https://example.com/book.m4a", ext="m4a", encryption_method=AESGCMEncryption(key=key, nonce=nonce))],
    )
    out_path = download_file((book, str(tmp_path / "gcm_ok"), 0, lambda _: None, None), resume=False)
    assert open(out_path, "rb").read() == plaintext

    # Tampered tag fails finalize() and never writes final output file
    tampered = bytearray(payload)
    tampered[-1] ^= 0xFF
    session.get.return_value = _mock_stream_response(
        [bytes(tampered)],
        headers={"Content-Length": str(len(tampered))},
    )
    bad_dir = tmp_path / "gcm_bad"
    with pytest.raises(ValueError):
        download_file((book, str(bad_dir), 0, lambda _: None, None), resume=False)
    assert not (tmp_path / "gcm_bad.m4a").exists()


def test_get_stream_files_supports_hls_methods_and_rejects_drm():
    key16 = b"0123456789abcdef"
    key32 = b"0123456789abcdef0123456789abcdef"
    init_bytes = b"ftypmoov"

    class _DummySource(Source):
        names = ["dummy"]
        match = [r"https://example\.com/.*"]

    # 1. AES-256-GCM + MPEG-TS SAMPLE-AES + fMP4 SAMPLE-AES + SAMPLE-AES-CTR with user key
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-MEDIA-SEQUENCE:10\n"
        '#EXT-X-KEY:METHOD=AES-256-GCM,URI="https://example.com/k32.bin",IV=0x00000000000000000000000000000001\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg0.bin\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.com/k16.bin"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg1.ts\n"
        '#EXT-X-MAP:URI="https://example.com/init.mp4"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg2.m4s\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="skd://fairplay",KEYFORMAT="com.apple.streamingkeydelivery"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg3.m4s\n"
    )

    src = _DummySource(build_default_options(decryption_key=None))
    src.get = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda u, **_kw: playlist.encode("utf-8") if u.endswith("m3u8") else init_bytes
    )
    src._get_page = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda u, **_kw: key32 if "k32" in u else key16
    )

    # Without user decryption_key, proprietary FairPlay KEYFORMAT raises explicit error
    with pytest.raises(GenericAudiobookDLException, match="KEYFORMAT"):
        src.get_stream_files("https://example.com/master.m3u8")

    # With user decryption_key for CENC, seg3 uses user key without calling DRM URI
    user_src = _DummySource(build_default_options(decryption_key=key16.hex()))
    user_src.get = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda u, **_kw: (
            playlist.replace(
                '#EXT-X-KEY:METHOD=AES-256-GCM,URI="https://example.com/k32.bin",IV=0x00000000000000000000000000000001\n#EXTINF:5.0,\nhttps://example.com/seg0.bin\n',
                "",
            ).encode("utf-8")
            if u.endswith("m3u8")
            else init_bytes
        )
    )
    user_src._get_page = MagicMock(side_effect=AssertionError("should not fetch key URI when user key matches"))  # type: ignore[method-assign]
    files = user_src.get_stream_files("https://example.com/master.m3u8")
    assert isinstance(files[0].encryption_method, SampleAESEncryption)
    assert isinstance(files[1].encryption_method, CENCDecryption)
    assert files[1].init_section == init_bytes
    assert isinstance(files[2].encryption_method, CENCDecryption)
    assert files[2].encryption_method.key == key16


def test_get_stream_files_aes_256_gcm_and_clear_sample_aes():
    key32 = os.urandom(32)
    key16 = os.urandom(16)

    class _DummySource(Source):
        names = ["dummy"]
        match = [r"https://example\.com/.*"]

    playlist = (
        "#EXTM3U\n"
        "#EXT-X-MEDIA-SEQUENCE:5\n"
        '#EXT-X-KEY:METHOD=AES-256-GCM,URI="https://example.com/k32.bin"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg0.bin\n"
        '#EXT-X-KEY:METHOD=AES-256,URI="https://example.com/k32.bin"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg1.ts\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.com/k16.bin"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg2.ts\n"
    )
    src = _DummySource(build_default_options())
    src.get = MagicMock(return_value=playlist.encode("utf-8"))  # type: ignore[method-assign]
    src._get_page = MagicMock(side_effect=lambda u, **_kw: key32 if "k32" in u else key16)  # type: ignore[method-assign]

    files = src.get_stream_files("https://example.com/p.m3u8")
    assert isinstance(files[0].encryption_method, AESGCMEncryption)
    assert files[0].encryption_method.key == key32
    assert files[0].encryption_method.nonce == (5).to_bytes(12, "big")
    assert isinstance(files[1].encryption_method, AESCBCEncryption)
    assert files[1].encryption_method.key == key32
    assert isinstance(files[2].encryption_method, SampleAESEncryption)
    assert files[2].encryption_method.key == key16


def test_parse_decryption_key_spec_and_schema_and_pipeline():
    k16 = "00112233445566778899aabbccddeeff"
    k32 = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    kid = "11223344-5566-7788-9900-aabbccddeeff"

    default_k, kmap = parse_decryption_key_spec(f"{k16}, {kid}:{k32}")
    assert default_k == bytes.fromhex(k16)
    assert kmap == {bytes.fromhex(kid.replace("-", "")): bytes.fromhex(k32)}

    with pytest.raises(ValueError):
        parse_decryption_key_spec("not-hex")
    with pytest.raises(ValueError):
        parse_decryption_key_spec("abcd")
    with pytest.raises(ValueError):
        parse_decryption_key_spec(f"{k16},{k32}")

    assert "decryption_key" in OPTIONS_BY_NAME
    assert OPTIONS_BY_NAME["decryption_key"] not in PERSISTED_GUI_OPTION_SPECS

    book = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="CENC"),
        files=[
            AudiobookFile(url="https://example.com/a.m4b", ext="m4b"),
            AudiobookFile(url="https://example.com/b.mp3", ext="mp3"),
        ],
    )
    _apply_user_decryption_key(book, build_default_options(decryption_key=k16))
    assert isinstance(book.files[0].encryption_method, CENCDecryption)
    assert book.files[0].encryption_method.key == bytes.fromhex(k16)
    assert book.files[1].encryption_method is None


def _build_widevine_pssh_box(kid: bytes) -> bytes:
    import struct
    from audiobookdl.utils.pssh import WIDEVINE_SYSTEM_ID

    proto = b"\x12\x10" + kid
    body = struct.pack(">B3s", 0, b"\x00\x00\x00") + WIDEVINE_SYSTEM_ID.bytes + struct.pack(">I", len(proto)) + proto
    return struct.pack(">I4s", 8 + len(body), b"pssh") + body


def test_hls_drm_pssh_error_reports_system_and_kid_and_validates_key_map():
    import base64
    from audiobookdl.utils.pssh import WIDEVINE_SYSTEM_ID

    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wrong_kid = bytes.fromhex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    key16 = bytes.fromhex("00112233445566778899aabbccddeeff")
    pssh_b64 = base64.b64encode(_build_widevine_pssh_box(kid)).decode("ascii")

    playlist = (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,{pssh_b64}",'
        f'KEYFORMAT="urn:uuid:{WIDEVINE_SYSTEM_ID}"\n'
        "#EXTINF:5.0,\n"
        "https://example.com/seg0.m4s\n"
    )

    class _DummySource(Source):
        names = ["dummy"]
        match = [r"https://example\.com/.*"]

    src_no_key = _DummySource(build_default_options(decryption_key=None))
    src_no_key.get = MagicMock(return_value=playlist.encode("utf-8"))  # type: ignore[method-assign]
    with pytest.raises(GenericAudiobookDLException) as exc_info:
        src_no_key.get_stream_files("https://example.com/p.m3u8")
    msg = str(exc_info.value)
    assert "Widevine" in msg
    assert kid.hex() in msg

    # Wrong KID:KEY fails fast before segment downloads
    src_wrong = _DummySource(build_default_options(decryption_key=f"{wrong_kid.hex()}:{key16.hex()}"))
    src_wrong.get = MagicMock(return_value=playlist.encode("utf-8"))  # type: ignore[method-assign]
    with pytest.raises(GenericAudiobookDLException, match=kid.hex()):
        src_wrong.get_stream_files("https://example.com/p.m3u8")

    # Bare key with single PSSH KID automatically populates kid and key_map
    src_bare = _DummySource(build_default_options(decryption_key=key16.hex()))
    src_bare.get = MagicMock(return_value=playlist.encode("utf-8"))  # type: ignore[method-assign]
    files = src_bare.get_stream_files("https://example.com/p.m3u8")
    assert isinstance(files[0].encryption_method, CENCDecryption)
    assert files[0].encryption_method.kid == kid
    assert files[0].encryption_method.key_map == {kid: key16}


def test_pipeline_and_decrypt_file_cenc_pssh_reconciliation(tmp_path):
    import struct

    kid = bytes.fromhex("11223344556677889900aabbccddeeff")
    wrong_kid = bytes.fromhex("223344556677889900aabbccddeeff11")
    key16 = bytes.fromhex("00112233445566778899aabbccddeeff")
    pssh_box = _build_widevine_pssh_box(kid)
    moov_box = struct.pack(">I4s", 8 + len(pssh_box), b"moov") + pssh_box
    init_section = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x02\x00" + moov_box

    book_mismatch = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="Mismatch"),
        files=[AudiobookFile(url="https://example.com/seg.m4s", ext="m4s", init_section=init_section)],
    )
    with pytest.raises(GenericAudiobookDLException) as exc_info:
        _apply_user_decryption_key(
            book_mismatch,
            build_default_options(decryption_key=f"{wrong_kid.hex()}:{key16.hex()}"),
        )
    assert kid.hex() in str(exc_info.value)
    assert wrong_kid.hex() in str(exc_info.value)

    book_bare = Audiobook(
        session=MagicMock(),
        metadata=AudiobookMetadata(title="BareKey"),
        files=[AudiobookFile(url="https://example.com/seg.m4s", ext="m4s", init_section=init_section)],
    )
    _apply_user_decryption_key(book_bare, build_default_options(decryption_key=key16.hex()))
    assert isinstance(book_bare.files[0].encryption_method, CENCDecryption)
    assert book_bare.files[0].encryption_method.kid == kid
    assert book_bare.files[0].encryption_method.key_map == {kid: key16}

    # Direct MP4 file on disk without init_section also fails fast on KID mismatch before spawning ffmpeg
    mp4_path = tmp_path / "cenc.mp4"
    mp4_path.write_bytes(init_section + struct.pack(">I4s", 12, b"mdat") + b"\x00\x00\x00\x00")
    with (
        patch("audiobookdl.output.encryption.find_executable", return_value="ffmpeg"),
        patch("audiobookdl.output.encryption.subprocess.run", side_effect=AssertionError("ffmpeg should not run")),
    ):
        with pytest.raises(GenericAudiobookDLException, match=kid.hex()):
            decrypt_file(str(mp4_path), CENCDecryption(key_map={wrong_kid: key16}))


@pytest.mark.gui
def test_gui_options_panel_decryption_key_masking_and_validation(tmp_path):
    import tkinter as tk
    from audiobookdl.gui.app import AudiobookDLApp

    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError as exc:
        pytest.skip(f"Tkinter display unavailable: {exc}")

    try:
        app = AudiobookDLApp(root, config_location=str(tmp_path / "cfg.toml"))
        dl_page = app.shell.get_page("download")
        assert dl_page is not None
        panel = dl_page.options_panel
        assert panel._decryption_key_entry.cget("show") == "*"

        panel._show_decryption_key_var.set(True)
        panel._toggle_decryption_key_visibility()
        assert panel._decryption_key_entry.cget("show") == ""

        app.decryption_key_var.set("bad-key")
        panel._validate_inline_fields()
        assert "hex" in panel._validation_var.get().lower()
        with pytest.raises(ValueError):
            app.build_options(urls_override=["https://example.com/book"])

        valid_hex = "00112233445566778899aabbccddeeff"
        app.decryption_key_var.set(valid_hex)
        panel._validate_inline_fields()
        assert panel._validation_var.get() == ""
        opts = app.build_options(urls_override=["https://example.com/book"])
        assert opts.decryption_key == valid_hex
    finally:
        root.destroy()

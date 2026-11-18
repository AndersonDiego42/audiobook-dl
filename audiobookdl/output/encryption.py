from collections.abc import Callable
import os
import struct
import subprocess
from typing import Literal, Protocol

from Crypto.Cipher import AES, ChaCha20, ChaCha20_Poly1305
from Crypto.Util.Padding import unpad

from audiobookdl.exceptions import GenericAudiobookDLException, MissingDependency
from audiobookdl.utils import find_executable, subprocess_kwargs
from audiobookdl.utils.audiobook import (
    AESCBCEncryption,
    AESCTREncryption,
    AESGCMEncryption,
    AudiobookFileEncryption,
    CENCDecryption,
    ChaCha20Encryption,
    SampleAESEncryption,
    reconcile_cenc_decryption,
)
from audiobookdl.utils.pssh import extract_cenc_kids_safe

DECRYPT_CHUNK_SIZE = 64 * 1024  # 64 KiB, an exact multiple of AES's 16-byte block size
AEAD_TAG_SIZE = 16
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
_VALID_AES_KEY_SIZES = (16, 24, 32)
_VALID_CHACHA20_NONCE_SIZES = (8, 12, 24)
_SAMPLE_AES_STREAM_TYPES = frozenset({0x0F, 0x1B, 0x81, 0x87})

# AC-3 frame size table (in 16-bit words) indexed by [fscod][frmsizecod // 2] (ATSC A/52 Table 5.18)
_AC3_FRAME_SIZE_WORDS: tuple[tuple[int, ...], ...] = (
    (64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 640, 768, 896, 1024, 1152, 1280),
    (69, 87, 104, 121, 139, 174, 208, 243, 278, 348, 417, 487, 557, 696, 835, 975, 1114, 1253, 1393),
    (96, 120, 144, 168, 192, 240, 288, 336, 384, 480, 576, 672, 768, 960, 1152, 1344, 1536, 1728, 1920),
)


class StreamDecryptor(Protocol):
    """Protocol for incremental byte-stream decryptors."""

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        """Decrypt the next ciphertext chunk, returning any ready plaintext bytes."""
        ...

    def finalize(self) -> bytes:
        """Flush trailing buffered bytes and verify padding or authentication tags."""
        ...


def _validate_aes_key(key: bytes) -> None:
    if len(key) not in _VALID_AES_KEY_SIZES:
        raise ValueError(f"AES key must be 16, 24, or 32 bytes (got {len(key)})")


class AESCBCStreamDecryptor:
    """Incrementally decrypts AES-CBC byte chunks with PKCS7 or no padding."""

    def __init__(
        self,
        key: bytes,
        iv: bytes,
        *,
        padding: Literal["pkcs7", "none"] = "pkcs7",
    ) -> None:
        _validate_aes_key(key)
        if len(iv) != AES.block_size:
            raise ValueError(f"AES-CBC IV must be {AES.block_size} bytes (got {len(iv)})")
        if padding not in ("pkcs7", "none"):
            raise ValueError(f"Unsupported AES-CBC padding mode: {padding!r}")
        self._cipher = AES.new(key, AES.MODE_CBC, iv)
        self._padding = padding
        self._buffer = bytearray()

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        """Buffer `chunk` and decrypt complete blocks (retaining final block when unpadding PKCS7)."""
        if not chunk:
            return b""
        self._buffer.extend(chunk)
        if self._padding == "pkcs7":
            # Retain at least 1 byte (up to one full block) so finalize() can validate and strip PKCS7 padding.
            if len(self._buffer) <= AES.block_size:
                return b""
            usable = ((len(self._buffer) - 1) // AES.block_size) * AES.block_size
        else:
            if len(self._buffer) < AES.block_size:
                return b""
            usable = (len(self._buffer) // AES.block_size) * AES.block_size
        out = self._cipher.decrypt(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        return out

    def finalize(self) -> bytes:
        """Decrypt any final buffered block(s) and validate alignment/padding."""
        if not self._buffer:
            return b""
        tail = bytes(self._buffer)
        self._buffer.clear()
        if len(tail) % AES.block_size != 0:
            raise ValueError(
                f"AES-CBC stream length must be a multiple of {AES.block_size} bytes "
                f"(got {len(tail)} trailing bytes)"
            )
        decrypted = self._cipher.decrypt(tail)
        if self._padding == "pkcs7":
            return unpad(decrypted, AES.block_size)
        return decrypted


class AESCTRStreamDecryptor:
    """Incrementally decrypts AES-CTR byte chunks."""

    def __init__(self, key: bytes, nonce: bytes) -> None:
        _validate_aes_key(key)
        if len(nonce) > AES.block_size:
            raise ValueError(f"AES-CTR nonce/IV must be <= {AES.block_size} bytes (got {len(nonce)})")
        if len(nonce) == AES.block_size:
            self._cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=nonce)
        else:
            self._cipher = AES.new(key, AES.MODE_CTR, nonce=nonce)

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        return self._cipher.decrypt(chunk)

    def finalize(self) -> bytes:
        return b""


class _AEADStreamHelper:
    """Shared AEAD chunk-buffering helper supporting explicit or trailing 16-byte authentication tags."""

    def __init__(
        self,
        decrypt_fn: Callable[[bytes], bytes],
        verify_fn: Callable[[bytes], None],
        *,
        tag: bytes | None = None,
    ) -> None:
        if tag is not None and len(tag) != AEAD_TAG_SIZE:
            raise ValueError(f"AEAD tag must be {AEAD_TAG_SIZE} bytes (got {len(tag)})")
        self._decrypt_fn = decrypt_fn
        self._verify_fn = verify_fn
        self._explicit_tag = tag
        self._buffer = bytearray()
        self._finalized = False

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        if self._finalized:
            raise ValueError("Cannot decrypt chunks after finalize()")
        if not chunk:
            return b""
        if self._explicit_tag is not None:
            return self._decrypt_fn(chunk)
        self._buffer.extend(chunk)
        if len(self._buffer) <= AEAD_TAG_SIZE:
            return b""
        usable = len(self._buffer) - AEAD_TAG_SIZE
        out = self._decrypt_fn(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        return out

    def finalize(self) -> bytes:
        if self._finalized:
            return b""
        self._finalized = True
        if self._explicit_tag is not None:
            self._verify_fn(self._explicit_tag)
            return b""
        if len(self._buffer) != AEAD_TAG_SIZE:
            received = len(self._buffer)
            self._buffer.clear()
            raise ValueError(
                f"AEAD stream is shorter than the {AEAD_TAG_SIZE}-byte authentication tag "
                f"(got {received} bytes)"
            )
        tag = bytes(self._buffer)
        self._buffer.clear()
        self._verify_fn(tag)
        return b""


class AESGCMStreamDecryptor:
    """Incrementally decrypts and authenticates AES-GCM streams."""

    def __init__(
        self,
        key: bytes,
        nonce: bytes,
        *,
        tag: bytes | None = None,
        aad: bytes = b"",
    ) -> None:
        _validate_aes_key(key)
        if not nonce:
            raise ValueError("AES-GCM nonce must not be empty")
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        if aad:
            cipher.update(aad)
        self._helper = _AEADStreamHelper(cipher.decrypt, cipher.verify, tag=tag)

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        return self._helper.decrypt_chunk(chunk)

    def finalize(self) -> bytes:
        return self._helper.finalize()


class ChaCha20StreamDecryptor:
    """Incrementally decrypts ChaCha20 or authenticated ChaCha20-Poly1305 streams."""

    def __init__(
        self,
        key: bytes,
        nonce: bytes,
        *,
        tag: bytes | None = None,
        aad: bytes = b"",
        poly1305: bool = False,
    ) -> None:
        if len(key) != 32:
            raise ValueError(f"ChaCha20 key must be 32 bytes (got {len(key)})")
        if len(nonce) not in _VALID_CHACHA20_NONCE_SIZES:
            raise ValueError(
                f"ChaCha20 nonce must be 8, 12, or 24 bytes (got {len(nonce)})"
            )
        if poly1305:
            aead_cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
            if aad:
                aead_cipher.update(aad)
            self._helper: _AEADStreamHelper | None = _AEADStreamHelper(
                aead_cipher.decrypt,
                aead_cipher.verify,
                tag=tag,
            )
            self._stream_cipher = None
        else:
            if tag is not None or aad:
                raise ValueError("tag and aad require poly1305=True for ChaCha20Encryption")
            self._helper = None
            self._stream_cipher = ChaCha20.new(key=key, nonce=nonce)

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        if self._helper is not None:
            return self._helper.decrypt_chunk(chunk)
        assert self._stream_cipher is not None
        return self._stream_cipher.decrypt(chunk)

    def finalize(self) -> bytes:
        if self._helper is not None:
            return self._helper.finalize()
        return b""


def create_stream_decryptor(encryption_method: AudiobookFileEncryption) -> StreamDecryptor | None:
    """Return a `StreamDecryptor` for byte-stream ciphers, or `None` for container-level formats."""
    if isinstance(encryption_method, AESCBCEncryption):
        return AESCBCStreamDecryptor(
            encryption_method.key,
            encryption_method.iv,
            padding=encryption_method.padding,
        )
    if isinstance(encryption_method, AESCTREncryption):
        return AESCTRStreamDecryptor(encryption_method.key, encryption_method.nonce)
    if isinstance(encryption_method, AESGCMEncryption):
        return AESGCMStreamDecryptor(
            encryption_method.key,
            encryption_method.nonce,
            tag=encryption_method.tag,
            aad=encryption_method.aad,
        )
    if isinstance(encryption_method, ChaCha20Encryption):
        return ChaCha20StreamDecryptor(
            encryption_method.key,
            encryption_method.nonce,
            tag=encryption_method.tag,
            aad=encryption_method.aad,
            poly1305=encryption_method.poly1305,
        )
    if isinstance(encryption_method, (SampleAESEncryption, CENCDecryption)):
        return None
    raise ValueError(f"Unsupported encryption method: {type(encryption_method).__name__}")


def _decrypt_cbc_blocks_nopad(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt complete 16-byte blocks with AES-CBC (no padding) and leave trailing bytes clear."""
    usable = (len(data) // AES.block_size) * AES.block_size
    if usable == 0:
        return data
    decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(data[:usable])
    return decrypted + data[usable:]


def _decrypt_adts_aac(payload: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt ADTS AAC frames according to Apple HLS SAMPLE-AES."""
    out = bytearray(payload)
    pos = 0
    total = len(out)
    while pos + 7 <= total:
        if out[pos] != 0xFF or (out[pos + 1] & 0xF0) != 0xF0:
            pos += 1
            continue
        header_len = 9 if (out[pos + 1] & 0x01) == 0 else 7
        frame_len = (
            ((out[pos + 3] & 0x03) << 11)
            | (out[pos + 4] << 3)
            | ((out[pos + 5] & 0xE0) >> 5)
        )
        if frame_len < header_len or pos + frame_len > total:
            break
        enc_start = pos + header_len + 16
        frame_end = pos + frame_len
        if enc_start < frame_end:
            out[enc_start:frame_end] = _decrypt_cbc_blocks_nopad(
                bytes(out[enc_start:frame_end]), key, iv
            )
        pos = frame_end
    return bytes(out)


def _ac3_syncframe_length(buf: bytearray, pos: int) -> int | None:
    if pos + 6 > len(buf):
        return None
    if buf[pos] != 0x0B or buf[pos + 1] != 0x77:
        return None
    bsid = (buf[pos + 5] >> 3) & 0x1F
    if bsid <= 10:
        fscod = (buf[pos + 4] >> 6) & 0x03
        frmsizecod = buf[pos + 4] & 0x3F
        if fscod >= 3 or frmsizecod >= 38:
            return None
        words = _AC3_FRAME_SIZE_WORDS[fscod][frmsizecod // 2]
        if fscod == 1 and (frmsizecod % 2) == 1:
            words += 1
        return words * 2
    # E-AC-3 (bsid > 10)
    numblk_words = (((buf[pos + 2] & 0x07) << 8) | buf[pos + 3]) + 1
    return numblk_words * 2


def _decrypt_ac3_frames(payload: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AC-3 / E-AC-3 syncframes according to Apple HLS SAMPLE-AES."""
    out = bytearray(payload)
    pos = 0
    total = len(out)
    while pos + 6 <= total:
        frame_len = _ac3_syncframe_length(out, pos)
        if frame_len is None or frame_len < 16 or pos + frame_len > total:
            pos += 1
            continue
        enc_start = pos + 16
        frame_end = pos + frame_len
        if enc_start < frame_end:
            out[enc_start:frame_end] = _decrypt_cbc_blocks_nopad(
                bytes(out[enc_start:frame_end]), key, iv
            )
        pos = frame_end
    return bytes(out)


def _decrypt_h264_nalus(payload: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt H.264 Annex-B IDR/non-IDR slice NALUs (10% pattern) per HLS SAMPLE-AES."""
    out = bytearray(payload)
    starts: list[tuple[int, int]] = []
    i = 0
    total = len(out)
    while i + 3 <= total:
        if out[i : i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, i + 4))
            i += 4
        elif out[i : i + 3] == b"\x00\x00\x01":
            starts.append((i, i + 3))
            i += 3
        else:
            i += 1
    for idx, (_sc_start, nal_start) in enumerate(starts):
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else total
        if nal_end <= nal_start:
            continue
        nal_type = out[nal_start] & 0x1F
        if nal_type not in (1, 5):
            continue
        # Leave NAL header (1 byte) + first 32 bytes unencrypted; 16-byte blocks in 1-encrypted / 9-clear pattern
        nal_len = nal_end - nal_start
        if nal_len <= 48:
            continue
        enc_pos = nal_start + 33
        cipher = AES.new(key, AES.MODE_CBC, iv)
        block_idx = 0
        while enc_pos + AES.block_size <= nal_end:
            if block_idx % 10 == 0:
                out[enc_pos : enc_pos + AES.block_size] = cipher.decrypt(
                    bytes(out[enc_pos : enc_pos + AES.block_size])
                )
            enc_pos += AES.block_size
            block_idx += 1
    return bytes(out)


def _decrypt_sample_aes_elementary(stream_type: int, payload: bytes, key: bytes, iv: bytes) -> bytes:
    if stream_type == 0x0F:
        return _decrypt_adts_aac(payload, key, iv)
    if stream_type in (0x81, 0x87):
        return _decrypt_ac3_frames(payload, key, iv)
    if stream_type == 0x1B:
        return _decrypt_h264_nalus(payload, key, iv)
    return payload


def _extract_ts_payload_slice(pkt: bytes) -> tuple[int, int] | None:
    """Return `(start_offset, end_offset)` of the payload within a 188-byte TS packet, or `None`."""
    afc = (pkt[3] >> 4) & 0x03
    if afc in (0, 2):
        return None
    offset = 4
    if afc == 3:
        afl = pkt[4]
        offset = 5 + afl
    if offset >= TS_PACKET_SIZE:
        return None
    return offset, TS_PACKET_SIZE


def _parse_ts_psi_section(pkt: bytes) -> bytes | None:
    bounds = _extract_ts_payload_slice(pkt)
    if bounds is None or not (pkt[1] & 0x40):
        return None
    start, end = bounds
    if start >= end:
        return None
    pointer = pkt[start]
    sec_start = start + 1 + pointer
    if sec_start + 3 > end:
        return None
    sec_len = ((pkt[sec_start + 1] & 0x0F) << 8) | pkt[sec_start + 2]
    sec_end = sec_start + 3 + sec_len
    if sec_end > end:
        return None
    return pkt[sec_start:sec_end]


def _parse_pmt_streams(ts_data: bytes) -> dict[int, int]:
    """Extract `{elementary_pid: stream_type}` from PAT and PMT in an MPEG-TS buffer."""
    pmt_pids: set[int] = set()
    elem_streams: dict[int, int] = {}
    for pkt_offset in range(0, len(ts_data), TS_PACKET_SIZE):
        pkt = ts_data[pkt_offset : pkt_offset + TS_PACKET_SIZE]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x0000:
            sec = _parse_ts_psi_section(pkt)
            if sec is None or sec[0] != 0x00 or len(sec) < 12:
                continue
            pos = 8
            end = len(sec) - 4
            while pos + 4 <= end:
                prog_num = (sec[pos] << 8) | sec[pos + 1]
                prog_pid = ((sec[pos + 2] & 0x1F) << 8) | sec[pos + 3]
                if prog_num != 0:
                    pmt_pids.add(prog_pid)
                pos += 4
        elif pid in pmt_pids:
            sec = _parse_ts_psi_section(pkt)
            if sec is None or sec[0] != 0x02 or len(sec) < 16:
                continue
            prog_info_len = ((sec[10] & 0x0F) << 8) | sec[11]
            pos = 12 + prog_info_len
            end = len(sec) - 4
            while pos + 5 <= end:
                stream_type = sec[pos]
                elem_pid = ((sec[pos + 1] & 0x1F) << 8) | sec[pos + 2]
                es_info_len = ((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4]
                if stream_type in _SAMPLE_AES_STREAM_TYPES:
                    elem_streams[elem_pid] = stream_type
                pos += 5 + es_info_len
    return elem_streams


def decrypt_mpegts_sample_aes(ts_data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt an HLS `SAMPLE-AES` MPEG-TS segment in-place over TS packet payloads."""
    if len(key) != 16:
        raise ValueError(f"SAMPLE-AES key must be 16 bytes (got {len(key)})")
    if len(iv) != AES.block_size:
        raise ValueError(f"SAMPLE-AES IV must be {AES.block_size} bytes (got {len(iv)})")
    if not ts_data:
        return b""
    if len(ts_data) % TS_PACKET_SIZE != 0:
        raise ValueError(
            f"MPEG-TS stream length must be a multiple of {TS_PACKET_SIZE} bytes "
            f"(got {len(ts_data)} bytes)"
        )
    for pkt_offset in range(0, len(ts_data), TS_PACKET_SIZE):
        if ts_data[pkt_offset] != TS_SYNC_BYTE:
            raise ValueError(f"Invalid MPEG-TS sync byte at offset {pkt_offset}")

    elem_streams = _parse_pmt_streams(ts_data)
    if not elem_streams:
        return ts_data

    out = bytearray(ts_data)
    pes_slices: dict[int, list[tuple[int, int]]] = {pid: [] for pid in elem_streams}

    def _commit_pes(pid: int) -> None:
        slices = pes_slices[pid]
        if not slices:
            return
        pes = bytearray()
        for start, end in slices:
            pes.extend(out[start:end])
        if len(pes) >= 9 and pes[:3] == b"\x00\x00\x01":
            pes_header_len = 9 + pes[8]
            if pes_header_len < len(pes):
                decrypted_es = _decrypt_sample_aes_elementary(
                    elem_streams[pid],
                    bytes(pes[pes_header_len:]),
                    key,
                    iv,
                )
                pes[pes_header_len:] = decrypted_es
                cursor = 0
                for start, end in slices:
                    span = end - start
                    out[start:end] = pes[cursor : cursor + span]
                    cursor += span
        slices.clear()

    for pkt_offset in range(0, len(out), TS_PACKET_SIZE):
        pkt = bytes(out[pkt_offset : pkt_offset + TS_PACKET_SIZE])
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid not in elem_streams:
            continue
        bounds = _extract_ts_payload_slice(pkt)
        if bounds is None:
            continue
        if pkt[1] & 0x40:
            _commit_pes(pid)
        rel_start, rel_end = bounds
        pes_slices[pid].append((pkt_offset + rel_start, pkt_offset + rel_end))

    for pid in elem_streams:
        _commit_pes(pid)
    return bytes(out)


def decrypt_file_sample_aes(
    path: str,
    key: bytes,
    iv: bytes,
    output_path: str | None = None,
    *,
    prefix: bytes = b"",
) -> None:
    """Decrypt an MPEG-TS `SAMPLE-AES` file atomically via a sibling temp file."""
    target = output_path or path
    tmp_path = f"{target}.dec.tmp"
    try:
        with open(path, "rb") as src:
            raw = src.read()
        decrypted = decrypt_mpegts_sample_aes(raw, key, iv)
        with open(tmp_path, "wb") as dst:
            if prefix:
                dst.write(prefix)
            if decrypted:
                dst.write(decrypted)
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _build_ffmpeg_cenc_args(method: CENCDecryption) -> list[str]:
    if method.key is None and not method.key_map:
        raise ValueError("CENC decryption requires at least one key or KID:KEY mapping")
    args: list[str] = []
    if method.key is not None:
        if len(method.key) != 16:
            raise ValueError(f"CENC key must be 16 bytes (got {len(method.key)})")
        if method.kid is not None and len(method.kid) != 16:
            raise ValueError(f"CENC KID must be 16 bytes (got {len(method.kid)})")
        args.extend(["-decryption_key", method.key.hex()])
    combined_map = dict(method.key_map)
    if method.kid is not None and method.key is not None and method.kid not in combined_map:
        combined_map[method.kid] = method.key
    if combined_map:
        for kid, key in combined_map.items():
            if len(kid) != 16 or len(key) != 16:
                raise ValueError("CENC KID and key entries must each be 16 bytes")
        dict_expr = ":".join(f"{kid.hex()}={key.hex()}" for kid, key in combined_map.items())
        args.extend(["-decryption_keys", dict_expr])
    return args


_MAX_CENC_METADATA_BOX_BYTES = 4 * 1024 * 1024
_MAX_CENC_METADATA_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_CENC_TOP_LEVEL_BOXES = 128


def _read_mp4_init_boxes(path: str) -> bytes:
    """Read top-level `moov`/`moof`/`pssh` boxes from an MP4 file on disk without loading `mdat`."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return b""
    if file_size < 8:
        return b""
    collected = bytearray()
    pos = 0
    try:
        with open(path, "rb") as src:
            for _ in range(_MAX_CENC_TOP_LEVEL_BOXES):
                if pos + 8 > file_size:
                    break
                src.seek(pos)
                header = src.read(8)
                if len(header) < 8:
                    break
                raw_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if raw_size == 1:
                    if pos + 16 > file_size:
                        break
                    ext_hdr = src.read(8)
                    if len(ext_hdr) < 8:
                        break
                    box_size = struct.unpack(">Q", ext_hdr)[0]
                    header_size = 16
                elif raw_size == 0:
                    box_size = file_size - pos
                else:
                    box_size = raw_size
                if box_size < header_size or pos + box_size > file_size:
                    break
                if (
                    box_type in (b"moov", b"moof", b"pssh")
                    and box_size <= _MAX_CENC_METADATA_BOX_BYTES
                    and len(collected) + box_size <= _MAX_CENC_METADATA_TOTAL_BYTES
                ):
                    src.seek(pos)
                    chunk = src.read(box_size)
                    if len(chunk) == box_size:
                        collected.extend(chunk)
                pos += box_size
    except OSError:
        return b""
    return bytes(collected)


def decrypt_file_cenc(
    path: str,
    method: CENCDecryption,
    output_path: str | None = None,
    *,
    prefix: bytes = b"",
) -> None:
    """Decrypt an ISO BMFF CENC/cbcs MP4 or fMP4 file using FFmpeg and replace atomically."""
    ffmpeg = find_executable("ffmpeg")
    if ffmpeg is None:
        raise MissingDependency(dependency="ffmpeg")
    effective_method = CENCDecryption(
        key=method.key,
        kid=method.kid,
        key_map=dict(method.key_map),
    )
    known_kids = extract_cenc_kids_safe(prefix) if prefix else ()
    if not known_kids:
        known_kids = extract_cenc_kids_safe(_read_mp4_init_boxes(path))
    reconcile_cenc_decryption(effective_method, known_kids)
    key_args = _build_ffmpeg_cenc_args(effective_method)
    target = output_path or path
    tmp_out = f"{target}.dec.tmp"
    combined_in: str | None = None
    try:
        input_path = path
        if prefix:
            combined_in = f"{target}.cenc_in.tmp"
            with open(combined_in, "wb") as dst, open(path, "rb") as src:
                dst.write(prefix)
                while True:
                    chunk = src.read(DECRYPT_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
            input_path = combined_in
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            *key_args,
            "-i",
            input_path,
            "-c",
            "copy",
            "-f",
            "mp4",
            tmp_out,
        ]
        result = subprocess.run(command, capture_output=True, text=True, **subprocess_kwargs())
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise GenericAudiobookDLException(
                f"FFmpeg CENC decryption failed{f': {detail}' if detail else ''}"
            )
        os.replace(tmp_out, target)
    except BaseException:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        raise
    finally:
        if combined_in is not None and os.path.exists(combined_in):
            try:
                os.remove(combined_in)
            except OSError:
                pass


def _decrypt_file_stream(
    path: str,
    decryptor: StreamDecryptor,
    output_path: str | None = None,
    *,
    prefix: bytes = b"",
) -> None:
    """Stream-decrypt a file via a sibling temp file and replace atomically."""
    target = output_path or path
    tmp_path = f"{target}.dec.tmp"
    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
            if prefix:
                dst.write(prefix)
            while True:
                chunk = src.read(DECRYPT_CHUNK_SIZE)
                if not chunk:
                    break
                out = decryptor.decrypt_chunk(chunk)
                if out:
                    dst.write(out)
            tail = decryptor.finalize()
            if tail:
                dst.write(tail)
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def decrypt_file(
    path: str,
    encryption_method: AudiobookFileEncryption,
    output_path: str | None = None,
    *,
    prefix: bytes = b"",
) -> None:
    """Decrypt encrypted file in place, or atomically into `output_path` when provided."""
    decryptor = create_stream_decryptor(encryption_method)
    if decryptor is not None:
        _decrypt_file_stream(path, decryptor, output_path=output_path, prefix=prefix)
        return
    if isinstance(encryption_method, SampleAESEncryption):
        decrypt_file_sample_aes(
            path,
            encryption_method.key,
            encryption_method.iv,
            output_path=output_path,
            prefix=prefix,
        )
        return
    if isinstance(encryption_method, CENCDecryption):
        decrypt_file_cenc(
            path,
            encryption_method,
            output_path=output_path,
            prefix=prefix,
        )
        return
    raise ValueError(f"Unsupported encryption method: {type(encryption_method).__name__}")

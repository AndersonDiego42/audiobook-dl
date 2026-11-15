from Crypto.Cipher import AES
import os
import pytest

from audiobookdl.output.encryption import DECRYPT_CHUNK_SIZE, decrypt_file, decrypt_file_aes
from audiobookdl.utils.audiobook import AESEncryption


def test_decrypt_file_streams_across_multiple_64k_chunks(tmp_path):
    assert DECRYPT_CHUNK_SIZE == 64 * 1024
    assert DECRYPT_CHUNK_SIZE % AES.block_size == 0

    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    # Span multiple 64 KiB chunks plus a non-aligned-to-64K trailing AES block
    plaintext = os.urandom(DECRYPT_CHUNK_SIZE * 2 + 16 * 5)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)

    target = tmp_path / "segment.ts.tmp"
    target.write_bytes(ciphertext)

    decrypt_file(str(target), AESEncryption(key=key, iv=iv))

    assert target.read_bytes() == plaintext
    assert not (tmp_path / "segment.ts.tmp.dec.tmp").exists()


def test_decrypt_file_aes_handles_empty_file(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    target = tmp_path / "empty.mp3"
    target.write_bytes(b"")

    decrypt_file_aes(str(target), key, iv)

    assert target.read_bytes() == b""
    assert not (tmp_path / "empty.mp3.dec.tmp").exists()


def test_decrypt_file_aes_preserves_original_and_cleans_tmp_on_error(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    # Invalid AES-CBC length (not a multiple of 16 bytes) triggers ValueError
    bad_payload = b"short-not-16-bytes"
    target = tmp_path / "broken.mp3"
    target.write_bytes(bad_payload)

    with pytest.raises(ValueError):
        decrypt_file_aes(str(target), key, iv)

    assert target.read_bytes() == bad_payload
    assert not (tmp_path / "broken.mp3.dec.tmp").exists()


def test_decrypt_file_with_output_path_preserves_source_ciphertext(tmp_path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = os.urandom(16 * 10)
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)

    src_tmp = tmp_path / "segment.ts.tmp"
    dst_file = tmp_path / "segment.ts"
    src_tmp.write_bytes(ciphertext)

    decrypt_file(str(src_tmp), AESEncryption(key=key, iv=iv), output_path=str(dst_file))

    assert dst_file.read_bytes() == plaintext
    assert src_tmp.read_bytes() == ciphertext
    assert not (tmp_path / "segment.ts.dec.tmp").exists()

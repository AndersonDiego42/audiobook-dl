import os
from typing import Optional
from Crypto.Cipher import AES
from audiobookdl.utils.audiobook import AudiobookFileEncryption, AESEncryption

DECRYPT_CHUNK_SIZE = 64 * 1024  # 64 KiB, an exact multiple of AES's 16-byte block size


class AESStreamDecryptor:
    """Incrementally decrypts AES-CBC byte chunks of arbitrary boundaries."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._cipher = AES.new(key, AES.MODE_CBC, iv)
        self._buffer = bytearray()

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        """Buffer `chunk` and decrypt any complete 16-byte AES blocks."""
        self._buffer.extend(chunk)
        usable = (len(self._buffer) // AES.block_size) * AES.block_size
        if usable <= 0:
            return b""
        out = self._cipher.decrypt(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        return out

    def finalize(self) -> bytes:
        """Decrypt any trailing bytes at end-of-stream."""
        if not self._buffer:
            return b""
        tail = bytes(self._buffer)
        self._buffer.clear()
        return self._cipher.decrypt(tail)


def decrypt_file(
    path: str,
    encryption_method: AudiobookFileEncryption,
    output_path: Optional[str] = None,
) -> None:
    """Decrypt encrypted file in place, or atomically into `output_path` when provided."""
    if isinstance(encryption_method, AESEncryption):
        decrypt_file_aes(
            path,
            encryption_method.key,
            encryption_method.iv,
            output_path=output_path,
        )
    else:
        raise ValueError(f"Unsupported encryption method: {type(encryption_method).__name__}")


def decrypt_file_aes(
    path: str,
    key: bytes,
    iv: bytes,
    output_path: Optional[str] = None,
) -> None:
    """Stream-decrypt an AES-CBC encrypted file via a sibling temp file and replace atomically."""
    target = output_path or path
    tmp_path = f"{target}.dec.tmp"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
            while True:
                chunk = src.read(DECRYPT_CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(cipher.decrypt(chunk))
        os.replace(tmp_path, target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

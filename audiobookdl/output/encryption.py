import os
from typing import Optional
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from audiobookdl.utils.audiobook import AudiobookFileEncryption, AESEncryption

DECRYPT_CHUNK_SIZE = 64 * 1024  # 64 KiB, an exact multiple of AES's 16-byte block size


class AESStreamDecryptor:
    """Incrementally decrypts PKCS7-padded AES-CBC byte chunks of arbitrary boundaries."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._cipher = AES.new(key, AES.MODE_CBC, iv)
        self._buffer = bytearray()

    def decrypt_chunk(self, chunk: bytes) -> bytes:
        """Buffer `chunk` and decrypt complete blocks while retaining the final block for unpadding."""
        if not chunk:
            return b""
        self._buffer.extend(chunk)
        # Retain at least 1 byte (up to one full block) so finalize() can validate and strip PKCS7 padding.
        if len(self._buffer) <= AES.block_size:
            return b""
        usable = ((len(self._buffer) - 1) // AES.block_size) * AES.block_size
        out = self._cipher.decrypt(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        return out

    def finalize(self) -> bytes:
        """Decrypt the final buffered block(s) and strip PKCS7 padding."""
        if not self._buffer:
            return b""
        tail = bytes(self._buffer)
        self._buffer.clear()
        if len(tail) % AES.block_size != 0:
            raise ValueError(
                f"AES-CBC stream length must be a multiple of {AES.block_size} bytes "
                f"(got {len(tail)} trailing bytes)"
            )
        return unpad(self._cipher.decrypt(tail), AES.block_size)


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
    """Stream-decrypt a PKCS7-padded AES-CBC file via a sibling temp file and replace atomically."""
    target = output_path or path
    tmp_path = f"{target}.dec.tmp"
    decryptor = AESStreamDecryptor(key, iv)
    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
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

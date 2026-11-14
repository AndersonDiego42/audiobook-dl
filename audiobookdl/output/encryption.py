import os
from Crypto.Cipher import AES
from audiobookdl.utils.audiobook import AudiobookFileEncryption, AESEncryption

DECRYPT_CHUNK_SIZE = 64 * 1024  # 64 KiB, an exact multiple of AES's 16-byte block size


def decrypt_file(path: str, encryption_method: AudiobookFileEncryption) -> None:
    """Decrypt encrypted file in place"""
    if isinstance(encryption_method, AESEncryption):
        decrypt_file_aes(path, encryption_method.key, encryption_method.iv)


def decrypt_file_aes(path: str, key: bytes, iv: bytes) -> None:
    """Stream-decrypt an AES-CBC encrypted file via a sibling temp file and replace atomically"""
    tmp_path = f"{path}.dec.tmp"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
            while True:
                chunk = src.read(DECRYPT_CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(cipher.decrypt(chunk))
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

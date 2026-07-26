"""Wire-compatible authenticated encryption, including legacy local CBC records."""

import hashlib
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def resolve_key(env=None):
    env = os.environ if env is None else env
    configured = env.get("ENCRYPTION_KEY", "")
    if re.fullmatch(r"[a-fA-F0-9]{64}", configured):
        return bytes.fromhex(configured)
    build = env.get("NEXT_PHASE") == "phase-production-build"
    if not build and (
        env.get("NODE_ENV") == "production" or env.get("ALPHADECK_DEGRADATION_MODE") == "strict"
    ):
        raise ValueError(
            "ENCRYPTION_KEY must be a stable 64-character hex string in production or strict mode."
        )
    return hashlib.sha256(b"alphadeck-local-development-encryption-key").digest()


def encrypt(value, *, key=None, nonce=None):
    nonce = os.urandom(12) if nonce is None else nonce
    content = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode()
    sealed = AESGCM(key or resolve_key()).encrypt(nonce, content, None)
    return ":".join(["v2", nonce.hex(), sealed[-16:].hex(), sealed[:-16].hex()])


def hex_part(value, label, length=None, *, empty=False):
    if (not value and not empty) or len(value) % 2 or re.search(r"[^a-fA-F0-9]", value):
        raise ValueError("Invalid encrypted payload: " + label)
    result = bytes.fromhex(value)
    if length is not None and len(result) != length:
        raise ValueError("Invalid encrypted payload: " + label)
    return result


def decrypt(value, *, key=None):
    selected = key or resolve_key()
    try:
        if value.startswith("v2:"):
            parts = value.split(":")
            if len(parts) != 4:
                raise ValueError("Invalid encrypted payload: format")
            nonce, tag, ciphertext = (
                hex_part(parts[1], "iv", 12),
                hex_part(parts[2], "auth tag", 16),
                hex_part(parts[3], "ciphertext", empty=True),
            )
            content = AESGCM(selected).decrypt(nonce, ciphertext + tag, None)
        elif re.fullmatch(r"[a-fA-F0-9]{32}:(?:[a-fA-F0-9]{2})+", value):
            iv, ciphertext = value.split(":")
            decryptor = Cipher(algorithms.AES(selected), modes.CBC(bytes.fromhex(iv))).decryptor()
            padded = decryptor.update(bytes.fromhex(ciphertext)) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            content = unpadder.update(padded) + unpadder.finalize()
        else:
            raise ValueError("Invalid encrypted payload: unsupported format")
    except InvalidTag as cause:
        raise ValueError("Encrypted payload authentication failed") from cause
    return content.decode("utf-8", "replace")

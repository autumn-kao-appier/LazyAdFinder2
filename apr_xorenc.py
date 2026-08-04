#!/usr/bin/env python3
"""Decode a string obfuscated with the SDK's AprXorEnc ``ae1`` format."""

import base64
import binascii


VERSION = "ae1"
SECRET_KEY = "6cxqx3vRwA41I8FvZFTjS55xWj5mjvVX2CfV0UP5ywgv0nZ6PoDUeH_it986sZWz"


def decrypt(encrypted: str) -> str:
    """Return the UTF-8 plaintext contained in an AprXorEnc string."""
    if not isinstance(encrypted, str):
        raise TypeError("encrypted must be a string")

    try:
        version, salt_length_text, payload = encrypted.split(":", 2)
    except ValueError as exc:
        raise ValueError("invalid AprXorEnc format") from exc

    if version != VERSION:
        raise ValueError(f"unsupported AprXorEnc version: {version!r}")

    try:
        salt_length = int(salt_length_text)
    except ValueError as exc:
        raise ValueError("invalid AprXorEnc salt length") from exc
    if salt_length <= 0 or salt_length >= len(payload):
        raise ValueError("invalid AprXorEnc salt length")

    salt = payload[-salt_length:]
    encoded_ciphertext = payload[:-salt_length]
    try:
        ciphertext = base64.urlsafe_b64decode(
            encoded_ciphertext + "=" * (-len(encoded_ciphertext) % 4)
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid AprXorEnc ciphertext") from exc

    key = (salt + SECRET_KEY).encode("utf-8")
    plaintext = bytes(
        byte ^ key[index % len(key)] for index, byte in enumerate(ciphertext)
    )
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("AprXorEnc plaintext is not valid UTF-8") from exc


if __name__ == "__main__":
    import sys

    value = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
    if not value:
        sys.exit("usage: apr_xorenc.py '<encrypted-string>' (or pipe it via stdin)")
    print(decrypt(value))

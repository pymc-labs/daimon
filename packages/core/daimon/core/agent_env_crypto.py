"""Agent environment encryption selected by storage metadata, never value text."""

import uuid
from typing import Literal

from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token

Encoding = Literal["plain", "fernet_v1"]


def decode_value(
    cipher: MultiFernet | None,
    value: str,
    *,
    encoding: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> str:
    """Decode only explicitly encrypted rows; arbitrary plaintext stays literal."""
    if encoding == "plain":
        return value
    if encoding != "fernet_v1":
        raise ValueError("Unknown agent environment storage encoding")
    if cipher is None:
        raise ValueError(
            "DAIMON_CRYPTO__KEYS is required to decrypt encrypted agent environment values"
        )
    try:
        return decrypt_token(cipher, value.encode("utf-8"))
    except (InvalidToken, UnicodeDecodeError):
        raise ValueError(
            f"Agent environment value {tenant_id}/{agent_id}/{key} cannot be decrypted "
            "with DAIMON_CRYPTO__KEYS; check the retained keys and stored ciphertext"
        ) from None


def encode_value(cipher: MultiFernet | None, value: str) -> tuple[str, Encoding]:
    """Accept literal user content and return its value and atomic storage tag."""
    if cipher is None:
        return value, "plain"
    return encrypt_token(cipher, value).decode("ascii"), "fernet_v1"

"""Versioned agent environment encryption; keyless deployments retain plaintext."""

from cryptography.fernet import MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token

PREFIX = "enc:v1:"


def decode_value(cipher: MultiFernet | None, value: str) -> str:
    """Unmarked values are legacy plaintext; marked values must decrypt."""
    if not value.startswith(PREFIX):
        return value
    if cipher is None:
        raise ValueError(
            "DAIMON_CRYPTO__KEYS is required to decrypt encrypted agent environment values"
        )
    return decrypt_token(cipher, value[len(PREFIX) :].encode("utf-8"))


def encode_value(cipher: MultiFernet | None, value: str) -> str:
    """Keep authenticated envelopes intact, never silently encrypt them twice."""
    if value.startswith(PREFIX):
        decode_value(cipher, value)
        return value
    if cipher is None:
        return value
    return PREFIX + encrypt_token(cipher, value).decode("ascii")

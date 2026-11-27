"""What the data store's declared `connections` section needs from shared MCP connections."""

from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.shared_connections import (
    NAME_PATTERN,
    SCOPE_PATTERN,
    SharedConnectionError,
    checked_address,
    encrypt_auth,
)

__all__ = [
    "NAME_PATTERN",
    "SCOPE_PATTERN",
    "SharedConnectionError",
    "build_multifernet",
    "checked_address",
    "decrypt_token",
    "encrypt_auth",
]

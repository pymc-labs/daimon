"""A source's credential: a JSON object sealed with the deployment's crypto keys.

`sources.credential` holds it as a Fernet token (the first key seals, every key opens, so a
rotation keeps old rows readable). Nothing but the sync worker and the setup paths opens it;
a reader role is never granted the column.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredentialError(RuntimeError):
    """A credential that cannot be opened; the message never holds its value."""


def _cipher(keys: Sequence[str]) -> MultiFernet:
    if not keys:
        raise CredentialError("DAIMON_CRYPTO__KEYS is required to seal or open source credentials")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def seal(keys: Sequence[str], credentials: dict[str, str]) -> str:
    return _cipher(keys).encrypt(json.dumps(credentials).encode()).decode()


def unseal(keys: Sequence[str], sealed: str | None) -> dict[str, str]:
    """The credential's fields; empty for a source with none."""
    if not sealed:
        return {}
    try:
        value = json.loads(_cipher(keys).decrypt(sealed.encode()))
    except InvalidToken:
        raise CredentialError(
            "a source credential cannot be opened with the retained keys"
        ) from None
    if not isinstance(value, dict):
        raise CredentialError("a source credential is not an object")
    return {str(k): str(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def resolve_ref(ref: str) -> str | None:
    """A secret named by a reference, `env:NAME` or `file:/path`; None when it cannot be read.
    Any other form is a `ValueError`: values never sit in a file the deployment keeps."""
    kind, _, name = ref.partition(":")
    if kind == "env":
        return os.environ.get(name) or None
    if kind == "file":
        try:
            return Path(name).read_text("utf-8").strip() or None
        except OSError:
            return None
    raise ValueError("a secret must be an env: or file: reference")

"""Capability-token verification for notebook-host uploads.

Mirror of ``daimon.core.notebooks.capability`` (the mint side). Duplicated, not
imported: the notebook-host is a standalone app and does not depend on
daimon-core. The two are kept in lockstep by tests on each side. The signature
is verified against the full ``admin_secrets`` rotation list, constant-time and
with no short-circuit — same property as ``admin.py:_bearer_dep``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

Op = Literal["blog", "notebook", "data"]


class CapabilityClaims(BaseModel):
    slug: str
    op: Op
    name: str | None
    max_bytes: int
    exp: int
    jti: str


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(secrets: list[str], token: str, *, now: datetime) -> CapabilityClaims:
    """Verify the HMAC signature (any rotation secret) and expiry. 403 on failure.

    Public-endpoint hardening: any malformed token (bad split, non-base64 part,
    or a payload that isn't valid claims JSON) maps to 403, never an unhandled 500.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        provided_sig = _unb64(sig_b64)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "malformed token") from exc
    # Constant-time, no short-circuit — no timing leak of which rotation secret matched.
    matched = False
    for secret in secrets:
        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(provided_sig, expected):
            matched = True
    if not matched:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad signature")
    try:
        claims = CapabilityClaims.model_validate_json(_unb64(payload_b64))
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "malformed token") from exc
    if datetime.fromtimestamp(claims.exp, tz=UTC) < now:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token expired")
    return claims

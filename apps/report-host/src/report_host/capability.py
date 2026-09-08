"""Capability-token verification for report-host publish uploads.

Mirror of ``daimon.core.notebooks.capability`` (the mint side). Duplicated,
not imported: report-host is a standalone app and does not depend on
daimon-core. The two are kept in lockstep by a test on each side — this
module's tests, and ``packages/core/tests/test_capability_lockstep.py`` on
the core side. The signature is verified against every configured admin
secret, constant-time and with no short-circuit, so verification time does
not leak which secret matched.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, SecretStr, ValidationError

Op = Literal["report"]


class CapabilityClaims(BaseModel, frozen=True, extra="forbid"):
    # extra="forbid": an unexpected key in the signed payload is a schema
    # mismatch between mint and verify, not something to silently drop — it
    # is exactly the drift the lockstep test on the core side exists to catch.
    slug: str
    op: Op
    name: str | None
    max_bytes: int
    exp: int
    jti: str


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(secrets: list[SecretStr], token: str, *, now: datetime) -> CapabilityClaims | None:
    """Verify the HMAC signature (any configured secret), expiry, and op.

    Returns ``None`` on any failure: a malformed token, a bad signature
    against every secret, an ``exp`` in the past, or an ``op`` that is not
    ``"report"``. Never raises on attacker-controlled input.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        provided_sig = _unb64(sig_b64)
    except (ValueError, binascii.Error):
        return None
    # Constant-time, no short-circuit — no timing leak of which secret matched.
    matched = False
    for secret in secrets:
        expected = hmac.new(
            secret.get_secret_value().encode(), payload_b64.encode(), hashlib.sha256
        ).digest()
        if hmac.compare_digest(provided_sig, expected):
            matched = True
    if not matched:
        return None
    try:
        claims = CapabilityClaims.model_validate_json(_unb64(payload_b64))
    except (ValueError, binascii.Error, ValidationError):
        return None
    if datetime.fromtimestamp(claims.exp, tz=UTC) < now:
        return None
    return claims

"""Mint and verify signed bundle handles.

A bundle handle carries the ownership facts a database row would otherwise
hold — which Files-API object, which tenant, which agent, its content hash,
and its expiry — as an HMAC-signed string. Verifying the signature and the
embedded claims is the whole ownership check; no table and no lookup are
needed.

Pure: no I/O, no clock, no RNG. The caller injects ``now``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, ValidationError

__all__ = ["BundleHandleClaims", "mint", "verify"]


class BundleHandleClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: str
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    sha256: str
    exp: int


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(
    secret: str,
    *,
    file_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    sha256: str,
    now: datetime,
    ttl_days: int,
) -> str:
    """Return a ``<payload_b64>.<sig_b64>`` bundle handle."""
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")
    fields = [file_id, str(tenant_id), str(agent_id), sha256]
    for field in fields:
        if "|" in field:
            raise ValueError("bundle handle fields must not contain '|'")
    exp = int((now + timedelta(days=ttl_days)).timestamp())
    payload = "|".join([*fields, str(exp)])
    payload_b64 = _b64(payload.encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def verify(
    secret: str,
    handle: str,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    now: datetime,
) -> BundleHandleClaims | None:
    """Verify a bundle handle. Returns ``None`` on every failure, indistinguishably.

    Malformed shape, bad base64, wrong field count, bad signature, expiry, and
    tenant/agent mismatch all collapse to ``None`` — the caller maps every case
    to one message, never leaking which check failed. A tz-naive ``now`` is a
    programming error and still raises ``ValueError``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")

    parts = handle.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts

    try:
        provided_sig = _unb64(sig_b64)
    except (ValueError, binascii.Error):
        return None

    expected_sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None

    try:
        payload = _unb64(payload_b64).decode()
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None

    fields = payload.split("|")
    if len(fields) != 5:
        return None
    file_id, claim_tenant_id, claim_agent_id, sha256, exp_str = fields

    try:
        claims = BundleHandleClaims(
            file_id=file_id,
            tenant_id=uuid.UUID(claim_tenant_id),
            agent_id=uuid.UUID(claim_agent_id),
            sha256=sha256,
            exp=int(exp_str),
        )
    except (ValueError, ValidationError):
        return None

    if claims.exp <= int(now.timestamp()):
        return None
    if claims.tenant_id != tenant_id:
        return None
    if claims.agent_id != agent_id:
        return None

    return claims

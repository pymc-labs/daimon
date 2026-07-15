"""Capability-token minting for notebook-host uploads.

Daimon-core mints a short-lived HMAC-signed token that authorizes ONE upload to
the notebook host. The host verifies it (notebook_host.capability) and the agent
never sees the admin secret. The destination (slug/op/name) is carried INSIDE the
signed payload, so a tampered URL path cannot redirect bytes to another slug.

Pure: the caller injects ``now`` (clock) and ``jti`` (nonce) — no I/O, no clock,
no RNG here, so the token is deterministic given its inputs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Literal

Op = Literal["blog", "notebook", "data"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint_token(
    secret: str,
    *,
    slug: str,
    op: Op,
    max_bytes: int,
    now: datetime,
    jti: str,
    ttl_seconds: int = 300,
    name: str | None = None,
) -> str:
    """Return a ``<payload_b64>.<sig_b64>`` capability token for one upload."""
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")
    payload: dict[str, object] = {
        "slug": slug,
        "op": op,
        "name": name,
        "max_bytes": max_bytes,
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": jti,
    }
    # Compact/canonical JSON — no whitespace. These exact bytes are what gets
    # signed, so the separators are load-bearing; do not reformat.
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = _b64(payload_json.encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"

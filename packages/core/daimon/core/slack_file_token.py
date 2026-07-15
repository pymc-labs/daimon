"""Signed, expiring tokens for the Slack file proxy.

Minted by the Slack adapter when it surfaces a file to the agent; verified by
the MCP adapter's ``/slack/file/{token}`` route before it fetches bytes from
Slack. Lives in core so both adapters share it without importing each other
(adapter-independence contract).

Token format mirrors ``daimon.core.slack_oauth.mint_state``:
    base64url(payload).base64url(sig)
payload = compact JSON ``{"team_id": str, "file_id": str, "exp": int}``
sig     = HMAC-SHA256(secret, payload_bytes)

Pure functions: ``now``/``exp`` and ``secret`` are injected — no clock or config
reads inside. ``hmac.compare_digest`` avoids timing oracles.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class SlackFileRef:
    """The workspace + file identity carried by a verified proxy token."""

    team_id: str
    file_id: str


def mint_file_token(*, team_id: str, file_id: str, exp: int, secret: str) -> str:
    """Mint a stateless HMAC-signed proxy token for one Slack file."""
    payload_bytes = json.dumps(
        {"team_id": team_id, "file_id": file_id, "exp": exp},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    b64_payload = base64.urlsafe_b64encode(payload_bytes).decode()
    b64_sig = base64.urlsafe_b64encode(sig).decode()
    return f"{b64_payload}.{b64_sig}"


def verify_file_token(token: str, *, secret: str, now: int) -> SlackFileRef | None:
    """Return the ``SlackFileRef`` iff the token verifies and is unexpired.

    Returns ``None`` for every invalid token — malformed structure, non-base64
    content, bad signature, or expiry — because the sole consumer (the proxy
    route) maps all of them to the same 403. This is a modelled "unverifiable"
    absence, not a swallowed error.
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    b64_payload, b64_sig = parts
    try:
        payload_bytes = base64.urlsafe_b64decode(b64_payload)
        sig = base64.urlsafe_b64decode(b64_sig)
    except (binascii.Error, ValueError):
        return None
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload_bytes)
        exp = int(data["exp"])
        team_id = str(data["team_id"])
        file_id = str(data["file_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if now > exp:
        return None
    return SlackFileRef(team_id=team_id, file_id=file_id)

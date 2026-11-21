"""Webhook verification presets, on the raw body before anything parses it.

Pure: the caller passes the trigger's live secrets (the newest first, then any still in
their rotation overlap), the request headers with lowercase names, the raw body and the
clock. Every comparison is constant time; timestamped schemes refuse a delivery more than
`TOLERANCE` off the clock. GitHub and bearer carry no timestamp, so ingress also dedupes
their bodies by hash.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets as random
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, Literal

Scheme = Literal["standard-webhooks", "github", "stripe", "slack", "bearer"]

TOLERANCE: Final = timedelta(minutes=5)
SECRET_PREFIX: Final = "whsec_"
#: Header names a delivery id is read from, by scheme, before the trigger's own field.
DELIVERY_HEADERS: Final[dict[str, str]] = {
    "standard-webhooks": "webhook-id",
    "github": "x-github-delivery",
}
#: Headers never stored with a delivery: they carry the secret or a session.
SECRET_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})


def new_secret() -> str:
    """`whsec_` and 32 random bytes in base64, the Standard Webhooks secret format."""
    return SECRET_PREFIX + base64.b64encode(random.token_bytes(32)).decode("ascii")


def hint_of(secret: str) -> str:
    return f"{SECRET_PREFIX}...{secret[-4:]}"


def _hex(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _fresh(stamp: str | None, now: datetime) -> bool:
    if stamp is None or not stamp.isdigit() or len(stamp) > 12:
        return False
    return abs(now.timestamp() - int(stamp)) <= TOLERANCE.total_seconds()


def _any_equal(given: Sequence[str], expected: Sequence[str]) -> bool:
    """Constant time per pair; every pair is compared, so timing says nothing of which."""
    hit = False
    for g in given:
        for e in expected:
            hit |= hmac.compare_digest(g.encode(), e.encode())
    return hit


def _standard_key(secret: str) -> bytes | None:
    raw = secret.removeprefix(SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None


def _standard(
    secrets: Sequence[str], headers: Mapping[str, str], body: bytes, now: datetime
) -> bool:
    msg_id, stamp = headers.get("webhook-id"), headers.get("webhook-timestamp")
    if not msg_id or not _fresh(stamp, now):
        return False
    given = [
        part.split(",", 1)[1]
        for part in headers.get("webhook-signature", "").split()
        if part.startswith("v1,")
    ]
    signed = f"{msg_id}.{stamp}.".encode() + body
    expected = [
        base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        for key in map(_standard_key, secrets)
        if key
    ]
    return _any_equal(given, expected)


def _github(secrets: Sequence[str], headers: Mapping[str, str], body: bytes) -> bool:
    given = headers.get("x-hub-signature-256", "")
    if not given.startswith("sha256="):
        return False
    return _any_equal([given.removeprefix("sha256=")], [_hex(s.encode(), body) for s in secrets])


def _stripe(secrets: Sequence[str], headers: Mapping[str, str], body: bytes, now: datetime) -> bool:
    pairs = [p.split("=", 1) for p in headers.get("stripe-signature", "").split(",") if "=" in p]
    stamp = next((v for k, v in pairs if k == "t"), None)
    if not _fresh(stamp, now):
        return False
    signed = f"{stamp}.".encode() + body
    return _any_equal(
        [v for k, v in pairs if k == "v1"], [_hex(s.encode(), signed) for s in secrets]
    )


def _slack(secrets: Sequence[str], headers: Mapping[str, str], body: bytes, now: datetime) -> bool:
    stamp, given = headers.get("x-slack-request-timestamp"), headers.get("x-slack-signature", "")
    if not _fresh(stamp, now) or not given.startswith("v0="):
        return False
    signed = f"v0:{stamp}:".encode() + body
    return _any_equal([given.removeprefix("v0=")], [_hex(s.encode(), signed) for s in secrets])


def _bearer(secrets: Sequence[str], headers: Mapping[str, str]) -> bool:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and _any_equal([token.strip()], secrets)


def verify(
    scheme: Scheme | None,
    secrets: Sequence[str],
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: datetime,
) -> bool:
    """Whether the delivery is signed by one of `secrets`. No scheme verifies nothing (an
    admin granted that); a scheme without a secret refuses."""
    if scheme is None:
        return True
    if not secrets:
        return False
    match scheme:
        case "standard-webhooks":
            return _standard(secrets, headers, body, now)
        case "github":
            return _github(secrets, headers, body)
        case "stripe":
            return _stripe(secrets, headers, body, now)
        case "slack":
            return _slack(secrets, headers, body, now)
        case "bearer":
            return _bearer(secrets, headers)

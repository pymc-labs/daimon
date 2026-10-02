"""A staged file offered in a Teams 1:1 chat by the MCP server, accepted in the adapter.

The offer is a FileConsentCard whose context round-trips through the client,
so it carries a token signed with a key derived from the Teams client secret,
which both processes hold: the file handle, the person, the chat and an
expiry. The adapter uploads the handle's bytes only when the clicker is that
person in that chat and the signature and expiry check out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

FILE_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
OFFER_TTL_S = 3600
# The consent context key holding the token; the adapter's own offers use "offer".
UPLOAD_KEY = "upload"
# Separates this HMAC key from every other use of the Teams client secret.
_KEY_LABEL = b"daimon.teams-file-offer.v1"


@dataclass(frozen=True)
class UploadOffer:
    handle_id: str
    user_id: str
    conversation_id: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(body: bytes, secret: str) -> bytes:
    key = hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()
    return hmac.new(key, body, hashlib.sha256).digest()


def sign_offer(offer: UploadOffer, *, secret: str, now: float) -> str:
    body = json.dumps(
        [offer.handle_id, offer.user_id, offer.conversation_id, int(now) + OFFER_TTL_S]
    ).encode()
    return f"{_b64(body)}.{_b64(_mac(body, secret))}"


def verify_offer(token: str, *, secret: str, now: float) -> UploadOffer | None:
    """The signed offer, or None if `token` is forged, expired or malformed."""
    try:
        b64_body, b64_mac = token.split(".")
        body, mac = _unb64(b64_body), _unb64(b64_mac)
        if not hmac.compare_digest(mac, _mac(body, secret)):
            return None
        handle_id, user_id, conversation_id, expires = json.loads(body)
        if now > int(expires):
            return None
        return UploadOffer(str(handle_id), str(user_id), str(conversation_id))
    except (ValueError, TypeError):  # bad base64, JSON, shape or int
        return None


def consent_attachment(name: str, size_bytes: int, token: str) -> dict[str, object]:
    """The FileConsentCard attachment, as Bot Framework JSON."""
    context = {UPLOAD_KEY: token}
    return {
        "contentType": FILE_CONSENT_CONTENT_TYPE,
        "name": name,
        "content": {
            "description": name,
            "sizeInBytes": size_bytes,
            "acceptContext": context,
            "declineContext": context,
        },
    }

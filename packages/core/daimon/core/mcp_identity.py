"""Signed caller identity for remote MCP servers that opt in.

An opted-in server is attached to an agent at Daimon's relay URL
(`<app root>/mcp-identity/<name>`). Each session reaches it with a credential of
its own, from a vault only that session mounts, whose signed `RELAY_CLAIM`
names the session's `thread_sessions` row (none for a routine or any other
session no person's turn is bound to). The relay verifies it, then forwards each
request to the server with a compact EdDSA JWS naming the agent, the workspace
and, when that very session is running a person's turn, who asked and where.
The server can then apply that person's own permissions.

The Ed25519 key lives only in the process serving the relay: the model runtime
and its sandbox see the relay URL and Daimon's own per-turn credential, never
the key or a token signed with it. Everything here is pure except the clock a
caller passes in.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast
from urllib.parse import unquote, urlsplit

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.stores.domain import TurnOriginRow

#: Relay path under the app root; the server's configured name follows it.
RELAY_PATH: Final = "/mcp-identity"
#: Signed chat-credential claim binding a relay credential to one session.
RELAY_CLAIM: Final = "daimon_relay"
#: Servers accept at most 300 s between iat and exp; 120 s leaves room for skew.
TOKEN_TTL: Final = timedelta(seconds=120)
#: A cached token is re-signed once it has less than this left.
RESIGN_MARGIN: Final = timedelta(seconds=30)
_SEED_BYTES: Final = 32

SigningKey = Ed25519PrivateKey


class SigningKeyError(ValueError):
    """The configured key is not an Ed25519 PEM or a 32-byte base64url seed."""


def load_signing_key(value: str) -> Ed25519PrivateKey:
    """Read an unencrypted Ed25519 PKCS#8 PEM or a raw base64url seed.

    Error messages never quote the value: it is a secret.
    """
    text = value.strip()
    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        except (ValueError, TypeError) as err:
            raise SigningKeyError("signing key PEM could not be read") from err
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError("signing key PEM is not an Ed25519 key")
        return key
    try:
        seed = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as err:
        raise SigningKeyError("signing key is neither a PEM nor a base64url seed") from err
    if len(seed) != _SEED_BYTES:
        raise SigningKeyError("signing key seed must decode to 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def encode_seed(key: Ed25519PrivateKey) -> str:
    """The key as the base64url seed `load_signing_key` reads back."""
    return _b64url(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def public_jwk(key: Ed25519PrivateKey) -> dict[str, str]:
    """The public half as an RFC 8037 JWK, for registering with a server."""
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}


def public_pem(key: Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def relay_base(public_url: str) -> str:
    """`<app root>/mcp-identity/` for the MCP endpoint URL `public_url` (`…/mcp`)."""
    root = public_url.rstrip("/").removesuffix("/mcp").rstrip("/")
    return canonical_mcp_url(root + RELAY_PATH) + "/"


def relay_url(public_url: str, name: str) -> str:
    return relay_base(public_url) + name


def relay_route_name(url: str, *, public_url: str) -> str | None:
    """Resolve the relay route reached by a server URL, ignoring query and path encoding."""
    parts = urlsplit(canonical_mcp_url(url))
    base = urlsplit(relay_base(public_url))
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
        return None
    prefix = base.path.rstrip("/") + "/"
    path = unquote(parts.path).rstrip("/")
    if not path.startswith(prefix):
        return None
    name = path[len(prefix) :]
    return name if name and "/" not in name else None


def identity_relay_urls(server_urls: Iterable[str], *, public_url: str) -> tuple[str, ...]:
    """The attached URLs that are this deployment's relay, which need its credential.

    Kept exactly as attached: the managed runtime matches a credential to a server
    by its URL string, so a canonical form could miss. One per canonical URL, as a
    vault holds one credential per server.
    """
    base = relay_base(public_url)
    by_server: dict[str, str] = {}
    for url in sorted(server_urls):
        if (canonical := canonical_mcp_url(url)).startswith(base):
            by_server.setdefault(canonical, url)
    return tuple(sorted(by_server.values()))


@dataclass(frozen=True)
class RelayBinding:
    """What a session's relay credential signs: the agent's name and, for a
    session that serves a person's turns, its `thread_sessions` row id."""

    agent_name: str
    session_key: uuid.UUID | None = None
    isolated: bool = False

    def claim(self) -> dict[str, str | bool]:
        value: dict[str, str | bool] = {"agent_name": self.agent_name}
        if self.session_key is not None:
            value["session"] = str(self.session_key)
        if self.isolated:
            value["isolated"] = True
        return value


def parse_relay_claim(raw: object) -> RelayBinding:
    """The binding a `RELAY_CLAIM` value names. Raises `ValueError` for any other shape."""
    if not isinstance(raw, Mapping):
        raise ValueError("relay claim is not an object")
    claim = cast("Mapping[str, object]", raw)
    name, key, isolated = claim.get("agent_name"), claim.get("session"), claim.get("isolated")
    if (
        not set(claim) <= {"agent_name", "session", "isolated"}
        or not isinstance(name, str)
        or not name
        or not (key is None or isinstance(key, str))
        or isolated is not None
        and isolated is not True
    ):
        raise ValueError("relay claim has the wrong shape")
    return RelayBinding(
        agent_name=name,
        session_key=None if key is None else uuid.UUID(key),
        isolated=isolated is True,
    )


@dataclass(frozen=True)
class TurnPlace:
    """Where a person's turn came from, as the platform's own API names it.

    `channel_id` is the channel, thread or DM that `message_id` can be read in.
    """

    channel_id: str | None
    message_id: str | None


@dataclass(frozen=True)
class IdentityClaims:
    """Every claim but `iat` and `exp`; a value of None is left out of the token."""

    iss: str
    aud: str
    agent: str
    agent_name: str | None = None
    sub: str | None = None
    ws: str | None = None
    channel: str | None = None
    msg: str | None = None

    def payload(self, *, now: datetime, ttl: timedelta = TOKEN_TTL) -> dict[str, str | int]:
        issued = int(now.timestamp())
        claims: dict[str, str | int] = {
            "iss": self.iss,
            "aud": self.aud,
            "iat": issued,
            "exp": issued + int(ttl.total_seconds()),
            "agent": self.agent,
        }
        for name, value in (
            ("agent_name", self.agent_name),
            ("sub", self.sub),
            ("ws", self.ws),
            ("channel", self.channel),
            ("msg", self.msg),
        ):
            if value:
                claims[name] = value
        return claims


def caller_claims(
    *,
    issuer: str,
    audience: str,
    agent_id: str,
    platform: str,
    workspace_id: str | None,
    platform_user_id: str | None,
    agent_name: str,
    place: TurnPlace | None,
) -> IdentityClaims:
    """Claims for one relayed request.

    `place` is the turn the calling session itself is running, or None: a
    routine or other headless session, a session no longer the thread's live
    one, two turns at once in that session, or a sender from another
    organisation. The token then names no person and no message rather than
    guess; it still names the agent.
    """
    person = place is not None and bool(platform_user_id)
    return IdentityClaims(
        iss=issuer,
        aud=audience,
        agent=agent_id,
        agent_name=agent_name,
        sub=f"{platform}:{platform_user_id}" if person else None,
        ws=f"{platform}:{workspace_id}" if workspace_id else None,
        channel=place.channel_id if place is not None else None,
        msg=place.message_id if person and place is not None else None,
    )


def sign_identity(
    claims: IdentityClaims,
    *,
    key: Ed25519PrivateKey,
    now: datetime,
    ttl: timedelta = TOKEN_TTL,
) -> str:
    """Compact JWS, header `{"alg":"EdDSA","typ":"JWT"}`."""
    return pyjwt.encode(
        claims.payload(now=now, ttl=ttl), key, algorithm="EdDSA", headers={"typ": "JWT"}
    )


class IdentityTokenCache:
    """Reuse a token for the same claims until it is within `margin` of expiry.

    The claims carry the turn's message, so in practice one token serves one
    turn. Bounded: the oldest entry goes first.
    """

    def __init__(
        self,
        key: Ed25519PrivateKey,
        *,
        ttl: timedelta = TOKEN_TTL,
        margin: timedelta = RESIGN_MARGIN,
        max_entries: int = 1024,
    ) -> None:
        if not timedelta(0) <= margin < ttl:
            raise ValueError("re-sign margin must be shorter than the token lifetime")
        self._key = key
        self._ttl = ttl
        self._margin = margin
        self._max_entries = max_entries
        self._tokens: OrderedDict[IdentityClaims, tuple[str, datetime]] = OrderedDict()

    def token(self, claims: IdentityClaims, *, now: datetime) -> str:
        cached = self._tokens.get(claims)
        if cached is not None and cached[1] - now > self._margin:
            self._tokens.move_to_end(claims)
            return cached[0]
        token = sign_identity(claims, key=self._key, now=now, ttl=self._ttl)
        expires_at = datetime.fromtimestamp(int(now.timestamp()), tz=now.tzinfo) + self._ttl
        self._tokens[claims] = (token, expires_at)
        self._tokens.move_to_end(claims)
        while len(self._tokens) > self._max_entries:
            self._tokens.popitem(last=False)
        return token


def matching_turn(
    origins: Iterable[TurnOriginRow], *, tenant_id: uuid.UUID, agent_id: uuid.UUID, thread_id: str
) -> TurnOriginRow | None:
    """The one running turn of this caller with this agent in the calling session's
    thread, or None.

    Two such turns at once (a message queued behind another) cannot be told
    apart, so neither is named.
    """
    found = [
        origin
        for origin in origins
        if origin.thread_id == thread_id
        and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=origin.responder_ma_agent_id)
        == agent_id
    ]
    return found[0] if len(found) == 1 else None


def origin_turn_place(origin: TurnOriginRow) -> TurnPlace:
    return TurnPlace(channel_id=origin.platform_channel_id, message_id=origin.platform_message_id)

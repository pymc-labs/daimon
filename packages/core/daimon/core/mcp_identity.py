"""Signed caller identity for remote MCP servers that opt in.

An opted-in server is attached to an agent at Daimon's relay URL
(`<app root>/mcp-identity/<name>`). Each session reaches it with a credential of
its own, from a vault only that session mounts, whose signed `RELAY_CLAIM`
names the session's `thread_sessions` row (none for a routine or any other
session no person's turn is bound to); a watch's poll mints one naming its watch.
The relay verifies it, then forwards each request to the server with a compact
EdDSA JWS naming the agent, the workspace and, when that very session is running
a person's turn, who asked and where.
The server can then apply that person's own permissions.

The Ed25519 key lives only in the process serving the relay: the model runtime
and its sandbox see the relay URL and Daimon's own per-turn credential, never
the key or a token signed with it. Everything here is pure except the clock a
caller passes in.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, cast, get_args

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import ascii_mcp_url, canonical_mcp_url, same_ascii_mcp_url
from daimon.core.stores.domain import ContinuationReason, TurnOriginRow
from daimon.core.turn_origin import origin_place

if TYPE_CHECKING:
    from daimon.core.signed_room import SignedRoom
    from daimon.core.stores.domain import RoutineRow, WatchRow

#: Relay path under the app root; the server's configured name follows it.
RELAY_PATH: Final = "/mcp-identity"
#: Signed chat-credential claim binding a relay credential to one session.
RELAY_CLAIM: Final = "daimon_relay"
#: The longest `routine` claim a server accepts.
ROUTINE_CLAIM_MAX: Final = 80
#: Why a call runs without a fresh message, as the `wake` claim says: `turn`, a person's
#: turn still running, or the reason a queued continuation woke (`ContinuationReason`).
WakeReason = Literal["turn"] | ContinuationReason
WAKE_REASONS: Final[frozenset[str]] = frozenset({"turn", *get_args(ContinuationReason)})
#: Servers accept at most 300 s between iat and exp; 120 s leaves room for skew.
TOKEN_TTL: Final = timedelta(seconds=120)
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


_RELAY_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def relay_server_name(url: str, public_url: str) -> str | None:
    """The server name when `url` is exactly `relay_url(public_url, name)` (this host, one
    path segment, no dot segments or query), else None."""
    name = ascii_mcp_url(url).rpartition("/")[2]
    if _RELAY_NAME.fullmatch(name) and same_ascii_mcp_url(url, relay_url(public_url, name)):
        return name
    return None


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
    #: The routine a scheduled run belongs to; with `session_key` only for a routine
    #: posting to its owner's DM, with every `dm_channel` field below.
    routine_id: uuid.UUID | None = None
    #: The routine's or watch's owner at minting; the relay stamps the run only
    #: while the row still names this user, so a later ownership change credits nobody.
    owner: str | None = None
    #: The watch a poll reads for; at most one of `session_key`, `routine_id` and this.
    watch_id: uuid.UUID | None = None
    #: The session's execution grant (`chat_session_id`), only with `session_key`. The
    #: turn holding it (`bind_origin_session`) is the one making the call, so the relay
    #: names that turn even beside a queued one or after the session row was replaced.
    grant: str | None = None
    #: The agent-chat session (its Managed Agents id) this credential is mounted in; set
    #: alone. The relay names a person only while that session's driver is one signed in
    #: to the hub (`daimon.core.agent_chat_requester`).
    agent_chat: str | None = None
    #: Where a routine's run posts, as `routine_destination_key` named it when the run
    #: started; only with `routine_id`. The relay signs routine claims only while the
    #: routine still posts there.
    destination: str | None = None
    #: The fire a routine's run belongs to (`routine_execution`). A channel or thread
    #: routine's run (`routine_id`, `owner`, `destination`, no `session_key`) carries it
    #: alone, and the relay signs routine claims only while that fire is live
    #: (`identity_relay._routine_run`). A routine posting to its owner's DM, in the
    #: owner's DM turn (`session_key`, `grant`, `routine_id`, `owner`, destination
    #: `dm::`), carries it with the routine's `updated_at` when the fire began, as ISO
    #: 8601, and the owner's DM channel: all three or none; the relay signs only while
    #: that fire is live and the routine enabled and unedited (`_dm_routine_run`).
    execution: uuid.UUID | None = None
    revision: str | None = None
    dm_channel: str | None = None

    def claim(self) -> dict[str, str]:
        value = {"agent_name": self.agent_name}
        if self.session_key is not None:
            value["session"] = str(self.session_key)
        if self.grant is not None:
            value["grant"] = self.grant
        if self.routine_id is not None:
            value["routine"] = str(self.routine_id)
        if self.watch_id is not None:
            value["watch"] = str(self.watch_id)
        if self.owner is not None:
            value["owner"] = self.owner
        if self.agent_chat is not None:
            value["agent_chat"] = self.agent_chat
        if self.destination is not None:
            value["destination"] = self.destination
        if self.execution is not None:
            value["execution"] = str(self.execution)
        if self.revision is not None:
            value["revision"] = self.revision
        if self.dm_channel is not None:
            value["dm_channel"] = self.dm_channel
        return value


@dataclass(frozen=True)
class DmRoutineRelay:
    """What a fire of a routine posting to its owner's DM binds into its turn's relay
    credential (`RelayBinding`): the routine, its owner, the fire's execution, the
    routine's `updated_at` when the fire began (ISO 8601) and, once opened, the DM."""

    routine_id: uuid.UUID
    owner: str
    execution: uuid.UUID
    revision: str
    channel: str | None = None

    def binding(self, agent_name: str, *, session_key: uuid.UUID, grant: str) -> RelayBinding:
        assert self.channel is not None
        return RelayBinding(
            agent_name=agent_name,
            session_key=session_key,
            grant=grant,
            routine_id=self.routine_id,
            owner=self.owner,
            destination=DM_ROUTINE_DESTINATION,
            execution=self.execution,
            revision=self.revision,
            dm_channel=self.channel,
        )


def parse_relay_claim(raw: object) -> RelayBinding:
    """The binding a `RELAY_CLAIM` value names. Raises `ValueError` for any other shape."""
    if not isinstance(raw, Mapping):
        raise ValueError("relay claim is not an object")
    claim = cast("Mapping[str, object]", raw)
    name, key, routine = claim.get("agent_name"), claim.get("session"), claim.get("routine")
    owner, watch, grant = claim.get("owner"), claim.get("watch"), claim.get("grant")
    agent_chat, destination = claim.get("agent_chat"), claim.get("destination")
    execution, revision, dm_channel = (
        claim.get("execution"),
        claim.get("revision"),
        claim.get("dm_channel"),
    )
    unattended = routine is not None or watch is not None
    # A routine in its owner's DM turn: the session, its grant and the routine together,
    # with every field the relay checks it by; any of them alone is a wrong shape.
    dm = (
        revision is not None
        or dm_channel is not None
        or (execution is not None and key is not None)
    )
    # A channel or thread routine's run: its fire alone beside the routine and its room.
    fire = execution is not None and not dm
    if (
        not set(claim)
        <= {
            "agent_name",
            "session",
            "routine",
            "watch",
            "owner",
            "grant",
            "agent_chat",
            "destination",
            "execution",
            "revision",
            "dm_channel",
        }
        or (dm and not _dm_routine_shape(claim))
        or (fire and not _routine_fire_shape(claim))
        or not (destination is None or (isinstance(destination, str) and routine is not None))
        or not (owner is None or (isinstance(owner, str) and owner and unattended))
        or not (grant is None or (isinstance(grant, str) and grant and key is not None))
        or not isinstance(name, str)
        or not name
        or not (key is None or isinstance(key, str))
        or not (routine is None or isinstance(routine, str))
        or not (watch is None or isinstance(watch, str))
        or (sum(value is not None for value in (key, routine, watch)) > 1 and not dm)
        or not (
            agent_chat is None
            or (
                isinstance(agent_chat, str)
                and agent_chat
                and set(claim) == {"agent_name", "agent_chat"}
            )
        )
    ):
        raise ValueError("relay claim has the wrong shape")
    return RelayBinding(
        agent_name=name,
        session_key=None if key is None else uuid.UUID(key),
        routine_id=None if routine is None else uuid.UUID(routine),
        owner=owner,
        watch_id=None if watch is None else uuid.UUID(watch),
        grant=grant,
        agent_chat=agent_chat,
        destination=destination,
        execution=None if execution is None else uuid.UUID(cast("str", execution)),
        revision=cast("str | None", revision),
        dm_channel=cast("str | None", dm_channel),
    )


def _dm_routine_shape(claim: Mapping[str, object]) -> bool:
    """Whether a relay claim is whole as a DM routine's: session, grant, routine, owner,
    the unopened-DM destination, execution, a timezone-aware revision and a channel id."""
    values = [claim.get(k) for k in ("session", "grant", "routine", "owner", "execution")]
    revision, channel = claim.get("revision"), claim.get("dm_channel")
    if not all(isinstance(v, str) and v for v in values) or "watch" in claim:
        return False
    if claim.get("destination") != DM_ROUTINE_DESTINATION or "agent_chat" in claim:
        return False
    if not (isinstance(channel, str) and channel.isascii() and channel.isdecimal()):
        return False
    try:
        uuid.UUID(cast("str", claim["execution"]))
        at = datetime.fromisoformat(cast("str", revision))
    except (TypeError, ValueError):
        return False
    return at.tzinfo is not None


def _routine_fire_shape(claim: Mapping[str, object]) -> bool:
    """Whether a relay claim is whole as a channel or thread routine's run bound to its
    fire: the routine, its owner, its destination and the fire's execution, no session."""
    if not all(isinstance(claim.get(k), str) and claim.get(k) for k in ("routine", "owner")):
        return False
    if not isinstance(claim.get("destination"), str) or {"session", "grant", "watch"} & set(claim):
        return False
    try:
        uuid.UUID(cast("str", claim["execution"]))
    except (TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class TurnPlace:
    """Where a person's turn came from, as the platform's own API names it.

    `channel_id` is the channel, thread or DM that `message_id` can be read in.
    """

    channel_id: str | None
    message_id: str | None


@dataclass(frozen=True)
class IdentityClaims:
    """Every claim but `iat`, `exp` and `jti`; a value of None is left out of the token."""

    iss: str
    aud: str
    agent: str
    agent_name: str | None = None
    sub: str | None = None
    ws: str | None = None
    channel: str | None = None
    msg: str | None = None
    #: What kind of place the answer goes to (`daimon.core.signed_room`); only with `channel`.
    room: str | None = None
    #: The routine a run posts for, with its `channel` and `room` and never a `sub` or `msg`.
    routine: str | None = None
    #: Why the call runs (`WakeReason`): `turn` with `sub` and `msg`, any other reason with
    #: `channel` and `room` and no `sub` or `msg`. Never with `routine`.
    wake: str | None = None
    #: With `routine` only: what a server's approval of the routine is bound to
    #: (`routine_revision`), and its name, for the server's own pages.
    rev: str | None = None
    routine_name: str | None = None
    #: With a channel or thread `routine` only: its owner, `<platform>:<user id>`. Not
    #: `sub`: it names who receives what the run makes for its owner, and reads nothing.
    owner: str | None = None

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
            ("room", self.room),
            ("routine", self.routine),
            ("wake", self.wake),
            ("rev", self.rev),
            ("routine_name", self.routine_name),
            ("owner", self.owner),
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
    room: SignedRoom | None = None,
    routine: str | None = None,
    wake: WakeReason | None = None,
    rev: str | None = None,
    routine_name: str | None = None,
    routine_owner: str | None = None,
) -> IdentityClaims:
    """Claims for one relayed request.

    `place` is the turn the calling session itself is running, or None: a
    routine or other headless session, a session no longer the thread's live
    one, two turns at once in that session, or a sender from another
    organisation. The token then names no person and no message rather than
    guess; it still names the agent. `room` is the kind of place the turn's answer
    goes to (`daimon.core.signed_room.turn_room`), sent only with the place's channel.

    `routine` names the routine a run posts for; `place` is then the room it posts to
    (`daimon.core.signed_room.routine_room`) and `room` its kind. Such a token names no
    person and no message, and is never signed without the room and its kind. A routine
    posting to its owner's DM (room `dm`) names the owner as the person, with the DM
    and no message.

    `wake` says why a call with no fresh message runs: `turn` is the person's live turn
    and keeps its `sub` and `msg`; any other reason is a continuation posting to `place`,
    signed like a routine with that room's kind and no person or message.

    `rev` and `routine_name` come only with `routine` (`routine_revision`).
    `routine_owner`, a channel or thread routine's owner, is signed as `owner`: who
    receives a file the run makes for its owner. It never makes the owner the person
    asking; a DM routine's owner is already `sub`.
    """
    owned = routine is not None and room == "dm"
    if routine is None and (rev is not None or routine_name is not None):
        raise ValueError("a routine's revision and name come only with its routine")
    if routine_owner is not None and (routine is None or owned or not routine_owner):
        raise ValueError("an owner claim comes only with a channel or thread routine")
    if rev is not None and not re.fullmatch(r"[0-9a-f]{64}", rev):
        raise ValueError("a routine's revision is a SHA-256 hex digest")
    if routine is not None and not (
        0 < len(routine) <= ROUTINE_CLAIM_MAX
        and place is not None
        and place.channel_id
        and room is not None
        and (room != "dm" or (not place.message_id and bool(platform_user_id)))
    ):
        raise ValueError("a routine claim needs the room the routine posts to, and its kind")
    if wake is not None:
        if wake not in WAKE_REASONS or routine is not None:
            raise ValueError("a wake claim is one known reason and never with a routine")
        if wake == "turn" and not (place is not None and place.message_id and platform_user_id):
            raise ValueError("a turn wake needs the person and their message")
        if wake != "turn" and not (
            place is not None
            and place.channel_id
            and not place.message_id
            and room is not None
            and room != "dm"
        ):
            raise ValueError("a continuation wake needs its room and its kind, and no message")
    person = owned or (
        routine is None and wake in (None, "turn") and place is not None and bool(platform_user_id)
    )
    channel = place.channel_id if place is not None else None
    return IdentityClaims(
        iss=issuer,
        aud=audience,
        agent=agent_id,
        agent_name=agent_name,
        sub=f"{platform}:{platform_user_id}" if person else None,
        ws=f"{platform}:{workspace_id}" if workspace_id else None,
        channel=channel,
        msg=place.message_id if person and place is not None else None,
        room=room if channel else None,
        routine=routine,
        wake=wake,
        rev=rev,
        routine_name=routine_name[:ROUTINE_CLAIM_MAX] if routine_name else None,
        owner=f"{platform}:{routine_owner}" if routine_owner else None,
    )


#: The layout `routine_revision` hashes; a new layout takes a new number.
ROUTINE_REVISION_VERSION: Final = 1


def routine_revision(row: RoutineRow, *, workspace: str | None) -> str:
    """What a server's approval of a routine is bound to, signed as `rev`.

    The SHA-256 hex of a fixed, versioned text: the tenant, the workspace as signed in
    `ws`, the routine's id and owner, and what an admin approves (`stores.routines.
    _SANDBOX_MATERIAL`) with the agent's name in place of its id, so healing a stale
    agent id keeps it. A pause, a resume, a new schedule or name keep it too; a new
    prompt, agent, owner or destination change it.
    """
    text = json.dumps(
        {
            "v": ROUTINE_REVISION_VERSION,
            "tenant": str(row.tenant_id),
            "ws": workspace or "",
            "routine": str(row.id),
            "owner": row.created_by_user_id or "",
            "agent": row.agent_name,
            "prompt": row.trigger_message,
            "destination_kind": row.destination_kind or "",
            "destination_id": row.destination_id or "",
            "channel_id": row.channel_id or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode()).hexdigest()


def sign_identity(
    claims: IdentityClaims,
    *,
    key: Ed25519PrivateKey,
    now: datetime,
    ttl: timedelta = TOKEN_TTL,
    jti: str | None = None,
) -> str:
    """Compact JWS, header `{"alg":"EdDSA","typ":"JWT"}`, with a fresh `jti` unless one is given.

    A server may accept each `jti` once, so every relayed request gets its own token: a
    replayed one is refused there. That stops a replay, not a stale claim."""
    payload: dict[str, str | int] = {
        **claims.payload(now=now, ttl=ttl),
        "jti": jti or uuid.uuid4().hex,
    }
    return pyjwt.encode(payload, key, algorithm="EdDSA", headers={"typ": "JWT"})


class IdentitySigner:
    """Signs one token per relayed request (`sign_identity`); nothing is cached or reused."""

    def __init__(self, key: Ed25519PrivateKey, *, ttl: timedelta = TOKEN_TTL) -> None:
        self._key = key
        self._ttl = ttl

    def token(self, claims: IdentityClaims, *, now: datetime) -> str:
        return sign_identity(claims, key=self._key, now=now, ttl=self._ttl)


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
        if origin.session_thread_id == thread_id
        and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=origin.responder_ma_agent_id)
        == agent_id
    ]
    return found[0] if len(found) == 1 else None


def origin_turn_place(origin: TurnOriginRow) -> TurnPlace:
    return TurnPlace(channel_id=origin.platform_channel_id, message_id=origin.platform_message_id)


#: `params._meta` keys a requester-aware server reads. Only the relay writes them.
REQUESTER_META_KEY: Final = "requester_platform_user_id"
CONVERSATION_META_KEY: Final = "requester_conversation"
KIND_META_KEY: Final = "requester_kind"
#: A one-time callback watch link for a `wake_tools` call; only the relay writes it.
WAKE_META_KEY: Final = "wake_url"
#: Who a stamped call is for: a person's live turn, a routine run on its owner's behalf,
#: or an agent-chat turn a person signed in to the hub started (it has no place).
RequesterKind = Literal["person", "routine", "agent_chat"]
#: Platforms whose user and channel ids the requester fields may carry.
REQUESTER_PLATFORMS: Final = frozenset({"discord"})


class RequesterBodyError(ValueError):
    """A relayed request body that cannot be stamped safely; the relay answers 400."""


#: A DM whose channel is not opened yet: an unattended run can DM only its owner.
UNOPENED_DM_PLACE: Final[Mapping[str, str | None]] = {
    "place_type": "dm",
    "channel_id": None,
    "parent_channel_id": None,
}


def requester_conversation(
    origin: TurnOriginRow, *, unattended: bool = False
) -> dict[str, str | None] | None:
    """Where the person's live turn runs, as `requester_conversation`; None when unknown.

    `channel_id` names the channel, thread or DM the conversation lives in;
    `parent_channel_id` names a thread's channel and is None otherwise. Read from
    the origin the platform adapter recorded, never from the caller's request. An
    `unattended` run in a DM with no recorded channel is still a DM (its owner's).
    """
    if origin_place(origin) == "dm":
        if not origin.platform_channel_id:
            return dict(UNOPENED_DM_PLACE) if unattended else None
        return {
            "place_type": "dm",
            "channel_id": origin.platform_channel_id,
            "parent_channel_id": None,
        }
    if not origin.thread_id or not origin.parent_channel_id:
        return None
    if origin.thread_id != origin.parent_channel_id:
        return {
            "place_type": "thread",
            "channel_id": origin.thread_id,
            "parent_channel_id": origin.parent_channel_id,
        }
    return {"place_type": "channel", "channel_id": origin.thread_id, "parent_channel_id": None}


def routine_conversation(row: RoutineRow) -> dict[str, str | None] | None:
    """Where a routine delivers, as `requester_conversation`; None when it has no
    destination or the destination's place is not recorded.

    A DM destination has no channel id until the DM is opened at delivery, so it is
    a DM with a null channel. A thread saved before its parent channel was recorded
    has no parent and cannot be named: None.
    """
    if row.destination_kind == "dm":
        # A routine can DM only its owner; the DM channel is opened at delivery.
        return dict(UNOPENED_DM_PLACE)
    if row.destination_id is None:
        return None
    if row.destination_kind == "channel":
        return {
            "place_type": "channel",
            "channel_id": row.destination_id,
            "parent_channel_id": None,
        }
    if row.destination_kind == "thread" and row.channel_id and row.channel_id != row.destination_id:
        return {
            "place_type": "thread",
            "channel_id": row.destination_id,
            "parent_channel_id": row.channel_id,
        }
    return None


#: `routine_destination_key` of every routine posting to its owner's DM (not yet opened).
DM_ROUTINE_DESTINATION: Final = "dm::"


def routine_destination_key(row: RoutineRow) -> str:
    """Where a routine posts, as one string a run's credential keeps (`RelayBinding`):
    `routine_conversation`'s place type, channel and parent, or `none`."""
    conversation = routine_conversation(row)
    if conversation is None:
        return "none"
    return ":".join(
        conversation.get(key) or "" for key in ("place_type", "channel_id", "parent_channel_id")
    )


def watch_conversation(row: WatchRow) -> dict[str, str | None]:
    """Where a watch reports, as `requester_conversation`: its thread, the channel a
    new thread per report opens in, or the owner's DM (opened at delivery)."""
    if row.delivery == "dm":
        return dict(UNOPENED_DM_PLACE)
    if row.delivery == "new_thread" or row.thread_id == row.parent_channel_id:
        return {
            "place_type": "channel",
            "channel_id": row.parent_channel_id,
            "parent_channel_id": None,
        }
    return {
        "place_type": "thread",
        "channel_id": row.thread_id,
        "parent_channel_id": row.parent_channel_id,
    }


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A duplicated key is read first by some parsers and last by others."""
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise RequesterBodyError("duplicate key in request body")
        found[key] = value
    return found


def _refuse_constant(name: str) -> object:
    raise RequesterBodyError(f"{name} is not valid JSON")


def _stamp_message(
    message: object,
    *,
    person: str | None,
    conversation: Mapping[str, str | None] | None,
    kind: RequesterKind | None,
    wake_url: str | None,
) -> dict[str, object]:
    if not isinstance(message, dict):
        raise RequesterBodyError("a request body must be an object or an array of objects")
    fields = cast("dict[str, object]", message)
    method = fields.get("method")
    params = fields.get("params")
    is_call = isinstance(method, str) and method.strip().casefold() == "tools/call"
    if not is_call and not isinstance(params, dict):
        return fields
    if is_call and not isinstance(params, dict):
        raise RequesterBodyError("a tool call needs params")
    call = cast("dict[str, object]", params)
    # `meta` is the field's name without its alias: drop it so no parser reads it.
    call.pop("meta", None)
    existing = call.get("_meta")
    meta: dict[str, object] = (
        {k: v for k, v in cast("dict[str, object]", existing).items()}
        if isinstance(existing, dict)
        else {}
    )
    meta.pop(REQUESTER_META_KEY, None)
    meta.pop(CONVERSATION_META_KEY, None)
    meta.pop(KIND_META_KEY, None)
    meta.pop(WAKE_META_KEY, None)
    # Only a tool call is stamped; any other method just loses what a caller put there.
    # A live turn is named only with its place; a routine's place may be unrecorded; an
    # agent-chat turn never has one, so none is sent that could pass it off as a DM.
    if (
        is_call
        and person is not None
        and (kind in ("routine", "agent_chat") or (kind == "person" and conversation))
    ):
        meta[REQUESTER_META_KEY] = person
        meta[KIND_META_KEY] = kind
        if conversation and kind != "agent_chat":
            meta[CONVERSATION_META_KEY] = dict(conversation)
    if is_call and wake_url is not None:
        meta[WAKE_META_KEY] = wake_url
    if is_call or existing is not None:
        call["_meta"] = meta
    return fields


def read_request_body(body: bytes) -> object:
    """A relayed body parsed strictly, once, for `called_tool` and `stamp_parsed`: one
    that is not UTF-8 JSON, repeats a key or is an empty batch raises `RequesterBodyError`."""
    try:
        parsed: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,  # pyright: ignore[reportArgumentType]
            parse_constant=_refuse_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as err:
        raise RequesterBodyError("request body is not valid JSON") from err
    if parsed == []:
        raise RequesterBodyError("a batch must not be empty")
    return parsed


def stamp_requester(
    body: bytes,
    *,
    person: str | None,
    conversation: Mapping[str, str | None] | None,
    kind: RequesterKind | None = "person",
) -> bytes:
    """`stamp_parsed` of the body `read_request_body` parses."""
    return stamp_parsed(
        read_request_body(body), person=person, conversation=conversation, kind=kind
    )


def stamp_parsed(
    parsed: object,
    *,
    person: str | None,
    conversation: Mapping[str, str | None] | None,
    kind: RequesterKind | None = "person",
    wake_url: str | None = None,
) -> bytes:
    """The request body with each `tools/call` stamped with who asked, where and for what.

    Whatever the caller put under `params._meta` for any requester key is
    removed first, so a value present was set here. `kind` is `person` for a live
    turn and `routine` for a routine run, whose `person` is the routine's owner and
    whose `conversation` is its delivery place (left out when it has none). `agent_chat`
    names the hub-signed-in person driving an agent-chat session, never with a place. With no
    `person` or `kind` (a foreign sender, two live turns) the keys are only removed.
    Requester keys are removed under `params._meta` of every method, not only tool calls.
    `wake_url` is set on each tool call when given; a caller's own one is always removed.
    A tool call without object params raises `RequesterBodyError`. Other methods and
    responses pass through, but the whole body is re-serialized so what was checked is
    what is sent. Modifies `parsed`.
    """
    if isinstance(parsed, list):
        out: object = [
            _stamp_message(
                item, person=person, conversation=conversation, kind=kind, wake_url=wake_url
            )
            for item in cast("list[object]", parsed)
        ]
    else:
        out = _stamp_message(
            parsed, person=person, conversation=conversation, kind=kind, wake_url=wake_url
        )
    try:
        return json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, RecursionError, ValueError) as err:
        raise RequesterBodyError("request body cannot be re-encoded") from err


def called_tool(parsed: object) -> str | None:
    """The tool a parsed body that is one `tools/call` names; None for a batch or another
    method."""
    if not isinstance(parsed, dict):
        return None
    fields = cast("dict[str, object]", parsed)
    method, params = fields.get("method"), fields.get("params")
    if not (
        isinstance(method, str)
        and method.strip().casefold() == "tools/call"
        and isinstance(params, dict)
    ):
        return None
    name = cast("dict[str, object]", params).get("name")
    return name if isinstance(name, str) else None

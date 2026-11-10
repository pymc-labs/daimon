"""Gated tool calls on a turn with no confirmation card of its own (OpenAI, Gemini).

The MCP gate records the call (`stores.host_call_cards`) and Daimon posts an Approve
card only its requester can press. A press never runs the call: an approve queues a
`confirmed_call` wake, and that wake turn runs every approved call for its thread and
agent before its first message, inside its own lease and with its own credential, so
the call runs under the current hold, seal and sharing, never the asking turn's.
Delivery is by wake only: no polling, no LISTEN/NOTIFY, no sleeping in here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

import structlog
from daimon.core.untrusted import render_untrusted

if TYPE_CHECKING:
    from daimon.core.posted_controls import PostedCard
    from daimon.core.stores.domain import TurnOriginRow
    from daimon.core.stores.host_call_cards import HostCallCardRow
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

#: Daimon's own tools; connector routes arrive with the relay (G2).
ROUTE_DAIMON: Final[str] = "daimon"

#: A call card's buttons: ``hcc:<card id>:approve`` or ``…:deny``.
CUSTOM_ID_PATTERN: Final[str] = r"hcc:(?P<card>[0-9a-f-]{36}):(?P<action>approve|deny)"
_CUSTOM_ID = re.compile(CUSTOM_ID_PATTERN)

CallAction = Literal["approve", "deny"]

#: How a profile's gated calls are confirmed. `native`: the provider's own card;
#: `in_process`: Daimon's in-process loop asks; `press_applies`: this module.
GatePath = Literal["native", "in_process", "press_applies"]
_OWN_GATES: Final[dict[str, GatePath]] = {"anthropic": "native", "selfhosted": "in_process"}

#: Rewritten to the running turn at execution, so the asking turn's id (gone by the
#: press) is not part of what was approved.
_UNDIGESTED: Final[frozenset[str]] = frozenset({"origin_context_id"})

#: The confirmed-call credential lives only as long as one call (review H1).
CONFIRMED_CALL_TTL_S: Final[int] = 120

WAITING_TEXT: Final[str] = (
    "Waiting for {person} to confirm on the card. Don't call it again; carry on or end your reply."
)
REFUSED_TEXT: Final[str] = (
    "This tool needs a confirmation card, which this conversation can't show."
)
TOO_MANY_TEXT: Final[str] = (
    "Too many cards are waiting here. Wait for {person} to answer them before asking again."
)
APPROVED_TEXT: Final[str] = "Approved. It runs in a moment."
DENIED_TEXT: Final[str] = "Denied. Nothing ran."
EXPIRED_TEXT: Final[str] = "This request ended before anyone clicked. Ask again to redo it."
ANSWERED_TEXT: Final[str] = "This card was already answered."
OFF_TEXT: Final[str] = "This card can't be used right now. Ask again later."
ADMIN_TEXT: Final[str] = "Only an admin or an owner can approve this. Ask an admin or an owner."
CRASHED_TEXT: Final[str] = "This may have run. Check before asking again."
NOT_RUN_TEXT: Final[str] = "This didn't run. Ask again to retry."
FOOTER: Final[str] = "Only {requester} can approve. Expires {expires}."
WAKE_SEED: Final[str] = (
    "Approved on the card. Tell the person what happened, from the result above."
)


def gate_path(profile: str) -> GatePath:
    """The confirmation path for a mux profile; anything not allow-listed is press_applies."""
    return _OWN_GATES.get(profile, "press_applies")


def waiting_text(person: str) -> str:
    return WAITING_TEXT.format(person=person)


def canonical_args(args: Mapping[str, object]) -> dict[str, object]:
    return {key: args[key] for key in sorted(args) if key not in _UNDIGESTED}


def canonical_call_digest(
    *,
    route: str,
    server: str,
    tool: str,
    args: Mapping[str, object],
    executing_agent_id: uuid.UUID,
) -> str:
    """What one press approves: which server, which tool, which arguments, for which agent."""
    payload = json.dumps(
        [route, server, tool, canonical_args(args), str(executing_agent_id)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def card_custom_id(card_id: uuid.UUID, action: CallAction) -> str:
    return f"hcc:{card_id}:{action}"


def parse_custom_id(custom_id: str) -> tuple[uuid.UUID, CallAction] | None:
    match = _CUSTOM_ID.fullmatch(custom_id)
    if match is None:
        return None
    try:
        return uuid.UUID(match["card"]), match["action"]  # pyright: ignore[reportReturnType]
    except ValueError:
        return None


def requested_call_card(row: HostCallCardRow) -> PostedCard:
    """The card as posted: the exact call it approves, and Approve / Deny for its requester."""
    from daimon.core.posted_controls import CardButton, PostedCard

    args = json.dumps(canonical_args(row.reference.get("args", {})), indent=2, ensure_ascii=False)
    return PostedCard(
        kind="call",
        state="requested",
        headline=f"🔧 {row.agent_name} wants to run {row.tool_name}",
        facts=(f"```json\n{args}\n```",),
        buttons=(
            CardButton(
                label="Approve", style="primary", custom_id=card_custom_id(row.id, "approve")
            ),
            CardButton(label="Deny", style="primary", custom_id=card_custom_id(row.id, "deny")),
        ),
        footer=FOOTER,
        requester_platform_user_id=row.requester_platform_user_id,
        expires_at_unix=int(row.expires_at.timestamp()),
    )


def answered_call_card(text: str, *, approved: bool) -> PostedCard:
    """The card after a press closed it: what happened, and no buttons."""
    from daimon.core.posted_controls import PostedCard

    if approved:
        return PostedCard(kind="call", state="applied", headline=f"✅ {text}")
    if text == EXPIRED_TEXT:
        return PostedCard(kind="call", state="expired", headline=f"⌛ {text}")
    return PostedCard(kind="call", state="refused", headline=f"🛡️ {text}")


async def post_call_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: HostCallCardRow,
    *,
    post: Callable[[PostedCard], Awaitable[str]],
) -> bool:
    """Post the card and record its message; a failed post cancels the record."""
    from daimon.core.stores import host_call_cards as cards

    try:
        message_id = await post(requested_call_card(row))
    except Exception:
        # Any post failure (permissions, size, network) leaves no pressable card,
        # so the record must not wait on one.
        log.exception("host_call.post_failed", card_id=str(row.id))
        async with sessionmaker.begin() as session:
            await cards.cancel(
                session, tenant_id=row.tenant_id, card_id=row.id, outcome=NOT_RUN_TEXT
            )
        return False
    async with sessionmaker.begin() as session:
        await cards.mark_posted(session, card_id=row.id, message_id=message_id)
    return True


@dataclass(frozen=True)
class CallPress:
    """What a press did. An open (not closed) press only answers the presser."""

    text: str
    closed: bool
    approved: bool = False


def wake_key(card_id: uuid.UUID) -> uuid.UUID:
    """One wake per record; a later wake finds its calls already run and stays quiet."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"confirmed_call:{card_id}")


async def press_host_call(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    enabled: bool,
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    action: CallAction,
    clicker: str,
    message_id: str,
    current_admin: Callable[[str], Awaitable[bool]],
    now: dt.datetime,
) -> CallPress | None:
    """Answer one press. None: not the requester's card (it stays as it is).

    The requester and message are checked before the claim, so nobody else's press
    can use the card up. An approve claims and queues its wake in one transaction.
    """
    from daimon.core.continuity.continuation import ContinuationRequest
    from daimon.core.continuity.wakes import enqueue_wake
    from daimon.core.stores import host_call_cards as cards

    async with sessionmaker() as session:
        card = await cards.get_card(session, tenant_id=tenant_id, card_id=card_id)
    if card is None or card.requester_platform_user_id != clicker or card.message_id != message_id:
        return None
    if not enabled:
        return CallPress(OFF_TEXT, closed=False)
    if card.status != "pending":
        return CallPress(card.outcome or ANSWERED_TEXT, closed=True)
    if (
        action == "approve"
        and card.reference.get("admin") is True
        and not await current_admin(clicker)
    ):
        return CallPress(ADMIN_TEXT, closed=False)
    to: Literal["approved", "denied"] = "approved" if action == "approve" else "denied"
    async with sessionmaker.begin() as session:
        claimed = await cards.claim(
            session,
            tenant_id=tenant_id,
            card_id=card_id,
            to=to,
            now=now,
            outcome=APPROVED_TEXT if to == "approved" else DENIED_TEXT,
        )
        if claimed is not None and to == "approved":
            await enqueue_wake(
                session,
                ContinuationRequest(
                    tenant_id=claimed.tenant_id,
                    platform=claimed.platform,  # pyright: ignore[reportArgumentType]
                    parent_channel_id=claimed.channel_id,
                    thread_id=claimed.thread_id,
                    requester_account_id=claimed.requester_account_id,
                    requester_external_user_id=claimed.requester_platform_user_id,
                    target_ma_agent_id=claimed.executing_ma_agent_id,
                    target_name=claimed.agent_name,
                    requested_work=WAKE_SEED,
                    reason="confirmed_call",
                    # The presser is here: the wake turn is theirs, as attended as a mention.
                    attended=True,
                    idempotency_key=wake_key(claimed.id),
                ),
                available_at=now,
            )
    if claimed is None:
        return CallPress(EXPIRED_TEXT if card.expires_at <= now else ANSWERED_TEXT, closed=True)
    log.info("host_call.pressed", card_id=str(card_id), action=action)
    return CallPress(claimed.outcome or ANSWERED_TEXT, closed=True, approved=to == "approved")


#: Calls one Daimon tool over MCP with `token`; raises on transport or tool failure.
CallTool = Callable[[str, str, Mapping[str, object]], Awaitable[object]]


@dataclass(frozen=True)
class ConfirmedCallRunner:
    """What a wake turn needs to run its approved calls before its first message."""

    sessionmaker: async_sessionmaker[AsyncSession]
    secret: bytes
    tenant_id: uuid.UUID
    platform: str
    thread_id: str
    executing_agent_id: uuid.UUID
    call_tool: CallTool
    clock: Callable[[], dt.datetime]


def confirmed_call_prefix(runner: ConfirmedCallRunner) -> Callable[[], Awaitable[str]]:
    """The once-only prefix hook (`mutation_gate.initial_send_prefix`).

    Cached, because a recovered send resends: the calls must never run twice.
    """
    done: list[str] = []

    async def prefix() -> str:
        if not done:
            done.append(await run_confirmed_calls(runner))
        return done[0]

    return prefix


async def sweep_stale_calls(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: dt.datetime
) -> int:
    """Close what no wake will: unanswered cards, approved calls that never ran, crashed runs.

    A call a crashed worker left executing may have run, so it says check first; an
    approved call that outlived its card never started, so it says it didn't run.
    """
    from daimon.core.stores import host_call_cards as cards

    async with sessionmaker.begin() as session:
        return await cards.sweep(
            session,
            now=now,
            executing_before=now - dt.timedelta(seconds=CONFIRMED_CALL_TTL_S),
            crashed_outcome=CRASHED_TEXT,
            unrun_outcome=NOT_RUN_TEXT,
        )


async def has_approved_calls(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    executing_agent_id: uuid.UUID,
    now: dt.datetime,
) -> bool:
    """Whether a `confirmed_call` wake has anything to run; stale calls are closed first.

    An earlier wake runs every approved call for the thread, so a later one for the
    same thread finds none and must not spend a turn.
    """
    from daimon.core.stores import host_call_cards as cards

    await sweep_stale_calls(sessionmaker, now=now)
    async with sessionmaker() as session:
        rows = await cards.list_approved(
            session,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            executing_agent_id=executing_agent_id,
        )
    return bool(rows)


async def run_confirmed_calls(runner: ConfirmedCallRunner) -> str:
    """Run every approved call for this thread and agent; their results, framed as untrusted."""
    from daimon.core.stores import host_call_cards as cards
    from daimon.core.turn_origin import current_execution_origin_id

    await sweep_stale_calls(runner.sessionmaker, now=runner.clock())
    async with runner.sessionmaker() as session:
        rows = await cards.list_approved(
            session,
            tenant_id=runner.tenant_id,
            platform=runner.platform,
            thread_id=runner.thread_id,
            executing_agent_id=runner.executing_agent_id,
        )
    if not rows:
        return ""
    origin_id = current_execution_origin_id.get()
    origin = None if origin_id is None else await _session_origin(runner, origin_id, rows[0])
    results: list[str] = []
    for row in rows:
        if origin is None:
            # Outside a lease-held turn there is no credential to run under: fail closed.
            async with runner.sessionmaker.begin() as session:
                await cards.fail_approved(session, card_id=row.id, outcome=NOT_RUN_TEXT)
            results.append(_framed(row, "failed", NOT_RUN_TEXT))
            continue
        results.append(await _run_one(runner, row, origin))
    return "\n\n".join(("Calls the person approved on a card ran before this message:", *results))


async def _session_origin(
    runner: ConfirmedCallRunner, origin_id: uuid.UUID, first: HostCallCardRow
) -> tuple[TurnOriginRow, str] | None:
    """This turn's origin and its session grant, binding the grant if none is bound yet."""
    from daimon.core.stores.turn_origins import bind_origin_session, get_active_origin

    async with runner.sessionmaker.begin() as session:
        origin = await get_active_origin(
            session,
            origin_id=origin_id,
            tenant_id=runner.tenant_id,
            account_id=first.requester_account_id,
            platform=runner.platform,
            now=runner.clock(),
        )
        if origin is None or origin.thread_id != runner.thread_id:
            return None
        grant = origin.chat_session_id
        if grant is None:
            # Same grant the send-time runtime derives (`mcp_auth`); one bound later
            # replaces it, after these calls have spent their credentials.
            grant = str(origin_id)
            await bind_origin_session(session, origin_id=origin_id, chat_session_id=grant)
    return origin, grant


async def _run_one(
    runner: ConfirmedCallRunner, row: HostCallCardRow, origin: tuple[TurnOriginRow, str]
) -> str:
    import jwt as pyjwt
    from daimon.core.mcp_auth import internal_token_label, mint_agent_mcp_token
    from daimon.core.stores import host_call_cards as cards
    from daimon.core.stores.mcp_tokens import revoke_mcp_token

    turn, grant = origin
    jti = uuid.uuid4()
    async with runner.sessionmaker.begin() as session:
        minted = await mint_agent_mcp_token(
            session,
            account_id=turn.account_id,
            tenant_id=runner.tenant_id,
            agent_id=runner.executing_agent_id,
            label=internal_token_label(jti),
            secret=runner.secret,
            now=runner.clock(),
            ttl_days=CONFIRMED_CALL_TTL_S / 86400,  # pyright: ignore[reportArgumentType]
            chat_execution=True,
            surface="provider_turn",
            jti=jti,
        )
    claims = pyjwt.decode(
        minted, runner.secret, algorithms=["HS256"], options={"verify_exp": False}
    )
    token = pyjwt.encode(
        {**claims, "chat_session_id": grant, "confirmed_call": str(row.id)},
        runner.secret,
        algorithm="HS256",
    )
    args = dict(row.reference.get("args", {}))
    if "origin_context_id" in args:
        args["origin_context_id"] = str(turn.id)
    try:
        await runner.call_tool(token, row.tool_name, args)
    except Exception:
        # The middleware records the tool's own outcome; this only covers a call that
        # never reached it (transport, refused credential).
        log.warning("host_call.call_failed", card_id=str(row.id), exc_info=True)
    finally:
        async with runner.sessionmaker.begin() as session:
            await revoke_mcp_token(session, jti=jti, now=runner.clock())
    async with runner.sessionmaker.begin() as session:
        await cards.fail_approved(session, card_id=row.id, outcome=NOT_RUN_TEXT)
        final = await cards.get_card(session, tenant_id=runner.tenant_id, card_id=row.id)
    status = final.status if final is not None else "failed"
    outcome = (final.outcome if final is not None else None) or NOT_RUN_TEXT
    log.info("host_call.executed", card_id=str(row.id), status=status)
    return _framed(row, status, outcome)


def _framed(row: HostCallCardRow, status: str, outcome: str) -> str:
    return render_untrusted(
        outcome, source=f"tool:{row.tool_name}", tag="confirmed_call", attrs={"status": status}
    )


def mcp_call_tool(url: str) -> CallTool:
    """Call a tool on Daimon's own MCP server at `url` with a confirmed_call token."""
    from daimon.core.mcp_tool_call import mcp_tool_session
    from pydantic import JsonValue

    async def call(token: str, name: str, args: Mapping[str, object]) -> object:
        async with mcp_tool_session(url, token) as session:
            return await session.call_tool(name, cast("Mapping[str, JsonValue]", args))

    return call

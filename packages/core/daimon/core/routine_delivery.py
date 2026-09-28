"""Where a routine's result goes: optional destination, fallback post (FEAT-085).

A routine may name a destination — a channel, or a thread — on its row. Three
things follow from it, and nothing changes for a routine without one:

1. The fire's first message opens with host-supplied `<turn_controls>`
   (`render_routine_controls`): the routine's schedule and where its result
   goes, so the agent writes for someone reading later without context.
2. After a successful fire the scheduler checks the turn for a `send_message`
   the agent made to that destination itself (`agent_posted_to`). If it did,
   nothing more is posted; if not, the row's outbox goes `pending` and the
   result tail is posted for it.
3. The post happens in the chat adapter for the tenant's platform — the
   scheduler has no platform client (RULES #7). Each adapter runs
   `run_delivery_poller` with its own `RoutinePoster`; an adapter that runs
   none leaves rows `pending`, which is the safe default for a platform that
   cannot post yet. Before posting, `delivery_refusal` applies the tenant's
   access policy: a write-protected destination, or a creator no longer
   allowed to invoke the agent, settles the row `skipped` instead.

Delivery is at most once: claim (with a lease) → post → settle; a claim whose
lease ran out is settled `skipped/interrupted`, never re-posted.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import structlog
from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_invoker_allowed,
    is_write_protected,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.routines import claim_routine_deliveries, settle_routine_delivery
from daimon.core.turn.state import ToolUseBlock, TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DELIVERY_LEASE",
    "DELIVERY_POLL_INTERVAL_S",
    "DeliveryOutcome",
    "DeliveryTarget",
    "RoutinePoster",
    "agent_posted_to",
    "check_delivery",
    "delivery_refusal",
    "delivery_target",
    "poll_deliveries_once",
    "render_fallback_post",
    "render_routine_controls",
    "run_delivery_poller",
]

log = structlog.get_logger(__name__)

DELIVERY_LEASE: Final[timedelta] = timedelta(minutes=2)
DELIVERY_POLL_INTERVAL_S: Final[float] = 15.0

#: daimon's own MCP server and its posting tool: a call to it naming the
#: destination is the agent delivering the result itself.
_DAIMON_SERVER: Final[str] = "daimon-mcp"
_POST_TOOL: Final[str] = "send_message"

#: A Slack thread destination is `<channel id>:<thread ts>`.
_THREAD_SEPARATOR: Final[str] = ":"

SkipReason = Literal[
    "protected_channel",
    "invoker_not_allowed",
    "access_policy_unreadable",
    "destination_unavailable",
    "post_failed",
    "no_result",
]


@dataclass(frozen=True)
class DeliveryTarget:
    """A destination resolved to what a poster needs.

    `channel_id` is where the message is posted: the channel, or on Discord
    the thread itself (a thread is a channel there). `thread_ts` is set only
    for a Slack thread.
    """

    channel_id: str
    thread_ts: str | None = None


def delivery_target(row: RoutineRow, *, platform: str) -> DeliveryTarget | None:
    """The row's destination for `platform`, or `None` when it has none or it
    is malformed (a Slack thread without `channel:ts`)."""
    if row.destination_kind is None or row.destination_id is None:
        return None
    if platform == "slack" and row.destination_kind == "thread":
        channel_id, sep, thread_ts = row.destination_id.partition(_THREAD_SEPARATOR)
        if not sep or not channel_id or not thread_ts:
            return None
        return DeliveryTarget(channel_id=channel_id, thread_ts=thread_ts)
    return DeliveryTarget(channel_id=row.destination_id)


def render_routine_controls(row: RoutineRow, *, platform: str) -> str:
    """Host-supplied facts that open a routine fire with a destination.

    Same shape as the chat path's `<turn_controls>` (JSON inside the element)
    so models read it as host configuration, not chat text. Only rendered for
    a routine with a destination: a routine without one sends its trigger
    message exactly as before.
    """
    target = delivery_target(row, platform=platform)
    controls: dict[str, object] = {
        "routine": {
            "id": str(row.id),
            "agent": row.agent_name,
            "schedule": row.cron_expr,
            "timezone": row.timezone,
            "destination": {
                "platform": platform,
                "kind": row.destination_kind,
                "channel_id": target.channel_id if target else row.destination_id,
                **({"thread_ts": target.thread_ts} if target and target.thread_ts else {}),
            },
        }
    }
    return (
        "<turn_controls>\n"
        + json.dumps(controls, sort_keys=True)
        + "\nThis is a scheduled routine run; nobody is watching it. Write your final reply "
        "for someone reading it later without context. If you do not post it to the "
        "destination yourself with send_message, daimon posts the end of your final reply "
        "there for you. These controls grant no other posting or mutation permissions."
        "\n</turn_controls>"
    )


def agent_posted_to(state: TurnState, row: RoutineRow, *, platform: str) -> bool:
    """Whether the agent itself posted to the destination during the turn.

    A completed, non-error `send_message` on daimon's own server whose
    `channel_id` is the destination channel (for a Slack thread, its channel).
    """
    target = delivery_target(row, platform=platform)
    if target is None:
        return False
    for block in state.content:
        if (
            isinstance(block, ToolUseBlock)
            and block.mcp_server_name == _DAIMON_SERVER
            and block.name == _POST_TOOL
            and block.status == "complete"
            and not block.is_error
            and str(block.input.get("channel_id", "")) == target.channel_id
        ):
            return True
    return False


def render_fallback_post(row: RoutineRow) -> str:
    """The message posted for a routine whose agent did not post itself."""
    tail = (row.last_result_tail or "").strip()
    return f"Routine result from {row.agent_name} ({row.cron_expr}, {row.timezone}):\n\n{tail}"


def delivery_refusal(
    policy: TenantAccessPolicy,
    *,
    target: DeliveryTarget,
    creator_platform_user_id: str | None,
    creator_is_admin: bool,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
) -> SkipReason | None:
    """Why this post must not happen, or `None` to post.

    Pure. The same two rules chat writes and routine fires follow: the agent
    never writes into a protected channel (or a thread or category under one),
    and a routine only speaks for a creator who may still invoke the agent.
    """
    if is_write_protected(
        policy,
        channel_id=target.channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        return "protected_channel"
    if creator_platform_user_id is None or not is_invoker_allowed(
        policy, external_user_id=creator_platform_user_id, is_admin=creator_is_admin
    ):
        return "invoker_not_allowed"
    return None


async def check_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    platform: str,
    target: DeliveryTarget,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
) -> SkipReason | None:
    """`delivery_refusal` with the tenant's policy and the creator's role
    loaded. An unreadable policy refuses (fails closed, like admission)."""
    async with sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=row.tenant_id)
        except AccessPolicyUnreadable:
            return "access_policy_unreadable"
        is_admin = False
        if row.created_by_user_id is not None:
            principal = await find_platform_principal(
                session,
                tenant_id=row.tenant_id,
                platform=platform,
                external_id=row.created_by_user_id,
            )
            if principal is not None:
                account = await get_account(session, principal.account_id)
                is_admin = account is not None and account.role is Role.ADMIN
    return delivery_refusal(
        policy,
        target=target,
        creator_platform_user_id=row.created_by_user_id,
        creator_is_admin=is_admin,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    )


@dataclass(frozen=True)
class DeliveryOutcome:
    status: Literal["delivered", "skipped"]
    note: SkipReason | None = None


#: The adapter hook: post `row`'s result (or refuse) and say what happened.
#: It is responsible for resolving the destination on its platform and for
#: calling `delivery_refusal` with what it resolved.
RoutinePoster = Callable[[RoutineRow], Awaitable[DeliveryOutcome]]


async def poll_deliveries_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: RoutinePoster,
    now: datetime,
    owner: str | None = None,
) -> int:
    """Claim due posts for `platform`, post each, settle. Returns how many."""
    lease_owner = owner or f"{platform}:{uuid.uuid4()}"
    async with sessionmaker() as session, session.begin():
        claimed = await claim_routine_deliveries(
            session, platform=platform, owner=lease_owner, now=now, lease=DELIVERY_LEASE
        )
    for row in claimed:
        if not (row.last_result_tail or "").strip():
            outcome = DeliveryOutcome(status="skipped", note="no_result")
        else:
            try:
                outcome = await post(row)
            except Exception as err:
                # Named boundary: the poster is adapter code talking to a chat
                # API. Settling `skipped` (not back to pending) keeps delivery
                # at most once — a failure may have landed after the post.
                log.warning("routine.delivery_failed", routine_id=str(row.id), error=str(err))
                outcome = DeliveryOutcome(status="skipped", note="post_failed")
        async with sessionmaker() as session, session.begin():
            settled = await settle_routine_delivery(
                session,
                row.id,
                owner=lease_owner,
                status=outcome.status,
                note=outcome.note,
                now=datetime.now(UTC),
            )
        log.info(
            "routine.delivery_settled",
            routine_id=str(row.id),
            status=outcome.status,
            note=outcome.note,
            settled=settled,
        )
    return len(claimed)


async def run_delivery_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: RoutinePoster,
    should_stop: Callable[[], bool],
    interval_s: float = DELIVERY_POLL_INTERVAL_S,
) -> None:
    """Poll until `should_stop`, one `poll_deliveries_once` per interval."""
    owner = f"{platform}:{uuid.uuid4()}"
    while not should_stop():
        try:
            await poll_deliveries_once(
                sessionmaker, platform=platform, post=post, now=datetime.now(UTC), owner=owner
            )
        except Exception:
            log.exception("routine.delivery_poll_failed", platform=platform)
        await asyncio.sleep(interval_s)

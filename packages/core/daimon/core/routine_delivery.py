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
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_subject
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
    "destination_shape_error",
    "render_fallback_dm",
    "DirectPost",
    "clear_creator",
    "discord_creator_may_post",
    "slack_creator_may_post",
    "teams_thread_id",
    "creator_refusal_for",
    "placement_unknown_is_unsafe",
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
#: The MCP search interface's proxy: `call_tool(name=..., arguments=...)`.
_CALL_TOOL: Final[str] = "call_tool"

_SLACK_CHANNEL: Final[re.Pattern[str]] = re.compile(r"[CG][A-Z0-9]{2,}")
_SLACK_THREAD: Final[re.Pattern[str]] = re.compile(r"[CG][A-Z0-9]{2,}:[0-9]+\.[0-9]+")
_TEAMS_CHANNEL: Final[str] = r"19:[\w-]+@thread\.(?:tacv2|skype)"
_TEAMS_THREAD: Final[re.Pattern[str]] = re.compile(rf"({_TEAMS_CHANNEL});messageid=([0-9]+)")

_TEAMS_DESTINATION: Final[re.Pattern[str]] = re.compile(
    rf"({_TEAMS_CHANNEL})(?:;messageid=[0-9]+)?"
)

#: A Slack thread destination is `<channel id>:<thread ts>`.
_THREAD_SEPARATOR: Final[str] = ":"

SkipReason = Literal[
    "protected_channel",
    "creator_cannot_post",
    "invoker_not_allowed",
    "access_policy_unreadable",
    "destination_unavailable",
    "post_failed",
    "no_result",
    "channel_isolated",
]


@dataclass(frozen=True)
class DeliveryTarget:
    """A destination resolved to what a poster needs.

    `channel_id` is where the message is posted: the channel, or on Discord
    the thread itself (a thread is a channel there). `thread_ts` is set only
    for a thread on Slack (its ts) or Teams (its root message id), whose
    channel is `channel_id`.
    """

    channel_id: str
    thread_ts: str | None = None


def delivery_target(row: RoutineRow, *, platform: str) -> DeliveryTarget | None:
    """The row's destination for `platform`, or `None` when it has none or it
    is malformed (a Slack thread without `channel:ts`)."""
    if row.destination_kind is None or row.destination_id is None:
        return None
    if platform == "teams" and row.destination_kind == "thread":
        thread = _TEAMS_THREAD.fullmatch(row.destination_id)
        return DeliveryTarget(*thread.groups()) if thread else None
    if platform == "slack" and row.destination_kind == "thread":
        channel_id, sep, thread_ts = row.destination_id.partition(_THREAD_SEPARATOR)
        if not sep or not channel_id or not thread_ts:
            return None
        return DeliveryTarget(channel_id=channel_id, thread_ts=thread_ts)
    return DeliveryTarget(channel_id=row.destination_id)


def teams_channel_of(destination_id: str) -> str | None:
    """The Teams channel a destination id names (the channel, or a thread in it);
    None for any other platform's id. Teams ids contain ":", so never split one there."""
    match = _TEAMS_DESTINATION.fullmatch(destination_id)
    return match.group(1) if match else None


def teams_thread_id(target: DeliveryTarget) -> str:
    """The conversation a Teams target is posted to: the channel, or the thread in it."""
    if target.thread_ts is None:
        return target.channel_id
    return f"{target.channel_id};messageid={target.thread_ts}"


DirectPost = Literal["allowed", "protected", "unverified"]


def render_routine_controls(
    row: RoutineRow, *, platform: str, direct_post: DirectPost = "allowed"
) -> str:
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
                # A Teams thread is posted to by its own id.
                **(
                    {"thread_id": row.destination_id}
                    if platform == "teams" and target and target.thread_ts
                    else {}
                ),
            },
        }
    }
    if direct_post == "allowed":
        delivery = (
            "If you do not post it to the destination yourself with send_message, daimon "
            "posts the end of your final reply there for you."
        )
    elif direct_post == "protected":
        # Protected since the routine was made: never invite a write there.
        delivery = (
            "The destination is now a protected channel: do not post there. daimon sends "
            "the end of your final reply to the routine's creator instead."
        )
    else:
        # Its parent channel or category could not be checked here: never
        # invite a direct write; the poster checks placement and delivers.
        delivery = (
            "Do not post to the destination yourself. daimon delivers the end of your "
            "final reply for you, to the destination if the workspace policy allows it "
            "and otherwise to the routine's creator."
        )
    return (
        "<turn_controls>\n"
        + json.dumps(controls, sort_keys=True)
        + "\nThis is a scheduled routine run; nobody is watching it. Write your final reply "
        "for someone reading it later without context. "
        + delivery
        + " These controls grant no other posting or mutation permissions."
        "\n</turn_controls>"
    )


def _posted_channel(block: ToolUseBlock) -> str | None:
    """The `channel_id` a successful daimon `send_message` posted to, or None.

    Recognises the direct call and the same call made through the MCP search
    interface (`call_tool(name="send_message", arguments={...})`), which is
    how a large daimon catalog is reached.
    """
    if block.mcp_server_name != _DAIMON_SERVER or block.status != "complete" or block.is_error:
        return None
    arguments: object = block.input
    if block.name == _CALL_TOOL:
        if block.input.get("name") != _POST_TOOL:
            return None
        arguments = block.input.get("arguments")
    elif block.name != _POST_TOOL:
        return None
    if not isinstance(arguments, dict):
        return None
    channel_id = cast("dict[str, object]", arguments).get("channel_id")
    return channel_id if isinstance(channel_id, str) else None


def agent_posted_to(state: TurnState, row: RoutineRow) -> bool:
    """Whether the agent itself posted to the destination during the turn.

    The comparison is on the full destination as `send_message` names it: a
    channel id, a Discord thread id, or a Slack thread's `<channel>:<ts>`. A
    post to the bare channel of a Slack thread destination is not the thread,
    and does not count.
    """
    if row.destination_id is None:
        return False
    return any(
        _posted_channel(block) == row.destination_id
        for block in state.content
        if isinstance(block, ToolUseBlock)
    )


def destination_shape_error(platform: str, kind: str, destination_id: str) -> str | None:
    """Why `destination_id` cannot name a `kind` on `platform`, or None.

    Discord channels and threads are numeric ids. Slack channels are
    `C…`/`G…` ids and a Slack thread is `<channel id>:<thread ts>`. A Teams
    channel is `19:…@thread.tacv2` and a thread `<channel>;messageid=<root id>`.
    """
    if platform == "discord":
        return None if destination_id.isdigit() else "a Discord channel or thread id is a number"
    if platform == "slack":
        if kind == "thread":
            if _SLACK_THREAD.fullmatch(destination_id) is None:
                return "a Slack thread is <channel id>:<thread ts>, e.g. C0123ABC:1717171717.123456"
            return None
        if _SLACK_CHANNEL.fullmatch(destination_id) is None:
            return "a Slack channel id looks like C0123ABC"
        return None
    if platform == "teams":
        if kind == "thread":
            if _TEAMS_THREAD.fullmatch(destination_id) is None:
                return "a Teams thread is <channel id>;messageid=<root message id>"
            return None
        if re.fullmatch(_TEAMS_CHANNEL, destination_id) is None:
            return "a Teams channel id looks like 19:abc123@thread.tacv2"
        return None
    return f"routine destinations are not supported on {platform}"


def render_fallback_post(row: RoutineRow) -> str:
    """The message posted for a routine whose agent did not post itself."""
    payload = (row.delivery_payload or "").strip()
    return f"Routine result from {row.agent_name} ({row.cron_expr}, {row.timezone}):\n\n{payload}"


_DM_REASONS: Final[dict[str, str]] = {
    "protected_channel": "its destination is a protected channel daimon does not post in",
    "creator_cannot_post": "you can no longer post in its destination",
    "destination_unavailable": "its destination could not be reached (missing, moved, or "
    "outside this workspace)",
}


def render_fallback_dm(row: RoutineRow, reason: str) -> str:
    """The direct message sent to the creator when the destination is unusable."""
    why = _DM_REASONS.get(reason, "its destination could not be used")
    return (
        f"Your routine for {row.agent_name} ({row.cron_expr}, {row.timezone}) finished, but "
        f"{why}, so its result is here instead. Update the routine's destination to fix "
        f"this.\n\n{(row.delivery_payload or '').strip()}"
    )


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

    Pure. Creator first: a routine only speaks for a creator who may still
    invoke the agent, and a refusal here means nothing is sent anywhere — not
    to the destination and not by direct message. Only then the destination:
    the agent never writes into a protected channel (or a thread or category
    under one), which a caller may answer with the direct-message fallback.
    """
    creator_refusal = creator_refusal_for(
        policy, creator_platform_user_id=creator_platform_user_id, creator_is_admin=creator_is_admin
    )
    if creator_refusal is not None:
        return creator_refusal
    if not authorize(
        policy,
        subject=Subject(),
        action=Action.POST,
        surface=Surface.ROUTINE,
        place=Place(
            channel_id=target.channel_id,
            parent_channel_id=parent_channel_id,
            category_id=category_id,
        ),
    ):
        return "protected_channel"
    return None


def creator_refusal_for(
    policy: TenantAccessPolicy, *, creator_platform_user_id: str | None, creator_is_admin: bool
) -> SkipReason | None:
    """`invoker_not_allowed` unless the routine's creator may still invoke."""
    if not authorize(
        policy,
        subject=build_subject(is_admin=creator_is_admin, platform_user_id=creator_platform_user_id),
        action=Action.ACT_FOR_CREATOR,
        surface=Surface.ROUTINE,
    ):
        return "invoker_not_allowed"
    return None


async def clear_creator(
    sessionmaker: async_sessionmaker[AsyncSession], row: RoutineRow, *, platform: str
) -> TenantAccessPolicy | SkipReason:
    """The tenant's policy if the routine may send anything at all, else why not.

    The gate every poster passes BEFORE resolving the destination or sending
    anything (destination post or direct-message fallback): the policy must be
    readable (fails closed, like admission) and the creator must still be
    allowed to invoke the agent. Protection and availability are decided only
    after this, and only choose where a cleared result goes.
    """
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
    refusal = creator_refusal_for(
        policy, creator_platform_user_id=row.created_by_user_id, creator_is_admin=is_admin
    )
    return refusal if refusal is not None else policy


def discord_creator_may_post(
    *,
    administrator: bool,
    view_channel: bool,
    send_messages: bool,
    is_thread: bool,
    send_messages_in_threads: bool,
    is_private_thread: bool,
    manage_threads: bool,
    is_thread_member: bool,
) -> bool:
    """Whether a routine's creator may post where the routine delivers.

    A routine posts on its creator's behalf, so it may only post where the
    creator could: the rule the `send_message` tool applies to a caller
    (view + send; in a thread, view + send in threads), plus a private thread
    needs membership or manage_threads, as reading one does. Administrators may
    always post. Pure: the adapter supplies the facts it resolved.
    """
    if administrator:
        return True
    if not view_channel:
        return False
    if not is_thread:
        return send_messages
    # In a thread Discord checks send_messages_in_threads, not send_messages.
    if not send_messages_in_threads:
        return False
    return not is_private_thread or manage_threads or is_thread_member


def slack_creator_may_post(
    *, is_im_or_mpim: bool, is_private: bool, is_guest: bool, is_member: bool
) -> bool:
    """Slack's channel-access rule for the routine's creator, as the channel
    tools apply it to a caller: never a DM; a private channel, or any channel
    for a guest, needs membership; a public channel is open to full members."""
    if is_im_or_mpim:
        return False
    if is_private or is_guest:
        return is_member
    return True


def placement_unknown_is_unsafe(
    policy: TenantAccessPolicy, *, platform: str, kind: str | None
) -> bool:
    """Whether a destination whose parent/category is unknown must be treated
    as protected.

    Used where the placement cannot be resolved (the scheduler has no platform
    client): a Discord thread may sit under a protected channel or category,
    and a Discord channel may sit in a protected category. Slack has no
    categories, and a Slack thread's channel is its destination id, so a
    Slack destination is always fully known.
    """
    if platform != "discord":
        return False
    if kind == "thread":
        return bool(policy.protected_channel_ids or policy.protected_category_ids)
    return bool(policy.protected_category_ids)


@dataclass(frozen=True)
class DeliveryOutcome:
    status: Literal["delivered", "skipped"]
    #: A `SkipReason`, or `dm_fallback:<reason>` for a result delivered to the
    #: creator by direct message instead of the destination.
    note: str | None = None


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
        if not (row.delivery_payload or "").strip():
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

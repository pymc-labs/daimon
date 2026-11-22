"""Tell a routine's owner by direct message that a run failed, on every chat platform.

Once per failing streak (`scheduler.tell_owner`), where routines may DM the owner. The
routine's agent writes it from fixed facts (`routine_owner_notice`) in one call with no
tools, on its own instructions, metered to the owner after the balance and cap gates. A
refusal, an owner the agent no longer admits (`_owner_admitted`, asked before the call), or a
call that cannot run or says nothing, sends the plain card. Discord sends from
the scheduler. A Slack or Teams notice waits on the routine (`queue_owner_notice`) for that
adapter, the only one that can reach the person, which applies its own DM rules
(`poll_owner_notices_once`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final, cast

import structlog
from anthropic import AsyncAnthropic
from daimon.core.auxiliary_calls import auxiliary_complete
from daimon.core.config import Settings, env_admits
from daimon.core.discord_membership import send_discord_dm, send_discord_error_dm
from daimon.core.dm_access import groups_by_members, roles_by_membership, sender_admits
from daimon.core.membership import MembershipResolver
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routine_delivery import routine_agent_name, routine_links
from daimon.core.routine_owner_notice import (
    OwnerNoticeFacts,
    owner_notice_card,
    owner_notice_facts,
    owner_notice_prompt,
)
from daimon.core.scheduler import OwnerNotifier
from daimon.core.spend_gate import SpendRequest, spend_gate_in_new_session
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform, RoutineRow
from daimon.core.stores.routines import queue_owner_notice, take_owner_notices
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_recording import record_auxiliary_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Refusals over budget or credits: the plain card, never a call that would spend more.
_NO_WRITE: Final = frozenset(
    {
        TerminationReason.ADMISSION_BALANCE_DEPLETED,
        TerminationReason.ADMISSION_CAP_EXCEEDED,
        TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
        TerminationReason.ADMISSION_DENIED,
    }
)
_WRITTEN: Final = frozenset(TerminationReason) - _NO_WRITE
_WRITE_TIMEOUT_S: Final = 120.0
#: One Slack or Teams notice in the poll loop: a hung chat API must not stall routine delivery.
_SEND_TIMEOUT_S: Final = 10.0
_MAX_TOKENS: Final = 1024


@dataclass(frozen=True)
class OwnerNotice:
    title: str
    body: str
    text: str | None = None
    """The agent's own words; None sends the card."""


#: One platform's DM to the owner: None once sent, else the fixed reason it was not sent.
OwnerDm = Callable[[RoutineRow, OwnerNotice], Awaitable[str | None]]


def discord_owner_dm(bot_token: str) -> OwnerDm:
    async def send(row: RoutineRow, notice: OwnerNotice) -> str | None:
        owner = row.created_by_user_id
        assert owner is not None
        if notice.text is not None:
            # Once sending starts, a failure is not retried as a card: no second DM.
            await send_discord_dm(bot_token, owner, notice.text)
        else:
            await send_discord_error_dm(bot_token, owner, notice.title, notice.body)
        return None

    return send


def build_owner_notifier(
    *,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    anthropic: AsyncAnthropic,
    discord_dm: OwnerDm | None,
    membership: MembershipResolver | None = None,
) -> OwnerNotifier:
    """The scheduler's `notify_owner`, for routines on every chat platform.

    `membership` reads a Discord owner's roles for the agent's DM grant."""

    async def notify(row: RoutineRow, reason: str, channel_told: bool) -> None:
        owner = row.created_by_user_id
        async with sm() as s:
            tenant = await get_tenant(s, row.tenant_id)
            guild_id = tenant.external_id if tenant is not None else ""
            platform = tenant.platform if tenant is not None else None
            if owner is None:
                closed: str | None = "no_owner"
            elif tenant is None or tenant.archived_at is not None:
                closed = "tenant_archived"
            elif platform not in CHAT_PLATFORMS or (platform == "discord" and discord_dm is None):
                closed = "platform_unsupported"
            elif not env_admits(settings.direct_message_policies, row.tenant_id, owner):
                closed = "dm_policy_denies"
            elif not await dm_enabled(s, tenant_id=row.tenant_id):
                closed = "dms_off"
            else:
                closed = None
        if closed is not None:
            return _skipped(row, closed)
        chat = cast(ChatPlatform, platform)
        assert owner is not None
        write = reason in _WRITTEN and await _owner_admitted(
            sm, row, owner, membership=membership if chat == "discord" else None, guild_id=guild_id
        )
        try:
            notice = await _compose(sm, settings, anthropic, row, reason, channel_told, chat, write)
        except Exception as exc:  # boundary: no facts, nothing was sent
            return _failed(sm, row, chat, exc)
        if chat == "discord" and discord_dm is not None:
            return await send_owner_notice(sm, row, notice, platform=chat, send=discord_dm)
        async with sm() as s, s.begin():
            await queue_owner_notice(s, row.id, asdict(notice))

    return notify


async def _owner_admitted(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    owner: str,
    *,
    membership: MembershipResolver | None,
    guild_id: str,
) -> bool:
    """Whether the routine agent's DM grant still admits the owner: asked before the paid call,
    on every platform, so an owner who lost access gets the card, never agent text. Only
    Discord roles can be read here; a Slack or Teams group grant stays unknown, which is
    not admitted, and that adapter's own check still gates the send."""
    groups = (
        roles_by_membership(membership, tenant_id=row.tenant_id, guild_id=guild_id)
        if guild_id and membership is not None
        else groups_by_members(None)
    )
    return await sender_admits(
        sm, tenant_id=row.tenant_id, agent_name=row.agent_name, user_id=owner, groups=groups
    )


async def _compose(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    anthropic: AsyncAnthropic,
    row: RoutineRow,
    reason: str,
    channel_told: bool,
    platform: ChatPlatform,
    write: bool,
) -> OwnerNotice:
    links = await routine_links(sm, settings, row, platform)
    facts = owner_notice_facts(
        row,
        reason,
        platform=platform,
        agent_name=await routine_agent_name(sm, row),
        channel_told=channel_told,
        now=datetime.now(UTC),
        link=links.routine(row.id) if links is not None else None,
    )
    title, body = owner_notice_card(facts)
    text: str | None = None
    if write:
        try:
            async with asyncio.timeout(_WRITE_TIMEOUT_S):
                text = await _agent_text(sm, settings, anthropic, row, facts, platform)
        except Exception as exc:  # boundary: any failure to write sends the card
            log.info("routine.owner_notice_unwritten", error_class=type(exc).__name__)
    return OwnerNotice(title, body, text)


async def _agent_text(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    anthropic: AsyncAnthropic,
    row: RoutineRow,
    facts: OwnerNoticeFacts,
    platform: ChatPlatform,
) -> str | None:
    """The routine's agent writes the notice on its own instructions, with no tools."""
    owner = row.created_by_user_id
    markup = settings.billing.markup
    request = SpendRequest(
        tenant_id=row.tenant_id,
        platform_user_id=owner,
        platform=platform,
        channel_id=None,
        agent_id=row.agent_id,
    )
    if (block := await spend_gate_in_new_session(sm, request=request, markup=markup)) is not None:
        log.info("routine.owner_notice_unwritten", reason=block)
        return None
    agent = await retrieve_agent(
        anthropic, row.agent_id, scope=resource_scope(tenant_id=str(row.tenant_id)), records=sm
    )
    model = agent.model.id
    done = await auxiliary_complete(
        anthropic,
        provider=None,
        purpose="owner_notice",
        system=agent.system or "",
        prompt=owner_notice_prompt(facts),
        max_tokens=_MAX_TOKENS,
        effort="none",
        anthropic_model=model if model.startswith("claude") else None,
    )
    await record_auxiliary_usage(
        purpose="owner_notice",
        provider=done.provider,
        sessionmaker=sm,
        tenant_id=row.tenant_id,
        platform_user_id=owner,
        model_id=done.model_id,
        input_tokens=done.usage.input_tokens,
        output_tokens=done.usage.output_tokens,
        cache_read_input_tokens=done.usage.cache_read_input_tokens,
        markup=markup,
        agent_id=row.agent_id,
    )
    return None if done.refused else (done.text.strip() or None)


async def send_owner_notice(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    notice: OwnerNotice,
    *,
    platform: str,
    send: OwnerDm,
) -> None:
    """Send one notice by `send`, then record that the owner was told, or why not."""
    try:
        closed = await send(row, notice)
    except Exception as exc:  # boundary: adapter code talking to a chat API
        return _failed(sm, row, platform, exc)
    if closed is not None:
        return _skipped(row, closed)
    written_by = "agent" if notice.text is not None else "plain"
    # A told owner is no issue: a log line only.
    log.info("routine.owner_notified", routine_id=str(row.id), written_by=written_by)


async def poll_owner_notices_once(
    sm: async_sessionmaker[AsyncSession], *, platform: str, send: OwnerDm
) -> int:
    """Send the notices waiting for `platform`'s owners; each is taken once, sent or not."""
    async with sm() as s, s.begin():
        taken = await take_owner_notices(s, platform=platform)
    for row, held in taken:
        notice = OwnerNotice(str(held["title"]), str(held["body"]), held.get("text"))
        try:
            async with asyncio.timeout(_SEND_TIMEOUT_S):
                await send_owner_notice(sm, row, notice, platform=platform, send=send)
        except TimeoutError:
            # Already taken, so it is never sent again; the owner may or may not have it.
            _skipped(row, "timeout")
    return len(taken)


def _skipped(row: RoutineRow, reason: str) -> None:
    log.info("routine.owner_notice_skipped", routine_id=str(row.id), reason=reason)


def _failed(
    sm: async_sessionmaker[AsyncSession], row: RoutineRow, platform: str, exc: BaseException
) -> None:
    report_ops_event(
        sm,
        OpsEvent(
            "routine.owner_notice_failed",
            row.tenant_id,
            reason=type(exc).__name__,
            routine_id=row.id,
            agent_id=row.agent_id,
            platform=platform,
        ),
    )

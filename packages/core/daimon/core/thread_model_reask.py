"""Ask again, once, when an agent edit leaves a conversation's model consent stale.

A conversation that picked another provider runs it on the agent's consent. When the agent
changes, that consent no longer matches, so its turns run on the agent's own model. The
first message after the change posts the preview card again; whoever confirms moves the
conversation back. The post happens off the turn: the reply never waits on it, and a failed
post is tried again by the next message. Each (consent, agent revision) is asked once.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.model_switch import SwitchRefusal, switch_fact_lines
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_model_reasks import (
    reask_key,
    reask_posted,
    reask_scope,
    record_reask,
)
from daimon.core.stores.thread_models import get_thread_choice
from daimon.core.thread_model_flow import ThreadConfirm, thread_actor
from daimon.core.thread_model_switch import APPLIES_TO, ThreadPick, preview_thread_or_refusal

if TYPE_CHECKING:
    from daimon.core.config import Settings
    from daimon.core.stores.domain import AgentBackendRevision, BackendAgent, Platform
    from daimon.core.turn.deps import TurnDeps
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

NOTICE_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class ThreadReask:
    """The card to post: what the conversation's pick changes, with its one Confirm."""

    tenant_id: uuid.UUID
    workspace_id: str
    """The tenant's platform id: a Slack team, a Teams tenant."""
    platform: str
    channel_id: str
    thread_id: str | None
    """The thread the platform posts into; None when the conversation is the channel (a DM)."""
    conversation_id: str
    """The id the pick is kept under (`conversation_key`)."""
    agent_name: str
    lines: tuple[str, ...]
    confirm: ThreadConfirm | None


ThreadReaskNotifier = Callable[[ThreadReask], Awaitable[None]]
"""Posts the card; raises when it could not, so the next message tries again."""

_PENDING: set[asyncio.Task[None]] = set()
_IN_FLIGHT: set[str] = set()


@dataclass(frozen=True)
class StaleThread:
    """What admission knows when a conversation's consent went stale."""

    deps: TurnDeps
    settings: Settings | None
    tenant_id: uuid.UUID
    platform: Platform
    channel_id: str
    thread_id: str | None
    conversation_id: str
    user_id: str
    admin: bool
    source: BackendAgent
    ma_agent_id: str
    agent_version: int | str
    consent: AgentBackendRevision


async def _ask_again(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: ThreadReaskNotifier,
    stale: StaleThread,
) -> None:
    scope = reask_scope(stale.tenant_id, stale.platform, stale.conversation_id)
    key = reask_key(stale.consent.digest, stale.agent_version)
    agent_id = derive_agent_uuid(tenant_id=stale.tenant_id, ma_agent_id=stale.ma_agent_id)
    async with sessionmaker() as session:
        if await reask_posted(session, tenant_id=stale.tenant_id, scope=scope, key=key):
            return
        choice = await get_thread_choice(
            session,
            tenant_id=stale.tenant_id,
            platform=stale.platform,
            thread_id=stale.conversation_id,
        )
        tenant = await get_tenant(session, stale.tenant_id)
    if choice is None or tenant is None:
        return
    actor, service = await thread_actor(
        turn_deps=stale.deps,
        sessionmaker=sessionmaker,
        settings=stale.settings,
        tenant_id=stale.tenant_id,
        platform=stale.platform,
        user_id=stale.user_id,
        admin=stale.admin,
    )
    pick = ThreadPick(
        service=service,
        sessionmaker=sessionmaker,
        settings=stale.settings,
        agent=stale.source,
        actor=actor,
        ma_agent_id=stale.ma_agent_id,
        platform=stale.platform,
        platform_user_id=stale.user_id,
    )
    value = f"{choice.provider}:{choice.id}"
    shown = await preview_thread_or_refusal(pick, value, admin=stale.admin)
    if not isinstance(shown, SwitchRefusal) and shown.preview is not None:
        async with asyncio.timeout(NOTICE_TIMEOUT_S):
            await notifier(
                ThreadReask(
                    tenant_id=stale.tenant_id,
                    workspace_id=tenant.external_id,
                    platform=stale.platform,
                    channel_id=stale.channel_id,
                    thread_id=stale.thread_id,
                    conversation_id=stale.conversation_id,
                    agent_name=stale.consent.agent_name,
                    lines=switch_fact_lines(shown, agent_name=None, applies_to=APPLIES_TO),
                    confirm=(
                        ThreadConfirm(value, shown.digest, shown.revision)
                        if shown.allowed
                        else None
                    ),
                )
            )
    # A refusal is as settled as a post: nothing to ask until the agent changes again.
    async with sessionmaker.begin() as session:
        await record_reask(
            session,
            tenant_id=stale.tenant_id,
            account_id=actor.account_id,
            agent_id=agent_id,
            platform=stale.platform,
            platform_user_id=stale.user_id,
            scope=scope,
            key=key,
        )


async def _run(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: ThreadReaskNotifier,
    stale: StaleThread,
    guard: str,
) -> None:
    try:
        await _ask_again(sessionmaker, notifier, stale)
    except Exception as exc:  # background card: a failure is logged, the next message retries
        log.warning(
            "thread_model.reask_failed",
            tenant_id=str(stale.tenant_id),
            err_type=type(exc).__name__,
        )
    finally:
        _IN_FLIGHT.discard(guard)


def spawn_thread_reask(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: ThreadReaskNotifier | None,
    stale: StaleThread,
) -> None:
    """Post the card off the turn; one task per conversation and agent revision at a time."""
    if notifier is None:
        return
    guard = f"{stale.tenant_id}:{stale.platform}:{stale.conversation_id}:{stale.agent_version}"
    if guard in _IN_FLIGHT:
        return
    _IN_FLIGHT.add(guard)
    task = asyncio.create_task(
        _run(sessionmaker, notifier, stale, guard), name="thread_model.reask"
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_thread_reasks() -> None:
    """Runtime shutdown/test barrier, never awaited by a turn."""
    tasks = [task for task in _PENDING if task.get_loop() is asyncio.get_running_loop()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

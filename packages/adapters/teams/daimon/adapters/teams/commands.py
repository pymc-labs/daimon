"""Text commands (`help`, `new`, `routines`, …): code-routed, no agent turn runs.

Commands answer in a 1:1 chat only. Their replies can carry account and
tenant details, and a channel reply is visible to everyone in it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.continuity.messages import render_fresh_start
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import get_live_thread_session
from microsoft_teams.api import MessageActivityInput, SentActivity

CHANNEL_POINTER = "Commands work in our 1:1 chat. Open a chat with me and send `{name}` there."


@dataclass(frozen=True)
class CommandContext:
    """One command invocation. `send` replies into the chat it came from."""

    inbound: TeamsInbound
    tenant_id: uuid.UUID
    args: str
    is_admin: bool
    runtime: TeamsRuntime
    send: Callable[[MessageActivityInput], Awaitable[SentActivity]]


CommandHandler = Callable[[CommandContext], Awaitable[None]]


def parse_command(text: str, names: Mapping[str, CommandHandler]) -> tuple[str, str] | None:
    """`(name, args)` when the message's first word is a known command."""
    word, _, args = text.strip().partition(" ")
    name = word.lower().lstrip("/")
    return (name, args.strip()) if name in names else None


async def fresh_start(context: CommandContext) -> None:
    """`new`: mark the live session for replacement; the next message starts clean."""
    inbound = context.inbound
    async with context.runtime.sessionmaker.begin() as session:
        principal = await find_platform_principal(
            session, tenant_id=context.tenant_id, platform="teams", external_id=inbound.user_id
        )
        live = None
        if principal is not None:
            live = await get_live_thread_session(
                session,
                tenant_id=context.tenant_id,
                platform="teams",
                thread_id=inbound.thread_id,
                account_id=principal.account_id,
            )
        if live is not None:
            await request_fresh_start(session, id=live.id, at=datetime.now(UTC))
    await context.send(MessageActivityInput(text=render_fresh_start("the agent")))

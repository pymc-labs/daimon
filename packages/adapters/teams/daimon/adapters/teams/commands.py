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
from microsoft_teams.cards import AdaptiveCard

CHANNEL_POINTER = "Commands work in our 1:1 chat. Open a chat with me and send `{name}` there."
ANSWERED_IN_CHAT = "I've answered `{name}` in our 1:1 chat."
NEW_IN_CHANNEL = "Each post is its own conversation: start a new post to begin afresh."


@dataclass(frozen=True)
class CommandContext:
    """One command invocation. `send` replies into the chat it came from."""

    inbound: TeamsInbound
    tenant_id: uuid.UUID
    args: str
    is_admin: bool
    runtime: TeamsRuntime
    send: Callable[[MessageActivityInput], Awaitable[SentActivity]]

    async def send_card(self, card: AdaptiveCard) -> None:
        await self.send(MessageActivityInput().add_card(card))


CommandHandler = Callable[[CommandContext], Awaitable[None]]


def parse_command(text: str, names: Mapping[str, CommandHandler]) -> tuple[str, str] | None:
    """`(name, args)` for a bare command word or `memory /<path>`; other prose is a turn."""
    word, _, args = text.strip().partition(" ")
    name, args = word.lower().lstrip("/"), args.strip()
    if name in names and (not args or (name == "memory" and args.startswith("/"))):
        return name, args
    return None


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

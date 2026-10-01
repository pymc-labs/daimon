"""Setup conversations in a 1:1 chat: open, route into, end.

Slack and Discord open a setup conversation as a new thread. A Teams 1:1 chat
has no threads, so Manage switches the chat itself into one: a setup binding
under the chat with its own thread key, `<chat>;setup=<id>`. While it is live
the chat's messages route to that key, so each setup conversation gets its own
session with Daimon and the chat's usual session is untouched, resuming once
setup ends. Rebinding the chat's own key would instead strand that session on a
responder change. Opening runs no turn and bills nothing; `new` or End ends it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable

from daimon.adapters.teams.card_actions import Actor
from daimon.adapters.teams.commands import CommandContext, fresh_start
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_card import ENDED, welcome_card
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.setup_conversations import build_setup_opener, resolve_setup_agents
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_agent_bindings import (
    create_binding,
    get_binding,
    list_active_bindings,
    update_lifecycle,
)
from daimon.core.teams_threads import new_setup_thread_id
from microsoft_teams.api import MessageActivityInput, SentActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Send = Callable[[MessageActivityInput], Awaitable[SentActivity]]


async def route_to_setup(
    sessionmaker: async_sessionmaker[AsyncSession], inbound: TeamsInbound, tenant_id: uuid.UUID
) -> TeamsInbound:
    """A 1:1 message, keyed to the chat's live setup conversation, or to the chat if none."""
    if inbound.kind != "dm":
        return inbound
    async with sessionmaker() as session:
        live = await list_active_bindings(
            session, tenant_id=tenant_id, platform="teams", parent_channel_id=inbound.channel_id
        )
    return dataclasses.replace(inbound, setup_thread_id=live[0].thread_id if live else None)


async def open_setup_conversation(
    runtime: TeamsRuntime, actor: Actor, *, target_ma_agent_id: str | None, send: Send
) -> None:
    """Bind the actor's 1:1 chat to a new setup conversation and post its welcome.

    One live setup conversation per chat: opening another ends the previous.
    """
    tenant_id, chat = actor.tenant_id, actor.conversation_id
    responder, target = await resolve_setup_agents(
        runtime.anthropic, tenant_id=tenant_id, target_ma_agent_id=target_ma_agent_id
    )
    target_name = str(target.metadata.get(MA_METADATA_KEY_NAME) or target.name) if target else None
    thread_id = new_setup_thread_id(chat)
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=actor.user_id
        )
        for old in await list_active_bindings(
            session, tenant_id=tenant_id, platform="teams", parent_channel_id=chat
        ):
            await _mark_ended(session, tenant_id, chat, old.thread_id)
        await create_binding(
            session,
            tenant_id=tenant_id,
            platform="teams",
            parent_channel_id=chat,
            thread_id=thread_id,
            responder_ma_agent_id=str(responder.id),
            responder_name=str(responder.metadata.get(MA_METADATA_KEY_NAME) or responder.name),
            configuration_target_ma_agent_id=str(target.id) if target else None,
            configuration_target_name=target_name,
            creator_account_id=principal.account_id,
        )
    opener = build_setup_opener(
        target_display=target_name, bot_mention=None, is_admin=actor.is_admin, admin_noun="an admin"
    )
    card = welcome_card(target_name=target_name, opener=opener, thread_id=thread_id)
    try:
        await asyncio.wait_for(send(MessageActivityInput().add_card(card)), SEND_TIMEOUT_S)
    except Exception:
        # Never leave a conversation live that nobody was told about.
        async with runtime.sessionmaker.begin() as session:
            await _mark_ended(session, tenant_id, chat, thread_id)
        raise


async def end_setup_conversation(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    chat_id: str,
    thread_id: str,
) -> bool:
    """End the chat's setup conversation; False when it had already ended.

    Scoped to `chat_id`, the actor's own chat, so nobody ends another's.
    """
    async with sessionmaker.begin() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant_id,
            platform="teams",
            parent_channel_id=chat_id,
            thread_id=thread_id,
        )
        if binding is None or binding.kind != "setup" or binding.deleted:
            return False
        await _mark_ended(session, tenant_id, chat_id, thread_id)
    return True


async def new_command(context: CommandContext) -> None:
    """`new`: ends a live setup conversation, otherwise starts the chat afresh."""
    inbound = context.inbound
    if inbound.setup_thread_id is None:
        await fresh_start(context)
        return
    await end_setup_conversation(
        context.runtime.sessionmaker,
        tenant_id=context.tenant_id,
        chat_id=inbound.channel_id,
        thread_id=inbound.setup_thread_id,
    )
    await context.send(MessageActivityInput(text=ENDED))


async def _mark_ended(
    session: AsyncSession, tenant_id: uuid.UUID, chat_id: str, thread_id: str
) -> None:
    await update_lifecycle(
        session,
        tenant_id=tenant_id,
        platform="teams",
        parent_channel_id=chat_id,
        thread_id=thread_id,
        deleted=True,
    )

"""Open a shared public setup thread from a selected agent's panel."""

from __future__ import annotations

import contextlib

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.checks import ADMIN_NOUN
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.roster import RosterAgent
from daimon.core.setup_conversations import (
    build_setup_opener,
    resolve_setup_agents,
    setup_thread_name,
)
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
from sqlalchemy.exc import SQLAlchemyError

import discord

_log = structlog.get_logger()


def _legacy_target(state: PanelState) -> RosterAgent | None:
    """The editor panel's selection, in the shape the read-only panel passes.

    The editor holds its own `RosterEntry`; it calls this function without a
    target and gets the same behaviour it had before the parameter existed.
    Goes away with the editor.
    """
    entry = state.selected
    if entry is None:
        return None
    return RosterAgent(
        name=entry.name,
        ma_agent_id=entry.ma_agent_id,
        model_id=entry.model,
        is_built_in=entry.is_system,
    )


async def open_setup_conversation(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    target: RosterAgent | None = None,
) -> None:
    """Validate identities, persist routing, then present a ready conversation.

    `target` is what the conversation will be about — the caller names it rather
    than the conversation inferring it, because the screen the person clicked
    from is the only thing that knows which agent they were reading about.

    The caller defers before entering. Opening this thread never creates an MA
    session or runs a turn; the opener is deterministic platform content.
    """
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        channel = channel.parent
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(
            "Setup conversations need a server text channel. Open `/agent-setup` there.",
            ephemeral=True,
        )
        return
    if interaction.client.user is None:
        await interaction.followup.send("The bot is still connecting. Try again.", ephemeral=True)
        return
    thread: discord.Thread | None = None
    try:
        tenant_id = await resolve_tenant_for_panel(runtime, interaction)
        selected = target if target is not None else _legacy_target(state)
        if selected is not None and not selected.ma_agent_id:
            raise DaimonError("That agent is not ready. Reopen `/agent-setup` and try again.")
        responder, ma_target = await resolve_setup_agents(
            runtime.anthropic,
            tenant_id=tenant_id,
            target_ma_agent_id=selected.ma_agent_id if selected else None,
        )
        target_name = selected.name if ma_target is not None and selected is not None else None
        opener = build_setup_opener(
            target_display=target_name,
            bot_mention=interaction.client.user.mention,
            is_admin=state.is_admin,
            admin_noun=ADMIN_NOUN,
        )
        thread = await channel.create_thread(
            name=setup_thread_name(target_name),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason="Member opened an agent setup conversation",
        )
        try:
            async with runtime.sessionmaker() as session:
                await create_binding(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    parent_channel_id=str(channel.id),
                    thread_id=str(thread.id),
                    responder_ma_agent_id=str(responder.id),
                    responder_name="Daimon",
                    configuration_target_ma_agent_id=str(ma_target.id) if ma_target else None,
                    configuration_target_name=target_name,
                    creator_account_id=state.account_id,
                )
                await session.commit()
            await thread.send(opener, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            # This is the platform creation boundary. Never leave a failed
            # opener advertised as ready; retain the original exception.
            try:
                await thread.delete(reason="Setup conversation could not be initialized")
            except discord.HTTPException:
                with contextlib.suppress(discord.HTTPException):
                    await thread.edit(name="Setup failed — please open a new conversation")
            with contextlib.suppress(SQLAlchemyError):
                async with runtime.sessionmaker() as session:
                    await update_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        platform="discord",
                        parent_channel_id=str(channel.id),
                        thread_id=str(thread.id),
                        deleted=True,
                    )
                    await session.commit()
            raise
        await interaction.followup.send(
            f"[Open the thread]({thread.jump_url})",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        _log.exception("setup_conversation.permission_denied")
        await interaction.followup.send(
            "I couldn't create or post in the setup thread here. Ask a server admin to give me "
            "Create Public Threads, Send Messages in Threads, and Manage Threads in this channel, "
            "then try again.",
            ephemeral=True,
        )
    except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as error:
        request_id = generate_request_id()
        _log.exception("setup_conversation.failed", request_id=request_id)
        await interaction.followup.send(render_error(error, request_id=request_id), ephemeral=True)

"""`/memory` slash command — the channel agent's shared memory, and your own.

`/memory` and `/memory path:<path>` show the shared memory read-only.
`/memory path:mine`, `path:mine <path>` and `path:forget-mine [code]` act on
the invoking member's own personal memory only (`daimon.core.memory_view`):
it is resolved from their account, and no argument can name anyone else.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection,
so this module does NOT enable annotations futures (same as billing.py).
"""

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.memory_view import (
    ChannelAgent,
    PersonalMemoryReply,
    get_own_personal_memory_store,
    parse_memory_args,
    personal_memory_reply,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import list_memory_entries, read_memory
from daimon.core.rule_views import is_memory_hidden
from daimon.core.scope import ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]

_DISCORD_LIMIT = 1900  # headroom under the 2000-char message cap (split.py convention)
_EMPTY = "No memory to show here."


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


async def _resolve_agent(
    runtime: DiscordRuntime, interaction: BotInteraction
) -> ChannelAgent | None:
    """The agent answering in the invoking channel, and the invoker's account.

    Returns None when the channel has no configured agent or its pin or channel
    isolation hides it from here. Raises UserFacingError when the configured
    agent doesn't exist on the MA side.
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
    async with runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform="discord",
            external_id=str(interaction.user.id),
        )
        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=str(interaction.channel_id),
        )
        config = await resolve_config(session, context=scope, default=runtime.deployment_default)
        policy = await load_access_policy(session, tenant_id=tenant_id)
    if config.agent_name is None:
        return None
    agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=config.agent_name
    )
    if agent is None:
        log.warning("discord.memory.agent_missing", configured_name=config.agent_name)
        raise UserFacingError(
            "The configured agent could not be found. Ask an admin to check the agent setup."
        )
    channel = interaction.channel
    parent = channel.parent_id if isinstance(channel, discord.Thread) else None
    if is_memory_hidden(
        policy,
        agent_names=(config.agent_name, *agent_pin_names(agent.name, agent.metadata)),
        channel_id=str(interaction.channel_id),
        parent_channel_id=str(parent) if parent is not None else None,
    ):
        return None
    return ChannelAgent(
        agent_name=config.agent_name,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        account_id=principal.account_id,
    )


async def _resolve_store(
    runtime: DiscordRuntime, interaction: BotInteraction
) -> tuple[str, str] | None:
    """Resolve (agent_name, shared memory_store_id) for the invoking channel, or None."""
    agent = await _resolve_agent(runtime, interaction)
    if agent is None:
        return None
    async with runtime.sessionmaker() as session:
        store_id = await get_memory_store_id(
            session, tenant_id=agent.tenant_id, agent_id=agent.agent_id
        )
    if store_id is None:
        return None
    return agent.agent_name, store_id


def _personal_text(reply: PersonalMemoryReply) -> str:
    if reply.content is not None:
        return _fenced(f"**`{reply.title}`**", reply.content, _DISCORD_LIMIT)
    parts = [f"**{reply.title}**"]
    if reply.paths:
        parts.append("\n".join(f"- `{p}`" for p in reply.paths))
    if reply.hint:
        parts.append(f"`{reply.hint}`")
    return _truncate("\n\n".join(parts))


def _truncate(text: str) -> str:
    if len(text) <= _DISCORD_LIMIT:
        return text
    return text[: _DISCORD_LIMIT - 20] + "\n… (truncated)"


def _fenced(header: str, content: str, limit: int) -> str:
    """Wrap content in a closed code fence, truncating content to fit limit.

    Truncating the CONTENT before wrapping (rather than truncating the fully
    wrapped string) guarantees the closing ``` fence is always present — a
    naive `_truncate(header + fence + content + fence)` can slice mid-fence
    and leave an unclosed code block that corrupts rendering. Backtick runs
    inside the content get a zero-width space so an embedded ``` can't close
    the wrapping fence early.
    """
    content = content.replace("```", "`​``")
    overhead = len(header) + len("\n```\n\n```")
    budget = limit - overhead
    if len(content) > budget:
        content = content[: budget - 16] + "\n… (truncated)"
    return f"{header}\n```\n{content}\n```"


@app_commands.guild_only()
class MemoryCog(commands.Cog):
    """Read-only view of the channel agent's persistent memory."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="memory",
        description="Show what this channel's agent remembers, or your own memory with it",
    )
    @app_commands.describe(
        path=(
            "A file to show (omit to list all). `mine` for your own memory, "
            "`forget-mine` to clear it"
        )
    )
    @require_registered_guild
    async def memory(self, interaction: BotInteraction, path: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "guild_only ensures guild context"
            request = parse_memory_args(path)
            if request.kind not in ("shared_list", "shared_show"):
                agent = await _resolve_agent(runtime, interaction)
                if agent is None:
                    await interaction.followup.send(_EMPTY, ephemeral=True)
                    return
                reply = await personal_memory_reply(
                    runtime.anthropic,
                    agent_name=agent.agent_name,
                    # Always the invoker's own: nothing in `path` selects an account.
                    store_id=await get_own_personal_memory_store(runtime.sessionmaker, agent),
                    request=request,
                    command="/memory path:",
                    scope=resource_scope(tenant_id=str(agent.tenant_id)),
                )
                await interaction.followup.send(
                    _personal_text(reply),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            resolved = await _resolve_store(runtime, interaction)
            if resolved is None:
                await interaction.followup.send(_EMPTY, ephemeral=True)
                return
            agent_name, store_id = resolved
            scope = resource_scope(
                tenant_id=str(
                    derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
                )
            )

            if path is None:
                paths: list[str] = []
                async for item in list_memory_entries(
                    runtime.anthropic, store_id, path_prefix="/", scope=scope
                ):
                    if item.type == "memory":
                        paths.append(item.path)
                if not paths:
                    await interaction.followup.send(_EMPTY, ephemeral=True)
                    return
                body = "\n".join(f"- `{p}`" for p in sorted(paths))
                await interaction.followup.send(
                    _truncate(f"**{agent_name}'s memory** ({len(paths)} files)\n{body}"),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            mem_id: str | None = None
            async for item in list_memory_entries(
                runtime.anthropic, store_id, path_prefix="/", scope=scope
            ):
                if item.type == "memory" and item.path == path:
                    mem_id = item.id
                    break
            if mem_id is None:
                await interaction.followup.send(
                    f"No memory file called `{path}`.\n\nRun `/memory` to see them all.",
                    ephemeral=True,
                )
                return
            mem = await read_memory(runtime.anthropic, store_id, mem_id, scope=scope)
            content = mem.content or ""
            await interaction.followup.send(
                _fenced(f"**`{path}`**", content, _DISCORD_LIMIT),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            log.warning("memory.handler.failed", rid=rid, error=str(exc))
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemoryCog(bot))

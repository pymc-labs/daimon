"""`/memory` slash command — read-only view of the channel agent's memory.

PEP-563 deferred evaluation breaks discord.py 2.x slash-param introspection,
so this module does NOT enable annotations futures (same as billing.py).
"""

from typing import cast

import anthropic
import structlog
from daimon.adapters.discord.checks import require_registered_guild
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import ScopeContext
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config

import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = structlog.get_logger()
BotInteraction = Interaction[commands.Bot]

_DISCORD_LIMIT = 1900  # headroom under the 2000-char message cap (split.py convention)
_EMPTY = "This agent has no memories yet — it will start remembering as it works."


def _get_runtime(interaction: BotInteraction) -> DiscordRuntime:
    return cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]  # DaimonBot.runtime not on Bot type


async def _resolve_store(
    runtime: DiscordRuntime, interaction: BotInteraction
) -> tuple[str, str] | None:
    """Resolve (agent_name, memory_store_id) for the invoking channel.

    Returns None when the channel has no configured agent or the agent has no
    memory store yet. Raises DaimonError when the configured agent doesn't
    exist on the MA side.
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
    if config.agent_name is None:
        return None
    agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=config.agent_name
    )
    if agent is None:
        raise DaimonError(f"Configured agent **{config.agent_name}** not found.")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    async with runtime.sessionmaker() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_uuid)
    if store_id is None:
        return None
    return config.agent_name, store_id


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
        description="Show what this channel's agent remembers (read-only)",
    )
    @app_commands.describe(path="Show one memory file's content (omit to list all)")
    @require_registered_guild
    async def memory(self, interaction: BotInteraction, path: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rid = generate_request_id()
        try:
            runtime = _get_runtime(interaction)
            assert interaction.guild_id is not None, "guild_only ensures guild context"
            resolved = await _resolve_store(runtime, interaction)
            if resolved is None:
                await interaction.followup.send(_EMPTY, ephemeral=True)
                return
            agent_name, store_id = resolved

            if path is None:
                paths: list[str] = []
                page = await runtime.anthropic.beta.memory_stores.memories.list(
                    store_id, path_prefix="/"
                )
                async for item in page:
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
            page = await runtime.anthropic.beta.memory_stores.memories.list(
                store_id, path_prefix="/"
            )
            async for item in page:
                if item.type == "memory" and item.path == path:
                    mem_id = item.id
                    break
            if mem_id is None:
                await interaction.followup.send(
                    f"No memory at `{path}`. Run `/memory` to list paths.",
                    ephemeral=True,
                )
                return
            mem = await runtime.anthropic.beta.memory_stores.memories.retrieve(
                mem_id, memory_store_id=store_id, view="full"
            )
            content = mem.content or ""
            await interaction.followup.send(
                _fenced(f"**`{path}`**", content, _DISCORD_LIMIT),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as exc:
            log.warning("memory.handler.failed", rid=rid, error=str(exc))
            await interaction.followup.send(
                render_error(exc, request_id=rid),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemoryCog(bot))

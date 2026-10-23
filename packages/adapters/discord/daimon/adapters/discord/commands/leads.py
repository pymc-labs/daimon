"""Private native lead queue; every record is read under `authorize_lead_read`."""

from typing import cast
from uuid import UUID

from daimon.adapters.discord.checks import require_registered_guild, resolve_tenant_for_interaction
from daimon.adapters.discord.lead_access import discord_lead_reads
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.lead_intake_access import read_target
from daimon.core.stores.lead_intake import IntakeStore

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


@app_commands.guild_only()
class LeadsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot

    @app_commands.command(name="leads", description="Private lead queue and review status")
    @require_registered_guild
    async def leads(self, interaction: BotInteraction, intake_id: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        tenant = await resolve_tenant_for_interaction(interaction.client, interaction)
        fernet = runtime.turn_deps.fernet
        resolver = runtime.turn_deps.membership_resolver
        if tenant is None or fernet is None or resolver is None or interaction.guild is None:
            await interaction.followup.send("Lead intake is unavailable.", ephemeral=True)
            return
        guild = interaction.guild
        try:
            store = IntakeStore(runtime.sessionmaker, fernet, tenant)
            origin = interaction.channel
            reads = await discord_lead_reads(
                sessionmaker=runtime.sessionmaker,
                store=store,
                resolver=resolver,
                tenant_id=tenant,
                guild=guild,
                user_id=str(interaction.user.id),
                origin=origin
                if isinstance(origin, discord.abc.GuildChannel | discord.Thread)
                else None,
            )
            config = reads.config

            # Private data never falls back to the invocation channel.
            if intake_id:
                record = await store.get(UUID(intake_id))
                await reads.check(read_target(record))
                title = record.submission.thread_title if record.submission else "Expired lead"
                content = f"{record.id} · {record.status}\n{title}\n{record.error_code or ''}"
                if record.progress.discord_thread_id:
                    content += f"\nhttps://discord.com/channels/{config.guild_id}/{record.progress.discord_thread_id}"
                content += (
                    "\nAsk Daimon to review this intake; approve exact CRM selections "
                    "in the conversation."
                )
            else:
                # The queue lists the current sales channel's records; each is
                # still read under the destination it was captured for.
                records = await store.list(
                    limit=20,
                    pending_only=True,
                    destination_channel_id=config.channel_id,
                    authorize_read=reads.check,
                )
                content = "\n".join(f"{r.id} · {r.status} · {r.error_code or ''}" for r in records)
                content = content or "No pending leads."
            await interaction.followup.send(
                content[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            await interaction.followup.send(
                "Lead intake is unavailable or access was refused.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

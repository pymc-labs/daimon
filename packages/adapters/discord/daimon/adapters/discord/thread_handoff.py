"""The Hand over button on a thread whose channel now answers with another agent.

When a channel's agent changes, a thread already running under the previous
agent can't take a turn: its session belongs to the previous agent, so every
mention is answered with a notice instead (`SessionAgentMismatch`). The notice
carries this button. A click hands the thread to the agent named in the
custom_id, for the person who clicked, through the same locked decision the
`hand_off_task` tool uses (`daimon.core.thread_handoff`). The next message in
the thread then runs as that agent, with the work carried across.

The custom_id names the agent only; it is a request, not a grant. The click is
decided for the clicker's live role and grants in the thread they clicked in,
so a forwarded or replayed custom_id can only ask for what the clicker could
ask for anyway.

Template-disjointness: `tho:` must not overlap `mfb:`, `ztc:`, `sup:` or the
wizard's `wz:`; discord.py fires every registered template that fullmatches.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Final, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import channel_admin_caller
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.thread_handoff import switch_thread_on_request

import discord
from discord.ext import commands

_log = structlog.get_logger()

CUSTOM_ID_TEMPLATE: Final[str] = r"tho:(?P<agent_id>[A-Za-z0-9_-]{1,90})"
_FAILED = "Something went wrong handing this conversation over -- please try again."


def build_custom_id(agent_id: str) -> str:
    return f"tho:{agent_id}"


class HandOverButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent button reconstructed from its custom_id; registered in `setup_hook`."""

    def __init__(self, *, agent_id: str, agent_name: str | None = None) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=f"Switch to {agent_name}"[:80] if agent_name else "Switch",
            custom_id=build_custom_id(agent_id),
        )
        super().__init__(button)
        self.agent_id = agent_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(agent_id=match["agent_id"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await self._hand_over(interaction)
        except Exception as err:
            # Dynamic-item dispatch is an adapter boundary: discord.py swallows
            # anything raised here, so log it and tell the clicker.
            _log.exception("thread_handoff.callback_failed", err_type=type(err).__name__)
            if interaction.response.is_done():
                await interaction.followup.send(_FAILED, ephemeral=True)
            else:
                await interaction.response.send_message(_FAILED, ephemeral=True)

    async def _hand_over(self, interaction: discord.Interaction[commands.Bot]) -> None:
        thread = interaction.channel
        if interaction.guild_id is None or not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "This button only works in a conversation thread.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = cast(DaimonBot, interaction.client)
        runtime = bot.runtime
        guild_id = str(interaction.guild_id)
        outcome = await switch_thread_on_request(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=guild_id),
            platform="discord",
            parent_channel_id=str(thread.parent_id),
            thread_id=str(thread.id),
            ma_agent_id=self.agent_id,
            caller=channel_admin_caller(interaction.user),
            default=runtime.turn_deps.deployment_default,
            channel=f"<#{thread.parent_id}>",
            now=datetime.now(UTC),
        )
        _log.info(
            "thread_handoff.clicked",
            platform="discord",
            switched=outcome.switched,
            agent_id=self.agent_id,
        )
        if not outcome.switched:
            await interaction.followup.send(outcome.text, ephemeral=True)
            return
        await thread.send(
            f"{interaction.user.mention} handed this conversation over. {outcome.text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if interaction.message is not None:
            # The notice's button has done its job.
            transport = DiscordPostTransport(
                bot,
                thread,
                name=interaction.message.author.name,
                avatar_url=None,
                builtin=False,
            )
            await transport.edit(interaction.message, view=None)
        # The public post above is the answer; drop the private "thinking".
        await interaction.delete_original_response()


def hand_over_view(*, agent_id: str, agent_name: str) -> discord.ui.View:
    """A one-button view for the responder-changed notice."""
    view = discord.ui.View(timeout=None)
    view.add_item(HandOverButton(agent_id=agent_id, agent_name=agent_name))
    return view

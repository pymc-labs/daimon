"""Channel isolation: keep this channel's own agents inside it.

Reached from Who answers where, by server admins only. Isolates the channel the
panel was opened in (a thread's parent), with a copy of the agent answering
there when it has no agent of its own, or ends its isolation. Every click
re-checks Manage Server live; the rules live in `daimon.core.channel_isolation_setup`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import is_isolated
from daimon.core.channel_isolation_setup import (
    END_ISOLATION_WARNING,
    set_channel_isolation,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import load_access_policy

import discord

log = structlog.get_logger()

ISOLATION_LABEL: Final = "Isolation"
ISOLATE_LABEL: Final = "Isolate"
ISOLATE_COPY_LABEL: Final = "Isolate with a copy"
END_LABEL: Final = "End isolation"
BACK_LABEL: Final = "◀ Back"
EXPLAINER: Final = (
    "-# Isolating seals this channel and pins its own agent to it: the agent answers only "
    "here and is hidden everywhere else, and inside only the channel's own agents are "
    "visible. Its messages are readable only from inside it. Isolating needs an agent that "
    "answers only here. **Isolate with a copy** makes one from the agent answering now when "
    "there is none."
)

_Callback = Callable[[discord.Interaction], Awaitable[None]]


def build_isolation_container(
    *, channel_name: str | None, isolated: bool, notice: str | None
) -> discord.ui.Container[discord.ui.LayoutView]:
    """The isolation card. Pure — no I/O."""
    label = f"#{channel_name}" if channel_name else "This channel"
    status = f"{label} is isolated." if isolated else f"{label} is not isolated."
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(ISOLATION_LABEL))
    container.add_item(discord.ui.TextDisplay(status if notice is None else f"{status}\n{notice}"))
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(EXPLAINER))
    if isolated:
        container.add_item(discord.ui.TextDisplay(f"-# Ending isolation: {END_ISOLATION_WARNING}"))
    return container


def _tenant_id(state: PanelState) -> uuid.UUID:
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


async def load_is_isolated(runtime: DiscordRuntime, *, state: PanelState) -> bool:
    async with runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=_tenant_id(state))
    return is_isolated(policy, channel_id=str(state.channel_id))


class IsolationView(PanelViewBase):
    """This channel's isolation, with the actions that change it."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        isolated: bool,
        notice: str | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        container = build_isolation_container(
            channel_name=state.channel_name, isolated=isolated, notice=notice
        )
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        buttons: list[tuple[str, discord.ButtonStyle, _Callback]] = [
            (BACK_LABEL, discord.ButtonStyle.secondary, self._on_back)
        ]
        if isolated:
            buttons.append((END_LABEL, discord.ButtonStyle.danger, self._on_end))
        else:
            buttons.append((ISOLATE_LABEL, discord.ButtonStyle.primary, self._on_isolate))
            buttons.append((ISOLATE_COPY_LABEL, discord.ButtonStyle.secondary, self._on_copy))
        for label, style, callback in buttons:
            button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=label, style=style
            )
            button.callback = callback  # type: ignore[method-assign]  # per-instance callback
            row.add_item(button)
        row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(row)
        self.add_item(container)

    async def _change(
        self, interaction: discord.Interaction, *, isolated: bool, copy: bool = False
    ) -> None:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.defer()
        state, tenant_id = self.state, _tenant_id(self.state)
        public_url = self.runtime.settings.mcp.public_url
        try:
            change = await set_channel_isolation(
                self.runtime.anthropic,
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(state.channel_id),
                isolated=isolated,
                default=self.runtime.deployment_default,
                actor_account_id=state.account_id,
                channel_label=state.channel_name,
                fork=copy,
                public_url=str(public_url) if public_url is not None else None,
            )
        except DaimonError as exc:  # a refusal, or a copy that can't be made
            notice = f"-# {exc} Nothing was changed."
        else:
            log.info(
                "agent_setup.isolation.saved",
                isolated=change.isolated,
                forked=bool(change.forked_from),
            )
            if change.forked_from is not None:
                copied = f"**{change.agent_name}**, a copy of **{change.forked_from}**"
                dropped = change.dropped_skills_note
                notice = f"-# {copied}, now answers only here." + (f" {dropped}" if dropped else "")
            elif change.isolated:
                notice = f"-# **{change.agent_name}** answers only here."
            else:
                notice = f"-# {END_ISOLATION_WARNING}"
        await self.swap_to(
            interaction,
            IsolationView(
                state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                isolated=await load_is_isolated(self.runtime, state=state),
                notice=notice,
            ),
        )

    async def _on_isolate(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, isolated=True)

    async def _on_copy(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, isolated=True, copy=True)

    async def _on_end(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, isolated=False)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        # Lazy import: the routing screen opens this one.
        from daimon.adapters.discord.agent_setup.routing_view import build_routing_view

        await interaction.response.defer()
        routing = await build_routing_view(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
        )
        await self.swap_to(interaction, routing)

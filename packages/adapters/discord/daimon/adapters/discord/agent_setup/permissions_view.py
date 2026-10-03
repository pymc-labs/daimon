"""Permissions: who can read this channel and who can post there.

Reached from Who answers where, by server admins only. Shows the rule of the
channel the panel was opened in (a thread's parent) and the agents kept to
it, and changes either side, keeps it to a copy of the agent answering there,
or releases its agents. Every change re-checks Manage Server live; the rules
live in `daimon.core.channel_rules`.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import structlog
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import ChannelReaders, ChannelWriters
from daimon.core.authz import build_subject
from daimon.core.channel_rules import (
    READERS_LABELS,
    WRITERS_LABELS,
    ChannelRuleRefused,
    ChannelRuleStatus,
    as_readers,
    as_writers,
    channel_rule_status,
    set_channel_rule,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.access_policy import load_access_policy

import discord

log = structlog.get_logger()

PERMISSIONS_LABEL: Final = "Permissions"
COPY_LABEL: Final = "Keep it to a copy"
RELEASE_LABEL: Final = "Release its agents"
BACK_LABEL: Final = "◀ Back"
EXPLAINER: Final = (
    "-# **Who can read it**: anyone; only this channel, so only turns here read its "
    "messages and conversations; or only its own agents, so also only agents kept here "
    "run and show here, and nowhere else. Its default agent becomes one; "
    f"**{COPY_LABEL}** makes one from the agent answering now.\n"
    "-# **Who can post**: anyone; only its own agents; or nobody, daimon included."
)
RELEASE_NOTE: Final = (
    f"-# **{RELEASE_LABEL}** drops the rules keeping its agents here, so they can run "
    "elsewhere, bringing what they remembered here."
)

_Callback = Callable[[discord.Interaction], Awaitable[None]]


def build_permissions_container(
    *, channel_name: str | None, status: ChannelRuleStatus, notice: str | None
) -> discord.ui.Container[discord.ui.LayoutView]:
    """The permissions card. Pure — no I/O."""
    label = f"#{channel_name}" if channel_name else "This channel"
    line = f"{label}\n-# {status.line}"
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(PERMISSIONS_LABEL))
    container.add_item(discord.ui.TextDisplay(line if notice is None else f"{line}\n{notice}"))
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(EXPLAINER))
    if status.agents and status.rule.readers != "own":
        container.add_item(discord.ui.TextDisplay(RELEASE_NOTE))
    return container


def _select[T: str](
    placeholder: str, labels: Mapping[T, str], current: T
) -> discord.ui.Select[discord.ui.LayoutView]:
    return discord.ui.Select(
        placeholder=placeholder,
        options=[
            discord.SelectOption(
                label=f"{placeholder}: {label}", value=value, default=value == current
            )
            for value, label in labels.items()
        ],
    )


def _tenant_id(state: PanelState) -> uuid.UUID:
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


async def load_rule_status(runtime: DiscordRuntime, *, state: PanelState) -> ChannelRuleStatus:
    async with runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=_tenant_id(state))
    return channel_rule_status(policy, str(state.channel_id))


class PermissionsView(PanelViewBase):
    """This channel's rule, with the controls that change it."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        status: ChannelRuleStatus,
        notice: str | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        container = build_permissions_container(
            channel_name=state.channel_name, status=status, notice=notice
        )
        self.readers_select = _select("Who can read it", READERS_LABELS, status.rule.readers)
        self.readers_select.callback = self._on_readers  # type: ignore[method-assign]  # per-instance callback
        self.writers_select = _select("Who can post", WRITERS_LABELS, status.rule.writers)
        self.writers_select.callback = self._on_writers  # type: ignore[method-assign]  # per-instance callback
        for select in (self.readers_select, self.writers_select):
            select_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            select_row.add_item(select)
            container.add_item(select_row)
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        buttons: list[tuple[str, discord.ButtonStyle, _Callback]] = [
            (BACK_LABEL, discord.ButtonStyle.secondary, self._on_back)
        ]
        if status.rule.readers != "own":
            buttons.append((COPY_LABEL, discord.ButtonStyle.secondary, self._on_copy))
            if status.agents:
                buttons.append((RELEASE_LABEL, discord.ButtonStyle.danger, self._on_release))
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
        self,
        interaction: discord.Interaction,
        *,
        readers: ChannelReaders | None = None,
        writers: ChannelWriters | None = None,
        copy: bool = False,
        release: bool = False,
    ) -> None:
        state, tenant_id = self.state, _tenant_id(self.state)
        audit = functools.partial(
            record_panel_write,
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=str(interaction.user.id),
            op="channel_rule",
        )
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            await audit(outcome="denied", reason="needs_admin")
            return
        await interaction.response.defer()
        public_url = self.runtime.settings.mcp.public_url
        try:
            change = await set_channel_rule(
                self.runtime.anthropic,
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(state.channel_id),
                readers=readers,
                writers=writers,
                # `refuse_if_not_admin` above checked the live role.
                subject=build_subject(is_admin=True, platform_user_id=str(interaction.user.id)),
                default=self.runtime.deployment_default,
                actor_account_id=state.account_id,
                copy=copy,
                channel_label=state.channel_name,
                public_url=str(public_url) if public_url is not None else None,
                release_agents=release,
            )
        except ChannelRuleRefused as exc:
            await audit(outcome="denied", reason=f"rule:{exc.reason}")
            notice = f"-# {exc} Nothing was changed."
        except DaimonError as exc:  # a copy that can't be made
            await audit(outcome="error", reason="failed")
            notice = f"-# {exc} Nothing was changed."
        else:
            await audit(outcome="allowed", reason="completed")
            log.info(
                "agent_setup.channel_rule.saved",
                readers=change.rule.readers,
                writers=change.rule.writers,
                copied=bool(change.copied_from),
            )
            notice = "\n".join(f"-# {note}" for note in change.notes)
        await self.swap_to(
            interaction,
            PermissionsView(
                state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                status=await load_rule_status(self.runtime, state=state),
                notice=notice,
            ),
        )

    async def _on_readers(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, readers=as_readers(self.readers_select.values[0]))

    async def _on_writers(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, writers=as_writers(self.writers_select.values[0]))

    async def _on_copy(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, readers="own", copy=True)

    async def _on_release(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, release=True)

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

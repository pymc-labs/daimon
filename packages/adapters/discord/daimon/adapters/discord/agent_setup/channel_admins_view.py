"""Channel admins: who runs each channel on top of the server admins.

Reached from Who answers where, by server admins only. Lists every channel that
has admins and edits this channel's in a modal with a role and a member select.
Every click and submit re-checks Manage Server live; the rendered state is a hint.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import structlog
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_admins import (
    MAX_CHANNEL_ADMIN_IDS,
    InvalidChannelAdminIds,
    fit_lines,
    fold_mentions,
    normalize_channel_admin_ids,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    list_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.domain import ChannelAdminsRow

import discord

log = structlog.get_logger()

CHANNEL_ADMINS_LABEL: Final = "Channel admins"
EDIT_LABEL: Final = "Edit this channel"
BACK_LABEL: Final = "◀ Back"
MAX_LISTED: Final = 15
LISTING_MAX_CHARS: Final = 3_000
"""Room for the listing inside Discord's 4000-character cap on a message's text."""
EXPLAINER: Final = (
    "-# Server admins run every channel. A channel's admins may change agents that answer "
    "only in channels they run, and pick those channels' default agent. Built-in agents and "
    "the server default stay with server admins."
)


def grant_line(row: ChannelAdminsRow) -> str:
    who = [f"<@&{role_id}>" for role_id in row.role_ids] + [f"<@{uid}>" for uid in row.user_ids]
    return f"<#{row.channel_id}> → {fold_mentions(who)}"


def build_channel_admins_container(
    grants: Sequence[ChannelAdminsRow],
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Fold every channel's admins into the panel card. Pure — no I/O."""
    lines = (grant_line(row) for row in grants[:MAX_LISTED])
    shown = fit_lines(lines, max_chars=LISTING_MAX_CHARS)
    if len(shown) < len(grants):
        shown.append(f"-# and {len(grants) - len(shown)} more")
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(CHANNEL_ADMINS_LABEL))
    container.add_item(
        discord.ui.TextDisplay("\n".join(shown) or "-# no channel has its own admins yet")
    )
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(EXPLAINER))
    return container


async def load_grants(runtime: DiscordRuntime, *, state: PanelState) -> list[ChannelAdminsRow]:
    async with runtime.sessionmaker() as session:
        return await list_channel_admins(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id)),
            platform="discord",
        )


class ChannelAdminsView(PanelViewBase):
    """Every channel's admins, with Edit for the channel the panel was opened in."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        grants: Sequence[ChannelAdminsRow],
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.grants = list(grants)
        container = build_channel_admins_container(self.grants)
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        row.add_item(back)
        if state.channel_id:
            edit: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=EDIT_LABEL, style=discord.ButtonStyle.primary
            )
            edit.callback = self._on_edit  # type: ignore[method-assign]  # per-instance callback
            row.add_item(edit)
        row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(row)
        self.add_item(container)

    def current_grant(self) -> ChannelAdminsRow | None:
        channel_id = str(self.state.channel_id)
        return next((row for row in self.grants if row.channel_id == channel_id), None)

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

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.send_modal(ChannelAdminsModal(self))


class ChannelAdminsModal(discord.ui.Modal):
    """Pick the roles and members who run this channel; empty both to clear it."""

    def __init__(self, view: ChannelAdminsView) -> None:
        name = view.state.channel_name or "this channel"
        super().__init__(title=f"Admins of #{name}"[:45])
        self._view = view
        grant = view.current_grant()
        self.roles: discord.ui.RoleSelect[discord.ui.View] = discord.ui.RoleSelect(
            min_values=0,
            max_values=MAX_CHANNEL_ADMIN_IDS,
            required=False,
            default_values=[discord.Object(id=int(r)) for r in (grant.role_ids if grant else ())],
        )
        self.users: discord.ui.UserSelect[discord.ui.View] = discord.ui.UserSelect(
            min_values=0,
            max_values=MAX_CHANNEL_ADMIN_IDS,
            required=False,
            default_values=[discord.Object(id=int(u)) for u in (grant.user_ids if grant else ())],
        )
        self.add_item(discord.ui.Label(text="Roles", component=self.roles))
        self.add_item(discord.ui.Label(text="Members", component=self.users))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        view, state = self._view, self._view.state
        role_ids = [str(role.id) for role in self.roles.values if not role.is_default()]
        user_ids = [str(user.id) for user in self.users.values if not user.bot]
        try:
            channel_id, roles, users = normalize_channel_admin_ids(
                "discord", channel_id=str(state.channel_id), role_ids=role_ids, user_ids=user_ids
            )
        except InvalidChannelAdminIds as exc:
            await interaction.response.send_message(f"{exc}. Nothing changed.", ephemeral=True)
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
        async with view.runtime.sessionmaker.begin() as session:
            if roles or users:
                await set_channel_admins(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    channel_id=channel_id,
                    role_ids=roles,
                    user_ids=users,
                    actor_account_id=state.account_id,
                )
            else:
                await delete_channel_admins(
                    session, tenant_id=tenant_id, platform="discord", channel_id=channel_id
                )
        log.info("agent_setup.channel_admins.saved", roles=len(roles), users=len(users))
        rebuilt = ChannelAdminsView(
            state,
            runtime=view.runtime,
            allowed_user_id=view.allowed_user_id,
            grants=await load_grants(view.runtime, state=state),
        )
        await view.swap_to(interaction, rebuilt)

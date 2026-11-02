"""Tenant-wide admin grants. Every mutation re-attests platform authority on submit."""

from __future__ import annotations

from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_platform_admin
from daimon.adapters.discord.layout import header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.daimon_admins import AdminGrant, change_grant, list_admins

import discord


class AdminGrantModal(discord.ui.Modal, title="Daimon admins"):
    subject: discord.ui.TextInput[AdminGrantModal] = discord.ui.TextInput(
        label="User or role ID", max_length=21
    )
    kind: discord.ui.TextInput[AdminGrantModal] = discord.ui.TextInput(
        label="user or role", default="user", max_length=4
    )
    action: discord.ui.TextInput[AdminGrantModal] = discord.ui.TextInput(
        label="grant or revoke", default="grant", max_length=6
    )

    def __init__(self, view: AdminsView) -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.view.allowed_user_id
            or interaction.guild_id != self.view.state.guild_id
            or not is_platform_admin(interaction)
        ):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only platform admins may grant or revoke.", ephemeral=True
            )
            return
        kind, action, subject = self.kind.value, self.action.value, self.subject.value
        if (
            kind not in {"user", "role"}
            or action not in {"grant", "revoke"}
            or not subject.isascii()
            or not subject.isdecimal()
            or not 15 <= len(subject) <= 21
        ):
            await interaction.response.send_message(
                "Enter a Discord ID, user or role, and grant or revoke.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        async with self.view.runtime.sessionmaker.begin() as session:
            await change_grant(
                session,
                tenant_id=derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                ),
                platform="discord",
                subject_kind="role" if kind == "role" else "user",
                subject_id=subject,
                actor_user_id=str(interaction.user.id),
                is_platform_admin=True,
                revoke=action == "revoke",
            )
        await interaction.followup.send(
            "Daimon admin grant updated. Platform admins retain access.", ephemeral=True
        )
        await self.view.refresh(interaction)


class AdminsView(PanelViewBase):
    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        grants: list[AdminGrant],
        platform_admin: bool,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        container.add_item(header("Daimon admins"))
        lines = [
            "Platform admins always have access. Only platform admins may grant or revoke. "
            "Billing and caps remain platform-only. "
            "Granted admins can change what agents do, including agents platform admins use."
        ]
        lines.extend(
            f"{g.subject_kind}: {g.subject_id}, added by {g.granted_by or 'erased'} "
            f"on {g.granted_at:%Y-%m-%d}"
            for g in grants[:25]
        )
        if len(grants) > 25:
            lines.append("More grants: use `daimon admins list`.")
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        if platform_admin:
            button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label="Grant / revoke"
            )
            button.callback = self._edit  # type: ignore[method-assign]
            row.add_item(button)
        back: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(label="Back")
        back.callback = self._back  # type: ignore[method-assign]
        row.add_item(back)
        container.add_item(row)
        self.add_item(container)

    async def _edit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.state.guild_id or not is_platform_admin(interaction):  # pyright: ignore[reportArgumentType]
            await interaction.response.send_message(
                "Only platform admins may grant or revoke.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AdminGrantModal(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        await self.swap_to(
            interaction,
            await build_admins_view(interaction, self.state, self.runtime, self.allowed_user_id),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.permissions_view import (
            PermissionsView,
            load_rule_status,
        )

        await interaction.response.defer()
        await self.swap_to(
            interaction,
            PermissionsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                status=await load_rule_status(self.runtime, state=self.state),
            ),
        )


async def build_admins_view(
    interaction: discord.Interaction,
    state: PanelState,
    runtime: DiscordRuntime,
    allowed_user_id: int,
) -> AdminsView:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    async with runtime.sessionmaker() as session:
        grants = await list_admins(session, tenant_id=tenant_id)
    return AdminsView(
        state,
        runtime=runtime,
        allowed_user_id=allowed_user_id,
        grants=grants,
        platform_admin=is_platform_admin(interaction),
    )  # pyright: ignore[reportArgumentType]

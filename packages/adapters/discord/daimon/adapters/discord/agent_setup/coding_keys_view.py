"""Coding keys: the live keys made from one agent's "Use from your coding tools" button.

An admin sees and revokes every key of the agent; the agent's owner sees and
revokes only their own; anyone else is refused the screen. Both facts are read at
click time (never from the panel's open-time snapshot), on open and again on each
Revoke. The list shows who made a key, when it was made and ends, and whether it
runs in one channel. It never shows the token, which was shown once at mint.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import structlog
from daimon.adapters.discord.agent_setup.budget import LAYOUT_TEXT_BUDGET
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_ownership import owns_agent
from daimon.core.coding_keys import (
    CodingKeyAccess,
    CodingKeyEntry,
    coding_key_access,
    list_coding_keys,
    revoke_coding_key,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.roster import RosterAgent, paginate

import discord

log = structlog.get_logger()

CODING_KEYS_LABEL = "🔑 Coding keys"
CODING_KEYS_REFUSAL = "Only an admin or this agent's owner can see its coding keys."
#: Keys per page: each costs a Section and its Revoke button, 3 of 40 components.
CODING_KEYS_PAGE_SIZE = 8


def key_line(entry: CodingKeyEntry) -> str:
    """One key as a line: who made it, when it was made and ends, where it runs."""
    maker = f"{entry.creator} (you)" if entry.mine else entry.creator
    made = int(entry.created_at.timestamp())
    ends = f"ends <t:{int(entry.expires_at.timestamp())}:d>" if entry.expires_at else "no end"
    where = "one channel only" if entry.bound else "any channel"
    return f"**{maker}** made it <t:{made}:d>, {ends}, {where}. Key {str(entry.jti)[:8]}"


def keys_text(agent_name: str, entries: list[CodingKeyEntry], *, access: CodingKeyAccess) -> str:
    whose = "Every" if access == "admin" else "Your"
    lines = [
        f"## Coding keys for {agent_name}",
        f"{whose} live key made from Use from your coding tools. Revoke one to stop it.",
    ]
    if not entries:
        lines.append("No live keys.")
    return "\n".join(lines)[:LAYOUT_TEXT_BUDGET]


class CodingKeysView(PanelViewBase):
    """The agent's coding keys the viewer may see, each with a Revoke button."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        agent: RosterAgent,
        access: CodingKeyAccess,
        entries: list[CodingKeyEntry],
        page: int = 0,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.agent = agent
        self.access: CodingKeyAccess = access
        self.entries = entries
        self.page = paginate(entries, page=page, page_size=CODING_KEYS_PAGE_SIZE)
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
            discord.ui.TextDisplay(keys_text(agent.name, entries, access=access))
        )
        for entry in self.page.items:
            revoke: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label="Revoke", style=discord.ButtonStyle.danger
            )
            revoke.callback = functools.partial(self._on_revoke, jti=entry.jti)  # type: ignore[method-assign]  # per-instance callback
            container.add_item(
                discord.ui.Section(discord.ui.TextDisplay(key_line(entry)), accessory=revoke)
            )
        pager = self.page_row(self.page, on_previous=self._on_previous, on_next=self._on_next)
        if pager is not None:
            container.add_item(pager)  # pyright: ignore[reportArgumentType]  # ActionRow[Self] is the same runtime item
        nav: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        nav.add_item(back)
        nav.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(nav)
        self.add_item(container)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, page=self.page.page - 1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, page=self.page.page + 1)

    async def _show(self, interaction: discord.Interaction, *, page: int) -> None:
        """Re-read the list live and show `page` of it; a viewer who lost access is turned away."""
        await interaction.response.defer()
        view = await load_coding_keys_view(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
            page=page,
        )
        if view is None:
            await interaction.followup.send(CODING_KEYS_REFUSAL, ephemeral=True)
            return
        await self.swap_to(interaction, view)

    async def _on_revoke(self, interaction: discord.Interaction, *, jti: uuid.UUID) -> None:
        await interaction.response.defer()
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        access = await live_coding_key_access(
            interaction, runtime=self.runtime, state=self.state, agent=self.agent
        )
        if access is None or interaction.guild_id != self.state.guild_id:
            await _audit(self.runtime, interaction, tenant_id, jti, "denied", "not_allowed")
            await interaction.followup.send(CODING_KEYS_REFUSAL, ephemeral=True)
            return
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)
        async with self.runtime.sessionmaker.begin() as session:
            revoked = await revoke_coding_key(
                session,
                jti=jti,
                tenant_id=tenant_id,
                agent_id=agent_id,
                access=access,
                viewer_account_id=self.state.account_id,
                now=dt.datetime.now(dt.UTC),
            )
        if revoked:
            await _audit(self.runtime, interaction, tenant_id, jti, "allowed", "completed")
            log.info("agent_setup.coding_keys.revoked", jti=str(jti))
        else:
            await _audit(self.runtime, interaction, tenant_id, jti, "error", "not_revocable")
            await interaction.followup.send("That key was already revoked.", ephemeral=True)
        await self._show(interaction, page=self.page.page)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.details_view import DetailsView

        await self.swap_to(
            interaction,
            DetailsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
            ),
        )


async def live_coding_key_access(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    agent: RosterAgent,
) -> CodingKeyAccess | None:
    """What the clicker may do with this agent's coding keys, read now."""
    admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
    owned = False
    if not admin:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
        async with runtime.sessionmaker() as session:
            owned = await owns_agent(
                session,
                tenant_id=tenant_id,
                ma_agent_id=agent.ma_agent_id,
                account_id=state.account_id,
            )
    return coding_key_access(is_admin=admin, owns_agent=owned)


async def load_coding_keys_view(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    allowed_user_id: int,
    agent: RosterAgent,
    page: int = 0,
) -> CodingKeysView | None:
    """The keys screen for what the clicker may see right now, or None to turn them away."""
    if interaction.guild_id != state.guild_id:
        return None
    access = await live_coding_key_access(interaction, runtime=runtime, state=state, agent=agent)
    if access is None:
        return None
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
    async with runtime.sessionmaker() as session:
        entries = await list_coding_keys(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            access=access,
            viewer_account_id=state.account_id,
            platform="discord",
            now=dt.datetime.now(dt.UTC),
        )
    return CodingKeysView(
        state,
        runtime=runtime,
        allowed_user_id=allowed_user_id,
        agent=agent,
        access=access,
        entries=entries,
        page=page,
    )


async def _audit(
    runtime: DiscordRuntime,
    interaction: discord.Interaction,
    tenant_id: uuid.UUID,
    jti: uuid.UUID,
    outcome: PanelOutcome,
    reason: str,
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id=str(interaction.user.id),
        op="coding_token_revoke",
        outcome=outcome,
        reason=reason,
        token_kind="agent",
        token_jti=jti,
    )

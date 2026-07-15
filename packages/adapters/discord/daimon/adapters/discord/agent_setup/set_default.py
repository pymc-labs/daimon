"""SetDefaultView + build_set_default_container + open_set_default launcher.

C9 V2 cascade panel: airy routing blocks, one action select, ChannelSelect row.
Migrated from classic discord.ui.View to LayoutView (plan 70-07).
"""

from __future__ import annotations

import dataclasses
import uuid

import structlog
from daimon.adapters.discord import layout
from daimon.adapters.discord.agent_setup.scope_default import (
    do_propagate,
    do_unpropagate,
    list_guild_propagations,
    resolve_account_display,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    TenantConfigRow,
    TenantScopeRef,
    _pick_agent,  # pyright: ignore[reportPrivateUsage]  # canonical cascade winner; adapter renders the result, never re-derives precedence.
)
from sqlalchemy.ext.asyncio import AsyncSession

import discord

log = structlog.get_logger()

# Scope refs are built before the per-interaction tenant is known; _do_set stamps
# the real tenant inside the session (Pitfall 4 / D-06).
_PLACEHOLDER_TENANT = uuid.UUID(int=0)


@dataclasses.dataclass(frozen=True)
class ScopeBlock:
    """One resolved routing block: scope label, agent name, and audit line.

    Pre-resolved strings passed to the pure builder. No I/O in the builder.
    """

    scope_label: str
    agent_name: str
    audit_line: str


def build_set_default_container(
    blocks: list[ScopeBlock],
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Pure C9 container builder. Takes pre-resolved ScopeBlock list; no I/O.

    Layout: header, hairline, then per-block a TextDisplay with three lines
    (bold scope label / backtick agent name / dim audit line), with air_gap
    separators between blocks.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(layout.header("⚙️ Default agent"))
    container.add_item(layout.hairline())
    for i, block in enumerate(blocks):
        if i > 0:
            container.add_item(layout.air_gap())
        block_text = f"**{block.scope_label}**\n⚙️ `{block.agent_name}`\n-# {block.audit_line}"
        container.add_item(discord.ui.TextDisplay(block_text))
    return container


def _channel_scope_label(channel_id: int, channel_name: str | None) -> str:
    if channel_name:
        return f"#{channel_name}"
    return f"#{channel_id}"


async def _resolve_row_audit(
    session: AsyncSession,
    row: ChannelConfigRow | TenantConfigRow,
) -> str:
    """Build 'set by {handle} · {date}' from a config row, resolving the Discord handle."""
    date_str = row.agent_name_set_at.strftime("%Y-%m-%d") if row.agent_name_set_at else None
    handle: str | None = None
    if row.agent_name_set_by_account_id is not None:
        handle = await resolve_account_display(session, account_id=row.agent_name_set_by_account_id)
    if handle and date_str:
        return f"set by {handle} · {date_str}"
    elif handle:
        return f"set by {handle}"
    elif date_str:
        return f"set · {date_str}"
    return "set (no audit)"


async def _build_scope_blocks(
    state: PanelState,
    interaction: discord.Interaction,  # type: ignore[type-arg]  # discord.Interaction generic arg requires commands.Bot; only used for guild.get_channel cache lookup
    session: AsyncSession | None = None,
) -> list[ScopeBlock]:
    """Shell assembly: build the ordered ScopeBlock list from state snapshot.

    When session is provided, audit lines include resolved Discord handles.
    Order: current channel override → other channel overrides → server default
    → everywhere-else fallback.
    Scopes without an agent produce no block (no (unset) rows).
    """
    tenant_row, ch_rows = state.cascade_view
    current_channel = next((c for c in ch_rows if c.channel_id == str(state.channel_id)), None)
    blocks: list[ScopeBlock] = []

    # 1. Current channel override (when set)
    if current_channel is not None and current_channel.agent_name:
        label = _channel_scope_label(state.channel_id, state.channel_name)
        if session is not None:
            audit = await _resolve_row_audit(session, current_channel)
        else:
            audit = "set"
        blocks.append(
            ScopeBlock(scope_label=label, agent_name=current_channel.agent_name, audit_line=audit)
        )

    # 2. Other channel overrides (when present)
    for ch_row in ch_rows:
        if ch_row.channel_id == str(state.channel_id):
            continue
        if not ch_row.agent_name:
            continue
        guild = interaction.guild  # type: ignore[union-attr]
        ch_channel_id = ch_row.channel_id or ""
        if guild is not None and ch_channel_id:
            channel_obj = guild.get_channel(int(ch_channel_id))
            label = f"#{channel_obj.name}" if channel_obj is not None else f"#{ch_channel_id}"  # type: ignore[union-attr]
        else:
            label = f"#{ch_channel_id}"
        if session is not None:
            audit = await _resolve_row_audit(session, ch_row)
        else:
            audit = "set"
        blocks.append(ScopeBlock(scope_label=label, agent_name=ch_row.agent_name, audit_line=audit))

    # 3. Server default (when set)
    if tenant_row is not None and tenant_row.agent_name:
        if session is not None:
            audit = await _resolve_row_audit(session, tenant_row)
        else:
            audit = "set"
        blocks.append(
            ScopeBlock(
                scope_label="whole server", agent_name=tenant_row.agent_name, audit_line=audit
            )
        )

    # 4. Everywhere-else fallback — always last
    # Core _pick_agent is the canonical precedence function; adapter NEVER re-derives.
    fallback_agent: str | None = None
    if state.deployment_default.agent_name:
        fallback_agent = state.deployment_default.agent_name
    else:
        _winner_name, _winner_tier = _pick_agent(
            current_channel, tenant_row, state.deployment_default
        )
        if _winner_name:
            fallback_agent = _winner_name
    if fallback_agent:
        blocks.append(
            ScopeBlock(
                scope_label="everywhere else",
                agent_name=fallback_agent,
                audit_line="system default",
            )
        )

    return blocks


class _ActionSelect(discord.ui.Select["SetDefaultView"]):
    """Full-width action select: Set/Clear scope options for the selected agent."""

    def __init__(
        self,
        state: PanelState,
        *,
        channel_default_exists: bool,
        server_default_exists: bool,
    ) -> None:
        channel_name = state.channel_name or str(state.channel_id)
        agent_name = state.selected.name if state.selected else "agent"
        options = [
            discord.SelectOption(
                label="📍 This channel",
                value="set_channel",
                description=f"#{channel_name} only — overrides the server default"[:100],
            ),
            discord.SelectOption(
                label="🌐 Whole server",
                value="set_server",
                description="every channel without its own override",
            ),
        ]
        if channel_default_exists:
            options.append(
                discord.SelectOption(
                    label="🗑️ Clear this channel's override",
                    value="clear_channel",
                    description="falls back to the server/system default",
                )
            )
        if server_default_exists:
            options.append(
                discord.SelectOption(
                    label="🗑️ Clear server default",
                    value="clear_server",
                )
            )
        super().__init__(
            placeholder=f"Set {agent_name} as default…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[type-arg]
        if self.view is None:
            return
        value = self.values[0]
        view = self.view
        if value == "set_channel":
            await view._do_set(  # pyright: ignore[reportPrivateUsage]  # select is a sibling component of the view
                interaction,
                ChannelScopeRef(
                    tenant_id=_PLACEHOLDER_TENANT,
                    channel_id=str(view.state.channel_id),
                ),
            )
        elif value == "set_server":
            await view._do_set(  # pyright: ignore[reportPrivateUsage]
                interaction,
                TenantScopeRef(tenant_id=_PLACEHOLDER_TENANT),
            )
        elif value == "clear_channel":
            await view._do_clear(  # pyright: ignore[reportPrivateUsage]
                interaction,
                ChannelScopeRef(
                    tenant_id=_PLACEHOLDER_TENANT,
                    channel_id=str(view.state.channel_id),
                ),
            )
        elif value == "clear_server":
            await view._do_clear(  # pyright: ignore[reportPrivateUsage]
                interaction,
                TenantScopeRef(tenant_id=_PLACEHOLDER_TENANT),
            )


class _ChannelPickSelect(discord.ui.ChannelSelect["SetDefaultView"]):
    """Channel picker: set the override for any text channel without leaving the panel."""

    def __init__(self, state: PanelState) -> None:
        agent_name = state.selected.name if state.selected else "agent"
        super().__init__(
            placeholder=f"…or pick any channel for {agent_name}",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[type-arg]
        if self.view is None:
            return
        picked_channel = self.values[0]
        await self.view._do_set(  # pyright: ignore[reportPrivateUsage]  # channel select is a sibling component of the view
            interaction,
            ChannelScopeRef(
                tenant_id=_PLACEHOLDER_TENANT,
                channel_id=str(picked_channel.id),
            ),
        )


class SetDefaultView(discord.ui.LayoutView):
    """C9 V2 cascade panel: airy routing blocks + action select + ChannelSelect.

    D-02/D-03: write-immediately (no confirm); winner derivation stays in core
    scope._pick_agent; every send/edit passes AllowedMentions.none() because audit
    lines render live <@id> mentions.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        blocks: list[ScopeBlock] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id

        tenant_row, ch_rows = state.cascade_view
        current_channel = next((c for c in ch_rows if c.channel_id == str(state.channel_id)), None)
        channel_default_exists = current_channel is not None and bool(current_channel.agent_name)
        server_default_exists = tenant_row is not None and bool(tenant_row.agent_name)

        container = build_set_default_container(blocks or [])
        container.add_item(layout.hairline())

        action_select = _ActionSelect(
            state,
            channel_default_exists=channel_default_exists,
            server_default_exists=server_default_exists,
        )
        action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow(
            action_select
        )
        container.add_item(action_row)

        channel_pick = _ChannelPickSelect(state)
        channel_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow(
            channel_pick
        )
        container.add_item(channel_row)

        from daimon.adapters.discord.agent_setup.edit_view import BackButton

        back_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back_row.add_item(BackButton())  # pyright: ignore[reportArgumentType]  # BackButton[View] is structurally compatible at runtime; discord.py V2 ActionRow accepts it
        container.add_item(back_row)

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.", ephemeral=True
            )
            return False
        # state.is_admin is a snapshot from panel-open; re-verify against the live
        # interaction so a demoted admin (or any future caller) cannot write a
        # guild-wide default. This is the authoritative gate for every select here.
        if not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
            await interaction.response.send_message(
                "Only server admins can change defaults.", ephemeral=True
            )
            return False
        return True

    async def _do_set(
        self,
        interaction: discord.Interaction,  # type: ignore[type-arg]
        scope: ChannelScopeRef | TenantScopeRef,
    ) -> None:
        """Write agent default at scope, then re-render the panel."""
        if self.state.selected is None:
            return
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            async with self.runtime.sessionmaker() as session, session.begin():
                scope = scope.model_copy(update={"tenant_id": tenant_id})
                await do_propagate(
                    session,
                    scope=scope,
                    tenant_id=tenant_id,
                    agent_name=self.state.selected.name,
                    actor_account_id=self.state.account_id,
                )
                cascade = await list_guild_propagations(session, tenant_id=tenant_id)
                self.state.cascade_view = cascade
                blocks = await _build_scope_blocks(self.state, interaction, session)
            await interaction.edit_original_response(
                view=SetDefaultView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    blocks=blocks,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.set_default.failed",
                err_type=type(err).__name__,
                request_id=rid,
            )
            await interaction.followup.send(render_error(err, request_id=rid), ephemeral=True)

    async def _do_clear(
        self,
        interaction: discord.Interaction,  # type: ignore[type-arg]
        scope: ChannelScopeRef | TenantScopeRef,
    ) -> None:
        """Clear agent default at scope, then re-render the panel."""
        await interaction.response.defer()
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            async with self.runtime.sessionmaker() as session, session.begin():
                scope = scope.model_copy(update={"tenant_id": tenant_id})
                await do_unpropagate(
                    session,
                    scope=scope,
                    actor_account_id=self.state.account_id,
                )
                cascade = await list_guild_propagations(session, tenant_id=tenant_id)
                self.state.cascade_view = cascade
                blocks = await _build_scope_blocks(self.state, interaction, session)
            await interaction.edit_original_response(
                view=SetDefaultView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    blocks=blocks,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.clear_default.failed",
                err_type=type(err).__name__,
                request_id=rid,
            )
            await interaction.followup.send(render_error(err, request_id=rid), ephemeral=True)


async def open_set_default(
    interaction: discord.Interaction,  # type: ignore[type-arg]
    state: PanelState,
    *,
    runtime: DiscordRuntime,
    allowed_user_id: int,
) -> None:
    """Send the ephemeral SetDefaultView. Owns the send site for the Default… button callback."""
    blocks = await _build_scope_blocks(state, interaction)
    await interaction.response.send_message(
        view=SetDefaultView(state, runtime=runtime, allowed_user_id=allowed_user_id, blocks=blocks),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

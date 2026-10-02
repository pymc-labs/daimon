"""Operator tokens: tenant-scoped MCP tokens for an external integration.

Reached from Who answers where, by server admins only. Lists the server's live operator
tokens, mints one in a modal (shown once, in an ephemeral) and revokes one from a
select. Every click and submit re-checks Manage Server live and is audited.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid
from collections.abc import Sequence
from typing import Final

import structlog
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_audit import PanelOp, PanelOutcome, record_panel_write
from daimon.core.panel_operator_tokens import (
    PANEL_SCOPES,
    PANEL_TTL_DAYS,
    list_panel_operator_tokens,
    mint_panel_operator_token,
    operator_token_line,
    revoke_panel_operator_token,
)
from daimon.core.stores.domain import McpTokenRow

import discord

log = structlog.get_logger()

OPERATOR_TOKENS_LABEL: Final = "🔑 Operator tokens"
MINT_LABEL: Final = "Mint a token"
BACK_LABEL: Final = "◀ Back"
MAX_LISTED: Final = 25
NOT_CONFIGURED: Final = "-# Operator tokens need the MCP server's public URL and signing key."
EXPLAINER: Final = (
    f"-# An operator token lets an integration call daimon's tenant tools as you, for "
    f"{PANEL_TTL_DAYS} days. It is shown once; revoke it here or with "
    "`daimon mcp revoke-token`."
)


def build_operator_tokens_container(
    rows: Sequence[McpTokenRow], *, notice: str | None = None
) -> discord.ui.Container[discord.ui.LayoutView]:
    """The listing card. Pure — no I/O, and never a token value."""
    lines = [f"`{operator_token_line(row)}`" for row in rows[:MAX_LISTED]]
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(OPERATOR_TOKENS_LABEL.removeprefix("🔑 ")))
    container.add_item(discord.ui.TextDisplay("\n".join(lines) or "-# no live operator tokens"))
    if notice:
        container.add_item(discord.ui.TextDisplay(notice))
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(EXPLAINER))
    return container


def _tenant_id(state: PanelState) -> uuid.UUID:
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


async def load_operator_tokens(runtime: DiscordRuntime, *, state: PanelState) -> list[McpTokenRow]:
    async with runtime.sessionmaker() as session:
        return await list_panel_operator_tokens(
            session, tenant_id=_tenant_id(state), now=dt.datetime.now(dt.UTC)
        )


class OperatorTokensView(PanelViewBase):
    """The server's live operator tokens, with Mint and a Revoke select."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        rows: Sequence[McpTokenRow],
        notice: str | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        configured = runtime.settings.mcp.jwt_secret is not None
        container = build_operator_tokens_container(
            rows, notice=notice if configured else NOT_CONFIGURED
        )
        if rows:
            revoke: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
                placeholder="Revoke a token…",
                options=[
                    discord.SelectOption(label=operator_token_line(row)[:100], value=str(row.jti))
                    for row in rows[:MAX_LISTED]
                ],
            )
            revoke.callback = functools.partial(self._on_revoke, select=revoke)  # type: ignore[method-assign]  # per-instance callback
            select_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            select_row.add_item(revoke)
            container.add_item(select_row)
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        row.add_item(back)
        if configured:
            mint: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=MINT_LABEL, style=discord.ButtonStyle.primary
            )
            mint.callback = self._on_mint  # type: ignore[method-assign]  # per-instance callback
            row.add_item(mint)
        row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(row)
        self.add_item(container)

    async def audit(
        self,
        interaction: discord.Interaction,
        *,
        op: PanelOp,
        outcome: PanelOutcome,
        reason: str,
        jti: uuid.UUID | None = None,
    ) -> None:
        await record_panel_write(
            self.runtime.sessionmaker,
            tenant_id=_tenant_id(self.state),
            platform="discord",
            platform_user_id=str(interaction.user.id),
            op=op,
            outcome=outcome,
            reason=reason,
            token_kind="operator",
            token_jti=jti,
        )

    async def rebuilt(self, *, notice: str | None = None) -> OperatorTokensView:
        return OperatorTokensView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            rows=await load_operator_tokens(self.runtime, state=self.state),
            notice=notice,
        )

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

    async def _on_mint(self, interaction: discord.Interaction) -> None:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            await self.audit(
                interaction, op="operator_token_mint", outcome="denied", reason="needs_admin"
            )
            return
        await interaction.response.send_modal(MintOperatorTokenModal(self))

    async def _on_revoke(
        self, interaction: discord.Interaction, *, select: discord.ui.Select[discord.ui.LayoutView]
    ) -> None:
        jti = uuid.UUID(select.values[0])
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            await self.audit(
                interaction,
                op="operator_token_revoke",
                outcome="denied",
                reason="needs_admin",
                jti=jti,
            )
            return
        await interaction.response.defer()
        async with self.runtime.sessionmaker.begin() as session:
            revoked = await revoke_panel_operator_token(
                session, tenant_id=_tenant_id(self.state), jti=jti, now=dt.datetime.now(dt.UTC)
            )
        await self.audit(
            interaction,
            op="operator_token_revoke",
            outcome="allowed" if revoked else "error",
            reason="completed" if revoked else "already_revoked",
            jti=jti,
        )
        log.info("agent_setup.operator_token.revoked", jti=str(jti), revoked=revoked)
        notice = "-# Token revoked." if revoked else "-# That token was already revoked."
        await self.swap_to(interaction, await self.rebuilt(notice=notice))


class MintOperatorTokenModal(discord.ui.Modal):
    """Pick the scopes and an optional label; the token comes back once, ephemerally."""

    def __init__(self, view: OperatorTokensView) -> None:
        super().__init__(title="Mint an operator token")
        self._view = view
        self.scopes: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            min_values=1,
            max_values=len(PANEL_SCOPES),
            options=[discord.SelectOption(label=scope, value=scope) for scope in PANEL_SCOPES],
        )
        self.label: discord.ui.TextInput[discord.ui.View] = discord.ui.TextInput(
            label="Label", required=False, max_length=100, placeholder="what it is for"
        )
        self.add_item(discord.ui.Label(text="Scopes", component=self.scopes))
        self.add_item(self.label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self._view
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            await view.audit(
                interaction, op="operator_token_mint", outcome="denied", reason="needs_admin"
            )
            return
        secret = view.runtime.settings.mcp.jwt_secret
        if secret is None:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
            return
        try:
            async with view.runtime.sessionmaker.begin() as session:
                minted = await mint_panel_operator_token(
                    session,
                    tenant_id=_tenant_id(view.state),
                    platform="discord",
                    platform_user_id=str(interaction.user.id),
                    scopes=self.scopes.values,
                    label=self.label.value,
                    secret=secret.get_secret_value().encode(),
                    now=dt.datetime.now(dt.UTC),
                )
        except OperatorTokenError as exc:
            await view.audit(
                interaction, op="operator_token_mint", outcome="denied", reason="scopes"
            )
            await interaction.response.send_message(f"{exc}. Nothing was minted.", ephemeral=True)
            return
        await view.audit(
            interaction,
            op="operator_token_mint",
            outcome="allowed",
            reason="completed",
            jti=minted.jti,
        )
        log.info("agent_setup.operator_token.minted", jti=str(minted.jti))  # never the token
        await view.swap_to(interaction, await view.rebuilt())
        await interaction.followup.send(
            f"```\n{minted.token}\n```\nScopes: {', '.join(sorted(minted.scopes))}. Expires "
            f"{minted.expires_at.date().isoformat()}. This is the one time it is shown.",
            ephemeral=True,
        )

"""Redeem-code modal for /billing. Admin only: the credit lands on the whole server.

The admin gate runs again on submit, because the modal outlives the click that
opened it. Redemption goes through ``daimon.core.promo_credit``; the panel is
re-rendered so the new balance and any timed credit show at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemRefused, PromoRedeemResult, redeem_promo_code

import discord

Rerender = Callable[[discord.Interaction], Awaitable[None]]


def _ts(value: datetime) -> str:
    return f"<t:{int(value.timestamp())}:f>"


def redeem_result_text(result: PromoRedeemResult) -> str:
    """The ephemeral reply to a redemption. Pure."""
    if isinstance(result, PromoRedeemRefused):
        return f"🎟️ {describe_refusal(result.reason)}"
    amount = f"**${result.amount_usd:,.2f}**"
    if result.credit_ends_at is None:
        return f"🎟️ Redeemed {amount} of credit. Balance: **${result.balance_usd:,.2f}**."
    window = f"until {_ts(result.credit_ends_at)}"
    if not result.granted and result.credit_starts_at is not None:
        window = f"from {_ts(result.credit_starts_at)} {window}"
    return (
        f"🎟️ Redeemed {amount} of timed credit, usable {window}. "
        "It is spent before other credit, and what is left then expires."
    )


class RedeemCodeModal(discord.ui.Modal, title="Redeem a promo code"):
    def __init__(
        self, *, runtime: DiscordRuntime, account_id: uuid.UUID, rerender: Rerender
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.account_id = account_id
        self.rerender = rerender
        self.code_in: discord.ui.TextInput[RedeemCodeModal] = discord.ui.TextInput(
            label="Promo code",
            placeholder="XXXXX-XXXXX-XXXXX-XXXXX",
            required=True,
            max_length=100,
        )
        self.add_item(self.code_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.guild_id is None:
            return
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
            return
        # Launched from the panel message, so defer() is a deferred message update
        # and the re-render below edits the panel in place.
        await interaction.response.defer()
        try:
            result = await redeem_promo_code(
                self.runtime.sessionmaker,
                tenant_id=derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                ),
                account_id=self.account_id,
                code=str(self.code_in.value or ""),
                now=datetime.now(UTC),
            )
            if not isinstance(result, PromoRedeemRefused):
                await self.rerender(interaction)
            await interaction.followup.send(redeem_result_text(result), ephemeral=True)
        except (DaimonError, discord.HTTPException) as exc:
            await interaction.followup.send(
                render_error(exc, request_id=generate_request_id()), ephemeral=True
            )

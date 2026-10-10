"""Redeem-code modal for /billing. Admin only: the credit lands on the whole server.

The admin gate runs again on submit, because the modal outlives the click that
opened it. Redemption goes through ``daimon.core.promo_credit``; the panel is
re-rendered so the new balance and any timed credit show at once.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.panel_audit import record_panel_write
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemRefused, PromoRedeemResult, redeem_promo_code
from sqlalchemy.exc import SQLAlchemyError

import discord

_log = structlog.get_logger()

Rerender = Callable[[discord.Interaction], Awaitable[None]]


def _ts(value: datetime) -> str:
    return f"<t:{int(value.timestamp())}:f>"


def redeem_result_text(result: PromoRedeemResult) -> str:
    """The ephemeral reply to a redemption. Pure."""
    if isinstance(result, PromoRedeemRefused):
        return f"🎟️ {describe_refusal(result.reason)}"
    amount = f"**${result.amount_usd:,.2f}**"
    if result.credit_ends_at is None:
        return f"🎟️ Added {amount} of credit.\n\nBalance: **${result.balance_usd:,.2f}**"
    if not result.granted and result.credit_starts_at is not None:
        return (
            f"🎟️ Credit scheduled: {amount} from {_ts(result.credit_starts_at)} "
            f"until {_ts(result.credit_ends_at)}."
        )
    return (
        f"🎟️ Added {amount} of credit.\n\n"
        "Used before credit with no expiry. Anything unused expires "
        f"{_ts(result.credit_ends_at)}."
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
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        audit = functools.partial(
            record_panel_write,
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=str(interaction.user.id),
            op="promo_redeem",
        )
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
            await audit(outcome="denied", reason="needs_admin")
            return
        # Launched from the panel message, so defer() is a deferred message update
        # and the re-render below edits the panel in place.
        await interaction.response.defer()
        try:
            result = await redeem_promo_code(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                account_id=self.account_id,
                code=str(self.code_in.value or ""),
                now=datetime.now(UTC),
            )
            await interaction.followup.send(redeem_result_text(result), ephemeral=True)
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            # Deferred already, so the admin gets an answer whatever failed.
            request_id = generate_request_id()
            _log.error(
                "billing_redeem.failed",
                guild_id=interaction.guild_id,
                request_id=request_id,
                exc_info=exc,
            )
            capture_exception_with_scope(exc)
            await interaction.followup.send(
                render_error(exc, request_id=request_id), ephemeral=True
            )
            await audit(outcome="error", reason="failed")
            return
        if isinstance(result, PromoRedeemRefused):
            await audit(outcome="denied", reason=f"promo:{result.reason}")
            return
        await audit(outcome="allowed", reason="completed")
        try:
            await self.rerender(interaction)
        except (DaimonError, discord.HTTPException, SQLAlchemyError) as exc:
            # The credit landed and the reply said so; only the panel is stale.
            _log.warning(
                "billing_redeem.rerender_failed", guild_id=interaction.guild_id, exc_info=exc
            )
            capture_exception_with_scope(exc)

"""Set charge and Decline on a pending support post.

Anyone acting in the configured support channel (or a thread in it) may
settle (decision 4); `settle_from_post` audits it. The requester is never
messaged (decision 8): the buttons and the settler's confirmation say so.
The custom id carries the request id, so the buttons work after a restart.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any, Final, Literal, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.support_post import discord_channel, settled_content, status_line
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import SettleOutcome, get_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support_plan_changes import is_support_channel, settle_from_post
from daimon.core.support_plans import (
    CHARGE_FIELD,
    CLIENT_NOT_NOTIFIED,
    DECLINE,
    REQUEST_GONE,
    SET_CHARGE,
    SETTLE_ELSEWHERE,
    SupportPlanError,
    already_settled_text,
    format_credits,
    option_text,
    parse_amount,
    settle_range_text,
    settled_text,
)

import discord
from discord.ext import commands

_log = structlog.get_logger()

SettleAction = Literal["set", "decline"]
CUSTOM_ID_TEMPLATE: Final = r"sps:(?P<escalation>[0-9a-f]{32}):(?P<action>set|decline)"
_FAILED = "That didn't work. Try again."


def build_settle_id(escalation_id: uuid.UUID, action: SettleAction) -> str:
    return f"sps:{escalation_id.hex}:{action}"


def settle_view(escalation_id: uuid.UUID) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SupportSettleButton(escalation_id=escalation_id, action="set"))
    view.add_item(SupportSettleButton(escalation_id=escalation_id, action="decline"))
    return view


def _settler(by: str | None) -> str | None:
    """A Discord settler as a mention; any other surface's name as stored."""
    return f"<@{by}>" if by and by.isdigit() else by


def _in_support_channel(interaction: discord.Interaction[Any], support: SupportSettings) -> bool:
    channel = interaction.channel
    parent = channel.parent_id if isinstance(channel, discord.Thread) else None
    return is_support_channel(
        configured=discord_channel(support),
        channel_id=str(interaction.channel_id),
        parent_id=None if parent is None else str(parent),
    )


async def _notice(
    interaction: discord.Interaction[Any], text: str, *, neutral: bool = False
) -> None:
    await interaction.followup.send(embed=build_notice_embed(text, neutral=neutral), ephemeral=True)


async def _close_post(
    interaction: discord.Interaction[Any], before: SupportEscalationRow, after: SupportEscalationRow
) -> None:
    """Edit the post the button is on: the final status and settler, no buttons."""
    message = interaction.message
    if message is None:
        return
    by = _settler(after.settled_by)
    final = status_line(after)
    content = settled_content(
        message.content,
        pending_line=status_line(before),
        final_line=f"{final} by {by}" if by else final,
    )
    try:
        await interaction.edit_original_response(content=content, view=None)
    except discord.HTTPException as exc:
        # The settle is committed; a stale post only shows old buttons, which
        # answer "Already charged" on the next click.
        _log.warning("support.post_edit_failed", escalation_id=str(after.id), error=str(exc))


async def _finish(
    interaction: discord.Interaction[Any], *, before: SupportEscalationRow, outcome: SettleOutcome
) -> None:
    after = outcome.row
    if after is None:
        await _notice(interaction, REQUEST_GONE)
        return
    if outcome.status in ("invalid", "over_balance"):
        text = settle_range_text(after.bounds, counted=after.counted, available=outcome.available)
        await _notice(interaction, text)
        return
    if after.status != "pending":
        await _close_post(interaction, before, after)
    if outcome.status == "settled":
        text = settled_text(charge=after.charge)
    elif outcome.status == "declined":
        text = settled_text(charge=None)
    else:
        by = _settler(after.settled_by)
        text = already_settled_text(status=after.status, charge=after.charge, by=by)
    await _notice(interaction, text, neutral=True)


async def _settle(
    interaction: discord.Interaction[Any],
    *,
    runtime: DiscordRuntime,
    row: SupportEscalationRow,
    charge: Decimal | None,
) -> None:
    """Settle at `charge` (None declines) after the interaction was deferred."""
    settler = str(interaction.user.id)
    account_id: uuid.UUID | None = None
    if interaction.guild_id is not None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        async with runtime.sessionmaker() as session:
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="discord", external_id=settler
            )
        account_id = None if principal is None else principal.account_id
    outcome = await settle_from_post(
        runtime.sessionmaker,
        escalation_id=row.id,
        charge=charge,
        settled_by=settler,
        settled_by_account_id=account_id,
        default_allowance=runtime.settings.support.credits_per_user,
    )
    _log.info("support.request_settled", escalation_id=str(row.id), status=outcome.status)
    await _finish(interaction, before=row, outcome=outcome)


async def _report_failure(interaction: discord.Interaction[Any]) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=build_notice_embed(_FAILED), ephemeral=True)
    else:
        await interaction.response.send_message(embed=build_notice_embed(_FAILED), ephemeral=True)


class SettleModal(discord.ui.Modal, title=SET_CHARGE):
    """The charge, prefilled with the hold; no note to the requester (decision 8)."""

    def __init__(self, *, runtime: DiscordRuntime, row: SupportEscalationRow) -> None:
        super().__init__()
        self._runtime = runtime
        self._row = row
        terms = [option_text(row.option)] if row.option is not None else []
        lines = [*terms, status_line(row), f"-# {CLIENT_NOT_NOTIFIED}"]
        self.add_item(discord.ui.TextDisplay("\n".join(lines)))
        amount: discord.ui.Label[SettleModal] = discord.ui.Label(
            text=CHARGE_FIELD,
            description=settle_range_text(row.bounds, counted=row.counted, available=None),
            component=discord.ui.TextInput(
                style=discord.TextStyle.short,
                required=True,
                max_length=12,
                default=format_credits(row.hold),
            ),
        )
        # `Label.component` is typed `Item`; it is the TextInput built one line up.
        self.amount_input = cast(discord.ui.TextInput[SettleModal], amount.component)
        self.add_item(amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            charge = parse_amount(
                str(self.amount_input.value or ""), name=CHARGE_FIELD.lower(), allow_zero=True
            )
        except SupportPlanError as err:
            await _notice(interaction, str(err))
            return
        await _settle(interaction, runtime=self._runtime, row=self._row, charge=charge)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Adapter boundary: discord.py routes on_submit failures here, not to a dispatcher."""
        _log.exception("support_settle.modal_failed", err_type=type(error).__name__)
        await _report_failure(interaction)


class SupportSettleButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Set charge or Decline; checks the channel, then the request as it stands now."""

    def __init__(self, *, escalation_id: uuid.UUID, action: SettleAction) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary if action == "set" else discord.ButtonStyle.secondary,
            label=SET_CHARGE if action == "set" else DECLINE,
            custom_id=build_settle_id(escalation_id, action),
        )
        super().__init__(button)
        self.escalation_id = escalation_id
        self.action: SettleAction = action

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        action: SettleAction = "set" if match["action"] == "set" else "decline"
        return cls(escalation_id=uuid.UUID(hex=match["escalation"]), action=action)

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            runtime = cast(DaimonBot, interaction.client).runtime
            if not _in_support_channel(interaction, runtime.settings.support):
                await interaction.response.send_message(
                    embed=build_notice_embed(SETTLE_ELSEWHERE), ephemeral=True
                )
                return
            async with runtime.sessionmaker() as session:
                row = await get_request(session, escalation_id=self.escalation_id)
            if row is None:
                await interaction.response.send_message(
                    embed=build_notice_embed(REQUEST_GONE), ephemeral=True
                )
            elif row.status != "pending":
                await interaction.response.defer()
                outcome = SettleOutcome(status="already_settled", row=row)
                await _finish(interaction, before=row, outcome=outcome)
            elif self.action == "decline":
                await interaction.response.defer()
                await _settle(interaction, runtime=runtime, row=row, charge=None)
            else:
                await interaction.response.send_modal(SettleModal(runtime=runtime, row=row))
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary; discord.py's dispatcher swallows
            # anything raised here
            _log.exception("support_settle.callback_failed", err_type=type(err).__name__)
            await _report_failure(interaction)


__all__ = ["CUSTOM_ID_TEMPLATE", "SettleModal", "SupportSettleButton", "settle_view"]

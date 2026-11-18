"""BillingPanelView, its container and the panels behind its buttons, for /billing.

The panel is one Components V2 container: the month, the credit left, the
channel's budget, for an admin the top spenders and channel budgets, and the
actions, each section set apart by a separator. Its
accent shows state: red with no credit left or the caller over their cap,
amber while timed credit expires within a week.

The Discord-native admin check (manage_guild | administrator | owner) is
re-derived from the live interaction on every render AND on every admin
click — it is never trusted from the rendered view. `is_admin` on the view is a
render hint that decides whether the admin actions appear at all; the boundary
is the click-time admin gate from `checks.py`, called as the first act of each
admin callback before any HTTP call or usage read. Every interaction rebuilds
the view with a fresh timeout, so a member who was an admin when the panel
opened may not be one when they click.

Admins get "Add credit" (amounts that POST to the MCP /billing/checkout route
via an authenticated account token; Discord never imports stripe), "Redeem
code" while a code is redeemable, and "Look up a person" for one member's
spend. Everyone gets "Expiry dates", a private reply, while the credit
includes timed credit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from daimon.adapters.discord import layout
from daimon.adapters.discord.billing_panel.read import (
    invoking_channel_id,
    is_platform_admin,
    load_billing_snapshot,
)
from daimon.adapters.discord.billing_panel.redeem import RedeemCodeModal
from daimon.adapters.discord.checks import refuse_if_not_platform_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.ui_render import Container, Text, panel_container
from daimon.core.billing_panel import (
    ADD_CREDIT,
    EXPIRY_DATES,
    LOOK_UP,
    REDEEM_CODE,
    TOPUP_AMOUNTS,
    BillingMarks,
    BillingPanelState,
    MemberRow,
    billing_screen,
    create_checkout,
    expiry_screen,
    lookup_line,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import remember_user_name
from daimon.core.stores.usage_events import (
    cost_for_user_in_tenant_since,
    turn_count_for_user_in_tenant_since,
)
from daimon.core.web_links import OPEN_IN_WEB, panel_links

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


def plain_name(name: str) -> str:
    """A display name as literal text: no markdown, no mention, no line break."""
    flat = " ".join(name.splitlines())
    return discord.utils.escape_mentions(discord.utils.escape_markdown(flat))


def spender_name(row: MemberRow) -> str:
    """The row's name as literal text, or for someone Discord never named to us a
    `<@id>` mention: the client shows it as their name, and the panel's
    `AllowedMentions.none()` keeps it from notifying them."""
    if row.display_name:
        return plain_name(row.display_name)
    if row.platform_user_id.isdigit():
        return f"<@{row.platform_user_id}>"
    return "Unknown member"


def _discord_date(moment: datetime) -> str:
    """The date and how far off it is, each shown in the reader's own timezone."""
    epoch = int(moment.timestamp())
    return f"<t:{epoch}:D> (<t:{epoch}:R>)"


MARKS = BillingMarks(
    person=spender_name,
    channel=lambda channel_id: f"<#{channel_id}>",
    date=_discord_date,
    accent=True,
    more_spenders=". Look one up below.",
)


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    controls: Sequence[discord.ui.ActionRow[Any]] = (),
) -> Container:
    """The /billing panel, with ``controls`` last; the Done render passes none."""
    return panel_container(billing_screen(state, since=since, now=now, marks=MARKS), controls)


def build_expiry_container(state: BillingPanelState) -> Container:
    """`## Expiry dates`, then `Unused credit expires:` and one row per timed credit."""
    return panel_container(expiry_screen(state.timed_credit, marks=MARKS))


def build_member_lookup_container(*, display_name: str, spend_usd: float, turns: int) -> Container:
    """The member lookup's private reply: `## Maya Chen` + `$14.02 this month`."""
    hdr = layout.header(plain_name(display_name))
    return discord.ui.Container(hdr, Text(lookup_line(spend_usd, turns)))


# ---------------------------------------------------------------------------
# Interactive selects and buttons
# ---------------------------------------------------------------------------


class _TopUpSelect(discord.ui.Select["BillingPanelView"]):
    """The "Add credit" select: a full-width list of top-up amounts. Admin panel only."""

    def __init__(self, state: BillingPanelState) -> None:
        options = [
            discord.SelectOption(
                label=f"${amount}",
                value=str(amount),
            )
            for amount in TOPUP_AMOUNTS
        ]
        super().__init__(
            placeholder=ADD_CREDIT,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or interaction.guild_id is None:
            return
        # The view's is_admin decided whether this select rendered; it does not
        # decide whether the click may mint a checkout link.
        if await refuse_if_not_platform_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
            return
        try:
            amount = int(self.values[0])
            url = await _create_checkout(
                self.view,
                guild_id=str(interaction.guild_id),
                amount=amount,
            )
            await interaction.response.send_message(
                embed=build_notice_embed(
                    f"Top up ${amount}: complete payment here (link is private):\n{url}",
                    neutral=True,
                ),
                ephemeral=True,
            )
        except (DaimonError, discord.HTTPException, httpx.HTTPStatusError) as exc:
            rid = generate_request_id()
            await interaction.response.send_message(
                embed=build_notice_embed(render_error(exc, request_id=rid)), ephemeral=True
            )


class _MemberLookupSelect(discord.ui.UserSelect["BillingPanelView"]):
    """The "Look up a person" picker: one member's spend this month. Admin panel only."""

    def __init__(self) -> None:
        super().__init__(
            placeholder=LOOK_UP,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or interaction.guild_id is None:
            return
        # Per-member spend is exactly what load_billing_snapshot's member branch
        # withholds — the gate has to run before the usage reads, not at render.
        if await refuse_if_not_platform_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
            return
        try:
            selected = self.values[0]
            guild_id = str(interaction.guild_id)
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
            now = datetime.now(UTC)
            since = datetime(now.year, now.month, 1, tzinfo=UTC)
            async with self.view.runtime.sessionmaker() as session:
                spend = await cost_for_user_in_tenant_since(
                    session,
                    tenant_id=tenant_id,
                    platform_user_id=str(selected.id),
                    since=since,
                )
                turns = await turn_count_for_user_in_tenant_since(
                    session,
                    tenant_id=tenant_id,
                    platform_user_id=str(selected.id),
                    since=since,
                )
            # The picker hands over their name: remembered like any other.
            remember_user_name(
                self.view.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                user_id=str(selected.id),
                display_name=selected.display_name,
                handle=selected.name,
            )
            lookup_container = build_member_lookup_container(
                display_name=selected.display_name,
                spend_usd=spend,
                turns=turns,
            )
            await interaction.response.send_message(
                view=layout.static_view(lookup_container),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, discord.HTTPException) as exc:
            rid = generate_request_id()
            await interaction.response.send_message(
                embed=build_notice_embed(render_error(exc, request_id=rid)), ephemeral=True
            )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _InvokerOnly(discord.ui.LayoutView):
    allowed_user_id: int

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                embed=build_notice_embed("Only the command invoker can use these buttons."),
                ephemeral=True,
            )
            return False
        return True


def web_spend_url(runtime: DiscordRuntime, guild_id: int | str, *, is_admin: bool) -> str | None:
    """The web page of this server's spend, or None with no web app."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    links = panel_links(runtime.settings.web, tenant_id, admin=is_admin)
    return None if links is None else links.spend()


class BillingPanelView(_InvokerOnly):
    def __init__(
        self,
        state: BillingPanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        is_admin: bool,
        account_id: uuid.UUID,
        now: datetime,
        since: datetime,
        web_url: str | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        self.is_admin = is_admin
        self.account_id = account_id
        self.panel_now = now
        self.panel_since = since

        rows: list[discord.ui.ActionRow[Any]] = []
        if is_admin:
            rows.append(discord.ui.ActionRow(_TopUpSelect(state)))
        actions: list[discord.ui.Button[BillingPanelView]] = []
        if state.is_admin and state.has_redeemable_promo_code:
            actions.append(_RedeemButton())
        if state.timed_credit:
            actions.append(_ExpiryButton())
        if actions:
            rows.append(discord.ui.ActionRow(*actions))
        if is_admin:
            rows.append(discord.ui.ActionRow(_MemberLookupSelect()))
        footer: list[discord.ui.Button[BillingPanelView]] = [_RefreshButton(), _DoneButton()]
        if web_url is not None:
            footer.append(
                discord.ui.Button(label=OPEN_IN_WEB, style=discord.ButtonStyle.link, url=web_url)
            )
        rows.append(discord.ui.ActionRow(*footer))
        self.add_item(build_billing_container(state, now=now, since=since, controls=rows))


class _RefreshButton(discord.ui.Button["BillingPanelView"]):
    def __init__(self) -> None:
        super().__init__(label="Refresh", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        await _rerender(interaction, self.view)


class _ExpiryButton(discord.ui.Button["BillingPanelView"]):
    """The "Expiry dates" button: when each part of the timed credit expires, privately."""

    def __init__(self) -> None:
        super().__init__(label=EXPIRY_DATES, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        await interaction.response.send_message(
            view=layout.static_view(build_expiry_container(self.view.state)),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _RedeemButton(discord.ui.Button["BillingPanelView"]):
    """Opens the redeem-code modal while a code the viewer may redeem is redeemable.

    Platform admins see it. The modal rechecks live platform authority on submit.
    """

    def __init__(self) -> None:
        super().__init__(label=REDEEM_CODE, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return

        async def rerender(submitted: discord.Interaction) -> None:
            await _rerender(submitted, view)

        await interaction.response.send_modal(
            RedeemCodeModal(runtime=view.runtime, account_id=view.account_id, rerender=rerender)
        )


class _DoneButton(discord.ui.Button["BillingPanelView"]):
    def __init__(self) -> None:
        super().__init__(label="Done", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        # Re-render a controls-less container instead of view=None (never view=None).
        controls_less = layout.static_view(
            build_billing_container(
                self.view.state, now=self.view.panel_now, since=self.view.panel_since
            )
        )
        await interaction.response.edit_message(
            view=controls_less,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _create_checkout(
    view: BillingPanelView,
    *,
    guild_id: str,
    amount: int,
) -> str:
    """POST to the MCP /billing/checkout route via an authenticated account token.

    Discord cannot import stripe directly (adapter independence). This helper
    sends a bearer-authed HTTP POST to the MCP billing route. The URL
    returned is the ephemeral Stripe Checkout Session URL.

    The bearer is a registered expiring non-admin account token: the checkout route
    only authenticates the account and uses the verifier-derived tenant — it never
    checks admin. Minting an is_admin+internal token here would hand an arbitrary
    platform account a non-revocable admin bearer at the MCP gate.
    """
    async with httpx.AsyncClient() as client:
        return await create_checkout(
            client,
            settings=view.runtime.settings.mcp,
            session_factory=view.runtime.sessionmaker,
            account_id=view.account_id,
            amount=amount,
        )


async def _rerender(
    interaction: discord.Interaction,
    view: BillingPanelView,
) -> None:
    runtime = view.runtime
    assert interaction.guild_id is not None
    assert interaction.guild is not None
    bot_interaction: BotInteraction = interaction  # type: ignore[assignment]  # narrowing to the Bot-bound interaction inside our adapter
    if not interaction.response.is_done():
        # Member fetches can take a moment; a deferred update keeps the click alive.
        await interaction.response.defer()
    now = datetime.now(UTC)
    since = datetime(now.year, now.month, 1, tzinfo=UTC)
    is_admin = is_platform_admin(bot_interaction)
    async with runtime.sessionmaker() as session:
        new_state = await load_billing_snapshot(
            session,
            guild=interaction.guild,
            guild_id=str(interaction.guild_id),
            caller_user_id=str(interaction.user.id),
            is_admin=is_admin,
            since=since,
            channel_id=invoking_channel_id(bot_interaction),
            now=now,
            client=interaction.client,
            sessionmaker=runtime.sessionmaker,
        )
    new_view = BillingPanelView(
        new_state,
        runtime=runtime,
        allowed_user_id=view.allowed_user_id,
        is_admin=is_admin,
        account_id=view.account_id,
        now=now,
        since=since,
        web_url=web_spend_url(runtime, interaction.guild_id, is_admin=is_admin),
    )
    await interaction.edit_original_response(
        view=new_view,
        allowed_mentions=discord.AllowedMentions.none(),
    )

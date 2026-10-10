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

import datetime as dt
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from daimon.adapters.discord import layout
from daimon.adapters.discord.billing_panel.read import (
    invoking_channel_id,
    is_guild_admin,
    load_billing_snapshot,
)
from daimon.adapters.discord.billing_panel.redeem import RedeemCodeModal
from daimon.adapters.discord.billing_panel.state import (
    COLOR_OVER_CAP,
    COLOR_WARNING,
    BillingPanelState,
    MemberRow,
)
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.billing_panel import (
    ADD_CREDIT,
    ASK_ADMIN,
    CHANNEL_BUDGET,
    CHANNEL_BUDGETS,
    CHANNEL_BUDGETS_SHOWN,
    EXPIRY_DATES,
    EXPIRY_INTRO,
    LOOK_UP,
    NOTHING_USED,
    REDEEM_CODE,
    TITLE,
    TOP_SPENDERS,
    TOP_SPENDERS_SHOWN,
    TOPUP_AMOUNTS,
    YOU,
    admin_summary,
    caller_line,
    channel_budget_line,
    channel_budget_phrase,
    credit_headline,
    expiry_rows,
    lookup_line,
    month_label,
    more_spenders,
    panel_tone,
    spender_line,
    timed_credit_note,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mcp_auth import mint_jwt
from daimon.core.platform_names import remember_user_name
from daimon.core.stores.usage_events import (
    cost_for_user_in_tenant_since,
    turn_count_for_user_in_tenant_since,
)

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]
Container = discord.ui.Container[discord.ui.LayoutView]
Text = discord.ui.TextDisplay[discord.ui.LayoutView]

# Fallback cost per turn used when guild has no usage history yet.
_FALLBACK_TURN_COST_USD = 0.10


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------


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


def _gap() -> discord.ui.Separator[discord.ui.LayoutView]:
    """The line and space between two blocks of a panel."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def _accent(state: BillingPanelState, now: datetime) -> int | None:
    tone = panel_tone(
        balance=state.guild_balance_usd,
        caller_spend=state.caller_spend,
        caller_cap=state.caller_cap,
        credits=state.timed_credit,
        now=now,
    )
    return {"alert": COLOR_OVER_CAP, "warning": COLOR_WARNING, None: None}[tone]


def _credit_text(state: BillingPanelState) -> str:
    """The total as a heading, the words under it, and the timed credit it includes."""
    figure, words = credit_headline(state.guild_balance_usd)
    lines = [f"### {figure}"]
    if words is not None:
        lines.append(words)
    if (note := timed_credit_note(state.timed_credit)) is not None:
        lines.append(f"-# {note}")
    if not state.is_admin:
        lines.append(f"-# {ASK_ADMIN}")
    return "\n".join(lines)


def estimate_turns(
    amount_usd: float,
    *,
    guild_spend: float,
    guild_turns: int,
) -> int:
    """Estimate turns purchasable for amount_usd given guild usage history.

    Uses the guild's average cost per turn when history is available
    (guild_spend > 0 and guild_turns > 0); falls back to
    _FALLBACK_TURN_COST_USD = $0.10/turn when there is no usage history yet.
    The fallback is a conservative estimate for new guilds.
    """
    if guild_spend > 0 and guild_turns > 0:
        cost_per_turn = guild_spend / guild_turns
    else:
        cost_per_turn = _FALLBACK_TURN_COST_USD
    return int(amount_usd / cost_per_turn)


# ---------------------------------------------------------------------------
# Pure container builders
# ---------------------------------------------------------------------------


def _spenders_text(state: BillingPanelState) -> str:
    """`**Top spenders**`, the top five by name, then `+ N more — look one up below`."""
    rows = [
        spender_line(rank, spender_name(row), cost=row.cost_usd, is_caller=row.is_caller)
        for rank, row in enumerate(state.member_rows[:TOP_SPENDERS_SHOWN], start=1)
    ] or [NOTHING_USED]
    if overflow := more_spenders(len(state.member_rows), state.over_cap_count):
        rows.append(f"-# + {overflow} more. Look one up below.")
    return "\n".join([f"**{TOP_SPENDERS}**", *rows])


def _channel_budgets_text(state: BillingPanelState, now: datetime) -> str:
    """`**Channel budgets**`: every channel budget, most used first, five then a count."""
    lines = [f"**{CHANNEL_BUDGETS}**"] + [
        channel_budget_line(status, label=f"<#{status.budget.channel_id}>", now=now)
        for status in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
    ]
    if (more := len(state.channel_budgets) - CHANNEL_BUDGETS_SHOWN) > 0:
        lines.append(f"-# + {more} more")
    return "\n".join(lines)


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    controls: Sequence[discord.ui.ActionRow[Any]] = (),
) -> Container:
    """The /billing panel: one container, its sections set apart by separators.

      - header: `## Billing` + `-# October 2026` + `-# $48.17 spent by 9 people`
        (a member's subtext is the month alone)
      - a member's own use: `**You**` + `$11.50 of your $25.00 this month`
      - credit: `### $62.40` + `total credit left` + the timed credit it includes
        (and for a member `-# Ask an admin to add credit.`)
      - `**This channel**` + `$1.20 of $5.00 used this month`, when the
        invoking channel has one
      - admin only: `**Top spenders**` by name, then `**Channel budgets**` when
        any exist
      - ``controls``, the action rows, last; the Done render passes none

    The accent is red with no credit left or the caller over their cap, amber
    while timed credit expires within a week, and absent otherwise.
    """
    subtext = (
        admin_summary(since, spend=state.guild_spend, people=state.guild_distinct_members)
        if state.is_admin
        else (month_label(since),)
    )
    sections: list[str] = []
    if not state.is_admin:
        own = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
        sections.append(f"**{YOU}**\n{own}")
    sections.append(_credit_text(state))
    if state.channel_budget is not None:
        phrase = channel_budget_phrase(state.channel_budget, now=now)
        sections.append(f"**{CHANNEL_BUDGET}**\n{phrase}")
    if state.is_admin:
        sections.append(_spenders_text(state))
        if state.channel_budgets:
            sections.append(_channel_budgets_text(state, now))
    header = Text("\n".join([f"## {TITLE}", *(f"-# {line}" for line in subtext)]))
    children: list[discord.ui.Item[discord.ui.LayoutView]] = [header]
    for section in sections:
        children += [_gap(), Text(section)]
    if controls:
        children += [_gap(), *controls]
    return discord.ui.Container(*children, accent_colour=_accent(state, now))


def build_expiry_container(state: BillingPanelState) -> Container:
    """`## Expiry dates`, then `Unused credit expires:` and one row per timed credit."""
    rows = expiry_rows(state.timed_credit, when=_discord_date)
    return discord.ui.Container(
        layout.header(EXPIRY_DATES), _gap(), Text("\n".join([EXPIRY_INTRO, *rows]))
    )


def build_member_lookup_container(
    *,
    display_name: str,
    spend_usd: float,
    turns: int,
    since: datetime,
    now: datetime,
) -> Container:
    """The member lookup's private reply: `## Maya Chen` + `$14.02 this month`.

    With no spend and no turns the body is `Nothing used this month`, which
    also covers someone who has no daimon account.
    """
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
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
            return
        try:
            amount = int(self.values[0])
            url = await _create_checkout(
                self.view,
                guild_id=str(interaction.guild_id),
                amount=amount,
            )
            await interaction.response.send_message(
                f"Top up ${amount}: complete payment here (link is private):\n{url}",
                ephemeral=True,
            )
        except (DaimonError, discord.HTTPException, httpx.HTTPStatusError) as exc:
            rid = generate_request_id()
            await interaction.response.send_message(
                render_error(exc, request_id=rid), ephemeral=True
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
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
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
                since=since,
                now=now,
            )
            await interaction.response.send_message(
                view=layout.static_view(lookup_container),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, discord.HTTPException) as exc:
            rid = generate_request_id()
            await interaction.response.send_message(
                render_error(exc, request_id=rid), ephemeral=True
            )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _InvokerOnly(discord.ui.LayoutView):
    allowed_user_id: int

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True


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
        if is_admin and state.has_redeemable_promo_code:
            actions.append(_RedeemButton())
        if state.timed_credit:
            actions.append(_ExpiryButton())
        if actions:
            rows.append(discord.ui.ActionRow(*actions))
        if is_admin:
            rows.append(discord.ui.ActionRow(_MemberLookupSelect()))
        rows.append(discord.ui.ActionRow(_RefreshButton(), _DoneButton()))
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
    """Opens the redeem-code modal. Admin panel only, while a code is redeemable.

    Re-gated on click and on submit.
    """

    def __init__(self) -> None:
        super().__init__(label=REDEEM_CODE, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # narrowing to the Bot-bound interaction inside our adapter
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
        # Re-render a controls-less container instead of view=None (B8: never view=None).
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
    sends a bearer-authed HTTP POST to the MCP route built in Plan 03. The URL
    returned is the ephemeral Stripe Checkout Session URL.

    The bearer is a plain non-admin account token (mint_jwt): the checkout route
    only authenticates the account and uses the verifier-derived tenant — it never
    checks admin. Minting an is_admin+internal token here would hand an arbitrary
    platform account a non-revocable admin bearer at the MCP gate (#162 / CR-01).
    """
    settings = view.runtime.settings
    # The /billing/checkout route is add_route'd at the app root, not under the
    # /mcp streamable endpoint — use app_root_url (strips the /mcp suffix).
    app_root_url = settings.mcp.app_root_url
    jwt_secret = settings.mcp.jwt_secret
    assert app_root_url is not None and jwt_secret is not None, (
        "MCP public_url + jwt_secret required for top-up; "
        "check DAIMON_MCP__PUBLIC_URL / DAIMON_MCP__JWT_SECRET"
    )
    # Tenant ids are derived deterministically from (platform, guild) — the same
    # uuid the turn pipeline bills against. The panel only renders for registered
    # guilds (require_registered_guild).
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    token = mint_jwt(
        account_id=view.account_id,
        secret=jwt_secret.get_secret_value().encode(),
        now=dt.datetime.now(dt.UTC),
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{app_root_url.rstrip('/')}/billing/checkout",
            json={"tenant_id": str(tenant_id), "guild_id": guild_id, "amount": amount},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return str(resp.json()["url"])


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
    is_admin = is_guild_admin(bot_interaction)
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
    )
    await interaction.edit_original_response(
        view=new_view,
        allowed_mentions=discord.AllowedMentions.none(),
    )

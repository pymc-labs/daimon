"""BillingPanelView + build_billing_container + buttons for /billing.

Discord-native admin check (manage_guild | administrator | owner) is re-resolved
on every Refresh via `is_guild_admin` (D-UX-04). Admin view shows a top-up
string select ($10/$25/$50/$100) and a UserSelect member-spend lookup that POST to
the MCP /billing/checkout route via an authenticated internal token. Discord never
imports stripe (TOPUP-02/OQ#4 rec (a)).
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from daimon.adapters.discord import layout
from daimon.adapters.discord.billing_panel.read import (
    is_guild_admin,
    load_billing_snapshot,
)
from daimon.adapters.discord.billing_panel.state import (
    COLOR_OVER_CAP,
    BillingPanelState,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores.usage_events import (
    cost_for_user_in_tenant_since,
    turn_count_for_user_in_tenant_since,
)

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

# Fallback cost per turn used when guild has no usage history yet.
_FALLBACK_TURN_COST_USD = 0.10


# ---------------------------------------------------------------------------
# Pure formatters (unchanged from embed era)
# ---------------------------------------------------------------------------


def _fmt_usd(value: float | Decimal) -> str:
    return f"${value:,.2f}"


def _period_label(since: datetime) -> str:
    return f"period: {since.strftime('%B %Y')} (UTC)"


def _is_over_cap(spend: float, cap: Decimal | None) -> bool:
    return cap is not None and spend > float(cap)


def _format_caller_line(spend: float, cap: Decimal | None, turns: int) -> str:
    """Regular-view caller line."""
    if cap is None:
        return f"💸 {_fmt_usd(spend)} spent · {turns} turns"
    cap_f = float(cap)
    pct = int(spend / cap_f * 100) if cap_f > 0 else 0
    return f"💸 {_fmt_usd(spend)} / {_fmt_usd(cap_f)} cap ({pct}%) · {turns} turns"


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
# V2 pure container builders (B8 design)
# ---------------------------------------------------------------------------


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Build the B8 billing Container (text-only, no ActionRows).

    Admin branch:
      - header: '💸 Billing · admin view' + subtext with period/guild totals
      - hairline
      - one TextDisplay: 🏦 Server credit group + 🏆 Top spenders group (top 5)
        followed by dim '{N} more members' line when N > 0

    Member branch:
      - header: '💸 Billing' + subtext with period
      - hairline
      - one TextDisplay: **You** group + 🏦 Server credit group

    Accent: COLOR_OVER_CAP only when caller is over their cap; no accent otherwise
    (COLOR_NOMINAL is retired — B8 decision).
    """
    accent = COLOR_OVER_CAP if _is_over_cap(state.caller_spend, state.caller_cap) else None

    if state.is_admin:
        subtext = (
            f"{_period_label(since)} · "
            f"guild total {_fmt_usd(state.guild_spend)} · "
            f"{state.guild_turns} turns · "
            f"{state.guild_distinct_members} active members"
        )
        hdr = layout.header("💸 Billing · admin view", subtext=subtext)

        # Server credit group
        credit_line = f"-# {_fmt_usd(state.guild_balance_usd)} balance"
        body_lines: list[str] = [
            "🏦 **Server credit**",
            credit_line,
            "",
            "🏆 **Top spenders**",
        ]
        top5 = state.member_rows[:5]
        if top5:
            for i, row in enumerate(top5):
                rank = i + 1
                you = " (you)" if row.is_caller else ""
                spend_str = _fmt_usd(row.cost_usd)
                body_lines.append(
                    f"-# {rank}. {row.display_name}{you}  {spend_str} · {row.turn_count} turns"
                )
        else:
            body_lines.append("-# no usage yet this period")

        # 'N more members' overflow line
        overflow = max(0, len(state.member_rows) - 5) + state.over_cap_count
        if overflow > 0:
            body_lines.append(f"-# {overflow} more members — look one up below")

        body: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(
            "\n".join(body_lines)
        )
        return discord.ui.Container(hdr, layout.hairline(), body, accent_colour=accent)

    # Member (non-admin) branch
    subtext = _period_label(since)
    hdr = layout.header("💸 Billing", subtext=subtext)

    if state.caller_spend == 0.0 and state.caller_turns == 0:
        caller_body = "-# no usage yet this period"
    else:
        caller_body = (
            f"-# {_format_caller_line(state.caller_spend, state.caller_cap, state.caller_turns)}"
        )

    credit_line = f"-# {_fmt_usd(state.guild_balance_usd)} balance (top-ups are admin-only)"

    body_lines_member: list[str] = [
        "**You**",
        caller_body,
        "",
        "🏦 **Server credit**",
        credit_line,
    ]
    body_member: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(
        "\n".join(body_lines_member)
    )
    return discord.ui.Container(hdr, layout.hairline(), body_member, accent_colour=accent)


def build_member_lookup_container(
    *,
    display_name: str,
    spend_usd: float,
    turns: int,
    since: datetime,
    now: datetime,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Pure builder for the member-spend lookup ephemeral reply.

    When spend == 0.0 and turns == 0 the body is the locked line
    'no usage this period' — covers the zero-daimon-account case invisibly.
    """
    hdr = layout.header(f"🔍 {display_name}", subtext=_period_label(since))

    if spend_usd == 0.0 and turns == 0:
        body_text = "no usage this period"
    else:
        body_text = f"{_fmt_usd(spend_usd)} spent · {turns} turns"

    body: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(body_text)
    return discord.ui.Container(hdr, layout.hairline(), body)


# ---------------------------------------------------------------------------
# Interactive selects
# ---------------------------------------------------------------------------


class _TopUpSelect(discord.ui.Select["BillingPanelView"]):
    """Full-width string select for top-up amounts. Admin card only."""

    def __init__(self, state: BillingPanelState) -> None:
        options = [
            discord.SelectOption(
                label=f"${amount}",
                value=str(amount),
                description=f"≈ {estimate_turns(float(amount), guild_spend=state.guild_spend, guild_turns=state.guild_turns):,} turns"[  # noqa: E501
                    :100
                ],
            )
            for amount in (10, 25, 50, 100)
        ]
        super().__init__(
            placeholder="💳 Top up server credit…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or interaction.guild_id is None:
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
        except (DaimonError, discord.HTTPException) as exc:
            rid = generate_request_id()
            await interaction.response.send_message(
                render_error(exc, request_id=rid), ephemeral=True
            )


class _MemberLookupSelect(discord.ui.UserSelect["BillingPanelView"]):
    """Native UserSelect for per-member spend lookup. Admin card only."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="🔍 Look up a member's spend…",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or interaction.guild_id is None:
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
# View shell (LayoutView)
# ---------------------------------------------------------------------------


class BillingPanelView(discord.ui.LayoutView):
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

        container = build_billing_container(state, now=now, since=since)
        self.add_item(container)
        self.add_item(layout.hairline())

        if is_admin:
            top_up_row = discord.ui.ActionRow(_TopUpSelect(state))
            self.add_item(top_up_row)
            lookup_row = discord.ui.ActionRow(_MemberLookupSelect())
            self.add_item(lookup_row)

        btn_row = discord.ui.ActionRow(_RefreshButton(), _DoneButton())
        self.add_item(btn_row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True


class _RefreshButton(discord.ui.Button["BillingPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        await _rerender(interaction, self.view)


class _DoneButton(discord.ui.Button["BillingPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Done",
            style=discord.ButtonStyle.secondary,
        )

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
    if interaction.response.is_done():
        await interaction.edit_original_response(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.edit_message(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

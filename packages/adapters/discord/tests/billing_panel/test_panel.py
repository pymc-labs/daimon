"""View + container tests for BillingPanelView (LayoutView), build_billing_container,
build_member_lookup_container, and estimate_turns."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

# pyright: reportPrivateUsage=false
from daimon.adapters.discord.billing_panel.panel import (
    BillingPanelView,
    build_billing_container,
    build_member_lookup_container,
    estimate_turns,
)
from daimon.adapters.discord.billing_panel.state import (
    COLOR_OVER_CAP,
    BillingPanelState,
    MemberRow,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.stores.domain import ChannelBudgetRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_member_row(**overrides: Any) -> MemberRow:
    base: dict[str, Any] = {
        "platform_user_id": "100000000000000001",
        "display_name": "alice",
        "cost_usd": 1.23,
        "turn_count": 4,
        "is_caller": False,
    }
    base.update(overrides)
    return MemberRow(**base)


def _make_state(**overrides: Any) -> BillingPanelState:
    base: dict[str, Any] = {
        "is_admin": False,
        "caller_user_id": "100000000000000001",
        "caller_spend": 0.0,
        "caller_turns": 0,
        "caller_cap": None,
        "guild_balance_usd": Decimal("0"),
        "guild_spend": 0.0,
        "guild_turns": 0,
        "guild_distinct_members": 0,
        "member_rows": (),
        "over_cap_count": 0,
    }
    base.update(overrides)
    return BillingPanelState(**base)


def _make_runtime() -> DiscordRuntime:
    return MagicMock(spec=DiscordRuntime)


def _joined_container_text(container: discord.ui.Container[Any]) -> str:
    """Collect all TextDisplay content from a Container, joined with newlines."""
    parts: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            parts.append(child.content)
    return "\n".join(parts)


def _find_select(
    view: discord.ui.LayoutView, cls: type[discord.ui.Select[Any]]
) -> discord.ui.Select[Any] | None:
    """Walk the LayoutView's ActionRow children to find a Select of the given class."""
    for item in view.walk_children():
        if isinstance(item, cls):
            return item
    return None


def _joined_view_text(view: discord.ui.LayoutView) -> str:
    """Collect all TextDisplay content anywhere in a LayoutView, joined with newlines."""
    return "\n".join(
        child.content for child in view.walk_children() if isinstance(child, discord.ui.TextDisplay)
    )


def _admin_interaction(*, guild_id: int, user_id: int = 42) -> MagicMock:
    """A click from a live guild admin.

    The caller has to be a spec'd `discord.Member` with a real permission flag:
    the click-time gate reads the live interaction, so a bare MagicMock user is
    not a Member and would be refused. `is_done()` is pinned False so any
    refusal lands on `response.send_message`, not `followup.send`.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = user_id + 1

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.guild_permissions.administrator = True
    user.guild_permissions.manage_guild = True

    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = user
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _non_admin_interaction(*, guild_id: int, user_id: int = 42) -> MagicMock:
    """A click from a member who is not (or is no longer) a guild admin.

    All three inputs the admin check reads are set: `administrator`,
    `manage_guild`, and a guild owner that is somebody else. Leaving any of them
    a bare mock attribute would make the caller a truthy admin and the refusal
    test inert.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = user_id + 1

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.guild_permissions.administrator = False
    user.guild_permissions.manage_guild = False

    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.guild = guild
    interaction.user = user
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


SINCE = datetime(2026, 5, 1, tzinfo=UTC)
NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


# ---- view-shape tests ----


_TEST_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")


def test_member_view_has_refresh_and_done_buttons_only() -> None:
    """Member (non-admin) view must have Refresh + Done buttons and no UserSelect."""
    view = BillingPanelView(
        _make_state(is_admin=False),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=False,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert "🔄 Refresh" in labels, "member view must have Refresh button"
    assert "Done" in labels, "member view must have Done button"

    user_select = _find_select(view, discord.ui.UserSelect)  # type: ignore[type-abstract]
    assert user_select is None, "member view must not have a UserSelect"


def test_admin_view_has_topup_select_with_4_options() -> None:
    """Admin view must have a string Select with exactly 4 options [$10/$25/$50/$100]."""
    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    # Find the string Select (not UserSelect)
    topup_select: discord.ui.Select[Any] | None = None
    for item in view.walk_children():
        if isinstance(item, discord.ui.Select) and not isinstance(item, discord.ui.UserSelect):
            topup_select = item
            break
    assert topup_select is not None, "admin view must have a string Select for top-up"
    options = topup_select.options
    assert len(options) == 4, f"top-up select must have exactly 4 options, got {len(options)}"
    values = [o.value for o in options]
    assert values == ["10", "25", "50", "100"], (
        "top-up select options must be ['10','25','50','100']"
    )
    for opt in options:
        assert opt.description is not None and opt.description.startswith("≈ "), (
            f"option '{opt.label}' description must start with '≈ '"
        )


def test_admin_view_has_user_select_for_member_lookup() -> None:
    """Admin view must have a UserSelect for member spend lookup."""
    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    user_select = _find_select(view, discord.ui.UserSelect)  # type: ignore[type-abstract]
    assert user_select is not None, "admin view must have a UserSelect for member lookup"
    assert isinstance(user_select, discord.ui.UserSelect), (
        "found select must be a discord.ui.UserSelect"
    )


# ---- invoker gate ----


@pytest.mark.asyncio
async def test_interaction_check_rejects_non_invoker() -> None:
    view = BillingPanelView(
        _make_state(),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=False,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    interaction = MagicMock()
    interaction.user.id = 999  # different from allowed_user_id=42
    interaction.response.send_message = AsyncMock()

    ok = await view.interaction_check(interaction)

    assert ok is False, "non-invoker click should be rejected"
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_interaction_check_accepts_invoker() -> None:
    view = BillingPanelView(
        _make_state(),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=False,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    interaction = MagicMock()
    interaction.user.id = 42
    ok = await view.interaction_check(interaction)
    assert ok is True, "the original invoker's click should pass interaction_check"


# ---- top-up select callback (transport-level httpx mock, no stripe import) ----


@pytest.mark.asyncio
async def test_topup_select_callback_posts_to_mcp_checkout_and_sends_ephemeral_url(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Select callback POSTs to /billing/checkout via httpx.MockTransport and sends URL ephemerally.

    The credit target is the deterministically derived tenant_id — no workspaces-table lookup.
    POST body must carry derive_tenant_uuid(discord, guild_id).
    """
    import json

    import httpx
    from daimon.adapters.discord.billing_panel.panel import _TopUpSelect
    from daimon.core.ma_identity import derive_tenant_uuid
    from pydantic import HttpUrl, SecretStr

    guild_id = "888000000000000001"
    expected_tenant_id = str(derive_tenant_uuid(platform="discord", workspace_id=guild_id))
    checkout_url = "https://checkout.stripe.com/pay/test_abc123"

    captured_requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"url": checkout_url}, request=request)

    mock_transport = httpx.MockTransport(_handle)

    import unittest.mock as _mock

    from daimon.core.config import McpSettings

    mock_settings = MagicMock()
    # public_url carries the /mcp streamable-endpoint suffix in production; the
    # billing route is add_route'd at the app root, so the checkout POST must go
    # to <root>/billing/checkout, NOT <…/mcp>/billing/checkout.
    mock_settings.mcp = McpSettings(
        public_url=HttpUrl("http://mcp-internal:8000/mcp"),
        jwt_secret=SecretStr("test-secret-for-button-callback-32b"),
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.settings = mock_settings
    runtime.sessionmaker = db_session_factory

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )

    topup_select: _TopUpSelect | None = None
    for item in view.walk_children():
        if isinstance(item, _TopUpSelect):
            topup_select = item
            break
    assert topup_select is not None, "admin view must contain a _TopUpSelect"

    interaction = _admin_interaction(guild_id=int(guild_id))
    # Simulate selecting "$10"
    topup_select._values = ["10"]  # pyright: ignore[reportAttributeAccessIssue]

    # Inject the mock transport so the real httpx.AsyncClient runs but hits our fake.
    with _mock.patch(
        "daimon.adapters.discord.billing_panel.panel.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=mock_transport),
    ):
        await topup_select.callback(interaction)

    assert len(captured_requests) == 1, "exactly one POST to /billing/checkout"
    req = captured_requests[0]
    assert str(req.url) == "http://mcp-internal:8000/billing/checkout", (
        "checkout POST must target the app-root /billing/checkout, not the /mcp endpoint path"
    )
    body = json.loads(req.content)
    assert body["tenant_id"] == expected_tenant_id, (
        "POST body tenant_id must be derive_tenant_uuid(discord, guild_id)"
    )
    assert body["guild_id"] == guild_id
    assert body["amount"] == 10

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    assert call_args.kwargs.get("ephemeral") is True, "top-up URL must be sent ephemerally"
    sent_text = call_args.args[0] if call_args.args else ""
    assert checkout_url in sent_text, "the Stripe checkout URL must be forwarded to the admin"
    assert "10" in sent_text, "the amount must appear in the ephemeral message"


@pytest.mark.asyncio
async def test_topup_checkout_token_carries_no_admin_or_internal_claim(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR-01 (#162 hardening): the billing-checkout bearer must not bake admin.

    The /billing/checkout route authenticates the account and uses the
    verifier-derived tenant; it never checks is_admin. Minting an
    is_admin+internal token from this adapter path would hand an arbitrary
    platform account a non-revocable bearer that passes the MCP admin gate's
    (is_admin AND internal) limb. The checkout bearer must be a plain non-admin
    account token, so a leaked checkout bearer grants no admin authority.
    """
    import json  # noqa: F401  (parity with sibling test imports)

    import httpx
    import jwt as pyjwt
    from daimon.adapters.discord.billing_panel.panel import _TopUpSelect
    from daimon.core.config import McpSettings
    from pydantic import HttpUrl, SecretStr

    secret = "test-secret-for-button-callback-32b"
    captured: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"url": "https://checkout.stripe.com/pay/x"}, request=request
        )

    mock_transport = httpx.MockTransport(_handle)
    import unittest.mock as _mock

    mock_settings = MagicMock()
    mock_settings.mcp = McpSettings(
        public_url=HttpUrl("http://mcp-internal:8000/mcp"),
        jwt_secret=SecretStr(secret),
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.settings = mock_settings
    runtime.sessionmaker = db_session_factory

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    topup_select = next(item for item in view.walk_children() if isinstance(item, _TopUpSelect))
    interaction = _admin_interaction(guild_id=888000000000000001)
    topup_select._values = ["10"]  # pyright: ignore[reportAttributeAccessIssue]

    with _mock.patch(
        "daimon.adapters.discord.billing_panel.panel.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=mock_transport),
    ):
        await topup_select.callback(interaction)

    assert len(captured) == 1, "exactly one POST to /billing/checkout"
    auth = captured[0].headers["authorization"]
    assert auth.lower().startswith("bearer "), "checkout POST must send a Bearer token"
    token = auth[len("bearer ") :]
    claims = pyjwt.decode(token, secret.encode(), algorithms=["HS256"])
    assert claims["sub"] == str(_TEST_ACCOUNT_ID), (
        "checkout bearer must authenticate the caller's own account"
    )
    assert "is_admin" not in claims, "billing checkout bearer must not bake is_admin (CR-01)"
    assert "internal" not in claims, (
        "billing checkout bearer must not carry the internal admin discriminator (CR-01)"
    )


@pytest.mark.asyncio
async def test_topup_select_renders_an_error_when_the_checkout_call_returns_a_non_2xx_status(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failing checkout POST must reach the panel's error renderer.

    `_create_checkout` calls `raise_for_status()`. Before the except tuple was
    widened, the resulting `httpx.HTTPStatusError` escaped the callback into
    discord.py's dispatcher, which shows the admin "This interaction failed" and
    no request id — the failure operators most need to be able to trace.
    """
    import httpx
    from daimon.adapters.discord.billing_panel.panel import _TopUpSelect
    from daimon.core.config import McpSettings
    from pydantic import HttpUrl, SecretStr

    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unknown account"}, request=request)

    mock_transport = httpx.MockTransport(_handle)
    import unittest.mock as _mock

    mock_settings = MagicMock()
    mock_settings.mcp = McpSettings(
        public_url=HttpUrl("http://mcp-internal:8000/mcp"),
        jwt_secret=SecretStr("test-secret-for-button-callback-32b"),
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.settings = mock_settings
    runtime.sessionmaker = db_session_factory

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    topup_select = next(item for item in view.walk_children() if isinstance(item, _TopUpSelect))
    interaction = _admin_interaction(guild_id=888000000000000001)
    topup_select._values = ["10"]  # pyright: ignore[reportAttributeAccessIssue]

    with _mock.patch(
        "daimon.adapters.discord.billing_panel.panel.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=mock_transport),
    ):
        await topup_select.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    assert call_args.kwargs.get("ephemeral") is True, "the failure notice must be ephemeral"
    sent_text = call_args.args[0] if call_args.args else ""
    assert "rid: " in sent_text, (
        "a failed checkout must render with a request id the operator can trace in logs"
    )
    assert "checkout.stripe.com" not in sent_text, "no checkout URL may be sent on a failure"
    assert db_session is not None


@pytest.mark.asyncio
async def test_topup_select_refuses_a_demoted_admin_without_creating_a_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member who was an admin when the panel rendered must mint no checkout link.

    The panel is self-renewing (every interaction rebuilds it with a fresh
    timeout), so the render-time `is_admin` snapshot is indefinitely stale.
    `_create_checkout` is replaced with a raiser: a gate that sends an ephemeral
    and then falls through would still fail this test.
    """
    from daimon.adapters.discord.billing_panel import panel as panel_module
    from daimon.adapters.discord.billing_panel.panel import _TopUpSelect

    async def _explode(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a non-admin click must never reach _create_checkout")

    monkeypatch.setattr(panel_module, "_create_checkout", _explode)

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    topup_select = next(item for item in view.walk_children() if isinstance(item, _TopUpSelect))
    interaction = _non_admin_interaction(guild_id=888000000000000001)
    topup_select._values = ["10"]  # pyright: ignore[reportAttributeAccessIssue]

    await topup_select.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    sent_text = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in sent_text, (
        f"the refusal must tell the member what permission is missing; got: {sent_text!r}"
    )
    assert "checkout.stripe.com" not in sent_text, "no checkout URL may be handed to a non-admin"
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_lookup_select_refuses_a_demoted_admin_without_reading_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-member spend is exactly what load_billing_snapshot's member branch withholds.

    Both usage reads are replaced with raisers, and the runtime's sessionmaker
    raises too, so the gate must refuse before the panel opens a session at all.
    """
    from daimon.adapters.discord.billing_panel import panel as panel_module
    from daimon.adapters.discord.billing_panel.panel import _MemberLookupSelect

    async def _explode_cost(*_args: object, **_kwargs: object) -> float:
        raise AssertionError("a non-admin click must never read another member's spend")

    async def _explode_turns(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("a non-admin click must never read another member's turn count")

    monkeypatch.setattr(panel_module, "cost_for_user_in_tenant_since", _explode_cost)
    monkeypatch.setattr(panel_module, "turn_count_for_user_in_tenant_since", _explode_turns)

    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = MagicMock(
        side_effect=AssertionError("a refused lookup must not open a session")
    )

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    lookup_select = next(
        item for item in view.walk_children() if isinstance(item, _MemberLookupSelect)
    )
    target = MagicMock(spec=discord.Member)
    target.id = 100000000000000007
    target.display_name = "bob"
    lookup_select._values = [target]  # pyright: ignore[reportAttributeAccessIssue]

    interaction = _non_admin_interaction(guild_id=888000000000000001)

    await lookup_select.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    sent_text = call_args.args[0]
    assert "Manage Server" in sent_text, (
        f"the refusal must tell the member what permission is missing; got: {sent_text!r}"
    )
    assert "view" not in call_args.kwargs, "no spend card may be rendered for a non-admin"
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_lookup_select_still_renders_spend_for_a_live_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The gate must not cost a live admin the lookup the panel exists to offer."""
    from daimon.adapters.discord.billing_panel.panel import _MemberLookupSelect

    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory

    view = BillingPanelView(
        _make_state(is_admin=True),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )
    lookup_select = next(
        item for item in view.walk_children() if isinstance(item, _MemberLookupSelect)
    )
    target = MagicMock(spec=discord.Member)
    target.id = 100000000000000007
    target.display_name = "bob"
    lookup_select._values = [target]  # pyright: ignore[reportAttributeAccessIssue]

    interaction = _admin_interaction(guild_id=888000000000000001)

    await lookup_select.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    assert call_args.kwargs.get("ephemeral") is True, "the spend card must stay ephemeral"
    rendered = call_args.kwargs.get("view")
    assert isinstance(rendered, discord.ui.LayoutView), (
        "a live admin's lookup must still render the member-spend card"
    )
    text = _joined_view_text(rendered)
    assert "bob" in text, "the card must name the looked-up member"
    assert "no usage this period" in text, (
        "a member with no seeded usage must render the zero-spend copy, not a refusal"
    )
    assert db_session is not None


# ---- V2 container builder tests (B8 design) ----


def test_admin_container_with_8_member_rows_renders_top5_plus_more_line() -> None:
    """Admin container with 8 member_rows renders exactly 5 spender rows + 'more members' line."""
    rows = tuple(
        _make_member_row(
            platform_user_id=f"u{i}",
            display_name=f"user{i}",
            cost_usd=float(8 - i),
            turn_count=i + 1,
        )
        for i in range(8)
    )
    state = _make_state(
        is_admin=True,
        guild_spend=36.0,
        guild_turns=36,
        guild_distinct_members=8,
        member_rows=rows,
        over_cap_count=2,
    )
    container = build_billing_container(state, now=NOW, since=SINCE)
    text = _joined_container_text(container)

    # Exactly 5 rank rows (check for "-# 1." through "-# 5." dim rows)
    for rank in range(1, 6):
        assert f"-# {rank}." in text, f"rank {rank} spender row must be present"
    assert "-# 6." not in text, "rank 6 must not appear — only top 5 render"

    # Overflow: (8 - 5) = 3 in member_rows beyond 5, plus over_cap_count=2 → 5 total
    assert "5 more members — look one up below" in text, (
        "overflow line must report (8-5)+over_cap_count=5 more members"
    )


def test_admin_container_with_5_or_fewer_rows_and_no_overflow_has_no_more_line() -> None:
    """Admin container with <=5 member_rows and over_cap_count==0 must not have 'more' line."""
    rows = tuple(
        _make_member_row(
            platform_user_id=f"u{i}",
            display_name=f"user{i}",
            cost_usd=float(5 - i),
        )
        for i in range(4)
    )
    state = _make_state(
        is_admin=True,
        member_rows=rows,
        over_cap_count=0,
    )
    container = build_billing_container(state, now=NOW, since=SINCE)
    text = _joined_container_text(container)
    assert "more members" not in text, (
        "no 'more members' line when member_rows<=5 and over_cap_count==0"
    )


def test_admin_container_header_subtext_contains_period_guild_totals_and_active_members() -> None:
    """Header subtext must contain period label, guild total, turn count, and active-member count."""
    state = _make_state(
        is_admin=True,
        guild_spend=42.50,
        guild_turns=17,
        guild_distinct_members=5,
    )
    container = build_billing_container(state, now=NOW, since=SINCE)
    # The first TextDisplay child is the header (from layout.header)
    header_td = next(c for c in container.children if isinstance(c, discord.ui.TextDisplay))
    text = header_td.content
    assert "May 2026" in text, "period month/year must appear in header subtext"
    assert "$42.50" in text, "guild total spend must appear in header subtext"
    assert "17 turns" in text, "guild turn count must appear in header subtext"
    assert "5 active members" in text, "active member count must appear in header subtext"


def test_member_container_has_you_group_and_no_top_spenders_group() -> None:
    """Member (non-admin) container has a 'You' group and no '🏆' group."""
    state = _make_state(
        is_admin=False,
        caller_spend=5.0,
        caller_turns=10,
    )
    container = build_billing_container(state, now=NOW, since=SINCE)
    text = _joined_container_text(container)
    assert "**You**" in text, "member container must contain the You group"
    assert "🏆" not in text, "member container must not contain the Top spenders group"


def test_both_views_show_the_invoking_channels_budget_only_when_it_has_one() -> None:
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id="c",
        limit_usd=Decimal("5"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    status = ChannelBudgetStatus(budget=budget, spent_usd=Decimal("1.2"), is_active=True)
    for is_admin in (False, True):
        with_budget = _make_state(is_admin=is_admin, channel_budget=status)
        text = _joined_container_text(build_billing_container(with_budget, now=NOW, since=SINCE))
        assert "this channel: $1.20 of $5.00 (monthly)" in text
        plain = _joined_container_text(
            build_billing_container(_make_state(is_admin=is_admin), now=NOW, since=SINCE)
        )
        assert "this channel" not in plain


def test_over_cap_container_has_color_over_cap_accent() -> None:
    """Container for an over-cap caller must use COLOR_OVER_CAP as accent_colour."""
    state = _make_state(caller_spend=200.0, caller_turns=10, caller_cap=Decimal("100.00"))
    container = build_billing_container(state, now=NOW, since=SINCE)
    assert container.accent_colour == COLOR_OVER_CAP, (
        "over-cap container must have COLOR_OVER_CAP accent"
    )


def test_nominal_container_has_no_accent() -> None:
    """Container for a nominal (under-cap) caller must have no accent colour."""
    state = _make_state(caller_spend=10.0, caller_turns=2, caller_cap=Decimal("100.00"))
    container = build_billing_container(state, now=NOW, since=SINCE)
    assert container.accent_colour is None, (
        "nominal container must have no accent_colour (COLOR_NOMINAL retired)"
    )


def test_member_lookup_container_zero_spend_renders_no_usage_copy() -> None:
    """lookup container with spend==0.0 and turns==0 must contain 'no usage this period'."""
    container = build_member_lookup_container(
        display_name="charlie",
        spend_usd=0.0,
        turns=0,
        since=SINCE,
        now=NOW,
    )
    text = _joined_container_text(container)
    assert "no usage this period" in text, (
        "zero-spend lookup must render the locked 'no usage this period' copy"
    )


def test_member_lookup_container_nonzero_spend_renders_spend_and_turns() -> None:
    """lookup container with real spend renders formatted spend + turns."""
    container = build_member_lookup_container(
        display_name="dave",
        spend_usd=12.34,
        turns=5,
        since=SINCE,
        now=NOW,
    )
    text = _joined_container_text(container)
    assert "$12.34" in text, "lookup container must show formatted spend"
    assert "5 turns" in text, "lookup container must show turn count"


def _budget_status(channel_id: str, spent: str) -> ChannelBudgetStatus:
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id=channel_id,
        limit_usd=Decimal("10"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return ChannelBudgetStatus(budget=budget, spent_usd=Decimal(spent), is_active=True)


def test_the_admin_view_lists_channel_budgets_and_the_member_view_does_not() -> None:
    budgets = tuple(_budget_status(str(100 + i), str(9 - i)) for i in range(7))
    admin = _joined_container_text(
        build_billing_container(
            _make_state(is_admin=True, channel_budgets=budgets), now=NOW, since=SINCE
        )
    )
    assert "<#100>: $9.00 of $10.00 (monthly) · 90% used" in admin
    assert "<#104>" in admin and "<#105>" not in admin, "only the five most used"
    assert "2 more channel budgets" in admin
    member = _joined_container_text(
        build_billing_container(
            _make_state(is_admin=False, channel_budgets=budgets), now=NOW, since=SINCE
        )
    )
    assert "Channel budgets" not in member, "a member sees no other channel"


def test_admin_container_budget_content_length_under_4000() -> None:
    """Admin container with 5 rows of 32-char names must fit in 4000 display characters."""
    long_name = "A" * 32
    rows = tuple(
        _make_member_row(
            platform_user_id=f"u{i}",
            display_name=long_name,
            cost_usd=float(5 - i),
            turn_count=10,
        )
        for i in range(5)
    )
    state = _make_state(
        is_admin=True,
        guild_spend=100.0,
        guild_turns=50,
        guild_distinct_members=10,
        member_rows=rows,
        over_cap_count=5,
        channel_budgets=tuple(_budget_status("9" * 20, "1" * 9) for _ in range(100)),
    )
    from daimon.adapters.discord import layout as layout_mod

    container = build_billing_container(state, now=NOW, since=SINCE)
    view = layout_mod.static_view(container)
    assert view.content_length() <= 4000, (
        f"budget test: content_length {view.content_length()} must be <= 4000"
    )


# ---- estimate_turns unit tests ----


def test_estimate_turns_uses_guild_average_when_history_available() -> None:
    """estimate_turns uses guild average: $5/100 turns = $0.05/turn → $10 buys 200 turns."""
    result = estimate_turns(10.0, guild_spend=5.0, guild_turns=100)
    assert result == 200, "average path: $10 / ($5/100 turns) = 200 turns"


def test_estimate_turns_uses_fallback_when_no_history() -> None:
    """estimate_turns falls back to $0.10/turn when guild has no history."""
    result = estimate_turns(10.0, guild_spend=0.0, guild_turns=0)
    assert result == 100, "fallback path: $10 / $0.10 = 100 turns"


def test_estimate_turns_fallback_when_guild_turns_zero_but_spend_nonzero() -> None:
    """estimate_turns falls back when guild_turns==0 even if guild_spend>0."""
    result = estimate_turns(10.0, guild_spend=5.0, guild_turns=0)
    assert result == 100, "zero turns means fallback applies even if spend is nonzero"


def test_estimate_turns_fallback_when_guild_spend_zero_but_turns_nonzero() -> None:
    """estimate_turns falls back when guild_spend==0 even if guild_turns>0."""
    result = estimate_turns(10.0, guild_spend=0.0, guild_turns=50)
    assert result == 100, "zero spend means fallback applies even if turns is nonzero"

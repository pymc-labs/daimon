"""View + container tests for BillingPanelView (LayoutView), build_billing_container,
build_member_lookup_container, and estimate_turns."""

from __future__ import annotations

import dataclasses
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
    build_expiry_container,
    build_member_lookup_container,
    estimate_turns,
)
from daimon.adapters.discord.billing_panel.state import (
    COLOR_OVER_CAP,
    COLOR_WARNING,
    BillingPanelState,
    MemberRow,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.promo_credit import ActiveTimedCredit
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
    assert labels == ["Refresh", "Done"], f"no timed credit, no admin actions: {labels}"

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
        assert opt.label == f"${opt.value}", "the label is the amount alone"
        assert opt.description is not None and opt.description.startswith("about "), (
            f"option '{opt.label}' says what it buys as `about N turns`"
        )
        assert opt.description.endswith(" turns")


def test_the_admin_panel_has_the_member_lookup_and_a_member_panel_does_not() -> None:
    lookup = _find_select(_panel(is_admin=True), discord.ui.UserSelect)  # type: ignore[type-abstract]
    assert lookup is not None and lookup.placeholder == "Look up a person"
    assert _find_select(_panel(), discord.ui.UserSelect) is None  # type: ignore[type-abstract]


def _button_rows(view: discord.ui.LayoutView) -> list[list[str | None]]:
    return [
        [c.label for c in row.children if isinstance(c, discord.ui.Button)]
        for row in view.walk_children()
        if isinstance(row, discord.ui.ActionRow)
    ]


def _panel(**overrides: Any) -> BillingPanelView:
    state = _make_state(**overrides)
    return BillingPanelView(
        state,
        runtime=_make_runtime(),
        allowed_user_id=42,
        is_admin=state.is_admin,
        account_id=_TEST_ACCOUNT_ID,
        now=NOW,
        since=SINCE,
    )


def test_buttons_follow_the_viewer_and_the_credit() -> None:
    timed = (ActiveTimedCredit(Decimal("5"), datetime(2026, 6, 30, tzinfo=UTC)),)
    admin = _button_rows(_panel(is_admin=True, timed_credit=timed, has_redeemable_promo_code=True))
    assert admin == [[], ["Redeem code", "Expiry dates"], [], ["Refresh", "Done"]], (
        "Add credit, the actions, Look up a person, then Refresh and Done"
    )
    assert _button_rows(_panel(is_admin=True)) == [[], [], ["Refresh", "Done"]]
    assert _button_rows(_panel(timed_credit=timed)) == [["Expiry dates"], ["Refresh", "Done"]]
    assert _button_rows(_panel()) == [["Refresh", "Done"]], "a member without timed credit"


@pytest.mark.asyncio
async def test_expiry_dates_replies_privately_with_each_credit_soonest_first() -> None:
    from daimon.adapters.discord.billing_panel.panel import _ExpiryButton

    timed = (
        ActiveTimedCredit(Decimal("20"), datetime(2026, 5, 20, 18, 0, tzinfo=UTC)),
        ActiveTimedCredit(Decimal("5"), datetime(2026, 5, 31, tzinfo=UTC)),
    )
    view = _panel(timed_credit=timed)
    button = next(item for item in view.walk_children() if isinstance(item, _ExpiryButton))
    interaction = _admin_interaction(guild_id=1)

    await button.callback(interaction)

    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    first, last = (int(c.ends_at.timestamp()) for c in timed)
    assert _joined_view_text(kwargs["view"]) == (
        "## Expiry dates\n"
        "Unused credit expires:\n"
        f"$20.00 on <t:{first}:D> (<t:{first}:R>)\n"
        f"$5.00 on <t:{last}:D> (<t:{last}:R>)"
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
    assert "rid: " not in sent_text, "a failed checkout must not expose trace ids"
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
    assert "Nothing used this month" in text, (
        "a member with no seeded usage must render the zero-spend copy, not a refusal"
    )
    assert db_session is not None


# ---- container builder tests ----


def _text(container: discord.ui.Container[Any]) -> str:
    return _joined_container_text(container)


def _blocks(container: discord.ui.Container[Any]) -> list[str]:
    """Each TextDisplay, and `---` for a large separator, in order."""
    out: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            out.append(child.content)
        elif isinstance(child, discord.ui.Separator):
            out.append("---" if child.spacing is discord.SeparatorSpacing.large else "-")
    return out


def _this_channel_budget() -> ChannelBudgetStatus:
    return _budget_status("c", "1.2", limit="5")


def _budget_status(channel_id: str, spent: str, *, limit: str = "10") -> ChannelBudgetStatus:
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id=channel_id,
        limit_usd=Decimal(limit),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return ChannelBudgetStatus(budget=budget, spent_usd=Decimal(spent), is_active=True)


def _credit(remaining: str, day: int) -> ActiveTimedCredit:
    return ActiveTimedCredit(Decimal(remaining), datetime(2026, 5, day, 18, 0, tzinfo=UTC))


def test_the_admin_panel_is_header_credit_channel_and_spenders_apart() -> None:
    state = _make_state(
        is_admin=True,
        guild_balance_usd=Decimal("62.4"),
        guild_spend=48.17,
        guild_distinct_members=9,
        timed_credit=(_credit("20", 30), _credit("5", 31)),
        channel_budget=_this_channel_budget(),
        member_rows=(_make_member_row(display_name="Maya Chen", cost_usd=14.02),),
        channel_budgets=(_budget_status("100", "4.1"),),
    )
    assert _blocks(build_billing_container(state, now=NOW, since=SINCE)) == [
        "## Billing\n-# May 2026\n-# $48.17 spent by 9 people",
        "---",
        "### $62.40\ntotal credit left\n-# Includes $25.00 that expires. It's used first.",
        "---",
        "**This channel**\n$1.20 of $5.00 used this month",
        "---",
        "**Top spenders**\n1. Maya Chen  $14.02",
        "---",
        "**Channel budgets**\n<#100>  $4.10 of $10.00 used this month",
    ]


def test_the_member_panel_adds_your_use_and_asks_an_admin_for_credit() -> None:
    state = _make_state(
        caller_spend=11.5, caller_turns=71, caller_cap=Decimal("25"), guild_balance_usd=Decimal("9")
    )
    assert _blocks(build_billing_container(state, now=NOW, since=SINCE)) == [
        "## Billing\n-# May 2026",
        "---",
        "**You**\n$11.50 of your $25.00 this month",
        "---",
        "### $9.00\ntotal credit left\n-# Ask an admin to add credit.",
    ]


def test_the_member_panel_shows_own_use_in_plain_words() -> None:
    def you(**overrides: Any) -> str:
        text = _text(build_billing_container(_make_state(**overrides), now=NOW, since=SINCE))
        return text.split("**You**\n", 1)[1].splitlines()[0]

    assert you(caller_spend=11.5, caller_turns=71) == "$11.50 used this month"
    assert you() == "Nothing used this month"


def test_a_negative_balance_says_no_credit_left_and_still_shows_timed_credit() -> None:
    state = _make_state(
        is_admin=True, guild_balance_usd=Decimal("-3.1"), timed_credit=(_credit("5", 20),)
    )
    blocks = _blocks(build_billing_container(state, now=NOW, since=SINCE))
    assert blocks[2] == (
        "### No credit left\n$3.10 spent beyond it\n"
        "-# Includes $5.00 that expires. It's used first."
    )


def test_controls_sit_last_and_done_drops_them() -> None:
    view = _panel(is_admin=True)
    container = next(c for c in view.children if isinstance(c, discord.ui.Container))
    assert isinstance(container.children[-1], discord.ui.ActionRow), "actions are the last block"
    plain = build_billing_container(view.state, now=NOW, since=SINCE)
    assert not any(isinstance(c, discord.ui.ActionRow) for c in plain.children)


def test_the_accent_shows_state() -> None:
    def accent(**overrides: Any) -> object:
        state = _make_state(**({"guild_balance_usd": Decimal("10")} | overrides))
        return build_billing_container(state, now=NOW, since=SINCE).accent_colour

    assert accent() is None, "nothing to flag"
    assert accent(guild_balance_usd=Decimal("0")) == COLOR_OVER_CAP, "no credit left"
    assert accent(caller_spend=200.0, caller_turns=1, caller_cap=Decimal("100")) == COLOR_OVER_CAP
    assert accent(timed_credit=(_credit("5", 20),)) == COLOR_WARNING, "expires within a week"
    assert accent(timed_credit=(_credit("5", 31),)) is None, "expires later"


def _section(state: BillingPanelState, title: str) -> str:
    blocks = _blocks(build_billing_container(state, now=NOW, since=SINCE))
    return next(block for block in blocks if block.startswith(title))


def test_top_spenders_names_five_and_counts_the_rest() -> None:
    rows = tuple(
        _make_member_row(
            platform_user_id=f"u{i}",
            display_name=f"user{i}",
            cost_usd=float(8 - i),
            is_caller=i == 1,
        )
        for i in range(8)
    )
    state = _make_state(is_admin=True, member_rows=rows, over_cap_count=2)
    assert _section(state, "**Top spenders**") == (
        "**Top spenders**\n1. user0  $8.00\n2. user1 (you)  $7.00\n3. user2  $6.00\n"
        "4. user3  $5.00\n5. user4  $4.00\n-# + 5 more — look one up below"
    ), "overflow is (8-5) rows plus over_cap_count=2"


def test_top_spenders_without_usage_and_no_channel_budgets_section() -> None:
    state = _make_state(is_admin=True)
    assert _section(state, "**Top spenders**") == "**Top spenders**\nNothing used this month"
    blocks = _blocks(build_billing_container(state, now=NOW, since=SINCE))
    assert not any(block.startswith("**Channel budgets**") for block in blocks)


def test_channel_budgets_lists_five_and_counts_the_rest_for_admins_only() -> None:
    budgets = tuple(_budget_status(str(100 + i), str(9 - i)) for i in range(7))
    section = _section(_make_state(is_admin=True, channel_budgets=budgets), "**Channel budgets**")
    assert section.splitlines()[1] == "<#100>  $9.00 of $10.00 used this month"
    assert "<#104>" in section and "<#105>" not in section and section.endswith("-# + 2 more")
    member = _blocks(
        build_billing_container(_make_state(channel_budgets=budgets), now=NOW, since=SINCE)
    )
    assert not any("Channel budgets" in block or "Top spenders" in block for block in member)


def test_top_spender_names_are_escaped() -> None:
    row = _make_member_row(display_name="@everyone **x** <@100000000000000009>")
    line = _section(_make_state(is_admin=True, member_rows=(row,)), "**Top spenders**")
    assert "@everyone" not in line and "<@100000000000000009>" not in line, line
    assert "\\*\\*x\\*\\*" in line, "markdown in a name is shown literally"


def test_someone_never_named_to_us_is_a_mention_the_client_names() -> None:
    row = _make_member_row(platform_user_id="100000000000004993", display_name=None)
    line = _section(_make_state(is_admin=True, member_rows=(row,)), "**Top spenders**")
    assert line.splitlines()[1] == "1. <@100000000000004993>  $1.23", (
        "no `User 4993`: the client renders the mention as their name"
    )
    assert "User " not in line


def test_no_rendered_panel_has_a_dot_separator_or_a_user_label() -> None:
    rows = tuple(
        _make_member_row(
            platform_user_id=f"10000000000000000{i}",
            display_name=None if i % 2 else f"user{i}",
            cost_usd=float(9 - i),
        )
        for i in range(7)
    )
    for is_admin in (True, False):
        state = _make_state(
            is_admin=is_admin,
            guild_spend=48.17,
            guild_turns=300,
            guild_distinct_members=9,
            member_rows=rows if is_admin else (),
            timed_credit=(_credit("20", 30), _credit("5", 31)),
            has_redeemable_promo_code=True,
            channel_budget=_this_channel_budget(),
            channel_budgets=(_budget_status("100", "4.1"),),
        )
        view = _panel(**{f.name: getattr(state, f.name) for f in dataclasses.fields(state)})
        texts = [_joined_view_text(view), _text(build_expiry_container(state))]
        texts += [
            f"{option.label} {option.description}"
            for item in view.walk_children()
            if isinstance(item, discord.ui.Select)
            for option in item.options
        ]
        for text in texts:
            assert "·" not in text and "≈" not in text, text
            assert "User " not in text, text


def test_member_lookup_reply_is_the_name_and_the_month() -> None:
    def lookup(spend: float, turns: int) -> list[str]:
        container = build_member_lookup_container(
            display_name="@everyone **x**", spend_usd=spend, turns=turns, since=SINCE, now=NOW
        )
        return _blocks(container)

    assert lookup(14.02, 3) == ["## @\u200beveryone \\*\\*x\\*\\*", "$14.02 this month"]
    assert lookup(0.0, 0)[1] == "Nothing used this month"


def test_admin_container_budget_content_length_under_4000() -> None:
    """The fullest admin panel fits Discord's 4000 display characters."""
    rows = tuple(
        _make_member_row(platform_user_id=f"u{i}", display_name="A" * 32, cost_usd=float(5 - i))
        for i in range(5)
    )
    state = _make_state(
        is_admin=True,
        guild_spend=100.0,
        guild_turns=50,
        guild_distinct_members=10,
        member_rows=rows,
        over_cap_count=5,
        timed_credit=tuple(_credit("1", 20 + i) for i in range(9)),
        has_redeemable_promo_code=True,
        channel_budget=_this_channel_budget(),
        channel_budgets=tuple(_budget_status("9" * 20, "1" * 9) for _ in range(100)),
    )
    fields = {field.name: getattr(state, field.name) for field in dataclasses.fields(state)}
    assert _panel(**fields).content_length() <= 4000


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

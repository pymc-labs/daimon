"""Behavioral tests for the Use from your coding tools button flow.

Assertions:
  (1) send_coding_tools_access writes an mcp_tokens row and replies ephemerally
      with the /mcp URL + Bearer token in copyable plain message content.
  (2) The reply is ephemeral (send_message called with ephemeral=True).
  (3) The Revoke callback flips revoked_at to non-null.
  (4) Revoke confirmation edits the content in place and drops the button.

Uses real Postgres (db_session_factory fixture), transport-level MA fakes,
and a MagicMock interaction (Discord boundary — the one allowed mock boundary).
No AsyncMock on client.beta.* methods; no model_construct; no MagicMock where
an SDK model is expected.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import jwt as pyjwt
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.agent_setup import mcp_access as mcp_access_mod
from daimon.adapters.discord.agent_setup.mcp_access import send_coding_tools_access
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.domain import AccountRow, TenantRow
from daimon.core.stores.mcp_tokens import count_tokens_for_account, get_mcp_token
from daimon.testing import FIXED_TS, ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    build_stub_anthropic,
    not_found_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PUBLIC_URL = "https://mcp.example.com"
_JWT_SECRET = "test-jwt-secret-32-bytes-padding!"
_JWT_SECRET_BYTES = _JWT_SECRET.encode()


def _entry(name: str, *, is_system: bool = False) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", system=None),
        is_system=is_system,
    )


def _find_button(view: discord.ui.View, label: str) -> discord.ui.Button[Any]:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"No button labeled {label!r}")


def _make_settings(
    *, public_url: str | None = _PUBLIC_URL, jwt_secret: str = _JWT_SECRET
) -> MagicMock:
    settings = MagicMock()
    settings.mcp.public_url = public_url
    # SecretStr-like object
    secret_obj = MagicMock()
    secret_obj.get_secret_value.return_value = jwt_secret
    settings.mcp.jwt_secret = secret_obj if public_url is not None else None
    return settings


def _make_interaction(*, user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    return interaction


async def _setup_tenant_and_account(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    external_id: str,
) -> tuple[TenantRow, AccountRow]:
    """Insert a Tenant + Account row pair for tests that call mint_agent_mcp_token.

    The mcp_tokens FK account_id → accounts.id requires an Account row to exist.
    We explicitly set the Account.id so it matches the PanelState.account_id
    the handler threads into the mint call.
    """
    tenant = await make_tenant(
        db_session, platform="discord", workspace_id=external_id, id=tenant_id
    )
    account = await make_account(db_session, tenant=tenant, id=account_id)
    return tenant, account


# ---------------------------------------------------------------------------
# Test 1: _on_coding_tools writes an mcp_tokens row and replies with config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_coding_tools_writes_row_and_replies_with_config_block(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Driving send_coding_tools_access writes an mcp_tokens row attributed to the personal
    account; the reply's plain content contains the /mcp URL and a Bearer token."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-panel01",
    )

    ma_agent_id = "agent_017abc_77_panel01"
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    interaction = _make_interaction()

    # Act
    await send_coding_tools_access(interaction, runtime=runtime, state=state, allowed_user_id=42)

    # Assert — config is in plain message content (copyable), not a LayoutView
    interaction.response.send_message.assert_called_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    content: str = kwargs["content"]

    assert _PUBLIC_URL in content, f"message content must contain the public_url; got: {content!r}"
    assert "Bearer " in content, f"message content must include 'Bearer ' token; got: {content!r}"

    # Decode the JWT to get the jti so we can verify the row
    # The Bearer token is embedded in the config content
    bearer_idx = content.index("Bearer ") + len("Bearer ")
    raw_after = content[bearer_idx:]
    # Token ends at a whitespace, newline, or backtick
    token_end = len(raw_after)
    for ch in ["\n", " ", "`", '"', "'"]:
        idx = raw_after.find(ch)
        if idx != -1 and idx < token_end:
            token_end = idx
    jwt_token = raw_after[:token_end]

    claims = pyjwt.decode(jwt_token, _JWT_SECRET_BYTES, algorithms=["HS256"])
    jti = uuid.UUID(claims["jti"])

    # Row exists in DB
    row = await get_mcp_token(db_session, jti=jti)
    assert row is not None, "mint must have written an mcp_tokens row"
    assert row.account_id == account_id, (
        f"token row must be attributed to the personal account_id; "
        f"got {row.account_id!r} expected {account_id!r}"
    )
    assert row.agent_id == str(agent_id), (
        f"token row agent_id must be the derived UUID; "
        f"got {row.agent_id!r} expected {str(agent_id)!r}"
    )
    assert row.tenant_id == tenant_id, "token row tenant_id must be the guild's tenant"


@pytest.mark.asyncio
async def test_on_coding_tools_mints_for_card_agent_when_newer_same_name_exists(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """A newer duplicate must not replace the exact agent represented by the card."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-coding-tools-identity",
    )

    card_agent = ma_agent(
        id="agent_card_snapshot",
        name="expert-bot",
        tenant_id=tenant_id,
        created_at=FIXED_TS,
    )
    newer_same_name = ma_agent(
        id="agent_newer_duplicate",
        name="expert-bot",
        tenant_id=tenant_id,
        created_at=FIXED_TS + timedelta(seconds=1),
    )
    router = MARouter()
    router.add_agent_list(card_agent, newer_same_name)
    router.add_agent(card_agent)
    runtime = DiscordRuntime(
        settings=_make_settings(),
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )
    selected = RosterAgent(
        name="expert-bot",
        ma_agent_id=card_agent.id,
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )
    state = PanelState(roster=[], selected=None, account_id=account_id)
    interaction = _make_interaction()

    await send_coding_tools_access(
        interaction,
        runtime=runtime,
        state=state,
        allowed_user_id=42,
        agent=selected,
    )

    content: str = interaction.response.send_message.call_args.kwargs["content"]
    bearer_idx = content.index("Bearer ") + len("Bearer ")
    jwt_token = content[bearer_idx:].split()[0].strip("`\"'")
    claims = pyjwt.decode(jwt_token, _JWT_SECRET_BYTES, algorithms=["HS256"])
    expected_agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=card_agent.id)
    assert claims["agent_id"] == str(expected_agent_id), (
        "coding-tools token must use the exact MA identity stored on the Details card"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable_kind", ["archived", "foreign", "missing"])
async def test_on_coding_tools_refuses_unavailable_card_agent_without_minting(
    unavailable_kind: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Never substitute a current same-name agent for an unavailable card identity."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id=f"test-guild-coding-tools-{unavailable_kind}",
    )
    card_tenant_id = uuid.uuid4() if unavailable_kind == "foreign" else tenant_id
    card_agent = ma_agent(
        id="agent_card_snapshot",
        name="expert-bot",
        tenant_id=card_tenant_id,
        archived_at=FIXED_TS if unavailable_kind == "archived" else None,
    )
    newer_same_name = ma_agent(
        id="agent_newer_duplicate",
        name="expert-bot",
        tenant_id=tenant_id,
        created_at=FIXED_TS + timedelta(seconds=1),
    )
    router = MARouter()
    router.add_agent_list(card_agent, newer_same_name)
    if unavailable_kind == "missing":
        router.add(
            "GET",
            r"/v1/agents/agent_card_snapshot",
            lambda _request, _match: not_found_response("missing agent"),
        )
    else:
        router.add_agent(card_agent)
    runtime = DiscordRuntime(
        settings=_make_settings(),
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )
    selected = RosterAgent(
        name="expert-bot",
        ma_agent_id=card_agent.id,
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )
    interaction = _make_interaction()

    await send_coding_tools_access(
        interaction,
        runtime=runtime,
        state=PanelState(roster=[], selected=None, account_id=account_id),
        allowed_user_id=42,
        agent=selected,
    )

    kwargs = interaction.response.send_message.call_args.kwargs
    refusal = interaction.response.send_message.call_args.args[0]
    assert kwargs["ephemeral"] is True, "unavailable card target errors must remain private"
    assert "no longer available" in refusal, (
        "unavailable exact identity should explain that Details must be reopened"
    )
    assert "Bearer " not in refusal, "refused card identity must expose no token"
    assert await count_tokens_for_account(db_session, account_id=account_id) == 0, (
        "refused card identity must create no token registry row"
    )


# ---------------------------------------------------------------------------
# Test 2: reply is ephemeral
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_coding_tools_reply_is_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """The send_message call for the coding-tools access reply must use ephemeral=True."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-ephemeral",
    )

    ma_agent_id = "agent_017abc_77_ephemeral"
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot-2", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot-2")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    interaction = _make_interaction()
    await send_coding_tools_access(interaction, runtime=runtime, state=state, allowed_user_id=42)

    interaction.response.send_message.assert_called_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True, (
        "coding-tools access reply must be ephemeral=True to prevent token leaks"
    )


# ---------------------------------------------------------------------------
# Test 3: Revoke handler flips revoked_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_callback_flips_revoked_at(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Driving the Revoke callback flips revoked_at from None to a non-null datetime."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-revoke",
    )

    ma_agent_id = "agent_017abc_77_revoke"
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot-3", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot-3")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    # First: mint a token via the handler
    interaction_mint = _make_interaction()
    await send_coding_tools_access(
        interaction_mint, runtime=runtime, state=state, allowed_user_id=42
    )

    # Extract the jti from the minted token (inside the view's text)
    mint_kwargs = interaction_mint.response.send_message.call_args.kwargs
    sent_view: discord.ui.View = mint_kwargs["view"]
    content = mint_kwargs["content"]
    bearer_idx = content.index("Bearer ") + len("Bearer ")
    raw_after = content[bearer_idx:]
    token_end = len(raw_after)
    for ch in ["\n", " ", "`", '"', "'"]:
        idx = raw_after.find(ch)
        if idx != -1 and idx < token_end:
            token_end = idx
    jwt_token = raw_after[:token_end]

    claims = pyjwt.decode(jwt_token, _JWT_SECRET_BYTES, algorithms=["HS256"])
    jti = uuid.UUID(claims["jti"])

    # Verify not yet revoked
    row_before = await get_mcp_token(db_session, jti=jti)
    assert row_before is not None, "row must exist before revoke"
    assert row_before.revoked_at is None, "revoked_at must be None before Revoke is clicked"

    # Extract the Revoke button from the minted view and drive its callback
    revoke_btn = _find_button(sent_view, "Revoke")
    assert revoke_btn.callback is not None, "Revoke button must have a callback"

    interaction_revoke = _make_interaction()
    await revoke_btn.callback(interaction_revoke)

    # Assert revoked_at is now non-null (PRIMARY assertion for this plan)
    row_after = await get_mcp_token(db_session, jti=jti)
    assert row_after is not None, "row must still exist after revoke"
    assert row_after.revoked_at is not None, (
        "revoke callback must flip revoked_at to a non-null datetime — PRIMARY assertion"
    )


# ---------------------------------------------------------------------------
# Test 4: Revoke confirmation edits the plain-content message in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_confirmation_edits_message_content_and_clears_view(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """The minted message is plain content + a classic View, so the Revoke
    confirmation edits the content in place and drops the button (view=None)."""
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-wr01",
    )

    ma_agent_id = "agent_017abc_77_wr01"
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot-wr01", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot-wr01")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    # Mint the token first
    interaction_mint = _make_interaction()
    await send_coding_tools_access(
        interaction_mint, runtime=runtime, state=state, allowed_user_id=42
    )

    # Get the Revoke button from the minted classic view
    mint_kwargs = interaction_mint.response.send_message.call_args.kwargs
    sent_view: discord.ui.View = mint_kwargs["view"]
    revoke_btn = _find_button(sent_view, "Revoke")

    # Drive the Revoke callback
    interaction_revoke = _make_interaction()
    await revoke_btn.callback(interaction_revoke)

    # Assert edit_message replaced the content and dropped the button (view=None)
    interaction_revoke.response.edit_message.assert_called_once()
    edit_kwargs = interaction_revoke.response.edit_message.call_args.kwargs

    assert "revoked" in edit_kwargs.get("content", "").lower(), (
        f"Revoke confirmation must edit the message content; got {edit_kwargs.get('content')!r}"
    )
    assert edit_kwargs.get("view") is None, (
        "Revoke confirmation must drop the button by passing view=None"
    )


# ---------------------------------------------------------------------------
# Shared timeout mechanics, but diverging shape (classic View, keeps content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_access_timeout_disables_revoke_and_keeps_the_config_block(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    from daimon.adapters.discord.agent_setup.mcp_access import EXPIRED_BUTTON_LABEL

    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-timeout",
    )

    ma_agent_id = "agent_017abc_77_timeout"
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot-timeout", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot-timeout")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    interaction_mint = _make_interaction()
    await send_coding_tools_access(
        interaction_mint, runtime=runtime, state=state, allowed_user_id=42
    )

    mint_kwargs = interaction_mint.response.send_message.call_args.kwargs
    sent_view = mint_kwargs["view"]

    await sent_view.on_timeout()

    interaction_mint.edit_original_response.assert_called_once()
    call_kwargs = interaction_mint.edit_original_response.call_args.kwargs
    assert "content" not in call_kwargs, (
        "the config block and its token must survive — no content override"
    )
    assert call_kwargs["view"] is sent_view, "the SAME instance is edited back, not a replacement"

    revoke_btn = _find_button(sent_view, EXPIRED_BUTTON_LABEL)
    assert revoke_btn.disabled is True, "the Revoke button must be disabled on timeout"
    assert sent_view.timeout == 300, "the shared on_timeout mixin leaves timeout values unchanged"


@pytest.mark.asyncio
async def test_revoke_stops_the_view_so_the_timer_cannot_fire(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    await _setup_tenant_and_account(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        external_id="test-guild-77-stop",
    )

    ma_agent_id = "agent_017abc_77_stop"
    real_agent = ma_agent(id=ma_agent_id, name="expert-bot-stop", tenant_id=tenant_id)

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    settings = _make_settings()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("expert-bot-stop")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    interaction_mint = _make_interaction()
    await send_coding_tools_access(
        interaction_mint, runtime=runtime, state=state, allowed_user_id=42
    )

    mint_kwargs = interaction_mint.response.send_message.call_args.kwargs
    sent_view = mint_kwargs["view"]
    revoke_btn = _find_button(sent_view, "Revoke")

    interaction_revoke = _make_interaction()
    await revoke_btn.callback(interaction_revoke)

    assert sent_view.is_finished() is True, (
        "revoke must stop the view so its timer cannot re-attach a button later"
    )

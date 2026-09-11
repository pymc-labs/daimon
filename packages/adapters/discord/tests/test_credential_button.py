"""Tests for CredentialRequestButton -- from_custom_id / interaction_check / callback."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import daimon.adapters.discord.credential_button as credential_button_mod
import discord
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.credential_button import CredentialRequestButton
from daimon.adapters.discord.credential_modals import (
    EnvCredentialModal,
    McpCredentialModal,
    RepoBindModal,
)
from daimon.adapters.discord.credential_repo_bind import _SHARED_AGENT_MESSAGE
from daimon.core.credential_requests import build_button_label, build_custom_id, mint_request_token
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_REQUESTER_ID = "100000000000000001"
_OTHER_USER_ID = "200000000000000002"

# Matches tests/agent_setup/test_authz.py's _admin_interaction /
# _member_interaction default guild_id -- every repo-kind gate test below
# must agree with this so it lands on the branch it names, not the
# wrong-guild one.
_GUILD_ID = 111


def _fake_bot(
    sessionmaker: Any,
    *,
    anthropic: Any = None,
    deployment_default: DeploymentDefault | None = None,
) -> Any:
    """A minimal stand-in for DaimonBot -- only
    `.runtime.{sessionmaker,anthropic,deployment_default}` are read."""
    return SimpleNamespace(
        runtime=SimpleNamespace(
            sessionmaker=sessionmaker,
            anthropic=anthropic,
            deployment_default=deployment_default or DeploymentDefault(),
        )
    )


def _interaction(*, user_id: str, client: Any) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = int(user_id)
    interaction.client = client
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    return interaction


def _repo_admin_interaction(*, client: Any, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.client = client
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 1
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _repo_member_interaction(*, client: Any, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.client = client
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 2
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _match(token: str) -> Any:
    custom_id = build_custom_id(token)
    matched = CredentialRequestButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test token must satisfy the button's own custom_id template"
    return matched


def _row(
    *,
    token: str,
    kind: Literal["env", "mcp", "repo"] = "env",
    target: str = "OPENAI_API_KEY",
    mcp_server_url: str | None = None,
    requester_platform_user_id: str = _REQUESTER_ID,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
) -> CredentialRequestRow:
    """Build a CredentialRequestRow in memory -- no DB needed for interaction_check/callback tests."""
    now = datetime.now(UTC)
    return CredentialRequestRow(
        token=token,
        kind=kind,
        tenant_id=tenant_id or uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        account_id=account_id or uuid.uuid4(),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=requester_platform_user_id,
        channel_id="chan-1",
        created_at=now,
        expires_at=expires_at or (now + timedelta(minutes=30)),
        used_at=used_at,
    )


def _make_agent(
    *, ma_agent_id: str, tenant_id: uuid.UUID, name: str, managed: bool
) -> BetaManagedAgentsAgent:
    metadata = {"daimon_tenant": str(tenant_id)}
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    return BetaManagedAgentsAgent(
        id=ma_agent_id,
        type="agent",
        name=name,
        model={"id": "claude-sonnet-4-6"},
        metadata=metadata,
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )


def _list_agents_handler(agents: list[BetaManagedAgentsAgent]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return list_response([agent.model_dump(mode="json") for agent in agents])

    return handler


async def _seed_repo_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    ma_agent_id: str,
    target: str = "github.com/o/repo-button",
    guild_id: int = _GUILD_ID,
) -> CredentialRequestRow:
    """Seed a real `kind="repo"` request row aligned to `_GUILD_ID`, with a
    real `accounts` row (the recorded `RepoAccessProof.account_id` an
    on_submit test writes carries an FK to `accounts.id`)."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=token,
            kind="repo",
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account.id,
            target=target,
            mcp_server_url=None,
            requester_platform_user_id=_REQUESTER_ID,
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return row


async def _seed_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: Literal["env", "mcp"] = "env",
    target: str = "OPENAI_API_KEY",
    mcp_server_url: str | None = None,
    requester_platform_user_id: str = _REQUESTER_ID,
) -> str:
    """Seed a real credential_requests row and return its token."""
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=f"guild-{token[:8]}")
        await create_credential_request(
            session,
            token=token,
            kind=kind,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            target=target,
            mcp_server_url=mcp_server_url,
            requester_platform_user_id=requester_platform_user_id,
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return token


# --- from_custom_id (real DB) -----------------------------------------------


async def test_from_custom_id_known_token_builds_button_with_target_label(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _seed_request(db_session_factory, kind="env", target="OPENAI_API_KEY")
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await CredentialRequestButton.from_custom_id(interaction, MagicMock(), _match(token))

    assert item.request_row is not None, "a known token must resolve its row"
    assert item.item.label == build_button_label("env", "OPENAI_API_KEY"), (
        "the reconstructed button must name the exact target"
    )


async def test_from_custom_id_unknown_token_returns_item_with_no_row_and_fallback_label(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    token = "never-minted-token-abcdef123456"

    item = await CredentialRequestButton.from_custom_id(interaction, MagicMock(), _match(token))

    assert item.request_row is None, "an unknown token must yield no row rather than raising"
    assert item.item.label == "Enter it privately", "fallback label is used when no row is found"


async def test_from_custom_id_db_failure_is_logged_and_interaction_check_rejects_gracefully(
    monkeypatch: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A DB failure during the lookup must be logged and must not raise -- the
    exception does not escape into discord.py's swallowing dispatcher."""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise SQLAlchemyError("connection reset")

    monkeypatch.setattr(credential_button_mod, "peek_credential_request", _boom)

    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    token = "db-failure-simulated-token-000001"

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        item = await CredentialRequestButton.from_custom_id(interaction, MagicMock(), _match(token))
    finally:
        structlog.reset_defaults()

    assert item.request_row is None, "a DB failure must not raise -- it degrades to no row"
    assert any(entry.get("event") == "credential_button.lookup_failed" for entry in cap.entries), (
        "the failure must be logged for observability, since discord.py's own logger "
        "would otherwise be the only trace of it"
    )

    check_interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    allowed = await item.interaction_check(check_interaction)

    assert allowed is False, "an item built from a failed lookup must reject the click"
    check_interaction.response.send_message.assert_awaited_once()


# --- interaction_check (in-memory row, no DB) -------------------------------


async def test_interaction_check_unknown_row_sends_ephemeral_and_rejects() -> None:
    item = CredentialRequestButton(
        token="unknown12345678901234", label="Add credential", request_row=None
    )
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "no row means the click must be rejected"
    interaction.response.send_message.assert_awaited_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "no longer valid" in message, "unknown-row rejection must tell the user to ask again"


async def test_interaction_check_wrong_requester_sends_ephemeral_and_rejects() -> None:
    row = _row(token="wrongrequester12345678", requester_platform_user_id=_REQUESTER_ID)
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_OTHER_USER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a non-requester click must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "someone else" in message, "rejection must indicate the request targeted another user"


async def test_interaction_check_expired_sends_ephemeral_and_rejects() -> None:
    row = _row(
        token="expiredrow123456789012",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an expired row must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "expired" in message, "rejection must say the request expired"


async def test_interaction_check_already_used_sends_ephemeral_and_rejects() -> None:
    row = _row(token="usedrow1234567890123456", used_at=datetime.now(UTC))
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an already-used row must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "already used" in message, "rejection must say the request was already used"


async def test_interaction_check_allows_requester_with_valid_row_and_sends_nothing() -> None:
    row = _row(token="validrow123456789012345", requester_platform_user_id=_REQUESTER_ID)
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is True, "the requester with a fresh, unused row must be allowed through"
    interaction.response.send_message.assert_not_awaited()


# --- callback ----------------------------------------------------------------


async def test_callback_dispatches_env_modal_for_env_kind() -> None:
    row = _row(token="envcallback1234567890123", kind="env")
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    bot = _fake_bot(MagicMock())
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, EnvCredentialModal), "env-kind rows must open EnvCredentialModal"


async def test_callback_dispatches_mcp_modal_for_mcp_kind() -> None:
    row = _row(
        token="mcpcallback1234567890123",
        kind="mcp",
        mcp_server_url="https://ext.example.com/mcp",
    )
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    bot = _fake_bot(MagicMock())
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, McpCredentialModal), "mcp-kind rows must open McpCredentialModal"


# --- callback (repo kind) ----------------------------------------------------


async def test_callback_repo_kind_admin_opens_repo_bind_modal_with_zero_ma_requests() -> None:
    row = _row(token="repoadmin1234567890123", kind="repo", target="github.com/o/r")
    item = CredentialRequestButton(token=row.token, label="Bind repo: o/r", request_row=row)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return list_response([])

    bot = _fake_bot(MagicMock(), anthropic=build_fake_anthropic(handler))
    interaction = _repo_admin_interaction(client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, RepoBindModal), "repo-kind rows must open RepoBindModal"
    assert len(calls) == 0, "the admin pre-filter must precede every MA request, costing zero I/O"


async def test_callback_repo_kind_member_against_defaults_managed_target_refuses_without_opening_modal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_button_managed"
    row = await _seed_repo_row(db_session_factory, ma_agent_id=ma_agent_id)
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="daimon", managed=True
    )
    bot = _fake_bot(
        db_session_factory, anthropic=build_fake_anthropic(_list_agents_handler([agent]))
    )
    item = CredentialRequestButton(token=row.token, label="Bind repo: o/r", request_row=row)
    interaction = _repo_member_interaction(client=bot)

    await item.callback(interaction)

    (
        interaction.response.send_modal.assert_not_awaited(),
        "a refused member must never see the token form",
    )
    assert interaction.response.send_message.call_args.args[0] == _SHARED_AGENT_MESSAGE, (
        "the pre-filter's refusal must name the shared-agent message specifically"
    )


async def test_callback_repo_kind_member_against_non_managed_non_reachable_target_opens_modal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_button_private"
    row = await _seed_repo_row(db_session_factory, ma_agent_id=ma_agent_id)
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="mine", managed=False
    )
    bot = _fake_bot(
        db_session_factory, anthropic=build_fake_anthropic(_list_agents_handler([agent]))
    )
    item = CredentialRequestButton(token=row.token, label="Bind repo: o/r", request_row=row)
    interaction = _repo_member_interaction(client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, RepoBindModal), (
        "a member binding a repo to their own, unshared agent must reach the modal"
    )


async def test_callback_repo_kind_pre_filter_timeout_opens_modal_and_submit_time_gate_still_refuses(
    monkeypatch: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The pair this test proves: a timed-out pre-filter trades UX (the
    refused member briefly sees the token field) for availability (the
    button still opens), and never trades away authorization -- the
    submit-time gate, driven by the real (unmocked) gate function, still
    refuses and writes nothing."""
    ma_agent_id = "agent_button_timeout"
    row = await _seed_repo_row(db_session_factory, ma_agent_id=ma_agent_id)
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="daimon", managed=True
    )
    runtime_anthropic = build_fake_anthropic(_list_agents_handler([agent]))
    bot = _fake_bot(db_session_factory, anthropic=runtime_anthropic)

    monkeypatch.setattr(credential_button_mod, "_PRE_FILTER_TIMEOUT_SECONDS", 0.01)

    async def _slow_gate(*_args: Any, **_kwargs: Any) -> bool:
        await asyncio.sleep(1)
        return False

    monkeypatch.setattr(
        credential_button_mod, "refuse_if_shared_and_not_admin_for_request", _slow_gate
    )

    item = CredentialRequestButton(token=row.token, label="Bind repo: o/r", request_row=row)
    interaction = _repo_member_interaction(client=bot)

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await item.callback(interaction)
    finally:
        structlog.reset_defaults()

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, RepoBindModal), "a timed-out pre-filter must still open the modal"
    assert any(
        entry.get("event") == "credential_button.repo_pre_filter_timed_out" for entry in cap.entries
    ), "the timeout must be logged so an operator can see the pre-filter eroding"

    submit_interaction = MagicMock()
    submit_interaction.response.defer = AsyncMock()
    submit_interaction.response.is_done.return_value = True
    submit_interaction.response.send_message = AsyncMock()
    submit_interaction.followup.send = AsyncMock()
    submit_interaction.guild_id = _GUILD_ID
    submit_interaction.user = MagicMock(spec=discord.Member)
    submit_interaction.user.id = 2
    submit_interaction.user.guild_permissions.administrator = False
    submit_interaction.user.guild_permissions.manage_guild = False
    submit_interaction.guild.owner_id = 999

    await sent_modal.on_submit(submit_interaction)

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
        consumed = await peek_credential_request(session, token=row.token)
    assert binding is None, "the submit-time gate must still refuse -- the write must never happen"
    assert consumed is not None and consumed.used_at is None, "a refused submit must burn no token"
    assert submit_interaction.followup.send.call_args.args[0] == _SHARED_AGENT_MESSAGE


# --- dispatch matching (no DB) -----------------------------------------------


def test_custom_id_that_does_not_fullmatch_template_never_dispatches() -> None:
    pattern = CredentialRequestButton.__discord_ui_compiled_template__
    assert pattern.fullmatch("not-a-credential-button-id") is None, (
        "an unrelated custom_id must never match this button's dispatch template"
    )
    assert pattern.fullmatch(build_custom_id("a" * 20)) is not None, (
        "a well-formed minted custom_id must match the dispatch template"
    )

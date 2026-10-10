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
    EnvFileModal,
    McpCredentialModal,
    RepoBindModal,
)
from daimon.adapters.discord.credential_repo_bind import _SHARED_AGENT_MESSAGE
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
    build_button_label,
    build_custom_id,
    mint_request_token,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.posted_controls import (
    ALREADY_USED_MESSAGE,
    EXPIRED_HEADLINE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
    expired_message,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response
from pydantic import SecretStr
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
    interaction.guild_id = _GUILD_ID
    interaction.client = client
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    return interaction


def _repo_admin_interaction(*, client: Any, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.client = client
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = int(_REQUESTER_ID)
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
    interaction.user.id = int(_REQUESTER_ID)
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _card_click(*, user_id: str) -> MagicMock:
    """A click on the card of a `with_card=True` row.

    `is_credential_interaction_valid` compares the thread, the parent channel
    and the card's own message id against the row, so a click that is meant
    to reach the expiry branch has to agree with all three.
    """
    interaction = _interaction(user_id=user_id, client=MagicMock())
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    interaction.message = MagicMock()
    interaction.message.id = 444
    _partial_card(interaction).edit = AsyncMock()
    return interaction


def _partial_card(interaction: MagicMock) -> MagicMock:
    """The partial message `edit_posted_card` re-renders, off the mock chain."""
    channel = interaction.client.get_partial_messageable.return_value
    return channel.get_partial_message.return_value  # pyright: ignore[reportAny]


def _card_text(view: discord.ui.LayoutView) -> str:
    """All text the rendered card shows, newline-joined."""
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


def _match(token: str) -> Any:
    custom_id = build_custom_id(token)
    matched = CredentialRequestButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test token must satisfy the button's own custom_id template"
    return matched


def _row(
    *,
    token: str,
    kind: Literal["env", "env_file", "mcp", "repo"] = "env",
    target: str = "OPENAI_API_KEY",
    mcp_server_url: str | None = None,
    requester_platform_user_id: str = _REQUESTER_ID,
    target_name: str | None = None,
    responder_name: str | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
    with_card: bool = False,
) -> CredentialRequestRow:
    """Build a CredentialRequestRow in memory -- no DB needed for interaction_check/callback tests.

    `with_card` gives the row the posted card the expiry flip below edits;
    without those ids there is no message to re-render and the edit is a
    no-op by design.
    """
    now = datetime.now(UTC)
    return CredentialRequestRow(
        idempotency_key=uuid.uuid4(),
        token=token,
        kind=kind,
        tenant_id=tenant_id or derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID)),
        agent_id=agent_id or uuid.uuid4(),
        account_id=account_id or uuid.uuid4(),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=requester_platform_user_id,
        channel_id="333" if with_card else "chan-1",
        platform="discord" if with_card else None,
        parent_channel_id="222" if with_card else None,
        origin_thread_id="333" if with_card else None,
        posted_message_id="444" if with_card else None,
        target_name=target_name,
        responder_name=responder_name,
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
    return ma_agent(
        id=ma_agent_id,
        name=name,
        metadata=metadata,
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
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="tester",
            requested_work=None,
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
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="tester",
            requested_work=None,
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
    assert message == NO_LONGER_VALID_MESSAGE, (
        "the refusal must be the shared card copy, so Discord and Slack cannot drift"
    )


async def test_interaction_check_wrong_requester_sends_ephemeral_and_rejects() -> None:
    row = _row(token="wrongrequester12345678", requester_platform_user_id=_REQUESTER_ID)
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_OTHER_USER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a non-requester click must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert message == WRONG_REQUESTER_MESSAGE, (
        "the refusal must be the shared card copy, so Discord and Slack cannot drift"
    )


async def test_interaction_check_expired_sends_ephemeral_and_rejects() -> None:
    row = _row(
        token="expiredrow123456789012",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        target_name="research-bot",
        responder_name="Daimon",
    )
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an expired row must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert message == expired_message(
        kind="env",
        agent_name="research-bot",
        responder_name="Daimon",
        target="OPENAI_API_KEY",
    ), "a late clicker must be told exactly what the card beside them now says"
    assert "research-bot" in message, "the way back must name the agent that was being set up"


async def test_interaction_check_already_used_sends_ephemeral_and_rejects() -> None:
    row = _row(token="usedrow1234567890123456", used_at=datetime.now(UTC))
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _interaction(user_id=_REQUESTER_ID, client=None)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an already-used row must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert message == ALREADY_USED_MESSAGE, (
        "the refusal must be the shared card copy, so Discord and Slack cannot drift"
    )


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


async def test_callback_dispatches_env_file_modal_for_env_file_kind() -> None:
    row = _row(token="envfilecallback12345678", kind="env_file", target=ENV_FILE_TARGET)
    item = CredentialRequestButton(token=row.token, label="Add keys from .env", request_row=row)
    bot = _fake_bot(MagicMock())
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, EnvFileModal), (
        "env_file-kind rows must open the upload form, not the single-value one"
    )


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


async def test_callback_repo_kind_member_unbound_target_refuses_modal(
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

    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


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
    submit_interaction.user.id = int(_REQUESTER_ID)
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


async def test_expired_click_flips_the_card() -> None:
    """The one person who could have used this card is the one who can retire it.

    Nothing sweeps expiries, so a card goes stale in the channel with its
    button still showing. The late click is the event that can still correct
    it, and the clicker's own refusal must not wait on that edit.
    """
    row = _row(
        token="expiredcard1234567890",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        target_name="research-bot",
        responder_name="Daimon",
        with_card=True,
    )
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _card_click(user_id=_REQUESTER_ID)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an expired row must still be rejected"
    interaction.client.get_partial_messageable.assert_called_once_with(333)
    interaction.client.get_partial_messageable.return_value.get_partial_message.assert_called_once_with(
        444
    )
    edit = _partial_card(interaction).edit
    edit.assert_awaited_once()
    card = _card_text(edit.call_args.kwargs["view"])
    assert EXPIRED_HEADLINE in card, "the card must now say the form expired"
    assert "research-bot" in card, "and name the agent, so the way back is on the card too"
    refusal = interaction.response.send_message.call_args.args[0]
    assert all(line in card for line in refusal.split("\n")), (
        "the card and the late clicker's refusal must say the same thing, line for line "
        f"(refusal={refusal!r}, card={card!r})"
    )


async def test_wrong_requester_click_does_not_edit() -> None:
    """Anyone in the channel can click; nobody else may retire the card."""
    row = _row(
        token="wrongrequestercard1234",
        requester_platform_user_id=_REQUESTER_ID,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        with_card=True,
    )
    item = CredentialRequestButton(token=row.token, label="Add credential", request_row=row)
    interaction = _card_click(user_id=_OTHER_USER_ID)

    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a non-requester click must be rejected"
    interaction.client.get_partial_messageable.assert_not_called()
    assert interaction.response.send_message.call_args.args[0] == WRONG_REQUESTER_MESSAGE, (
        "the wrong clicker is told whose request this is, and changes nothing"
    )


# --- callback (mcp_oauth kind): a private sign-in link, no modal ------------------


async def test_callback_mcp_oauth_kind_spends_the_request_and_sends_a_private_sign_in_link(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The OAuth card's click opens no modal: the request is spent atomically,
    a flow row is minted and only the requester gets the start link."""
    from daimon.core.stores import mcp_oauth_flows as flows_store
    from daimon.testing.crypto import make_fernet

    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=f"guild-{token[:8]}")
        row = await create_credential_request(
            session,
            token=token,
            kind="mcp_oauth",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_test"),
            account_id=uuid.uuid4(),
            target="notion",
            mcp_server_url="https://mcp.notion.com/mcp",
            requester_platform_user_id=_REQUESTER_ID,
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="daimon",
            requested_work=None,
        )
    agents = [ma_agent(id="ag_test", name="daimon", tenant_id=tenant.id).model_dump(mode="json")]
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(lambda _r: list_response(agents)),
        sessionmaker=db_session_factory,
        settings=SimpleNamespace(
            mcp=SimpleNamespace(app_root_url="https://d.example", jwt_secret=SecretStr("s" * 32))
        ),
        turn_deps=SimpleNamespace(fernet=make_fernet()),
    )
    bot = SimpleNamespace(runtime=runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    item = CredentialRequestButton(token=token, label="Connect", request_row=row)

    await item.callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True, "the link is for the requester alone"
    view = kwargs["view"]
    button = next(c for c in view.children if isinstance(c, discord.ui.Button))
    assert button.url is not None and button.url.startswith(
        "https://d.example/oauth/mcp/start?state="
    )
    state = button.url.rsplit("state=", 1)[1]
    async with db_session_factory() as session:
        flow = await flows_store.get_flow(session, state=state)
        spent = await peek_credential_request(session, token=token)
    assert flow is not None and flow.request_token == token, "the link points at a minted flow"
    assert spent is not None and spent.used_at is not None, "the request is spent on the click"


async def test_callback_mcp_oauth_kind_refuses_the_click_when_the_deployment_has_no_crypto_keys(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The sign-in routes only mount with crypto keys; without them the click must
    not spend the request on a link to nowhere."""
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=f"guild-{token[:8]}")
        row = await create_credential_request(
            session,
            token=token,
            kind="mcp_oauth",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            target="notion",
            mcp_server_url="https://mcp.notion.com/mcp",
            requester_platform_user_id=_REQUESTER_ID,
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="daimon",
            requested_work=None,
        )
    runtime = SimpleNamespace(
        sessionmaker=db_session_factory,
        settings=SimpleNamespace(
            mcp=SimpleNamespace(app_root_url="https://d.example", jwt_secret=SecretStr("s" * 32))
        ),
        turn_deps=SimpleNamespace(fernet=None),
    )
    interaction = _interaction(user_id=_REQUESTER_ID, client=SimpleNamespace(runtime=runtime))
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await CredentialRequestButton(token=token, label="Connect", request_row=row).callback(
        interaction
    )

    text = interaction.followup.send.call_args.args[0]
    assert "cannot sign you in" in text, "the person learns the operator must finish setup"
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=token)
    assert spent is not None and spent.used_at is None, "the request survives for a later click"

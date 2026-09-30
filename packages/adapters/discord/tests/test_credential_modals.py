"""Tests for EnvCredentialModal / McpCredentialModal / RepoBindModal -- atomic
consume then the existing write paths, with secret-hygiene assertions.

`_admin_interaction` / `_member_interaction` are copied verbatim from
`tests/agent_setup/test_authz.py` (same rationale as
`test_credential_repo_bind.py`'s copy: `tests/` carries no `__init__.py`, so
importing across sibling test files does not resolve when a file is run
alone). `_GUILD_ID` matches those builders' own default `guild_id=111` so
every seeded tenant and every interaction agree on the tenant a repo-bind
gate test resolves against — see `_seed_repo_request`'s docstring for why
that alignment matters."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.discord import credential_modals as credential_modals_mod
from daimon.adapters.discord import credential_repo_bind as credential_repo_bind_mod
from daimon.adapters.discord.credential_modals import (
    EnvCredentialModal,
    EnvFileModal,
    McpCredentialModal,
    RepoBindModal,
    SkillRepoModal,
)
from daimon.adapters.discord.credential_repo_bind import (
    _SHARED_AGENT_MESSAGE,
    _SHARED_AGENT_SKILLS_MESSAGE,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
    build_custom_id,
    build_skill_repo_target,
    mint_request_token,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.env_file import MAX_ENV_FILE_BYTES
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.posted_controls import RECEIVED_FOOTER
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores import scoped_config_write
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.agent_skill_repo_credentials import get_skill_repo_credential
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import (
    AgentFileRow,
    CredentialRequestRow,
    TaskContinuationRow,
    TenantRow,
)
from daimon.core.stores.seeded_skills import record_seeded_skill
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, build_stub_anthropic, list_response
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SECRET_VALUE = "super-secret-env-value-do-not-leak"
_MCP_TOKEN = "super-secret-mcp-token-do-not-leak"

# See the module docstring: matches tests/agent_setup/test_authz.py's
# _admin_interaction / _member_interaction default guild_id.
_GUILD_ID = 111


def _runtime(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: Any = None,
    public_url: HttpUrl | None = None,
    jwt_secret: str | None = None,
    crypto_keys: tuple[str, ...] = (),
    oauth_scopes: tuple[str, ...] = ("repo", "read:user"),
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = public_url
    if jwt_secret is not None:
        secret_mock = MagicMock()
        secret_mock.get_secret_value.return_value = jwt_secret
        settings.mcp.jwt_secret = secret_mock
    else:
        settings.mcp.jwt_secret = None
    # Defaults to no crypto configured; RepoBindModal tests that exercise
    # store_inline_pat / load_agent_inline_pat pass real crypto_keys so they
    # can round-trip through a real MultiFernet.
    settings.crypto.keys = tuple(MagicMock(get_secret_value=lambda k=k: k) for k in crypto_keys)
    settings.github.oauth_scopes = oauth_scopes
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic
        if anthropic is not None
        else build_stub_anthropic(
            _vault_handler(
                "unused",
                "unused",
                [],
                tenant_id=str(derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))),
            )
        ),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        # fernet is real: the MCP modal encrypts an agent-scoped copy of the token.
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # credential-modal tests never run a turn
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )


def _admin_interaction(*, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 100000000000000001
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.defer = AsyncMock()
    # RepoBindModal.on_submit always defers before the gate runs, so by the
    # time any ephemeral is sent the interaction is acked -- is_done() is
    # True, matching real Discord behaviour post-defer.
    interaction.response.is_done.return_value = True
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _member_interaction(*, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 100000000000000001
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.defer = AsyncMock()
    interaction.response.is_done.return_value = True
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


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


async def _seed_repo_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    ma_agent_id: str,
    target: str = "github.com/o/hygiene-repo",
    guild_id: int = _GUILD_ID,
) -> CredentialRequestRow:
    """Seed a `kind="repo"` request row, deliberately NOT mirroring
    `_seed_env_request`'s random `workspace_id=str(_GUILD_ID)` -- a repo
    bind's gate test must land on a tenant that matches the interaction
    builders' `guild_id`, or every gate-touching assertion below passes on
    the wrong-guild branch instead of the one it names.

    Seeds a real `accounts` row (not a bare `uuid.uuid4()`): the resolved
    `RepoAccessProof.account_id` written by `set_binding` carries an FK to
    `accounts.id`, so a fabricated account id would fail the write with an
    `IntegrityError` that has nothing to do with the behaviour under test.
    """
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
            requester_platform_user_id="100000000000000001",
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="tester",
            requested_work=None,
        )
    return row


async def _seed_skill_repo_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    ma_agent_id: str,
    target: str,
    guild_id: int = _GUILD_ID,
    with_origin: bool = False,
) -> CredentialRequestRow:
    """Seed a `kind="skill_repo"` request row. Same real-tenant/real-account
    reasoning as `_seed_repo_request`: `set_skill_repo_credential` writes a
    proof carrying an FK to `accounts.id`."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=token,
            kind="skill_repo",
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account.id,
            target=target,
            mcp_server_url=None,
            requester_platform_user_id="100000000000000001",
            channel_id="333" if with_origin else "chan-1",
            platform="discord" if with_origin else None,
            parent_channel_id="222" if with_origin else None,
            origin_thread_id="333" if with_origin else None,
            posted_message_id="444" if with_origin else None,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=ma_agent_id,
            target_name="tester",
            requested_work=None,
        )
    return row


async def _ensure_tenant(session: AsyncSession) -> TenantRow:
    """The `_GUILD_ID` install, created once however many rows a test seeds.

    `make_tenant` refuses a second insert for the same workspace, and several
    tests below seed two requests (an env form and an MCP form) in the one
    install they both have to agree on.
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    existing = await get_tenant(session, tenant_id)
    if existing is not None:
        return existing
    return await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))


def _sent_message(interaction: MagicMock) -> str:
    """Return the ephemeral text sent, on whichever half of the response fired."""
    if interaction.response.send_message.called:
        return str(interaction.response.send_message.call_args.args[0])
    return str(interaction.followup.send.call_args.args[0])


def _assert_secret_absent_from_every_reply(interaction: MagicMock, secret: str) -> None:
    """Pin T-18-11/T-18-16: the pasted value must never reach any surface an
    interaction mock recorded, across every response call the modal could
    have made -- not just the first positional string of the first call."""
    for mock_attr in (
        interaction.response.send_message,
        interaction.followup.send,
        interaction.edit_original_response,
    ):
        for call in mock_attr.call_args_list:
            for arg in call.args:
                assert secret not in str(arg), (
                    f"{mock_attr._mock_name} positional arg leaked the secret"
                )
            for value in call.kwargs.values():
                assert secret not in str(value), f"{mock_attr._mock_name} kwarg leaked the secret"


async def _seed_env_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    target: str = "OPENAI_API_KEY",
    expires_at: datetime | None = None,
    with_origin: bool = False,
    requested_work: str | None = None,
    replaces_updated_at: datetime | None = None,
) -> CredentialRequestRow:
    """Seed a `kind="env"` request row.

    `requested_work` is what the card promised to resume once the value
    lands, and `replaces_updated_at` is the exact `updated_at` of the value
    the card offered to replace -- the two frozen facts every truthful
    outcome below is decided from.
    """
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await _ensure_tenant(session)
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=token,
            kind="env",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            account_id=account.id,
            target=target,
            replaces_updated_at=replaces_updated_at,
            mcp_server_url=None,
            requester_platform_user_id="100000000000000001",
            channel_id="333" if with_origin else "chan-1",
            platform="discord" if with_origin else None,
            parent_channel_id="222" if with_origin else None,
            origin_thread_id="333" if with_origin else None,
            posted_message_id="444" if with_origin else None,
            expires_at=expires_at or (datetime.now(UTC) + timedelta(minutes=30)),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=_MA_AGENT_ID,
            target_name="tester",
            requested_work=requested_work,
        )
    return row


async def _seed_mcp_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    mcp_server_url: str = "https://ext.example.com/mcp",
    with_origin: bool = False,
    requested_work: str | None = None,
) -> CredentialRequestRow:
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await _ensure_tenant(session)
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=token,
            kind="mcp",
            tenant_id=tenant.id,
            # Derived, not random: the modal now resolves the MA agent by
            # re-deriving this uuid5, so a random value would make every
            # attach take the agent-not-found branch.
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            account_id=account.id,
            target="linear",
            mcp_server_url=mcp_server_url,
            requester_platform_user_id="100000000000000001",
            channel_id="333" if with_origin else "chan-1",
            platform="discord" if with_origin else None,
            parent_channel_id="222" if with_origin else None,
            origin_thread_id="333" if with_origin else None,
            posted_message_id="444" if with_origin else None,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=_MA_AGENT_ID,
            target_name="tester",
            requested_work=requested_work,
        )
    return row


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = _GUILD_ID
    interaction.user.id = 100000000000000001
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _as_card_interaction(interaction: MagicMock) -> MagicMock:
    """Point an interaction at the card of a row seeded `with_origin=True`.

    `is_credential_interaction_valid` compares the thread, parent and posted
    message against the row, so a card-carrying row needs an interaction that
    agrees with it; the partial-message chain is the seam
    `edit_posted_card` re-renders the card through. Applied to whichever
    builder a test needs -- the admin and member builders included, since
    the replacement gate reads the clicker's live guild permissions.
    """
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    _partial_card(interaction).edit = AsyncMock()
    return interaction


def _card_interaction() -> MagicMock:
    """A plain modal submit on a card-carrying row."""
    return _as_card_interaction(_interaction())


async def _queued_continuation(
    db_session_factory: async_sessionmaker[AsyncSession], row: CredentialRequestRow
) -> TaskContinuationRow | None:
    """The turn this request queued, if it queued one."""
    async with db_session_factory() as session:
        return await get_continuation(session, idempotency_key=row.idempotency_key)


async def _stored_key(
    db_session_factory: async_sessionmaker[AsyncSession],
    row: CredentialRequestRow,
    key: str,
) -> AgentFileRow | None:
    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    return next((file for file in files if file.key == key), None)


def _partial_card(interaction: MagicMock) -> MagicMock:
    """The partial message `edit_posted_card` re-renders, off the mock chain."""
    channel = interaction.client.get_partial_messageable.return_value
    return channel.get_partial_message.return_value  # pyright: ignore[reportAny]


def _card_edits(interaction: MagicMock) -> list[discord.ui.LayoutView]:
    """Every view the modal re-rendered the request's own card with."""
    return [call.kwargs["view"] for call in _partial_card(interaction).edit.call_args_list]


def _card_text(view: discord.ui.LayoutView) -> str:
    """All text the rendered card shows, newline-joined."""
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


def _card_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


# --- EnvCredentialModal ------------------------------------------------------


async def test_env_modal_submit_consumes_token_and_writes_agent_file(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="OPENAI_API_KEY")
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(rows) == 1, "exactly one agent_files row must be written"
    assert rows[0].key == "OPENAI_API_KEY", "the key must come from the consumed row's target"
    assert rows[0].content == _SECRET_VALUE, "the value comes from the modal's TextInput"

    interaction.followup.send.assert_not_awaited()
    _assert_secret_absent_from_every_reply(interaction, _SECRET_VALUE)


async def test_env_modal_successful_submit_edits_the_card_into_the_received_state(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="STRIPE_KEY", with_origin=True)
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with()
    edits = _card_edits(interaction)
    assert len(edits) == 2, (
        "the card moves twice: to received when the consume commits, then to what happened"
    )
    assert RECEIVED_FOOTER in _card_text(edits[0]), (
        "the received card must say the value arrived and is being saved"
    )
    assert "STRIPE_KEY saved for tester." in _card_text(edits[1]), (
        "the terminal card must report the write that actually landed"
    )
    assert _card_buttons(edits[0]) == [], "a spent request must offer no button to click again"
    assert _card_buttons(edits[1]) == [], "the terminal card offers no button either"


async def test_env_modal_rejected_value_leaves_the_request_button_live(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="NEVER_SET", with_origin=True)
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = "   "  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_not_awaited()
    assert _card_edits(interaction) == [], (
        "nothing was consumed, so the card must stay in its requested state"
    )


async def test_env_modal_dead_request_does_not_touch_the_button(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="ONE_SHOT", with_origin=True)
    runtime = _runtime(sessionmaker=db_session_factory)

    first = EnvCredentialModal(runtime=runtime, request_row=row)
    first.value_input._value = "first-value"  # pyright: ignore[reportPrivateUsage]
    await first.on_submit(_card_interaction())

    second = EnvCredentialModal(runtime=runtime, request_row=row)
    second.value_input._value = "second-value"  # pyright: ignore[reportPrivateUsage]
    second_interaction = _card_interaction()
    await second.on_submit(second_interaction)

    # The resubmission consumed nothing, so it must not redraw the card.
    second_interaction.edit_original_response.assert_not_awaited()
    assert _card_edits(second_interaction) == [], (
        "a resubmission on a spent request must leave the card as the first one left it"
    )


async def test_env_modal_stores_the_value_even_when_the_card_edit_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="EDIT_FAILS", with_origin=True)
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    _partial_card(interaction).edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "message deleted")
    )
    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(rows) == 1, "the secret is written before the card edit is attempted"
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "applied", (
        "a card that could not be redrawn is a feedback downgrade, never a failed submission"
    )


async def test_env_modal_double_submit_writes_exactly_one_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="TOGGL_TOKEN")
    runtime = _runtime(sessionmaker=db_session_factory)

    first_modal = EnvCredentialModal(runtime=runtime, request_row=row)
    first_modal.value_input._value = "first-value"  # pyright: ignore[reportPrivateUsage]
    await first_modal.on_submit(_interaction())

    second_modal = EnvCredentialModal(runtime=runtime, request_row=row)
    second_modal.value_input._value = "second-value"  # pyright: ignore[reportPrivateUsage]
    second_interaction = _interaction()
    await second_modal.on_submit(second_interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(rows) == 1, "a resubmission on an already-consumed token must write nothing new"
    assert rows[0].content == "first-value", "only the first submission's value is stored"

    message = second_interaction.followup.send.call_args.args[0]
    assert "no longer valid" in message, "the second submission must report the request is dead"


async def test_env_modal_expired_row_consumes_nothing_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(
        db_session_factory,
        target="EXPIRED_KEY",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert rows == [], "an expired row must never produce a write"


async def test_env_modal_empty_value_writes_nothing_and_does_not_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="EMPTY_KEY")
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = "   "  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert rows == [], "a blank value must never produce a write"
    message = interaction.followup.send.call_args.args[0]
    assert "empty" in message.lower(), "the toast must ask the user to try again"

    # The token must still be usable -- a fail-fast rejection did not consume it.
    retry_modal = EnvCredentialModal(runtime=runtime, request_row=row)
    retry_modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]
    await retry_modal.on_submit(_interaction())
    retry_rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(retry_rows) == 1, "the token must still be consumable after a validation rejection"


async def test_env_modal_value_over_byte_cap_writes_nothing_and_does_not_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="TOO_BIG_KEY")
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = "x" * 5000  # pyright: ignore[reportPrivateUsage]  # over the 4096-byte cap

    interaction = _interaction()
    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert rows == [], "an over-cap value must never produce a write"
    message = interaction.followup.send.call_args.args[0]
    assert "too large" in message.lower(), "the toast must report the size cap"

    retry_modal = EnvCredentialModal(runtime=runtime, request_row=row)
    retry_modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]
    await retry_modal.on_submit(_interaction())
    retry_rows = await list_agent_files(db_session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(retry_rows) == 1, "the token must still be consumable after a cap rejection"


async def test_env_modal_confirmation_states_shared_agent_exposure(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="SHARED_KEY", with_origin=True)
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    assert "Anyone who talks to tester can use it." in _card_text(_card_edits(interaction)[-1]), (
        "the card must disclose the credential is usable by every caller of the agent"
    )


async def test_env_modal_never_logs_the_secret_value(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="LOGGED_KEY")
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(_interaction())
    finally:
        structlog.reset_defaults()

    for entry in cap.entries:
        assert _SECRET_VALUE not in repr(entry), "no log line may contain the secret value"


# --- McpCredentialModal ------------------------------------------------------


_MA_AGENT_ID = "agent_01CredModal"


def _ma_agent_json(tenant_id: str, *, mcp_servers: list[dict[str, str]] | None = None) -> Any:
    """One MA agent payload, shaped as the SDK parses it."""
    return {
        "id": _MA_AGENT_ID,
        "type": "agent",
        "name": "test-agent",
        "description": None,
        "system": None,
        "model": {"id": "claude-sonnet-5"},
        "mcp_servers": mcp_servers if mcp_servers is not None else [],
        "skills": [],
        "tools": [],
        "metadata": {"daimon_tenant": tenant_id},
        "archived_at": None,
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "version": 1,
    }


def _vault_handler(
    vault_id: str,
    per_agent_display: str,
    creds_created: list[dict[str, Any]],
    *,
    tenant_id: str = "",
    agent_updates: list[dict[str, Any]] | None = None,
) -> Any:
    def _handler(req: httpx.Request) -> httpx.Response:
        # Agent routes back the attach half of the flow (#49): the modal must
        # add the server to the agent it just stored a credential for.
        if req.method == "GET" and req.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [_ma_agent_json(tenant_id)], "has_more": False}
            )
        if req.method == "GET" and req.url.path == f"/v1/agents/{_MA_AGENT_ID}":
            return httpx.Response(200, json=_ma_agent_json(tenant_id))
        if req.method == "POST" and req.url.path == f"/v1/agents/{_MA_AGENT_ID}":
            body = json.loads(req.content)
            if agent_updates is not None:
                agent_updates.append(body)
            return httpx.Response(
                200, json=_ma_agent_json(tenant_id, mcp_servers=body.get("mcp_servers") or [])
            )
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": vault_id,
                            "type": "vault",
                            "display_name": per_agent_display,
                            "metadata": None,
                            "archived_at": None,
                            "created_at": "2026-04-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == f"/v1/vaults/{vault_id}/credentials":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == f"/v1/vaults/{vault_id}/credentials":
            body = json.loads(req.content)
            creds_created.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_1",
                    "type": "credential",
                    "vault_id": vault_id,
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                },
            )
        raise AssertionError(f"unexpected: {req.method} {req.url.path}")

    return _handler


async def test_mcp_modal_submit_consumes_token_and_writes_vault_credential(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_mcp_request(
        db_session_factory, mcp_server_url="https://ext.example.com/mcp", with_origin=True
    )
    vault_id = "vlt_credmodal"
    per_agent_display = f"daimon-mcp:{row.account_id}:{row.agent_id}"
    creds_created: list[dict[str, Any]] = []
    agent_updates: list[dict[str, Any]] = []

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(
            _vault_handler(
                vault_id,
                per_agent_display,
                creds_created,
                tenant_id=str(row.tenant_id),
                agent_updates=agent_updates,
            )
        ),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    assert len(creds_created) == 1, "exactly one credential must be POSTed to the per-agent vault"
    assert creds_created[0]["auth"]["mcp_server_url"] == "https://ext.example.com/mcp", (
        "the mcp_server_url must come from the consumed row, never user input"
    )
    assert creds_created[0]["auth"]["token"] == _MCP_TOKEN

    assert len(agent_updates) == 1, (
        "the server must also be attached to the agent — a vault credential for a "
        "server the agent never declares is unreachable (#49)"
    )
    attached = agent_updates[0]
    assert {"name": "linear", "type": "url", "url": "https://ext.example.com/mcp"} in (
        attached["mcp_servers"]
    ), "the attached server takes its name from the consumed row and its url from the request"
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "linear"
        for t in attached["tools"]
    ), "MA rejects an mcp_servers entry with no matching mcp_toolset, so both must be written"

    card = _card_text(_card_edits(interaction)[-1])
    assert "tester is connected to linear." in card, "the card names what was connected"
    assert "its tools are available from your next message here" in card.lower(), (
        "the card must say when the connection becomes usable"
    )
    interaction.followup.send.assert_not_awaited()


async def test_mcp_modal_unconfigured_mcp_reports_misconfiguration_no_consume_no_vault_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    def _no_http(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no vault HTTP calls expected: {req.method} {req.url}")

    row = await _seed_mcp_request(db_session_factory)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_no_http),
        public_url=None,
        jwt_secret=None,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    message = interaction.followup.send.call_args.args[0]
    assert "Ask the operator to finish setup" in message, "must name who can fix setup"
    assert "Nothing was saved" in message, "the guard runs before any value is written"
    assert "public_url" not in message and "jwt_secret" not in message, (
        "internal setting names must not be shown to people"
    )

    # The token must still be unconsumed -- retry with configured settings must succeed.
    retry_runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(
            _vault_handler(
                "vlt_retry",
                f"daimon-mcp:{row.account_id}:{row.agent_id}",
                [],
                tenant_id=str(row.tenant_id),
            )
        ),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    retry_modal = McpCredentialModal(runtime=retry_runtime, request_row=row)
    retry_modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]
    retry_interaction = _interaction()
    await retry_modal.on_submit(retry_interaction)
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "applied", (
        "the request must still be consumable once daimon-mcp is configured"
    )
    retry_interaction.followup.send.assert_not_awaited()


async def test_mcp_modal_vault_write_failure_keeps_exception_details_private(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    def _failing_vault(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/agents":
            agent = ma_agent(
                id=_MA_AGENT_ID,
                name="test-agent",
                metadata={
                    "daimon_tenant": str(
                        derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
                    )
                },
            )
            return list_response([agent.model_dump(mode="json")])
        raise httpx.ConnectError("upstream reset by peer -- request envelope: secret=abc123")

    row = await _seed_mcp_request(db_session_factory)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_failing_vault),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    message = interaction.followup.send.call_args.args[0]
    assert "APIConnectionError" not in message, "exception class names stay in operator logs"
    assert "secret=abc123" not in message, (
        "the stringified exception (which can carry the request envelope) must never reach the user"
    )


async def test_mcp_modal_disables_the_button_even_when_the_write_below_it_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The consume is what kills the button, not the vault write after it.

    A downstream failure still leaves the request spent, so a button that
    still looks live would only ever earn an "already used" refusal.
    """

    def _failing_vault(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/agents":
            agent = ma_agent(
                id=_MA_AGENT_ID,
                name="test-agent",
                metadata={
                    "daimon_tenant": str(
                        derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
                    )
                },
            )
            return list_response([agent.model_dump(mode="json")])
        raise httpx.ConnectError("upstream reset by peer")

    row = await _seed_mcp_request(db_session_factory, with_origin=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_failing_vault),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    edits = _card_edits(interaction)
    assert RECEIVED_FOOTER in _card_text(edits[0]), (
        "a consumed request moves its card to received regardless of the write's outcome"
    )
    assert _card_buttons(edits[0]) == [], "the received card offers no button to click again"
    assert "Nothing was saved for tester." in _card_text(edits[-1]), (
        "a card left on 'Saving…' would describe a save that has already stopped"
    )
    message = interaction.followup.send.call_args.args[0]
    assert "saving the MCP token did not finish" in message, "the failure must still be reported"
    assert "APIConnectionError" not in message, "exception classes stay in operator logs"
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "write_failed", (
        "the spent request records that nothing was written"
    )
    assert await _queued_continuation(db_session_factory, row) is None, (
        "no turn may resume on a token that never reached a store"
    )


async def test_mcp_missing_server_url_records_write_failed_rather_than_staying_pending(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row that lost the server it named cannot be saved against anything.

    The consume already happened, so the request has to end somewhere: it
    ends as `write_failed`, and no continuation is queued, because there is
    no connection for a waiting turn to use.
    """
    row = await _seed_mcp_request(db_session_factory, mcp_server_url=None)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(
            _vault_handler(
                "vlt_no_url",
                f"daimon-mcp:{row.account_id}:{row.agent_id}",
                [],
                tenant_id=str(row.tenant_id),
            )
        ),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "missing its server URL" in _sent_message(interaction), (
        "the submitter is still told why nothing happened"
    )
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "write_failed", (
        "the spent request must not sit with no recorded outcome"
    )
    assert await _queued_continuation(db_session_factory, row) is None, (
        "nothing was saved, so no turn is owed"
    )


async def test_mcp_agent_gone_after_the_vault_write_renders_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The token landed; the agent it was for did not survive the form.

    Same shape as the attach failure below it — the save is real, the
    connection is not — so the card is `partial` rather than a refusal, and
    the continuation carries no work.
    """
    row = await _seed_mcp_request(
        db_session_factory, with_origin=True, requested_work="pull this week's open issues"
    )
    vault = _vault_handler(
        "vlt_agent_gone",
        f"daimon-mcp:{row.account_id}:{row.agent_id}",
        [],
        tenant_id=str(row.tenant_id),
    )

    # The agent survives the pre-consume target check and is gone by the time
    # the attach looks it up — the only window in which this branch is real.
    lookups = 0

    def _agent_gone(request: httpx.Request) -> httpx.Response:
        nonlocal lookups
        if request.method == "GET" and request.url.path == "/v1/agents":
            lookups += 1
            if lookups > 1:
                return httpx.Response(200, json={"data": [], "has_more": False})
        return vault(request)

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_agent_gone),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    card = _card_text(_card_edits(interaction)[-1])
    assert "linear token saved for tester." in card, "the card must credit what was saved"
    assert "The connection did not finish, so its tools are not available yet." in card, (
        "and must not claim a connection nothing could attach"
    )
    assert RECEIVED_FOOTER not in card, "the card must not stay on 'Saving…'"
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "write_failed", (
        "a connection that was never attached is not an applied request"
    )
    queued = await _queued_continuation(db_session_factory, row)
    assert queued is not None, "the spent request is still recorded for the trail"
    assert queued.requested_work is None, (
        "the waiting work must not resume against tools that are not connected"
    )


async def test_mcp_modal_never_logs_the_raw_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_mcp_request(db_session_factory)
    vault_id = "vlt_hygiene"
    per_agent_display = f"daimon-mcp:{row.account_id}:{row.agent_id}"
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(
            _vault_handler(vault_id, per_agent_display, [], tenant_id=str(row.tenant_id))
        ),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(_interaction())
    finally:
        structlog.reset_defaults()

    for entry in cap.entries:
        assert _MCP_TOKEN not in repr(entry), "no log line may contain the raw token"


# --- RepoBindModal ------------------------------------------------------


async def test_repo_modal_public_repo_blank_token_admin_binds_anon_against_managed_target(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Targeting a defaults-managed agent is deliberate: against a
    non-managed, non-reachable agent this would pass without ever exercising
    the admin branch, and would stay green even with the gate deleted."""
    ma_agent_id = "agent_admin_managed"
    monkeypatch.setattr(credential_repo_bind_mod, "is_public_repo", AsyncMock(return_value=True))
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/public-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    await modal.on_submit(_admin_interaction())

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None, "an admin binding a public repo to a shared agent must write a row"
    assert binding.repo_url == "o/public-repo"
    assert binding.default_branch == "main"
    assert binding.ma_secret_ref == "anon:"
    assert binding.proof_kind == "public"


async def test_repo_modal_public_repo_blank_token_member_binds_anon_against_own_target(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_member_private"
    monkeypatch.setattr(credential_repo_bind_mod, "is_public_repo", AsyncMock(return_value=True))
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/public-repo-2"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="mine", managed=False)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    await modal.on_submit(_member_interaction())

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None, (
        "a member binding a public repo to their own, unshared agent must succeed"
    )
    assert binding.ma_secret_ref == "anon:"
    assert binding.proof_kind == "public"


async def test_repo_modal_pasted_token_that_github_accepts_binds_inline_pat(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_admin_pat_ok"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/pat-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_accepted_token_xyz"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_admin_interaction())

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None
    assert binding.ma_secret_ref == f"inline-pat:{row.agent_id}"
    assert binding.proof_kind == "pat"


async def test_repo_modal_pasted_token_that_github_refuses_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_admin_pat_refused"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=False)
    )
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/pat-refused-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_refused_token_xyz"  # pyright: ignore[reportPrivateUsage]
    interaction = _admin_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is None, "a GitHub-refused token must never produce a binding"
    message = interaction.followup.send.call_args.args[0]
    assert "can't access this repo" in message, "the panel's own refusal copy must be surfaced"


async def test_repo_modal_double_submit_writes_exactly_one_binding(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_admin_resubmit"
    monkeypatch.setattr(credential_repo_bind_mod, "is_public_repo", AsyncMock(return_value=True))
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/resubmit-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )

    first_modal = RepoBindModal(runtime=runtime, request_row=row)
    await first_modal.on_submit(_admin_interaction())

    second_modal = RepoBindModal(runtime=runtime, request_row=row)
    second_interaction = _admin_interaction()
    await second_modal.on_submit(second_interaction)

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None, "the first submission must still have written its binding"

    message = second_interaction.followup.send.call_args.args[0]
    assert "no longer valid" in message, (
        "a resubmission on an already-consumed token must be refused"
    )


async def test_repo_modal_refusal_burns_no_token_and_reports_shared_agent_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_member_refused"
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/refused-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    interaction = _member_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
        consumed = await peek_credential_request(session, token=row.token)
    assert binding is None, "a refused member must never produce a binding"
    assert consumed is not None and consumed.used_at is None, (
        "a refusal must burn no token -- the gate runs before the consume"
    )
    assert _sent_message(interaction) == _SHARED_AGENT_MESSAGE, (
        "the refusal must name the shared-agent message specifically, not merely refuse"
    )


@pytest.mark.parametrize(
    "scenario", ["happy", "refused_by_github", "unexpected_exception", "refused_by_gate"]
)
async def test_repo_modal_never_leaks_the_pasted_token(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    scenario: str,
) -> None:
    """Parametrized secret-hygiene pin across all four submit-time outcomes,
    not the happy path alone -- T-18-11/T-18-16's whole point is that a leak
    on an error path ships green if only the happy path is tested."""
    hygiene_pat = "ghp_hygiene_do_not_leak_0000000000"
    ma_agent_id = f"agent_hygiene_{scenario}"
    managed = scenario == "refused_by_gate"
    row = await _seed_repo_request(
        db_session_factory, ma_agent_id=ma_agent_id, target="github.com/o/hygiene-repo"
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="bot", managed=managed)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    if scenario == "unexpected_exception":

        async def _boom(http_client: httpx.AsyncClient, *, owner_repo: str, pat: str) -> bool:
            raise httpx.ConnectError(f"upstream reset -- request envelope: token={pat}")

        monkeypatch.setattr(credential_repo_bind_mod, "pat_can_access_repo", _boom)
    elif scenario == "refused_by_github":
        monkeypatch.setattr(
            credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=False)
        )
    else:
        monkeypatch.setattr(
            credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
        )

    modal = RepoBindModal(runtime=runtime, request_row=row)
    modal.pat_in._value = hygiene_pat  # pyright: ignore[reportPrivateUsage]
    interaction = _member_interaction() if scenario == "refused_by_gate" else _admin_interaction()

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(interaction)
    finally:
        structlog.reset_defaults()

    for entry in cap.entries:
        assert hygiene_pat not in repr(entry), (
            f"[{scenario}] no log record may contain the pasted token"
        )
        pat_masked = entry.get("pat_masked")
        if pat_masked is not None:
            assert pat_masked != hygiene_pat, (
                f"[{scenario}] the mask itself must not equal the full value"
            )

    minted_custom_id = build_custom_id(row.token)
    assert hygiene_pat not in minted_custom_id, (
        f"[{scenario}] custom_id must never carry the pasted token"
    )

    _assert_secret_absent_from_every_reply(interaction, hygiene_pat)

    if scenario == "happy":
        async with db_session_factory() as session:
            binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
        assert binding is not None and binding.ma_secret_ref == f"inline-pat:{row.agent_id}"
    elif scenario == "refused_by_github":
        message = _sent_message(interaction)
        assert "can't access this repo" in message
    elif scenario == "unexpected_exception":
        message = _sent_message(interaction)
        assert "ConnectError" not in message, "exception classes stay in operator logs"
        assert "working repo did not finish" in message, "the failed operation must be clear"
    elif scenario == "refused_by_gate":
        assert _sent_message(interaction) == _SHARED_AGENT_MESSAGE
        assert not any(
            entry.get("event") == "credential_modal.repo.submit" for entry in cap.entries
        ), (
            "a gate refusal must never reach the submit log line -- the gate must run before it "
            "(this is the mutation this test pins: moving the log line above the gate turns it red)"
        )


# --- SkillRepoModal -----------------------------------------------------


async def test_skill_repo_submission_writes_the_skill_credential_not_the_working_repo_binding(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The pasted token lands as this repo's SKILL credential, and nowhere else.

    A later sync of the same repo resolves its token from that row, so it has
    to exist -- but writing an `agent_repo_binding` instead (or as well) would
    silently move the working repo the agent clones and runs, which is a
    separate decision with its own admin gate that a skill import must never
    make."""
    ma_agent_id = "agent_skill_repo_bind"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_modals_mod, "pat_can_access_repo", AsyncMock(return_value=True))
    monkeypatch.setattr(credential_modals_mod, "run_skill_sync", AsyncMock(return_value=[]))

    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/skills-repo", "main", ""),
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_skill_repo_token"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_admin_interaction())

    async with db_session_factory() as session:
        credential = await get_skill_repo_credential(
            session,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            repo_url="https://github.com/o/skills-repo",
        )
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert credential is not None, (
        "without this row the stored token is unreachable and every later sync of the "
        "skill repo falls back to an anonymous 404"
    )
    assert credential.ma_secret_ref == f"inline-pat:{row.agent_id}"
    assert credential.proof_kind == "pat"
    assert credential.default_branch == "main", "the branch comes from the request's own target"
    assert binding is None, (
        "a skill import must not change the working repo the agent clones and runs"
    )


async def test_skill_repo_modal_attaches_the_imported_skills_to_the_requested_agent(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Importing puts skills in the tenant library; the request names an agent,
    so the modal must also attach them. Import-without-attach leaves the user
    with an agent that has no skills and a success message saying otherwise."""
    ma_agent_id = "agent_skill_repo_attach"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_modals_mod, "pat_can_access_repo", AsyncMock(return_value=True))
    monkeypatch.setattr(
        credential_modals_mod,
        "run_skill_sync",
        AsyncMock(
            return_value=[
                ResourceOutcome(
                    kind="skill",
                    name="imported-skill",
                    action=Action.CREATED,
                    anthropic_id="skill_01imported",
                )
            ]
        ),
    )

    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/attach-repo", "main", ""),
        with_origin=True,
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=False)

    updates: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(agent.id):
            # Both the version-retry re-fetch and the update itself address the
            # agent directly and must parse as ONE agent; only the list route
            # gets the list envelope.
            if request.method in ("POST", "PATCH"):
                updates.append(json.loads(request.content))
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        return list_response([agent.model_dump(mode="json")])

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(handler),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_attach_token"  # pyright: ignore[reportPrivateUsage]
    interaction = _as_card_interaction(_admin_interaction())
    await modal.on_submit(interaction)

    assert updates, "the modal must call agents.update to attach the imported skills"
    attached_ids = {entry["skill_id"] for entry in updates[-1]["skills"]}
    assert "skill_01imported" in attached_ids, (
        "the newly imported skill must be attached to the agent the request named"
    )
    card = _card_text(_card_edits(interaction)[-1])
    assert "1 skill added to tester from o/attach-repo." in card, (
        "the card counts what was imported and names where it came from"
    )
    interaction.followup.send.assert_not_awaited()


async def test_skill_repo_modal_never_attaches_to_a_managed_agent_even_for_admin(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An attach never stamps the reconciler's spec hash, so the seeded agent
    would drift for good. The import still lands in the library."""
    ma_agent_id = "agent_skill_repo_seeded"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_modals_mod, "pat_can_access_repo", AsyncMock(return_value=True))
    monkeypatch.setattr(
        credential_modals_mod,
        "run_skill_sync",
        AsyncMock(
            return_value=[
                ResourceOutcome(
                    kind="skill", name="imported", action=Action.CREATED, anthropic_id="skill_01"
                )
            ]
        ),
    )
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/seeded-repo", "main", ""),
        with_origin=True,
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    updates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method in ("POST", "PATCH"):
            updates.append(request.url.path)
        if request.url.path.endswith(agent.id):
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        return list_response([agent.model_dump(mode="json")])

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(handler),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_seeded_token"  # pyright: ignore[reportPrivateUsage]
    interaction = _as_card_interaction(_admin_interaction())

    await modal.on_submit(interaction)

    assert updates == [], "the managed agent must not be updated"
    assert "built-in agent" in interaction.followup.send.call_args.args[0], (
        "the submitter is told why the skills were not attached"
    )
    card = _card_text(_card_edits(interaction)[-1])
    assert "but not added to tester" in card, "the card must not claim the agent has them"


async def test_skill_repo_modal_refuses_a_member_on_a_shared_agent_before_consuming(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A skill import reaches everyone a shared agent answers, so it needs an admin."""
    ma_agent_id = "agent_skill_repo_member"
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(credential_modals_mod, "run_skill_sync", sync)
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/member-repo", "main", ""),
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_member_token"  # pyright: ignore[reportPrivateUsage]
    interaction = _as_card_interaction(_member_interaction())

    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        consumed = await peek_credential_request(session, token=row.token)
        credential = await get_skill_repo_credential(
            session,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            repo_url="https://github.com/o/member-repo",
        )
    assert consumed is not None and consumed.used_at is None, "the refusal spends nothing"
    assert credential is None, "no token is stored for a refused member"
    assert not sync.called, "nothing is imported"
    assert _sent_message(interaction) == _SHARED_AGENT_SKILLS_MESSAGE, (
        "the refusal names the skills case, not the working-repo one"
    )


async def test_skill_repo_modal_refuses_a_member_on_a_reachable_unmanaged_agent(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Not built-in, but it answers for the whole server, so a member still needs an admin."""
    ma_agent_id = "agent_skill_repo_reachable"
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(credential_modals_mod, "run_skill_sync", sync)
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/reachable-repo", "main", ""),
    )
    async with db_session_factory() as session, session.begin():
        await scoped_config_write.set_fields(
            session,
            scope=TenantScopeRef(tenant_id=row.tenant_id),
            tenant_id=row.tenant_id,
            agent_name="shared",
        )
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="shared", managed=False
    )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_member_token"  # pyright: ignore[reportPrivateUsage]
    interaction = _as_card_interaction(_member_interaction())

    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        consumed = await peek_credential_request(session, token=row.token)
    assert consumed is not None and consumed.used_at is None, "the refusal spends nothing"
    assert not sync.called, "nothing is imported"
    assert _sent_message(interaction) == _SHARED_AGENT_SKILLS_MESSAGE


async def test_skill_repo_modal_passes_seeded_names_and_admin_status_to_the_sync(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The sync can only refuse seeded names and member replacements it is told about."""
    ma_agent_id = "agent_skill_repo_private"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_modals_mod, "pat_can_access_repo", AsyncMock(return_value=True))
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(credential_modals_mod, "run_skill_sync", sync)
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/private-repo", "main", ""),
    )
    async with db_session_factory() as session, session.begin():
        await record_seeded_skill(
            session, tenant_id=row.tenant_id, name="eda", content_hash="h", anthropic_id="sk_eda"
        )
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="private", managed=False
    )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_private_token"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_as_card_interaction(_member_interaction()))

    assert sync.call_args.kwargs["seeded_skill_names"] == frozenset({"eda"})
    assert sync.call_args.kwargs["is_admin"] is False


async def test_skill_repo_modal_puts_a_refused_import_on_the_card(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refusal is not a success: nothing attaches, and the card says why."""
    ma_agent_id = "agent_skill_repo_refused"
    monkeypatch.setattr(
        credential_repo_bind_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_modals_mod, "pat_can_access_repo", AsyncMock(return_value=True))
    reason = "skill name 'eda' belongs to a default skill. Rename the skill and re-sync."
    monkeypatch.setattr(
        credential_modals_mod,
        "run_skill_sync",
        AsyncMock(
            return_value=[
                ResourceOutcome(
                    kind="skill", name="eda", action=Action.FAILED, error="raw", refusal=reason
                )
            ]
        ),
    )
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id=ma_agent_id,
        target=build_skill_repo_target("https://github.com/o/refused-repo", "main", ""),
        with_origin=True,
    )
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=row.tenant_id, name="daimon", managed=False
    )
    updates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method in ("POST", "PATCH"):
            updates.append(request.url.path)
        return list_response([agent.model_dump(mode="json")])

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(handler),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_refused_token"  # pyright: ignore[reportPrivateUsage]
    interaction = _as_card_interaction(_admin_interaction())

    await modal.on_submit(interaction)

    card = _card_text(_card_edits(interaction)[-1])
    assert updates == [], "nothing imported, so nothing is attached"
    assert "The skills did not import." in card and reason in card
    assert "added to" not in card and "again to retry" not in card


@pytest.mark.parametrize("failure_stage", ["verification", "authorization", "import"])
async def test_skill_repo_failure_reports_only_confirmed_token_saves(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_skill_repo_request(
        db_session_factory,
        ma_agent_id="agent_skill_repo_failure",
        target=build_skill_repo_target("https://github.com/o/skills-repo", "main", ""),
    )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(
            lambda request: list_response(
                [
                    ma_agent(
                        id="agent_skill_repo_failure",
                        name="test-agent",
                        metadata={"daimon_tenant": str(row.tenant_id)},
                    ).model_dump(mode="json")
                ]
            )
        ),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    probes = 0

    def github_response(request: httpx.Request) -> httpx.Response:
        nonlocal probes
        if request.url.path == "/repos/o/skills-repo":
            probes += 1
            if failure_stage == "verification":
                raise httpx.ConnectError("private upstream detail", request=request)
            if failure_stage == "authorization" and probes == 2:
                return httpx.Response(403)
            return httpx.Response(200, json={"private": True})
        raise httpx.ConnectError("private upstream detail", request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(original_client, transport=httpx.MockTransport(github_response)),
    )
    modal = SkillRepoModal(runtime=runtime, request_row=row)
    modal.pat_in._value = "ghp_skill_failure_token"  # pyright: ignore[reportPrivateUsage]  # Discord input boundary
    interaction = _admin_interaction()

    await modal.on_submit(interaction)

    message = interaction.followup.send.call_args.args[0]
    async with db_session_factory() as session:
        credential = await get_skill_repo_credential(
            session,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            repo_url="https://github.com/o/skills-repo",
        )
        persisted = await peek_credential_request(session, token=row.token)
    if failure_stage == "import":
        assert credential is not None, "the skill credential was committed before import failed"
        assert "Token saved" in message, "an import failure must preserve confirmed save success"
        assert persisted is not None and persisted.outcome == "write_failed", (
            "a stored token whose skills never imported is not an applied request"
        )
    else:
        assert credential is None, "failed authorization must not store the token"
        assert "Token saved" not in message and "Token stored" not in message, (
            "a failure before storage must not claim that the token was saved"
        )
    assert "retry" in message, "a consumed request needs a concrete retry instruction"
    assert "private upstream detail" not in message and "ConnectError" not in message, (
        "unexpected exception details stay in operator logs"
    )


@pytest.mark.parametrize("wrong_install", [False, True])
async def test_env_submit_rechecks_requester_and_install_before_consuming(
    db_session_factory: async_sessionmaker[AsyncSession], wrong_install: bool
) -> None:
    row = await _seed_env_request(db_session_factory)
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE
    interaction = _interaction()
    if wrong_install:
        interaction.guild_id = 222
    else:
        interaction.user.id = 999
    await modal.on_submit(interaction)
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert persisted is not None and persisted.used_at is None, (
        "an invalid caller must not consume the request"
    )
    assert files == [], "an invalid caller must not write a credential"
    assert "no longer valid" in interaction.followup.send.call_args.args[0], (
        "submission must honestly refuse changed authority"
    )


@pytest.mark.parametrize("recreated", [False, True])
async def test_env_submit_never_saves_to_a_deleted_target_or_recreated_namesake(
    db_session_factory: async_sessionmaker[AsyncSession], recreated: bool
) -> None:
    row = await _seed_env_request(db_session_factory)
    namesake = ma_agent(
        id="agent_recreated",
        name="test-agent",
        metadata={"daimon_tenant": str(row.tenant_id)},
    )
    anthropic = build_fake_anthropic(
        lambda request: list_response([namesake.model_dump(mode="json")] if recreated else [])
    )
    modal = EnvCredentialModal(
        runtime=_runtime(sessionmaker=db_session_factory, anthropic=anthropic), request_row=row
    )
    modal.value_input._value = _SECRET_VALUE
    interaction = _interaction()
    await modal.on_submit(interaction)
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert persisted is not None and persisted.used_at is None, (
        "missing identity must not consume the request"
    )
    assert files == [], "a namesake must not receive the old target's credential"
    assert "no longer exists" in interaction.followup.send.call_args.args[0], (
        "missing target needs a specific explanation"
    )


async def test_env_submit_updates_stored_card_when_modal_payload_omits_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, with_origin=True)
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE
    interaction = _interaction()
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    partial_channel = interaction.client.get_partial_messageable.return_value
    card = partial_channel.get_partial_message.return_value
    card.edit = AsyncMock()
    await modal.on_submit(interaction)
    interaction.client.get_partial_messageable.assert_called_with(333)
    partial_channel.get_partial_message.assert_called_with(444)
    assert card.edit.await_count == 2, "the card moves to received, then to applied"
    interaction.edit_original_response.assert_not_awaited()
    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert len(files) == 1, (
        "an authenticated modal remains valid without an optional message payload"
    )


# --- EnvFileModal ------------------------------------------------------------


async def _seed_env_file_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    with_origin: bool = True,
) -> CredentialRequestRow:
    """Seed a `kind="env_file"` request row, carrying its card by default:
    the file form's only success receipt is the card it edits, so a row with
    no posted message would make the interesting assertion unobservable."""
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
        row = await create_credential_request(
            session,
            token=token,
            kind="env_file",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            account_id=uuid.uuid4(),
            target=ENV_FILE_TARGET,
            mcp_server_url=None,
            requester_platform_user_id="100000000000000001",
            channel_id="333" if with_origin else "chan-1",
            platform="discord" if with_origin else None,
            parent_channel_id="222" if with_origin else None,
            origin_thread_id="333" if with_origin else None,
            posted_message_id="444" if with_origin else None,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="tester",
            requested_work=None,
        )
    return row


def _uploaded(content: bytes, *, size: int | None = None) -> MagicMock:
    """One uploaded attachment, faked at the platform boundary.

    `size` is what Discord announces in the interaction payload, which the
    form reads before downloading anything; it defaults to the truth.
    """
    attachment = MagicMock(spec=discord.Attachment)
    attachment.size = len(content) if size is None else size
    attachment.read = AsyncMock(return_value=content)
    return attachment


def _env_file_modal(
    *,
    runtime: DiscordRuntime,
    row: CredentialRequestRow,
    attachment: MagicMock | None,
) -> EnvFileModal:
    modal = EnvFileModal(runtime=runtime, request_row=row)
    modal.file_input._values = [] if attachment is None else [attachment]  # pyright: ignore[reportPrivateUsage]
    return modal


async def test_env_file_oversize_attachment_is_refused_before_download(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_file_request(db_session_factory)
    attachment = _uploaded(b"TOGGL_TOKEN=abc\n", size=MAX_ENV_FILE_BYTES + 1)
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory), row=row, attachment=attachment
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    attachment.read.assert_not_awaited()
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert persisted is not None and persisted.used_at is None, (
        "a file too large to read costs nothing -- the request must stay clickable"
    )
    assert files == [], "nothing may be written from a file that was never downloaded"
    assert _card_edits(interaction) == [], "the card must stay in its requested state"
    assert "too big" in _sent_message(interaction), (
        "the person needs to know the file itself is the problem"
    )


async def test_env_file_parse_error_does_not_consume_the_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_file_request(db_session_factory)
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory),
        row=row,
        attachment=_uploaded(b"TOGGL_TOKEN\nOPENAI_API_KEY=ok\n"),
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert persisted is not None and persisted.used_at is None, (
        "a typo in the file must not burn the one click this card is good for"
    )
    assert files == [], "a rejected file is rejected whole -- not even its readable line lands"
    assert _card_edits(interaction) == [], (
        "nothing was consumed, so the card must stay in its requested state"
    )
    message = _sent_message(interaction)
    assert "line 1" in message, "the rejection names the line that could not be read"
    assert "No keys were saved for tester" in message, "the rejection names the agent"


async def test_env_file_collision_refuses_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_file_request(db_session_factory)
    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            key="TOGGL_TOKEN",
            content="the-value-already-stored",
            set_by_account_id=None,
        )
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory),
        row=row,
        attachment=_uploaded(b"TOGGL_TOKEN=replacement-value\nNEW_KEY=brand-new-value\n"),
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert [file.key for file in files] == ["TOGGL_TOKEN"], (
        "one colliding key refuses the whole file -- the other key must not land alone"
    )
    assert files[0].content == "the-value-already-stored", (
        "the key that was already set must keep the value it had"
    )
    assert persisted is not None and persisted.outcome == "stale_replacement", (
        "the spent request must record that it refused rather than applied"
    )

    edits = _card_edits(interaction)
    assert len(edits) == 1, "a refused upload re-renders its own card exactly once"
    card = _card_text(edits[0])
    assert "TOGGL_TOKEN" in card and "line 1" in card, (
        "the refusal names the colliding key and where it was in the file"
    )
    assert "replacement-value" not in card and "the-value-already-stored" not in card, (
        "no value may reach the card -- it is public to the channel"
    )
    assert "TOGGL_TOKEN" in _sent_message(interaction), (
        "the uploader gets the same refusal without having to read the card"
    )


async def test_env_file_valid_upload_writes_all_keys_atomically_and_edits_card_applied(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_file_request(db_session_factory)
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory),
        row=row,
        attachment=_uploaded(
            b"# our keys\nTOGGL_TOKEN=toggl-secret\nexport OPENAI_API_KEY='openai-secret'\n"
        ),
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert {file.key: file.content for file in files} == {
        "OPENAI_API_KEY": "openai-secret",
        "TOGGL_TOKEN": "toggl-secret",
    }, "every key in an accepted file lands, with the value the file gave it"
    assert persisted is not None and persisted.outcome == "applied", (
        "the spent request must record that the import landed"
    )

    edits = _card_edits(interaction)
    assert len(edits) == 1, "an applied upload re-renders its own card exactly once"
    card = _card_text(edits[0])
    assert "2 keys saved for tester" in card, "the card counts what was saved"
    assert "toggl-secret" not in card and "openai-secret" not in card, "no value may reach the card"
    interaction.followup.send.assert_not_awaited()


async def test_env_file_key_appearing_mid_write_rolls_back_the_whole_file(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The compare-and-set that fails on the second key must undo the first.

    A key can be created between the collision read and the write, and the
    store reports that as a failed precondition rather than raising. Without
    the savepoint the file would land half-applied -- the one failure mode
    nobody notices, because the agent simply behaves as if one key were
    never given.
    """
    row = await _seed_env_file_request(db_session_factory)
    real_put = credential_modals_mod.put_agent_file_if_unchanged
    seen: list[str] = []

    async def _fails_on_the_second_key(session: AsyncSession, **kwargs: Any) -> Any:
        seen.append(str(kwargs["key"]))
        if len(seen) == 1:
            return await real_put(session, **kwargs)
        return None

    monkeypatch.setattr(
        credential_modals_mod, "put_agent_file_if_unchanged", _fails_on_the_second_key
    )
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory),
        row=row,
        attachment=_uploaded(b"FIRST_KEY=first-value\nSECOND_KEY=second-value\n"),
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert seen == ["FIRST_KEY", "SECOND_KEY"], "the write loop must reach the failing key"
    assert files == [], "the key written before the failure must be rolled back with it"
    assert persisted is not None and persisted.used_at is not None, (
        "the request stays spent: the savepoint rolls back the writes, not the consume"
    )
    assert persisted.outcome == "stale_replacement", (
        "a key that appeared mid-write is recorded the same way a collision is"
    )
    card = _card_text(_card_edits(interaction)[0])
    assert "SECOND_KEY" in card, "the refusal names the key that was already set"
    assert "second-value" not in card and "first-value" not in card, "no value may reach the card"


# --- submission outcomes: continuation, replacement, dispatch ----------------


async def test_env_submission_records_a_private_input_continuation_in_the_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The value and the follow-up turn it unblocks commit together, or not at all.

    A continuation queued without its value would run a billed turn against a
    key the agent does not have; a value stored without its continuation would
    leave the person waiting for a turn nobody will run.
    """
    row = await _seed_env_request(
        db_session_factory,
        target="TOGGL_TOKEN",
        with_origin=True,
        requested_work="chart last month's tracked hours",
    )
    runtime = _runtime(sessionmaker=db_session_factory)

    async def _write_fails(session: AsyncSession, **kwargs: Any) -> Any:
        raise RuntimeError("the write blew up")

    monkeypatch.setattr(credential_modals_mod, "put_agent_file_if_unchanged", _write_fails)
    doomed = EnvCredentialModal(runtime=runtime, request_row=row)
    doomed.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]
    await doomed.on_submit(_card_interaction())

    assert await _queued_continuation(db_session_factory, row) is None, (
        "a write that rolled back must leave no turn queued behind it"
    )
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.used_at is None, (
        "the consume rolls back with the write it shares a transaction with"
    )

    monkeypatch.undo()
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_card_interaction())

    queued = await _queued_continuation(db_session_factory, row)
    assert queued is not None, "an applied value owes exactly one queued turn"
    assert queued.status == "pending", "the modal queues the turn; it never claims it"
    assert queued.reason == "private_input_applied", (
        "the reason distinguishes this from a task handoff's continuation"
    )
    assert queued.idempotency_key == row.idempotency_key, (
        "the request's own key is what makes the queued turn at-most-once"
    )
    assert queued.requested_work == "chart last month's tracked hours", (
        "the turn resumes the work the card promised, in the person's own words"
    )
    assert queued.thread_id == "333" and queued.parent_channel_id == "222", (
        "the turn is addressed to the thread the card was posted in"
    )
    assert queued.target_ma_agent_id == _MA_AGENT_ID, (
        "the destination is the concrete agent the row froze, never a name"
    )
    stored = await _stored_key(db_session_factory, row, "TOGGL_TOKEN")
    assert stored is not None and stored.content == _SECRET_VALUE, (
        "the value the turn needs landed in the same transaction"
    )


async def test_env_submission_records_none_requested_work_for_a_save_only_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing was waiting on this key, so the card promises no turn."""
    row = await _seed_env_request(db_session_factory, target="SAVE_ONLY", with_origin=True)
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    queued = await _queued_continuation(db_session_factory, row)
    assert queued is not None and queued.requested_work is None, (
        "a save-only request is still recorded, carrying no work to resume"
    )
    card = _card_text(_card_edits(interaction)[-1])
    assert "SAVE_ONLY saved for tester." in card, "the card reports the key that landed"
    assert "next message" not in card, (
        "with nothing waiting on it, the card must not promise a turn"
    )


async def test_stale_replacement_leaves_the_stored_value_alone_and_renders_superseded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The card promised to replace a specific value; that value already moved."""
    row = await _seed_env_request(
        db_session_factory,
        target="ROTATING_KEY",
        with_origin=True,
        requested_work="rerun the export with the new key",
        replaces_updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            key="ROTATING_KEY",
            content="the-value-someone-else-wrote",
            set_by_account_id=None,
        )
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _as_card_interaction(_admin_interaction())
    await modal.on_submit(interaction)

    stored = await _stored_key(db_session_factory, row, "ROTATING_KEY")
    assert stored is not None and stored.content == "the-value-someone-else-wrote", (
        "a failed precondition must leave the current value exactly as it was"
    )
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "stale_replacement", (
        "the spent request records that it replaced nothing"
    )
    assert await _queued_continuation(db_session_factory, row) is None, (
        "no value landed, so no turn may resume the work waiting on it"
    )
    card = _card_text(_card_edits(interaction)[-1])
    assert "ROTATING_KEY was not replaced for tester." in card, (
        "the card must say the replacement did not happen"
    )
    assert "Someone else changed it while this form was open." in card, "and say why"
    assert _SECRET_VALUE not in card, "no value may reach the card"
    assert "was not replaced" in _sent_message(interaction), (
        "the submitter is told the same thing without having to read the card"
    )


async def test_replacement_needs_admin_at_submit_renders_refused_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replacing a key on a shared agent is re-decided when the form is submitted.

    The precondition itself is satisfiable here -- the stored value is exactly
    the one the card froze -- so only the gate can stop this write, which is
    the mutation this test pins.
    """
    async with db_session_factory() as session, session.begin():
        tenant = await _ensure_tenant(session)
        stored = await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            key="SHARED_SECRET",
            content="the-value-in-use",
            set_by_account_id=None,
        )
    row = await _seed_env_request(
        db_session_factory,
        target="SHARED_SECRET",
        with_origin=True,
        requested_work="rerun the nightly export",
        replaces_updated_at=stored.updated_at,
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=_MA_AGENT_ID, tenant_id=tenant_id, name="daimon", managed=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(_list_agents_handler([agent])),
    )
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _as_card_interaction(_member_interaction())
    await modal.on_submit(interaction)

    current = await _stored_key(db_session_factory, row, "SHARED_SECRET")
    assert current is not None and current.content == "the-value-in-use", (
        "a refused replacement must not touch the value in use"
    )
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.used_at is not None, (
        "the click is spent either way: the gate runs after the consume"
    )
    assert persisted.outcome == "write_failed", "the trail records that nothing was written"
    assert await _queued_continuation(db_session_factory, row) is None, (
        "a refused write owes no turn"
    )
    card = _card_text(_card_edits(interaction)[-1])
    assert "SHARED_SECRET was not replaced for tester." in card, (
        "the card must say the replacement was refused"
    )
    assert "An admin can ask" in card, "and say who can make it"
    assert "was not replaced" in _sent_message(interaction), (
        "the submitter gets the refusal in their own reply too"
    )


async def test_mcp_attach_failure_renders_partial_and_records_an_audit_continuation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stored token whose server never attached is partial, not applied.

    The continuation is still recorded -- the request ended somewhere -- but
    it carries no work: resuming a task that needs those tools would fail on
    a connection the agent does not have.
    """
    row = await _seed_mcp_request(
        db_session_factory, with_origin=True, requested_work="pull this week's open issues"
    )
    vault = _vault_handler(
        "vlt_attach_fail",
        f"daimon-mcp:{row.account_id}:{row.agent_id}",
        [],
        tenant_id=str(row.tenant_id),
    )

    def _attach_fails(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/v1/agents/{_MA_AGENT_ID}":
            raise httpx.ConnectError("upstream reset by peer")
        return vault(request)

    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_attach_fails),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    card = _card_text(_card_edits(interaction)[-1])
    assert "linear token saved for tester." in card, "the card must credit what was saved"
    assert "The connection did not finish, so its tools are not available yet." in card, (
        "and must not claim the connection the attach never made"
    )
    assert _MCP_TOKEN not in card, "no token may reach the card"
    async with db_session_factory() as session:
        persisted = await peek_credential_request(session, token=row.token)
    assert persisted is not None and persisted.outcome == "write_failed", (
        "a half-finished connection is not an applied request"
    )
    queued = await _queued_continuation(db_session_factory, row)
    assert queued is not None, "the spent request is still recorded for the trail"
    assert queued.requested_work is None, (
        "the waiting work must not resume against tools that are not connected"
    )


async def test_success_paths_send_no_ephemeral_receipt(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The card is the receipt; an ephemeral would say the same thing twice."""
    env_row = await _seed_env_request(db_session_factory, target="NO_TOAST", with_origin=True)
    env_modal = EnvCredentialModal(
        runtime=_runtime(sessionmaker=db_session_factory), request_row=env_row
    )
    env_modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]
    env_interaction = _card_interaction()
    await env_modal.on_submit(env_interaction)
    env_interaction.followup.send.assert_not_awaited()
    assert "NO_TOAST saved for tester." in _card_text(_card_edits(env_interaction)[-1]), (
        "sanity: the card this receipt was dropped in favour of actually rendered"
    )

    mcp_row = await _seed_mcp_request(db_session_factory, with_origin=True)
    mcp_runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(
            _vault_handler(
                "vlt_no_toast",
                f"daimon-mcp:{mcp_row.account_id}:{mcp_row.agent_id}",
                [],
                tenant_id=str(mcp_row.tenant_id),
            )
        ),
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
    )
    mcp_modal = McpCredentialModal(runtime=mcp_runtime, request_row=mcp_row)
    mcp_modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]
    mcp_interaction = _card_interaction()
    await mcp_modal.on_submit(mcp_interaction)
    mcp_interaction.followup.send.assert_not_awaited()
    assert "tester is connected to linear." in _card_text(_card_edits(mcp_interaction)[-1]), (
        "sanity: the card this receipt was dropped in favour of actually rendered"
    )


async def test_dispatch_is_spawned_after_the_card_edit_on_the_origin_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The follow-up turn is handed to the bot, never awaited inside the form.

    Awaiting a billed turn here would blow the interaction's lifetime long
    before the turn finished, and it must not start until the card the person
    is watching already says what happened.
    """
    row = await _seed_env_request(
        db_session_factory,
        target="DISPATCHED_KEY",
        with_origin=True,
        requested_work="finish the export you started",
    )
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    thread = MagicMock(spec=discord.Thread)
    interaction.client.get_channel = MagicMock(return_value=thread)
    interaction.client.dispatch_continuations_in_thread = MagicMock()
    interaction.client._spawn = MagicMock()
    order = MagicMock()
    order.attach_mock(_partial_card(interaction).edit, "card_edit")
    order.attach_mock(interaction.client._spawn, "spawn")

    await modal.on_submit(interaction)

    interaction.client.get_channel.assert_called_once_with(333)
    interaction.client.dispatch_continuations_in_thread.assert_called_once_with(
        tenant_id=row.tenant_id, thread=thread, guild_id=str(_GUILD_ID)
    )
    interaction.client._spawn.assert_called_once_with(
        interaction.client.dispatch_continuations_in_thread.return_value
    )
    assert [call[0] for call in order.mock_calls] == ["card_edit", "card_edit", "spawn"], (
        "the turn is only handed over once the card already reports the write"
    )


async def test_dispatch_is_skipped_when_the_origin_is_no_longer_a_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A destination that is not a thread cannot host a turn, so none is spawned."""
    row = await _seed_env_request(
        db_session_factory,
        target="GONE_THREAD_KEY",
        with_origin=True,
        requested_work="finish the export you started",
    )
    modal = EnvCredentialModal(runtime=_runtime(sessionmaker=db_session_factory), request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    interaction.client.get_channel = MagicMock(return_value=None)
    interaction.client.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown channel")
    )
    interaction.client._spawn = MagicMock()

    await modal.on_submit(interaction)

    interaction.client._spawn.assert_not_called()
    stored = await _stored_key(db_session_factory, row, "GONE_THREAD_KEY")
    assert stored is not None, "the value still landed; only its follow-up could not be started"


async def test_env_file_upload_queues_its_continuation_with_the_keys(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A whole file lands under the same rule one key does: keys and turn together."""
    row = await _seed_env_file_request(db_session_factory)
    modal = _env_file_modal(
        runtime=_runtime(sessionmaker=db_session_factory),
        row=row,
        attachment=_uploaded(b"TOGGL_TOKEN=toggl-secret\n"),
    )

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    queued = await _queued_continuation(db_session_factory, row)
    assert queued is not None and queued.reason == "private_input_applied", (
        "an accepted file owes the same queued turn a single key does"
    )
    assert queued.requested_work is None, "this card carried no work to resume"
    card = _card_text(_card_edits(interaction)[-1])
    assert "1 key saved for tester." in card, "the card counts what landed"
    assert "next message" not in card, "nothing was waiting on the file, so no turn is promised"


async def test_mcp_modal_refuses_a_token_the_server_rejects_before_any_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 401/403 from the server at the door: nothing stored, nothing attached,
    the card refused with the way out named (the case a Notion token hit)."""
    import dataclasses

    from daimon.core.mcp_oauth import McpProbe

    row = await _seed_mcp_request(
        db_session_factory, mcp_server_url="https://mcp.notion.com/mcp", with_origin=True
    )
    creds_created: list[dict[str, Any]] = []
    agent_updates: list[dict[str, Any]] = []
    probed: list[tuple[str, str]] = []

    async def probe(url: str, token: str) -> McpProbe:
        probed.append((url, token))
        return McpProbe(status_code=403, resource_metadata_url=None)

    runtime = dataclasses.replace(
        _runtime(
            sessionmaker=db_session_factory,
            anthropic=build_stub_anthropic(
                _vault_handler(
                    "vlt_probe",
                    f"daimon-mcp:{row.account_id}:{row.agent_id}",
                    creds_created,
                    tenant_id=str(row.tenant_id),
                    agent_updates=agent_updates,
                )
            ),
            public_url=HttpUrl("https://mcp.example.com/mcp"),
            jwt_secret="x" * 32,
        ),
        mcp_token_probe=probe,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = _MCP_TOKEN  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction()
    await modal.on_submit(interaction)

    assert probed == [("https://mcp.notion.com/mcp", _MCP_TOKEN)], "the server is asked first"
    assert creds_created == [], "a rejected token is never written to a vault"
    assert agent_updates == [], "and the server is never attached"
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=row.token)
    assert spent is not None and spent.outcome == "token_rejected"
    text = interaction.followup.send.call_args.args[0]
    assert "did not accept" in text and "connect it with your account" in text, (
        "the person learns the token was refused and that OAuth is the way out"
    )

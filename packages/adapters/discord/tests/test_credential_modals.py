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
    McpCredentialModal,
    RepoBindModal,
    SkillRepoModal,
)
from daimon.adapters.discord.credential_repo_bind import _SHARED_AGENT_MESSAGE
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import (
    build_custom_id,
    build_skill_repo_target,
    mint_request_token,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
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
        anthropic=anthropic if anthropic is not None else build_stub_anthropic(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # credential-modal tests never run a turn
    )


def _admin_interaction(*, guild_id: int = _GUILD_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 1
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
    interaction.user.id = 2
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


async def _seed_repo_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    ma_agent_id: str,
    target: str = "github.com/o/hygiene-repo",
    guild_id: int = _GUILD_ID,
) -> CredentialRequestRow:
    """Seed a `kind="repo"` request row, deliberately NOT mirroring
    `_seed_env_request`'s random `workspace_id=f"g-{token[:8]}"` -- a repo
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
        )
    return row


async def _seed_skill_repo_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    ma_agent_id: str,
    target: str,
    guild_id: int = _GUILD_ID,
) -> CredentialRequestRow:
    """Seed a `kind="skill_repo"` request row. Same real-tenant/real-account
    reasoning as `_seed_repo_request`: `set_binding` writes a proof carrying an
    FK to `accounts.id`."""
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
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return row


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
) -> CredentialRequestRow:
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=f"g-{token[:8]}")
        row = await create_credential_request(
            session,
            token=token,
            kind="env",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            target=target,
            mcp_server_url=None,
            requester_platform_user_id="100000000000000001",
            channel_id="chan-1",
            expires_at=expires_at or (datetime.now(UTC) + timedelta(minutes=30)),
        )
    return row


async def _seed_mcp_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    mcp_server_url: str = "https://ext.example.com/mcp",
) -> CredentialRequestRow:
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=f"g-{token[:8]}")
        row = await create_credential_request(
            session,
            token=token,
            kind="mcp",
            tenant_id=tenant.id,
            # Derived, not random: the modal now resolves the MA agent by
            # re-deriving this uuid5, so a random value would make every
            # attach take the agent-not-found branch.
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            account_id=uuid.uuid4(),
            target="linear",
            mcp_server_url=mcp_server_url,
            requester_platform_user_id="100000000000000001",
            channel_id="chan-1",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    return row


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


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

    toast = interaction.followup.send.call_args.args[0]
    assert "OPENAI_API_KEY" in toast, "confirmation names the key"
    assert _SECRET_VALUE not in toast, "confirmation never echoes the secret value"


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
    row = await _seed_env_request(db_session_factory, target="SHARED_KEY")
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = _SECRET_VALUE  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    toast = interaction.followup.send.call_args.args[0]
    assert "anyone who talks to this agent" in toast, (
        "confirmation must disclose the credential is usable by every caller of the agent"
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
    row = await _seed_mcp_request(db_session_factory, mcp_server_url="https://ext.example.com/mcp")
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

    interaction = _interaction()
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

    toast = interaction.followup.send.call_args.args[0]
    assert "anyone who talks to this agent" in toast.lower(), (
        "confirmation must disclose the credential is usable by every caller of the agent"
    )


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
    assert "not configured" in message, "must report the daimon-mcp misconfiguration"

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
    retry_message = retry_interaction.followup.send.call_args.args[0]
    assert "not configured" not in retry_message, (
        "the request must still be consumable once daimon-mcp is configured"
    )


async def test_mcp_modal_vault_write_failure_surfaces_only_exception_class_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    def _failing_vault(req: httpx.Request) -> httpx.Response:
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
    assert "APIConnectionError" in message, "only the exception class name is surfaced"
    assert "secret=abc123" not in message, (
        "the stringified exception (which can carry the request envelope) must never reach the user"
    )


async def test_mcp_modal_never_logs_the_raw_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_mcp_request(db_session_factory)
    vault_id = "vlt_hygiene"
    per_agent_display = f"daimon-mcp:{row.account_id}:{row.agent_id}"
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(_vault_handler(vault_id, per_agent_display, [])),
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
        assert "ConnectError" in message, "only the exception class name must be surfaced"
    elif scenario == "refused_by_gate":
        assert _sent_message(interaction) == _SHARED_AGENT_MESSAGE
        assert not any(
            entry.get("event") == "credential_modal.repo.submit" for entry in cap.entries
        ), (
            "a gate refusal must never reach the submit log line -- the gate must run before it "
            "(this is the mutation this test pins: moving the log line above the gate turns it red)"
        )


# --- SkillRepoModal -----------------------------------------------------


async def test_skill_repo_modal_binds_the_repo_so_a_later_sync_can_resolve_the_pat(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The pasted token must produce an agent_repo_binding row, not just a
    credential. The skill-sync resolver walks this tenant's bindings FOR THIS
    REPO to find a per-agent PAT, so without the row the stored token is
    unreachable and every later sync falls back to an anonymous 404."""
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
        binding = await get_binding(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None, (
        "a pasted skill-repo token must bind the repo -- without the binding the "
        "credential is stored but unreachable, which is the loop this pins"
    )
    assert binding.ma_secret_ref == f"inline-pat:{row.agent_id}"
    assert binding.proof_kind == "pat"


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
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant_id, name="daimon", managed=True)

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
    await modal.on_submit(_admin_interaction())

    assert updates, "the modal must call agents.update to attach the imported skills"
    attached_ids = {entry["skill_id"] for entry in updates[-1]["skills"]}
    assert "skill_01imported" in attached_ids, (
        "the newly imported skill must be attached to the agent the request named"
    )

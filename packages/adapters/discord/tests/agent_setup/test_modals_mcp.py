"""Tests for AddMcpModal agent_uuid resolution and per-agent vault write (66-03).

Covers:
- Happy path: credential lands in daimon-mcp:{account_id}:{agent_uuid} vault
- Missing agent (find_agent_by_daimon_tag returns None) → ephemeral error, no vault write
- Unconfigured MCP (public_url or jwt_secret is None) → ephemeral error, no vault write
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import modals_mcp as modals_mcp_mod
from daimon.adapters.discord.agent_setup.modals import AddMcpModal
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_mcp_credentials as cred_store
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MA_AGENT_ID = "agent_mcp_modal_test_1234"


def _entry(name: str, *, is_system: bool = False) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6"),
        is_system=is_system,
    )


def _fake_ma_agent(tenant_id: uuid.UUID) -> BetaManagedAgentsAgent:
    """Real SDK BetaManagedAgentsAgent — no model_construct, no MagicMock."""
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id=_MA_AGENT_ID,
        type="agent",
        name="my-agent",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        created_at=now,
        updated_at=now,
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "my-agent"},
        mcp_servers=[],
        tools=[],
        skills=[],
    )


def _runtime(
    *,
    anthropic: Any,
    public_url: HttpUrl | None,
    jwt_secret: str | None,
    sessionmaker: Any = None,
    deployment_default: DeploymentDefault | None = None,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = public_url
    if jwt_secret is not None:
        _secret_value = jwt_secret

        jwt_secret_mock = MagicMock()
        jwt_secret_mock.get_secret_value.return_value = _secret_value
        settings.mcp.jwt_secret = jwt_secret_mock
    else:
        settings.mcp.jwt_secret = None
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
        billing_config=None,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        deployment_default=deployment_default
        if deployment_default is not None
        else DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        # fernet is real: the MCP modal encrypts an agent-scoped copy of the token.
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # never runs a turn
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )


def _runtime_configured(
    *,
    anthropic: Any,
    sessionmaker: Any = None,
    deployment_default: DeploymentDefault | None = None,
) -> DiscordRuntime:
    """Runtime with mcp.public_url and mcp.jwt_secret both set."""
    return _runtime(
        anthropic=anthropic,
        public_url=HttpUrl("https://mcp.example.com/mcp"),
        jwt_secret="x" * 32,
        sessionmaker=sessionmaker,
        deployment_default=deployment_default,
    )


def _interaction(user_id: int = 42, *, is_admin: bool = True, guild_id: int = 12345) -> MagicMock:
    """A discord.Interaction stand-in, a live guild admin by default so the
    write-callback's authz gate short-circuits without a DB read. Tests
    proving the reachable/system-agent refusal pass ``is_admin=False``."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = is_admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _noop_reconcile() -> Any:
    from daimon.core.defaults.report import Action, ResourceOutcome

    async def _impl(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return ResourceOutcome(
            kind="agent", name="my-agent", action=Action.UPDATED, anthropic_id="agent_x"
        )

    return _impl


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_mcp_modal_resolves_agent_uuid_and_writes_per_agent_vault(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: credential lands in daimon-mcp:{account_id}:{agent_uuid} vault.

    The vault handler checks the display_name so this assertion verifies that
    agent_uuid is derived correctly and the vault is looked up by the right key.
    """
    # The modal now persists an agent-scoped copy of the token, which FKs to
    # tenants — seed the row the tenant_id fixture names.
    async with db_session_factory() as _session, _session.begin():
        await make_tenant(_session, id=tenant_id, workspace_id="guild-mcp-modal-vault")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID)
    per_agent_display = f"daimon-mcp:{account_id}:{agent_uuid}"

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", _noop_reconcile())

    async def fake_find(client: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return _fake_ma_agent(tenant_id)

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", fake_find)

    vault_id = "vlt_per_agent"
    creds_created: list[dict[str, Any]] = []

    def vault_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            import json as _json

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
            import json as _json

            body = _json.loads(req.content)
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

    rt = _runtime_configured(
        anthropic=build_stub_anthropic(vault_handler), sessionmaker=db_session_factory
    )
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_xxxx_1234"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert len(creds_created) == 1, "exactly one credential must be POSTed to the per-agent vault"
    assert creds_created[0]["auth"]["mcp_server_url"] == "https://ext.example.com/mcp", (
        "credential must target the submitted MCP server URL"
    )
    assert creds_created[0]["auth"]["token"] == "tok_xxxx_1234", (
        "credential token must be the user-submitted value"
    )
    # The vault write alone only serves the submitter. The agent-scoped row is
    # what lets every OTHER caller's session mount the same credential.
    async with db_session_factory() as session:
        stored = await cred_store.list_credentials(
            session, tenant_id=tenant_id, agent_id=agent_uuid
        )
    assert [row.mcp_server_url for row in stored] == ["https://ext.example.com/mcp"], (
        "the modal must persist an agent-scoped copy for other callers' sessions"
    )


@pytest.mark.asyncio
async def test_add_mcp_modal_agent_not_found_sends_ephemeral_error_no_vault_write(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """When find_agent_by_daimon_tag returns None, an ephemeral error is sent
    and no vault credential write occurs."""
    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", _noop_reconcile())

    async def agent_not_found(client: Any, *, tenant_id: uuid.UUID, name: str) -> None:
        return None

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", agent_not_found)

    vault_calls: list[tuple[str, str]] = []

    def vault_handler(req: httpx.Request) -> httpx.Response:
        vault_calls.append((req.method, req.url.path))
        raise AssertionError(f"unexpected vault call: {req.method} {req.url.path}")

    rt = _runtime_configured(anthropic=build_stub_anthropic(vault_handler))
    selected = _entry("missing-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_yyyy_5678"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    content = str(interaction.followup.send.call_args)
    assert "missing-agent" in content, (
        "ephemeral error must mention the agent name so the user knows which lookup failed"
    )
    assert vault_calls == [], "no vault API calls must occur when agent lookup fails"


@pytest.mark.asyncio
async def test_add_mcp_modal_unconfigured_mcp_sends_ephemeral_error_no_vault_write(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """When runtime has no mcp.public_url/jwt_secret, an ephemeral error is sent
    and no vault write occurs — the MCP server URL credential cannot be written
    without the daimon-mcp bootstrap parameters."""
    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", _noop_reconcile())

    async def fake_find(client: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return _fake_ma_agent(tenant_id)

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", fake_find)

    vault_calls: list[tuple[str, str]] = []

    def vault_handler(req: httpx.Request) -> httpx.Response:
        vault_calls.append((req.method, req.url.path))
        raise AssertionError(f"unexpected vault call: {req.method} {req.url.path}")

    # Runtime with NO mcp.public_url — cannot bootstrap the per-agent vault.
    rt = _runtime(
        anthropic=build_stub_anthropic(vault_handler),
        public_url=None,
        jwt_secret=None,
    )
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_zzzz_9012"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    content = str(interaction.followup.send.call_args)
    assert "daimon-mcp" in content or "configured" in content, (
        "ephemeral error must indicate MCP is not configured"
    )
    assert vault_calls == [], "no vault API calls must occur when MCP is unconfigured"


# ---------------------------------------------------------------------------
# #142 panel guard tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_mcp_modal_rejects_reserved_name_ephemeral_no_mutation_no_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Submitting server name 'daimon-mcp' is rejected ephemerally
    before defer — no state mutation, no reconcile call, zero MA/vault HTTP calls."""
    # Record any reconcile call as a failure
    reconcile_calls: list[str] = []

    async def recording_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        reconcile_calls.append("called")
        raise AssertionError("reconcile must not be called on reserved-name rejection")

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", recording_reconcile)

    # Any HTTP call (MA or vault) should fail the test
    def no_http(req: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"no MA/vault HTTP calls expected on reserved-name rejection: {req.method} {req.url}"
        )

    rt = _runtime_configured(anthropic=build_stub_anthropic(no_http))
    selected = _entry("my-agent")
    initial_mcp_servers = list(selected.spec.mcp_servers or [])
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "daimon-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://any.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_test_1234"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    # Must send an ephemeral rejection
    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, (
        "rejection must be ephemeral so only the submitter sees it"
    )
    message = str(call_kwargs)
    assert "reserved built-in daimon server" in message, (
        "rejection must cite the reserved server policy"
    )

    # defer must NOT have been called (guard fires before defer)
    interaction.response.defer.assert_not_called()

    # state must be unchanged
    assert list(state.selected.spec.mcp_servers or []) == initial_mcp_servers, (
        "mcp_servers state must be unchanged on reserved-name rejection"
    )

    # reconcile must not have been called
    assert reconcile_calls == [], "reconcile must not be invoked on reserved-name rejection"


@pytest.mark.asyncio
async def test_add_mcp_modal_rejects_own_endpoint_url_trailing_slash_variant(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Submitting a URL equal to public_url (trailing-slash variant)
    is rejected ephemerally before defer — no reconcile, no vault write."""
    reconcile_calls: list[str] = []

    async def recording_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        reconcile_calls.append("called")
        raise AssertionError("reconcile must not be called on own-endpoint rejection")

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", recording_reconcile)

    def no_http(req: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"no MA/vault HTTP calls expected on own-endpoint rejection: {req.method} {req.url}"
        )

    rt = _runtime_configured(anthropic=build_stub_anthropic(no_http))
    selected = _entry("my-agent")
    initial_mcp_servers = list(selected.spec.mcp_servers or [])
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    # trailing-slash variant of "https://mcp.example.com/mcp" (from _runtime_configured)
    modal.url_in._value = "https://mcp.example.com/mcp/"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_test_5678"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, (
        "rejection must be ephemeral so only the submitter sees it"
    )
    message = str(call_kwargs)
    assert "deployment's own MCP endpoint" in message, "rejection must cite the own-endpoint policy"

    interaction.response.defer.assert_not_called()

    assert list(state.selected.spec.mcp_servers or []) == initial_mcp_servers, (
        "mcp_servers state must be unchanged on own-endpoint rejection"
    )

    assert reconcile_calls == [], "reconcile must not be invoked on own-endpoint rejection"


@pytest.mark.asyncio
async def test_add_mcp_modal_unrelated_server_proceeds_past_guard(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """An unrelated server name + URL passes the guard and reaches defer.

    The negative case: the guard must not over-block legitimate MCP servers.
    We assert that defer IS called, confirming the guard allowed the request through.
    The test short-circuits after reconcile via a recorded noop so we don't need
    a full vault stack for this assertion.
    """
    reconcile_calls: list[str] = []

    async def recording_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        from daimon.core.defaults.report import Action, ResourceOutcome

        reconcile_calls.append("called")
        return ResourceOutcome(
            kind="agent", name="my-agent", action=Action.UPDATED, anthropic_id="agent_x"
        )

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", recording_reconcile)

    # After reconcile succeeds the modal calls find_agent_by_daimon_tag for vault write;
    # stub it to return None so the modal exits cleanly via the "agent not found" branch.
    async def agent_not_found(client: Any, *, tenant_id: uuid.UUID, name: str) -> None:
        return None

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", agent_not_found)

    rt = _runtime_configured(anthropic=build_stub_anthropic())
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "totally-unrelated"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://different.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_test_9012"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    # Guard must not have sent an ephemeral rejection
    interaction.response.send_message.assert_not_called()
    # defer MUST have been called — the guard allowed the request through
    interaction.response.defer.assert_called_once()
    # reconcile was reached
    assert reconcile_calls == ["called"], "unrelated server must proceed to reconcile"


# ---------------------------------------------------------------------------
# Modal submit returns to the launching view (EditView), not AgentSetupView
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_mcp_submit_returns_to_edit_view(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AddMcpModal.on_submit must return the user to EditView, not a nested AgentSetupView.

    The vault write is made to fail (500) so this test doesn't need to model a
    full vault bootstrap — modals_mcp.py's on_submit falls through to the final
    edit_original_response regardless of vault-write outcome.
    """
    from daimon.adapters.discord.agent_setup.edit_view import EditView

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", _noop_reconcile())

    async def fake_find(client: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return _fake_ma_agent(tenant_id)

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", fake_find)

    def vault_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom", "type": "api_error"}})

    rt = _runtime_configured(
        anthropic=build_stub_anthropic(vault_handler), sessionmaker=db_session_factory
    )
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=False,
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=99)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_zzzz_9999"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_called_once()
    view_kwarg = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), (
        "AddMcpModal submit must return to EditView, not AgentSetupView"
    )
    assert view_kwarg.allowed_user_id == 99, "the invoker gate must survive the round trip"


# ---------------------------------------------------------------------------
# Click-time authz gate — the guard sits above the #142 reserved
# name/own-endpoint checks and below them alphabetically: reserved-name still
# fires first (see test_add_mcp_modal_rejects_reserved_name_...), and a
# non-reserved, reachable or system target is refused before defer/reconcile.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_mcp_modal_refuses_write_when_target_is_reachable_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.testing.factories import make_tenant

    async def unexpected_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", unexpected_reconcile)

    guild_id = 910003
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=guild_id,
        is_admin=False,
    )
    rt = _runtime_configured(
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="my-agent"),
    )

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_test_1234"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    assert not (state.selected.spec.mcp_servers or []), (
        "an MCP entry must not be appended when the write is refused"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_mcp_modal_refuses_write_on_system_agent_even_for_admin(
    monkeypatch: pytest.MonkeyPatch, account_id: uuid.UUID
) -> None:
    async def unexpected_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", unexpected_reconcile)

    selected = _entry("daimon", is_system=True)
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_id=12345,
        is_admin=True,
    )
    rt = _runtime_configured(anthropic=build_stub_anthropic())

    modal = AddMcpModal(state, runtime=rt, allowed_user_id=42)
    modal.name_in._value = "ext-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ext.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "tok_test_5678"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=True)
    await modal.on_submit(interaction)

    assert not (state.selected.spec.mcp_servers or []), (
        "a system agent's mcp_servers must never gain an entry from the panel"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

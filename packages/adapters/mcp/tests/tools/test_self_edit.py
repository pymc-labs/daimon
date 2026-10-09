"""DB-backed unit tests for self-edit MCP tools.

Plan 02 covers the 4 ``agent_files`` tools.
Plan 03 adds the 3 ``agent_repo_binding`` tools, exercising the full
mint → vault.create → DB write → vault.delete-old orchestration via a
transport-level ``httpx.MockTransport`` against a real ``AsyncAnthropic``
(per ``guideline:testing``).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.self_edit import (
    REDACTED_VALUE,
    AgentRepoBindingPublic,
    _clear_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
    _get_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
    _self_delete_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_list_files_impl,  # pyright: ignore[reportPrivateUsage]
    _self_read_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_write_file_impl,  # pyright: ignore[reportPrivateUsage]
    _set_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
    register_self_edit_tools,
)
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import seed_tenant


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client or MagicMock(spec=AsyncAnthropic),
        settings=MagicMock(),  # type: ignore[arg-type]  # tests inject only what they exercise
        deployment_default=DeploymentDefault(),
    )


def _runtime_with_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    managed: bool = False,
    default_name: str | None = None,
    display_name: str = "project-agent",
    routing_name: str | None = None,
) -> tuple[McpRuntime, uuid.UUID]:
    """A runtime whose MA lists one agent for the tenant; returns its derived id.

    `self_delete_file` resolves the calling agent to decide whether a member
    may remove its keys, so these tests need an agent that actually resolves.
    `managed` makes it the built-in agent; `default_name` makes it reachable
    tenant-wide as the deployment default.
    """
    from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import DeploymentDefault as _DeploymentDefault
    from daimon.testing.ma import build_fake_anthropic, list_response
    from daimon.testing.ma_models import ma_agent

    metadata: dict[str, str] = {}
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    if routing_name is not None:
        metadata[MA_METADATA_KEY_NAME] = routing_name
    agent = ma_agent(
        id="ag_self",
        name=display_name,
        tenant_id=tenant_id,
        metadata=metadata or None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([agent.model_dump(mode="json")])
        raise AssertionError(f"unexpected MA request {request.method} {request.url.path}")

    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(handler),
        settings=MagicMock(),  # type: ignore[arg-type]  # tests inject only what they exercise
        deployment_default=_DeploymentDefault(agent_name=default_name)
        if default_name
        else _DeploymentDefault(),
    )
    return runtime, derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_self")


async def _seed_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Insert a Tenant row via the given factory and return its id."""
    async with sessionmaker.begin() as session:
        return await seed_tenant(session)


async def _seed_tenant_and_fixed_account(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> uuid.UUID:
    """Seed a tenant plus an accounts row at a caller-chosen id.

    Needed by any test whose bind reaches a successful ``set_binding`` call:
    the write now threads a ``RepoAccessProof`` carrying ``account_id``, and
    the row's ``proof_account_id`` FK requires that account to already exist.
    """
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        await make_account(session, tenant=tenant, id=account_id)
        return tenant.id


_UNSET: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _auth_identity(
    *,
    account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = _UNSET,
    is_admin: bool = True,
) -> AuthIdentity:
    """Build an AuthIdentity for tool tests.

    ``agent_id`` defaults to a fresh UUID. Pass ``agent_id=None`` to test the
    null-agent_id guard (the impl must raise ``ToolError`` before touching the DB).
    ``is_admin`` defaults to True — these tests exercise admin-context tool flows.
    Pass ``is_admin=False`` to test the admin gate.
    """
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=Role.USER,
        agent_id=uuid.uuid4() if agent_id is _UNSET else agent_id,
        is_admin=is_admin,
    )


# ---------------------------------------------------------------------------
# Plan 02: agent_files tool tests
# ---------------------------------------------------------------------------


async def test_self_write_then_read_round_trip(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id)

    written = await _self_write_file_impl(runtime, auth, key="CONFIG_TOKEN", content="hello: world")
    assert written.key == "CONFIG_TOKEN", "write must persist the requested key"
    assert written.content == "hello: world", "write must persist the requested content"

    read = await _self_read_file_impl(runtime, auth, key="CONFIG_TOKEN")
    assert read is not None, "read of just-written key must hit"
    assert read.key == "CONFIG_TOKEN", "read must return the stored row"
    assert read.content == REDACTED_VALUE, "values never come back as tool output"


async def test_self_list_files_returns_only_caller_partition(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """SC-3: cross-agent isolation enforced by composite-PK alone."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    runtime = _runtime(committing_sessionmaker)

    auth_a = _auth_identity(tenant_id=tenant_id, agent_id=agent_a)
    await _self_write_file_impl(runtime, auth_a, key="CONFIG_TOKEN", content="a")
    await _self_write_file_impl(runtime, auth_a, key="NOTES_TOKEN", content="b")

    auth_b = _auth_identity(tenant_id=tenant_id, agent_id=agent_b)
    rows = await _self_list_files_impl(runtime, auth_b)

    assert rows == [], (
        "agent B must not see agent A's files even within the same tenant — "
        "cross-agent isolation is enforced by composite PK"
    )


async def test_self_delete_file_refuses_an_unbound_agent_key_even_when_absent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(committing_sessionmaker, tenant_id=tenant_id)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    with pytest.raises(ToolError, match="agent key acts as a member"):
        await _self_delete_file_impl(runtime, auth, key="never-written")


async def test_self_delete_file_refuses_an_unbound_agent_key_after_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(committing_sessionmaker, tenant_id=tenant_id)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    await _self_write_file_impl(runtime, auth, key="EPHEMERAL_TOKEN", content="bye")
    with pytest.raises(ToolError, match="agent key acts as a member"):
        await _self_delete_file_impl(runtime, auth, key="EPHEMERAL_TOKEN")
    assert len(await _self_list_files_impl(runtime, auth)) == 1


async def test_self_write_file_rejects_empty_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Empty key raises StoreError at the store; tool layer remaps to ToolError."""
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    with pytest.raises(ToolError, match="key must not be empty"):
        await _self_write_file_impl(runtime, auth, key="", content="x")


async def test_self_read_file_returns_none_on_missing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    row = await _self_read_file_impl(runtime, auth, key="never-written")

    assert row is None, "read of a never-written key must return None (not raise)"


async def test_missing_agent_id_raises(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Every impl rejects auth.agent_id is None at the entry."""
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(agent_id=None)

    with pytest.raises(ToolError, match="agent_id missing"):
        await _self_write_file_impl(runtime, auth, key="k", content="v")
    with pytest.raises(ToolError, match="agent_id missing"):
        await _self_read_file_impl(runtime, auth, key="k")
    with pytest.raises(ToolError, match="agent_id missing"):
        await _self_list_files_impl(runtime, auth)
    with pytest.raises(ToolError, match="agent_id missing"):
        await _self_delete_file_impl(runtime, auth, key="k")
    with pytest.raises(ToolError, match="agent_id missing"):
        await _set_repo_binding_impl(
            runtime, auth, repo_url="https://github.com/o/r", default_branch="main"
        )
    with pytest.raises(ToolError, match="agent_id missing"):
        await _get_repo_binding_impl(runtime, auth)
    with pytest.raises(ToolError, match="agent_id missing"):
        await _clear_repo_binding_impl(runtime, auth)


# ---------------------------------------------------------------------------
# Plan 03: agent_repo_binding tool tests — vault transport infrastructure
# ---------------------------------------------------------------------------


_VAULT_ID = "vault_test_1"


def _vault_list_response_for(account_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, object]:
    return {
        "data": [
            {
                "id": _VAULT_ID,
                "type": "vault",
                "display_name": f"daimon-mcp:{account_id}:{agent_id}",
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": _VAULT_ID,
        "last_id": _VAULT_ID,
    }


def _credential_response(
    *,
    cred_id: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a BetaManagedAgentsCredential JSON shape for create/get responses."""
    return {
        "id": cred_id,
        "type": "vault_credential",
        "vault_id": _VAULT_ID,
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
        "archived_at": None,
        "metadata": metadata or {},
        "auth": {
            "type": "static_bearer",
            "mcp_server_url": "https://github.com",
        },
    }


def _api_error_response(status: int = 500) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "error",
            "error": {"type": "api_error", "message": "test failure"},
        },
    )


def _make_stub_anthropic_for_vaults(
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    new_cred_id: str = "cred_new_001",
    create_status: int = 200,
    delete_status: int = 204,
    record: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> AsyncAnthropic:
    """Build a real AsyncAnthropic backed by a transport that handles vault routes.

    Routes:
      - GET    /v1/vaults                                     → list with one matching vault
      - POST   /v1/vaults/{vault_id}/credentials              → create new credential
      - DELETE /v1/vaults/{vault_id}/credentials/{cred_id}    → 204
    """
    cred_path_re = re.compile(r"^/v1/vaults/([^/]+)/credentials$")
    cred_item_re = re.compile(r"^/v1/vaults/([^/]+)/credentials/([^/]+)$")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
        if record is not None:
            record.append((method, path, body))

        # GET /v1/vaults (list)
        if method == "GET" and path == "/v1/vaults":
            return httpx.Response(200, json=_vault_list_response_for(account_id, agent_id))
        # POST /v1/vaults/{vault_id}/credentials
        if method == "POST" and cred_path_re.match(path):
            if create_status != 200:
                return _api_error_response(create_status)
            raw_md = body.get("metadata")
            md: dict[str, str] = {}
            if isinstance(raw_md, dict):
                for k, v in raw_md.items():  # type: ignore[reportUnknownVariableType]
                    if isinstance(k, str) and isinstance(v, str):
                        md[k] = v
            return httpx.Response(200, json=_credential_response(cred_id=new_cred_id, metadata=md))
        # DELETE /v1/vaults/{vault_id}/credentials/{cred_id}
        if method == "DELETE" and cred_item_re.match(path):
            if delete_status >= 400:
                return _api_error_response(delete_status)
            return httpx.Response(204)
        return httpx.Response(404, json={"type": "error", "error": {"message": "no route"}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


# ---------------------------------------------------------------------------
# Plan 03: agent_repo_binding tool tests
# ---------------------------------------------------------------------------


def _github_probe_client(
    *,
    status_code: int = 200,
    record: list[str] | None = None,
) -> httpx.AsyncClient:
    """Stub the GitHub repo-access probe (`pat_can_access_repo`) endpoint.

    ``status_code=200`` simulates a credential that can read the repo;
    ``status_code=404`` simulates one that cannot (or a repo that doesn't
    exist). Records each requested path when ``record`` is given, so a test
    can assert exactly which ``owner/repo`` string was probed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request.url.path)
        if status_code == 404:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(status_code, json={"private": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_set_repo_binding_happy_path(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full orchestration: mint → vault.create → DB row → no old ref to delete."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_HAPPY"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    record: list[tuple[str, str, dict[str, Any]]] = []
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, new_cred_id="cred_happy_1", record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    result = await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r",
        default_branch="main",
        service="github",
        http_client=_github_probe_client(),
    )

    assert isinstance(result, AgentRepoBindingPublic), "must return projection model, not raw row"
    assert result.repo_url == "o/r", "set_binding normalizes repo_url to canonical owner/repo"
    assert result.default_branch == "main", "default_branch must round-trip"
    assert result.agent_id == agent_id, "agent_id must come from auth"
    assert not hasattr(result, "ma_secret_ref"), (
        "AgentRepoBindingPublic must not expose ma_secret_ref to the agent"
    )

    methods = [(m, p) for m, p, _ in record]
    assert ("GET", "/v1/vaults") in methods, "must call vault discovery"
    assert ("POST", f"/v1/vaults/{_VAULT_ID}/credentials") in methods, (
        "must call credentials.create on discovered vault"
    )


async def test_set_repo_binding_writes_row_with_new_cred_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DB row's ma_secret_ref must equal the credential id returned by vault.create."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)
    new_id = "cred_xyz_42"

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_002"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, new_cred_id=new_id
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r",
        default_branch="main",
        http_client=_github_probe_client(),
    )

    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must exist"
    assert row.ma_secret_ref == new_id, (
        "DB row's ma_secret_ref must equal the cred id returned by vault.create"
    )


async def test_get_repo_binding_returns_public_projection(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """get must return AgentRepoBindingPublic (no ma_secret_ref) when bound."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_id = uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r",
            default_branch="trunk",
            ma_secret_ref="cred_seeded",
            proof=None,
        )

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)

    result = await _get_repo_binding_impl(runtime, auth)

    assert result is not None, "binding must be returned when one exists"
    assert isinstance(result, AgentRepoBindingPublic), (
        "must be the public projection, not the raw row"
    )
    assert not hasattr(result, "ma_secret_ref"), "projection must omit ma_secret_ref"
    assert result.default_branch == "trunk", "default_branch must round-trip"


async def test_get_repo_binding_returns_none_when_unbound(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    result = await _get_repo_binding_impl(runtime, auth)

    assert result is None, "unbound agent must return None (not raise)"


async def test_clear_repo_binding_removes_row_and_calls_vault_delete(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r",
            default_branch="main",
            ma_secret_ref="cred_to_delete",
            proof=None,
        )

    record: list[tuple[str, str, dict[str, Any]]] = []
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    result = await _clear_repo_binding_impl(runtime, auth)

    assert result == {"cleared": True}, "clear must return {cleared: True}"
    methods_paths = [(m, p) for m, p, _ in record]
    assert (
        "DELETE",
        f"/v1/vaults/{_VAULT_ID}/credentials/cred_to_delete",
    ) in methods_paths, "must call vault credentials.delete with the seeded cred id"

    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is None, (
        "DB row must be removed after clear"
    )


async def test_clear_repo_binding_idempotent_on_no_binding(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Clear on no binding returns {cleared: True} without raising."""
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    result = await _clear_repo_binding_impl(runtime, auth)

    assert result == {"cleared": True}, (
        "clear must be idempotent — no binding still returns {cleared: True}"
    )


async def test_clear_repo_binding_keeps_row_when_credential_delete_fails(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A credential that could not be revoked is still live, so its pointer stays."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r",
            default_branch="main",
            ma_secret_ref="cred_will_500",
            proof=None,
        )

    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, delete_status=500
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(ToolError, match="still live"):
        await _clear_repo_binding_impl(runtime, auth)

    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, (
        "the binding row is the only pointer to the credential — it must survive a "
        "failed revocation rather than orphaning a live token"
    )
    assert row.ma_secret_ref == "cred_will_500", "the pointer must still name the live credential"


async def test_clear_repo_binding_keeps_row_when_binding_was_made_from_another_account(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The vault is per account, the binding row is not.

    A caller from a different account than the one that bound the repo cannot
    reach the credential, so clearing must refuse instead of reporting success.
    """
    tenant_id = await _seed_tenant(committing_sessionmaker)
    binding_account_id = uuid.uuid4()
    clearing_account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r",
            default_branch="main",
            ma_secret_ref="cred_in_another_vault",
            proof=None,
        )

    # The only vault MA knows about belongs to the account that bound the repo.
    client = _make_stub_anthropic_for_vaults(account_id=binding_account_id, agent_id=agent_id)
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=clearing_account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(ToolError, match="cannot be revoked"):
        await _clear_repo_binding_impl(runtime, auth)

    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is not None, (
        "clearing from an account that cannot reach the credential must leave the "
        "binding row in place rather than orphaning a live token"
    )


async def test_clear_repo_binding_skips_vault_when_secret_is_not_a_vault_credential(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A public-repo binding has no MA credential, so there is nothing to revoke."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r",
            default_branch="main",
            ma_secret_ref="anon:",
            proof=None,
        )

    record: list[tuple[str, str, dict[str, Any]]] = []
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    result = await _clear_repo_binding_impl(runtime, auth)

    assert result == {"cleared": True}, "a binding with no stored credential must clear"
    assert record == [], (
        f"no vault call may be made for a binding that stores no credential; got {record!r}"
    )
    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is None, (
        "DB row must be removed"
    )


async def test_set_repo_binding_vault_failure_leaves_no_row(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(critical) Vault upload failure must leave NO binding row."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_FAIL"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, create_status=500
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(ToolError, match="vault upload failed"):
        await _set_repo_binding_impl(
            runtime,
            auth,
            repo_url="https://github.com/o/r",
            default_branch="main",
            service="github",
            http_client=_github_probe_client(),
        )

    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is None, (
        "vault upload failure must leave no binding row — upload-first ordering"
    )


async def test_set_repo_binding_db_failure_deletes_new_vault_cred(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If set_binding raises after vaults.credentials.create
    succeeds, the freshly-minted vault credential must be best-effort deleted before
    the exception propagates. Without cleanup, retries accumulate orphan credentials.
    """
    from daimon.core.errors import StoreError

    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    new_id = "cred_orphan_candidate"

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_DB_FAIL"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise StoreError("boom")

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.set_binding", _boom)

    record: list[tuple[str, str, dict[str, Any]]] = []
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, new_cred_id=new_id, record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    # StoreError must propagate — cleanup is best-effort, not exception-swallowing
    # (per the project's error-propagation rules).
    with pytest.raises(StoreError, match="boom"):
        await _set_repo_binding_impl(
            runtime,
            auth,
            repo_url="https://github.com/o/r",
            default_branch="main",
            service="github",
            http_client=_github_probe_client(),
        )

    # Assert 1: a DELETE was issued for the just-created credential.
    delete_path = f"/v1/vaults/{_VAULT_ID}/credentials/{new_id}"
    methods_paths = [(m, p) for m, p, _ in record]
    assert ("DELETE", delete_path) in methods_paths, (
        f"cleanup DELETE for new_cred {new_id} must be issued on DB failure — "
        f"actual transport requests: {methods_paths!r}"
    )

    # Assert 2: no AgentRepoBinding row was left behind.
    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is None, (
        "DB write failure must leave no binding row (the set_binding raise prevented commit)"
    )


async def test_set_repo_binding_db_failure_cleanup_swallows_anthropic_error(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BL-01 defensive: if the cleanup DELETE itself returns 500, the original
    StoreError must still propagate (cleanup is best-effort, swallows anthropic.APIError).
    """
    from daimon.core.errors import StoreError

    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_DB_FAIL_2"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise StoreError("db gone")

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.set_binding", _boom)

    client = _make_stub_anthropic_for_vaults(
        account_id=account_id,
        agent_id=agent_id,
        new_cred_id="cred_cleanup_will_500",
        delete_status=500,
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(StoreError, match="db gone"):
        await _set_repo_binding_impl(
            runtime,
            auth,
            repo_url="https://github.com/o/r",
            default_branch="main",
            http_client=_github_probe_client(),
        )


async def test_set_repo_binding_no_github_credential_hint(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NoBindingError → ToolError naming the private repo-binding request."""

    async def _fake_mint(**_kwargs: object) -> str:
        raise NoBindingError("no github credential")

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    with pytest.raises(ToolError, match="request_repo_binding"):
        await _set_repo_binding_impl(
            runtime, auth, repo_url="https://github.com/o/r", default_branch="main"
        )


async def test_set_repo_binding_provider_config_error_maps_to_tool_error(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_mint(**_kwargs: object) -> str:
        raise ProviderConfigError("missing GH_APP_ID")

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()

    with pytest.raises(ToolError, match="missing GH_APP_ID"):
        await _set_repo_binding_impl(
            runtime, auth, repo_url="https://github.com/o/r", default_branch="main"
        )


async def test_pat_plaintext_never_in_logs(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T-19-04-02: the PAT plaintext must never appear in any log line."""
    SENTINEL = "ghp_SENTINEL_TEST_PAT_DO_NOT_LOG_42"
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)

    async def _fake_mint(**_kwargs: object) -> str:
        return SENTINEL

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(account_id=account_id, agent_id=agent_id)
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r",
        default_branch="main",
        service="github",
        http_client=_github_probe_client(),
    )

    captured = capsys.readouterr()
    logs = captured.out + captured.err
    assert SENTINEL not in logs, f"PAT plaintext must never appear in any log line. logs: {logs!r}"


async def test_set_repo_binding_rebind_deletes_old_credential(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebind path: old credential is deleted AFTER the new credential POST + DB write."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)

    # Pre-seed an existing binding with a known old ref.
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r-old",
            default_branch="main",
            ma_secret_ref="old-cred-id",
            proof=None,
        )

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_REBIND"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    record: list[tuple[str, str, dict[str, Any]]] = []
    new_id = "cred_new_rebind"
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, new_cred_id=new_id, record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r-new",
        default_branch="main",
        http_client=_github_probe_client(),
    )

    # Sequence assertion: POST new cred BEFORE DELETE old cred.
    methods_paths = [(m, p) for m, p, _ in record]
    post_path = f"/v1/vaults/{_VAULT_ID}/credentials"
    delete_path = f"/v1/vaults/{_VAULT_ID}/credentials/old-cred-id"
    post_idx = next(
        (i for i, (m, p) in enumerate(methods_paths) if m == "POST" and p == post_path),
        None,
    )
    delete_idx = next(
        (i for i, (m, p) in enumerate(methods_paths) if m == "DELETE" and p == delete_path),
        None,
    )
    assert post_idx is not None, f"POST {post_path} must occur during rebind"
    assert delete_idx is not None, f"DELETE {delete_path} must occur during rebind"
    assert post_idx < delete_idx, (
        "POST new cred must occur BEFORE DELETE old cred (ordering guarantee)"
    )

    # The DB row's ma_secret_ref must now point at the new cred.
    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must exist"
    assert row.ma_secret_ref == new_id, (
        "after rebind, DB row's ma_secret_ref must point at the new credential id"
    )
    assert row.repo_url == "o/r-new", (
        "repo_url must reflect the rebind (normalized to canonical owner/repo), "
        "not the seeded original"
    )


async def test_set_repo_binding_rebind_raises_when_previous_credential_survives(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding overwrites the only pointer to the previous credential.

    When that credential cannot be deleted it is still live with nothing
    naming it, which the caller must be told about.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)
    async with committing_sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/r-old",
            default_branch="main",
            ma_secret_ref="old-cred-id",
            proof=None,
        )

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_REBIND_ORPHAN"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id,
        agent_id=agent_id,
        new_cred_id="cred_new_after_orphan",
        delete_status=500,
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(ToolError, match="still live"):
        await _set_repo_binding_impl(
            runtime,
            auth,
            repo_url="https://github.com/o/r-new",
            default_branch="main",
            http_client=_github_probe_client(),
        )

    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "the rebind itself stands — only the revocation failed"
    assert row.ma_secret_ref == "cred_new_after_orphan", (
        "the row must point at the new credential; the raise reports the un-revoked old one"
    )


# ---------------------------------------------------------------------------
# Plan 06: repo-access verification before any vault or DB write
# ---------------------------------------------------------------------------


async def test_set_repo_binding_refuses_when_credential_lacks_repo_access(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minted credential that cannot read the target repo refuses the bind
    before any vault upload — no orphan credential, no binding row.
    """
    tenant_id = await _seed_tenant(committing_sessionmaker)
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_NO_ACCESS"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    record: list[tuple[str, str, dict[str, Any]]] = []
    client = _make_stub_anthropic_for_vaults(
        account_id=account_id, agent_id=agent_id, record=record
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    with pytest.raises(ToolError, match="cannot read"):
        await _set_repo_binding_impl(
            runtime,
            auth,
            repo_url="https://github.com/o/private-repo",
            default_branch="main",
            http_client=_github_probe_client(status_code=404),
        )

    assert await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id) is None, (
        "a refused bind must leave no binding row"
    )
    methods = [(m, p) for m, p, _ in record]
    assert not any(p.startswith(f"/v1/vaults/{_VAULT_ID}/credentials") for _, p in methods), (
        f"a refused bind must upload nothing to the vault — actual transport requests: {methods!r}"
    )


async def test_set_repo_binding_records_pat_proof_when_credential_can_read_repo(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful bind records a pat-kind proof attributed to the caller."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_HAS_ACCESS"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(account_id=account_id, agent_id=agent_id)
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r",
        default_branch="main",
        http_client=_github_probe_client(status_code=200),
    )

    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must exist after a successful bind"
    assert row.proof_kind == "pat", "a credentialed access check must record kind='pat'"
    assert row.proof_at is not None, "proof must carry a timestamp"
    assert row.proof_account_id == account_id, (
        "proof must attribute the calling account, not any other identity"
    )


async def test_set_repo_binding_probes_canonical_owner_repo_for_full_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full-URL repo_url is probed against the canonical owner/repo path —
    the same string set_binding will normalize and store — so a normalization
    drift between the probe and the stored row cannot pass silently.
    """
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = await _seed_tenant_and_fixed_account(committing_sessionmaker, account_id=account_id)

    async def _fake_mint(**_kwargs: object) -> str:
        return "ghp_TEST_PAT_URL_FORM"

    monkeypatch.setattr("daimon.adapters.mcp.tools.self_edit.dispatch_mint_token", _fake_mint)
    client = _make_stub_anthropic_for_vaults(account_id=account_id, agent_id=agent_id)
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(account_id=account_id, tenant_id=tenant_id, agent_id=agent_id)

    probed_paths: list[str] = []
    await _set_repo_binding_impl(
        runtime,
        auth,
        repo_url="https://github.com/o/r.git",
        default_branch="main",
        http_client=_github_probe_client(record=probed_paths),
    )

    assert probed_paths == ["/repos/o/r"], (
        "the probe must hit the canonical owner/repo path, not the raw full-URL input — "
        f"actual probed paths: {probed_paths!r}"
    )


# ---------------------------------------------------------------------------
# Plan 04: confused-deputy structural check on registered tool schemas
# ---------------------------------------------------------------------------


_SELF_EDIT_TOOL_NAMES = {
    "self_write_file",
    "self_read_file",
    "self_list_files",
    "self_delete_file",
    "set_repo_binding",
    "get_repo_binding",
    "clear_repo_binding",
}


async def test_no_identity_args_in_self_edit_tool_schemas(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Structural check: no self-edit tool exposes agent_id, tenant_id,
    or account_id in its input schema. Identity is JWT-resolved server-side;
    a client-supplied identity arg is a confused-deputy hole. This is a
    structural impossibility check — not defense in depth.
    """
    mcp = FastMCP(name="test-self-edit")
    runtime = _runtime(committing_sessionmaker)
    register_self_edit_tools(mcp, runtime)

    tools = await mcp.local_provider.list_tools()
    forbidden = {"agent_id", "tenant_id", "account_id"}

    seen: set[str] = set()
    for tool in tools:
        if tool.name not in _SELF_EDIT_TOOL_NAMES:
            continue
        seen.add(tool.name)
        properties = set((tool.parameters or {}).get("properties", {}).keys())
        leaked = properties & forbidden
        assert not leaked, (
            f"tool {tool.name} schema leaks identity args {leaked} — "
            "all identity must be JWT-resolved server-side"
        )

    missing = _SELF_EDIT_TOOL_NAMES - seen
    assert not missing, f"register_self_edit_tools did not register all 7 tools; missing: {missing}"


async def test_self_read_and_list_withhold_values_a_person_entered(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """H3: a key submitted through a credential form never comes back as tool output.

    The form path writes with the requester's account id, and the requester's
    later turns read with that same id, so the reader and the writer match.
    """
    from daimon.core.stores.agent_files import put_agent_file

    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_id = uuid.uuid4()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="CRM_TOKEN",
            content="crm-secret-value",
            # Same account the credential form records: the person who asked.
            set_by_account_id=auth.account_id,
        )
    await _self_write_file_impl(runtime, auth, key="NOTES_TOKEN", content="my own note")

    secret = await _self_read_file_impl(runtime, auth, key="CRM_TOKEN")
    own = await _self_read_file_impl(runtime, auth, key="NOTES_TOKEN")
    listed = await _self_list_files_impl(runtime, auth)

    assert secret is not None and "crm-secret-value" not in secret.content
    assert secret.content == REDACTED_VALUE
    assert own is not None and own.content == REDACTED_VALUE
    assert [r.key for r in listed] == ["CRM_TOKEN", "NOTES_TOKEN"]
    assert all(
        "crm-secret-value" not in r.content and "my own note" not in r.content for r in listed
    )


async def test_self_write_file_without_encryption_keys_names_the_operator_step(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """H6: on a deployment with no crypto keys, the agent is told why nothing saved."""
    tenant_id = await _seed_tenant(committing_sessionmaker)
    keyless = async_sessionmaker(
        bind=committing_sessionmaker.kw["bind"],
        expire_on_commit=False,
        info={"crypto_keys": (), "crypto_allow_plaintext": False},
    )
    runtime = _runtime(keyless)
    auth = _auth_identity(tenant_id=tenant_id)

    with pytest.raises(ToolError, match="DAIMON_CRYPTO__KEYS"):
        await _self_write_file_impl(runtime, auth, key="NOTES_TOKEN", content="x")


@pytest.mark.parametrize(
    ("key", "match"),
    [
        ("TAR_OPTIONS", "reserved name"),
        ("R_PROFILE_USER", "reserved name"),
        ("LD_PRELOAD", "reserved name"),
        ("SNOWFLAKE_USER", "not a secret name"),
        ("DATABASE_URL", "not a secret name"),
        ("NOTES", "not a secret name"),
        ("notes.md", "must match"),
    ],
)
async def test_self_write_file_refuses_every_non_secret_name(
    committing_sessionmaker: async_sessionmaker[AsyncSession], key: str, match: str
) -> None:
    """An agent key is always a member: only secret names, never tool controls."""
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match=match):
        await _self_write_file_impl(runtime, auth, key=key, content="v")


async def test_self_write_file_never_replaces_a_stored_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An agent key is a member: it adds keys, it never overwrites one a person set."""
    from daimon.core.stores.agent_files import get_agent_file, put_agent_file

    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_id = uuid.uuid4()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="CRM_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )

    with pytest.raises(ToolError, match="already set"):
        await _self_write_file_impl(runtime, auth, key="CRM_TOKEN", content="attacker")

    async with committing_sessionmaker() as session:
        row = await get_agent_file(session, tenant_id=tenant_id, agent_id=agent_id, key="CRM_TOKEN")
    assert row is not None and row.content == "the-value-in-use", "the stored value is untouched"


@pytest.mark.parametrize(
    ("held", "adding"),
    [("GH_TOKEN", "GITHUB_TOKEN"), ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")],
)
async def test_self_write_file_never_adds_an_alias_of_a_held_key(
    committing_sessionmaker: async_sessionmaker[AsyncSession], held: str, adding: str
) -> None:
    from daimon.core.stores.agent_files import list_agent_files, put_agent_file

    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_id = uuid.uuid4()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key=held,
            content="the-value-in-use",
            set_by_account_id=None,
        )

    with pytest.raises(ToolError, match=f"Adding {adding} would replace {held}"):
        await _self_write_file_impl(runtime, auth, key=adding, content="attacker")

    async with committing_sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    assert [r.key for r in rows] == [held], "the alias must not be stored"


@pytest.mark.parametrize(
    ("managed", "default_name"),
    [(True, None), (False, "project-agent")],
    ids=["built-in", "reachable"],
)
async def test_self_delete_then_add_cannot_replace_a_key_on_a_shared_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    managed: bool,
    default_name: str | None,
) -> None:
    """Delete-then-add was a replacement with no gate; on a shared agent the delete is refused."""
    from daimon.core.stores.agent_files import get_agent_file, put_agent_file

    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(
        committing_sessionmaker, tenant_id=tenant_id, managed=managed, default_name=default_name
    )
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="CRM_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )

    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _self_delete_file_impl(runtime, auth, key="CRM_TOKEN")
    with pytest.raises(ToolError, match="already set"):
        await _self_write_file_impl(runtime, auth, key="CRM_TOKEN", content="attacker")

    async with committing_sessionmaker() as session:
        row = await get_agent_file(session, tenant_id=tenant_id, agent_id=agent_id, key="CRM_TOKEN")
    assert row is not None and row.content == "the-value-in-use"


async def test_self_delete_then_add_cannot_swap_an_alias_on_a_shared_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Delete GITHUB_TOKEN, add GH_TOKEN: the delete half is refused."""
    from daimon.core.stores.agent_files import list_agent_files, put_agent_file

    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(
        committing_sessionmaker, tenant_id=tenant_id, managed=True
    )
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    async with committing_sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="GITHUB_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )

    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _self_delete_file_impl(runtime, auth, key="GITHUB_TOKEN")
    with pytest.raises(ToolError, match="would replace GITHUB_TOKEN"):
        await _self_write_file_impl(runtime, auth, key="GH_TOKEN", content="attacker")

    async with committing_sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    assert [r.key for r in rows] == ["GITHUB_TOKEN"]


async def test_self_delete_file_fails_closed_when_the_agent_does_not_resolve(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, _agent_id = _runtime_with_agent(committing_sessionmaker, tenant_id=tenant_id)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=uuid.uuid4())
    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _self_delete_file_impl(runtime, auth, key="CRM_TOKEN")


async def test_concurrent_self_writes_of_two_alias_names_store_only_one(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writers adding GH_TOKEN and GITHUB_TOKEN at once: the key-set lock lets one win.

    The patched name read waits (briefly) for the other writer to read too, so
    without the lock both read "neither held" and both insert.
    """
    import asyncio
    import contextlib

    import daimon.adapters.mcp.tools.self_edit as self_edit_mod
    from daimon.core.stores.agent_files import list_agent_files

    tenant_id = await _seed_tenant(committing_sessionmaker)
    agent_id = uuid.uuid4()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)

    real = self_edit_mod.list_turn_key_names
    readers = 0
    both_read = asyncio.Event()

    async def read_then_wait(session: AsyncSession, **kw: Any) -> tuple[str, ...]:
        nonlocal readers
        names = await real(session, **kw)
        readers += 1
        if readers >= 2:
            both_read.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_read.wait(), timeout=1.0)
        return names

    monkeypatch.setattr(self_edit_mod, "list_turn_key_names", read_then_wait)

    results = await asyncio.gather(
        _self_write_file_impl(runtime, auth, key="GH_TOKEN", content="a"),
        _self_write_file_impl(runtime, auth, key="GITHUB_TOKEN", content="b"),
        return_exceptions=True,
    )

    async with committing_sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    assert len(rows) == 1, f"exactly one alias may land, got {[r.key for r in rows]}"
    refused = [r for r in results if isinstance(r, ToolError)]
    assert len(refused) == 1 and "would replace" in str(refused[0])


async def _held(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    key: str,
) -> None:
    from daimon.core.stores.agent_files import put_agent_file

    async with sessionmaker.begin() as session:
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            key=key,
            content="the-value-in-use",
            set_by_account_id=None,
        )


async def _assert_delete_then_add_refused(
    runtime: McpRuntime,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    from daimon.core.stores.agent_files import list_agent_files

    auth = _auth_identity(tenant_id=tenant_id, agent_id=agent_id)
    await _held(sessionmaker, tenant_id=tenant_id, agent_id=agent_id, key="GITHUB_TOKEN")
    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _self_delete_file_impl(runtime, auth, key="GITHUB_TOKEN")
    with pytest.raises(ToolError, match="would replace GITHUB_TOKEN"):
        await _self_write_file_impl(runtime, auth, key="GH_TOKEN", content="attacker")
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    assert [(r.key, r.content) for r in rows] == [("GITHUB_TOKEN", "the-value-in-use")]


async def test_delete_then_add_refused_when_display_and_routing_names_differ(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Display 'Acme Display', daimon_name 'acme', a channel configured for 'acme'."""
    from daimon.core.scope import ChannelScopeRef
    from daimon.core.stores.scoped_config_write import set_fields

    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(
        committing_sessionmaker,
        tenant_id=tenant_id,
        display_name="Acme Display",
        routing_name="acme",
    )
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="chan-acme"),
            tenant_id=tenant_id,
            agent_name="acme",
        )
    await _assert_delete_then_add_refused(
        runtime, committing_sessionmaker, tenant_id=tenant_id, agent_id=agent_id
    )


async def test_delete_then_add_refused_while_a_handoff_thread_is_bound_to_the_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(committing_sessionmaker, tenant_id=tenant_id)
    async with committing_sessionmaker.begin() as session:
        await create_binding(
            session,
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id="chan-1",
            thread_id="thread-1",
            responder_ma_agent_id="ag_self",
            responder_name="project-agent",
            kind="handoff",
        )
    await _assert_delete_then_add_refused(
        runtime, committing_sessionmaker, tenant_id=tenant_id, agent_id=agent_id
    )


async def test_delete_then_add_refused_when_the_agent_is_someones_personal_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.scope import UserScopeRef
    from daimon.core.stores.scoped_config_write import set_fields
    from daimon.core.stores.tenants import get_tenant
    from daimon.testing.factories import make_account

    tenant_id = await _seed_tenant(committing_sessionmaker)
    runtime, agent_id = _runtime_with_agent(committing_sessionmaker, tenant_id=tenant_id)
    async with committing_sessionmaker.begin() as session:
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=UserScopeRef(account_id=account.id),
            tenant_id=tenant_id,
            agent_name="project-agent",
        )
    await _assert_delete_then_add_refused(
        runtime, committing_sessionmaker, tenant_id=tenant_id, agent_id=agent_id
    )

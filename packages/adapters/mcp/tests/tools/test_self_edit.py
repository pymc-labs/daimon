"""DB-backed unit tests for self-edit MCP tools.

Tests for the agent file MCP tools.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.self_edit import (
    REDACTED_VALUE,
    _self_delete_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_list_files_impl,  # pyright: ignore[reportPrivateUsage]
    _self_read_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_write_file_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
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
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma import build_fake_anthropic, list_response

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

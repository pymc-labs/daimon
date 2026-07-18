"""Tests for _require_admin ToolError gate.

Covers:
  - _require_admin helper itself (Wave-0 contract)
  - Per-module gating: one mutating impl per module raises on non-admin; reads pass through
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _require_admin
from daimon.adapters.mcp.tools.agents import _create_agent_impl, _list_agents_impl
from daimon.adapters.mcp.tools.environments import (
    _archive_environment_impl,
    _create_environment_impl,
    _list_environments_impl,
    _update_environment_impl,
)
from daimon.adapters.mcp.tools.self_edit import _self_read_file_impl, _self_write_file_impl
from daimon.adapters.mcp.tools.skills import _list_impl, _sync_impl
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec, EnvironmentSpec
from daimon.core.stores.domain import Role
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from factories import make_ma_agent
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.asyncio

_D28_MESSAGE = "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"


# ---------------------------------------------------------------------------
# Wave-0: _require_admin helper contract
# ---------------------------------------------------------------------------


def test_require_admin_raises_tool_error_when_not_admin() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    with pytest.raises(ToolError) as exc_info:
        _require_admin(auth)
    assert str(exc_info.value) == _D28_MESSAGE, (
        "non-admin chat caller must be refused with the admin-required message"
    )


def test_require_admin_passes_when_admin() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=True,
    )
    result = _require_admin(auth)
    assert result is None, "admin caller passes the gate"


# ---------------------------------------------------------------------------
# Per-module gating: agents.py
# ---------------------------------------------------------------------------


def _agents_runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_create_agent_impl_raises_when_not_admin() -> None:
    """Non-admin caller is rejected before any MA call."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    runtime = _agents_runtime(MagicMock(spec=AsyncAnthropic))
    spec = AgentSpec(name="my-agent", model="claude-opus-4-5")
    with pytest.raises(ToolError) as exc_info:
        await _create_agent_impl(runtime, auth, spec)
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_create_agent_impl must refuse non-admin with admin-required message"
    )


async def test_list_agents_impl_does_not_raise_for_non_admin() -> None:
    """Reads are ungated — non-admin callers can list agents."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _req, _m: list_response(
            [
                make_ma_agent(
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.USER,
        is_admin=False,
    )
    result = await _list_agents_impl(_agents_runtime(client), auth, page=None)
    assert isinstance(result, list), "non-admin read must succeed and return a list"


# ---------------------------------------------------------------------------
# Per-module gating: self_edit.py
# ---------------------------------------------------------------------------


async def test_self_write_file_impl_raises_when_not_admin() -> None:
    """Non-admin agent caller is rejected before any DB call."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        agent_id=uuid.uuid4(),
        is_admin=False,
    )
    runtime = McpRuntime(
        session_factory=MagicMock(),
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    with pytest.raises(ToolError) as exc_info:
        await _self_write_file_impl(runtime, auth, key="config.yaml", content="hello")
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_self_write_file_impl must refuse non-admin with admin-required message"
    )


async def test_self_read_file_impl_does_not_raise_admin_gate_for_non_admin(
    committing_sessionmaker: Any,
) -> None:
    """Read tool is ungated — non-admin agent can read its own files."""
    from factories import seed_tenant  # type: ignore[import-untyped]

    async with committing_sessionmaker.begin() as session:
        tenant_id = await seed_tenant(session)

    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        agent_id=uuid.uuid4(),
        is_admin=False,
    )
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    # Non-existent key returns None — no admin gate should fire
    result = await _self_read_file_impl(runtime, auth, key="missing")
    assert result is None, "non-admin read must return None for missing key without admin gate"


# ---------------------------------------------------------------------------
# Per-module gating: skills.py
# ---------------------------------------------------------------------------


async def test_sync_impl_raises_when_not_admin() -> None:
    """Non-admin caller is rejected before any HTTP call."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    runtime = McpRuntime(
        session_factory=MagicMock(),
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    with pytest.raises(ToolError) as exc_info:
        await _sync_impl(runtime, auth, "https://github.com/x/y", "main", "")
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_sync_impl must refuse non-admin with admin-required message"
    )


async def test_list_impl_does_not_raise_admin_gate_for_non_admin() -> None:
    """Reads are ungated — non-admin callers can list skills."""
    from anthropic.types.beta import SkillListResponse

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_1",
                    display_title="my-skill",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    runtime = McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    result = await _list_impl(runtime, auth)
    assert isinstance(result, list), "non-admin read must succeed and return a list"


# ---------------------------------------------------------------------------
# Per-module gating: environments.py
# ---------------------------------------------------------------------------


def _env_runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_create_environment_impl_raises_when_not_admin() -> None:
    """Non-admin caller is rejected before any MA call."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    spec = EnvironmentSpec(name="e")
    with pytest.raises(ToolError) as exc_info:
        await _create_environment_impl(_env_runtime(MagicMock(spec=AsyncAnthropic)), auth, spec)
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_create_environment_impl must refuse non-admin with admin-required message"
    )


async def test_update_environment_impl_raises_when_not_admin() -> None:
    """Non-admin caller is rejected before empty-patch validation or any MA call.

    The gate must be the FIRST line — before the empty-patch check fires.
    """
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    with pytest.raises(ToolError) as exc_info:
        await _update_environment_impl(
            _env_runtime(MagicMock(spec=AsyncAnthropic)),
            auth,
            name="e",
            config=None,
            description="x",
        )
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_update_environment_impl must refuse non-admin with admin-required message"
    )


async def test_archive_environment_impl_raises_when_not_admin() -> None:
    """Non-admin caller is rejected before any MA call."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        is_admin=False,
    )
    with pytest.raises(ToolError) as exc_info:
        await _archive_environment_impl(_env_runtime(MagicMock(spec=AsyncAnthropic)), auth, "e")
    assert str(exc_info.value) == _D28_MESSAGE, (
        "_archive_environment_impl must refuse non-admin with admin-required message"
    )


async def test_list_environments_impl_does_not_raise_for_non_admin() -> None:
    """Read is ungated — non-admin callers can list environments."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _req, _m: list_response(
            [
                BetaEnvironment(
                    id="env_1",
                    type="environment",
                    name="demo",
                    config={
                        "type": "cloud",
                        "networking": {"type": "unrestricted"},
                        "packages": {
                            "apt": [],
                            "cargo": [],
                            "gem": [],
                            "go": [],
                            "npm": [],
                            "pip": [],
                        },
                    },
                    description="",
                    created_at="2026-04-24T00:00:00Z",
                    updated_at="2026-04-24T00:00:00Z",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.USER,
        is_admin=False,
    )
    result = await _list_environments_impl(_env_runtime(client), auth, page=None)
    assert isinstance(result, list), "non-admin read must succeed and return a list"

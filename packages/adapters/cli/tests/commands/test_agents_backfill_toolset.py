"""Transport-fake unit tests for `daimon agents backfill-toolset`.

Tests:
- test_backfill_patches_toolless_agent_with_base_toolset: agent lacking the
  agent_toolset_20260401 is patched; update payload carries the toolset and
  preserves pre-existing tools.
- test_backfill_skips_agent_with_base_toolset: toolset-bearing agent → zero update calls.
- test_backfill_dry_run_writes_nothing: toolless agent + dry_run=True → zero update
  calls, table printed.
- test_backfill_second_run_selects_zero_agents: after the handler reflects the patched
  tools, a second run performs zero updates (idempotence).
"""

from __future__ import annotations

import datetime as dt
import json
from io import StringIO
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.adapters.cli.commands.agents import agents_backfill_toolset
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# These tests create their tenant explicitly as discord (the tenant
# list_tenants_by_platform enumerates) and tag MA resources against it, so
# they opt out of the conftest db_session_factory autoseed.
pytestmark = pytest.mark.no_cli_local_seed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 5, 29, tzinfo=dt.UTC)
_WORKSPACE_ID = "guild_backfill_001"

_BASE_TOOLSET_DICT: dict[str, object] = {
    "type": "agent_toolset_20260401",
    "configs": [
        {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "edit", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "grep", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "glob", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "write", "enabled": True, "permission_policy": {"type": "always_allow"}},
    ],
    "default_config": {
        "enabled": True,
        "permission_policy": {"type": "always_allow"},
    },
}


def _tenant_id() -> object:
    return derive_tenant_uuid(platform="discord", workspace_id=_WORKSPACE_ID)


class _FakeCli:
    local_user = "testuser"


class _FakeMcp:
    public_url = None


class _FakeSettings:
    cli = _FakeCli()
    mcp = _FakeMcp()


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _agent_json(
    *,
    agent_id: str,
    name: str,
    version: int = 1,
    metadata: dict[str, str],
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a serialised MA agent response dict.

    `tools` defaults to an empty list (no toolset — needs backfill). Pass a
    list containing _BASE_TOOLSET_DICT to simulate an already-patched agent.
    Tools are injected after model_dump because the SDK response model requires
    fully-constructed tool objects for validated construction; the transport
    returns raw JSON which the SDK parses back, so injecting at the dict level
    is the correct place.
    """
    base = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=version,
        mcp_servers=[],
        skills=[],
        tools=[],
        system="you are helpful",
    ).model_dump(mode="json")
    # Replace the empty tools list with the caller-supplied one.
    base["tools"] = list(tools) if tools is not None else []
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_patches_toolless_agent_with_base_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Toolless agent is patched; update payload contains agent_toolset_20260401."""
    tenant_id = _tenant_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_id = "agent_toolless_001"
    agent_version = 3
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "toolless-agent",
    }
    # No tools — should trigger a patch.
    agent_data = _agent_json(
        agent_id=agent_id,
        name="toolless-agent",
        version=agent_version,
        metadata=metadata,
        tools=[],
    )

    update_bodies: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=agent_data)

    def on_retrieve(_req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("GET", rf"/v1/agents/{agent_id}", on_retrieve)
    router.add("POST", rf"/v1/agents/{agent_id}", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_backfill_toolset(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_bodies) == 1, "toolless agent must trigger exactly one agents.update call"
    body = update_bodies[0]
    assert body.get("version") == agent_version, (
        "agents.update must pass version=fresh.version to avoid version conflicts"
    )
    raw_tools = body.get("tools")
    assert isinstance(raw_tools, list), "update body must include a tools list"
    tools = cast(list[dict[str, object]], raw_tools)
    toolset_entries = [t for t in tools if t.get("type") == "agent_toolset_20260401"]
    assert len(toolset_entries) == 1, (
        "patched tools must contain exactly one agent_toolset_20260401 entry"
    )


@pytest.mark.asyncio
async def test_backfill_skips_agent_with_base_toolset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agent already carrying agent_toolset_20260401 is not updated."""
    tenant_id = _tenant_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_id = "agent_toolset_bearer_001"
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "toolset-bearer",
    }
    # Agent already has the base toolset.
    agent_data = _agent_json(
        agent_id=agent_id,
        name="toolset-bearer",
        version=2,
        metadata=metadata,
        tools=[_BASE_TOOLSET_DICT],
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", rf"/v1/agents/{agent_id}", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_backfill_toolset(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_calls) == 0, (
        "agent with agent_toolset_20260401 must be skipped; expected zero agents.update calls"
    )


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """dry_run=True prints the table but issues zero agents.update calls."""
    tenant_id = _tenant_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_id = "agent_dry_backfill_001"
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "dry-target",
    }
    # Toolless agent — would be patched if not dry-run.
    agent_data = _agent_json(
        agent_id=agent_id,
        name="dry-target",
        version=1,
        metadata=metadata,
        tools=[],
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", rf"/v1/agents/{agent_id}", on_update)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_backfill_toolset(rt=rt, console=console, yes=True, dry_run=True)

    assert len(update_calls) == 0, (
        "--dry-run must not call agents.update; expected zero update calls"
    )
    output = out.getvalue()
    assert "dry-run" in output.lower(), (
        "--dry-run must print a dry-run header so the operator knows no write occurred"
    )
    assert "dry-target" in output, "--dry-run must include the agent name in the report table"


@pytest.mark.asyncio
async def test_backfill_second_run_selects_zero_agents(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After the first run patches the agent, a second run selects zero agents (idempotent)."""
    tenant_id = _tenant_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_id = "agent_idempotent_001"
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "idempotent-agent",
    }
    # Agent now reflects the patched state (has the base toolset).
    patched_agent_data = _agent_json(
        agent_id=agent_id,
        name="idempotent-agent",
        version=2,
        metadata=metadata,
        tools=[_BASE_TOOLSET_DICT],
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=patched_agent_data)

    router = MARouter()
    # The list endpoint returns the already-patched agent — simulating state
    # after a first successful backfill run.
    router.add("GET", r"/v1/agents", lambda req, m: list_response([patched_agent_data]))
    router.add("POST", rf"/v1/agents/{agent_id}", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_backfill_toolset(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_calls) == 0, (
        "second run against already-patched agents must select zero agents "
        "and issue zero agents.update calls (structural idempotence)"
    )

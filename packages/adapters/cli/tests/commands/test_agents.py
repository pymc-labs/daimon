from __future__ import annotations

import datetime as dt
import json
import uuid
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import pytest
import typer
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_always_allow_policy import (
    BetaManagedAgentsAlwaysAllowPolicy,
)
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
from anthropic.types.beta.beta_managed_agents_mcp_toolset_default_config import (
    BetaManagedAgentsMCPToolsetDefaultConfig,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.adapters.cli.commands.agents import (
    agents_archive,
    agents_create,
    agents_fork,
    agents_get,
    agents_list,
    agents_update,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import SpecError, StoreError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# These tests create their tenant explicitly as cli:local (the tenant
# discover_tenant derives for the CLI) and tag MA resources against it, so they
# opt out of the conftest db_session_factory autoseed to avoid a duplicate.
pytestmark = pytest.mark.no_cli_local_seed


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
    name: str = "default",
    version: int = 1,
    metadata: dict[str, str] | None = None,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, object]:
    now = dt.datetime(2026, 4, 22, tzinfo=dt.UTC)
    md: dict[str, str] = metadata or {}
    if tenant_id is not None:
        md = {"daimon_tenant": str(tenant_id), "daimon_name": name, **md}
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata=md,
        description=None,
        archived_at=None,
        created_at=now,
        updated_at=now,
        version=version,
        mcp_servers=[],
        skills=[],
        tools=[],
        system="you are helpful",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_agents_list_returns_ma_columns(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """list calls list_agents_by_tenant and emits MA-native columns."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        tenant_id = tenant.id

    agent1 = _agent_json(agent_id="ag_1", name="agent-alpha", tenant_id=tenant_id)
    agent2 = _agent_json(agent_id="ag_2", name="agent-beta", tenant_id=tenant_id)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent1, agent2]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_list(rt=rt, console=console, as_json=False)

    out = cast(StringIO, console.file).getvalue()
    assert "agent-alpha" in out, "list output must include first agent name"
    assert "agent-beta" in out, "list output must include second agent name"


@pytest.mark.asyncio
async def test_agents_get_resolves_via_tag(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get resolves the agent via MA tag lookup, not DB store."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    agent_data = _agent_json(agent_id="ag_found", name="my-agent", tenant_id=tenant_id)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    # Should not raise — agent found via tag
    await agents_get(rt=rt, console=console, name="my-agent", as_json=False)


@pytest.mark.asyncio
async def test_agents_get_with_include_archived_passes_flag_to_sdk(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """--include-archived must propagate to client.beta.agents.list, so archived
    agents become findable from the CLI without dropping to the SDK.
    """
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    archived_agent = _agent_json(agent_id="ag_archived", name="ghost-agent", tenant_id=tenant_id)
    seen_include_archived: list[str | None] = []

    def list_with_flag_capture(req: httpx.Request, _m: object) -> httpx.Response:
        seen_include_archived.append(req.url.params.get("include_archived"))
        return list_response([archived_agent])

    router = MARouter()
    router.add("GET", r"/v1/agents", list_with_flag_capture)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_get(
        rt=rt,
        console=console,
        name="ghost-agent",
        as_json=False,
        include_archived=True,
    )
    assert seen_include_archived == ["true"], (
        f"include_archived flag must propagate to MA; got query values {seen_include_archived}"
    )


@pytest.mark.asyncio
async def test_agents_get_not_found_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """get raises StoreError when no agent matches the tag."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([]))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="no agent named"):
        await agents_get(rt=rt, console=console, name="ghost-agent", as_json=False)


@pytest.mark.asyncio
async def test_agents_create_calls_sdk_create(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create calls beta.agents.create with params from the spec file."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_new", name="new-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    # reconcile_agent does a GET list (find_agents_by_daimon_tag) before creating
    router.add("GET", r"/v1/agents", lambda req, m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: new-agent\nmodel: claude-sonnet-4-6\n")

    await agents_create(rt=rt, console=console, path=spec_path)

    assert len(created_bodies) == 1, f"expected 1 POST to /agents, got {len(created_bodies)}"
    body = created_bodies[0]
    assert body.get("name") == "new-agent", "create body must include agent name"
    md = body.get("metadata")
    assert isinstance(md, dict), "create body must include metadata dict"
    assert md.get("daimon_name") == "new-agent", "metadata must tag daimon_name"
    assert md.get("daimon_tenant") == str(tenant_id), "metadata must tag daimon_tenant"
    assert md.get("daimon_account") == str(derive_guild_account_uuid(tenant_id)), (
        "CLI-created agents must carry the derived guild account so the panel can edit them"
    )


@pytest.mark.asyncio
async def test_agents_update_passes_version(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """update sends version=<agent.version from MA> in the JSON body."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    agent_data = _agent_json(agent_id="ag_01", name="my-agent", version=7, tenant_id=tenant_id)
    update_bodies: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_01", name="my-agent", version=8, tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/ag_01", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=80)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: my-agent\nmodel: claude-sonnet-4-6\nsystem: hello\n")

    await agents_update(rt=rt, console=console, name="my-agent", path=spec_path)

    assert len(update_bodies) == 1, f"expected 1 POST to /agents/ag_01, got {len(update_bodies)}"
    assert update_bodies[0].get("version") == 7, (
        f"update body must include version=7 from MA agent, got {update_bodies[0].get('version')!r}"
    )


@pytest.mark.asyncio
async def test_agents_update_keeps_guild_account_stamp(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """update on a guild-stamped agent must re-stamp the guild account, not the
    personal CLI principal — reconcile_agent rebuilds the full metadata dict, so
    threading the personal account through would flip ownership back to personal.
    """
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id
    guild_account = derive_guild_account_uuid(tenant_id)

    agent_data = _agent_json(
        agent_id="ag_guild",
        name="guild-agent",
        version=2,
        tenant_id=tenant_id,
        metadata={"daimon_account": str(guild_account)},
    )
    update_bodies: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(
                agent_id="ag_guild", name="guild-agent", version=3, tenant_id=tenant_id
            ),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/ag_guild", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=80)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: guild-agent\nmodel: claude-sonnet-4-6\nsystem: hello\n")

    await agents_update(rt=rt, console=console, name="guild-agent", path=spec_path)

    assert len(update_bodies) == 1, f"expected 1 update POST, got {len(update_bodies)}"
    md = update_bodies[0].get("metadata")
    assert isinstance(md, dict), "update body must include metadata dict"
    assert md.get("daimon_account") == str(guild_account), (
        "update must keep the guild-account stamp — re-stamping the personal CLI "
        "principal would flip a guild-owned agent back to personal ownership"
    )


@pytest.mark.asyncio
async def test_agents_update_preserves_inherited_mcp_toolset_when_yaml_omits_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A YAML that omits mcp wiring must not orphan an inherited MCP server.

    MA's update is a per-field partial merge: a raw `tools` array would replace
    the array (dropping the inherited mcp_toolset) while preserving the
    mcp_servers entry, leaving a server with no toolset referencing it -> 400.
    Routing through reconcile_agent re-merges both halves, so the update payload
    carries the inherited daimon-mcp server AND an mcp_toolset that references it.
    """
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    agent_data = BetaManagedAgentsAgent(
        id="ag_mcp",
        type="agent",
        name="my-fork",
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "my-fork"},
        description=None,
        archived_at=None,
        created_at=dt.datetime(2026, 4, 22, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 4, 22, tzinfo=dt.UTC),
        version=3,
        mcp_servers=[
            BetaManagedAgentsMCPServerURLDefinition(
                name="daimon-mcp", type="url", url="https://mcp.example/mcp"
            )
        ],
        skills=[],
        tools=[
            BetaManagedAgentsMCPToolset(
                type="mcp_toolset",
                mcp_server_name="daimon-mcp",
                configs=[],
                default_config=BetaManagedAgentsMCPToolsetDefaultConfig(
                    enabled=True,
                    permission_policy=BetaManagedAgentsAlwaysAllowPolicy(type="always_allow"),
                ),
            )
        ],
        system="you are helpful",
    ).model_dump(mode="json")
    update_bodies: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_mcp", name="my-fork", version=4, tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/ag_mcp", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=80)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: my-fork\nmodel: claude-sonnet-4-6\nsystem: hello\n")

    await agents_update(rt=rt, console=console, name="my-fork", path=spec_path)

    assert len(update_bodies) == 1, f"expected 1 update POST, got {len(update_bodies)}"
    body = update_bodies[0]
    servers = cast(list[dict[str, object]], body.get("mcp_servers") or [])
    tools = cast(list[dict[str, object]], body.get("tools") or [])
    assert any(srv.get("name") == "daimon-mcp" for srv in servers), (
        f"inherited daimon-mcp server must survive the update; got mcp_servers={servers!r}"
    )
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "daimon-mcp" for t in tools
    ), f"update must re-include the mcp_toolset referencing daimon-mcp; got tools={tools!r}"


@pytest.mark.asyncio
async def test_agents_update_rename_raises_spec_error(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Names are identity; a YAML name != the target name must raise, not rename."""
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=80)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: renamed\nmodel: claude-sonnet-4-6\nsystem: hello\n")

    with pytest.raises(SpecError):
        await agents_update(rt=rt, console=console, name="original", path=spec_path)


@pytest.mark.asyncio
async def test_agents_fork_strips_server_fields_and_creates(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork retrieves source from MA, strips server fields, creates new agent with target name."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    source_data = _agent_json(
        agent_id="ag_source", name="base-agent", version=3, tenant_id=tenant_id
    )
    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_fork", name="forked-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    # list for tag lookup
    router.add("GET", r"/v1/agents", lambda req, m: list_response([source_data]))
    # retrieve for full agent data
    router.add("GET", r"/v1/agents/ag_source", lambda req, m: httpx.Response(200, json=source_data))
    # create for the fork
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_fork(rt=rt, console=console, src="base-agent", dst="forked-agent")

    assert len(created_bodies) == 1, f"expected 1 POST to /agents, got {len(created_bodies)}"
    body = created_bodies[0]

    # Server fields must be stripped
    for server_field in ("id", "version", "created_at", "updated_at", "archived_at", "type"):
        assert server_field not in body, (
            f"server field {server_field!r} must be stripped from fork payload"
        )

    # New name and metadata must be set
    assert body.get("name") == "forked-agent", "fork body must use target name"
    md = body.get("metadata")
    assert isinstance(md, dict), "fork body must include metadata dict"
    assert md.get("daimon_name") == "forked-agent", "fork metadata must tag daimon_name with target"
    assert md.get("daimon_tenant") == str(tenant_id), "fork metadata must tag daimon_tenant"
    assert md.get("daimon_account") == str(derive_guild_account_uuid(tenant_id)), (
        "CLI-forked agents must carry the derived guild account so the panel can edit them"
    )


async def test_agents_fork_adds_base_toolset_when_source_lacks_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forking a legacy agent created before the base-toolset guarantee must not
    propagate the hole — the fork gains the base toolset so skills stay usable."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    source_data = _agent_json(
        agent_id="ag_source", name="base-agent", version=3, tenant_id=tenant_id
    )
    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_fork", name="forked-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([source_data]))
    router.add("GET", r"/v1/agents/ag_source", lambda req, m: httpx.Response(200, json=source_data))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_fork(rt=rt, console=console, src="base-agent", dst="forked-agent")

    assert len(created_bodies) == 1, f"expected 1 POST to /agents, got {len(created_bodies)}"
    tools = created_bodies[0].get("tools")
    assert isinstance(tools, list), "fork body must include tools"
    tool_types = [t.get("type") for t in tools]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    assert "agent_toolset_20260401" in tool_types, (
        "fork of a toolless source must gain the base toolset; skills require read"
    )


@pytest.mark.asyncio
async def test_agents_fork_missing_dst_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises BadParameter before any MA call when dst is None."""
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(typer.BadParameter, match="destination name is required"):
        await agents_fork(rt=rt, console=console, src="agent-x", dst=None)


@pytest.mark.asyncio
async def test_agents_fork_same_name_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises StoreError when dst equals src."""
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="conflicts with source"):
        await agents_fork(rt=rt, console=console, src="agent-x", dst="agent-x")


@pytest.mark.asyncio
async def test_agents_archive_calls_sdk_archive(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """archive finds agent by tag then calls beta.agents.archive(agent.id)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    agent_data = _agent_json(agent_id="ag_doomed", name="doomed", tenant_id=tenant_id)
    archive_paths: list[str] = []

    def on_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archive_paths.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/ag_doomed/archive", on_archive)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_archive(rt=rt, console=console, name="doomed", yes=True)

    assert len(archive_paths) == 1, f"expected 1 archive call, got {len(archive_paths)}"
    assert archive_paths[0].endswith("/ag_doomed/archive"), (
        f"archive must use MA agent id in URL, got {archive_paths[0]!r}"
    )


# ---------------------------------------------------------------------------
# Task 1: agents_create — guard, mcp merge, spec_hash/guidance/guild-account
# ---------------------------------------------------------------------------


class _FakeMcpWithUrl:
    public_url = "https://mcp.example.com/mcp"


class _FakeSettingsWithUrl:
    cli = _FakeCli()
    mcp = _FakeMcpWithUrl()


def _build_rt_with_url(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return CliRuntime(
        settings=cast(Settings, _FakeSettingsWithUrl()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


@pytest.mark.asyncio
async def test_agents_create_rejects_when_name_exists_in_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create raises StoreError and makes zero POST calls when a same-name agent
    already exists in the tenant, regardless of which account owns it."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    # Existing agent stamped with a DIFFERENT account (not the guild account).
    other_account = uuid.uuid4()
    existing_agent = _agent_json(
        agent_id="ag_existing",
        name="new-agent",
        tenant_id=tenant_id,
        metadata={"daimon_account": str(other_account)},
    )

    post_calls: list[str] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        post_calls.append(req.url.path)
        return httpx.Response(200, json=existing_agent)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([existing_agent]))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: new-agent\nmodel: claude-sonnet-4-6\n")

    with pytest.raises(StoreError, match="already exists in this server"):
        await agents_create(rt=rt, console=console, path=spec_path)

    assert post_calls == [], (
        "create must not POST to /v1/agents when a same-name agent already exists in the tenant"
    )


@pytest.mark.asyncio
async def test_agents_create_merges_daimon_mcp_when_public_url_set(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create payload contains the daimon-mcp mcp_servers entry AND the
    mcp_toolset tools entry when public_url is configured — both halves."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_new", name="new-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_url(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: new-agent\nmodel: claude-sonnet-4-6\n")

    await agents_create(rt=rt, console=console, path=spec_path)

    assert len(created_bodies) == 1, f"expected 1 POST to /v1/agents, got {len(created_bodies)}"
    body = created_bodies[0]
    servers = cast(list[dict[str, object]], body.get("mcp_servers") or [])
    tools = cast(list[dict[str, object]], body.get("tools") or [])
    assert any(
        s.get("name") == "daimon-mcp" and s.get("url") == "https://mcp.example.com/mcp"
        for s in servers
    ), f"create payload must include daimon-mcp mcp_servers entry; got mcp_servers={servers!r}"
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "daimon-mcp" for t in tools
    ), f"create payload must include mcp_toolset referencing daimon-mcp; got tools={tools!r}"


@pytest.mark.asyncio
async def test_agents_create_stamps_spec_hash_guidance_and_guild_account(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """create payload carries daimon_spec_hash, guild-account stamp, and the
    credential-guidance sentinel in the system prompt."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id
    guild_account = derive_guild_account_uuid(tenant_id)

    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_new", name="stamped-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([]))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text("name: stamped-agent\nmodel: claude-sonnet-4-6\nsystem: hello\n")

    await agents_create(rt=rt, console=console, path=spec_path)

    assert len(created_bodies) == 1, f"expected 1 POST to /v1/agents, got {len(created_bodies)}"
    body = created_bodies[0]
    md = cast(dict[str, object], body.get("metadata") or {})

    assert md.get("daimon_spec_hash"), (
        "create payload metadata must include a non-empty daimon_spec_hash"
    )
    assert md.get("daimon_account") == str(guild_account), (
        "create payload metadata must stamp the guild account, not the personal CLI principal"
    )
    # managed=False means the key is absent (build_metadata only writes daimon_managed when True)
    assert "daimon_managed" not in md, (
        "managed=False must leave daimon_managed key absent from metadata"
    )
    system = cast(str, body.get("system") or "")
    assert "<!-- daimon:credential-guidance v1 -->" in system, (
        "create payload system must include the credential-guidance sentinel block"
    )


# ---------------------------------------------------------------------------
# Task 2: agents_fork — three-merge sequence + destination collision guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_fork_merges_daimon_mcp_when_public_url_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork payload contains daimon-mcp mcp_servers entry, mcp_toolset tools entry,
    AND the agent_toolset_20260401 base toolset — all three guarantees."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    source_data = _agent_json(
        agent_id="ag_source", name="base-agent", version=1, tenant_id=tenant_id
    )
    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_fork", name="forked-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    # list for dst collision check (empty = no collision) and src tag lookup
    router.add("GET", r"/v1/agents", lambda req, m: list_response([source_data]))
    router.add("GET", r"/v1/agents/ag_source", lambda req, m: httpx.Response(200, json=source_data))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt_with_url(db_session_factory, router)

    await agents_fork(rt=rt, console=console, src="base-agent", dst="forked-agent")

    assert len(created_bodies) == 1, f"expected 1 POST to /v1/agents, got {len(created_bodies)}"
    body = created_bodies[0]
    servers = cast(list[dict[str, object]], body.get("mcp_servers") or [])
    tools = cast(list[dict[str, object]], body.get("tools") or [])
    assert any(
        s.get("name") == "daimon-mcp" and s.get("url") == "https://mcp.example.com/mcp"
        for s in servers
    ), f"fork payload must include daimon-mcp mcp_servers entry; got mcp_servers={servers!r}"
    assert any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "daimon-mcp" for t in tools
    ), f"fork payload must include mcp_toolset referencing daimon-mcp; got tools={tools!r}"
    tool_types = [t.get("type") for t in tools]
    assert "agent_toolset_20260401" in tool_types, (
        "fork payload must include agent_toolset_20260401 base toolset; "
        f"got tool types {tool_types!r}"
    )


@pytest.mark.asyncio
async def test_agents_fork_skips_mcp_merge_when_public_url_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork payload has no daimon-mcp entries when public_url is None (no-op merge)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    source_data = _agent_json(
        agent_id="ag_source", name="base-agent", version=1, tenant_id=tenant_id
    )
    created_bodies: list[dict[str, object]] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=_agent_json(agent_id="ag_fork", name="forked-agent", tenant_id=tenant_id),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([source_data]))
    router.add("GET", r"/v1/agents/ag_source", lambda req, m: httpx.Response(200, json=source_data))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    # public_url=None via the default _build_rt fixture
    rt = _build_rt(db_session_factory, router)

    await agents_fork(rt=rt, console=console, src="base-agent", dst="forked-agent")

    assert len(created_bodies) == 1, f"expected 1 POST to /v1/agents, got {len(created_bodies)}"
    body = created_bodies[0]
    servers = cast(list[dict[str, object]], body.get("mcp_servers") or [])
    tools = cast(list[dict[str, object]], body.get("tools") or [])
    assert not any(s.get("name") == "daimon-mcp" for s in servers), (
        f"no daimon-mcp server expected when public_url is None; got mcp_servers={servers!r}"
    )
    assert not any(t.get("mcp_server_name") == "daimon-mcp" for t in tools), (
        f"no daimon-mcp toolset expected when public_url is None; got tools={tools!r}"
    )


@pytest.mark.asyncio
async def test_agents_fork_rejects_when_destination_name_exists_in_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork raises StoreError and makes zero POST calls when the destination name
    is already held by an agent in the tenant (regardless of account ownership)."""
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
        tenant_id = tenant.id

    # Source agent the user wants to fork
    source_data = _agent_json(
        agent_id="ag_source", name="base-agent", version=1, tenant_id=tenant_id
    )
    # Destination name already taken by an agent with a different account stamp
    other_account = uuid.uuid4()
    dst_agent = _agent_json(
        agent_id="ag_dst",
        name="forked-agent",
        tenant_id=tenant_id,
        metadata={"daimon_account": str(other_account)},
    )

    post_calls: list[str] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        post_calls.append(req.url.path)
        return httpx.Response(200, json=dst_agent)

    router = MARouter()
    # list returns both agents so the dst collision check finds dst_agent
    router.add("GET", r"/v1/agents", lambda req, m: list_response([source_data, dst_agent]))
    router.add("POST", r"/v1/agents", on_create)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    with pytest.raises(StoreError, match="already exists in this server"):
        await agents_fork(rt=rt, console=console, src="base-agent", dst="forked-agent")

    assert post_calls == [], (
        "fork must not POST to /v1/agents when destination name already exists in the tenant"
    )

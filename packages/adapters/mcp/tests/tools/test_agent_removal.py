from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_removal import (
    _detach_mcp_server_impl,
    _list_agent_keys_impl,
    _remove_agent_key_impl,
    _remove_skill_impl,
)
from daimon.adapters.mcp.tools.agents import AgentInfo
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from factories import make_ma_agent
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_ALLOW_ALL: dict[str, Any] = {"enabled": True, "permission_policy": {"type": "always_allow"}}


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.mcp.public_url = None
    return settings


def _runtime(
    client: AsyncAnthropic,
    *,
    session_factory: async_sessionmaker[AsyncSession] | MagicMock | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=session_factory if session_factory is not None else MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=_make_settings(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
        fernet=None,
    )


def _conflict_response() -> httpx.Response:
    """MA's 409 stale-version conflict shape (mirrors test_agents.py)."""
    return httpx.Response(
        409,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Concurrent modification detected. Please fetch the latest version and retry.",
            },
        },
    )


def _build_no_retry_anthropic(router: MARouter) -> AsyncAnthropic:
    """AsyncAnthropic with the SDK's own 409 auto-retry disabled (mirrors test_agents.py)."""
    return AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(router.dispatch),
            base_url="https://api.anthropic.com",
        ),
        max_retries=0,
    )


def _spec_agent_router(
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    agent_name: str,
    agent_id: str = "ag_removal",
    mcp_servers: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    skills: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str], AsyncAnthropic]:
    """MARouter + fake client for one agent; captures the PATCH body and a
    request log (method+path) so tests can assert zero requests were issued."""
    captured: dict[str, Any] = {}
    request_log: list[str] = []
    metadata = {"daimon_tenant": str(tenant_id), "daimon_name": agent_name}
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)

    def _agent_payload() -> dict[str, Any]:
        return make_ma_agent(
            id=agent_id,
            name=agent_name,
            mcp_servers=mcp_servers or [],
            tools=tools or [],
            skills=skills or [],
            metadata=metadata,
        ).model_dump(mode="json")

    def on_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        request_log.append("GET /v1/agents")
        return list_response([_agent_payload()])

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        request_log.append("GET /v1/agents/{id}")
        return httpx.Response(200, json=_agent_payload())

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        request_log.append("POST /v1/agents/{id}")
        captured.update(json_body(req))
        return httpx.Response(
            200, json=make_ma_agent(id=agent_id, name=agent_name).model_dump(mode="json")
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", on_list)
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    return captured, request_log, build_fake_anthropic(router.dispatch)


async def _make_tenant_with_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_name: str | None,
) -> uuid.UUID:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        if agent_name is not None:
            await set_fields(
                session,
                scope=TenantScopeRef(tenant_id=tenant.id),
                tenant_id=tenant.id,
                agent_name=agent_name,
                mode="agent",
            )
    return tenant.id


# ---------------------------------------------------------------------------
# detach_mcp_server
# ---------------------------------------------------------------------------


async def test_detach_mcp_server_impl_removes_server_and_matching_toolset() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="demo",
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "ctx7",
                "configs": [],
                "default_config": _ALLOW_ALL,
            },
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": _ALLOW_ALL,
            },
        ],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _detach_mcp_server_impl(
        _runtime(client), auth, agent_name="demo", server_name="ctx7"
    )

    assert isinstance(result, AgentInfo)
    assert captured.get("mcp_servers") == [], "the detached server must be gone from mcp_servers"
    tool_types_and_names = [
        (t.get("type"), t.get("mcp_server_name")) for t in captured.get("tools", [])
    ]
    assert ("mcp_toolset", "ctx7") not in tool_types_and_names, (
        "the matching mcp_toolset entry must be removed alongside the server"
    )
    assert ("agent_toolset_20260401", None) in tool_types_and_names, "other tools must be preserved"


async def test_detach_mcp_server_impl_raises_when_not_attached_and_issues_no_update() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    _captured, request_log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="demo",
        mcp_servers=[{"name": "other", "type": "url", "url": "https://other.example/mcp"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="other"):
        await _detach_mcp_server_impl(_runtime(client), auth, agent_name="demo", server_name="ctx7")
    assert "POST /v1/agents/{id}" not in request_log, (
        "a typo'd server name must not issue an update"
    )


async def test_detach_mcp_server_impl_rejects_reserved_server_before_any_request() -> None:
    _captured, request_log, client = _spec_agent_router(
        tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), agent_name="demo"
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
    )
    with pytest.raises(ToolError, match="reserved"):
        await _detach_mcp_server_impl(
            _runtime(client), auth, agent_name="demo", server_name=DAIMON_MCP_SERVER_NAME
        )
    assert request_log == [], "the reserved-name guard must fire before any agent lookup"


async def test_detach_mcp_server_impl_rejects_system_agent_no_daimon_account() -> None:
    tenant_id = uuid.uuid4()
    _captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=None,
        agent_name="daimon",
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    with pytest.raises(ToolError, match="system agent"):
        await _detach_mcp_server_impl(
            _runtime(client), auth, agent_name="daimon", server_name="ctx7"
        )


async def test_detach_mcp_server_impl_rejects_non_admin_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="scoped-agent",
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _detach_mcp_server_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="scoped-agent",
            server_name="ctx7",
        )


async def test_detach_mcp_server_impl_allows_non_admin_when_agent_unreachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="unscoped-agent",
        mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    await _detach_mcp_server_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="unscoped-agent",
        server_name="ctx7",
    )
    assert captured.get("mcp_servers") == [], "an unreachable agent's detach is not gated"


async def test_detach_mcp_server_impl_retries_once_on_version_conflict() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "demo",
        "daimon_account": str(account_id),
    }
    update_calls: list[int] = []

    def on_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                make_ma_agent(
                    id="ag_conflict",
                    name="demo",
                    mcp_servers=[
                        {"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}
                    ],
                    metadata=metadata,
                ).model_dump(mode="json")
            ]
        )

    def on_retrieve(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=make_ma_agent(
                id="ag_conflict",
                name="demo",
                mcp_servers=[{"name": "ctx7", "type": "url", "url": "https://ctx7.example/mcp"}],
                version=1,
                metadata=metadata,
            ).model_dump(mode="json"),
        )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(len(update_calls) + 1)
        if len(update_calls) == 1:
            return _conflict_response()
        return httpx.Response(
            200,
            json=make_ma_agent(id="ag_conflict", name="demo", version=2).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", on_list)
    router.add("GET", r"/v1/agents/([^/]+)", on_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = _build_no_retry_anthropic(router)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _detach_mcp_server_impl(
        _runtime(client), auth, agent_name="demo", server_name="ctx7"
    )
    assert isinstance(result, AgentInfo)
    assert len(update_calls) == 2, "must retry exactly once after a version conflict"


# ---------------------------------------------------------------------------
# remove_skill
# ---------------------------------------------------------------------------


async def test_remove_skill_impl_detaches_by_raw_skill_id_preserving_others() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="demo",
        skills=[
            {"type": "anthropic", "skill_id": "skill_x", "version": "1"},
            {"type": "anthropic", "skill_id": "skill_y", "version": "1"},
        ],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _remove_skill_impl(_runtime(client), auth, agent_name="demo", skill_id="skill_x")

    assert isinstance(result, AgentInfo)
    assert captured.get("skills") == [{"skill_id": "skill_y", "type": "anthropic"}]
    assert "tools" not in captured, "remove_skill must not touch the base tool set"


async def test_remove_skill_impl_detaches_by_resolved_display_name() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "demo",
        "daimon_account": str(account_id),
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                make_ma_agent(
                    id="ag_removal",
                    name="demo",
                    skills=[{"type": "custom", "skill_id": "skill_build", "version": "1"}],
                    metadata=metadata,
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda _r, _m: list_response(
            [
                SkillListResponse(
                    id="skill_build",
                    type="custom",
                    display_title=f"{str(tenant_id)[:8]}-build-models",
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    captured: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200, json=make_ma_agent(id="ag_removal", name="demo").model_dump(mode="json")
        )

    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=make_ma_agent(
                id="ag_removal",
                name="demo",
                skills=[{"type": "custom", "skill_id": "skill_build", "version": "1"}],
                metadata=metadata,
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _remove_skill_impl(
        _runtime(client), auth, agent_name="demo", skill_id="build-models"
    )
    assert isinstance(result, AgentInfo)
    assert captured.get("skills") == [], "resolved display name must match and detach the skill"


async def test_remove_skill_impl_raises_when_nothing_matches_listing_attached() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    _captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="demo",
        skills=[{"type": "anthropic", "skill_id": "skill_x", "version": "1"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="skill_x"):
        await _remove_skill_impl(
            _runtime(client), auth, agent_name="demo", skill_id="does-not-exist"
        )


async def test_remove_skill_impl_rejects_system_agent_no_daimon_account() -> None:
    tenant_id = uuid.uuid4()
    _captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=None,
        agent_name="daimon",
        skills=[{"type": "anthropic", "skill_id": "skill_x", "version": "1"}],
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )
    with pytest.raises(ToolError, match="system agent"):
        await _remove_skill_impl(_runtime(client), auth, agent_name="daimon", skill_id="skill_x")


async def test_remove_skill_impl_rejects_non_admin_when_agent_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    _captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="scoped-agent",
        skills=[{"type": "anthropic", "skill_id": "skill_x", "version": "1"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    with pytest.raises(ToolError, match="admin must change"):
        await _remove_skill_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="scoped-agent",
            skill_id="skill_x",
        )


async def test_remove_skill_impl_allows_non_admin_when_agent_unreachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name=None)
    captured, _log, client = _spec_agent_router(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="unscoped-agent",
        skills=[{"type": "anthropic", "skill_id": "skill_x", "version": "1"}],
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    await _remove_skill_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="unscoped-agent",
        skill_id="skill_x",
    )
    assert captured.get("skills") == [], "an unreachable agent's skill removal is not gated"


# ---------------------------------------------------------------------------
# list_agent_keys / remove_agent_key
# ---------------------------------------------------------------------------


def _agent_only_router(
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    agent_id: str,
    account_id: uuid.UUID | None,
) -> AsyncAnthropic:
    metadata = {"daimon_tenant": str(tenant_id), "daimon_name": agent_name}
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [make_ma_agent(id=agent_id, name=agent_name, metadata=metadata).model_dump(mode="json")]
        ),
    )
    return build_fake_anthropic(router.dispatch)


def _multi_agent_router(
    *, tenant_id: uuid.UUID, agents: list[tuple[str, str, uuid.UUID | None]]
) -> AsyncAnthropic:
    """agents: list of (agent_id, agent_name, account_id)."""
    payloads: list[dict[str, Any]] = []
    for agent_id, agent_name, account_id in agents:
        metadata = {"daimon_tenant": str(tenant_id), "daimon_name": agent_name}
        if account_id is not None:
            metadata["daimon_account"] = str(account_id)
        payloads.append(
            make_ma_agent(id=agent_id, name=agent_name, metadata=metadata).model_dump(mode="json")
        )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(payloads))
    return build_fake_anthropic(router.dispatch)


async def test_list_agent_keys_impl_returns_sorted_key_names_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = "ag_env"
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="demo", agent_id=agent_id, account_id=uuid.uuid4()
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", id=tenant_id)
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="ZKEY", content="v1"
        )
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="AKEY", content="v2"
        )
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="MKEY", content="v3"
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    result = await _list_agent_keys_impl(
        _runtime(client, session_factory=db_session_factory), auth, agent_name="demo"
    )
    assert result == ["AKEY", "MKEY", "ZKEY"], "must return only the three key names, sorted"


async def test_list_agent_keys_impl_returns_empty_list_when_none_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="demo", agent_id="ag_empty", account_id=uuid.uuid4()
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    result = await _list_agent_keys_impl(
        _runtime(client, session_factory=db_session_factory), auth, agent_name="demo"
    )
    assert result == [], "no variables set must return an empty list, not an error"


async def test_list_agent_keys_impl_raises_when_agent_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    with pytest.raises(ToolError, match="not found"):
        await _list_agent_keys_impl(
            _runtime(client, session_factory=db_session_factory), auth, agent_name="ghost"
        )


async def test_list_agent_keys_impl_never_returns_a_stored_value(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = "ag_secret"
    distinctive_value = "sk-super-secret-distinctive-value-9f2a"
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="demo", agent_id=agent_id, account_id=uuid.uuid4()
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", id=tenant_id)
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key="API_KEY",
            content=distinctive_value,
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    result = await _list_agent_keys_impl(
        _runtime(client, session_factory=db_session_factory), auth, agent_name="demo"
    )
    assert result == ["API_KEY"]
    assert distinctive_value not in str(result), (
        "the listing must never surface the stored value under any name"
    )


async def test_list_agent_keys_impl_callable_by_non_admin_on_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="scoped-agent", agent_id="ag_scoped", account_id=account_id
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    result = await _list_agent_keys_impl(
        _runtime(client, session_factory=db_session_factory), auth, agent_name="scoped-agent"
    )
    assert result == [], "the listing is not reachability-gated, even on a default agent"


async def test_remove_agent_key_impl_removes_existing_key(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = "ag_env"
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="demo", agent_id=agent_id, account_id=uuid.uuid4()
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", id=tenant_id)
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="API_KEY", content="v1"
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    runtime = _runtime(client, session_factory=db_session_factory)
    result = await _remove_agent_key_impl(runtime, auth, agent_name="demo", key="API_KEY")
    assert result.removed is True
    assert result.agent_name == "demo"
    assert result.key == "API_KEY"

    remaining = await _list_agent_keys_impl(runtime, auth, agent_name="demo")
    assert remaining == [], "a follow-up listing must no longer contain the removed key"


async def test_remove_agent_key_impl_is_idempotent_when_key_absent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="demo", agent_id="ag_env2", account_id=uuid.uuid4()
    )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    result = await _remove_agent_key_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="demo",
        key="NEVER_SET",
    )
    assert result.removed is False, "removing an absent key must succeed idempotently"


async def test_remove_agent_key_impl_raises_when_agent_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    with pytest.raises(ToolError, match="not found"):
        await _remove_agent_key_impl(
            _runtime(client, session_factory=db_session_factory),
            auth,
            agent_name="ghost",
            key="API_KEY",
        )


async def test_remove_agent_key_impl_callable_by_non_admin_on_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = uuid.uuid4()
    tenant_id = await _make_tenant_with_default_agent(db_session_factory, agent_name="scoped-agent")
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="scoped-agent", agent_id="ag_scoped2", account_id=account_id
    )

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.USER, is_admin=False)
    result = await _remove_agent_key_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="scoped-agent",
        key="ANY_KEY",
    )
    assert result.removed is False, "removal is not reachability-gated, even on a default agent"


async def test_remove_agent_key_impl_succeeds_against_seeded_agent_with_no_daimon_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    agent_id = "ag_seeded"
    client = _agent_only_router(
        tenant_id=tenant_id, agent_name="daimon", agent_id=agent_id, account_id=None
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", id=tenant_id)
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="SEEDED_KEY", content="v1"
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    result = await _remove_agent_key_impl(
        _runtime(client, session_factory=db_session_factory),
        auth,
        agent_name="daimon",
        key="SEEDED_KEY",
    )
    assert result.removed is True, "the system-agent guard must not apply to env-variable removal"


async def test_remove_agent_key_impl_isolates_between_agents_in_same_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    agent_a_id, agent_b_id = "ag_a", "ag_b"
    client = _multi_agent_router(
        tenant_id=tenant_id,
        agents=[
            (agent_a_id, "agent-a", uuid.uuid4()),
            (agent_b_id, "agent-b", uuid.uuid4()),
        ],
    )
    agent_a_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_a_id)
    agent_b_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_b_id)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", id=tenant_id)
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_a_uuid, key="SHARED", content="a-value"
        )
        await put_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_b_uuid, key="SHARED", content="b-value"
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=False
    )
    runtime = _runtime(client, session_factory=db_session_factory)
    result = await _remove_agent_key_impl(runtime, auth, agent_name="agent-a", key="SHARED")
    assert result.removed is True

    remaining = await _list_agent_keys_impl(runtime, auth, agent_name="agent-b")
    assert remaining == ["SHARED"], "removing from agent-a must not affect agent-b's SHARED key"

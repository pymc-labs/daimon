"""The chat tool and the shared edit service send Managed Agents the same writes."""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _set_agent_builtin_tool_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import agent_editing
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentPatch
from daimon.core.authz import Subject, Surface
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ALLOW: dict[str, Any] = {"type": "always_allow"}
_TOOLSET: dict[str, Any] = {
    "type": "agent_toolset_20260401",
    "default_config": {"enabled": True, "permission_policy": _ALLOW},
    "configs": [{"name": "web_search", "enabled": False, "permission_policy": _ALLOW}],
}


def _recording_client(tenant_id: uuid.UUID, writes: list[dict[str, Any]]) -> AsyncAnthropic:
    """One editable agent; every update body is recorded."""
    agent = ma_agent(
        id="ag_a",
        name="a",
        tenant_id=tenant_id,
        metadata={"daimon_name": "a", "daimon_account": str(uuid.uuid4())},
        tools=[_TOOLSET],
    )

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        writes.append(json_body(req))
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents$", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    router.add_agent(agent)
    router.add("POST", r"/v1/agents/ag_a$", on_update)
    return build_fake_anthropic(router.dispatch)


def _runtime(client: AsyncAnthropic, factory: async_sessionmaker[AsyncSession]) -> McpRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.tool_safety.denied = ()
    return McpRuntime(
        session_factory=factory,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _actor(tenant_id: uuid.UUID, account_id: uuid.UUID) -> AgentActor:
    return AgentActor(
        tenant_id=tenant_id,
        account_id=account_id,
        subject=Subject(is_admin=True),
        surface=Surface.WEB,
    )


async def test_update_agent_tool_and_service_make_the_same_ma_write(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    model = ALLOWED_MODEL_IDS[0]
    tools: list[Any] = [{"type": "mcp_toolset", "mcp_server_name": "docs"}]
    servers: list[Any] = [{"name": "docs", "type": "url", "url": "https://docs.example.com/mcp"}]

    via_tool: list[dict[str, Any]] = []
    auth = AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN,
        is_admin=True,
        is_platform_admin=True,
    )
    await _update_agent_impl(
        _runtime(_recording_client(tenant_id, via_tool), db_session_factory),
        auth,
        name="a",
        model=model,
        description="Answers questions",
        system="Be brief.",
        tools=tools,
        mcp_servers=servers,
        skills=None,
    )

    via_service: list[dict[str, Any]] = []
    deps = AgentEditDeps(
        client=_recording_client(tenant_id, via_service),
        session_factory=db_session_factory,
        public_url=None,
    )
    await agent_editing.update_agent(
        deps,
        actor=_actor(tenant_id, account_id),
        ma_agent_id="ag_a",
        patch=AgentPatch(
            model=model,
            description="Answers questions",
            system="Be brief.",
            tools=tools,
            mcp_servers=servers,
        ),
        expected_version=None,
    )

    assert len(via_tool) == 1, "the tool writes once"
    assert via_tool == via_service, "the tool and the service send the same update body"


async def test_set_builtin_tool_and_service_make_the_same_ma_write(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    via_tool: list[dict[str, Any]] = []
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _set_agent_builtin_tool_impl(
        _runtime(_recording_client(tenant_id, via_tool), db_session_factory),
        auth,
        "a",
        tool="web_search",
        enabled=True,
    )

    via_service: list[dict[str, Any]] = []
    deps = AgentEditDeps(
        client=_recording_client(tenant_id, via_service),
        session_factory=db_session_factory,
        public_url=None,
    )
    await agent_editing.write_builtin_tool(
        deps,
        actor=_actor(tenant_id, account_id),
        ma_agent_id="ag_a",
        tool="web_search",
        enabled=True,
    )

    assert len(via_tool) == 1, "the tool writes once"
    assert via_tool == via_service, "the tool and the service send the same toolset"

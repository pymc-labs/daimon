"""Member setup writes require the target's channel rule, including private MCP forms."""

from __future__ import annotations

import copy
from dataclasses import replace

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.tools.agent_removal import (
    _detach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _remove_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.agents import (
    _attach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.reachability import channel_admin_caller
from daimon.adapters.mcp.tools.skill_uploads import (
    _add_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_attach import decide_mcp_connect
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_skill_uploads import _MD, ROOM, USER, _world  # pyright: ignore[reportPrivateUsage]

OTHER = "222222222222222222"
URL = "https://example.com/mcp"


@pytest.mark.parametrize("target", ["base", "helper", "other-team"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize(
    "operation",
    [
        "system",
        "model",
        "description",
        "tools",
        "skills",
        "mcp_servers",
        "attach",
        "detach",
        "add_skill",
        "remove_skill",
        "token",
        "oauth",
    ],
)
async def test_mutation_target_ownership(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    target: str,
    admin: bool,
    operation: str,
) -> None:
    world = await _world(committing_sessionmaker)
    if operation == "model":
        # MA returns a model config even when the update accepts a model id.
        inner = world.runtime.client._client._transport  # pyright: ignore[reportPrivateUsage]

        async def handler(request: httpx.Request) -> httpx.Response:
            response = await inner.handle_async_request(request)
            if request.method == "POST" and request.url.path.startswith("/v1/agents/"):
                body = response.json()
                if isinstance(body.get("model"), str):
                    body["model"] = {"id": body["model"], "speed": None}
                    return httpx.Response(response.status_code, json=body)
            return response

        world.runtime = replace(
            world.runtime,
            client=AsyncAnthropic(
                api_key="test",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
        )
    async with committing_sessionmaker.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=ROOM,
            role_ids=[],
            user_ids=[USER],
            actor_account_id=None,
        )
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(
                agent_rules={
                    "helper": AgentRule(runs_in=(ROOM,)),
                    "other-team": AgentRule(runs_in=(OTHER,)),
                }
            ),
        )
        for channel, name in ((ROOM, "helper"), (OTHER, "other-team")):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=channel),
                tenant_id=world.tenant_id,
                agent_name=name,
                mode="agent",
                set_by_admin=True,
            )
    # All targets share the guild account stamp. It is never proof of authorship.
    agent = ma_agent(
        id=f"agent_{target}",
        name=target,
        tenant_id=world.tenant_id,
        metadata={"daimon_account": str(world.account_id)},
        mcp_servers=[{"name": "existing", "type": "url", "url": "https://old.example/mcp"}],
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "existing",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
        ],
        skills=[{"type": "anthropic", "skill_id": "pptx", "version": "1"}],
    )
    world.state.agents[agent.id] = agent.model_dump(mode="json")
    auth = (
        world.auth()
        if admin
        else replace(
            world.auth(admin=False, platform="discord"),
            chat_agent_id=derive_agent_uuid(
                tenant_id=world.tenant_id, ma_agent_id="agent_ordinary"
            ),
            administered_channel_ids=frozenset({ROOM}),
        )
    )
    before = copy.deepcopy(world.state.agents)
    allowed = admin or target == "helper"

    async def mutate() -> None:
        if operation in ("token", "oauth"):
            decision = await decide_mcp_connect(
                world.runtime.session_factory,
                tenant_id=world.tenant_id,
                agent=agent,
                agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id=agent.id),
                server_name="new",
                url=URL,
                platform="discord",
                caller=channel_admin_caller(auth),
                default=world.runtime.deployment_default,
                shares_token=operation == "token",
            )
            assert not decision.replaces, "a new server still needs ownership"
            assert decision.refused is not allowed
        elif operation == "attach":
            await _attach_mcp_server_impl(
                world.runtime,
                auth,
                agent_name=target,
                server_name="new",
                url=URL,
                expected_ma_agent_id=agent.id,
            )
        elif operation == "detach":
            await _detach_mcp_server_impl(
                world.runtime,
                auth,
                agent_name=target,
                server_name="existing",
                expected_ma_agent_id=agent.id,
            )
        elif operation == "add_skill":
            result = await _add_skill_impl(
                world.runtime, auth, agent_name=target, expected_ma_agent_id=agent.id, skill_md=_MD
            )
            assert result.status == "preview"
        elif operation == "remove_skill":
            await _remove_skill_impl(
                world.runtime,
                auth,
                agent_name=target,
                skill_id="pptx",
                expected_ma_agent_id=agent.id,
            )
        else:
            await _update_agent_impl(
                world.runtime,
                auth,
                target,
                expected_ma_agent_id=agent.id,
                model="claude-opus-4-8" if operation == "model" else None,
                description="Updated" if operation == "description" else None,
                system="Updated instructions" if operation == "system" else None,
                tools=[{"type": "mcp_toolset", "mcp_server_name": "new"}]
                if operation == "mcp_servers"
                else [{"type": "agent_toolset_20260401"}]
                if operation == "tools"
                else None,
                skills=[{"type": "anthropic", "skill_id": "pptx", "version": "1"}]
                if operation == "skills"
                else None,
                mcp_servers=[{"name": "new", "type": "url", "url": URL}]
                if operation == "mcp_servers"
                else None,
            )

    if allowed or operation in ("token", "oauth"):
        await mutate()
    else:
        with pytest.raises(ToolError):
            await mutate()
    if not allowed:
        assert world.state.agents == before
        assert world.created == [], "refusal must precede uploading any skill"


@pytest.mark.parametrize("operation", ["system", "attach", "add_skill"])
@pytest.mark.parametrize("agent_key", [False, True], ids=["direct-member", "agent-key"])
async def test_unbound_base_outside_chat_requires_an_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession], operation: str, agent_key: bool
) -> None:
    world = await _world(committing_sessionmaker)
    auth = world.auth(admin=False, platform="discord")
    if agent_key:
        auth = replace(
            auth, agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_helper")
        )
    assert auth.chat_agent_id is None and not auth.administered_channel_ids
    before = copy.deepcopy(world.state.agents)
    with pytest.raises(ToolError, match="admin"):
        if operation == "system":
            await _update_agent_impl(
                world.runtime,
                auth,
                "helper",
                expected_ma_agent_id="agent_helper",
                model=None,
                description=None,
                system="Unbound edit",
                tools=None,
                mcp_servers=None,
                skills=None,
            )
        elif operation == "attach":
            await _attach_mcp_server_impl(
                world.runtime,
                auth,
                agent_name="helper",
                expected_ma_agent_id="agent_helper",
                server_name="new",
                url=URL,
            )
        else:
            await _add_skill_impl(
                world.runtime,
                auth,
                agent_name="helper",
                expected_ma_agent_id="agent_helper",
                skill_md=_MD,
            )
    assert world.state.agents == before and world.created == []

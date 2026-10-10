"""Members discover only agents that can answer at their authenticated location."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_removal import _list_agent_keys_impl
from daimon.adapters.mcp.tools.agents import _get_agent_impl, _list_agents_impl
from daimon.adapters.mcp.tools.propagation import _explain_agent_resolution_impl
from daimon.adapters.mcp.tools.skills import _get_impl, _list_impl
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("platform", ["slack", "discord"])
@pytest.mark.parametrize("caller", ["member", "admin", "external", "bound-key", "unbound-key"])
@pytest.mark.parametrize("origin_kind", ["valid", "omitted", "other-account", "expired"])
async def test_agent_reads_follow_member_location_and_preserve_admin_visibility(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
    caller: str,
    origin_kind: str,
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                agent_channel_pins={
                    "local": ("CLOCAL",),
                    "other": ("COTHER",),
                }
            ),
        )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=other.id if origin_kind == "other-account" else account.id,
            platform=platform,
            parent_channel_id="CLOCAL",
            thread_id="123.456",
            responder_ma_agent_id="ag_shared",
            responder_name="shared",
            configuration_target_ma_agent_id="ag_shared",
            configuration_target_name="shared",
            role=Role.USER,
            now=now,
            expires_at=now + dt.timedelta(minutes=-1 if origin_kind == "expired" else 10),
        )
    router = MARouter()
    router.add_agent_list(
        ma_agent(id="ag_shared", name="shared", tenant_id=tenant.id),
        ma_agent(id="ag_draft", name="draft", tenant_id=tenant.id),
        ma_agent(id="ag_local", name="local", tenant_id=tenant.id),
        ma_agent(
            id="ag_other", name="renamed", tenant_id=tenant.id, metadata={"daimon_name": "other"}
        ),
    )
    skills = [
        SkillListResponse(
            id=f"sk_{name}",
            display_title=tenant_scoped_display_title(tenant_id=tenant.id, name=f"{name}/notes"),
            source="custom",
            type="custom",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            latest_version="v1",
        ).model_dump(mode="json")
        for name in ("shared", "local", "renamed", "draft")
    ]
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response(skills))
    router.add("GET", r"/v1/skills/sk_shared/versions", lambda _r, _m: list_response([]))
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),
        deployment_default=DeploymentDefault(agent_name="shared"),
    )
    executing = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_shared")
    auth = AuthIdentity(
        tenant_id=tenant.id,
        account_id=account.id,
        role=Role.USER,
        platform=platform,
        is_admin=caller == "admin",
        is_external=caller == "external",
        chat_agent_id=executing,
    )
    if caller.endswith("key"):
        auth = replace(
            auth,
            agent_id=executing,
            chat_agent_id=None,
            bound_channel_id="CLOCAL" if caller == "bound-key" else None,
        )
    origin_id = None if origin_kind == "omitted" else str(origin.id)
    expected: set[str] = set()
    if caller == "admin":
        expected |= {"shared", "local", "renamed", "draft"}
    elif caller == "bound-key" or (caller in {"member", "external"} and origin_kind == "valid"):
        expected |= {"shared", "local"}
    result = await _list_agents_impl(runtime, auth, None, origin_id)
    assert {agent.name for agent in result} == expected
    assert {skill.name for skill in await _list_impl(runtime, auth, origin_id)} == {
        f"{name}/notes" for name in expected
    }
    if "shared" in expected:
        assert (await _get_impl(runtime, auth, "shared/notes", origin_id)).id == "sk_shared"
        explanation = await _explain_agent_resolution_impl(
            runtime, auth, "CLOCAL", origin_context_id=origin_id
        )
        assert explanation.effective_agent_name == "shared"
    else:
        with pytest.raises(ToolError, match="not found"):
            await _get_impl(runtime, auth, "shared/notes", origin_id)
        with pytest.raises(ToolError, match="verified channel/thread"):
            await _explain_agent_resolution_impl(
                runtime, auth, "CLOCAL", origin_context_id=origin_id
            )
    if caller != "admin":
        with pytest.raises(ToolError, match="not found"):
            await _get_impl(runtime, auth, "draft/notes", origin_id)
        with pytest.raises(ToolError, match="verified channel/thread"):
            await _explain_agent_resolution_impl(
                runtime, auth, "COTHER", origin_context_id=origin_id
            )
        with pytest.raises(ToolError, match="verified channel/thread"):
            await _explain_agent_resolution_impl(
                runtime, auth, "CLOCAL", "unrelated-thread", origin_id
            )
        with pytest.raises(ToolError, match="not found"):
            await _get_agent_impl(runtime, auth, "renamed", origin_context_id=origin_id)
        with pytest.raises(ToolError, match="not found"):
            await _list_agent_keys_impl(
                runtime, auth, agent_name="renamed", origin_context_id=origin_id
            )


@pytest.mark.parametrize("platform", ["slack", "discord", "teams"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
async def test_routing_explanation_hides_draft_targets_and_other_threads_from_members(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
    admin: bool,
    deleted: bool,
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform)
        account = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="CLOCAL"),
            tenant_id=tenant.id,
            agent_name="local",
            mode="agent",
        )
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant.id),
            tenant_id=tenant.id,
            agent_name="shadowed-workspace",
            mode="agent",
        )
        for thread in ("current-thread", "other-thread"):
            await create_binding(
                session,
                tenant_id=tenant.id,
                platform=platform,
                parent_channel_id="CLOCAL",
                thread_id=thread,
                responder_ma_agent_id="ag_setup",
                responder_name="setup",
                configuration_target_ma_agent_id="ag_draft",
                configuration_target_name="draft",
            )
        if deleted:
            await update_lifecycle(
                session,
                tenant_id=tenant.id,
                platform=platform,
                parent_channel_id="CLOCAL",
                thread_id="current-thread",
                deleted=True,
            )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform=platform,
            parent_channel_id="CLOCAL",
            thread_id="current-thread",
            responder_ma_agent_id="ag_setup",
            responder_name="setup",
            configuration_target_ma_agent_id="ag_draft",
            configuration_target_name="draft",
            role=Role.USER,
            now=now,
            expires_at=now + dt.timedelta(minutes=10),
        )
    router = MARouter()
    router.add_agent_list(
        *[
            ma_agent(id=f"ag_{name}", name=name, tenant_id=tenant.id)
            for name in ("local", "setup", "draft", "shadowed-workspace", "shadowed-deployment")
        ]
    )
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),
        deployment_default=DeploymentDefault(agent_name="shadowed-deployment"),
    )
    auth = AuthIdentity(
        tenant_id=tenant.id,
        account_id=account.id,
        role=Role.USER,
        platform=platform,
        is_admin=admin,
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_setup"),
    )
    if deleted:
        with pytest.raises(ToolError, match="deleted") as error:
            await _explain_agent_resolution_impl(
                runtime, auth, "CLOCAL", "current-thread", str(origin.id)
            )
        assert ("draft" in str(error.value)) == admin
        # A deleted binding contributes no responder and cannot break discovery.
        roster = await _list_agents_impl(runtime, auth, None, str(origin.id))
        assert {row.name for row in roster} == (
            {"local", "setup", "draft", "shadowed-workspace", "shadowed-deployment"}
            if admin
            else {"local"}
        )
        return
    explanation = await _explain_agent_resolution_impl(
        runtime, auth, "CLOCAL", "current-thread", str(origin.id)
    )
    assert explanation.effective_agent_name == "setup"
    assert explanation.channel_default == "local"
    assert explanation.configuration_target_name == ("draft" if admin else None)
    assert explanation.configuration_target_ma_agent_id == ("ag_draft" if admin else None)
    assert explanation.tenant_default == ("shadowed-workspace" if admin else None)
    assert explanation.deployment_default == ("shadowed-deployment" if admin else None)
    assert {row.thread_id for row in explanation.recent_setup_conversations} == (
        {"current-thread", "other-thread"} if admin else {"current-thread"}
    )
    assert all(
        row.configuration_target_name == ("draft" if admin else None)
        for row in explanation.recent_setup_conversations
    )
    if not admin:
        assert "draft" not in explanation.explanation

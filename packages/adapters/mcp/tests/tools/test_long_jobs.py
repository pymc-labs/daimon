"""Registration proves the exact owner-approved provider call before queuing work."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import long_jobs as tools
from daimon.core.authz import Subject
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.long_job_models import LongJobPolicy
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import long_jobs as store
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import set_funding_mode
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.parametrize("mode", ["approved", "unapproved", "different_command", "deadline_over"])
async def test_start_binds_approved_provider_identity_before_any_launch(
    db_engine, db_clean, monkeypatch, mode
):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://unused/db"),
        anthropic=AnthropicSettings(api_key=SecretStr("fake")),
        _env_file=None,
    )
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        owner = await make_account(db, tenant=tenant)
        await set_funding_mode(db, tenant_id=tenant.id, funding_mode="operator_funded")
        await store.set_policy(
            db,
            tenant_id=tenant.id,
            agent_id=None,
            policy=LongJobPolicy(enabled=True),
            ceilings=settings.long_jobs,
            actor=Subject(is_admin=True),
            set_by=str(owner.id),
        )
    auth = AuthIdentity(
        tenant_id=tenant.id,
        account_id=owner.id,
        platform="discord",
        role=Role.USER,
        platform_user_id="user",
        chat_session_id="chat",
    )
    origin_id = str(uuid.uuid4())
    origin = SimpleNamespace(
        attended=True,
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        responder_ma_agent_id="agent_job",
        responder_name="worker",
    )
    monkeypatch.setattr(tools, "_auth", AsyncMock(return_value=auth))
    monkeypatch.setattr(tools, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(tools._session_gate, "session_card_gap", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tools,
        "get_live_thread_session",
        AsyncMock(return_value=SimpleNamespace(ma_session_id="ses_parent")),
    )
    monkeypatch.setattr(
        tools, "resolve", AsyncMock(return_value=SimpleNamespace(environment_name="default"))
    )
    env = ma_environment(id="env_job", updated_at="2026-10-23T00:00:00Z")
    monkeypatch.setattr(tools, "find_environment_by_daimon_tag", AsyncMock(return_value=env))
    now = datetime.now(UTC).isoformat()
    arguments = dict(command="sleep 600", title="fit", origin_context_id=origin_id)
    if mode == "deadline_over":
        arguments["deadline_minutes"] = 71
    wire_args = dict(arguments)
    if mode == "different_command":
        wire_args["command"] = "echo different"
    events = [
        {
            "id": "call_approved",
            "type": "agent.mcp_tool_use",
            "mcp_server_name": "daimon-mcp",
            "name": "start_long_job",
            "input": wire_args,
            "processed_at": now,
        }
    ]
    if mode != "unapproved":
        events.append(
            {
                "id": "allow",
                "type": "user.tool_confirmation",
                "tool_use_id": "call_approved",
                "result": "allow",
                "processed_at": now,
            }
        )
    agent = ma_agent(id="agent_job", version=8)
    parent = ma_session(
        id="ses_parent",
        agent=agent,
        environment_id=env.id,
        metadata={"daimon_tenant": str(tenant.id), "daimon_account": str(owner.id)},
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/ses_parent$",
        lambda _r, _m: httpx.Response(200, json=parent.model_dump(mode="json")),
    )
    router.add("GET", r"/v1/sessions/ses_parent/events$", lambda _r, _m: list_response(events))
    # No session-create route: any attempt to launch during registration fails.
    ma = build_fake_anthropic(router.dispatch)
    mcp = FastMCP("long jobs")
    tools.register_long_job_tools(
        mcp,
        McpRuntime(
            session_factory=sm, client=ma, settings=settings, deployment_default=DeploymentDefault()
        ),
    )
    try:
        async with Client(mcp) as client:
            if mode != "approved":
                with pytest.raises(ToolError):
                    await client.call_tool("start_long_job", arguments)
            else:
                first = await client.call_tool("start_long_job", arguments)
                second = await client.call_tool("start_long_job", arguments)
                assert first.data is not None and second.data is not None
                assert (
                    json.loads(first.content[0].text)["id"]
                    == json.loads(second.content[0].text)["id"]
                )
        async with sm.begin() as db:
            rows = await store.list_jobs(db, tenant_id=tenant.id, owner_account_id=owner.id)
            assert len(rows) == (1 if mode == "approved" else 0)
            if rows:
                [row] = rows
                assert row.owner_account_id == owner.id and row.parent_session_id == parent.id
                assert row.agent_id == agent.id and row.agent_version == 8
                assert row.environment_id == env.id and row.thread_id == "thread"
                assert row.tool_use_id == "call_approved" and row.worker_session_id is None
                assert row.state == "queued"
    finally:
        await ma.close()

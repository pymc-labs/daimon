"""Chat requests require an authenticated admin's active origin before lookups."""

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _request_agent_backend_switch_impl
from daimon.core.stores.domain import Role
from daimon.testing.db import db_nullpool_engine  # noqa: F401
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize("caller", ["member", "channel_admin", "agent_key", "headless"])
async def test_non_chat_admins_never_resolve_an_agent_or_preview(
    caller: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = AuthIdentity(
        tenant_id=uuid4(),
        account_id=uuid4(),
        role=Role.ADMIN,
        is_admin=True,
        platform="discord",
        platform_user_id="123",
    )
    if caller == "member":
        auth = replace(auth, role=Role.USER, is_admin=False)
    elif caller == "channel_admin":
        auth = replace(
            auth, role=Role.USER, is_admin=False, administered_channel_ids=frozenset({"456"})
        )
    elif caller == "agent_key":
        auth = replace(auth, agent_id=uuid4())
    else:
        auth = replace(auth, platform_user_id=None)
    runtime = MagicMock()
    origin = AsyncMock()
    monkeypatch.setattr("daimon.adapters.mcp.tools.agents.get_chat_origin", origin)
    with pytest.raises(ToolError):
        await _request_agent_backend_switch_impl(
            cast(McpRuntime, runtime), auth, agent_name="Researcher", target_backend="openai"
        )
    origin.assert_not_awaited()
    runtime.client.assert_not_called()
    runtime.session_factory.assert_not_called()


async def test_a_missing_active_origin_never_resolves_or_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = AuthIdentity(
        tenant_id=uuid4(),
        account_id=uuid4(),
        role=Role.ADMIN,
        is_admin=True,
        platform="discord",
        platform_user_id="123",
    )
    runtime = MagicMock()
    origin = AsyncMock(return_value=None)
    monkeypatch.setattr("daimon.adapters.mcp.tools.agents.get_chat_origin", origin)
    with pytest.raises(ToolError, match="active chat"):
        await _request_agent_backend_switch_impl(
            cast(McpRuntime, runtime),
            auth,
            agent_name="Researcher",
            target_backend="openai",
            preview_digest="a" * 64,
            expected_revision=0,
        )
    origin.assert_awaited_once()
    runtime.client.assert_not_called()
    runtime.session_factory.assert_not_called()


@pytest.fixture
async def active_chat(db_preflight, db_nullpool_engine):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    import httpx
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import BackendActor, make_backend_service
    from daimon.core.config import McpSettings, Settings, TurnSettings
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.turn_origins import create_origin
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import (
        make_account,
        make_platform_principal,
        make_tenant,
        make_thread_session,
    )
    from daimon.testing.ma import list_response
    from daimon.testing.ma_models import ma_agent, ma_session
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("Backend control proofs require an owned DB or isolated CI")
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="123"
        )
        mapping = await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            thread_id="thread",
            channel_id="channel",
            ma_session_id="sess_owned",
            ma_agent_id="agent_owned",
        )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            responder_ma_agent_id="agent_owned",
            responder_name="Researcher",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    tools = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {
                    "name": "request_agent_backend_switch",
                    "enabled": True,
                    "permission_policy": {"type": "always_ask"},
                }
            ],
        }
    ]
    native = ma_agent(id="agent_owned", tenant_id=str(tenant.id), name="Researcher", tools=tools)
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = "Researcher"
    frozen = ma_session(
        id="sess_owned",
        agent=native,
        metadata={"daimon_tenant": str(tenant.id), "daimon_account": str(account.id)},
    ).model_dump(mode="json")
    trace = ScriptedTransport()
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(path="mux", channel_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        runtime = McpRuntime(sessions, client, settings, DeploymentDefault())
        auth = AuthIdentity(
            tenant_id=tenant.id,
            account_id=account.id,
            role=Role.ADMIN,
            is_admin=True,
            platform="discord",
            platform_user_id="123",
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_owned"),
        )
        service = make_backend_service(sessions, settings.turn)
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(
            tenant.id,
            ("anthropic", "openai", "gemini"),
            policy.revision,
            BackendActor(tenant.id, account.id, Subject(is_admin=True)),
        )

        def queue_gate():
            trace.queue(
                ScriptedReply("GET", "/v1/sessions/sess_owned", httpx.Response(200, json=frozen))
            )

        def queue_agent():
            trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload])))

        yield (
            runtime,
            auth,
            origin,
            service,
            trace,
            payload,
            frozen,
            queue_gate,
            queue_agent,
            mapping,
        )


async def test_chat_preview_then_confirm_applies_real_scoped_cas_once(active_chat):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.expected_revision == 0
    assert "unknown" in preview.cost_line.lower()
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
    )
    assert changed.status == "changed"
    gate()
    agent()
    agent()
    with pytest.raises(ToolError, match="Preview again"):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
            expected_revision=0,
            preview_digest=preview.preview_digest,
        )
    agent()
    actual = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    current = await service.current(actual)
    assert current.local == 1 and current.backend == "openai"
    events = await service.audit(auth.tenant_id, agent_id=actual.agent_id)
    assert len(events) == 1 and events[0].operation == "set"
    assert events[0].account_id == auth.account_id
    assert not trace.replies and not trace.violations
    assert all(r.method == "GET" for r in trace.requests)


@pytest.mark.parametrize("missing", ["native_session", "ungated_session", "feature_off"])
async def test_no_verified_native_card_refuses_before_source_agent_reads(active_chat, missing):
    from daimon.core.config import TurnSettings
    from daimon.core.stores.thread_sessions import mark_dead

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    if missing == "native_session":
        # Native preparation has a StateStore binding, never an MA mapping. An
        # origin with that identity cannot provide a native approved-card proof.
        async with runtime.session_factory.begin() as session:
            await mark_dead(session, id=mapping.id)
    elif missing == "ungated_session":
        frozen["agent"]["tools"][0]["configs"] = []
        gate()
    else:
        runtime = replace(
            runtime, settings=runtime.settings.model_copy(update={"turn": TurnSettings()})
        )
    with pytest.raises(ToolError):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="gemini",
            origin_context_id=str(origin.id),
        )
    assert not trace.replies and not trace.violations
    assert all(r.path != "/v1/agents" for r in trace.requests)


@pytest.mark.parametrize(
    "path,channel_backends,present",
    [
        ("legacy", False, False),
        ("legacy", True, False),
        ("mux", False, False),
        ("mux", True, True),
    ],
)
async def test_registration_uses_only_the_existing_backend_flags(path, channel_backends, present):
    from daimon.adapters.mcp.tools.agents import register_agent_tools
    from daimon.core.config import TurnSettings
    from fastmcp import FastMCP

    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path=path, channel_backends=channel_backends)
    mcp = FastMCP()
    register_agent_tools(mcp, cast(McpRuntime, runtime))
    tools = await mcp.local_provider.list_tools()
    names = [tool.name for tool in tools]
    assert ("request_agent_backend_switch" in names) is present
    assert len(names) == len(set(names))


@pytest.mark.parametrize("recorded_gate", [False, True])
async def test_live_sdk_omitted_overrides_require_the_scoped_recorded_gate(
    active_chat, recorded_gate
):
    import httpx
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.session_snapshot import (
        desired_snapshot,
        fingerprint_identity,
        fingerprint_mutable,
    )
    from daimon.core.stores.thread_sessions import record_snapshot
    from daimon.testing.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    payload["tools"][0]["configs"] = []
    frozen["agent"]["tools"][0]["configs"] = []
    native = BetaManagedAgentsAgent.model_validate(payload)
    snapshot = desired_snapshot(
        native,
        hidden_mcp_server_names=frozenset(),
        environment_id=frozen["environment_id"],
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        public_url=str(runtime.settings.mcp.public_url),
        confirmed_tools=frozenset({"request_agent_backend_switch"})
        if recorded_gate
        else frozenset(),
    )
    async with runtime.session_factory.begin() as session:
        await record_snapshot(
            session,
            id=mapping.id,
            snapshot=snapshot,
            identity_fingerprint=fingerprint_identity(snapshot),
            mutable_fingerprint=fingerprint_mutable(snapshot),
        )
    gate()
    trace.queue(ScriptedReply("GET", "/v1/agents/agent_owned", httpx.Response(200, json=payload)))
    if recorded_gate:
        agent()
        preview = await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
        )
        assert preview.status == "preview"
    else:
        with pytest.raises(ToolError, match="cannot verify"):
            await _request_agent_backend_switch_impl(
                runtime,
                auth,
                agent_name="Researcher",
                target_backend="openai",
                origin_context_id=str(origin.id),
            )
    assert not trace.replies and not trace.violations
    assert all(request.method == "GET" for request in trace.requests)

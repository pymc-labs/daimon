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
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma import MARouter, list_response
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
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
            sender_foreign=False,
            shared_channel=False,
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
    # `daimon-mcp` must be both a tool and a matching `mcp_servers` entry (the
    # real agent carries both together, `AgentSpec._require_mcp_toolset_when_
    # mcp_servers_set`); otherwise DD3's obligation check sees an unresolvable
    # resource and hard-refuses rather than declaring the toolset a drop.
    mcp_servers = [{"name": "daimon-mcp", "type": "url", "url": "https://mcp.invalid"}]
    native = ma_agent(
        id="agent_owned",
        tenant_id=str(tenant.id),
        name="Researcher",
        tools=tools,
        mcp_servers=mcp_servers,
    )
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = "Researcher"
    frozen = ma_session(
        id="sess_owned",
        agent=native,
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_account": str(account.id),
            # DD3's trusted source capture requires positive proof of a public
            # context (never inferred from an absent field): the stamped
            # channel/thread must match the verified origin's exactly.
            "daimon_channel": "channel",
            "daimon_thread": "thread",
        },
    ).model_dump(mode="json")
    # DD3's trusted source capture reads the real target agent and its
    # deployment environment in a verified host context: a deployment default
    # naming both (as production's `defaults/config.yaml` does), and a router
    # fallback serving them, same as the agent/session scripts above.
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "default"
    router = MARouter()
    router.add("GET", r"/v1/agents/agent_owned", lambda *_: httpx.Response(200, json=payload))
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda *_: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/environments", lambda *_: list_response([environment.model_dump(mode="json")])
    )
    # The capture callback repeats its own real session-privacy read on every
    # preview/write (never a cached fact): a plain fallback for the unchanged
    # session covers that extra read without every `gate()` call site having
    # to queue twice for the one request they already scripted explicitly.
    router.add("GET", r"/v1/sessions/sess_owned", lambda *_: httpx.Response(200, json=frozen))
    trace = ScriptedTransport(router=router)
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(path="mux", channel_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        runtime = McpRuntime(
            sessions,
            client,
            settings,
            DeploymentDefault(agent_name="Daimon", environment_name="default"),
        )
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


def _without_own_tools(payload):
    """Remove the agent's own tools while retaining the frozen chat toolset."""
    payload["tools"] = []
    payload["mcp_servers"] = []


@pytest.mark.parametrize("model_controls", [False, True])
@pytest.mark.parametrize("own_tools", [False, True])
@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_chat_preview_then_confirm_applies_real_scoped_cas_once(
    active_chat, target, own_tools, model_controls
):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    if not own_tools:
        _without_own_tools(payload)
    if model_controls:
        payload["model"] = {
            "id": "claude-haiku-5-5",
            "speed": "standard",
            "effort": {"type": "medium"},
        }
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend=target,
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.expected_revision == 0
    assert "unknown" in preview.cost_line.lower()
    drops = preview.declared_drops
    assert {"memory", "sandbox"} <= set(drops)
    assert ("model_controls" in drops) == model_controls
    assert own_tools or drops == (
        ("memory", "model_controls", "sandbox") if model_controls else ("memory", "sandbox")
    )
    extra = next(item for item in ("files", "repository", "skills") if item not in drops)
    # The confirmed call must name exactly the previewed losses.
    for accepted in (None, (), drops[:1], (*drops, extra)):
        gate()
        agent()
        agent()
        with pytest.raises(ToolError, match="(?i)preview again"):
            await _request_agent_backend_switch_impl(
                runtime,
                auth,
                agent_name="Researcher",
                target_backend=target,
                origin_context_id=str(origin.id),
                expected_revision=0,
                preview_digest=preview.preview_digest,
                accepted_drops=accepted,
            )
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend=target,
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed" and changed.declared_drops == preview.declared_drops
    gate()
    agent()
    agent()
    with pytest.raises(ToolError, match="Someone just switched Researcher; it's now on"):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend=target,
            origin_context_id=str(origin.id),
            expected_revision=0,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    agent()
    actual = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    current = await service.current(actual)
    assert current.local == 1 and current.backend == target
    assert current.declared_drops == preview.declared_drops
    events = await service.audit(auth.tenant_id, agent_id=actual.agent_id)
    assert len(events) == 1 and events[0].operation == "set"
    assert events[0].account_id == auth.account_id
    assert not trace.replies and not trace.violations
    assert all(r.method == "GET" for r in trace.requests)


async def test_the_first_preview_tells_the_model_to_post_the_card_in_the_same_turn(active_chat):
    """SMOKE 42591c55 J5: the model read "If they want to proceed" and asked the person
    to reply "yes" instead of calling again, so no card was posted. The preview must say
    the originating request is the go-ahead and that the card, not a chat reply, confirms.
    """
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    _without_own_tools(payload)
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"
    summary = " ".join(preview.summary.lower().split())
    assert "same reply" in summary, preview.summary
    assert "do not ask" in summary and "reply yes" in summary, preview.summary
    assert "confirm on it" in summary, preview.summary
    assert not trace.replies and not trace.violations


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
        stopped = TurnSettings(path="legacy", channel_backends=False, agent_backends=False)
        runtime = replace(runtime, settings=runtime.settings.model_copy(update={"turn": stopped}))
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


@pytest.mark.parametrize("target", ["openai", "gemini"])
@pytest.mark.parametrize("recorded_gate", [None, "current", "with_retired_channel_switch"])
async def test_live_sdk_omitted_overrides_require_the_scoped_recorded_gate(
    active_chat, recorded_gate, target
):
    import httpx
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.session_snapshot import (
        desired_snapshot,
        fingerprint_identity,
        fingerprint_mutable,
    )
    from daimon.core.stores.thread_sessions import record_snapshot
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

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
        confirmed_tools={
            None: frozenset[str](),
            "current": frozenset({"request_agent_backend_switch"}),
            # A session bound while the retired chat channel switch was gated too.
            "with_retired_channel_switch": frozenset(
                {"request_agent_backend_switch", "request_channel_backend_switch"}
            ),
        }[recorded_gate],
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
    # The card gate reads the live agent's toolset (once here, and once more in
    # the preview's own source capture): both reads see the gated toolset, while
    # the source the preview captures afterwards is the tool-less agent.
    gated = [
        ScriptedReply("GET", "/v1/agents/agent_owned", httpx.Response(200, json=payload))
        for _ in range(2)
    ]
    trace.queue(gated[0])
    _without_own_tools(payload)
    if recorded_gate is not None:
        agent()
        trace.queue(gated[1])
        preview = await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend=target,
            origin_context_id=str(origin.id),
        )
        assert preview.status == "preview"
    else:
        with pytest.raises(ToolError, match="cannot verify"):
            await _request_agent_backend_switch_impl(
                runtime,
                auth,
                agent_name="Researcher",
                target_backend=target,
                origin_context_id=str(origin.id),
            )
    assert not trace.replies and not trace.violations
    assert all(request.method == "GET" for request in trace.requests)


async def test_a_private_session_context_refuses_the_preview(active_chat):
    """`_capture_backend_source`'s `require_public_context=True` gate
    (daimon/adapters/mcp/tools/agents.py) must refuse a preview whose live session's
    stamped channel doesn't match the verified chat origin exactly, even though the
    origin/admin/live-session checks above it all pass. Here the session is missing
    its channel stamp entirely (as an un-public or pre-DD3 session would be).
    """
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    # A tool-bearing source refuses on its own terms (asserted elsewhere in this
    # file); strip it so the only possible refusal here is the context gate.
    _without_own_tools(payload)
    del frozen["metadata"]["daimon_channel"]
    del frozen["metadata"]["daimon_thread"]
    gate()
    agent()
    with pytest.raises(ToolError, match="server channel or thread where I answer"):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
        )
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("stamp", ["channel_only", "other_channel", "other_thread"])
async def test_a_thread_opened_by_a_channel_mention_previews_the_switch(active_chat, stamp):
    """A first mention's session is stamped before its thread exists: channel only.

    That session is the live one in this origin's thread, so the switch previews.
    A session stamped with another channel or another thread still refuses.
    """
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    _without_own_tools(payload)
    if stamp == "channel_only":
        del frozen["metadata"]["daimon_thread"]
    elif stamp == "other_channel":
        del frozen["metadata"]["daimon_thread"]
        frozen["metadata"]["daimon_channel"] = "another-channel"
    else:
        frozen["metadata"]["daimon_thread"] = "another-thread"
    gate()
    agent()
    if stamp == "channel_only":
        preview = await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
        )
        assert preview.status == "preview"
    else:
        with pytest.raises(ToolError, match="server channel or thread where I answer"):
            await _request_agent_backend_switch_impl(
                runtime,
                auth,
                agent_name="Researcher",
                target_backend="openai",
                origin_context_id=str(origin.id),
            )
    assert not trace.replies and not trace.violations


async def test_native_no_drop_chat_confirmation_binds_empty_consent(active_chat):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    gate()
    agent()
    quote = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="anthropic",
        origin_context_id=str(origin.id),
    )
    assert quote.status == "preview" and quote.declared_drops == ()
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="anthropic",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=quote.preview_digest,
    )
    assert changed.status == "changed" and changed.declared_drops == ()
    agent()
    from daimon.core.channel_backend import load_backend_agent

    source = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    revision = await service.current(source)
    assert revision.drop_consent is not None and revision.declared_drops == ()
    assert not trace.replies and not trace.violations


async def test_chat_switch_to_a_named_model_previews_and_applies_that_model(active_chat):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    _without_own_tools(payload)
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        model="gemini-4-argon",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.model == "gemini-4-argon"
    assert preview.models == ("gemini-3.8-flash", "gemini-4-argon")
    assert "gemini-4-argon" in preview.cost_line
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        model="gemini-4-argon",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed" and changed.model == "gemini-4-argon"
    agent()
    actual = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    current = await service.current(actual)
    assert (current.backend, current.model) == ("gemini", "gemini-4-argon")
    assert not trace.violations


@pytest.mark.parametrize(("backend", "model"), [("gemini", "gemini-3.8-pro"), ("openai", "x")])
async def test_an_unknown_model_names_the_choices_before_any_lookup(backend, model) -> None:
    from daimon.adapters.mcp.tools.agents import _require_selectable_model

    with pytest.raises(ToolError, match="isn't available") as refused:
        _require_selectable_model(backend, model)
    assert "Pick one of:" in str(refused.value)
    _require_selectable_model("gemini", None)
    _require_selectable_model("gemini", "gemini-4-argon")


async def test_the_agents_owner_may_request_a_switch_and_a_member_may_not(active_chat):
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_owners

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    member = replace(auth, role=Role.USER, is_admin=False)
    gate()
    agent()
    with pytest.raises(ToolError, match="needs its owner or a server admin"):
        await _request_agent_backend_switch_impl(
            runtime,
            member,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
        )
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned"),
            owner_account_id=auth.account_id,
            source="admin_assigned",
        )
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        member,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.expected_revision == 0


async def test_chat_states_the_agents_thread_mode_and_requirements(active_chat):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    options = {
        "agent_name": "Researcher",
        "target_backend": "anthropic",
        "origin_context_id": str(origin.id),
        "thread_mode": "shared",
        "required_capabilities": ("tool_loop",),
    }
    gate()
    agent()
    quote = await _request_agent_backend_switch_impl(runtime, auth, **options)
    assert quote.agent_settings == (
        "shares threads where the channel allows it; requires tool_loop"
    )
    # The confirmed call must state the same settings as the preview.
    gate()
    agent()
    agent()
    with pytest.raises(ToolError, match="(?i)preview again"):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            **{**options, "thread_mode": None},
            expected_revision=0,
            preview_digest=quote.preview_digest,
        )
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime, auth, **options, expected_revision=0, preview_digest=quote.preview_digest
    )
    assert changed.status == "changed"
    agent()
    source = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    revision = await service.current(source)
    assert revision.backend == "anthropic" and revision.thread_mode == "shared"
    assert set(revision.requires or {}) == {"tool_loop"}


async def test_an_owner_may_not_ask_to_share_threads(active_chat):
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_owners

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned"),
            owner_account_id=auth.account_id,
            source="admin_assigned",
        )
    owner = replace(auth, role=Role.USER, is_admin=False)
    gate()
    agent()
    with pytest.raises(ToolError, match="Sharing threads applies in every channel"):
        await _request_agent_backend_switch_impl(
            runtime,
            owner,
            agent_name="Researcher",
            target_backend="anthropic",
            origin_context_id=str(origin.id),
            thread_mode="shared",
        )


async def test_the_switch_says_when_this_channel_pins_its_own_provider(active_chat):
    from daimon.core.channel_backend import (
        channel_ref,
        clear_channel_backend,
        set_channel_backend,
    )
    from mux.contracts.config import BackendConfig

    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    _without_own_tools(payload)
    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    async with runtime.session_factory.begin() as session:
        await set_channel_backend(
            session, channel, BackendConfig(backend="openai", model="gpt-6-luna")
        )
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    assert preview.channel_pin == (
        "This channel pins OpenAI (gpt-6-luna), so this agent keeps answering on that here"
        ' until an admin clears it: say "clear this channel\'s pin".'
    )
    assert preview.summary.endswith(preview.channel_pin)
    async with runtime.session_factory.begin() as session:
        await clear_channel_backend(session, channel)
    gate()
    agent()
    cleared = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    assert cleared.channel_pin is None and "pins" not in cleared.summary
    assert "channel_pin" not in cleared.model_dump()
    assert not trace.violations


@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_a_copy_switches_from_a_thread_another_agent_answers(active_chat, target):
    """J1/J2: the provider is the copy's own setting, so a thread whose responder is
    another agent (here Researcher) previews and applies it for the copy."""
    import httpx
    from daimon.core.channel_backend import load_backend_agent
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, gate, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend=target,
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.agent_name == "Researcher-copy"
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend=target,
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed"
    agent()
    source = await load_backend_agent(
        runtime.client,
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        agent_name="Researcher-copy",
    )
    assert (await service.current(source)).backend == target
    agent()
    researcher = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    assert await service.current(researcher) is None
    assert not trace.violations


def _admin_auth():
    return AuthIdentity(
        tenant_id=uuid4(),
        account_id=uuid4(),
        role=Role.ADMIN,
        is_admin=True,
        platform="discord",
        platform_user_id="123",
    )


@pytest.mark.parametrize(
    ("refusal", "says"),
    [
        ({"reason": "agent_not_found"}, "no agent named 'X'"),
        ({"reason": "backend_not_allowed"}, "OpenAI isn't allowed in this server"),
        ({"reason": "source_context_unavailable"}, "server channel or thread where I answer"),
        (
            {"reason": "source_context_unavailable", "detail": "no_live_session"},
            "couldn't find my conversation in this thread",
        ),
        (
            {"reason": "source_context_unavailable", "detail": "other_agents_session"},
            "Another agent's conversation",
        ),
        (
            {"reason": "source_context_unavailable", "detail": "session_not_gated"},
            "started without the Confirm card",
        ),
        (
            {"reason": "source_context_unavailable", "detail": "session_unreadable"},
            "Try again in a minute",
        ),
        ({"reason": "stale_preview"}, "preview again"),
        (
            {"reason": "stale_revision"},
            "Someone just switched X. Ask again if you still want OpenAI",
        ),
        ({"reason": "owner_or_admin_required"}, "needs its owner"),
        (
            {"reason": "source_unsupported", "missing": ("credentials",)},
            "a saved key that one of its kept connections needs",
        ),
        (
            {"reason": "source_unsupported", "missing": ("source_definition",)},
            "can't represent",
        ),
        ({"reason": "source_unavailable", "detail": "environment"}, "missing or archived"),
        ({"reason": "source_unavailable", "detail": "identity"}, "account that can change it"),
        ({"reason": "source_unavailable"}, "Try again in a minute"),
        ({"reason": "some_new_reason"}, "tell whoever runs Daimon"),
    ],
)
def test_each_switch_refusal_names_its_cause(refusal, says):
    from daimon.adapters.mcp.tools.agents import _backend_switch_refusal
    from daimon.core.channel_backend import AgentBackendRefused

    reason = refusal.pop("reason")
    text = str(
        _backend_switch_refusal(
            AgentBackendRefused(reason, **refusal), "X", "openai", _admin_auth()
        )
    )
    assert says in text and text.endswith("Nothing changed.")
    assert "unavailable or changed" not in text and "_" not in text
    # A refusal from the preview never sends the requester back to the same preview.
    if reason in ("source_unsupported", "source_unavailable"):
        assert "preview again" not in text.lower()


@pytest.mark.parametrize(
    "said",
    [
        "selfhosted_admission_disabled",
        "openai.persistent_workspace is not runnable in this release",
    ],
)
def test_internal_backend_strings_never_reach_the_requester(said):
    from daimon.adapters.mcp.tools.agents import _backend_switch_refusal
    from daimon.core.channel_backend import BackendUnsupported

    text = str(_backend_switch_refusal(BackendUnsupported(said), "X", "openai", _admin_auth()))
    assert said not in text and "OpenAI can't run here right now" in text


def test_an_unknown_error_never_shows_its_type():
    from daimon.adapters.mcp.tools.agents import _backend_switch_refusal
    from daimon.core.errors import DaimonError

    class SecretInternalError(DaimonError):
        pass

    text = str(_backend_switch_refusal(SecretInternalError("x"), "X", "openai", _admin_auth()))
    assert "SecretInternalError" not in text and "whoever runs Daimon" in text


def test_a_provider_word_for_another_provider_is_refused():
    from daimon.adapters.mcp.tools.agents import _resolve_agent_model

    with pytest.raises(ToolError, match="is Gemini, but this switch names OpenAI"):
        _resolve_agent_model("openai", "gemini")


@pytest.mark.parametrize(
    ("backend", "word"),
    [
        ("openai", "GPT"),
        ("openai", "OpenAI"),
        ("openai", "ChatGPT"),
        ("gemini", "Gemini"),
        ("gemini", "Google"),
    ],
)
def test_a_provider_word_alone_means_the_provider_default(backend, word):
    from daimon.adapters.mcp.tools.agents import _resolve_agent_model

    assert _resolve_agent_model(backend, word) is None


@pytest.mark.parametrize(("name", "provider"), [("gemini 4 argon", "gemini"), ("astra", "openai")])
def test_creating_on_another_providers_model_says_how(name, provider):
    """An existing agent moves with the switch; a new one is made there directly."""
    from daimon.adapters.mcp.tools.agents import _resolve_native_agent_model

    with pytest.raises(ToolError, match="request_agent_backend_switch") as refused:
        _resolve_native_agent_model(name)
    assert f"target_backend={provider}" in str(refused.value)
    assert "Create the agent on a Claude model" not in str(refused.value)


def test_an_unknown_claude_id_is_not_waved_through():
    from daimon.adapters.mcp.tools.agents import _resolve_native_agent_model

    with pytest.raises(ToolError):
        _resolve_native_agent_model("claude-made-up-9")


async def test_a_member_who_doesnt_own_the_copy_is_refused_before_the_fallback(active_chat):
    import httpx
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_owners
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, gate, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))
    member = replace(auth, role=Role.USER, is_admin=False)

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    gate()
    agent()
    reads = len(trace.requests)
    with pytest.raises(ToolError, match="needs its owner"):
        await _request_agent_backend_switch_impl(
            runtime,
            member,
            agent_name="Researcher-copy",
            target_backend="gemini",
            origin_context_id=str(origin.id),
        )
    # Refused at the editor check: the copy's own source was never captured.
    assert not any(r.path == "/v1/agents/agent_copy" for r in trace.requests[reads:])
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_copy"),
            owner_account_id=auth.account_id,
            source="admin_assigned",
        )
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        member,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview" and preview.agent_name == "Researcher-copy"


def test_clearing_to_the_default_with_another_providers_word_is_refused():
    from daimon.adapters.mcp.tools.agents import _resolve_agent_model

    with pytest.raises(ToolError, match="is OpenAI, but this switch names Anthropic"):
        _resolve_agent_model(None, "gpt")
    assert _resolve_agent_model(None, "claude") is None


def test_an_unsupported_switch_names_the_provider_it_stays_on():
    from daimon.adapters.mcp.tools.agents import _backend_switch_refusal
    from daimon.core.channel_backend import AgentBackendRefused

    refused = AgentBackendRefused("source_unsupported", missing=("instructions",))
    text = str(_backend_switch_refusal(refused, "X", "openai", _admin_auth(), current="gemini"))
    assert "or keep it on Gemini." in text


async def _release_rows(runtime, auth):
    from daimon.core.thread_release import pending_releases

    async with runtime.session_factory() as session:
        return await pending_releases(session, tenant_id=auth.tenant_id)


async def test_switching_the_agent_answering_here_moves_this_conversation(active_chat):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    assert await _release_rows(runtime, auth) == []  # a preview moves nothing
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert "This conversation moves to Gemini from the next message" in changed.summary
    assert "aren't carried over" in changed.summary
    ((channel, thread, account, _),) = await _release_rows(runtime, auth)
    assert (channel, thread, account) == ("channel", "thread", str(auth.account_id))


async def test_switching_a_copy_leaves_this_conversation_and_says_so(active_chat):
    import httpx
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, gate, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert "isn't the one answering here" in changed.summary
    assert await _release_rows(runtime, auth) == []


@pytest.mark.parametrize("kind", ["handoff", "setup"])
async def test_a_confirmed_switch_of_the_copy_this_thread_was_handed_to_moves_it(active_chat, kind):
    """J2: hand this conversation to the copy (same provider), then switch the copy
    with a confirmation: the confirmed switch moves the conversation. A setup
    conversation, whose agent is only its configuration target, never moves."""
    import httpx
    from daimon.core.stores.thread_agent_bindings import create_binding
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, gate, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))
    async with runtime.session_factory.begin() as session:
        await create_binding(
            session,
            tenant_id=auth.tenant_id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            responder_ma_agent_id="agent_copy",
            responder_name="Researcher-copy",
            kind=kind,
        )

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    rows = await _release_rows(runtime, auth)
    if kind == "handoff":
        assert "This conversation moves to Gemini from the next message" in changed.summary
        assert len(rows) == 1
    else:
        assert "isn't the one answering here" in changed.summary
        assert rows == []


async def test_a_switch_preview_to_gpt_never_creates_the_tenants_environment(
    active_chat, monkeypatch
):
    from daimon.core.turn import openai_environment

    async def created(*_args, **_kwargs):
        raise AssertionError("a preview created the tenant's OpenAI environment")

    monkeypatch.setattr(openai_environment, "tenant_environment", created)
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "offline-key")
    runtime, auth, origin, service, trace, payload, frozen, gate, agent, mapping = active_chat
    _without_own_tools(payload)
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"


def test_two_switches_at_once_name_where_the_agent_is_now():
    from daimon.adapters.mcp.tools.agents import _backend_switch_refusal
    from daimon.core.channel_backend import AgentBackendRefused

    text = str(
        _backend_switch_refusal(
            AgentBackendRefused("stale_revision"),
            "Daimon",
            "openai",
            _admin_auth(),
            current="gemini",
        )
    )
    assert text == (
        "Someone just switched Daimon; it's now on Gemini. "
        "Ask again if you still want OpenAI. Nothing changed."
    )


@pytest.mark.parametrize("default", ["deployment", "server", "managed"])
async def test_switching_the_default_agent_offers_a_copy_unless_its_for_everyone(
    active_chat, default
):
    from daimon.core.scope import DeploymentDefault, TenantScopeRef
    from daimon.core.stores.scoped_config_write import set_fields

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    if default == "managed":
        from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED

        # The built-in agent is known by its managed mark, whatever its name.
        payload["metadata"][MA_METADATA_KEY_MANAGED] = "true"
    elif default == "deployment":
        runtime = replace(
            runtime,
            deployment_default=DeploymentDefault(
                agent_name="Researcher", environment_name="default"
            ),
        )
    elif default == "server":
        async with runtime.session_factory.begin() as session:
            await set_fields(
                session,
                scope=TenantScopeRef(tenant_id=auth.tenant_id),
                tenant_id=auth.tenant_id,
                agent_name="Researcher",
            )
    member = replace(auth, role=Role.USER, is_admin=False)
    switch = {
        "agent_name": "Researcher",
        "target_backend": "gemini",
        "origin_context_id": str(origin.id),
    }
    # Without "for everyone", a member and an admin alike are offered a copy instead.
    for who in (member, auth):
        gate()
        agent()
        with pytest.raises(ToolError) as refused:
            await _request_agent_backend_switch_impl(runtime, who, **switch)
        assert str(refused.value).startswith(
            "Switching Researcher changes it for everyone on this server. "
            "To try Gemini just here, I can make a copy of me on Gemini. Nothing changed."
        )
    # "For everyone" still needs the owner or an admin.
    gate()
    agent()
    with pytest.raises(ToolError, match="needs its owner or a server admin"):
        await _request_agent_backend_switch_impl(runtime, member, **switch, for_everyone=True)
    assert await _release_rows(runtime, auth) == []
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(runtime, auth, **switch, for_everyone=True)
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        **switch,
        for_everyone=True,
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed"
    assert "New conversations with Researcher use Gemini" in changed.summary


async def test_a_non_default_agent_switches_without_saying_for_everyone(active_chat):
    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    assert runtime.deployment_default.agent_name != "Researcher"
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
    )
    assert preview.status == "preview"


async def test_a_provider_turn_previews_then_daimon_posts_the_card_and_nothing_changes(
    active_chat, monkeypatch
):
    """OpenAI/Gemini turns have no card of their own: the confirm call posts Daimon's."""
    from unittest.mock import AsyncMock

    import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod
    from daimon.core.stores.backend_switch_cards import get_card
    from daimon.core.switch_cards import parse_custom_id

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    provider_turn = replace(auth, no_confirmation_surface=True)
    post = AsyncMock(return_value="message-1")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", post)
    switch = {
        "agent_name": "Researcher",
        "target_backend": "gemini",
        "origin_context_id": str(origin.id),
    }
    # No session card gate is read: a provider turn has none.
    agent()
    preview = await _request_agent_backend_switch_impl(runtime, provider_turn, **switch)
    assert preview.status == "preview"
    agent()
    agent()
    posted = await _request_agent_backend_switch_impl(
        runtime,
        provider_turn,
        **switch,
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert posted.status == "awaiting_confirmation"
    assert "Nothing changes until the requester presses Confirm" in posted.summary
    agent()
    actual = await load_backend_agent_for(runtime, auth)
    assert await service.current(actual) is None, "posting the card changes nothing"
    assert await _release_rows(runtime, auth) == []
    ((_, kwargs),) = [(call.args, call.kwargs) for call in post.await_args_list]
    assert kwargs["channel_id"] == "thread", "only into the verified origin thread"
    card = kwargs["card"]
    assert card.requester_platform_user_id == "123"
    assert card.headline.startswith("🔁 Switch Researcher to Gemini")
    card_id, action = parse_custom_id(card.buttons[0].custom_id)
    assert action == "confirm"
    async with runtime.session_factory() as session:
        row = await get_card(session, tenant_id=auth.tenant_id, card_id=card_id)
    assert row is not None and row.status == "pending" and row.message_id == "message-1"
    assert (row.channel_id, row.thread_id) == ("channel", "thread")
    assert row.reference["preview_digest"] == preview.preview_digest
    # One waiting card per requester and agent: asking again while it waits posts nothing.
    agent()
    with pytest.raises(ToolError, match="already a Confirm card waiting"):
        agent()
        await _request_agent_backend_switch_impl(
            runtime,
            provider_turn,
            **switch,
            expected_revision=0,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    assert post.await_count == 1


async def test_a_provider_turn_card_is_refused_when_the_preview_moved(active_chat, monkeypatch):
    from unittest.mock import AsyncMock

    import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    provider_turn = replace(auth, no_confirmation_surface=True)
    post = AsyncMock(return_value="message-1")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", post)
    agent()
    with pytest.raises(ToolError, match="Nothing changed"):
        agent()
        await _request_agent_backend_switch_impl(
            runtime,
            provider_turn,
            agent_name="Researcher",
            target_backend="gemini",
            origin_context_id=str(origin.id),
            expected_revision=0,
            preview_digest="0" * 64,
        )
    post.assert_not_awaited()


async def test_a_claude_turn_never_self_posts_a_card(active_chat, monkeypatch):
    """The Claude path keeps its provider-shown card: the confirmed call applies."""
    from unittest.mock import AsyncMock

    import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    post = AsyncMock(return_value="message-1")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", post)
    switch = {
        "agent_name": "Researcher",
        "target_backend": "gemini",
        "origin_context_id": str(origin.id),
    }
    gate()
    agent()
    preview = await _request_agent_backend_switch_impl(runtime, auth, **switch)
    gate()
    agent()
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        **switch,
        expected_revision=0,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed"
    post.assert_not_awaited()


async def load_backend_agent_for(runtime, auth):
    from daimon.core.channel_backend import load_backend_agent

    return await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )


async def test_a_provider_turn_switch_that_carries_a_hand_off_posts_no_card(
    active_chat, monkeypatch
):
    """The hand-off a switch carries runs at the write, which a self-posted press doesn't do."""
    from unittest.mock import AsyncMock

    import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod

    runtime, auth, origin, service, trace, payload, _, gate, agent, _ = active_chat
    _without_own_tools(payload)
    provider_turn = replace(auth, no_confirmation_surface=True)
    post = AsyncMock(return_value="message-1")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", post)
    switch = {
        "agent_name": "Researcher",
        "target_backend": "gemini",
        "origin_context_id": str(origin.id),
        "unsaved_work": "leave",
    }
    agent()
    preview = await _request_agent_backend_switch_impl(runtime, provider_turn, **switch)
    agent()
    with pytest.raises(ToolError, match="hands the conversation over"):
        await _request_agent_backend_switch_impl(
            runtime,
            provider_turn,
            **switch,
            expected_revision=0,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    post.assert_not_awaited()

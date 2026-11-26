"""Chat requests require an authenticated admin's active origin before lookups."""

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _request_agent_backend_switch_impl
from daimon.core.stores.domain import Role
from daimon.testing.db import db_nullpool_engine  # noqa: F401
from fastmcp.exceptions import ToolError

from ..harness import auth_identity


async def test_a_missing_active_origin_never_resolves_or_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = auth_identity(
        uuid4(), uuid4(), platform="discord", platform_user_id="123", chat_session_id="test-session"
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
    from daimon.core.channel_backend import NO_CAPTURE, BackendActor, make_backend_service
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
            chat_session_id="test-session",
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
    # switch (never a cached fact): a plain fallback for the unchanged
    # session covers that extra read without every `gate()` call site having
    # to queue twice for the one request they already scripted explicitly.
    router.add("GET", r"/v1/sessions/sess_owned", lambda *_: httpx.Response(200, json=frozen))
    trace = ScriptedTransport(router=router)
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(channel_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        runtime = McpRuntime(
            sessions,
            client,
            settings,
            DeploymentDefault(agent_name="Daimon", environment_name="default"),
        )
        auth = auth_identity(
            account.id,
            tenant.id,
            platform="discord",
            platform_user_id="123",
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_owned"),
            chat_session_id="test-session",
        )
        service = make_backend_service(sessions, settings.turn, capture_source=NO_CAPTURE)
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


async def _origin_like(runtime, origin, **changes):
    """A second store-made origin of the same caller, differing only in `changes`."""
    from datetime import UTC, datetime

    from daimon.core.stores.turn_origins import create_origin

    fields = origin.model_dump(
        include={
            "tenant_id",
            "account_id",
            "platform",
            "parent_channel_id",
            "thread_id",
            "responder_ma_agent_id",
            "responder_name",
            "configuration_target_ma_agent_id",
            "configuration_target_name",
            "role",
            "expires_at",
            "sender_foreign",
            "shared_channel",
            "attended",
            "chat_session_id",
        }
    )
    async with runtime.session_factory.begin() as session:
        return await create_origin(session, now=datetime.now(UTC), **{**fields, **changes})


def _without_own_tools(payload):
    """Remove the agent's own tools while retaining the frozen chat toolset."""
    payload["tools"] = []
    payload["mcp_servers"] = []


@pytest.mark.parametrize("model_controls", [False, True])
@pytest.mark.parametrize("own_tools", [False, True])
@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_a_chat_switch_applies_on_the_first_call_like_a_model_change(
    active_chat, target, own_tools, model_controls
):
    """One call switches the agent; a call on a revision read before it is refused."""
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    if not own_tools:
        _without_own_tools(payload)
    if model_controls:
        payload["model"] = {
            "id": "claude-haiku-5-5",
            "speed": "fast",
            "effort": {"type": "medium"},
        }
    switch = {
        "agent_name": "Researcher",
        "target_backend": target,
        "origin_context_id": str(origin.id),
    }
    agent()
    changed = await _request_agent_backend_switch_impl(runtime, auth, **switch, expected_revision=0)
    assert changed.status == "changed" and changed.expected_revision == 1, "one call applies"
    assert "unknown" in changed.cost_line.lower(), changed.cost_line
    drops = changed.declared_drops
    assert {"memory", "sandbox"} <= set(drops), drops
    settings = {"fast mode", "effort setting"} & set(drops)
    carried = {"fast mode"} if target == "openai" else {"fast mode", "effort setting"}
    assert settings == (carried if model_controls else set()), drops
    assert not {"model_controls", "fast_mode", "effort_setting"} & set(drops), drops
    assert own_tools or set(drops) == {"memory", "sandbox"} | settings, drops
    agent()
    with pytest.raises(ToolError, match="Someone just switched Researcher; it's now on"):
        await _request_agent_backend_switch_impl(runtime, auth, **switch, expected_revision=0)
    agent()
    actual = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    current = await service.current(actual)
    assert current.local == 1 and current.backend == target, "only the first call wrote"
    events = await service.audit(auth.tenant_id, agent_id=actual.agent_id)
    assert len(events) == 1 and events[0].operation == "set", "one audited write"
    assert events[0].account_id == auth.account_id, "audited as the requester"
    assert not trace.replies and not trace.violations, "every scripted reply was used"
    assert all(r.method == "GET" for r in trace.requests), "the switch only reads the agent"


async def test_the_switch_says_what_is_off_and_asks_nothing(active_chat):
    """What the new provider can't carry is said as information, never as a question."""
    runtime, auth, origin, _, trace, _, _, _, agent, _ = active_chat
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.declared_drops, "the fixture's agent loses something on OpenAI"
    assert "Off on OpenAI:" in changed.summary, changed.summary
    assert "come back when you switch back" in changed.summary, changed.summary
    said = changed.summary.lower()
    assert "?" not in said, changed.summary
    assert not any(word in said for word in ("confirm", "card", "approve", "preview")), said
    assert not trace.replies and not trace.violations, "every scripted reply was used"


@pytest.mark.parametrize("missing", ["native_session", "feature_off"])
async def test_a_switch_without_this_threads_session_refuses_before_capturing_the_source(
    active_chat, missing
):
    """No live session for this thread (or the feature off) refuses; the source isn't read."""
    from daimon.core.config import TurnSettings
    from daimon.core.stores.thread_sessions import mark_dead

    runtime, auth, origin, _, trace, _, _, _, agent, mapping = active_chat
    if missing == "native_session":
        # Native preparation has a StateStore binding, never an MA mapping.
        async with runtime.session_factory.begin() as session:
            await mark_dead(session, id=mapping.id)
        agent()
    else:
        stopped = TurnSettings(channel_backends=False, agent_backends=False)
        runtime = replace(runtime, settings=runtime.settings.model_copy(update={"turn": stopped}))
    with pytest.raises(ToolError):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="gemini",
            origin_context_id=str(origin.id),
            expected_revision=0,
        )
    assert not trace.replies and not trace.violations, "every scripted reply was used"
    assert all(r.path != "/v1/agents/agent_owned" for r in trace.requests), (
        "the source is never captured"
    )


@pytest.mark.parametrize("channel_backends", [False, True])
async def test_registration_uses_only_the_existing_backend_flags(channel_backends):
    from daimon.adapters.mcp.tools.agents import register_agent_tools
    from daimon.core.config import TurnSettings
    from fastmcp import FastMCP

    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(channel_backends=channel_backends)
    mcp = FastMCP()
    register_agent_tools(mcp, cast(McpRuntime, runtime))
    tools = await mcp.local_provider.list_tools()
    names = [tool.name for tool in tools]
    assert ("request_agent_backend_switch" in names) is channel_backends
    assert len(names) == len(set(names))


@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_a_session_that_never_gated_the_switch_cannot_confirm_it(active_chat, target):
    """A move to another provider needs the card; a session that holds no ask can't show one."""
    runtime, auth, origin, service, trace, payload, frozen, _, agent, _ = active_chat
    frozen["agent"]["tools"][0]["configs"] = []
    _without_own_tools(payload)
    agent()
    with pytest.raises(ToolError, match="cannot verify the confirmation card") as refused:
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend=target,
            origin_context_id=str(origin.id),
            expected_revision=0,
        )
    assert "Nothing changed" in str(refused.value)
    assert "Start a new conversation" in str(refused.value), "the person is told what fixes it"
    agent()
    assert await service.current(await load_backend_agent_for(runtime, auth)) is None, (
        "nothing was written"
    )
    assert not trace.violations


async def test_a_move_to_another_provider_previews_before_it_asks(active_chat):
    """Without expected_revision the call changes nothing and hands out the revision that asks."""
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    agent()
    shown = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
    )
    assert shown.status == "preview" and shown.expected_revision == 0, shown
    assert "sends its conversations" in shown.summary, shown.summary
    agent()
    assert await service.current(await load_backend_agent_for(runtime, auth)) is None, (
        "the preview wrote nothing"
    )


async def test_a_private_session_context_refuses_the_switch(active_chat):
    """`_capture_backend_source`'s `require_public_context=True` gate
    (daimon/adapters/mcp/tools/agents.py) must refuse a switch whose live session's
    stamped channel doesn't match the verified chat origin exactly, even though the
    origin/admin/live-session checks above it all pass. Here the session is missing
    its channel stamp entirely (as an un-public or pre-DD3 session would be).
    """
    runtime, auth, origin, service, trace, payload, frozen, _, agent, mapping = active_chat
    # A tool-bearing source refuses on its own terms (asserted elsewhere in this
    # file); strip it so the only possible refusal here is the context gate.
    _without_own_tools(payload)
    del frozen["metadata"]["daimon_channel"]
    del frozen["metadata"]["daimon_thread"]
    agent()
    with pytest.raises(ToolError, match="Ask this agent in a server channel or thread"):
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
            expected_revision=0,
        )
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("stamp", ["channel_only", "other_channel", "other_thread"])
async def test_a_thread_opened_by_a_channel_mention_switches(active_chat, stamp):
    """A first mention's session is stamped before its thread exists: channel only.

    That session is the live one in this origin's thread, so the switch applies.
    A session stamped with another channel or another thread still refuses.
    """
    runtime, auth, origin, _, trace, payload, frozen, _, agent, _ = active_chat
    _without_own_tools(payload)
    if stamp == "channel_only":
        del frozen["metadata"]["daimon_thread"]
    elif stamp == "other_channel":
        del frozen["metadata"]["daimon_thread"]
        frozen["metadata"]["daimon_channel"] = "another-channel"
    else:
        frozen["metadata"]["daimon_thread"] = "another-thread"
    switch = {
        "agent_name": "Researcher",
        "target_backend": "openai",
        "origin_context_id": str(origin.id),
    }
    agent()
    if stamp == "channel_only":
        changed = await _request_agent_backend_switch_impl(
            runtime, auth, **switch, expected_revision=0
        )
        assert changed.status == "changed", "the thread's own session switches"
    else:
        with pytest.raises(ToolError, match="Ask this agent in a server channel or thread"):
            await _request_agent_backend_switch_impl(runtime, auth, **switch, expected_revision=0)
    assert not trace.replies and not trace.violations, "every scripted reply was used"


async def test_a_native_no_drop_switch_writes_on_the_first_call(active_chat):
    """A switch that loses nothing applies on the first call, like a model change."""
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="anthropic",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed", "one call applies"
    assert changed.declared_drops == (), "nothing is declared"
    agent()
    source = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    revision = await service.current(source)
    assert revision is not None and revision.backend == "anthropic", "written on the first call"
    assert not trace.replies and not trace.violations, "every scripted reply was used"


@pytest.mark.parametrize("attended", [True, False])
async def test_a_provider_turn_switch_applies_only_when_someone_started_the_run(
    active_chat, attended
):
    """A routine or wake has nobody present: its switch is refused, like a model change."""
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    origin = await _origin_like(runtime, origin, attended=attended)
    agent()

    async def switch():
        return await _request_agent_backend_switch_impl(
            runtime,
            replace(auth, no_confirmation_surface=True),
            agent_name="Researcher",
            target_backend="anthropic",
            origin_context_id=str(origin.id),
            expected_revision=0,
        )

    if not attended:
        with pytest.raises(ToolError, match="nobody started this run"):
            await switch()
        agent()
        assert await service.current(await load_backend_agent_for(runtime, auth)) is None, (
            "an unattended run writes nothing"
        )
        return
    shown = await switch()
    assert shown.status == "changed", "the first call applies"
    agent()
    written = await service.current(await load_backend_agent_for(runtime, auth))
    assert written is not None, "the switch is written"
    assert not trace.replies and not trace.violations, "every scripted reply was used"


async def test_a_switch_to_where_the_agent_already_is_changes_nothing(active_chat):
    """Asking again for the provider and model in force writes no revision."""
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    agent()
    ask = dict(
        agent_name="Researcher", target_backend="anthropic", origin_context_id=str(origin.id)
    )
    auth = replace(auth, no_confirmation_surface=True)
    first = await _request_agent_backend_switch_impl(runtime, auth, **ask)
    agent()
    again = await _request_agent_backend_switch_impl(runtime, auth, **ask)
    agent()
    current = await service.current(await load_backend_agent_for(runtime, auth))
    assert current is not None and current.local == first.expected_revision, (
        "the repeat wrote no new revision"
    )
    assert again.expected_revision == first.expected_revision, "it names the revision in force"
    assert "Nothing changed" in again.summary, "the summary says so"


async def test_a_no_drop_switch_on_a_provider_turn_when_in_a_direct_message_then_is_refused(
    active_chat,
):
    """Refused though it needs no card: the switch reads the agent's setup as a public
    place runs it, and a direct message is not one (a Claude turn's read refuses it too)."""
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    origin = await _origin_like(runtime, origin, parent_channel_id="dm:123", thread_id="dm:123")
    agent()
    with pytest.raises(ToolError, match="from a direct message") as refused:
        await _request_agent_backend_switch_impl(
            runtime,
            replace(auth, no_confirmation_surface=True),
            agent_name="Researcher",
            target_backend="anthropic",
            origin_context_id=str(origin.id),
            expected_revision=0,
        )
    assert "Nothing changed." in str(refused.value), "the refusal says nothing changed"
    assert "I " not in str(refused.value), (
        "worded as a Claude turn's refusal, not in the first person"
    )
    agent()
    written = await service.current(await load_backend_agent_for(runtime, auth))
    assert written is None, "a refused switch writes nothing"
    assert not trace.replies and not trace.violations, "every scripted reply was used"


async def test_chat_switch_to_a_named_model_applies_that_model(active_chat):
    """A named model is the one written, and the reply lists the provider's choices."""
    from daimon.core.channel_backend import load_backend_agent
    from mux.profiles import selectable_models

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        model="gemini-4-argon",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed" and changed.model == "gemini-4-argon", "that model"
    assert changed.models == selectable_models("gemini"), "the list is the selectable list"
    assert "gemini-4-argon" in changed.cost_line, changed.cost_line
    # The same agent on another model, never a version or copy of it.
    assert changed.summary.startswith("Researcher now uses Gemini 4 Argon. "), changed.summary
    assert not {"version", "copy", "variant"} & set(changed.summary.lower().split())
    agent()
    actual = await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )
    current = await service.current(actual)
    assert (current.backend, current.model) == ("gemini", "gemini-4-argon"), "written"
    assert not trace.violations, "no unscripted reads"


async def test_a_switch_states_an_effort_and_a_later_settings_change_keeps_it(active_chat):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, _trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    base = {
        "agent_name": "Researcher",
        "target_backend": "openai",
        "origin_context_id": str(origin.id),
    }

    async def confirm(revision=None, **stated):
        agent()
        done = await _request_agent_backend_switch_impl(
            runtime, auth, **base, **stated, expected_revision=revision
        )
        assert done.status == "changed"
        agent()
        source = await load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name="Researcher",
        )
        return await service.current(source)

    first = await confirm(0, effort="high")
    assert first.effort == "high", "the call states the effort"
    second = await confirm(thread_mode="per_caller")
    assert second.effort == "high", "stating other settings keeps the effort the agent has"
    agent()
    with pytest.raises(ToolError, match="isn't an effort") as refused:
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            **{**base, "target_backend": "gemini"},
            effort="high",
            expected_revision=0,
        )
    assert "Nothing changed" in str(refused.value), "a Gemini agent takes no effort"


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

    runtime, auth, origin, service, trace, payload, frozen, _, agent, mapping = active_chat
    member = replace(auth, role=Role.USER, is_admin=False, is_platform_admin=False)
    agent()
    with pytest.raises(ToolError, match="needs its owner or a server admin"):
        await _request_agent_backend_switch_impl(
            runtime,
            member,
            agent_name="Researcher",
            target_backend="openai",
            origin_context_id=str(origin.id),
            expected_revision=0,
        )
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned"),
            owner_account_id=auth.account_id,
            source="admin_assigned",
        )
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        member,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed" and changed.expected_revision == 1, "the owner switches"


async def test_chat_states_the_agents_thread_mode_and_requirements(active_chat):
    from daimon.core.channel_backend import load_backend_agent

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    options = {
        "agent_name": "Researcher",
        "target_backend": "anthropic",
        "origin_context_id": str(origin.id),
        "thread_mode": "shared",
        "required_capabilities": ("tool_loop",),
    }
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime, auth, **options, expected_revision=0
    )
    assert changed.status == "changed", "the stated settings apply with the switch"
    assert changed.agent_settings == (
        "shares threads where the channel allows it; requires tool_loop"
    ), changed.agent_settings
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

    runtime, auth, origin, service, trace, payload, frozen, _, agent, mapping = active_chat
    async with runtime.session_factory.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id="agent_owned"),
            owner_account_id=auth.account_id,
            source="admin_assigned",
        )
    owner = replace(auth, role=Role.USER, is_admin=False, is_platform_admin=False)
    agent()
    with pytest.raises(ToolError, match="Sharing threads applies in every channel"):
        await _request_agent_backend_switch_impl(
            runtime,
            owner,
            agent_name="Researcher",
            target_backend="anthropic",
            origin_context_id=str(origin.id),
            thread_mode="shared",
            expected_revision=0,
        )


async def test_the_switch_says_when_this_channel_pins_its_own_provider(active_chat):
    from daimon.core.channel_backend import (
        channel_ref,
        clear_channel_backend,
        set_channel_backend,
    )
    from mux.contracts.config import BackendConfig

    runtime, auth, origin, service, trace, payload, frozen, _, agent, mapping = active_chat
    _without_own_tools(payload)
    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    async with runtime.session_factory.begin() as session:
        await set_channel_backend(
            session, channel, BackendConfig(backend="openai", model="gpt-6-luna")
        )
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.channel_pin == (
        "This channel pins OpenAI (gpt-6-luna), so this agent keeps answering on that here"
        ' until an admin clears it: say "clear this channel\'s pin".'
    ), changed.channel_pin
    assert changed.summary.endswith(changed.channel_pin), changed.summary
    async with runtime.session_factory.begin() as session:
        await clear_channel_backend(session, channel)
    agent()
    cleared = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
        expected_revision=1,
    )
    assert cleared.channel_pin is None and "pins" not in cleared.summary
    assert "channel_pin" not in cleared.model_dump()
    assert not trace.violations


@pytest.mark.parametrize("target", ["openai", "gemini"])
async def test_a_copy_switches_from_a_thread_another_agent_answers(active_chat, target):
    """J1/J2: the provider is the copy's own setting, so a thread whose responder is
    another agent (here Researcher) applies it for the copy."""
    import httpx
    from daimon.core.channel_backend import load_backend_agent
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, _, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend=target,
        origin_context_id=str(origin.id),
        expected_revision=0,
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
    return auth_identity(uuid4(), uuid4(), platform="discord", platform_user_id="123")


@pytest.mark.parametrize(
    ("refusal", "says"),
    [
        ({"reason": "agent_not_found"}, "no agent named 'X'"),
        ({"reason": "backend_not_allowed"}, "OpenAI isn't allowed in this server"),
        ({"reason": "source_context_unavailable"}, "Ask this agent in a server channel or thread"),
        (
            {"reason": "source_context_unavailable", "detail": "no_live_session"},
            "couldn't find my conversation in this thread",
        ),
        (
            {"reason": "source_context_unavailable", "detail": "other_agents_session"},
            "Another agent's conversation",
        ),
        (
            {"reason": "source_context_unavailable", "detail": "session_unreadable"},
            "Try again in a minute",
        ),
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
    assert "preview" not in text.lower(), "no refusal asks for a preview"


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

    runtime, auth, origin, service, trace, payload, _, _, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))
    member = replace(auth, role=Role.USER, is_admin=False, is_platform_admin=False)

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    agent()
    reads = len(trace.requests)
    with pytest.raises(ToolError, match="needs its owner"):
        await _request_agent_backend_switch_impl(
            runtime,
            member,
            agent_name="Researcher-copy",
            target_backend="gemini",
            origin_context_id=str(origin.id),
            expected_revision=0,
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
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        member,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed" and changed.agent_name == "Researcher-copy", "applied"


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
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert "This conversation moves to Gemini from the next message" in changed.summary
    assert "keeps its earlier messages" in changed.summary, changed.summary
    ((channel, thread, account, _),) = await _release_rows(runtime, auth)
    assert (channel, thread, account) == ("channel", "thread", str(auth.account_id))


async def test_switching_a_copy_leaves_this_conversation_and_says_so(active_chat):
    import httpx
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, _, _, _ = active_chat
    _without_own_tools(payload)
    copy = {**payload, "id": "agent_copy", "name": "Researcher-copy"}
    copy["metadata"] = {**payload["metadata"], "daimon_name": "Researcher-copy"}
    trace.router.add("GET", r"/v1/agents/agent_copy", lambda *_: httpx.Response(200, json=copy))

    def agent():
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload, copy])))

    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert "isn't the one answering here" in changed.summary
    assert await _release_rows(runtime, auth) == []


@pytest.mark.parametrize("kind", ["handoff", "setup"])
async def test_a_switch_of_the_copy_this_thread_was_handed_to_moves_it(active_chat, kind):
    """J2: hand this conversation to the copy (same provider), then switch the copy
    and the switch moves the conversation. A setup
    conversation, whose agent is only its configuration target, never moves."""
    import httpx
    from daimon.core.stores.thread_agent_bindings import create_binding
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    runtime, auth, origin, service, trace, payload, _, _, _, _ = active_chat
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

    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher-copy",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    rows = await _release_rows(runtime, auth)
    if kind == "handoff":
        assert "This conversation moves to Gemini from the next message" in changed.summary
        assert len(rows) == 1
    else:
        assert "isn't the one answering here" in changed.summary
        assert rows == []


async def test_a_switch_to_gpt_never_creates_the_tenants_environment(active_chat, monkeypatch):
    from daimon.core.turn import openai_environment

    async def created(*_args, **_kwargs):
        raise AssertionError("a switch created the tenant's OpenAI environment")

    monkeypatch.setattr(openai_environment, "tenant_environment", created)
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "offline-key")
    runtime, auth, origin, service, trace, payload, frozen, _, agent, mapping = active_chat
    _without_own_tools(payload)
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="openai",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed", "the switch applies"


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

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
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
    member = replace(auth, role=Role.USER, is_admin=False, is_platform_admin=False)
    switch = {
        "agent_name": "Researcher",
        "target_backend": "gemini",
        "origin_context_id": str(origin.id),
    }
    # Without "for everyone", a member and an admin alike are offered a copy instead.
    for who in (member, auth):
        agent()
        with pytest.raises(ToolError) as refused:
            await _request_agent_backend_switch_impl(runtime, who, **switch, expected_revision=0)
        assert str(refused.value).startswith(
            "Switching Researcher changes it for everyone on this server. "
            "To try Gemini just here, I can switch only this conversation to Gemini, "
            "or make a copy of me on Gemini. Nothing changed."
        )
    # "For everyone" still needs the owner or an admin.
    agent()
    with pytest.raises(ToolError, match="needs its owner or a server admin"):
        await _request_agent_backend_switch_impl(
            runtime, member, **switch, for_everyone=True, expected_revision=0
        )
    assert await _release_rows(runtime, auth) == []
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        **switch,
        for_everyone=True,
        expected_revision=0,
    )
    assert changed.status == "changed"
    assert (
        "Researcher now uses Gemini. New conversations with Researcher use it." in changed.summary
    )


async def test_a_non_default_agent_switches_without_saying_for_everyone(active_chat):
    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    assert runtime.deployment_default.agent_name != "Researcher"
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed", "the switch applies"


@pytest.mark.parametrize("on", ["anthropic", "openai"])
async def test_a_no_drop_switch_of_the_agent_everyone_uses_when_for_everyone_then_applies(
    active_chat, on
):
    """A provider switch of the default agent goes like a Claude model change of it.

    Both need "for everyone" and then apply on the first call when nothing stops working.
    """
    from daimon.core.scope import DeploymentDefault

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    payload["model"] = {"id": "claude-haiku-5-5", "speed": "standard"}
    runtime = replace(
        runtime,
        deployment_default=DeploymentDefault(agent_name="Researcher", environment_name="default"),
    )
    if on == "openai":
        to_gpt = {
            "agent_name": "Researcher",
            "target_backend": "openai",
            "origin_context_id": str(origin.id),
            "for_everyone": True,
        }
        agent()
        await _request_agent_backend_switch_impl(
            runtime,
            auth,
            **to_gpt,
            expected_revision=0,
        )
        agent()
        moved = await service.current(await load_backend_agent_for(runtime, auth))
        assert moved is not None and moved.backend == "openai", "the agent starts on GPT"
    # From Claude this is a model change (Haiku to the default); from GPT a provider switch.
    to_claude = {
        "agent_name": "Researcher",
        "target_backend": "anthropic",
        "origin_context_id": str(origin.id),
    }
    revision = 1 if on == "openai" else 0
    agent()
    with pytest.raises(ToolError, match="changes it for everyone on this server"):
        await _request_agent_backend_switch_impl(
            runtime, auth, **to_claude, expected_revision=revision
        )
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        auth,
        **to_claude,
        for_everyone=True,
        expected_revision=revision,
    )
    assert changed.status == "changed", "nothing stops working, so no card either way"
    agent()
    now = await service.current(await load_backend_agent_for(runtime, auth))
    assert now is not None and now.backend in (None, "anthropic"), "the agent is on Claude"
    assert not trace.replies and not trace.violations, "every scripted reply was used"


@pytest.mark.parametrize("provider_turn", [True, False])
async def test_a_switch_applies_without_posting_a_card(active_chat, monkeypatch, provider_turn):
    """Neither a Claude turn nor a provider turn posts a card: the call applies."""
    from unittest.mock import AsyncMock

    import daimon.adapters.mcp.tools.discord._switch_card as switch_card_mod

    runtime, auth, origin, service, trace, payload, _, _, agent, _ = active_chat
    _without_own_tools(payload)
    post = AsyncMock(return_value="message-1")
    monkeypatch.setattr(switch_card_mod, "post_switch_card", post)
    caller = replace(auth, no_confirmation_surface=provider_turn)
    agent()
    changed = await _request_agent_backend_switch_impl(
        runtime,
        caller,
        agent_name="Researcher",
        target_backend="gemini",
        origin_context_id=str(origin.id),
        expected_revision=0,
    )
    assert changed.status == "changed", "the first call applies"
    post.assert_not_awaited()
    agent()
    current = await service.current(await load_backend_agent_for(runtime, auth))
    assert current is not None and current.backend == "gemini", "written at once"
    assert not trace.replies and not trace.violations, "every scripted reply was used"


async def load_backend_agent_for(runtime, auth):
    from daimon.core.channel_backend import load_backend_agent

    return await load_backend_agent(
        runtime.client, runtime.session_factory, tenant_id=auth.tenant_id, agent_name="Researcher"
    )


async def test_an_older_callers_consent_fields_are_ignored_and_the_switch_applies(monkeypatch):
    """A caller still sending preview_digest and accepted_drops reaches the same switch."""
    import daimon.adapters.mcp.tools.agents as agents_mod
    from daimon.adapters.mcp.tools.agents import AgentBackendSwitchInfo, register_agent_tools
    from daimon.core.config import TurnSettings
    from fastmcp import FastMCP

    seen: dict[str, object] = {}

    async def switched(runtime, auth, **call):
        seen.update(call)
        return AgentBackendSwitchInfo(
            status="changed",
            agent_name="Researcher",
            target_backend="openai",
            expected_revision=1,
            unsupported_skills=(),
            unsupported_mcps=(),
            unsupported_capabilities=(),
            shared_thread_limits=(),
            cost_line="",
            summary="Researcher now uses GPT.",
        )

    monkeypatch.setattr(agents_mod, "_request_agent_backend_switch_impl", switched)
    monkeypatch.setattr(agents_mod, "_auth", AsyncMock(return_value=MagicMock()))
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(channel_backends=True, agent_backends=True)
    mcp = FastMCP(name="t")
    register_agent_tools(mcp, runtime)
    result = await mcp.call_tool(
        "request_agent_backend_switch",
        {
            "agent_name": "Researcher",
            "target_backend": "openai",
            "expected_revision": 1,
            "preview_digest": "a" * 64,
            "accepted_drops": ["mcp"],
        },
    )
    assert result.structured_content is not None, "the tool answered"
    assert result.structured_content["status"] == "changed", result.structured_content
    assert seen["expected_revision"] == 1, "the revision still guards the write"
    assert "preview_digest" not in seen and "accepted_drops" not in seen, seen

"""Backend switching uses the existing confirmation gate with a bound reference."""

from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
)
from pydantic import ValidationError


def _call(**changes: object) -> ToolCall:
    data: dict[str, object] = {
        "agent_name": "Researcher",
        "target_backend": "openai",
        "expected_revision": 2,
        "preview_digest": "a" * 64,
    }
    data.update(changes)
    return ToolCall(
        tool_use_id="switch-test",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_agent_backend_switch",
        input=data,
    )


def test_switch_prompt_binds_reference_and_never_accepts_model_facts() -> None:
    prompt = prompt_for_tool_call(
        _call(cost_line="Free", unsupported_skills=[], detail_lines=["All supported"]),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert prompt.admin is True
    assert prompt.backend_switch is not None
    assert prompt.backend_switch.model_dump() == {
        "agent": "Researcher",
        "target_backend": "openai",
        "expected_revision": 2,
        "preview_digest": "a" * 64,
    }
    assert prompt.detail_lines == ()
    assert prompt.consequence is None
    assert "Free" not in prompt.model_dump_json()
    assert "All supported" not in prompt.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": -1},
        {"expected_revision": "2"},
        {"expected_revision": True},
        {"preview_digest": "bad"},
        {"target_backend": "unknown"},
        {"agent_name": ""},
    ],
)
def test_malformed_switch_reference_never_creates_an_approval(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        prompt_for_tool_call(
            _call(**changes), requester_platform_user_id="requester", now=datetime.now(UTC)
        )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": -1},
        {"expected_revision": "2"},
        {"expected_revision": None},
        {"expected_revision": True},
        {"preview_digest": "bad"},
        {"target_backend": "unknown"},
        {"agent_name": ""},
    ],
)
async def test_malformed_switch_is_denied_without_crashing_sibling_decisions(
    changes: dict[str, object], enabled: bool
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_tools
    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval

    confirm = AsyncMock(return_value="approved")
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(enabled=enabled),
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    preview_call = _call(preview_digest=None).model_copy(update={"tool_use_id": "preview-sibling"})
    denied, preview = await asyncio.gather(
        posture.decide(_call(**changes)), posture.decide(preview_call)
    )
    assert denied.allow is False
    assert denied.deny_message and "did not run" in denied.deny_message
    assert preview.allow is True
    confirm.assert_not_awaited()


def test_preview_is_a_read_but_confirm_is_never_unattended() -> None:
    policy = ToolSafetyPolicy(enabled=True, unattended_writes=("*",))
    trusted = frozenset({DAIMON_SERVER_NAME})
    assert (
        decide_tool_call(
            policy,
            _call(preview_digest=None),
            attended=True,
            trusted_servers=trusted,
            confirmed_tools=frozenset({"request_agent_backend_switch"}),
        ).outcome
        == "allow"
    )
    assert (
        decide_tool_call(
            policy,
            _call(),
            attended=True,
            trusted_servers=trusted,
            confirmed_tools=frozenset({"request_agent_backend_switch"}),
        ).outcome
        == "ask"
    )
    assert (
        decide_tool_call(
            policy,
            _call(),
            attended=False,
            trusted_servers=trusted,
            confirmed_tools=frozenset({"request_agent_backend_switch"}),
        ).outcome
        == "deny"
    )


@pytest.fixture
async def owned_preview(db_preflight, db_committing_engine):
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import BackendActor, load_backend_agent, make_backend_service
    from daimon.core.config import TurnSettings
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_account, make_platform_principal, make_tenant
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("Backend control proofs require an owned DB or isolated CI")
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="requester"
        )
    trace = ScriptedTransport()
    native = ma_agent(id="owned-agent", tenant_id=str(tenant.id), name="Researcher")
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = "Researcher"

    def reply():
        return ScriptedReply("GET", "/v1/agents", list_response([payload]))

    trace.queue(reply())
    async with trace.client() as client:
        agent = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="Researcher"
        )
        service = make_backend_service(sessions, TurnSettings(path="mux", channel_backends=True))
        actor = BackendActor(tenant.id, account.id, Subject(is_admin=True))
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(
            tenant.id, ("anthropic", "openai"), policy.revision, actor
        )
        preview = await service.preview(agent, "openai")
        prompt = prompt_for_tool_call(
            _call(expected_revision=preview.setting_revision, preview_digest=preview.digest),
            requester_platform_user_id="requester",
            now=datetime.now(UTC),
        )
        yield service, agent, actor, sessions, client, trace, payload, reply, prompt


async def test_server_preview_and_current_admin_are_the_only_card_authority(owned_preview):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    admin = AsyncMock(return_value=True)
    prepare, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    assert prepared.backend_switch == prompt.backend_switch
    assert prepared.expires_at == prompt.expires_at
    assert prepared.admin
    preview = await service.preview(agent, "openai")
    assert prepared.detail_lines[-1] == preview.cost_line
    assert "worst case per reply: unknown" in prepared.detail_lines[-1].lower()
    assert prepared.detail_lines[0].startswith("Unsupported skills: ")
    trace.queue(reply())
    assert await check(prepared, "requester") is None
    assert admin.await_count == 2
    assert await service.current(agent) is None
    rows = await service.audit(agent.tenant_id, agent_id=agent.agent_id)
    assert rows == ()
    assert not trace.violations and not trace.replies


async def test_a_click_claiming_drops_against_an_undeclared_preview_refuses(owned_preview):
    """`verified()`'s card-side recheck (confirmation.py) must refuse a click whose
    reference claims accepted drops when the real, re-fetched preview declares none --
    the chat-card mirror of `AgentBackendService`'s own service-side guard against the
    same claim.
    """
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    admin = AsyncMock(return_value=True)
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    preview = await service.preview(agent, "openai")
    assert not preview.declared_drops, "this fixture's agent must declare no drops"
    assert prompt.backend_switch is not None
    claiming_drops = prompt.model_copy(
        update={
            "backend_switch": prompt.backend_switch.model_copy(
                update={"accepted_drops": ("memory",)}
            )
        }
    )
    trace.queue(reply())
    assert await check(claiming_drops, "requester") is not None
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id, agent_id=agent.agent_id) == ()


@pytest.mark.parametrize("change", ["admin", "allowlist", "configuration", "revision"])
async def test_click_rechecks_real_preview_and_rejects_changed_facts(owned_preview, change):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    admin = AsyncMock(return_value=change != "admin")
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    if change == "configuration":
        payload["system"] = "Configuration changed after preview"
    elif change == "allowlist":
        policy = await service.tenant_policy(agent.tenant_id)
        await service.set_tenant_allowlist(agent.tenant_id, ("anthropic",), policy.revision, actor)
    elif change == "revision":
        await service.set_backend(agent, "anthropic", 0, actor)
    trace.queue(reply())
    refused = await check(prompt, "requester")
    assert refused is not None
    if change == "revision":
        # Someone else's switch landed after this card was posted: say where it is now.
        assert refused.startswith(
            f"Someone just switched {prompt.backend_switch.agent}; it's now on Anthropic. "
            "Ask again if you still want"
        ), refused
    current = await service.current(agent)
    assert current is None if change != "revision" else current.local == 1
    assert not trace.violations and not trace.replies


@pytest.mark.parametrize("bad_scope", ["tenant", "account", "requester"])
async def test_card_scope_refuses_before_any_agent_read(owned_preview, bad_scope):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    tenant_id = uuid4() if bad_scope == "tenant" else agent.tenant_id
    account_id = uuid4() if bad_scope == "account" else actor.account_id
    admin = AsyncMock(return_value=True)
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=tenant_id,
        account_id=account_id,
        current_admin=admin,
    )
    assert await check(prompt, "other" if bad_scope == "requester" else "requester") is not None
    admin.assert_not_awaited()
    assert len(trace.requests) == 1
    assert await service.current(agent) is None


@pytest.mark.parametrize("admin_prompt,clicker", [(False, "requester"), (True, "other")])
async def test_shared_admin_predicate_skips_legacy_and_other_requesters(admin_prompt, clicker):
    from unittest.mock import AsyncMock

    from daimon.core.posted_controls.lifecycle import check_confirmation_admin

    check = AsyncMock(side_effect=AssertionError("must not perform admin I/O"))
    prompt = prompt_for_tool_call(
        _call(), requester_platform_user_id="requester", now=datetime.now(UTC)
    ).model_copy(update={"admin": admin_prompt})
    assert await check_confirmation_admin(prompt, clicker, check) is None
    check.assert_not_awaited()


async def test_shared_admin_predicate_fails_closed_on_checker_error():
    from unittest.mock import AsyncMock

    from daimon.core.posted_controls.lifecycle import check_confirmation_admin

    prompt = prompt_for_tool_call(
        _call(), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    check = AsyncMock(side_effect=RuntimeError("admin lookup unavailable"))
    assert await check_confirmation_admin(prompt, "requester", check) is not None
    assert await check_confirmation_admin(prompt, "requester", None) is not None
    assert await check_confirmation_admin(prompt, "requester", check, "approved") is not None
    # A Deny changes nothing and needs no evidence: the check (which records a
    # re-previewed card's Confirm, J5) never sees it.
    check.reset_mock()
    assert await check_confirmation_admin(prompt, "requester", check, "denied") is None
    check.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_only_explicit_backend_tools_are_gated_when_safety_is_off(enabled):
    from daimon.core.tool_safety import session_tools_for_policy

    selected = frozenset({"request_agent_backend_switch"}) if enabled else frozenset()
    tools = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": DAIMON_SERVER_NAME,
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        }
    ]
    trusted = frozenset({DAIMON_SERVER_NAME})
    policy = ToolSafetyPolicy()
    gated = session_tools_for_policy(
        policy, tools, trusted_servers=trusted, confirmed_tools=selected
    )
    if not enabled:
        assert gated is None
        return
    assert gated[0]["configs"] == [
        {
            "name": "request_agent_backend_switch",
            "enabled": True,
            "permission_policy": {"type": "always_ask"},
        }
    ]
    assert gated[0]["default_config"] == tools[0]["default_config"]
    assert (
        session_tools_for_policy(policy, gated, trusted_servers=trusted, confirmed_tools=selected)
        is None
    )
    assert (
        decide_tool_call(
            policy, _call(), attended=True, trusted_servers=trusted, confirmed_tools=selected
        ).outcome
        == "ask"
    )
    assert (
        decide_tool_call(
            policy, _call(), attended=False, trusted_servers=trusted, confirmed_tools=selected
        ).outcome
        == "deny"
    )
    assert (
        decide_tool_call(
            policy,
            _call(preview_digest=None),
            attended=True,
            trusted_servers=trusted,
            confirmed_tools=selected,
        ).outcome
        == "allow"
    )


@pytest.mark.parametrize("stage", ["agent", "preview"])
async def test_permission_revocation_during_awaited_evidence_refuses(
    owned_preview, monkeypatch, stage
):
    from daimon.core import channel_backend
    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    admin_state = {"allowed": True}
    load = channel_backend.load_backend_agent
    preview = service.preview

    async def changing_load(*args, **kwargs):
        loaded = await load(*args, **kwargs)
        if stage == "agent":
            admin_state["allowed"] = False
        return loaded

    async def changing_preview(*args, **kwargs):
        result = await preview(*args, **kwargs)
        if stage == "preview":
            admin_state["allowed"] = False
        return result

    async def current_admin(clicker):
        return admin_state["allowed"]

    monkeypatch.setattr(channel_backend, "load_backend_agent", changing_load)
    monkeypatch.setattr(service, "preview", changing_preview)
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=current_admin,
    )
    trace.queue(reply())
    assert await check(prompt, "requester") is not None
    assert await service.current(agent) is None
    assert not trace.violations and not trace.replies


@pytest.mark.parametrize(
    "bad_field", ["digest", "target_backend", "tenant_id", "agent_id", "allowed"]
)
async def test_malformed_service_preview_cannot_claim_an_admin_card(
    owned_preview, monkeypatch, bad_field
):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    preview = await service.preview(agent, "openai")
    bad = {
        "digest": "invalid",
        "target_backend": "gemini",
        "tenant_id": uuid4(),
        "agent_id": uuid4(),
        "allowed": False,
    }
    monkeypatch.setattr(
        service,
        "preview",
        AsyncMock(return_value=preview.model_copy(update={bad_field: bad[bad_field]})),
    )
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
    )
    trace.queue(reply())
    assert await check(prompt, "requester") is not None
    assert await service.current(agent) is None
    assert not trace.violations and not trace.replies


def test_unselected_backend_call_keeps_legacy_decisions_and_operator_denial_wins():
    trusted = frozenset({DAIMON_SERVER_NAME})
    assert (
        decide_tool_call(
            ToolSafetyPolicy(enabled=True),
            _call(),
            attended=True,
            trusted_servers=trusted,
        ).outcome
        == "allow"
    )
    assert (
        decide_tool_call(
            ToolSafetyPolicy(enabled=True, denied=("daimon-mcp/request_agent_backend_switch",)),
            _call(),
            attended=True,
            trusted_servers=trusted,
            confirmed_tools=frozenset({"request_agent_backend_switch"}),
        ).reason
        == "denied_by_operator"
    )


@pytest.mark.parametrize(
    "call_name,server",
    [
        ("publish_report", DAIMON_SERVER_NAME),
        ("add_skill", DAIMON_SERVER_NAME),
        ("get_issue", "third-party"),
        ("bash", None),
    ],
)
async def test_backend_only_opt_in_does_not_approve_other_blocked_calls(call_name, server):
    from unittest.mock import AsyncMock

    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval

    confirm = AsyncMock(return_value="approved")
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(),
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=frozenset({"request_agent_backend_switch"}),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(
        ToolCall(
            tool_use_id="blocked",
            server_name=server,
            tool_name=call_name,
            input={"content_hash": "a" * 64},
        )
    )
    assert not result.allow
    confirm.assert_not_awaited()


@pytest.mark.parametrize("attended", [False, True])
async def test_safety_off_backend_confirmation_uses_the_one_existing_surface(attended):
    from unittest.mock import AsyncMock

    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval

    confirm = AsyncMock(return_value="approved")
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(),
        requester_platform_user_id="requester",
        confirm=confirm,
        attended=attended,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=frozenset({"request_agent_backend_switch"}),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(_call())
    assert result.allow is attended
    if attended:
        confirm.assert_awaited_once()
        assert confirm.await_args.args[0].admin is True
    else:
        confirm.assert_not_awaited()


async def test_publish_still_asks_only_when_explicitly_selected_with_backend_controls():
    from unittest.mock import AsyncMock

    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval, RequireApproval

    confirm = AsyncMock(return_value="approved")
    default = chat_tool_confirmation(
        ToolSafetyPolicy(),
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
    )
    assert isinstance(default, RequireApproval)
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(),
        requester_platform_user_id="requester",
        confirm=confirm,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        asks_before_publishing=True,
        confirmed_tools=frozenset({"request_agent_backend_switch"}),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(
        ToolCall(
            tool_use_id="publish",
            server_name=DAIMON_SERVER_NAME,
            tool_name="publish_report",
        )
    )
    assert result.allow
    confirm.assert_awaited_once()
    assert not confirm.await_args.args[0].admin


def test_legacy_prompt_serialization_omits_the_opt_in_fields():
    from datetime import timedelta

    from daimon.core.confirmation import ConfirmationPrompt

    legacy = ConfirmationPrompt(
        title="Create issue?",
        action="Create issue",
        denied_action="Create issue not completed",
        requester_platform_user_id="requester",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert "admin" not in legacy.model_dump()
    assert "backend_switch" not in legacy.model_dump()
    assert '"admin"' not in legacy.model_dump_json()
    opted = prompt_for_tool_call(
        _call(), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    assert opted.model_dump()["admin"] is True
    assert opted.model_dump()["backend_switch"]["preview_digest"] == "a" * 64


@pytest.mark.parametrize("selected", [False, True])
async def test_real_sdk_create_and_desired_snapshot_use_the_same_opt_in_tools(selected):
    import httpx
    from daimon.core.config import McpSettings
    from daimon.core.session_snapshot import (
        desired_snapshot,
        hash_mcp_servers,
        hash_tools,
        session_mcp_servers,
        session_tools,
    )
    from daimon.core.sessions import create_session
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

    native = ma_agent(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": DAIMON_SERVER_NAME,
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"name": DAIMON_SERVER_NAME, "type": "url", "url": "https://foreign.invalid"}],
    )
    settings = McpSettings(public_url="https://mcp.invalid")
    confirmed = frozenset({"request_agent_backend_switch"}) if selected else frozenset()
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions",
            httpx.Response(200, json=ma_session(agent=native).model_dump(mode="json")),
        )
    )
    async with trace.client() as client:
        await create_session(
            client,
            agent=native,
            environment=ma_environment(),
            mcp_settings=settings,
            confirmed_tools=confirmed,
        )
        if not selected:
            trace.queue(
                ScriptedReply(
                    "POST",
                    "/v1/sessions",
                    httpx.Response(200, json=ma_session(agent=native).model_dump(mode="json")),
                )
            )
            await create_session(
                client,
                agent=native,
                environment=ma_environment(),
                mcp_settings=settings,
            )
            assert trace.requests[0].body == trace.requests[1].body
    sent = trace.requests[0].json()
    tools = session_tools(
        native,
        frozenset(),
        tool_safety=ToolSafetyPolicy(),
        public_url=str(settings.public_url),
        confirmed_tools=confirmed,
    )
    servers = session_mcp_servers(
        native,
        frozenset(),
        tool_safety=ToolSafetyPolicy(),
        public_url=str(settings.public_url),
        confirmed_tools=confirmed,
    )
    snapshot = desired_snapshot(
        native,
        hidden_mcp_server_names=frozenset(),
        environment_id=ma_environment().id,
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        public_url=str(settings.public_url),
        confirmed_tools=confirmed,
    )
    assert snapshot.tools_sha256 == hash_tools(tools)
    assert snapshot.mcp_servers_sha256 == hash_mcp_servers(servers)
    if selected:
        assert sent["agent"]["tools"] == [
            {
                "type": "mcp_toolset",
                "mcp_server_name": DAIMON_SERVER_NAME,
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
        assert sent["agent"]["mcp_servers"] == [
            {
                "name": DAIMON_SERVER_NAME,
                "type": "url",
                "url": "https://mcp.invalid/",
            }
        ]
        assert sent["agent"]["tools"][0]["configs"][0]["name"] == "request_agent_backend_switch"
        assert sent["agent"]["mcp_servers"][0]["url"] == "https://mcp.invalid/"
    else:
        assert sent["agent"] == native.id
        assert list(tools) == native.tools
        assert list(servers) == native.mcp_servers
    assert not trace.violations and not trace.replies


async def test_real_sdk_refresh_preserves_default_bytes_and_the_selected_gate(owned_preview):
    import httpx
    from daimon.core.config import McpSettings
    from daimon.core.session_compat import ReplaceToolsAndMcpServers
    from daimon.core.session_snapshot import desired_snapshot, hash_tools, session_tools
    from daimon.core.session_update_ops import AppliedOps, apply_update_ops
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    service, backend_agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    native = ma_agent(
        id="agent-refresh",
        tenant_id=str(backend_agent.tenant_id),
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": DAIMON_SERVER_NAME,
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"name": DAIMON_SERVER_NAME, "type": "url", "url": "https://foreign.invalid"}],
    )
    settings = McpSettings(public_url="https://mcp.invalid")
    recorded = desired_snapshot(
        native,
        hidden_mcp_server_names=frozenset(),
        environment_id="env-refresh",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        public_url=str(settings.public_url),
    )
    selected = frozenset({"request_agent_backend_switch"})
    bodies = []
    for options in ({}, {"confirmed_tools": frozenset()}, {"confirmed_tools": selected}):
        trace.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/sess-refresh",
                httpx.Response(
                    200, json=ma_session(id="sess-refresh", agent=native).model_dump(mode="json")
                ),
            )
        )
        result = await apply_update_ops(
            client,
            sessions,
            ops=(ReplaceToolsAndMcpServers(),),
            session_id="sess-refresh",
            recorded=recorded,
            agent=native,
            tenant_id=backend_agent.tenant_id,
            agent_uuid=backend_agent.agent_id,
            account_id=actor.account_id,
            mcp=settings,
            tool_safety=ToolSafetyPolicy(),
            fernet=None,
            github_fallback_pat=None,
            github_app_id=None,
            github_app_private_key=None,
            now=datetime.now(UTC),
            **options,
        )
        assert isinstance(result, AppliedOps)
        request = trace.requests[-1]
        bodies.append(request.body)
        if options.get("confirmed_tools"):
            tools = session_tools(
                native,
                frozenset(),
                tool_safety=ToolSafetyPolicy(),
                public_url=str(settings.public_url),
                confirmed_tools=selected,
            )
            assert result.snapshot.tools_sha256 == hash_tools(tools)
            sent = request.json()["agent"]
            assert sent["tools"][0]["configs"] == [
                {
                    "name": "request_agent_backend_switch",
                    "enabled": True,
                    "permission_policy": {"type": "always_ask"},
                }
            ]
            assert sent["mcp_servers"][0]["url"] == "https://mcp.invalid/"
        else:
            assert result.snapshot.tools_sha256 == recorded.tools_sha256
            assert result.snapshot.mcp_servers_sha256 == recorded.mcp_servers_sha256
    assert bodies[0] == bodies[1]
    assert bodies[1] != bodies[2]
    assert not trace.violations and not trace.replies


def test_switch_prompt_carries_an_optional_model() -> None:
    picked = prompt_for_tool_call(
        _call(target_backend="gemini", model="gemini-4-argon"),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert picked.backend_switch is not None and picked.backend_switch.model == "gemini-4-argon"
    legacy = prompt_for_tool_call(
        _call(), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    assert legacy.backend_switch is not None and "model" not in legacy.backend_switch.model_dump()


@pytest.mark.parametrize(
    ("default_agent", "place"), [(False, "server"), (True, "server"), (True, "workspace")]
)
async def test_card_names_the_model_and_where_the_switch_applies(
    owned_preview, default_agent, place
):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )
    preview = await service.preview(agent, "gemini", "gemini-4-argon")
    prompt = prompt_for_tool_call(
        _call(
            target_backend="gemini",
            model="gemini-4-argon",
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            for_everyone=default_agent,
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    prepare, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
        default_agent_name="Researcher" if default_agent else "daimon",
        place_word=place,
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    assert prepared.title == "Switch Researcher to Gemini (gemini-4-argon)?"
    assert prepared.consequence is not None
    reach = (
        f"Affects new conversations for everyone on this {place}"
        if default_agent
        else "Affects new conversations wherever Researcher answers"
    )
    # A switch moves each person's own conversation on their next message (#1495);
    # never "existing threads keep" the old provider.
    assert prepared.consequence.startswith(
        reach + ". Each person's own conversation with Researcher moves to Gemini on their "
        "next message; shared threads stay on Anthropic."
    )
    assert "keep" not in prepared.consequence.split("\n")[0]
    assert prepared.detail_lines[-1] == preview.cost_line
    # The same card for the default model refuses: the server preview differs.
    other = prompt.model_copy(
        update={"backend_switch": prompt.backend_switch.model_copy(update={"model": None})}
    )
    trace.queue(reply())
    assert await check(other, "requester") is not None
    assert await service.current(agent) is None


async def test_the_agents_owner_may_confirm_without_being_an_admin(owned_preview):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks
    from daimon.core.stores import agent_owners

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    admin = AsyncMock(return_value=False)
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=admin,
    )
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="admin_assigned",
        )
    trace.queue(reply())
    assert await check(prompt, "requester") is None
    # Ownership moves away between the card and the click: the click refuses.
    async with sessions.begin() as session:
        await agent_owners.clear_owner(session, tenant_id=agent.tenant_id, agent_id=agent.agent_id)
    trace.queue(reply())
    assert await check(prompt, "requester") is not None
    assert await service.current(agent) is None


def test_switch_prompt_binds_the_stated_agent_settings() -> None:
    from daimon.core.confirmation import reference_settings, settings_line

    prompt = prompt_for_tool_call(
        _call(thread_mode="shared", required_capabilities=["tool_loop"]),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert prompt.backend_switch is not None
    settings = reference_settings(prompt.backend_switch)
    assert settings is not None and settings.facts() == {
        "requires": {"tool_loop": {"level": "required", "fallback": None}},
        "thread_mode": "shared",
    }
    assert settings_line(settings.facts()) == (
        "shares threads where the channel allows it; requires tool_loop"
    )
    plain = prompt_for_tool_call(_call(), requester_platform_user_id="r", now=datetime.now(UTC))
    assert plain.backend_switch is not None and reference_settings(plain.backend_switch) is None
    with pytest.raises(ValidationError):
        prompt_for_tool_call(
            _call(thread_mode="everyone"), requester_platform_user_id="r", now=datetime.now(UTC)
        )


async def test_the_card_says_plainly_that_saved_keys_wont_be_available(owned_preview, monkeypatch):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    real = await service.preview(agent, "openai")
    dropping = type(real).model_validate(
        {
            **real.model_dump(),
            "drop_consent": {
                "declared_drops": ["credentials", "memory", "sandbox"],
                "source_pin": "a" * 64,
                "target_digest": "b" * 64,
            },
        }
    )
    monkeypatch.setattr(service, "preview", AsyncMock(return_value=dropping))
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
    )
    prompt = prompt_for_tool_call(
        _call(
            expected_revision=dropping.setting_revision,
            preview_digest=dropping.digest,
            accepted_drops=list(dropping.declared_drops),
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    assert prepared.action == "Switch Researcher to OpenAI"
    assert (
        "On OpenAI it won't have its saved keys, its memory and its sandbox's files and packages"
        in prepared.consequence
    )
    assert prepared.consequence.count("saved keys") == 1
    assert "credentials" not in prepared.action + prepared.consequence
    assert "Nothing is deleted" in prepared.consequence


@pytest.mark.parametrize("answering", ["Researcher", "someone-else", None])
async def test_the_card_says_this_conversation_moves_only_when_the_agent_answers_here(
    owned_preview, answering
):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
        answering_agent_name=answering,
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    moves = "This conversation moves to OpenAI from your next message"
    assert (moves in prepared.consequence) == (answering == "Researcher")
    assert "shared threads stay on Anthropic" in prepared.consequence


@pytest.mark.parametrize("handed_to", ["Researcher", "someone-else", None])
async def test_a_hand_off_in_this_thread_decides_whose_conversation_moves(owned_preview, handed_to):
    """J2: the turn's agent hands the thread to the copy, then switches the copy.

    The approved switch moves the copy's conversation here and drops its history,
    so the card must say so even though the turn's own agent is someone else.
    """
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks
    from daimon.core.stores.thread_agent_bindings import upsert_responder_binding

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    if handed_to is not None:
        async with sessions.begin() as session:
            await upsert_responder_binding(
                session,
                tenant_id=agent.tenant_id,
                platform="discord",
                parent_channel_id="parent-1",
                thread_id="thread-1",
                responder_ma_agent_id=f"agent_{handed_to}",
                responder_name=handed_to,
                created_by_account_id=actor.account_id,
                now=datetime.now(UTC),
            )
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
        answering_agent_name="daimon",
        handoff_thread=("discord", "parent-1", "thread-1"),
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    moves = "This conversation moves to OpenAI from your next message"
    assert (moves in prepared.consequence) == (handed_to == "Researcher")
    if handed_to == "Researcher":
        assert "earlier messages here aren't carried over" in prepared.consequence


async def _reload(owned_preview):
    from daimon.core.channel_backend import load_backend_agent

    _, agent, _, sessions, client, *_ = owned_preview
    return await load_backend_agent(
        client, sessions, tenant_id=agent.tenant_id, agent_name="Researcher"
    )


def _switch_callbacks(owned_preview, *, default_agent_name: str = "daimon"):
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks

    service, agent, actor, sessions, client, *_ = owned_preview
    return backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
        default_agent_name=default_agent_name,
        answering_agent_name="Researcher",
    )


def _switch_prompt(preview, **changes: object):
    return prompt_for_tool_call(
        _call(
            target_backend="gemini",
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            **changes,
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )


@pytest.mark.parametrize("default", ["named", "managed"])
async def test_a_default_agent_card_is_refused_unless_its_for_everyone(owned_preview, default):
    """A dropped for_everyone on the confirm call posts no card: the copy offer instead."""
    from daimon.core.confirmation import CardRefusal
    from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )
    if default == "managed":
        # A built-in agent is found by its managed mark, whatever its name.
        payload["metadata"][MA_METADATA_KEY_MANAGED] = "true"
    trace.queue(reply())
    agent = await _reload(owned_preview)
    preview = await service.preview(agent, "gemini", None)
    prepare, check = _switch_callbacks(
        owned_preview, default_agent_name="Researcher" if default == "named" else "daimon"
    )
    trace.queue(reply())
    with pytest.raises(CardRefusal) as refused:
        await prepare(_switch_prompt(preview))
    assert str(refused.value) == (
        "Switching Researcher changes it for everyone on this server. "
        "To try Gemini just here, I can make a copy of me on Gemini. Nothing changed."
    )
    trace.queue(reply())
    assert await check(_switch_prompt(preview), "requester") == str(refused.value)
    trace.queue(reply())
    prepared = await prepare(_switch_prompt(preview, for_everyone=True))
    assert prepared.consequence is not None
    assert prepared.consequence.startswith("Affects new conversations for everyone on this server")
    assert await service.current(agent) is None


async def test_a_switch_that_lands_before_the_card_is_posted_says_who_won(owned_preview):
    from daimon.core.confirmation import CardRefusal

    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )
    preview = await service.preview(agent, "gemini", None)
    await service.set_backend(agent, "openai", 0, actor)
    prepare, _ = _switch_callbacks(owned_preview)
    trace.queue(reply())
    with pytest.raises(CardRefusal) as refused:
        await prepare(_switch_prompt(preview))
    assert str(refused.value) == (
        "Someone just switched Researcher; it's now on OpenAI. "
        "Ask again if you still want Gemini. Nothing changed."
    )


async def test_a_refusal_worded_for_the_person_reaches_the_model_with_do_not_retry():
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import CardRefusal
    from daimon.core.tool_safety import OPEN_TOOL_SAFETY, backend_switch_tools
    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval

    said = (
        "Someone just switched Researcher; it's now on OpenAI. Ask again if you still want Gemini."
    )
    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="requester",
        confirm=AsyncMock(side_effect=CardRefusal(said)),
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(_call())
    # The person's words, then one instruction for the model only.
    assert result.allow is False
    assert result.deny_message == f"{said} Nothing changed. Do not retry it."


async def test_the_card_never_promises_a_move_in_a_setup_conversation(owned_preview):
    """A setup conversation stays put on an approved switch, so the card says nothing moves."""
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import backend_switch_callbacks
    from daimon.core.stores.thread_agent_bindings import create_binding

    service, agent, actor, sessions, client, trace, payload, reply, prompt = owned_preview
    async with sessions.begin() as session:
        await create_binding(
            session,
            tenant_id=agent.tenant_id,
            platform="discord",
            parent_channel_id="parent-1",
            thread_id="thread-1",
            responder_ma_agent_id="agent_daimon",
            responder_name="daimon",
            configuration_target_name="Researcher",
            kind="setup",
        )
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=sessions,
        anthropic=client,
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        current_admin=AsyncMock(return_value=True),
        answering_agent_name="Researcher",
        handoff_thread=("discord", "parent-1", "thread-1"),
    )
    trace.queue(reply())
    prepared = await prepare(prompt)
    assert "This conversation moves" not in prepared.consequence
    assert "shared threads stay on Anthropic" in prepared.consequence


async def _switch_card(
    owned_preview, *, expires_in: float = 600, requester: str = "requester", client: bool = False
):
    """A self-posted switch card for the fixture's OpenAI preview, as the MCP tool records it."""
    from datetime import timedelta

    from daimon.core.confirmation import BackendSwitchReference
    from daimon.core.stores.backend_switch_cards import create_card, mark_posted

    service, agent, actor, sessions, *_ = owned_preview
    preview = await service.preview(agent, "openai")
    reference = BackendSwitchReference.model_validate(
        {
            "agent": "Researcher",
            "target_backend": "openai",
            "expected_revision": preview.setting_revision,
            "preview_digest": preview.digest,
        }
    )
    async with sessions.begin() as session:
        card = await create_card(
            session,
            tenant_id=agent.tenant_id,
            account_id=actor.account_id,
            platform="discord",
            channel_id="parent-1",
            thread_id="thread-1",
            setup_thread=False,
            requester_platform_user_id=requester,
            client=client,
            answering_agent_name="Researcher",
            reference=reference.model_dump(mode="json"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )
        await mark_posted(session, card_id=card.id, message_id="posted-1")
    return card


def _press(owned_preview, card_id, *, action="confirm", clicker="requester", admin=True):
    from unittest.mock import AsyncMock

    from daimon.core.config import TurnSettings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.switch_cards import press_switch_card

    _, agent, _, sessions, client, *_ = owned_preview
    return press_switch_card(
        deps=None,
        sessionmaker=sessions,
        anthropic=client,
        turn_settings=TurnSettings(path="mux", channel_backends=True),
        deployment_default=DeploymentDefault(agent_name="daimon"),
        tenant_id=agent.tenant_id,
        card_id=card_id,
        action=action,
        clicker=clicker,
        current_admin=admin if callable(admin) else AsyncMock(return_value=admin),
        place_word="server",
        now=datetime.now(UTC),
        message_id="posted-1",
    )


async def _card_status(owned_preview, card_id):
    from daimon.core.stores.backend_switch_cards import get_card

    _, agent, _, sessions, *_ = owned_preview
    async with sessions() as session:
        card = await get_card(session, tenant_id=agent.tenant_id, card_id=card_id)
    assert card is not None
    return card.status


async def test_only_the_requester_can_press_a_self_posted_switch_card(owned_preview):
    """Anyone else's press is ignored and leaves the card usable by its requester."""
    service, agent, *_ = owned_preview
    card = await _switch_card(owned_preview)
    assert await _press(owned_preview, card.id, clicker="someone-else") is None
    assert await _press(owned_preview, card.id, action="cancel", clicker="someone-else") is None
    assert await _card_status(owned_preview, card.id) == "pending"
    assert await service.current(agent) is None


async def test_confirming_a_self_posted_card_switches_once(owned_preview):
    from daimon.core.switch_cards import answered_switch_card

    service, agent, _, _, _, trace, _, reply, _ = owned_preview
    card = await _switch_card(owned_preview)
    trace.queue(reply())
    trace.queue(reply())
    press = await _press(owned_preview, card.id)
    assert press is not None and press.applied and press.closed
    assert press.text.startswith("Switched Researcher to OpenAI.")
    current = await service.current(agent)
    assert current is not None and current.backend == "openai"
    assert await _card_status(owned_preview, card.id) == "applied"
    assert answered_switch_card(press).state == "applied"
    # A second press answers with what happened; nothing is applied twice.
    again = await _press(owned_preview, card.id)
    assert again is not None and not again.applied and again.text == press.text
    assert (await service.current(agent)).local == current.local


async def test_cancelling_a_self_posted_card_changes_nothing(owned_preview):
    service, agent, *_ = owned_preview
    card = await _switch_card(owned_preview)
    press = await _press(owned_preview, card.id, action="cancel")
    assert press is not None and press.text == "Cancelled. Nothing changed."
    assert await _card_status(owned_preview, card.id) == "cancelled"
    assert await service.current(agent) is None


async def test_a_self_posted_card_pressed_after_it_expired_says_so(owned_preview):
    from daimon.core.switch_cards import EXPIRED_TEXT, answered_switch_card

    service, agent, *_ = owned_preview
    card = await _switch_card(owned_preview, expires_in=-1)
    press = await _press(owned_preview, card.id)
    assert press is not None and press.text == EXPIRED_TEXT
    assert answered_switch_card(press).state == "expired"
    assert await service.current(agent) is None


async def test_a_switch_that_lands_before_the_press_says_who_won(owned_preview):
    from daimon.core.switch_cards import answered_switch_card

    service, agent, actor, _, _, trace, _, reply, _ = owned_preview
    card = await _switch_card(owned_preview)
    await service.set_backend(agent, "anthropic", 0, actor)
    trace.queue(reply())
    press = await _press(owned_preview, card.id)
    assert press is not None and not press.applied
    assert press.text.startswith(
        "Someone just switched Researcher; it's now on Anthropic. Ask again if you still want OpenAI."
    )
    assert answered_switch_card(press).state == "refused"
    assert await _card_status(owned_preview, card.id) == "refused"
    assert (await service.current(agent)).backend == "anthropic"


def test_the_requested_switch_card_names_its_requester_and_expiry():
    import uuid

    from daimon.core.switch_cards import parse_custom_id, requested_switch_card

    card_id = uuid.uuid4()
    card = requested_switch_card(
        card_id,
        title="Switch Researcher to OpenAI?",
        facts=("Affects new conversations wherever Researcher answers.",),
        requester_platform_user_id="42",
        expires_at=datetime(2026, 10, 23, 18, 0, tzinfo=UTC),
    )
    assert card.footer == "Only {requester} can confirm. Expires {expires}."
    assert card.requester_platform_user_id == "42" and card.expires_at_unix is not None
    assert [parse_custom_id(button.custom_id or "") for button in card.buttons] == [
        (card_id, "confirm"),
        (card_id, "cancel"),
    ]
    assert parse_custom_id("bsw:not-a-card:confirm") is None


async def test_only_the_posted_message_can_press_its_switch_card(owned_preview):
    from unittest.mock import AsyncMock

    from daimon.core.config import TurnSettings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.backend_switch_cards import mark_posted
    from daimon.core.switch_cards import press_switch_card

    _, agent, _, sessions, client, *_ = owned_preview
    card = await _switch_card(owned_preview)
    async with sessions.begin() as session:
        await mark_posted(session, card_id=card.id, message_id="posted-1")

    async def press(message_id: str):
        return await press_switch_card(
            deps=None,
            sessionmaker=sessions,
            anthropic=client,
            turn_settings=TurnSettings(path="mux", channel_backends=True),
            deployment_default=DeploymentDefault(agent_name="daimon"),
            tenant_id=agent.tenant_id,
            card_id=card.id,
            action="cancel",
            clicker="requester",
            current_admin=AsyncMock(return_value=True),
            place_word="server",
            now=datetime.now(UTC),
            message_id=message_id,
        )

    assert await press("another-message") is None
    assert await _card_status(owned_preview, card.id) == "pending"
    cancelled = await press("posted-1")
    assert cancelled is not None and cancelled.text == "Cancelled. Nothing changed."


async def test_cancel_never_overwrites_a_confirm_that_claimed_the_card_first(owned_preview):
    from daimon.core.stores.backend_switch_cards import claim, finish
    from daimon.core.switch_cards import CONFIRMING_TEXT

    _, agent, _, sessions, *_ = owned_preview
    card = await _switch_card(owned_preview)
    async with sessions.begin() as session:
        assert await claim(
            session, tenant_id=agent.tenant_id, card_id=card.id, now=datetime.now(UTC)
        )
    # While that Confirm is still applying, Cancel answers only the presser.
    cancelling = await _press(owned_preview, card.id, action="cancel")
    assert cancelling is not None and not cancelling.closed
    assert cancelling.text == CONFIRMING_TEXT
    assert await _card_status(owned_preview, card.id) == "claimed"
    async with sessions.begin() as session:
        await finish(session, card_id=card.id, status="applied", outcome="Switched Researcher.")
    late = await _press(owned_preview, card.id, action="cancel")
    assert late is not None and late.text == "Switched Researcher."
    assert await _card_status(owned_preview, card.id) == "applied"


async def test_an_error_after_the_claim_closes_the_card_in_plain_words(owned_preview):
    from unittest.mock import AsyncMock

    from daimon.core.switch_cards import FAILED_TEXT

    service, agent, *_ = owned_preview
    card = await _switch_card(owned_preview)
    press = await _press(
        owned_preview, card.id, admin=AsyncMock(side_effect=RuntimeError("platform down"))
    )
    assert press is not None and press.closed and press.text == FAILED_TEXT
    assert await _card_status(owned_preview, card.id) == "refused"
    assert await service.current(agent) is None


async def test_a_requester_from_another_organisation_is_never_an_admin_at_the_press(owned_preview):
    service, agent, _, _, _, trace, _, reply, _ = owned_preview
    card = await _switch_card(owned_preview, client=True)
    trace.queue(reply())
    press = await _press(owned_preview, card.id, admin=True)
    assert press is not None and not press.applied
    assert await _card_status(owned_preview, card.id) == "refused"
    assert await service.current(agent) is None

"""Backend controls on the confirmation gate.

Admin settings cards ask, and so does a move to another provider once it carries
its revision; a thread's model pick never does. A switch card posted before
switches applied at once still answers its press.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
)

#: A backend control the admin card still confirms.
_GATED = "request_channel_pin_clear"


def _call(**changes: object) -> ToolCall:
    data: dict[str, object] = {
        "expected_revision": 2,
        "preview_digest": "a" * 64,
    }
    data.update(changes)
    return ToolCall(
        tool_use_id="pin-clear-test",
        server_name=DAIMON_SERVER_NAME,
        tool_name=_GATED,
        input=data,
    )


@pytest.mark.parametrize(
    "policy",
    [ToolSafetyPolicy(), ToolSafetyPolicy(enabled=True, unattended_writes=("*",))],
    ids=["safety-off", "safety-on"],
)
@pytest.mark.parametrize("attended", [False, True])
def test_a_thread_model_pick_is_never_gated_whoever_is_watching(attended, policy) -> None:
    """A thread's model pick runs like a model change: even an old digest asks for no card."""
    from daimon.core.tool_safety import backend_switch_tools, confirmation_gated_tools

    call = ToolCall(
        tool_use_id="switch",
        server_name=DAIMON_SERVER_NAME,
        tool_name="set_thread_model",
        input={"agent_name": "Researcher", "preview_digest": "a" * 64, "digest": "a" * 64},
    )
    verdict = decide_tool_call(
        policy,
        call,
        attended=attended,
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert verdict.outcome == "allow", f"a thread pick runs without a card: {verdict}"
    assert "set_thread_model" not in confirmation_gated_tools()


@pytest.mark.parametrize(
    "policy",
    [ToolSafetyPolicy(), ToolSafetyPolicy(enabled=True, unattended_writes=("*",))],
    ids=["safety-off", "safety-on"],
)
@pytest.mark.parametrize("attended", [False, True])
def test_a_provider_switch_asks_only_once_it_carries_the_revision(attended, policy) -> None:
    """The preview carries no revision and changes nothing; the confirming call needs the card."""
    from daimon.core.tool_safety import backend_switch_tools

    def verdict(data):
        call = ToolCall(
            tool_use_id="switch",
            server_name=DAIMON_SERVER_NAME,
            tool_name="request_agent_backend_switch",
            input={"agent_name": "Researcher", "target_backend": "openai", **data},
        )
        return decide_tool_call(
            policy,
            call,
            attended=attended,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            confirmed_tools=backend_switch_tools(enabled=True),
        ).outcome

    assert verdict({}) == "allow", "the preview changes nothing"
    assert verdict({"expected_revision": 0}) == ("ask" if attended else "deny"), (
        "the confirming call waits for the card, and an unattended run is refused"
    )


_DEPS = SimpleNamespace()


@pytest.fixture
async def owned_preview(db_preflight, db_committing_engine, monkeypatch):
    from daimon.core import channel_backend
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import (
        AgentBackendService,
        BackendActor,
        load_backend_agent,
    )
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

        # These tests pin the service without a source capture.
        service = AgentBackendService(sessions, enabled=True)
        monkeypatch.setattr(channel_backend, "make_backend_service", lambda *_, **__: service)
        actor = BackendActor(tenant.id, account.id, Subject(is_admin=True))
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(
            tenant.id, ("anthropic", "openai"), policy.revision, actor
        )
        yield service, agent, actor, sessions, client, trace, payload, reply


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

    selected = frozenset({_GATED}) if enabled else frozenset()
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
            "name": _GATED,
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
            ToolSafetyPolicy(enabled=True, denied=(f"daimon-mcp/{_GATED}",)),
            _call(),
            attended=True,
            trusted_servers=trusted,
            confirmed_tools=frozenset({_GATED}),
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
        confirmed_tools=frozenset({_GATED}),
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
        confirmed_tools=frozenset({_GATED}),
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
        confirmed_tools=frozenset({_GATED}),
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
    assert "configuration" not in legacy.model_dump()
    assert '"admin"' not in legacy.model_dump_json()
    opted = prompt_for_tool_call(
        _call(), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    assert opted.model_dump()["admin"] is True
    assert opted.model_dump()["configuration"]["preview_digest"] == "a" * 64


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
    confirmed = frozenset({_GATED}) if selected else frozenset()
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
            tool_safety=OPEN_TOOL_SAFETY,
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
                tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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
                        "name": _GATED,
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
        assert sent["agent"]["tools"][0]["configs"][0]["name"] == _GATED
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

    service, backend_agent, actor, sessions, client, trace, payload, reply = owned_preview
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
        tool_safety=OPEN_TOOL_SAFETY,
    )
    selected = frozenset({_GATED})
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
                    "name": _GATED,
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


async def test_a_refusal_worded_for_the_person_reaches_the_model_as_is():
    from unittest.mock import AsyncMock

    from daimon.core.confirmation import CardRefusal
    from daimon.core.tool_safety import OPEN_TOOL_SAFETY, backend_switch_tools
    from daimon.core.turn.approvals import chat_tool_confirmation
    from daimon.core.turn.posture import PolicyApproval

    said = "Someone just changed this channel's pin. Ask again if you still want this."
    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="requester",
        confirm=AsyncMock(side_effect=CardRefusal(said)),
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=backend_switch_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(_call())
    assert result.allow is False and result.deny_message == said


async def _switch_card(
    owned_preview,
    *,
    expires_in: float = 600,
    requester: str = "requester",
    client: bool = False,
    **reference: object,
):
    """A switch card's row as Daimon stored it before switches applied at once."""
    import uuid
    from datetime import timedelta

    from daimon.core._models import BackendSwitchCard

    service, agent, actor, sessions, *_ = owned_preview
    revision = (await service.preview(agent, "openai")).setting_revision
    card = BackendSwitchCard(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        account_id=actor.account_id,
        platform="discord",
        channel_id="parent-1",
        thread_id="thread-1",
        setup_thread=False,
        requester_platform_user_id=requester,
        client=client,
        answering_agent_name="Researcher",
        reference={
            "agent": "Researcher",
            "target_backend": "openai",
            "expected_revision": revision,
            "preview_digest": "a" * 64,
            **reference,
        },
        status="pending",
        message_id="posted-1",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )
    async with sessions.begin() as session:
        session.add(card)
    return card


def _press(owned_preview, card_id, *, action="confirm", clicker="requester", admin=True):
    from unittest.mock import AsyncMock

    from daimon.core.config import TurnSettings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.switch_cards import press_switch_card

    _, agent, _, sessions, client, *_ = owned_preview
    return press_switch_card(
        deps=_DEPS,
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

    service, agent, _, _, _, trace, _, reply = owned_preview
    card = await _switch_card(owned_preview)
    trace.queue(reply())
    trace.queue(reply())
    press = await _press(owned_preview, card.id)
    assert press is not None and press.applied and press.closed, press
    assert press.text.startswith("Researcher now uses OpenAI.")
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

    service, agent, actor, _, _, trace, _, reply = owned_preview
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


def test_an_old_switch_cards_buttons_still_parse():
    """A press on a card posted before switches applied at once still finds its row."""
    import uuid

    from daimon.core.switch_cards import parse_custom_id

    card_id = uuid.uuid4()
    for action in ("confirm", "cancel"):
        assert parse_custom_id(f"bsw:{card_id}:{action}") == (card_id, action), action
    assert parse_custom_id("bsw:not-a-card:confirm") is None, "a malformed id presses nothing"


async def test_only_the_posted_message_can_press_its_switch_card(owned_preview):
    from unittest.mock import AsyncMock

    from daimon.core.config import TurnSettings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.switch_cards import press_switch_card

    _, agent, _, sessions, client, *_ = owned_preview
    card = await _switch_card(owned_preview)

    async def press(message_id: str):
        return await press_switch_card(
            deps=_DEPS,
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
    service, agent, _, _, _, trace, _, reply = owned_preview
    card = await _switch_card(owned_preview, client=True)
    trace.queue(reply())
    press = await _press(owned_preview, card.id, admin=True)
    assert press is not None and not press.applied
    assert await _card_status(owned_preview, card.id) == "refused"
    assert await service.current(agent) is None


async def test_an_old_card_with_a_stale_preview_and_accepted_losses_still_applies(owned_preview):
    """What an old card bound (its preview digest, the losses accepted) is read and ignored."""
    service, agent, _, _, _, trace, _, reply = owned_preview
    card = await _switch_card(
        owned_preview, preview_digest="0" * 64, accepted_drops=["mcp:github", "skill:notes"]
    )
    trace.queue(reply())
    trace.queue(reply())
    press = await _press(owned_preview, card.id)
    assert press is not None and press.applied, f"an old card applies: {press}"
    current = await service.current(agent)
    assert current is not None and current.backend == "openai", "the switch it named was written"
    assert await _card_status(owned_preview, card.id) == "applied", "the row is closed once"


@pytest.mark.parametrize(("everyone", "who"), [(True, "everyone"), (False, "next message")])
def test_the_switch_card_names_the_provider_and_who_it_moves(everyone, who) -> None:
    call = ToolCall(
        tool_use_id="switch",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_agent_backend_switch",
        input={
            "agent_name": "Researcher",
            "target_backend": "gemini",
            "for_everyone": everyone,
            "expected_revision": 0,
        },
    )
    card = prompt_for_tool_call(call, requester_platform_user_id="1", now=datetime.now(UTC))
    assert card.title == "Move Researcher to Gemini?", card.title
    assert who in (card.consequence or ""), card.consequence
    assert "Gemini receives" in (card.consequence or ""), "the card says who gets the data"

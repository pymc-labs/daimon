"""Mocked permission tests for synthetic Teams personas through real HTTP ingress."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import patch

import httpx
import pytest
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolUseEvent,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
)
from daimon.adapters.teams import (
    billing_panel,
    privacy_card,
    routines_card,
    setup_card,
)
from daimon.adapters.teams.channel_settings_card import CHANNEL_DIALOG
from daimon.adapters.teams.thread_handoff import VERB as HANDOFF_VERB
from daimon.adapters.teams.tool_confirmation import VERB as TOOL_CONFIRM_VERB
from daimon.core.config import SupportSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma_models import ma_agent
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME, ma_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    finish_turns,
    make_card_action,
    make_channel_activity,
    make_group_activity,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
Outcome = Literal["admit", "refuse"]


@dataclass(frozen=True)
class Case:
    """A mocked permission test's handler, activity kind and observable outcome."""

    family: str
    name: str
    kind: Literal["command", "card", "dialog_open", "dialog_submit"]
    key: str
    op: str = ""
    admin: Outcome = "admit"
    member: Outcome = "admit"

    def expected(self, persona: Persona) -> Outcome:
        if persona.name == "admin":
            return self.admin
        # Every case is a 1:1 chat, which Teams never opens across organisations.
        return "refuse" if persona.name == "external" else self.member


# A guest is answered as a member is; only the admin owns the admin team.
CASES: tuple[Case, ...] = (
    Case("command", "setup", "command", "setup"),
    Case("command", "here", "command", "here"),
    Case("command", "routines", "command", "routines"),
    Case("command", "memory", "command", "memory"),
    Case("command", "privacy", "command", "privacy"),
    Case("command", "billing", "command", "billing"),
    Case("command", "help", "command", "help"),
    Case("command", "new", "command", "new"),
    Case("command", "support_enabled", "command", "support"),
    Case("card", "routines_refresh", "card", routines_card.VERB, "refresh"),
    Case("card", "privacy_refresh", "card", privacy_card.VERB, "refresh"),
    Case("card", "billing_refresh", "card", billing_panel.VERB, "refresh"),
    Case(
        "card",
        "billing_topup",
        "card",
        billing_panel.VERB,
        "topup",
        member="refuse",
    ),
    Case(
        "card",
        "billing_lookup",
        "card",
        billing_panel.VERB,
        "lookup",
        member="refuse",
    ),
    Case("card", "setup_agents", "card", setup_card.VERB, "agents"),
    Case("dialog", "agent_create_open", "dialog_open", setup_card.CREATE_DIALOG),
    Case("dialog", "agent_create_submit_invalid", "dialog_submit", setup_card.CREATE_DIALOG),
    Case("dialog", "token_open", "dialog_open", setup_card.TOKEN_DIALOG),
    Case("dialog", "token_submit_missing_agent", "dialog_submit", setup_card.TOKEN_DIALOG),
    Case("dialog", "skill_open", "dialog_open", setup_card.SKILL_DIALOG),
    Case("dialog", "skill_submit_missing_agent", "dialog_submit", setup_card.SKILL_DIALOG),
    Case(
        "dialog",
        "routine_create_open",
        "dialog_open",
        routines_card.CREATE_DIALOG,
    ),
    Case(
        "dialog",
        "operator_open",
        "dialog_open",
        setup_card.OPERATOR_DIALOG,
        admin="admit",
        member="refuse",
    ),
    Case(
        "dialog",
        "operator_submit",
        "dialog_submit",
        setup_card.OPERATOR_DIALOG,
        "mint",
        admin="admit",
        member="refuse",
    ),
    Case("dialog", "channel_open", "dialog_open", CHANNEL_DIALOG, member="refuse"),
    Case(
        "dialog",
        "channel_rule_submit",
        "dialog_submit",
        CHANNEL_DIALOG,
        "rule",
        admin="admit",
        member="refuse",
    ),
)


def _payload(case: Case, persona: Persona) -> dict[str, object]:
    if case.kind == "command":
        payload = make_message_activity(text=case.key, aad_object_id=persona.user_id)
    elif case.kind == "card":
        payload = make_card_action(case.key, case.op, user=persona.user_id)
    elif case.kind == "dialog_open":
        payload = make_invoke("task/fetch", {"data": {"dialog_id": case.key}}, user=persona.user_id)
    else:
        payload = make_invoke(
            "task/submit",
            {"data": {"action": case.key, "op": case.op, "channel": THREAD_ID}},
            user=persona.user_id,
        )
    if persona.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    return payload


def _sent_text(fake: PersonaTeamsApiFake) -> str:
    return json.dumps([request.body for request in fake.activity_requests], ensure_ascii=False)


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.family}-{case.name}")
@pytest.mark.parametrize("persona", PERSONAS, ids=lambda persona: persona.name)
async def test_handler_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession],
    case: Case,
    persona: Persona,
) -> None:
    """Mocked permission tests observe the command reply or invoke refusal."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=persona)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    if case.name == "support_enabled":
        runtime.settings.support = SupportSettings(
            teams_escalation_channel_id="19:ops@thread.tacv2"
        )
    async with running_service(runtime, fake) as service:
        result = await post_activity(service, _payload(case, persona))
        await finish_turns(service.turns)
    evidence = json.dumps(result, ensure_ascii=False) + _sent_text(fake)
    refusals = (
        "not available for this account",
        "Only an admin can change this",  # not the skills note's "can change them"
        "Only an admin can create",
        "Only an admin can top up",
        "Only an admin can look up",
        "Only an admin can mint",
        "Only the owner",
        "Ask in a channel that uses its own agents.",
    )
    if case.expected(persona) == "refuse":
        assert any(refusal in evidence for refusal in refusals), evidence
    elif case.kind == "command":
        assert fake.activity_requests, f"{case.name} emitted no reply"
        assert "not available for this account" not in evidence, evidence
    else:
        assert result is not None, f"{case.name} emitted no invoke response"
        assert not any(refusal in evidence for refusal in refusals), evidence
    if persona.name == "admin" and case.name in {"setup", "routines", "billing"}:
        assert any(f"/teams/{ADMIN_TEAM}/members" in url for url in graph.requests)


@pytest.mark.parametrize("kind", ("personal", "group", "channel"))
@pytest.mark.parametrize("persona", PERSONAS, ids=lambda persona: persona.name)
async def test_message_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    persona: Persona,
) -> None:
    """Mocked permission tests observe a real turn or a sender refusal."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    session_id = "sesn_persona_matrix"
    router = build_turn_router(str(tenant), session_id=session_id)
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=persona)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        deployment_default=DeploymentDefault(
            agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    builders = {
        "personal": make_message_activity,
        "group": make_group_activity,
        "channel": make_channel_activity,
    }
    payload = builders[kind](text="hello daimon", aad_object_id=persona.user_id)
    if persona.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    session = ma_session(id=session_id, agent_id=AGENT_ID, environment_id=ENV_ID)
    # Managed Agents session creation is outside the Teams/Microsoft network edge.
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            await post_activity(service, payload)
            await finish_turns(service.turns)
    evidence = _sent_text(fake)
    # Teams never opens a 1:1 chat across organisations: one naming a foreign tenant is refused.
    if persona.name == "external" and kind == "personal":
        assert "not available for this account" in evidence, evidence
    else:
        assert any(
            request.body.get("text") == "Hello from the agent!"
            for request in fake.activity_requests
        ), evidence


async def test_a_guest_owner_of_the_admin_team_is_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guest who owns the admin team administers the deployment, as any owner does."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    guest = PERSONAS[2]
    graph = OwnerGraphFake(owners={guest.user_id})
    fake = PersonaTeamsApiFake(active=guest)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    activity = make_invoke(
        "task/fetch", {"data": {"dialog_id": setup_card.OPERATOR_DIALOG}}, user=guest.user_id
    )
    async with running_service(runtime, fake) as service:
        result = await post_activity(service, activity)
    assert result is not None, "the dialog opens"
    assert "Only an admin" not in json.dumps(result), result
    assert any(f"/teams/{ADMIN_TEAM}/members" in url for url in graph.requests)


@pytest.mark.parametrize("requester", PERSONAS, ids=lambda persona: persona.name)
@pytest.mark.usefixtures("instant_output_settle")
async def test_tool_confirmation_only_requester_can_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
    requester: Persona,
) -> None:
    """Mocked permission tests route a real pending card through ingress."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    session_id = "sesn_persona_approval"
    sent: list[dict[str, Any]] = []
    tool = BetaManagedAgentsAgentMCPToolUseEvent(
        id="tu_persona",
        type="agent.mcp_tool_use",
        name="create_issue",
        mcp_server_name="linear",
        input={"title": "synthetic"},
        evaluated_permission="ask",
        processed_at=datetime.now(UTC),
    )
    pause = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_pause",
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionRequiresAction(
            type="requires_action", event_ids=["tu_persona"]
        ),
        processed_at=datetime.now(UTC),
    )
    done = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_done",
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        processed_at=datetime.now(UTC),
    )
    streams = [
        sse_response([tool.model_dump(mode="json"), pause.model_dump(mode="json")]),
        sse_response([done.model_dump(mode="json")]),
    ]
    router = MARouter()
    router.add("GET", r"/v1/files$", lambda _request, _match: list_response([]))
    router.add(
        "GET",
        rf"/v1/sessions/{session_id}/events/stream",
        lambda _request, _match: streams.pop(0),
    )
    build_turn_router(str(tenant), session_id=session_id, sent_event_bodies=sent, router=router)
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=requester)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        deployment_default=DeploymentDefault(
            agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    runtime = dataclasses.replace(
        runtime,
        turn_deps=dataclasses.replace(
            runtime.turn_deps,
            tool_safety=ToolSafetyPolicy(enabled=True, effects={"linear": "write"}),
        ),
    )

    def click(user: Persona) -> dict[str, object]:
        payload = make_card_action(TOOL_CONFIRM_VERB, "approve", user=user.user_id, token=token)
        if requester.name == "external":
            payload["conversation"] = {
                "id": THREAD_ID,
                "conversationType": "channel",
                "tenantId": ENTRA_TENANT_ID,
            }
            payload["channelData"] = {
                "tenant": {"id": ENTRA_TENANT_ID},
                "channel": {"id": CHANNEL_ID},
                "team": {"id": "19:team@thread.tacv2"},
            }
            if user.name == "external":
                sender = payload["from"]
                assert isinstance(sender, dict)
                sender["tenantId"] = user.tenant_id
        return payload

    session = ma_session(id=session_id, agent_id=AGENT_ID, environment_id=ENV_ID)
    message = (
        make_channel_activity(text="hello", aad_object_id=requester.user_id)
        if requester.name == "external"
        else make_message_activity(text="hello", aad_object_id=requester.user_id)
    )
    if requester.name == "external":
        sender = message["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = requester.tenant_id
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            await post_activity(service, message)
            async with asyncio.timeout(10):
                while not (match := re.search(r'"token":\s*"([^"]+)"', _sent_text(fake))):
                    await asyncio.sleep(0.01)
            token = match.group(1)
            stranger = PERSONAS[1] if requester.name == "admin" else PERSONAS[0]
            wrong = await post_activity(service, click(stranger))
            assert "Only" in json.dumps(wrong), wrong
            assert len(sent) == 1, sent
            accepted = await post_activity(service, click(requester))
            assert "Approved" in json.dumps(accepted), accepted
            await finish_turns(service.turns)
    assert any(
        event.get("type") == "user.tool_confirmation" and event.get("result") == "allow"
        for body in sent
        for event in body["events"]
    ), sent


@pytest.mark.parametrize("persona", PERSONAS, ids=lambda persona: persona.name)
async def test_thread_handoff_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], persona: Persona
) -> None:
    """Mocked permission tests check the stored handoff after a Teams click."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant, channel_id=CHANNEL_ID),
            tenant_id=tenant,
            agent_name="research-bot",
        )
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant))
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=persona)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    activity = make_card_action(
        HANDOFF_VERB, "hand_over", user=persona.user_id, agent="ag_research"
    )
    activity["conversation"] = {
        "id": THREAD_ID,
        "conversationType": "channel",
        "tenantId": ENTRA_TENANT_ID,
    }
    activity["channelData"] = {
        "tenant": {"id": ENTRA_TENANT_ID},
        "channel": {"id": CHANNEL_ID},
        "team": {"id": "19:team@thread.tacv2"},
    }
    if persona.name == "external":
        sender = activity["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    async with running_service(runtime, fake) as service:
        answer = await post_activity(service, activity)
    async with db_session_factory() as session:
        route = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant, channel_id=CHANNEL_ID, thread_id=THREAD_ID, platform="teams"
            ),
            default=runtime.deployment_default,
        )
    assert route.responder_ma_agent_id == "ag_research", answer
    assert "handed this conversation over" in json.dumps(answer)


@pytest.mark.parametrize("persona", PERSONAS, ids=lambda persona: persona.name)
async def test_routine_create_submit_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], persona: Persona
) -> None:
    """Mocked permission tests submit a complete routine form through ingress."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    router = MARouter()
    agent = ma_agent(tenant_id=tenant, name="daimon")
    router.add_agent(agent)
    router.add(
        "GET",
        r"/v1/agents$",
        lambda _request, _match: list_response([agent.model_dump(mode="json")]),
    )
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=persona)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    activity = make_invoke(
        "task/submit",
        {
            "data": {
                "action": routines_card.CREATE_DIALOG,
                "agent": "daimon",
                "cron": "0 9 * * *",
                "timezone": "UTC",
                "message": "standup",
            }
        },
        user=persona.user_id,
    )
    if persona.name == "external":
        sender = activity["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    async with running_service(runtime, fake) as service:
        answer = await post_activity(service, activity)
    evidence = json.dumps(answer)
    if persona.name == "external":
        assert "not available for this account" in evidence, "a 1:1 across organisations"
    else:
        # As in chat, a member may schedule the agent this chat answers with.
        assert "Routine created for daimon" in evidence, answer

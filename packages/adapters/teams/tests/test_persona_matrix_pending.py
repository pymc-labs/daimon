"""Mocked permission tests for pending Teams provider and wizard controls."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.teams.provider_actions import VERB as PROVIDER_VERB
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.wizard import VERB as WIZARD_VERB
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.wizard_session import create_wizard_session, get_wizard_session
from daimon.core.turn.provider_actions import (
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionContext,
    ProviderActionPrompt,
    ProviderActionRequester,
)
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from mux.contracts.events import RequiredAction
from mux.contracts.ids import ResourceRef, Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    THREAD_ID,
    build_teams_runtime,
    make_card_action,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SPEC = WizardSpec(
    prompt="Order form",
    steps=[
        Step(
            key="color",
            question="Pick a color",
            kind=StepKind.CHOICE,
            options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
        )
    ],
)


def _click(
    verb: str, op: str, token_key: str, token: str, user: Persona, *, channel: bool
) -> dict[str, object]:
    payload = make_card_action(verb, op, user=user.user_id, **{token_key: token})
    if channel:
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


def _runtime(
    db: async_sessionmaker[AsyncSession], requester: Persona, graph: OwnerGraphFake
) -> TeamsRuntime:
    settings = build_teams_runtime(db).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    return build_teams_runtime(
        db,
        teams=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )


@pytest.mark.parametrize("requester", PERSONAS, ids=lambda persona: persona.name)
async def test_wizard_requester_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], requester: Persona
) -> None:
    """Mocked permission tests retain a wizard choice only from its requester."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    channel = requester.name == "external"
    conversation = THREAD_ID if channel else CONVERSATION_ID
    short_id = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=requester.user_id
        )
        await create_wizard_session(
            session,
            short_id=short_id,
            tenant_id=TENANT,
            account_id=principal.account_id,
            requester_platform_user_id=requester.user_id,
            channel_id=conversation,
            message_id="m-7",
            spec=SPEC.model_dump(mode="json"),
            answers={},
            current_step=0,
            status="open",
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=requester)
    runtime = _runtime(db_session_factory, requester, graph)
    stranger = PERSONAS[1] if requester.name == "admin" else PERSONAS[0]
    async with running_service(runtime, fake) as service:
        wrong = await post_activity(
            service,
            _click(WIZARD_VERB, "s0_c0", "wz", short_id, stranger, channel=channel),
        )
        right = await post_activity(
            service,
            _click(WIZARD_VERB, "s0_c0", "wz", short_id, requester, channel=channel),
        )
    async with db_session_factory() as session:
        row = await get_wizard_session(session, short_id=short_id)
    assert "someone else" in json.dumps(wrong).lower(), wrong
    assert row is not None and row.answers == {"color": ["red"]}, right
    assert "Pick a color" in json.dumps(right) or "Order form" in json.dumps(right)


def _provider_prompt(requester: Persona, conversation: str) -> ProviderActionPrompt:
    action = RequiredAction(
        id="private-action-id",
        kind="native",
        native_type="computer_use_approval_request",
        payload={"request": {"type": "browser_origin_access", "origin": "https://example.org"}},
    )
    scope = Scope(
        tenant_id="tenant", account_id="account", principal_id="host", authorization_id="turn"
    )
    session = ResourceRef(
        id="session",
        kind="session",
        provider="openai",
        account_scope_id="project",
        tenant_id="tenant",
        account_id="account",
    )
    context = ProviderActionContext(
        scope,
        session,
        "root",
        ProviderActionRequester("teams", conversation, requester.user_id),
        datetime.now(UTC) + timedelta(minutes=10),
    )

    def response(word: str) -> NativeActionResponse:
        return NativeActionResponse.from_payload({"choice": word})

    surface = BrowserOriginAccess(
        "https://example.org", response("approve"), response("deny"), response("cancel")
    )
    return ProviderActionPrompt(action, context, surface)


@pytest.mark.parametrize("requester", PERSONAS, ids=lambda persona: persona.name)
async def test_provider_action_requester_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], requester: Persona
) -> None:
    """Mocked permission tests preserve a pending provider action for its requester."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    channel = requester.name == "external"
    conversation = THREAD_ID if channel else CONVERSATION_ID
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=requester)
    runtime = _runtime(db_session_factory, requester, graph)
    prompt = _provider_prompt(requester, conversation)
    stranger = PERSONAS[1] if requester.name == "admin" else PERSONAS[0]
    async with running_service(runtime, fake) as service:
        hook = service.turns.provider_actions.hook(
            conversation_id=conversation, service_url=SERVICE_URL, thread_id=conversation
        )
        waiting = asyncio.ensure_future(hook(prompt))
        async with asyncio.timeout(5):
            while not service.turns.provider_actions.pending:
                await asyncio.sleep(0)
            while next(iter(service.turns.provider_actions.pending.values())).message_id is None:
                await asyncio.sleep(0)
        token = next(iter(service.turns.provider_actions.pending))
        wrong = await post_activity(
            service,
            _click(PROVIDER_VERB, "0", "token", token, stranger, channel=channel),
        )
        assert not waiting.done(), wrong
        right = await post_activity(
            service,
            _click(PROVIDER_VERB, "0", "token", token, requester, channel=channel),
        )
        result = await asyncio.wait_for(waiting, timeout=5)
    assert "Only" in json.dumps(wrong) or "requester" in json.dumps(wrong), wrong
    assert "Response recorded." in json.dumps(right), right
    assert isinstance(prompt.surface, BrowserOriginAccess)
    assert result == prompt.surface.approve

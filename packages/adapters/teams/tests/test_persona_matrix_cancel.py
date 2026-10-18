"""Mocked permission tests for a running Teams turn's Cancel card."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.adapters.teams import card
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.testing import build_fake_anthropic, ma_session
from daimon.testing.ma import MARouter, sse_response
from daimon.testing.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    finish_turns,
    make_channel_activity,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SESSION_ID = "sesn_persona_cancel"
STREAM = rf"/v1/sessions/{SESSION_ID}/events/stream"


def _click(user: Persona, action: dict[str, object], *, channel: bool) -> dict[str, object]:
    payload = make_invoke(
        "adaptiveCard/action", {"action": action, "trigger": "manual"}, user=user.user_id
    )
    payload["replyToId"] = "m-1"
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


@pytest.mark.parametrize("requester", PERSONAS, ids=lambda persona: persona.name)
async def test_cancel_requester_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], requester: Persona
) -> None:
    """Mocked permission tests interrupt only the active turn's author."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    held = asyncio.Event()
    release = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        held.set()
        await release.wait()
        yield sse_response([]).content

    first = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())
    idle = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_idle_persona",
        type="session.status_idle",
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")
    streams = [first, sse_response([idle])]
    sent: list[dict[str, Any]] = []
    router = MARouter()
    router.add("GET", STREAM, lambda _request, _match: streams.pop(0))
    build_turn_router(str(TENANT), session_id=SESSION_ID, sent_event_bodies=sent, router=router)
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
    channel = requester.name == "external"
    message = (
        make_channel_activity(text="hello", aad_object_id=requester.user_id)
        if channel
        else make_message_activity(text="hello", aad_object_id=requester.user_id)
    )
    if channel:
        sender = message["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = requester.tenant_id
    stranger = PERSONAS[1] if requester.name == "admin" else PERSONAS[0]
    session = ma_session(id=SESSION_ID, agent_id=AGENT_ID, environment_id=ENV_ID)
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            await post_activity(service, message)
            await asyncio.wait_for(held.wait(), timeout=10)
            [status] = fake.activity_requests
            attachments = status.body.get("attachments")
            assert isinstance(attachments, list)
            content = cast(dict[str, Any], cast(list[dict[str, Any]], attachments)[0]["content"])
            action = next(
                a
                for block in content["body"]
                if block["type"] == "ActionSet"
                for a in block["actions"]
            )
            assert action["verb"] == card.CANCEL_VERB
            value: dict[str, object] = {
                "type": "Action.Execute",
                "verb": action["verb"],
                "data": action["data"],
            }
            wrong = await post_activity(service, _click(stranger, value, channel=channel))
            assert "Only" in json.dumps(wrong) or "author" in json.dumps(wrong), wrong
            assert len(sent) == 1, "a stranger sends no interrupt to Managed Agents"
            accepted = await post_activity(service, _click(requester, value, channel=channel))
            await finish_turns(service.turns)
    assert "Cancelling" in json.dumps(accepted), accepted
    assert [event["type"] for batch in sent for event in batch["events"]] == [
        "user.message",
        "user.interrupt",
    ]
    assert not release.is_set()
    assert streams == []

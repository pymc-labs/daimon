"""Mocked permission tests for votes on an answer posted through Teams ingress."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal
from unittest.mock import patch

import httpx
import pytest
from daimon.adapters.teams import card
from daimon.adapters.teams.feedback import FEEDBACK_DIALOG
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.testing import build_fake_anthropic, ma_session
from daimon.testing.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    ENTRA_TENANT_ID,
    build_teams_runtime,
    finish_turns,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SESSION = "sesn_persona_feedback"
Route = Literal["up", "down", "submit", "fetch", "builtin"]


def _vote(route: Route, actor: Persona, message_id: str) -> dict[str, object]:
    if route in {"up", "down"}:
        dialog = card.VOTE_UP_DIALOG if route == "up" else card.VOTE_DOWN_DIALOG
        payload = make_invoke("task/fetch", {"data": {"dialog_id": dialog}}, user=actor.user_id)
    elif route == "fetch":
        payload = make_invoke(
            "message/fetchTask",
            {"data": {"actionName": "feedback", "actionValue": {"reaction": "dislike"}}},
            user=actor.user_id,
        )
    elif route == "submit":
        payload = make_invoke(
            "task/submit",
            {
                "data": {
                    "action": FEEDBACK_DIALOG,
                    "message": message_id,
                    "text": "synthetic feedback",
                }
            },
            user=actor.user_id,
        )
    else:
        payload = make_invoke(
            "message/submitAction",
            {
                "actionName": "feedback",
                "actionValue": {
                    "reaction": "dislike",
                    "feedback": json.dumps({"feedbackText": "synthetic feedback"}),
                },
            },
            user=actor.user_id,
        )
    payload["replyToId"] = message_id
    if actor.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = actor.tenant_id
    return payload


@pytest.mark.parametrize("route", ("up", "down", "submit", "fetch", "builtin"))
@pytest.mark.parametrize("actor", PERSONAS, ids=lambda persona: persona.name)
async def test_posted_answer_feedback_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], actor: Persona, route: Route
) -> None:
    """Mocked permission tests vote, fetch, submit and use Teams' built-in route."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=PERSONAS[0])
    router = build_turn_router(str(TENANT), session_id=SESSION)
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
    session = ma_session(id=SESSION, agent_id=AGENT_ID, environment_id=ENV_ID)
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            await post_activity(
                service, make_message_activity(text="hello", aad_object_id=PERSONAS[0].user_id)
            )
            await finish_turns(service.turns)
            answer_positions = [
                i
                for i, request in enumerate(fake.activity_requests, start=1)
                if request.body.get("text") == "Hello from the agent!"
            ]
            assert answer_positions, "a real turn posts the answer before feedback"
            answer = f"m-{answer_positions[-1]}"
            result = await post_activity(service, _vote(route, actor, answer))
            await finish_turns(service.turns)
    async with db_session_factory() as db:
        rows = list(
            await db.execute(
                text("SELECT platform_user_id, vote, message_id FROM message_feedback")
            )
        )
    if actor.name in {"admin", "member", "guest"}:
        assert len(rows) == 1, result
        assert rows[0].platform_user_id == actor.user_id
        assert rows[0].message_id == answer
        assert rows[0].vote == ("up" if route == "up" else "down")
    else:
        assert rows == [], result

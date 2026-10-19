"""Mocked permission tests for Teams support and Ask a human dialogs."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Literal
from unittest.mock import patch

import httpx
import pytest
from daimon.adapters.teams import card
from daimon.core.config import SupportSettings
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
    make_card_action,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SESSION = "sesn_persona_support"
OPS = "19:ops@thread.tacv2"
Route = Literal["ask_open", "ask_submit", "support_send"]


def _invoke(route: Route, actor: Persona, answer: str, token: str) -> dict[str, object]:
    if route == "ask_open":
        payload = make_invoke(
            "task/fetch", {"data": {"dialog_id": card.ASK_HUMAN_DIALOG}}, user=actor.user_id
        )
    elif route == "ask_submit":
        payload = make_invoke(
            "task/submit",
            {"data": {"action": card.ASK_HUMAN_DIALOG, "message": answer, "note": "help me"}},
            user=actor.user_id,
        )
    else:
        payload = make_card_action("support", "send", user=actor.user_id, ask=token, note="help me")
    payload["replyToId"] = answer
    if actor.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = actor.tenant_id
    return payload


@pytest.mark.parametrize("route", ("ask_open", "ask_submit", "support_send"))
@pytest.mark.parametrize("actor", PERSONAS, ids=lambda persona: persona.name)
async def test_support_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], actor: Persona, route: Route
) -> None:
    """Mocked permission tests gate support forms and answer dialogs by actor."""
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
    runtime.settings.support = SupportSettings(teams_escalation_channel_id=OPS)
    session = ma_session(id=SESSION, agent_id=AGENT_ID, environment_id=ENV_ID)
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            if route == "support_send":
                await post_activity(service, make_message_activity(text="support"))
                await finish_turns(service.turns)
                sent = json.dumps([request.body for request in fake.activity_requests])
                match = re.search(r'"ask":\s*"([^"]+)"', sent)
                assert match is not None, sent
                answer = "m-7"
                token = match.group(1)
            else:
                await post_activity(service, make_message_activity(text="hello"))
                await finish_turns(service.turns)
                answer_positions = [
                    i
                    for i, request in enumerate(fake.activity_requests, start=1)
                    if request.body.get("text") == "Hello from the agent!"
                ]
                assert answer_positions, [request.body for request in fake.activity_requests]
                answer = f"m-{answer_positions[-1]}"
                token = ""
            result = await post_activity(service, _invoke(route, actor, answer, token))
            await finish_turns(service.turns)
    async with db_session_factory() as db:
        count = (await db.execute(text("SELECT count(*) FROM support_escalations"))).scalar_one()
    if route == "ask_open":
        assert count == 0
        if actor.name in {"admin", "member", "guest"}:
            assert result["task"]["type"] == "continue", result
        else:
            assert result["task"]["type"] == "message", result
    elif route == "support_send":
        assert count == (1 if actor.name in {"admin", "member", "guest"} else 0), result
    else:
        assert count == (1 if actor.name in {"admin", "member", "guest"} else 0), result

"""Mocked permission tests for private-channel Graph notification ingress."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.teams.channel_subscriptions import NOTIFICATIONS_PATH, ChannelSubscriptions
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import set_plain_text_mentions
from daimon.testing import build_fake_anthropic, ma_session
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    TEAM_GROUP_ID,
    build_teams_runtime,
    finish_turns,
    running_service,
    teams_settings,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake
from .test_channel_subscriptions import (
    PRIVATE,
    SECRET,
    SUBSCRIPTION,
    FakeGraph,
    _stored,  # pyright: ignore[reportPrivateUsage]
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "no_renewals")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SESSION = "sesn_persona_graph"


@pytest.fixture
def db_session_factory(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> async_sessionmaker[AsyncSession]:
    """Use separate DB connections while notification and turn tasks overlap."""
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


@pytest.fixture
def no_renewals() -> Iterator[None]:
    """Keep subscription renewal outside the notification being tested."""
    with patch.object(ChannelSubscriptions, "run", new_callable=AsyncMock):
        yield


@pytest.mark.parametrize(
    "actor", (*PERSONAS, None), ids=["admin", "member", "guest", "external", "application"]
)
async def test_graph_private_channel_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession], actor: Persona | None
) -> None:
    """Mocked permission tests pass Graph senders through the real notification route."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    async with db_session_factory.begin() as db:
        await set_plain_text_mentions(db, tenant_id=TENANT, enabled=True)
    await _stored(
        db_session_factory,
        expires_at=datetime.now(UTC) + timedelta(days=3),
        subscription_id=SUBSCRIPTION,
    )
    graph = FakeGraph()
    owner_graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=actor)
    message_id = "1700000000001"
    sender: dict[str, Any] = (
        {"application": {"id": BOT_CLIENT_ID, "displayName": "Daimon"}}
        if actor is None
        else {"user": {"id": actor.user_id, "tenantId": actor.tenant_id, "displayName": actor.name}}
    )
    graph.messages[message_id] = {
        "id": message_id,
        "messageType": "message",
        "createdDateTime": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "from": sender,
        "body": {"contentType": "html", "content": "<p>@Daimon hello</p>"},
    }

    def graph_edge(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/teams/{ADMIN_TEAM}/members"):
            return owner_graph(request)
        return graph.handler(request)

    settings = teams_settings(public_url="https://bot.example").model_copy(
        update={"admin_team_id": ADMIN_TEAM}
    )
    router = build_turn_router(str(TENANT), session_id=SESSION)
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        deployment_default=DeploymentDefault(
            agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph_edge)),
        private_channel_messages=True,
    )
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('{message_id}')"
    entry = {
        "subscriptionId": SUBSCRIPTION,
        "clientState": SECRET,
        "changeType": "created",
        "resource": resource,
    }
    session = ma_session(id=SESSION, agent_id=AGENT_ID, environment_id=ENV_ID)
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        async with running_service(runtime, fake) as service:
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(NOTIFICATIONS_PATH, json={"value": [entry]})
            await asyncio.gather(*service.turns._tasks)  # pyright: ignore[reportPrivateUsage]
            await finish_turns(service.turns)
    assert response.status_code == 202
    messages = [r.body.get("text") for r in fake.activity_requests]
    answered = "Hello from the agent!" in messages
    assert answered is (actor is not None), messages

"""The `routines` panel, driven through the real SDK route: command, buttons, create dialog.

Only the outbound Bot Framework transport and the MA agent listing are faked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.routines_panel import ADMIN_ONLY, NOT_OWNER
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, list_routines_for_tenant
from daimon.testing import build_fake_anthropic, list_response, ma_agent
from daimon.testing.asgi import asgi_lifespan
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


@pytest.fixture(autouse=True)
async def provisioned_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)


def _agents(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path == "/v1/agents":
        agent = ma_agent(tenant_id=TENANT, name="daimon")
        return list_response([agent.model_dump(mode="json")])
    return httpx.Response(404, json={"error": f"unhandled {request.url.path}"})


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake, admins: tuple[str, ...] = ()
) -> AsyncIterator[TeamsHttpService]:
    settings = teams_settings(admins=admins)
    runtime = build_teams_runtime(
        db_factory, anthropic=build_fake_anthropic(_agents), teams=settings
    )
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(fake)
    )
    async with asgi_lifespan(service.app):
        await service.turns.start()
        yield service


async def _post(service: TeamsHttpService, payload: dict[str, object]) -> Any:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json() if response.content else None


def _invoke(name: str, value: dict[str, object], *, user: str = AAD_OBJECT_ID) -> dict[str, object]:
    return {
        "type": "invoke",
        "name": name,
        "id": f"invoke-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": f"29:{user}", "aadObjectId": user},
        "recipient": {"id": BOT_ACCOUNT_ID},
        "conversation": {
            "id": CONVERSATION_ID,
            "conversationType": "personal",
            "tenantId": ENTRA_TENANT_ID,
        },
        "replyToId": "m-7",
        "value": value,
    }


def _click(
    op: str, routine: RoutineRow | None = None, *, user: str = AAD_OBJECT_ID
) -> dict[str, object]:
    data: dict[str, object] = {"action": "routines", "op": op}
    if routine is not None:
        data["routine"] = str(routine.id)
    action = {"type": "Action.Execute", "verb": "routines", "data": data}
    return _invoke("adaptiveCard/action", {"action": action, "trigger": "manual"}, user=user)


async def _routine(
    db_factory: async_sessionmaker[AsyncSession], *, owner: str, message: str
) -> RoutineRow:
    async with db_factory.begin() as session:
        return await create_routine(
            session,
            tenant_id=TENANT,
            created_by_user_id=owner,
            agent_id="agent_1",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=message,
            next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
        )


async def _load(db_factory: async_sessionmaker[AsyncSession], row: RoutineRow) -> RoutineRow | None:
    async with db_factory() as session:
        return await get_routine(session, row.id, tenant_id=TENANT)


@pytest.mark.asyncio
async def test_command_lists_every_routine_with_buttons_only_where_the_viewer_may_act(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    mine = await _routine(db_session_factory, owner=AAD_OBJECT_ID, message="my standup")
    theirs = await _routine(db_session_factory, owner=OTHER_AAD_OBJECT_ID, message="their digest")
    async with _running(db_session_factory, teams_api_fake) as service:
        await _post(service, make_message_activity(text="routines"))
        await service.turns.drain(timeout=30)

    card = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "my standup" in card and "their digest" in card, "everyone sees every routine"
    assert str(mine.id) in card, "the creator gets buttons on their own routine"
    assert str(theirs.id) not in card, "no buttons on a routine the viewer cannot manage"
    assert "New routine" not in card, "only an admin is offered create"


@pytest.mark.asyncio
async def test_pause_and_resume_update_the_row_and_replace_the_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    row = await _routine(db_session_factory, owner=AAD_OBJECT_ID, message="my standup")
    async with _running(db_session_factory, teams_api_fake) as service:
        paused_card = await _post(service, _click("pause", row))
        paused = await _load(db_session_factory, row)
        resumed_card = await _post(service, _click("resume", row))
        resumed = await _load(db_session_factory, row)

    assert paused is not None and not paused.enabled and paused.next_fire_at is None
    assert "Resume" in json.dumps(paused_card), "the replaced card offers the opposite toggle"
    assert resumed is not None and resumed.enabled and resumed.next_fire_at is not None
    assert "Pause" in json.dumps(resumed_card)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["pause", "output", "delete", "confirm_delete"])
async def test_a_non_owner_is_refused_and_the_row_is_untouched(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake, op: str
) -> None:
    row = await _routine(db_session_factory, owner=AAD_OBJECT_ID, message="my standup")
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await _post(service, _click(op, row, user=OTHER_AAD_OBJECT_ID))

    assert response["value"] == NOT_OWNER, "a stale or forwarded card grants nothing"
    assert await _load(db_session_factory, row) == row, "the routine is unchanged"


@pytest.mark.asyncio
async def test_an_admin_deletes_any_routine_after_confirming(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    row = await _routine(db_session_factory, owner=OTHER_AAD_OBJECT_ID, message="their digest")
    async with _running(db_session_factory, teams_api_fake, (AAD_OBJECT_ID,)) as service:
        confirm = await _post(service, _click("delete", row))
        still_there = await _load(db_session_factory, row)
        done = await _post(service, _click("confirm_delete", row))

    assert "can't be undone" in json.dumps(confirm), "delete asks for confirmation first"
    assert still_there is not None, "nothing is deleted before the confirmation"
    assert await _load(db_session_factory, row) is None
    assert "Routine deleted" in json.dumps(done), "the panel returns with a notice"


@pytest.mark.asyncio
async def test_create_dialog_is_admin_only(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    fetch = {"data": {"dialog_id": "routine_create", "msteams": {"type": "task/fetch"}}}
    async with _running(db_session_factory, teams_api_fake, (AAD_OBJECT_ID,)) as service:
        admin = await _post(service, _invoke("task/fetch", fetch))
        user = await _post(service, _invoke("task/fetch", fetch, user=OTHER_AAD_OBJECT_ID))

    assert admin["task"]["type"] == "continue", "an admin gets the form"
    assert '"daimon"' in json.dumps(admin), "the agent picker lists the tenant's agents"
    assert user["task"] == {"type": "message", "value": ADMIN_ONLY}


@pytest.mark.asyncio
async def test_create_rejects_a_bad_cron_then_creates_the_routine_for_the_admin(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    form = {"action": "routine_create", "agent": "daimon", "timezone": "UTC", "message": "standup"}
    async with _running(db_session_factory, teams_api_fake, (AAD_OBJECT_ID,)) as service:
        bad = await _post(service, _invoke("task/submit", {"data": form | {"cron": "61 * * * *"}}))
        good = await _post(service, _invoke("task/submit", {"data": form | {"cron": "0 9 * * *"}}))

    assert bad["task"]["type"] == "continue", "the form comes back to fix"
    assert "invalid cron expression" in json.dumps(bad)
    assert "Created routine on daimon" in good["task"]["value"]
    async with db_session_factory() as session:
        [row] = await list_routines_for_tenant(session, tenant_id=TENANT)
    assert row.created_by_user_id == AAD_OBJECT_ID, "the clicker owns the routine"
    assert row.next_fire_at is not None
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edits and edits[-1].url.endswith("/activities/m-7"), "the panel refreshes in place"

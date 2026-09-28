"""End to end: a message POSTed through the real SDK route runs a core turn.

Only the outbound Bot Framework transport and the MA turn are faked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.identity import DENIED
from daimon.core._models import ThreadSession
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.asgi import asgi_lifespan
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_channel_activity,
    make_message_activity,
    patched_admission,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> AsyncIterator[tuple[TeamsHttpService, list[dict[str, Any]]]]:
    """A started service whose MA turn answers "Hello from Teams!"."""
    service = create_teams_http_service(
        settings=teams_settings(),
        runtime=build_teams_runtime(db_factory),
        client=build_teams_client(fake),
    )
    turns: list[dict[str, Any]] = []

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        turns.append(kwargs)
        state = TurnState(content=[TextBlock(kind="text", text="Hello from Teams!")])
        await lifecycle.on_terminal_success(state)
        return state

    with (
        patched_admission(),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as run_turn,
    ):
        run_turn.side_effect = _fake_run_turn
        async with asgi_lifespan(service.app):
            # One shared test connection: let the boot sweep finish first.
            await service.turns.start()
            yield service, turns


async def _post(service: TeamsHttpService, payload: dict[str, object]) -> None:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code in (200, 201, 202), response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "conversation"),
    [(make_message_activity(), CONVERSATION_ID), (make_channel_activity(), THREAD_ID)],
)
async def test_message_runs_a_turn_and_the_answer_replaces_the_card(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    payload: dict[str, object],
    conversation: str,
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await _post(service, payload)
        await service.turns.drain(timeout=30)

    assert len(turns) == 1
    posts = [r for r in teams_api_fake.activity_requests if r.method == "POST"]
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert len(posts) == 1 and "attachments" in posts[0].body, "one status card"
    assert httpx.URL(posts[0].url).path == f"/test/v3/conversations/{conversation}/activities"
    assert edits and edits[-1].url.endswith("/activities/m-1")
    assert "Hello from Teams!" in json.dumps(edits[-1].body)


@pytest.mark.asyncio
async def test_duplicate_delivery_runs_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await _post(service, make_message_activity(activity_id="dup-1"))
        await _post(service, make_message_activity(activity_id="dup-1"))
        await service.turns.drain(timeout=30)
    assert len(turns) == 1


@pytest.mark.asyncio
async def test_unprovisioned_organisation_is_told_no_turn_runs(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await _post(service, make_message_activity())
        await service.turns.drain(timeout=30)
    assert turns == []
    assert [r.body.get("text") for r in teams_api_fake.activity_requests] == [DENIED]


@pytest.mark.asyncio
async def test_new_in_a_dm_asks_for_a_fresh_session(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await _post(service, make_message_activity())
        await service.turns.drain(timeout=30)
        service.turns.draining = False
        await _post(service, make_message_activity(text="new", activity_id="activity-2"))
        await service.turns.drain(timeout=30)

    assert len(turns) == 1, "the command runs no turn"
    assert "Starting fresh" in json.dumps(teams_api_fake.activity_requests[-1].body)
    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(ThreadSession).where(ThreadSession.thread_id == CONVERSATION_ID)
            )
        ).scalar_one()
    assert row.fresh_start_requested_at is not None

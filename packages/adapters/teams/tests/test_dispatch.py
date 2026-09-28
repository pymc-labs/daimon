"""End to end: a message POSTed through the real SDK route runs a core turn.

Only the outbound Bot Framework transport and the MA turn are faked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from daimon.core._models import ThreadSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    THREAD_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_channel_activity,
    make_message_activity,
    patched_turns,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    admins: tuple[str, ...] = (),
) -> AsyncIterator[tuple[TeamsHttpService, list[dict[str, Any]]]]:
    """A started service whose MA turn answers "Hello from Teams!"."""
    runtime = build_teams_runtime(db_factory, teams=teams_settings(admins=admins))
    with patched_turns("Hello from Teams!") as turns:
        async with running_service(runtime, fake) as service:
            yield service, turns


@pytest.mark.asyncio
@pytest.mark.usefixtures("provisioned_tenant")
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
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, payload)
        await service.turns.drain(timeout=30)

    assert len(turns) == 1
    posts = [r for r in teams_api_fake.activity_requests if r.method == "POST"]
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert len(posts) == 1 and "attachments" in posts[0].body, "one status card"
    assert httpx.URL(posts[0].url).path == f"/test/v3/conversations/{conversation}/activities"
    assert edits and edits[-1].url.endswith("/activities/m-1")
    assert "Hello from Teams!" in json.dumps(edits[-1].body)


@pytest.mark.asyncio
@pytest.mark.usefixtures("provisioned_tenant")
async def test_duplicate_delivery_runs_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity(activity_id="dup-1"))
        await post_activity(service, make_message_activity(activity_id="dup-1"))
        await service.turns.drain(timeout=30)
    assert len(turns) == 1


@pytest.mark.asyncio
async def test_unprovisioned_organisation_is_told_no_turn_runs(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity())
        await service.turns.drain(timeout=30)
    assert turns == []
    assert [r.body.get("text") for r in teams_api_fake.activity_requests] == [DENIED]


@pytest.mark.asyncio
@pytest.mark.usefixtures("provisioned_tenant")
async def test_new_in_a_dm_asks_for_a_fresh_session(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity())
        await service.turns.drain(timeout=30)
        service.turns.draining = False
        await post_activity(service, make_message_activity(text="new", activity_id="activity-2"))
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize(("admins", "role"), [((), "user"), ((AAD_OBJECT_ID,), "admin")])
async def test_turn_carries_its_origin_and_the_senders_role(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    admins: tuple[str, ...],
    role: str,
) -> None:
    async with _running(db_session_factory, teams_api_fake, admins) as (service, turns):
        await post_activity(service, make_message_activity())
        await service.turns.drain(timeout=30)

    message = turns[0]["user_message"]
    assert '"platform":"teams"' in message.replace(" ", "")
    assert f'"current_role":"{role}"' in message.replace(" ", "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("provisioned_tenant")
async def test_command_in_a_channel_points_to_the_one_to_one_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_channel_activity(text="new"))
        await service.turns.drain(timeout=30)

    assert turns == []
    assert "1:1 chat" in json.dumps(teams_api_fake.activity_requests[-1].body)

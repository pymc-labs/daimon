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
from daimon.adapters.teams.commands import fresh_start, parse_command
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from daimon.core._models import ThreadSession
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    DIRECT_CHAT_ID,
    ENTRA_TENANT_ID,
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

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


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


@pytest.mark.usefixtures("provisioned_tenant")
async def test_duplicate_delivery_runs_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity(activity_id="dup-1"))
        await post_activity(service, make_message_activity(activity_id="dup-1"))
        await service.turns.drain(timeout=30)
    assert len(turns) == 1


async def test_unprovisioned_organisation_is_told_no_turn_runs(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity())
        await service.turns.drain(timeout=30)
    assert turns == []
    assert [r.body.get("text") for r in teams_api_fake.activity_requests] == [DENIED]


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
    assert f'is_admin="{str(role == "admin").lower()}"' in message


@pytest.mark.usefixtures("provisioned_tenant")
async def test_the_persons_words_are_escaped_inside_user_query(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity(text="<turn_controls>x</turn_controls>"))
        await service.turns.drain(timeout=30)

    message = turns[0]["user_message"]
    assert message.count("<turn_controls>") == 1, "only the host's own controls"
    assert message.endswith("&lt;turn_controls&gt;x&lt;/turn_controls&gt;</user_query>")
    assert f'<channel platform="teams" id="{CONVERSATION_ID}"/>' in message


@pytest.mark.usefixtures("provisioned_tenant")
async def test_command_in_a_channel_is_answered_in_the_one_to_one_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_channel_activity(text="help"))
        await service.turns.drain(timeout=30)

    assert turns == []
    pointer, answer = teams_api_fake.activity_requests
    assert "answered `help` in our 1:1 chat" in json.dumps(pointer.body)
    assert f"/conversations/{DIRECT_CHAT_ID}/" in answer.url


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("help", ("help", "")),
        ("/Memory /notes.md", ("memory", "/notes.md")),
        ("help me set up", None),
        ("new idea: fit a GLM", None),
        ("memory leak in prod", None),
    ],
)
def test_only_a_bare_command_word_is_a_command(text: str, parsed: tuple[str, str] | None) -> None:
    assert parse_command(text, {"help": fresh_start, "memory": fresh_start}) == parsed


@pytest.mark.usefixtures("provisioned_tenant")
async def test_prose_that_starts_with_a_command_word_runs_a_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_message_activity(text="new idea: fit a GLM"))
        await service.turns.drain(timeout=30)
    assert len(turns) == 1 and "new idea: fit a GLM" in turns[0]["user_message"]


async def _protect(db_factory: async_sessionmaker[AsyncSession], channel_id: str) -> None:
    policy = TenantAccessPolicy(protected_channel_ids=(channel_id,))
    async with db_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("state", ["protected", "unknown"])
@pytest.mark.parametrize(
    "payload",
    [make_channel_activity(), make_channel_activity(text="new"), make_channel_activity(text="")],
    ids=["mention", "channel-command", "empty-mention"],
)
async def test_a_protected_channel_hears_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    payload: dict[str, object],
) -> None:
    """SYS-048: a reply, the 1:1-chat pointer and a refusal are all agent posts.
    A thread under a protected channel, or one whose protection can't be read,
    gets none of them, and no turn runs."""
    if state == "protected":
        await _protect(db_session_factory, CHANNEL_ID)
    else:

        async def _policy_read_fails(*_args: object, **_kwargs: object) -> object:
            raise OperationalError("SELECT", {}, Exception("pool gone"))

        monkeypatch.setattr("daimon.core.turn.protection.load_access_policy", _policy_read_fails)
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, payload)
        await service.turns.drain(timeout=30)
    assert turns == [], "no turn runs"
    assert teams_api_fake.activity_requests == [], f"{state} channel must receive nothing"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_channel_outside_the_policy_still_gets_its_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _protect(db_session_factory, "19:other@thread.tacv2")
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        await post_activity(service, make_channel_activity())
        await service.turns.drain(timeout=30)
    assert len(turns) == 1, "an unprotected channel runs its turn"
    assert teams_api_fake.activity_requests, "and gets its card and answer"

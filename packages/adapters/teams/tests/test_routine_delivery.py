"""Routine results posted to a Teams channel or thread, or to the creator's 1:1 chat."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from daimon.adapters.teams.routine_delivery import make_teams_routine_poster
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import DeliveryOutcome
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.uuid4()
CREATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHANNEL = "19:chan@thread.tacv2"


class _Teams:
    def __init__(self, *, roster: bool = True, post_status: int | None = None) -> None:
        self.roster, self.post_status = roster, post_status
        self.posts: list[tuple[str, str]] = []

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        return f"29:{aad_object_id}" if self.roster else None

    async def open_chat(self, member_id: str) -> str:
        return f"a:dm-{member_id}"

    async def post(self, conversation_id: str, text: str) -> None:
        if self.post_status and not conversation_id.startswith("a:"):
            request = httpx.Request("POST", "https://smba.example")
            raise httpx.HTTPStatusError(
                "refused", request=request, response=httpx.Response(self.post_status)
            )
        self.posts.append((conversation_id, text))


def _row(**overrides: object) -> RoutineRow:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "created_by_user_id": CREATOR,
        "agent_id": "ag",
        "agent_name": "daimon",
        "cron_expr": "0 9 * * 1",
        "timezone": "UTC",
        "trigger_message": "go",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": "All green.",
        "delivery_payload": "All green.",
        "destination_kind": "thread",
        "destination_id": f"{CHANNEL};messageid=17",
        "created_at": now,
        "updated_at": now,
    }
    return RoutineRow.model_validate(base | overrides)


async def _post(
    factory: async_sessionmaker[AsyncSession], teams: _Teams, row: RoutineRow
) -> DeliveryOutcome:
    poster = make_teams_routine_poster(factory, teams, tenant_id=TENANT, dm_policies={})
    return await poster(row)


async def test_a_thread_destination_gets_the_result_in_the_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _Teams()
    assert await _post(db_session_factory, teams, _row()) == DeliveryOutcome(status="delivered")
    [(where, text)] = teams.posts
    assert where == f"{CHANNEL};messageid=17" and "All green." in text


@pytest.mark.parametrize(
    ("teams", "reason"),
    [
        (_Teams(roster=False), "creator_cannot_post"),
        (_Teams(post_status=404), "destination_unavailable"),
    ],
)
async def test_an_unusable_destination_sends_the_result_to_the_creator(
    db_session_factory: async_sessionmaker[AsyncSession], teams: _Teams, reason: str
) -> None:
    outcome = await _post(db_session_factory, teams, _row())
    if teams.roster:
        assert outcome == DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")
        [(where, text)] = teams.posts
        assert where == f"a:dm-29:{CREATOR}" and "All green." in text
    else:
        assert outcome == DeliveryOutcome(status="skipped", note=reason), (
            "someone off the roster cannot be reached through it either"
        )


async def test_a_protected_channel_is_never_posted_in(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(protected_channel_ids=(CHANNEL,)),
        )
    teams = _Teams()
    poster = make_teams_routine_poster(
        db_session_factory,
        teams,
        tenant_id=tenant.id,
        dm_policies={tenant.id: DirectMessagePolicy(mode="disabled")},
    )
    outcome = await poster(_row(tenant_id=tenant.id))
    assert outcome == DeliveryOutcome(status="skipped", note="protected_channel")
    assert teams.posts == [], "the thread's channel is what the policy checks"


async def test_another_organisations_routine_is_not_posted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams = _Teams()
    outcome = await _post(db_session_factory, teams, _row(tenant_id=uuid.uuid4()))
    assert outcome.status == "skipped" and teams.posts == []

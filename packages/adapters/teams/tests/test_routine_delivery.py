"""Routine results posted to a Teams channel or thread, or to the creator's 1:1 chat."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from daimon.adapters.teams.activity_feed import ActivityFeed, chain_id
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

    async def post(self, conversation_id: str, text: str) -> str:
        if self.post_status and not conversation_id.startswith("a:"):
            request = httpx.Request("POST", "https://smba.example")
            raise httpx.HTTPStatusError(
                "refused", request=request, response=httpx.Response(self.post_status)
            )
        self.posts.append((conversation_id, text))
        return f"m-{len(self.posts)}"


class _Notices(ActivityFeed):
    """Records `notify` calls instead of sending them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def notify(self, conversation_id: str, message_id: str, **kwargs: object) -> None:  # type: ignore[override]
        self.calls.append((conversation_id, message_id, kwargs))


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
    factory: async_sessionmaker[AsyncSession],
    teams: _Teams,
    row: RoutineRow,
    feed: ActivityFeed | None = None,
) -> DeliveryOutcome:
    poster = make_teams_routine_poster(factory, teams, tenant_id=TENANT, dm_policies={}, feed=feed)
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


@pytest.mark.parametrize("channel_id", [CHANNEL, None], ids=["saved-channel", "legacy-row"])
async def test_an_isolated_channels_result_never_falls_back_to_a_dm(
    db_session_factory: async_sessionmaker[AsyncSession], channel_id: str | None
) -> None:
    """A routine into an isolated channel whose post fails is skipped, never sent to
    the creator's 1:1 chat, as on Discord and Slack; a row saved without its channel
    is placed by its thread id."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(CHANNEL,), isolated_channel_ids=(CHANNEL,)
            ),
        )
    teams = _Teams(post_status=404)
    poster = make_teams_routine_poster(
        db_session_factory, teams, tenant_id=tenant.id, dm_policies={}
    )
    outcome = await poster(_row(tenant_id=tenant.id, channel_id=channel_id))
    assert outcome == DeliveryOutcome(status="skipped", note="destination_unavailable")
    assert teams.posts == [], "the result stays inside the isolated channel"


@pytest.mark.parametrize("fallback", [False, True])
async def test_long_routine_delivers_every_word_to_thread_or_creator(db_session_factory, fallback):
    result = " ".join(f"digestword{i}" for i in range(1200)) + " FINAL-COMPLETE"
    teams = _Teams(post_status=404 if fallback else None)
    outcome = await _post(db_session_factory, teams, _row(delivery_payload=result))
    assert outcome.status == "delivered"
    assert len(teams.posts) > 1 and all(len(text) <= 4000 for _, text in teams.posts)
    assert "".join(text for _, text in teams.posts).split("\n\n")[-1] == result
    expected_place = f"a:dm-29:{CREATOR}" if fallback else f"{CHANNEL};messageid=17"
    assert all(where == expected_place for where, _ in teams.posts)


async def test_a_channel_result_notifies_its_creator_once_per_routine(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The creator's feed links to the first chunk; one chain per routine."""
    teams, feed, row = _Teams(), _Notices(), _row()
    await _post(db_session_factory, teams, row, feed)
    [(where, message_id, notice)] = feed.calls
    assert (where, message_id) == (f"{CHANNEL};messageid=17", "m-1"), "the posted result"
    assert notice["recipient_id"] == CREATOR, "the routine's creator"
    assert notice["chain"] == chain_id(row.id), "each run replaces the routine's last notice"
    assert "All green." in str(notice["preview"]), "the result previews"


async def test_a_dm_fallback_sends_no_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A result sent to the creator's 1:1 chat notifies by itself."""
    teams, feed = _Teams(post_status=404), _Notices()
    await _post(db_session_factory, teams, _row(), feed)
    assert teams.posts and feed.calls == [], "no notice for a 1:1 chat"


async def test_dm_row_is_explicitly_refused_without_channel_or_dm_io(db_session_factory):
    teams = _Teams()
    result = await _post(
        db_session_factory, teams, _row(destination_kind="dm", destination_id=CREATOR)
    )
    assert result.status == "skipped" and "not supported here" in result.note
    assert not teams.posts


async def test_generated_files_get_plain_unsupported_note(db_session_factory):
    from daimon.core.routine_files import OUTBOX_FILES, PREFIX, RoutineFile, RoutinePayload

    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report", filename="report.csv", mime_type="text/csv", size_bytes=4
                )
            ],
        ).model_dump_json()
    )
    teams = _Teams()
    outcome = await _post(
        db_session_factory, teams, _row(delivery_payload=payload, delivery_note=OUTBOX_FILES)
    )
    assert outcome.status == "delivered"
    [(where, text)] = teams.posts
    assert where == f"{CHANNEL};messageid=17"
    assert "Report" in text and "files are not supported here" in text
    assert "file_report" not in text


async def _shared_tenant_row(
    factory: async_sessionmaker[AsyncSession], **overrides: object
) -> RoutineRow:
    from daimon.core.stores.shared_channels import mark_channel_shared

    async with factory.begin() as session:
        tenant = await make_tenant(session)
        await mark_channel_shared(
            session, tenant_id=tenant.id, platform="teams", channel_id=CHANNEL, source="platform"
        )
    return _row(tenant_id=tenant.id, channel_id=CHANNEL, **overrides)


async def test_a_shared_channels_result_never_falls_back_to_a_dm(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _shared_tenant_row(db_session_factory)
    teams = _Teams(post_status=404)
    poster = make_teams_routine_poster(
        db_session_factory, teams, tenant_id=row.tenant_id, dm_policies={}
    )

    outcome = await poster(row)

    assert outcome == DeliveryOutcome(status="skipped", note="destination_unavailable")
    assert teams.posts == [], "nothing reaches the creator's 1:1 chat"


async def test_a_shared_channels_result_is_not_quoted_in_the_creators_activity_feed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _shared_tenant_row(db_session_factory)
    teams, feed = _Teams(), _Notices()
    poster = make_teams_routine_poster(
        db_session_factory, teams, tenant_id=row.tenant_id, dm_policies={}, feed=feed
    )

    outcome = await poster(row)

    assert outcome == DeliveryOutcome(status="delivered")
    assert [where for where, _ in teams.posts] == [f"{CHANNEL};messageid=17"]
    assert feed.calls == [], "the notice would carry the result outside the channel"

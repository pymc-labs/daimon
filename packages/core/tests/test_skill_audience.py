"""A scoped skill mounts only where the turn's whole, named audience holds its scope."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import skill_audience
from daimon.core.data_audience import DataAudience, user_subject
from daimon.core.skill_audience import TurnPlace, scoped_skill_audience
from daimon.core.stores import audience_facts, room_viewers, workspace_skills
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.shared_helpers import settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[DataAudience]:
    """The audiences the data store was asked about; it holds `finance` for anyone."""
    asked: list[DataAudience] = []

    async def held(_settings: object, _tenant: uuid.UUID, audience: DataAudience) -> frozenset[str]:
        asked.append(audience)
        return frozenset({"finance"})

    monkeypatch.setattr(skill_audience, "held_scopes", held)
    return asked


async def _place(db_session: AsyncSession, workspace: str) -> TurnPlace:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=workspace)
    asker = await make_platform_principal(
        db_session, platform="discord", external_id="1", tenant=tenant
    )
    for skill_id, scope in (("skill_fin", "finance"), ("skill_open", None)):
        await workspace_skills.record_version(
            db_session,
            tenant_id=tenant.id,
            skill_id=skill_id,
            name=skill_id,
            version="v1",
            digest="d",
            files={"SKILL.md": b"x"},
            scope=scope,
        )
    await db_session.commit()
    return TurnPlace(
        tenant_id=tenant.id,
        platform="discord",
        channel_id="10",
        thread_id=None,
        asker_user_id="1",
        asker_account_id=asker.account_id,
        ma_agent_id="agent_1",
        private=True,
        closed=False,
    )


async def test_a_private_turn_asks_the_store_for_the_askers_scopes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    seen: list[DataAudience],
) -> None:
    place = await _place(db_session, "4101")
    scoped, held = await scoped_skill_audience(
        db_session_factory, settings(), place, ["skill_fin", "skill_open"], now=NOW
    )
    assert (scoped, held) == ({"skill_fin": "finance"}, {"finance"})
    assert [a.people for a in seen] == [{place.asker_account_id}]


@pytest.mark.parametrize("change", ["closed", "slack_room", "unknown_viewers", "no_settings"])
async def test_an_audience_that_cannot_be_named_holds_no_scope(
    change: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    seen: list[DataAudience],
) -> None:
    place = await _place(db_session, "4102")
    if change == "closed":
        place = replace(place, closed=True)
    if change == "slack_room":
        place = replace(place, platform="slack", private=False)
    if change == "unknown_viewers":
        place = replace(place, private=False)
    found = await scoped_skill_audience(
        db_session_factory,
        None if change == "no_settings" else settings(),
        place,
        ["skill_fin"],
        now=NOW,
    )
    assert found == ({"skill_fin": "finance"}, frozenset())
    assert seen == []


async def test_a_discord_room_asks_for_every_stored_viewer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    seen: list[DataAudience],
) -> None:
    place = replace(await _place(db_session, "4103"), private=False)
    generation = await audience_facts.publish_facts(
        db_session, tenant_id=place.tenant_id, rooms={}, now=NOW, fresh_until=NOW + timedelta(1)
    )
    viewers = frozenset(user_subject("discord", "4103", u) for u in ("1", "2"))
    await room_viewers.publish_viewers(
        db_session,
        tenant_id=place.tenant_id,
        generation=generation,
        viewers={"10": viewers},
        now=NOW,
    )
    await db_session.commit()
    _, held = await scoped_skill_audience(
        db_session_factory, settings(), place, ["skill_fin"], now=NOW
    )
    assert held == {"finance"}
    (audience,) = seen
    assert audience.guests == {user_subject("discord", "4103", "2")}, "the unlinked viewer counts"


async def test_a_failed_scope_check_holds_no_scope(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    place = await _place(db_session, "4104")

    async def broken(*_args: object) -> frozenset[str]:
        raise OSError("store down")

    monkeypatch.setattr(skill_audience, "held_scopes", broken)
    found = await scoped_skill_audience(
        db_session_factory, settings(), place, ["skill_fin"], now=NOW
    )
    assert found == ({"skill_fin": "finance"}, frozenset())

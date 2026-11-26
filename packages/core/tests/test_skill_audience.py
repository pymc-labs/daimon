"""A scoped skill mounts only where the turn's whole, named audience holds its scope."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import skill_audience
from daimon.core.data_audience import DataAudience, user_subject
from daimon.core.skill_audience import TurnPlace, scoped_skill_audience
from daimon.core.stores import audience_facts, room_viewers, workspace_skills
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.turn import runtime_manifest, session_skills
from daimon.core.turn.selfhosted_deployment import mountable_skills
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.shared_helpers import settings
from mux.contracts.ids import SkillRef
from mux.profiles import PERSISTENT_WORKSPACE
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


# The audience's held scopes; None is a turn whose audience was not checked.
_HELD = [frozenset({"ops"}), None, frozenset({"finance"})]
_HELD_IDS = ["unheld", "unchecked", "held"]


@pytest.mark.parametrize("held", _HELD, ids=_HELD_IDS)
async def test_another_providers_session_copies_no_withheld_skill(
    held: frozenset[str] | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent-origin template pins the agent's capture: a scoped skill there mounts only
    for an audience holding its scope, even one the live agent no longer carries."""
    place = await _place(db_session, "4105")
    pins = (SkillRef(id="skill_fin", version="v1"), SkillRef(id="skill_open", version="v1"))
    monkeypatch.setattr(
        runtime_manifest,
        "represented_source",
        lambda *_a: SimpleNamespace(session_skills=pins, skill_names={}),
    )
    copied: list[tuple[SkillRef, ...]] = []

    async def copy(*_a: object, pins: tuple[SkillRef, ...], **_k: object) -> tuple[Any, ...]:
        copied.append(pins)
        return pins, ()

    monkeypatch.setattr(session_skills, "copy_skills", copy)
    request = SimpleNamespace(
        admission=SimpleNamespace(backend_revision_origin="agent", held_skill_scopes=held),
        source=SimpleNamespace(source=None),
        target_spec=None,
        tenant_id=place.tenant_id,
        deps=SimpleNamespace(sessionmaker=db_session_factory, anthropic=None),
    )
    await session_skills.turn_session_skills(
        cast(Any, request),
        lambda: cast(Any, None),
        profile=PERSISTENT_WORKSPACE,
        scope=cast(Any, None),
        account_scope_id="project",
    )
    kept = pins if held is not None and "finance" in held else pins[1:]
    assert copied == [kept], f"copied {copied}"


@pytest.mark.parametrize("held", _HELD, ids=_HELD_IDS)
async def test_a_self_hosted_turn_mounts_no_withheld_skill(
    held: frozenset[str] | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The record's skills and the channel's rows both drop a skill the audience lacks."""
    place = await _place(db_session, "4106")

    def row(skill_id: str) -> ChannelSkillRow:
        return ChannelSkillRow(
            tenant_id=place.tenant_id,
            platform="discord",
            channel_id="10",
            skill_id=skill_id,
            version="v1",
            name=skill_id,
            owner_agent_name=None,
            added_by_account_id=None,
            added_at=NOW,
        )

    skills, rows = await mountable_skills(
        db_session_factory,
        place.tenant_id,
        held,
        (SkillRef(id="skill_fin"), SkillRef(id="skill_open")),
        [row("skill_fin"), row("skill_open")],
    )
    kept = ["skill_fin", "skill_open"] if held is not None and "finance" in held else ["skill_open"]
    assert [s.id for s in skills] == kept, f"the record mounts {skills}"
    assert [r.skill_id for r in rows] == kept, "the channel's scoped row follows the same rule"

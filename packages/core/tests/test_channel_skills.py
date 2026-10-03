"""Channel skills: which skill a channel may add, and which a turn there runs with."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import build_subject
from daimon.core.channel_skills import (
    ChannelSkillChoice,
    applicable_skills,
    choose_channel_skill,
    may_set_channel_skills,
    turn_channel_skills,
)
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_skills import (
    add_channel_skill,
    list_channel_skills,
    remove_channel_skill,
)
from daimon.core.stores.domain import ChannelSkillRow
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _skill(skill_id: str, title: str, *, version: str | None = "v2") -> dict[str, object]:
    return {
        "id": skill_id,
        "created_at": _NOW.isoformat(),
        "display_title": title,
        "latest_version": version,
        "source": "custom",
        "type": "skill",
        "updated_at": _NOW.isoformat(),
    }


def _client(skills: Sequence[dict[str, object]]) -> AsyncAnthropic:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/v1/skills")
        return httpx.Response(200, json={"data": list(skills), "next_page": None})

    return build_fake_anthropic(handler)


def _row(
    skill_id: str, name: str, *, owner: str | None = None, version: str = "v1"
) -> ChannelSkillRow:
    return ChannelSkillRow(
        tenant_id=uuid.uuid4(),
        platform="slack",
        channel_id="C1",
        skill_id=skill_id,
        version=version,
        name=name,
        owner_agent_name=owner,
        added_by_account_id=None,
        added_at=_NOW,
    )


def test_only_a_server_admin_may_set_a_channels_skills() -> None:
    admin = build_subject(is_admin=True, platform_user_id="U1")
    channel_admin = build_subject(
        is_admin=False, platform_user_id="U2", administered_channel_ids=frozenset({"C1"})
    )
    assert may_set_channel_skills(admin, "C1")
    refused = may_set_channel_skills(channel_admin, "C1")
    assert not refused and refused.reason == "admin_required"


async def test_a_library_skill_is_chosen_by_name_at_its_latest_version(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    title = tenant_scoped_display_title(tenant_id=tenant.id, name="pdf-tools")
    client = _client([_skill("skill_lib", title)])

    choice = await choose_channel_skill(
        db_session,
        client,
        tenant_id=tenant.id,
        channel_id="C1",
        skill="pdf-tools",
        agent=ma_agent(name="shared"),
        current=(),
    )

    assert not isinstance(choice, str)
    assert (choice.skill_id, choice.version, choice.name, choice.owner_agent_name) == (
        "skill_lib",
        "v2",
        "pdf-tools",
        None,
    )


async def test_another_agents_upload_and_another_tenants_skill_are_refused(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other_agent = tenant_scoped_display_title(tenant_id=tenant.id, name="x", agent_name="other")
    foreign = tenant_scoped_display_title(tenant_id=uuid.uuid4(), name="y")
    client = _client([_skill("skill_other", other_agent), _skill("skill_foreign", foreign)])

    async def choose(skill: str) -> ChannelSkillChoice | str:
        return await choose_channel_skill(
            db_session,
            client,
            tenant_id=tenant.id,
            channel_id="C1",
            skill=skill,
            agent=ma_agent(name="shared"),
            current=(),
        )

    assert await choose("skill_other") == "not_usable"
    assert await choose("other/x") == "not_usable"
    assert await choose("skill_foreign") == "not_found"
    assert await choose("missing") == "not_found"


async def test_the_answering_agents_own_upload_is_usable_unless_isolated_elsewhere(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    title = tenant_scoped_display_title(tenant_id=tenant.id, name="x", agent_name="rx")
    client = _client([_skill("skill_rx", title)])
    agent = ma_agent(name="rx")

    async def choose() -> ChannelSkillChoice | str:
        return await choose_channel_skill(
            db_session,
            client,
            tenant_id=tenant.id,
            channel_id="C1",
            skill="rx/x",
            agent=agent,
            current=(),
        )

    usable = await choose()
    assert not isinstance(usable, str) and usable.owner_agent_name == "rx"

    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("C2",),
            isolated_channel_ids=("C2",),
            agent_channel_pins={"rx": ("C2",)},
        ),
    )
    assert await choose() == "not_usable", "an isolated channel's agent keeps its skills there"


async def test_a_clashing_mount_and_a_full_agent_are_refused(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    lib = tenant_scoped_display_title(tenant_id=tenant.id, name="report")
    own = tenant_scoped_display_title(tenant_id=tenant.id, name="report", agent_name="shared")
    client = _client([_skill("skill_lib", lib), _skill("skill_own", own)])

    clash = await choose_channel_skill(
        db_session,
        client,
        tenant_id=tenant.id,
        channel_id="C1",
        skill="report",
        agent=ma_agent(
            name="shared", skills=[{"type": "custom", "skill_id": "skill_own", "version": "1"}]
        ),
        current=(),
    )
    assert clash == "mount_clash"

    full = ma_agent(
        name="shared",
        skills=[
            {"type": "custom", "skill_id": f"skill_{i}", "version": "1"}
            for i in range(AGENT_SKILL_CAP)
        ],
    )
    too_many = await choose_channel_skill(
        db_session,
        client,
        tenant_id=tenant.id,
        channel_id="C1",
        skill="report",
        agent=full,
        current=(),
    )
    assert too_many == "too_many"


def test_a_turn_adds_only_skills_the_agent_lacks_and_may_run() -> None:
    agent = ma_agent(
        name="shared", skills=[{"type": "custom", "skill_id": "skill_held", "version": "1"}]
    )
    rows = [
        _row("skill_held", "held"),
        _row("skill_lib", "lib"),
        _row("skill_other", "other", owner="other"),
        _row("skill_own", "own", owner="shared"),
        _row("skill_dup", "lib"),
    ]

    picked = applicable_skills(rows, agent=agent, agent_names=("shared",), bodies=None)

    assert [(s.skill_id, s.version) for s in picked] == [("skill_lib", "v1"), ("skill_own", "v1")]


async def test_a_turn_reads_the_channels_rows_and_drops_a_clash_with_the_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    for skill_id, name in (("skill_a", "a"), ("skill_b", "b")):
        await add_channel_skill(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            skill_id=skill_id,
            version="v1",
            name=name,
            owner_agent_name=None,
            actor_account_id=None,
        )
    await db_session.commit()
    own_b = tenant_scoped_display_title(tenant_id=tenant.id, name="b", agent_name="shared")
    client = _client([_skill("skill_own_b", own_b)])
    agent = ma_agent(
        name="shared", skills=[{"type": "custom", "skill_id": "skill_own_b", "version": "1"}]
    )

    async def turn(channel_id: str) -> list[str]:
        picked = await turn_channel_skills(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            platform="slack",
            channel_id=channel_id,
            agent=agent,
            agent_names=("shared",),
        )
        return [skill.skill_id for skill in picked]

    assert await turn("C1") == ["skill_a"]
    assert await turn("C2") == []
    assert await remove_channel_skill(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C1", skill_id="skill_a"
    )
    assert not await remove_channel_skill(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C1", skill_id="skill_a"
    )
    rows = await list_channel_skills(db_session, tenant_id=tenant.id, platform="slack")
    assert [row.skill_id for row in rows] == ["skill_b"]

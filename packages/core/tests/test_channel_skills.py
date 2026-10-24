"""Channel skills: which skill a channel may add, and which a turn there runs with."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsAnthropicSkill
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import build_subject
from daimon.core.channel_skills import (
    PLATFORM_SKILL_RETRY_AFTER,
    ChannelSkillChoice,
    PlatformSkillMemory,
    applicable_skills,
    choose_channel_skill,
    may_set_channel_skills,
    platform_skill_refs,
    skill_room,
    turn_channel_skills,
    turn_platform_skills,
)
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.loader import load_platform_skills
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    tenant_scoped_display_title,
)
from daimon.core.ma_resolver import ResolverCache
from daimon.core.session_snapshot import MaSkill
from daimon.core.specs import SkillRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_skills import (
    add_channel_skill,
    list_channel_skills,
    remove_channel_skill,
)
from daimon.core.stores.domain import ChannelSkillRow
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"


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
        platform="slack",
        platform_skills={},
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
            platform="slack",
            platform_skills={},
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
            platform="slack",
            platform_skills={},
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
        platform="slack",
        platform_skills={},
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
        platform="slack",
        platform_skills={},
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


async def test_a_shared_listing_missing_a_skill_the_agent_now_holds_is_read_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A skill the agent gained after the listing was cached still keeps a clashing
    channel skill out: the stale listing is read again rather than trusted."""
    tenant_id = uuid.uuid4()
    own_b = tenant_scoped_display_title(tenant_id=tenant_id, name="b", agent_name="shared")
    listing: list[dict[str, object]] = []
    client = _client(listing)
    cache = ResolverCache()

    async def turn(skills: list[dict[str, str]]) -> list[str]:
        picked = await turn_channel_skills(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            platform="slack",
            channel_id="C1",
            agent=ma_agent(name="shared", skills=skills),
            agent_names=("shared",),
            rows=[_row("skill_b", "b")],
            cache=cache,
        )
        return [skill.skill_id for skill in picked]

    assert await turn([]) == ["skill_b"], "no clash while the agent holds nothing"
    listing.append(_skill("skill_own_b", own_b))
    held = [{"type": "custom", "skill_id": "skill_own_b", "version": "1"}]
    assert await turn(held) == [], "the fresh listing shows the clash, so skill_b stays out"


def _default_agent(*, managed: bool = True) -> BetaManagedAgentsAgent:
    """The seeded default agent as the provider holds it: its 11 custom skills, no built-ins."""
    return ma_agent(
        name="daimon",
        tenant_id=uuid.uuid4(),
        metadata={MA_METADATA_KEY_MANAGED: "true"} if managed else None,
        skills=[
            {"type": "custom", "skill_id": f"skill_own_{i}", "version": "1"} for i in range(11)
        ],
    )


@pytest.mark.parametrize("configured", [True, False], ids=["microsoft-on", "microsoft-off"])
@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
def test_the_default_agents_office_skills_are_added_on_teams_only(
    platform: str, configured: bool
) -> None:
    """`defaults/` limits docx, pptx, xlsx and microsoft-365 to Teams; others add nothing.

    microsoft-365 also requires Microsoft accounts to be set up.
    """
    features = frozenset({"microsoft_accounts"} if configured else ())
    limited = load_platform_skills(_DEFAULTS, features=features)
    refs = platform_skill_refs(_default_agent(), platform, limited)
    office = ["docx", "pptx", "xlsx"]
    expected = (office + ["microsoft-365"] * configured) if platform == "teams" else []
    assert [ref.skill_id for ref in refs] == expected, (
        "only a Teams turn gets the Office skills, and microsoft-365 only where it is set up"
    )
    assert (
        platform_skill_refs(_default_agent(managed=False), "teams", load_platform_skills(_DEFAULTS))
        == ()
    ), "an agent the defaults pipeline does not manage never gets them, whatever its name"


async def test_platform_skills_count_toward_the_cap_and_a_dropped_skill_is_logged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default agent's 11 skills leave room for 9 channel skills, 5 on Teams.

    The provider's limit counts the whole session `skills` list, so the Office
    built-ins and `microsoft-365` added on Teams count first; a channel that
    added 9 runs there with its first 5 and one warning.
    """
    agent = _default_agent()
    tenant_id = uuid.uuid4()
    m365 = tenant_scoped_display_title(tenant_id=tenant_id, name="microsoft-365")
    office = await turn_platform_skills(
        _client([_skill("skill_m365", m365)]),
        tenant_id=tenant_id,
        platform="teams",
        agent=agent,
        platform_skills=load_platform_skills(_DEFAULTS),
        now=_NOW,
    )
    assert [(s.type, s.skill_id, s.version) for s in office] == [
        *(("anthropic", name, "latest") for name in ("docx", "pptx", "xlsx")),
        ("custom", "skill_m365", "latest"),
    ], "the built-ins need no lookup; the seeded upload is found by title; all run at latest"
    assert skill_room(agent) == AGENT_SKILL_CAP - 11, "on Slack and Discord the room is as before"
    assert skill_room(agent, office) == AGENT_SKILL_CAP - 15, "on Teams the platform's count too"
    unconfigured = await turn_platform_skills(
        _client([_skill("skill_m365", m365)]),
        tenant_id=tenant_id,
        platform="teams",
        agent=agent,
        platform_skills=load_platform_skills(_DEFAULTS, features=frozenset()),
        now=_NOW,
    )
    assert skill_room(agent, unconfigured) == AGENT_SKILL_CAP - 14 == 6, (
        "without Microsoft accounts, Teams keeps the room microsoft-365 would take"
    )

    async def turn(count: int, platform_skills: Sequence[MaSkill] = office) -> list[str]:
        picked = await turn_channel_skills(
            db_session_factory,
            _client([]),
            tenant_id=tenant_id,
            platform="teams",
            channel_id="C1",
            agent=agent,
            agent_names=("daimon",),
            rows=[_row(f"skill_{i}", f"s{i}") for i in range(count)],
            platform_skills=platform_skills,
        )
        return [s.skill_id for s in picked]

    with capture_logs() as logs:
        first, second = await turn(9), await turn(9)
        await turn(10)
    assert first == second == [f"skill_{i}" for i in range(5)], (
        "the five added first run on every turn, the same five each time"
    )
    warnings = [log["dropped"] for log in logs if log["event"] == "channel_skills.over_cap"]
    assert warnings == [
        [f"skill_{i}" for i in range(5, 9)],
        [f"skill_{i}" for i in range(5, 10)],
    ], "the dropped skills are named once, not on every turn, and again only when they change"
    assert await turn(9, ()) == [f"skill_{i}" for i in range(9)], "without them, nine fit"


async def test_a_custom_platform_skill_keeps_its_last_id_through_a_failed_read() -> None:
    """A custom platform skill resolves to the tenant's upload by title.

    A failed read keeps the id last seen, so the session's skills don't change
    and it isn't replaced; only a complete list without it leaves it out, and
    that is logged once per process.
    """
    tenant_id = uuid.uuid4()
    agent = _default_agent()
    limited = {"daimon": (SkillRef(type="custom", skill_id="m365", platforms=("teams",)),)}
    title = tenant_scoped_display_title(tenant_id=tenant_id, name="m365")
    memory = PlatformSkillMemory()

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}})

    async def turn(client: AsyncAnthropic, memory: PlatformSkillMemory = memory) -> list[str]:
        picked = await turn_platform_skills(
            client,
            tenant_id=tenant_id,
            platform="teams",
            agent=agent,
            platform_skills=limited,
            now=_NOW,
            memory=memory,
        )
        return [s.skill_id for s in picked]

    with capture_logs() as logs:
        assert await turn(_client([_skill("skill_m365", title)])) == ["skill_m365"], (
            "the seeded upload is added by its id"
        )
        assert await turn(build_fake_anthropic(failing)) == ["skill_m365"], (
            "a failed read keeps the last id, so the session reads as current"
        )
        assert await turn(build_fake_anthropic(failing), PlatformSkillMemory()) == [], (
            "with no earlier read the skill is left out and the turn runs"
        )
        assert await turn(_client([])) == [], "a complete list without it leaves it out"
        assert await turn(_client([])) == [], "and keeps it out"
    warnings = [log for log in logs if log["event"] == "channel_skills.platform_skills_unavailable"]
    assert [log["missing"] for log in warnings] == [["m365"]], (
        "logged once per process, not per turn"
    )


async def test_platform_skills_fill_only_the_room_the_agents_own_skills_leave() -> None:
    """An agent holding 19 skills gets one Office skill on Teams rather than a failing session."""
    agent = ma_agent(
        name="daimon",
        tenant_id=uuid.uuid4(),
        metadata={MA_METADATA_KEY_MANAGED: "true"},
        skills=[
            {"type": "custom", "skill_id": f"skill_own_{i}", "version": "1"}
            for i in range(AGENT_SKILL_CAP - 1)
        ],
    )
    tenant_id = uuid.uuid4()
    with capture_logs() as logs:
        for _ in range(2):
            picked = await turn_platform_skills(
                _client([]),
                tenant_id=tenant_id,
                platform="teams",
                agent=agent,
                platform_skills=load_platform_skills(_DEFAULTS),
                now=_NOW,
            )
            assert [s.skill_id for s in picked] == ["docx"], "the first ref fills the one slot"
            assert skill_room(agent, picked) == 0, "no channel skill fits beside them"
    dropped = [log["dropped"] for log in logs if log["event"].endswith("platform_skills_over_cap")]
    assert dropped == [["pptx", "xlsx"]], "the dropped ones are named once"


async def test_a_refused_platform_skill_stays_out_until_its_deadline() -> None:
    """After the provider refuses a platform skill, new sessions leave it out for a while."""
    tenant_id = uuid.uuid4()
    memory = PlatformSkillMemory()
    assert memory.refuse(tenant_id, "docx", now=_NOW), "the first refusal is new"
    assert not memory.refuse(tenant_id, "docx", now=_NOW), "a second one is not logged again"

    async def turn(now: datetime, tenant: uuid.UUID = tenant_id) -> list[str]:
        picked = await turn_platform_skills(
            _client([]),
            tenant_id=tenant,
            platform="teams",
            agent=_default_agent(),
            platform_skills=load_platform_skills(_DEFAULTS),
            now=now,
            memory=memory,
        )
        return [s.skill_id for s in picked]

    assert await turn(_NOW) == ["pptx", "xlsx"], "the refused skill is left out"
    assert await turn(_NOW, uuid.uuid4()) == ["docx", "pptx", "xlsx"], "another tenant keeps it"
    later = _NOW + PLATFORM_SKILL_RETRY_AFTER + timedelta(seconds=1)
    assert await turn(later) == ["docx", "pptx", "xlsx"], "past the deadline it is tried again"


async def test_a_teams_channel_counts_the_default_agents_office_skills_when_adding(
    db_session: AsyncSession,
) -> None:
    """On Teams the default agent's built-ins count toward the cap and keep their names.

    The default agent holds 11 skills and Teams adds 4, so a Teams channel may
    add 5, a Slack one 9; a custom skill named like a built-in would clash with it.
    """
    tenant = await make_tenant(db_session)
    titles = {
        name: tenant_scoped_display_title(tenant_id=tenant.id, name=name)
        for name in ("report", "docx", "microsoft-365")
    }
    client = _client([_skill(f"skill_{name}", title) for name, title in titles.items()])
    current = [_row(f"skill_{i}", f"s{i}") for i in range(5)]

    async def choose(skill: str, platform: str) -> ChannelSkillChoice | str:
        return await choose_channel_skill(
            db_session,
            client,
            tenant_id=tenant.id,
            channel_id="C1",
            skill=skill,
            agent=_default_agent(),
            current=current if skill == "report" else (),
            platform=platform,
            platform_skills=load_platform_skills(_DEFAULTS),
        )

    assert await choose("report", "teams") == "too_many", "a sixth is refused on Teams"
    assert not isinstance(await choose("report", "slack"), str), "Slack has room for it"
    assert await choose("docx", "teams") == "mount_clash", "docx is the built-in's name on Teams"
    assert not isinstance(await choose("docx", "slack"), str), "Slack has no docx built-in"


def test_a_channel_skill_named_like_a_built_in_is_left_out() -> None:
    """A built-in mounts under its id, so a channel's `docx` would clash with it."""
    agent = ma_agent(
        name="shared", skills=[{"type": "anthropic", "skill_id": "pdf", "version": "latest"}]
    )
    office = (BetaManagedAgentsAnthropicSkill(type="anthropic", skill_id="docx", version="latest"),)
    rows = [_row("skill_docx", "docx"), _row("skill_pdf", "pdf"), _row("skill_ok", "ok")]

    picked = applicable_skills(
        rows, agent=agent, agent_names=("shared",), bodies=None, platform_skills=office
    )

    assert [s.skill_id for s in picked] == ["skill_ok"], (
        "names of the agent's and the platform's built-ins are taken, even unread"
    )


def test_only_a_server_admin_may_set_a_channels_skills() -> None:
    admin = build_subject(is_admin=True, platform_user_id="U1")
    member = build_subject(is_admin=False, platform_user_id="U2")
    assert may_set_channel_skills(admin, "C1")
    refused = may_set_channel_skills(member, "C1")
    assert not refused and refused.reason == "admin_required"

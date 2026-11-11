"""The chat settings panel: every registered key is reachable, and only as its viewer may."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from daimon.core import settings_panel as panel
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Subject
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.features import service
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, read_features
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import choice, listed
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
AGENTS = [panel.AgentChoice("a1", "Alpha"), panel.AgentChoice("a2", "Beta")]


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def _place(actor: FeatureActor, thread: str | None = None) -> FeaturePlace:
    return FeaturePlace.of(actor.tenant_id, "discord", "C1", thread)


def _actor(tenant_id: uuid.UUID, admin: bool) -> FeatureActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin

    return FeatureActor(tenant_id, None, "chat", "person", admin_for)


async def _agents() -> list[panel.AgentChoice]:
    return AGENTS


@pytest.fixture(autouse=True)
def native(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    check = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="person"))
    monkeypatch.setattr(service, "approval_admin", check)
    return check


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    return tenant.id


async def test_the_root_picks_a_group_and_each_group_lists_its_features(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    root = await panel.open_panel(db_session_factory, _settings(), actor, _place(actor))
    groups = [o.value for o in root.selects[0].options]
    assert groups and root.back is None
    seen: set[str] = set()
    for op in groups:
        screen = await panel.show(db_session_factory, _settings(), actor, _place(actor), op)
        assert screen is not None and screen.back is not None
        seen |= {o.value.split(":")[2] for o in screen.selects[0].options}
    place = _place(actor)
    views = await read_features(db_session_factory, _settings(), place)
    discord = {v.spec.key for v in listed(views) if v.spec.on_platform("discord")}
    assert seen == discord, "the groups list exactly the features that are set up or set"


async def test_a_feature_that_is_not_set_up_and_not_set_is_not_listed(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Unavailable here with no saved value: out of every group, as the web hides it."""
    actor = _actor(await _tenant(db_session), True)
    views = await read_features(db_session_factory, _settings(), _place(actor))
    hidden = {
        v.spec.key
        for v in views
        if v.spec.on_platform("discord") and v.resolved.unavailable is not None
    } - {v.spec.key for v in listed(views)}
    assert hidden, "the test deployment must have a feature that is not set up"
    root = await panel.open_panel(db_session_factory, _settings(), actor, _place(actor))
    shown: set[str] = set()
    for group in (o.value for o in root.selects[0].options):
        screen = await panel.show(db_session_factory, _settings(), actor, _place(actor), group)
        assert screen is not None, f"group {group} opens"
        shown |= {o.value.split(":")[2] for o in screen.selects[0].options}
    assert not hidden & shown, f"hidden features must not be offered: {hidden & shown}"


async def test_every_feature_opens_with_a_control_for_an_admin(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    for spec in FEATURES.values():
        if not spec.on_platform("discord"):
            continue
        op = panel.open_op(spec.category, spec.key)
        screen = await panel.show(
            db_session_factory, _settings(), actor, _place(actor, "T1"), op, agents=_agents
        )
        assert screen is not None and screen.back is not None, spec.key
        assert len(screen.selects) <= 4, spec.key
        for select in screen.selects:
            assert 0 < len(select.options) <= panel.SELECT_LIMIT, spec.key


async def test_a_member_is_told_who_can_help_when_nothing_is_theirs(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), False)
    op = panel.open_op("chat", "cost_footer")
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor, "T1"), op, agents=_agents
    )
    assert screen is not None and not screen.selects
    assert "Ask a server admin" in screen.lines[-1]


async def test_pressing_a_choice_changes_it_and_shows_the_feature_again(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    raw = choice("cost_footer", "channel", "hidden")
    screen = await panel.press(db_session_factory, _settings(), actor, raw, _place(actor, "T1"))
    assert screen is not None and screen.ok and screen.notice
    assert "Channel: Hidden." in screen.lines
    locked = choice("cost_footer", "channel", "shown", locked=True)
    screen = await panel.press(db_session_factory, _settings(), actor, locked, _place(actor, "T1"))
    assert screen is not None and screen.ok and "Channel: Shown, locked." in screen.lines


async def test_a_refused_change_says_so_and_a_forged_press_says_nothing(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), False)
    raw = choice("cost_footer", "channel", "hidden")
    screen = await panel.press(db_session_factory, _settings(), actor, raw, _place(actor, "T1"))
    assert screen is not None and not screen.ok and screen.notice
    for forged in ("cost_footer:channel:nope", "open:nope", "open:chat:approvals", None, 4):
        assert (
            await panel.press(db_session_factory, _settings(), actor, forged, _place(actor, "T1"))
            is None
        )


async def test_an_admin_picks_an_agent_and_sets_it_there(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    op = panel.open_op("chat", "progress_plans")
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), op, agents=_agents
    )
    assert screen is not None
    picker = screen.selects[-1]
    assert [o.label for o in picker.options] == ["Alpha", "Beta"]
    agent = await panel.show(
        db_session_factory,
        _settings(),
        actor,
        _place(actor),
        picker.options[0].value,
        agents=_agents,
    )
    assert agent is not None and len(agent.selects) == 1
    pick = agent.selects[0].options[0].value
    assert pick.endswith("@a1")
    done = await panel.press(
        db_session_factory, _settings(), actor, pick, _place(actor), agents=_agents
    )
    assert done is not None and done.ok


async def test_a_builtin_tool_is_not_offered_per_agent_in_chat(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    op = panel.open_op("model_input", "tool.bash")
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), op, agents=_agents
    )
    assert screen is not None
    assert "Change for an agent" not in [s.placeholder for s in screen.selects]


async def test_no_select_is_cut_by_the_platform_option_limit(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Discord allows 25 options a select: a group lists every shown feature on the platform within
    it, and an agent picker never offers more, so nothing is silently dropped."""
    actor = _actor(await _tenant(db_session), True)
    many = [panel.AgentChoice(f"a{i}", f"Agent {i}") for i in range(40)]

    async def agents() -> list[panel.AgentChoice]:
        return many

    for platform in ("discord", "slack", "teams"):
        place = FeaturePlace.of(actor.tenant_id, platform, "C1")
        shown = {
            v.spec.key for v in listed(await read_features(db_session_factory, _settings(), place))
        }
        for category in {spec.category for spec in FEATURES.values()}:
            screen = await panel.show(
                db_session_factory, _settings(), actor, place, panel.open_op(category)
            )
            assert screen is not None
            offered = len(screen.selects[0].options)
            wanted = sum(
                1
                for s in FEATURES.values()
                if s.category == category and s.on_platform(platform) and s.key in shown
            )
            assert offered == wanted <= panel.SELECT_LIMIT, f"{platform} {category} is cut"
    op = panel.open_op("chat", "progress_plans")
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), op, agents=agents
    )
    assert screen is not None
    assert all(len(s.options) <= panel.SELECT_LIMIT for s in screen.selects), "picker is capped"


async def _walk(
    db_session_factory: Sessions, actor: FeatureActor, op: str, agents: panel.AgentLoader | None
) -> list[panel.PanelScreen]:
    """The screens reached from `op` by pressing Next until there is none."""
    screens: list[panel.PanelScreen] = []
    while True:
        screen = await panel.show(
            db_session_factory, _settings(), actor, _place(actor), op, agents=agents
        )
        assert screen is not None, op
        screens.append(screen)
        nxt = [o for o in screen.paging if o.label == panel.NEXT]
        if not nxt:
            return screens
        op = nxt[0].value


async def test_a_long_agent_list_pages_and_every_agent_is_reachable(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """40 agents make two pages of the picker; Next and Previous lead between them, and the
    two pages together offer every agent once."""
    actor = _actor(await _tenant(db_session), True)
    many = [panel.AgentChoice(f"a{i}", f"Agent {i:02d}") for i in range(40)]

    async def agents() -> list[panel.AgentChoice]:
        return many

    first, second = await _walk(
        db_session_factory, actor, panel.open_op("chat", "progress_plans"), agents
    )
    pickers = [s.selects[-1] for s in (first, second)]
    assert [p.placeholder for p in pickers] == [
        "Change for an agent, page 1 of 2",
        "Change for an agent, page 2 of 2",
    ]
    offered = [o.label for p in pickers for o in p.options]
    assert offered == [a.name for a in many], "every agent once, in order"
    assert [o.label for o in first.paging] == [panel.NEXT]
    assert [o.label for o in second.paging] == [panel.PREVIOUS]
    back = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), second.paging[0].value, agents=agents
    )
    assert back is not None and back.selects[-1] == pickers[0], "Previous returns to page 1"
    chosen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), pickers[1].options[0].value,
        agents=agents,
    )  # fmt: skip
    assert chosen is not None and chosen.back is not None
    assert chosen.back.value == panel.open_op("chat", "progress_plans", page=0, picker=1), (
        "Back from an agent returns to its picker page"
    )


async def test_back_from_a_setting_returns_to_the_group_page_it_was_picked_from(
    db_session: AsyncSession, db_session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group's page rides in the op, also through the agent picker's own paging."""
    actor = _actor(await _tenant(db_session), True)
    monkeypatch.setattr(panel, "SELECT_LIMIT", 2)
    many = [panel.AgentChoice(f"a{i}", f"Agent {i:02d}") for i in range(5)]
    key = "completion_pings"

    async def agents() -> list[panel.AgentChoice]:
        return many

    async def show(op: str) -> panel.PanelScreen:
        found = await panel.show(
            db_session_factory, _settings(), actor, _place(actor), op, agents=agents
        )
        assert found is not None, op
        return found

    groups = await _walk(db_session_factory, actor, panel.open_op("chat"), None)
    page = next(
        n
        for n, g in enumerate(groups)
        if any(o.value.startswith(f"open:chat:{key}") for o in g.selects[0].options)
    )
    assert page > 0, "the setting sits past the first page"
    picked = await show(
        next(o for o in groups[page].selects[0].options if f":{key}" in o.value).value
    )
    assert picked.back is not None
    assert picked.back.value == panel.open_op("chat", page=page), "Back returns to its group page"
    assert (await show(picked.back.value)).lines == groups[page].lines, "and shows that page"

    next_agents = await show(next(o for o in picked.paging if o.label == panel.NEXT).value)
    assert next_agents.back is not None
    assert next_agents.back.value == panel.open_op("chat", page=page), "paging the picker keeps it"
    agent_screen = await show(next_agents.selects[-1].options[0].value)
    assert agent_screen.back is not None
    assert agent_screen.back.value == panel.open_op("chat", key, page=page, picker=1), (
        "Back from an agent returns to the setting, on its group page and picker page"
    )


async def test_an_agent_id_that_cannot_ride_in_an_op_is_not_offered(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Discord caps a select value at 100 characters and an id with a mark in it would split
    the op; such an agent is left out, and every value left fits."""
    actor = _actor(await _tenant(db_session), True)
    agents_ = [
        panel.AgentChoice("a1", "Fine"),
        panel.AgentChoice("a" * 80, "Too long"),
        panel.AgentChoice("a~1", "Tilde"),
        panel.AgentChoice("a:b@c", "Separators"),
    ]

    async def agents() -> list[panel.AgentChoice]:
        return agents_

    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor),
        panel.open_op("chat", "progress_plans"), agents=agents,
    )  # fmt: skip
    assert screen is not None
    picker = screen.selects[-1]
    assert [o.label for o in picker.options] == ["Fine"], "only the safe id is offered"
    longest = max(len(o.value) for s in screen.selects for o in s.options)
    assert longest <= 100, "every select value fits Discord's limit"


async def test_a_short_agent_list_has_no_paging(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), panel.open_op("chat", "progress_plans"),
        agents=_agents,
    )  # fmt: skip
    assert screen is not None and screen.paging == ()
    assert screen.selects[-1].placeholder == "Change for an agent"


async def test_a_long_group_pages_and_every_setting_is_reachable(
    db_session: AsyncSession, db_session_factory: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a small page, a group's settings spread over pages that together list each once."""
    actor = _actor(await _tenant(db_session), True)
    monkeypatch.setattr(panel, "SELECT_LIMIT", 2)
    whole = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), panel.open_op("chat")
    )
    assert whole is not None and len(whole.selects[0].options) == 2
    pages = await _walk(db_session_factory, actor, panel.open_op("chat"), None)
    keys = [o.value for p in pages for o in p.selects[0].options]
    shown = {
        v.spec.key
        for v in listed(await read_features(db_session_factory, _settings(), _place(actor)))
        if v.spec.category == "chat" and v.spec.on_platform("discord")
    }
    assert len(pages) > 1, "the group needs more than one page"
    assert sorted(k.split(":")[2].split("~")[0] for k in keys) == sorted(shown), "each setting once"
    assert all(len(p.selects[0].options) <= 2 for p in pages)
    assert len({tuple(p.lines) for p in pages}) == len(pages), "a page lists its own settings"


def test_a_page_is_part_of_the_op_and_a_forged_one_names_nothing() -> None:
    """`~<page>[~<picker>]` follows the group, the setting or the agent; anything else is refused."""
    assert panel.parse_open(panel.open_op("chat", page=2)) == panel.OpenOp("chat", page=2)
    assert panel.parse_open(panel.open_op("chat", "progress_plans", "a1", 3, 4)) == panel.OpenOp(
        "chat", "progress_plans", "a1", 3, 4
    )
    assert panel.parse_open(panel.open_op("chat", "progress_plans", page=0, picker=2)) == (
        panel.OpenOp("chat", "progress_plans", None, 0, 2)
    )
    assert panel.parse_open(panel.open_op("chat")) == panel.OpenOp("chat")
    forged_ops = (
        "open:chat~x", "open:chat~-1", "open:chat~", "open:chat~99999", "open~1~2",
        "open:chat~1~2", "open:chat:progress_plans~1~2~3", "open:chat:progress_plans@a~b",
        "open:chat:progress_plans@" + "a" * 41, "open:chat:progress_plans@a:b",
    )  # fmt: skip
    for forged in forged_ops:
        assert panel.parse_open(forged) is None, forged


async def test_a_stale_page_shows_the_last_one(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A page past the end of a list that has shrunk shows the last page, not an empty one."""
    actor = _actor(await _tenant(db_session), True)
    op = panel.open_op("chat", "progress_plans", page=7)
    screen = await panel.show(
        db_session_factory, _settings(), actor, _place(actor), op, agents=_agents
    )
    assert screen is not None and screen.paging == ()
    assert [o.label for o in screen.selects[-1].options] == ["Alpha", "Beta"]


async def test_a_level_overridden_above_says_what_decides(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A channel's own On under a server Off reads as overridden, not as in effect. The state
    arises when the server is turned Off after the channel was set."""
    tenant = await _tenant(db_session)
    actor = _actor(tenant, True)
    on = choice("watches", "channel", "on")
    assert (
        await panel.press(db_session_factory, _settings(), actor, on, _place(actor))
    ) is not None
    changer = service.FeatureActor(tenant, None, "web", None, actor.admin_for)
    await service.change_feature(
        db_session_factory, _settings(), changer, key="watches", tier="workspace", value="off",
        place=FeaturePlace(tenant, "discord"),
    )  # fmt: skip
    op = panel.open_op("capabilities", "watches")
    screen = await panel.show(db_session_factory, _settings(), actor, _place(actor), op)
    assert screen is not None
    assert "In effect: Off, set for the server." in screen.lines, screen.lines
    assert "Channel: On, overridden by the server." in screen.lines, screen.lines
    refused = await panel.press(db_session_factory, _settings(), actor, on, _place(actor))
    assert refused is not None and not refused.ok, "turning it on again is refused"


@pytest.mark.parametrize(
    "raw",
    ["approvals:thread:maybe", "colour:workspace:on", "a:b", "approvals:workspace:off:x", None, 3],
)
async def test_a_forged_choice_writes_and_says_nothing(
    db_session: AsyncSession, db_session_factory: Sessions, raw: object
) -> None:
    actor = _actor(await _tenant(db_session), True)
    assert (
        await panel.apply_choice(db_session_factory, _settings(), actor, raw, _place(actor, "T1"))
        is None
    ), f"{raw!r} changed or said something"


async def test_apply_choice_answers_with_the_change_or_the_refusal(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    member = _actor(await _tenant(db_session), False)
    place = _place(member, "T1")
    done = await panel.apply_choice(
        db_session_factory, _settings(), member, "participation:thread:on", place
    )
    assert done is not None and done[0], "a member may set a thread's participation"
    assert "Thread participation is now On for this thread" in done[1], done[1]
    refused = await panel.apply_choice(
        db_session_factory, _settings(), member, "participation:channel:on", place
    )
    assert refused is not None and not refused[0], "a member may not set a channel"
    assert "Ask a server admin" in refused[1], refused[1]

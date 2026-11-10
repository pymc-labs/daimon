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
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import choice
from daimon.core.scope_settings import SettingsActor, SettingsPlace
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
CHANNEL = SettingsPlace.of("discord", "C1", None)
THREAD = SettingsPlace.of("discord", "C1", "T1")
AGENTS = [panel.AgentChoice("a1", "Alpha"), panel.AgentChoice("a2", "Beta")]


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def _actor(tenant_id: uuid.UUID, admin: bool) -> SettingsActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin

    return SettingsActor(tenant_id, None, "chat", "person", admin_for)


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
    root = await panel.open_panel(db_session_factory, _settings(), actor, CHANNEL)
    groups = [o.value for o in root.selects[0].options]
    assert groups and root.back is None
    seen: set[str] = set()
    for op in groups:
        screen = await panel.show(db_session_factory, _settings(), actor, CHANNEL, op)
        assert screen is not None and screen.back is not None
        seen |= {o.value.split(":")[2] for o in screen.selects[0].options}
    discord = {k for k, s in FEATURES.items() if s.on_platform("discord")}
    assert seen == discord


async def test_every_feature_opens_with_a_control_for_an_admin(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    for spec in FEATURES.values():
        if not spec.on_platform("discord"):
            continue
        op = panel.open_op(spec.category, spec.key)
        screen = await panel.show(
            db_session_factory, _settings(), actor, THREAD, op, agents=_agents
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
    screen = await panel.show(db_session_factory, _settings(), actor, THREAD, op, agents=_agents)
    assert screen is not None and not screen.selects
    assert "Ask a server admin" in screen.lines[-1]


async def test_pressing_a_choice_changes_it_and_shows_the_feature_again(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    raw = choice("cost_footer", "channel", "hidden")
    screen = await panel.press(db_session_factory, _settings(), actor, raw, THREAD)
    assert screen is not None and screen.ok and screen.notice
    assert "Channel: Hidden." in screen.lines
    locked = choice("cost_footer", "channel", "shown", locked=True)
    screen = await panel.press(db_session_factory, _settings(), actor, locked, THREAD)
    assert screen is not None and screen.ok and "Channel: Shown, locked." in screen.lines


async def test_a_refused_change_says_so_and_a_forged_press_says_nothing(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), False)
    raw = choice("cost_footer", "channel", "hidden")
    screen = await panel.press(db_session_factory, _settings(), actor, raw, THREAD)
    assert screen is not None and not screen.ok and screen.notice
    for forged in ("cost_footer:channel:nope", "open:nope", "open:chat:approvals", None, 4):
        assert await panel.press(db_session_factory, _settings(), actor, forged, THREAD) is None


async def test_an_admin_picks_an_agent_and_sets_it_there(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    op = panel.open_op("chat", "progress_plans")
    screen = await panel.show(db_session_factory, _settings(), actor, CHANNEL, op, agents=_agents)
    assert screen is not None
    picker = screen.selects[-1]
    assert [o.label for o in picker.options] == ["Alpha", "Beta"]
    agent = await panel.show(
        db_session_factory, _settings(), actor, CHANNEL, picker.options[0].value, agents=_agents
    )
    assert agent is not None and len(agent.selects) == 1
    pick = agent.selects[0].options[0].value
    assert pick.endswith("@a1")
    done = await panel.press(db_session_factory, _settings(), actor, pick, CHANNEL, agents=_agents)
    assert done is not None and done.ok


async def test_a_builtin_tool_is_not_offered_per_agent_in_chat(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    actor = _actor(await _tenant(db_session), True)
    op = panel.open_op("model_input", "tool.bash")
    screen = await panel.show(db_session_factory, _settings(), actor, CHANNEL, op, agents=_agents)
    assert screen is not None
    assert "Change for an agent" not in [s.placeholder for s in screen.selects]


async def test_no_select_is_cut_by_the_platform_option_limit(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """Discord allows 25 options a select: a group lists every feature on the platform within
    it, and an agent picker never offers more, so nothing is silently dropped."""
    actor = _actor(await _tenant(db_session), True)
    many = [panel.AgentChoice(f"a{i}", f"Agent {i}") for i in range(40)]

    async def agents() -> list[panel.AgentChoice]:
        return many

    for platform in ("discord", "slack", "teams"):
        place = SettingsPlace.of(platform, "C1", None)
        for category in {spec.category for spec in FEATURES.values()}:
            screen = await panel.show(
                db_session_factory, _settings(), actor, place, panel.open_op(category)
            )
            assert screen is not None
            listed = len(screen.selects[0].options)
            wanted = sum(
                1 for s in FEATURES.values() if s.category == category and s.on_platform(platform)
            )
            assert listed == wanted <= panel.SELECT_LIMIT, f"{platform} {category} is cut"
    op = panel.open_op("chat", "progress_plans")
    screen = await panel.show(db_session_factory, _settings(), actor, CHANNEL, op, agents=agents)
    assert screen is not None
    assert all(len(s.options) <= panel.SELECT_LIMIT for s in screen.selects), "picker is capped"


async def test_a_level_overridden_above_says_what_decides(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    """A channel's own On under a server Off reads as overridden, not as in effect. The state
    arises when the server is turned Off after the channel was set."""
    tenant = await _tenant(db_session)
    actor = _actor(tenant, True)
    on = choice("watches", "channel", "on")
    assert (await panel.press(db_session_factory, _settings(), actor, on, CHANNEL)) is not None
    changer = service.FeatureActor(tenant, None, "web", None, actor.admin_for)
    await service.change_feature(
        db_session_factory, _settings(), changer, key="watches", tier="workspace", value="off",
        place=FeaturePlace(tenant, "discord"),
    )  # fmt: skip
    op = panel.open_op("capabilities", "watches")
    screen = await panel.show(db_session_factory, _settings(), actor, CHANNEL, op)
    assert screen is not None
    assert "In effect: Off, set for the server." in screen.lines, screen.lines
    assert "Channel: On, overridden by the server." in screen.lines, screen.lines
    refused = await panel.press(db_session_factory, _settings(), actor, on, CHANNEL)
    assert refused is not None and not refused.ok, "turning it on again is refused"

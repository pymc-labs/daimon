"""The Settings group's registry, readers and writer: one rule per key for every surface."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core import scope_settings
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.approval_gate import ApprovalGate
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelChangeRefused, Expected
from daimon.core.channel_switches import SwitchChange
from daimon.core.config import Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.scope_cascade import CascadeMode, CascadeModes, ResolvedCascade
from daimon.core.scope_settings import (
    OVERRIDES_OFF,
    SETTING_KEYS,
    SPECS,
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    change_refusal,
    change_setting,
    read_settings,
    setting_refusal,
)
from daimon.core.tool_safety import ToolSafetyPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.uuid4()
THREAD = SettingsPlace.of("discord", "C1", "T1")


class _Factory:
    """A sessionmaker stand-in: every session is a mock, and nothing reaches a database."""

    def __init__(self) -> None:
        self.session = MagicMock()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[Any]:
        yield self.session

    def __call__(self) -> Any:
        return self._open()

    def begin(self) -> Any:
        return self._open()


def _factory() -> async_sessionmaker[AsyncSession]:
    return _Factory()  # type: ignore[return-value]


def _settings(*, overrides: bool = True) -> Settings:
    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=overrides, allow_gate_overrides=overrides)
    settings.discord = object()
    settings.teams = None
    settings.thread_participation.mode = CascadeMode.OFF
    settings.progress_plans.mode = CascadeMode.OFF
    return settings


def _actor(
    *, admin: bool = False, delegated: frozenset[DelegatedAction] = frozenset()
) -> SettingsActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin or (action is not None and action in delegated)

    return SettingsActor(
        tenant_id=TENANT,
        account_id=None,
        surface="chat",
        native_user_id="person",
        admin_for=admin_for,
    )


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    fakes = {
        "approval_admin": AsyncMock(return_value=Subject(is_admin=True, platform_user_id="person")),
        "set_approval_gate": AsyncMock(
            return_value=ApprovalGate("off", "server", "person", TENANT, "inherit")
        ),
        "set_switch": AsyncMock(
            return_value=SwitchChange(
                None, CascadeMode.ON, ResolvedCascade(CascadeMode.ON, "thread")
            )
        ),
        "get_cost_footer": AsyncMock(return_value=True),
        "set_cost_footer": AsyncMock(),
        "record_change": AsyncMock(),
        "load_access_policy": AsyncMock(return_value=TenantAccessPolicy()),
        "get_switch_modes": AsyncMock(return_value=CascadeModes(None, None, None)),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(scope_settings, name, fake)
    return fakes


def test_registry_names_four_keys_with_their_values_and_levels() -> None:
    assert SETTING_KEYS == ("approvals", "participation", "progress_plans", "cost_footer")
    assert dict(SPECS["approvals"].labels) == {
        "on": "Ask",
        "off": "Allow writes",
        "inherit": "Inherit",
    }
    assert SPECS["participation"].values == ("on", "off", "disabled", "inherit")
    assert SPECS["participation"].platforms == frozenset({"discord", "teams"})
    assert SPECS["progress_plans"].platforms == frozenset({"discord", "slack", "teams"})
    assert SPECS["cost_footer"].levels == ("server",)


def test_place_levels_and_thread_keys() -> None:
    assert SettingsPlace.of("discord", "C1", "C1").levels() == ("server", "channel")
    assert THREAD.levels() == ("server", "channel", "thread")
    slack = SettingsPlace.of("slack", "C1", "C1:1.2")
    assert slack.thread_id == "1.2"
    assert slack.switch_thread_id == "C1:1.2"
    assert slack.approvals_target("thread") == "C1:1.2"
    assert THREAD.approvals_target("thread") == "T1"
    assert THREAD.approvals_target("server") is None


@pytest.mark.parametrize(
    ("key", "level", "value", "place", "reason"),
    [
        ("colour", "server", "on", THREAD, "unknown_key"),
        ("approvals", "server", "disabled", THREAD, "bad_value"),
        ("approvals", "planet", "on", THREAD, "bad_level"),
        ("participation", "server", "on", SettingsPlace.of("slack", "C1", None), "wrong_platform"),
        ("cost_footer", "channel", "hidden", THREAD, "server_only"),
        ("cost_footer", "thread", "hidden", THREAD, "server_only"),
        ("approvals", "thread", "on", SettingsPlace.of("discord", "C1", None), "no_such_level"),
    ],
)
def test_refusals_before_any_read(
    key: str, level: str, value: str, place: SettingsPlace, reason: str
) -> None:
    refusal = setting_refusal(key, level, value, place=place)
    assert refusal is not None and refusal.reason == reason


def test_cost_footer_at_channel_level_is_refused_in_a_plain_sentence() -> None:
    refusal = setting_refusal("cost_footer", "channel", "hidden", place=THREAD)
    assert str(refusal) == (
        "Cost footer is set for the whole server only. Change it at the server level."
    )


async def test_read_reports_every_key_with_its_own_resolver(stores: dict[str, AsyncMock]) -> None:
    stores["load_access_policy"].return_value = TenantAccessPolicy(
        approvals="on",
        approvals_set_by="a",
        channel_rules={"T1": ChannelRule(approvals="off", approvals_set_by="a")},
    )
    stores["get_switch_modes"].return_value = CascadeModes(
        CascadeMode.DISABLED, CascadeMode.ON, None
    )
    views = await read_settings(
        _factory(),
        tenant_id=TENANT,
        place=THREAD,
        defaults=SettingsDefaults.from_settings(_settings(), "discord"),
    )
    by_key = {view.key: view for view in views}
    assert by_key["approvals"].effective == "off", "the most specific level wins"
    assert by_key["approvals"].decided_by == "thread"
    assert by_key["approvals"].own("server") == "on"
    assert by_key["participation"].effective == "disabled", "disabled is final below it"
    assert by_key["participation"].decided_by == "server"
    assert by_key["cost_footer"].effective == "shown"
    assert [item.level for item in by_key["cost_footer"].levels] == ["server"]


async def test_approvals_read_says_saved_but_ignored_when_overrides_are_off(
    stores: dict[str, AsyncMock],
) -> None:
    (view,) = await read_settings(
        _factory(),
        tenant_id=TENANT,
        place=THREAD,
        defaults=SettingsDefaults.from_settings(_settings(overrides=False), "discord"),
        keys=("approvals",),
    )
    assert not view.active
    assert view.effective_label == "Ask"
    assert view.note.endswith(OVERRIDES_OFF)


async def test_participation_is_disabled_where_its_platform_is_not_set_up() -> None:
    defaults = SettingsDefaults.from_settings(_settings(), "teams")
    assert defaults.participation is CascadeMode.DISABLED


async def test_approvals_need_a_live_native_admin(stores: dict[str, AsyncMock]) -> None:
    stores["approval_admin"].return_value = Subject(platform_user_id="person")
    with pytest.raises(ChannelChangeRefused, match="Only a server admin can change approvals"):
        await change_setting(
            _factory(),
            _settings(),
            _actor(admin=True),
            key="approvals",
            level="server",
            value="off",
            place=THREAD,
        )
    stores["set_approval_gate"].assert_not_awaited()


async def test_a_daimon_granted_admin_sets_the_footer_but_not_approvals(
    stores: dict[str, AsyncMock],
) -> None:
    granted = _actor(delegated=frozenset(DelegatedAction))
    stores["approval_admin"].return_value = Subject(platform_user_id="person")
    with pytest.raises(ChannelChangeRefused):
        await change_setting(
            _factory(),
            _settings(),
            granted,
            key="approvals",
            level="server",
            value="off",
            place=THREAD,
        )
    change = await change_setting(
        _factory(),
        _settings(),
        granted,
        key="cost_footer",
        level="server",
        value="hidden",
        place=THREAD,
    )
    assert change.before == "shown" and change.after == "hidden"
    stores["set_cost_footer"].assert_awaited_once()
    recorded = stores["record_change"].await_args.kwargs
    assert recorded["setting"] == "cost_footer" and recorded["channel_id"] == "*"


async def test_footer_needs_the_agent_settings_admin(stores: dict[str, AsyncMock]) -> None:
    with pytest.raises(ChannelChangeRefused, match="Ask a server admin"):
        await change_setting(
            _factory(),
            _settings(),
            _actor(),
            key="cost_footer",
            level="server",
            value="hidden",
            place=THREAD,
        )
    stores["set_cost_footer"].assert_not_awaited()


async def test_footer_stale_expected_writes_nothing(stores: dict[str, AsyncMock]) -> None:
    with pytest.raises(StaleChannelValue):
        await change_setting(
            _factory(),
            _settings(),
            _actor(admin=True),
            key="cost_footer",
            level="server",
            value="hidden",
            place=THREAD,
            expected=Expected("hidden"),
        )
    stores["set_cost_footer"].assert_not_awaited()


async def test_approvals_change_is_written_with_its_record(stores: dict[str, AsyncMock]) -> None:
    change = await change_setting(
        _factory(),
        _settings(),
        _actor(),
        key="approvals",
        level="thread",
        value="off",
        place=THREAD,
    )
    call = stores["set_approval_gate"].await_args.kwargs
    assert call["target"] == "T1"
    assert call["subject"].is_admin
    assert call["record"].channel_id == "C1" and call["record"].thread_id == "T1"
    assert call["record"].surface == "chat"
    assert change.before == "inherit"
    stores["approval_admin"].assert_awaited_once()


async def test_member_may_change_a_thread_switch_but_not_the_channel(
    stores: dict[str, AsyncMock],
) -> None:
    await change_setting(
        _factory(),
        _settings(),
        _actor(),
        key="participation",
        level="thread",
        value="on",
        place=THREAD,
    )
    target = stores["set_switch"].await_args.kwargs["target"]
    assert target.scope_id == "T1" and target.channel_id == "C1"
    with pytest.raises(ChannelChangeRefused, match="Only a server admin"):
        await change_setting(
            _factory(),
            _settings(),
            _actor(),
            key="participation",
            level="channel",
            value="on",
            place=THREAD,
        )
    assert stores["set_switch"].await_count == 1


async def test_disabled_is_not_a_thread_value(stores: dict[str, AsyncMock]) -> None:
    with pytest.raises(ChannelChangeRefused):
        await change_setting(
            _factory(),
            _settings(),
            _actor(admin=True),
            key="progress_plans",
            level="thread",
            value="disabled",
            place=THREAD,
        )
    stores["set_switch"].assert_not_awaited()


async def test_agent_keys_and_unlinked_people_never_change_approvals(
    stores: dict[str, AsyncMock],
) -> None:
    async def yes(action: DelegatedAction | None) -> bool:
        return True

    for actor in (
        SettingsActor(TENANT, None, "chat", "person", yes, via_agent_key=True),
        SettingsActor(TENANT, None, "web", None, yes),
    ):
        assert (
            await change_refusal(
                _factory(), _settings(), actor, key="approvals", level="server", platform="discord"
            )
            is not None
        )
    stores["approval_admin"].assert_not_awaited()

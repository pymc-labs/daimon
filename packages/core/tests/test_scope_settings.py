"""The Settings group: registry, reader, writer and panel, one rule per key for every surface."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core import scope_settings as core
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.approval_gate import ApprovalGate
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelChangeRefused, Expected
from daimon.core.channel_switches import SwitchChange, switch_refusal
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.errors import StaleChannelValue
from daimon.core.scope_cascade import CascadeMode, CascadeModes, ResolvedCascade
from daimon.core.scope_settings import (
    OVERRIDES_OFF,
    SPECS,
    LevelValue,
    SettingsActor,
    SettingsPlace,
    SettingView,
    apply_choice,
    build_settings_panel,
    change_setting,
    decode_choice,
    moved_to_settings,
    read_settings,
    setting_refusal,
    switch_default,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy

TENANT = uuid.uuid4()
THREAD = SettingsPlace.of("discord", "C1", "T1")


class _Factory:
    """A sessionmaker stand-in: every session is a mock, and nothing reaches a database."""

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[Any]:
        yield MagicMock()

    def __call__(self) -> Any:
        return self._open()

    begin = __call__


def _settings(*, overrides: bool = True) -> Any:
    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=overrides, allow_gate_overrides=overrides)
    settings.teams = None
    settings.thread_participation.mode = settings.progress_plans.mode = CascadeMode.OFF
    return settings


def _actor(kind: str = "member") -> SettingsActor:
    """``member``, ``admin``, ``granted`` (every delegated action), ``agent_key``, ``web_only``."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return kind in ("admin", "agent_key", "web_only") or (
            kind == "granted" and action is not None
        )

    native = None if kind == "web_only" else "person"
    return SettingsActor(TENANT, None, "chat", native, admin_for, via_agent_key=kind == "agent_key")


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    on = ResolvedCascade(CascadeMode.ON, "thread")

    async def set_switch(_sm: Any, actor: Any, switch: Any, **kw: Any) -> SwitchChange:
        """The real switch rules (admin levels, the thread lock); no database."""
        rule = switch_refusal(
            switch,
            actor,
            platform=kw["platform"],
            deployment=kw["deployment"],
            scope=kw["target"].scope,
            mode=kw["mode"],
        )
        if rule is not None:
            raise rule
        return SwitchChange(None, CascadeMode.ON, on)

    fakes = {
        "approval_admin": AsyncMock(return_value=Subject(is_admin=True, platform_user_id="person")),
        "set_approval_gate": AsyncMock(
            return_value=ApprovalGate("off", "server", "person", TENANT, "inherit")
        ),
        "set_switch": AsyncMock(side_effect=set_switch),
        "get_cost_footer": AsyncMock(return_value=True),
        "set_cost_footer": AsyncMock(),
        "record_change": AsyncMock(),
        "load_access_policy": AsyncMock(return_value=TenantAccessPolicy()),
        "get_switch_modes": AsyncMock(return_value=CascadeModes(None, None, None)),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(core, name, fake)
    return fakes


async def _change(key: str, level: str, value: str, actor: str = "admin", **kw: Any) -> Any:
    return await change_setting(
        _Factory(),
        _settings(),
        _actor(actor),
        key=key,
        level=level,
        value=value,
        place=THREAD,
        **kw,
    )  # type: ignore[arg-type]


def test_registry_places_and_defaults() -> None:
    assert SPECS["approvals"].labels == {"on": "Ask", "off": "Allow writes", "inherit": "Inherit"}
    assert SPECS["participation"].platforms == frozenset({"discord", "teams"})
    assert SPECS["cost_footer"].levels == ("server",)
    assert SettingsPlace.of("discord", "C1", "C1").levels() == ("server", "channel")
    slack = SettingsPlace.of("slack", "C1", "C1:1.2")
    assert (slack.thread_id, slack.thread_key, slack.target("thread")) == (
        "1.2",
        "C1:1.2",
        "C1:1.2",
    )
    assert THREAD.target("thread") == "T1" and THREAD.target("server") is None
    assert switch_default(_settings(), "participation", "teams") is CascadeMode.DISABLED


@pytest.mark.parametrize(
    ("key", "level", "value", "place", "reason"),
    [
        ("colour", "server", "on", THREAD, "unknown_key"),
        ("approvals", "server", "disabled", THREAD, "bad_value"),
        ("approvals", "planet", "on", THREAD, "bad_level"),
        ("participation", "server", "on", SettingsPlace.of("slack", "C1", None), "wrong_platform"),
        ("cost_footer", "thread", "hidden", THREAD, "server_only"),
        ("approvals", "thread", "on", SettingsPlace.of("discord", "C1", None), "no_such_level"),
    ],
)
def test_refusals_before_any_read(
    key: str, level: str, value: str, place: Any, reason: str
) -> None:
    refusal = setting_refusal(key, level, value, place=place)
    assert refusal is not None and refusal.reason == reason
    if reason == "server_only":
        assert str(refusal) == (
            "Cost footer is set for the whole server only. Change it at the server level."
        )


async def test_read_uses_each_keys_resolver(stores: dict[str, AsyncMock]) -> None:
    stores["load_access_policy"].return_value = TenantAccessPolicy(
        approvals="on", approvals_set_by="a", channel_rules={"T1": ChannelRule(approvals="off")}
    )
    stores["get_switch_modes"].return_value = CascadeModes(
        CascadeMode.DISABLED, CascadeMode.ON, None
    )
    views = {
        v.key: v
        for v in await read_settings(
            _Factory(), settings=_settings(), tenant_id=TENANT, place=THREAD
        )
    }  # type: ignore[arg-type]
    assert (views["approvals"].effective, views["approvals"].decided_by) == ("off", "thread")
    assert views["approvals"].own("server") == "on"
    assert views["participation"].effective == "disabled", "disabled is final below its level"
    assert [item.level for item in views["cost_footer"].levels] == ["server"]
    (ignored,) = await read_settings(
        _Factory(),
        settings=_settings(overrides=False),
        tenant_id=TENANT,
        place=THREAD,
        keys=("approvals",),
    )  # type: ignore[arg-type]
    assert not ignored.active and ignored.note.endswith(OVERRIDES_OFF)


# (key, level, value, actor, native admin live?, refusal match or None, store written)
CHANGES = [
    ("approvals", "thread", "off", "member", True, None, "set_approval_gate"),
    (
        "approvals",
        "server",
        "off",
        "admin",
        False,
        "Only a server admin can change approvals",
        None,
    ),
    ("approvals", "server", "off", "granted", False, "can change approvals", None),
    ("approvals", "server", "off", "agent_key", True, "can change approvals", None),
    ("approvals", "server", "off", "web_only", True, "can change approvals", None),
    ("cost_footer", "server", "hidden", "granted", False, None, "set_cost_footer"),
    ("cost_footer", "server", "hidden", "member", True, "Ask a server admin", None),
    ("participation", "thread", "on", "member", False, None, "set_switch"),
    ("participation", "channel", "on", "member", False, "Only a server admin", None),
    ("progress_plans", "thread", "disabled", "admin", False, "thread-level", None),
]


@pytest.mark.parametrize(("key", "level", "value", "actor", "native", "refused", "store"), CHANGES)
async def test_who_may_change_what(
    stores: dict[str, AsyncMock], key: str, level: str, value: str, actor: str, native: bool,
    refused: str | None, store: str | None,
) -> None:  # fmt: skip
    stores["approval_admin"].return_value = Subject(is_admin=native, platform_user_id="person")
    if refused is not None:
        with pytest.raises(ChannelChangeRefused, match=refused):
            await _change(key, level, value, actor)
    else:
        change = await _change(key, level, value, actor)
        assert change.after == value
    for name in ("set_approval_gate", "set_cost_footer"):
        assert stores[name].await_count == (name == store)
    if actor in ("agent_key", "web_only"):
        stores["approval_admin"].assert_not_awaited()


async def test_writes_carry_the_place_record_and_stale_check(stores: dict[str, AsyncMock]) -> None:
    await _change("approvals", "thread", "off")
    call = stores["set_approval_gate"].await_args.kwargs
    assert call["target"] == "T1" and call["subject"].is_admin
    assert (call["record"].channel_id, call["record"].thread_id) == ("C1", "T1")
    await _change("participation", "thread", "on", "member")
    target = stores["set_switch"].await_args.kwargs["target"]
    assert (target.scope_id, target.channel_id) == ("T1", "C1")
    footer = await _change("cost_footer", "server", "hidden")
    assert (
        stores["record_change"].await_args.kwargs["channel_id"] == "*" and footer.before == "shown"
    )
    with pytest.raises(StaleChannelValue):
        await _change("cost_footer", "server", "hidden", expected=Expected("hidden"))
    assert stores["set_cost_footer"].await_count == 1


def _view(
    key: Any, *, own: tuple[str, ...] = ("server", "channel", "thread"), available: bool = True
) -> SettingView:
    levels = tuple(
        LevelValue(level, "inherit" if key != "cost_footer" else "shown") for level in own
    )  # type: ignore[arg-type]
    return SettingView(
        key, SPECS[key].name, levels, "on", "Ask", "default", available, True, "Note."
    )


@pytest.mark.parametrize(
    ("admin", "native", "place", "expected"),
    [
        (False, False, THREAD, {"participation": ["Thread: On", "Thread: Off"]}),
        (True, True, THREAD, {
            "approvals": [f"{lv}: {v}" for lv in ("Server", "Channel", "Thread") for v in ("Ask", "Allow writes")],
            "participation": [f"{lv}: {v}" for lv in ("Server", "Channel") for v in ("On", "Off", "Disabled")]
            + ["Thread: On", "Thread: Off"],
            "cost_footer": ["Server: Hidden"],
        }),
        (True, True, SettingsPlace.of("slack", "C1", None), {"approvals": ["Workspace: Ask", "Workspace: Allow writes", "Channel: Ask", "Channel: Allow writes"]}),
    ],
)  # fmt: skip
def test_the_panel_offers_each_viewer_their_choices(
    admin: bool, native: bool, place: SettingsPlace, expected: dict[str, list[str]]
) -> None:
    levels = tuple(place.levels())
    views = [
        _view("approvals", own=levels),
        _view("participation", own=levels),
        _view("cost_footer", own=("server",)),
    ]
    if place.platform == "slack":
        views = views[:1]
    panel = build_settings_panel(views, place=place, admin=admin, native_admin=native)
    offered = {row.key: [label for _, label in row.choices] for row in panel.rows if row.choices}
    assert offered == expected
    assert panel.subline == core.PICK
    nobody = build_settings_panel(
        [_view("approvals")], place=THREAD, admin=True, native_admin=False
    )
    assert nobody.subline == "Only a server admin can change these. Ask a server admin."
    off = build_settings_panel(
        [_view("participation", available=False)], place=THREAD, admin=True, native_admin=True
    )
    assert off.rows[0].choices == ()


@pytest.mark.parametrize(
    "raw", ["approvals:thread:maybe", "colour:server:on", "a:b", "approvals:server:off:x", None, 3]
)
async def test_a_forged_choice_writes_and_says_nothing(
    stores: dict[str, AsyncMock], raw: object
) -> None:
    assert decode_choice(raw) is None
    assert await apply_choice(_Factory(), _settings(), _actor("admin"), raw, THREAD) is None  # type: ignore[arg-type]
    stores["set_approval_gate"].assert_not_awaited()


async def test_apply_choice_answers_with_the_change_or_the_refusal(
    stores: dict[str, AsyncMock],
) -> None:
    done = await apply_choice(
        _Factory(), _settings(), _actor("member"), "participation:thread:on", THREAD
    )  # type: ignore[arg-type]
    assert done is not None and done[0] and "Thread participation is now On" in done[1]
    refused = await apply_choice(
        _Factory(), _settings(), _actor("member"), "participation:channel:on", THREAD
    )  # type: ignore[arg-type]
    assert refused is not None and not refused[0] and "Ask a server admin" in refused[1]


@pytest.mark.parametrize(
    ("platform", "said"),
    [("discord", "run `/settings`"), ("slack", "run `/settings`"), ("teams", "send `settings`")],
)
def test_retired_controls_point_to_settings(platform: str, said: str) -> None:
    assert moved_to_settings(platform) == f"Next time, {said}."


@pytest.mark.parametrize(
    ("arguments", "line"),
    [
        ({"key": "approvals", "level": "thread", "value": "off"}, "Approvals: Allow writes for this thread"),
        ({"key": "cost_footer", "level": "server", "value": "hidden"}, "Cost footer: Hidden for all channels"),
        ({"key": "cost_footer", "level": "thread", "value": "hidden"}, None),
    ],
)  # fmt: skip
def test_the_agent_tool_card_says_the_change_in_the_panel_words(
    arguments: dict[str, object], line: str | None
) -> None:
    call = ToolCall(
        tool_use_id="s", server_name=DAIMON_SERVER_NAME, tool_name="change_setting",
        input={"origin_context_id": "origin", **arguments},
    )  # fmt: skip
    prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
    assert prompt.consequence == line
    assert prompt.detail_lines == (
        (line,) if line else ("Key: cost_footer", "Level: thread", "Value: hidden")
    )

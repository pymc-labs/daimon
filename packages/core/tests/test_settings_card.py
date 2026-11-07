"""The `/settings` panel model every chat platform draws."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.scope_settings import LevelValue, SettingsPlace, SettingView
from daimon.core.settings_card import (
    PICK,
    build_settings_panel,
    decode_choice,
    encode_choice,
    may_change_for,
    moved_to_settings,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

PLACE = SettingsPlace.of("discord", "C1", "T1")


def _view(key: str, levels: tuple[tuple[str, str], ...], *, available: bool = True) -> SettingView:
    return SettingView(
        key=key,  # type: ignore[arg-type]
        name={"approvals": "Approvals", "participation": "Thread participation"}.get(
            key, "Cost footer"
        ),
        levels=tuple(LevelValue(level, own) for level, own in levels),  # type: ignore[arg-type]
        effective="on",
        effective_label="Ask",
        decided_by="default",
        available=available,
        active=True,
        note="Approvals: Ask, the default.",
    )


ALL = (("server", "inherit"), ("channel", "inherit"), ("thread", "inherit"))


def test_a_member_sees_thread_switches_only_and_who_to_ask() -> None:
    panel = build_settings_panel(
        [
            _view("approvals", ALL),
            _view("participation", ALL),
            _view("cost_footer", (("server", "shown"),)),
        ],
        place=PLACE,
        may_change=may_change_for(admin=False, native_admin=False),
    )
    approvals, participation, footer = panel.rows
    assert approvals.choices == () and footer.choices == ()
    assert {choice.label for choice in participation.choices} == {"Thread: On", "Thread: Off"}
    assert panel.subline == PICK


def test_nobody_with_no_choices_is_told_who_can_help() -> None:
    panel = build_settings_panel(
        [_view("approvals", ALL)],
        place=PLACE,
        may_change=may_change_for(admin=True, native_admin=False),
    )
    assert panel.subline == "Only a server admin can change these. Ask a server admin."


def test_a_native_admin_gets_every_level_but_never_a_thread_lock() -> None:
    panel = build_settings_panel(
        [_view("approvals", ALL), _view("participation", ALL)],
        place=PLACE,
        may_change=may_change_for(admin=True, native_admin=True),
    )
    approvals, participation = panel.rows
    assert [c.label for c in approvals.choices] == [
        "Server: Ask",
        "Server: Allow writes",
        "Channel: Ask",
        "Channel: Allow writes",
        "Thread: Ask",
        "Thread: Allow writes",
    ], "the current value is not offered again"
    assert "Thread: Disabled" not in {c.label for c in participation.choices}
    assert "Server: Disabled" in {c.label for c in participation.choices}


def test_slack_says_workspace_for_the_server_level() -> None:
    panel = build_settings_panel(
        [_view("approvals", (("server", "inherit"), ("channel", "inherit")))],
        place=SettingsPlace.of("slack", "C1", None),
        may_change=may_change_for(admin=True, native_admin=True),
    )
    assert panel.rows[0].choices[0].label == "Workspace: Ask"


def test_an_unavailable_row_has_no_controls() -> None:
    panel = build_settings_panel(
        [_view("participation", ALL, available=False)],
        place=PLACE,
        may_change=may_change_for(admin=True, native_admin=True),
    )
    assert panel.rows[0].choices == ()


def test_choices_round_trip_and_forgeries_are_dropped() -> None:
    assert decode_choice(encode_choice("approvals", "thread", "off")) == (
        "approvals",
        "thread",
        "off",
    )
    for forged in (
        "approvals:thread:disabled",
        "colour:server:on",
        "a:b",
        None,
        3,
        "cost_footer:x:shown",
    ):
        assert decode_choice(forged) is None


@pytest.mark.parametrize(
    ("platform", "said"),
    [("discord", "run `/settings`"), ("slack", "run `/settings`"), ("teams", "send `settings`")],
)
def test_retired_controls_point_to_settings(platform: str, said: str) -> None:
    assert moved_to_settings(platform) == f"Next time, {said}."


@pytest.mark.parametrize(
    "arguments,line",
    [
        (
            {"key": "approvals", "level": "thread", "value": "off"},
            "Approvals: Allow writes for this thread",
        ),
        (
            {"key": "approvals", "level": "channel", "value": "on"},
            "Approvals: Ask for this channel",
        ),
        (
            {"key": "cost_footer", "level": "server", "value": "hidden"},
            "Cost footer: Hidden for all channels",
        ),
    ],
)
def test_the_agent_tool_card_says_the_change_in_the_panel_words(
    arguments: dict[str, object], line: str
) -> None:
    call = ToolCall(
        tool_use_id="setting",
        server_name=DAIMON_SERVER_NAME,
        tool_name="change_setting",
        input={"origin_context_id": "origin", **arguments},
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
    assert prompt.consequence == line
    assert prompt.detail_lines == (line,)
    assert "origin" not in " ".join(prompt.detail_lines)


def test_a_change_the_tool_would_refuse_keeps_the_generic_card() -> None:
    call = ToolCall(
        tool_use_id="setting",
        server_name=DAIMON_SERVER_NAME,
        tool_name="change_setting",
        input={"key": "cost_footer", "level": "thread", "value": "hidden"},
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
    assert prompt.consequence is None
    assert "Level: thread" in prompt.detail_lines

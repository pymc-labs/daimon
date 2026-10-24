"""microsoft_send waits for the person's Approve wherever Microsoft accounts are on."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from daimon.core.confirmation import (
    SEND_BODY_LIMIT,
    ConfirmationPrompt,
    confirmed_tools_for_turn,
    prompt_for_tool_call,
)
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
    has_confirmation_gate,
    microsoft_send_tools,
    session_tools_for_policy,
)
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.posture import PolicyApproval

_TRUSTED = frozenset({DAIMON_SERVER_NAME})
_TOOLS: list[dict[str, object]] = [
    {
        "type": "mcp_toolset",
        "mcp_server_name": DAIMON_SERVER_NAME,
        "default_config": {"permission_policy": {"type": "always_allow"}},
    }
]


def _call(**data: object) -> ToolCall:
    return ToolCall(
        tool_use_id="t1",
        server_name=DAIMON_SERVER_NAME,
        tool_name="microsoft_send",
        input={"action": "send_mail", "to": ["ada@example.com"], "subject": "Notes", **data},
    )


def _deps(*, configured: bool, tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY) -> TurnDeps:
    return cast(
        TurnDeps,
        SimpleNamespace(
            backend_selection_enabled=False, microsoft_send=configured, tool_safety=tool_safety
        ),
    )


def test_turns_select_microsoft_send_only_when_configured() -> None:
    on = confirmed_tools_for_turn(_deps(configured=True))
    assert on - {"create_watch", "resume_watch", "rotate_watch_key"} == {"microsoft_send"}, (
        "configured: sessions ask before Microsoft sends"
    )
    safe = confirmed_tools_for_turn(
        _deps(configured=True, tool_safety=ToolSafetyPolicy(enabled=True))
    )
    assert safe - {"create_watch", "resume_watch", "rotate_watch_key"} == {
        "microsoft_send",
        "microsoft_write",
    }, "tool safety on: writes ask too"
    off = _deps(configured=False, tool_safety=ToolSafetyPolicy(enabled=True))
    assert confirmed_tools_for_turn(off) == {"create_watch", "resume_watch", "rotate_watch_key"}, (
        "unconfigured Microsoft: only standing watch actions are selected"
    )


def test_selected_microsoft_send_asks_with_tool_safety_off() -> None:
    selected = microsoft_send_tools(enabled=True)
    asked = decide_tool_call(
        OPEN_TOOL_SAFETY, _call(), attended=True, trusted_servers=_TRUSTED, confirmed_tools=selected
    )
    assert asked.outcome == "ask", "an attended send must wait for the card"
    unattended = decide_tool_call(
        OPEN_TOOL_SAFETY,
        _call(),
        attended=False,
        trusted_servers=_TRUSTED,
        confirmed_tools=selected,
    )
    assert unattended.outcome == "deny", "a run nobody watches must never send as the person"
    gated = session_tools_for_policy(
        OPEN_TOOL_SAFETY, _TOOLS, trusted_servers=_TRUSTED, confirmed_tools=selected
    )
    assert gated is not None and has_confirmation_gate(gated, tool_name="microsoft_send"), (
        "the session must hold microsoft_send on always_ask, the gate the tool checks"
    )


def test_microsoft_send_is_not_asked_unless_selected() -> None:
    gated = session_tools_for_policy(
        ToolSafetyPolicy(enabled=True), _TOOLS, trusted_servers=_TRUSTED
    )
    assert gated is None or not has_confirmation_gate(gated, tool_name="microsoft_send"), (
        "a deployment without Microsoft accounts must not gain a config for an absent tool"
    )


def test_card_shows_every_recipient_location_and_body_without_details() -> None:
    """Who and what goes out is in the always-visible text, never truncated."""
    long_cc = "a.very.long.mailbox.name.for.an.optional.attendee@subsidiary.example.com"
    prompt = prompt_for_tool_call(
        _call(
            action="create_event",
            start="2026-09-15T10:00",
            end="2026-09-15T11:00",
            cc=["bob@example.com", long_cc],
            location="Room 4",
            body="Agenda:\n  budget review",
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    shown = prompt.consequence or ""
    assert prompt.title == 'Send invite "Notes"?', "the card must say what goes out"
    assert shown.startswith("It goes out from your Microsoft account."), "sent as the person"
    assert "To: ada@example.com" in shown, "the card must list the required attendees"
    assert f"Optional: bob@example.com, {long_cc}" in shown, "every cc address, untruncated"
    assert "Where: Room 4" in shown, "an invite shows where"
    assert "Message: Agenda: budget review" in shown, "the card previews the body"
    assert "When: 2026-09-15T10:00 to 2026-09-15T11:00 (UTC)" in prompt.detail_lines, (
        "an invite shows when, in which time zone"
    )


def test_card_shows_the_time_zone_the_invite_is_in() -> None:
    prompt = prompt_for_tool_call(
        _call(
            action="create_event",
            start="2026-09-15T10:00",
            end="2026-09-15T11:00",
            time_zone="Europe/Berlin",
            body="Hi",
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert "When: 2026-09-15T10:00 to 2026-09-15T11:00 (Europe/Berlin)" in (
        prompt.consequence or ""
    ), "the person must see which zone the times are in"


def test_long_body_is_previewed_and_given_in_full_behind_details() -> None:
    """Nothing the person approves is cut: past the preview, Details has the rest."""
    body = "\n".join(f"Line {n}: " + "word " * 20 for n in range(60))
    prompt = prompt_for_tool_call(
        _call(body=body), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    shown = prompt.consequence or ""
    more = len(" ".join(body.split())) - 400
    assert f"(+{more} more characters, see Details)" in shown, (
        "the preview must say how much is left"
    )
    assert "Line 59" not in shown, "the visible text stays a preview"
    details = "\n".join(prompt.detail_lines)
    assert all(line in details for line in body.splitlines()), "Details must hold every body line"
    assert len(body) < SEND_BODY_LIMIT, "the fixture must be a body the tool sends"


def test_mail_card_labels_cc_recipients() -> None:
    """A mail's cc list is shown as Cc."""
    prompt = prompt_for_tool_call(
        _call(cc=["eve@example.com"], body="Hi"),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert "Cc: eve@example.com" in (prompt.consequence or ""), "mail cc must be visible"


def _write(policy: ToolSafetyPolicy, *, attended: bool = True) -> str:
    call = ToolCall(
        tool_use_id="t2",
        server_name=DAIMON_SERVER_NAME,
        tool_name="microsoft_write",
        input={"method": "PUT", "path": "/me/drive/items/i1/content", "file_handle": "h"},
    )
    selected = microsoft_send_tools(enabled=True, tool_safety_on=policy.enabled)
    return decide_tool_call(
        policy, call, attended=attended, trusted_servers=_TRUSTED, confirmed_tools=selected
    ).outcome


def test_microsoft_write_asks_only_with_tool_safety_on() -> None:
    """A change to the person's files waits for the card where tool safety is on."""
    on = ToolSafetyPolicy(enabled=True)
    assert _write(on) == "ask", "tool safety on: an attended write must ask"
    assert _write(on, attended=False) == "deny", "tool safety on: nobody to ask, so refused"
    gated = session_tools_for_policy(
        on,
        _TOOLS,
        trusted_servers=_TRUSTED,
        confirmed_tools=microsoft_send_tools(enabled=True, tool_safety_on=True),
    )
    assert gated is not None and has_confirmation_gate(gated, tool_name="microsoft_write"), (
        "tool safety on: the session must hold microsoft_write on always_ask"
    )
    assert _write(OPEN_TOOL_SAFETY) == "allow", "tool safety off: writes run as before"
    off = session_tools_for_policy(
        OPEN_TOOL_SAFETY,
        _TOOLS,
        trusted_servers=_TRUSTED,
        confirmed_tools=microsoft_send_tools(enabled=True, tool_safety_on=False),
    )
    assert off is not None and not has_confirmation_gate(off, tool_name="microsoft_write"), (
        "tool safety off: microsoft_write must not gain a gate"
    )
    absent = session_tools_for_policy(on, _TOOLS, trusted_servers=_TRUSTED)
    assert absent is None or not has_confirmation_gate(absent, tool_name="microsoft_write"), (
        "without Microsoft accounts no session gains a config for the absent tool"
    )


def _lines(prompt: ConfirmationPrompt) -> list[str]:
    return (prompt.consequence or "").splitlines()


def test_a_subject_cannot_add_a_recipient_line() -> None:
    """Newlines in what the model wrote stay inside the field they came in."""
    prompt = prompt_for_tool_call(
        _call(subject="Hi\nTo: ceo@else.com", body="Hi"),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert [line for line in _lines(prompt) if line.startswith("To:")] == ["To: ada@example.com"], (
        "only the real recipients may head a line"
    )
    assert "\n" not in prompt.title and "\n" not in (prompt.action or ""), "one-line title"
    assert "Subject: Hi To: ceo@else.com" in _lines(prompt), "the subject reads on one line"
    assert "Subject: Hi To: ceo@else.com" in prompt.detail_lines


def test_location_and_addresses_stay_on_their_own_lines() -> None:
    prompt = prompt_for_tool_call(
        _call(
            action="create_event",
            start="2026-09-15T10:00",
            end="2026-09-15T11:00\nTo: ceo@else.com",
            to=["ada@example.com\rCc: boss@else.com"],
            location="Room 4\u2028To: ceo@else.com\u202e",
            body="Hi",
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    shown = "\n".join((*_lines(prompt), *prompt.detail_lines))
    heads = [line.split(":")[0] for line in _lines(prompt)]
    assert heads == [
        "It goes out from your Microsoft account.",
        "To",
        "Subject",
        "When",
        "Where",
        "Message",
    ], "each field is one line, whatever its text held"
    assert "\u202e" not in shown and "\u2028" not in shown, "no direction or line separators"


def test_a_long_body_of_short_lines_is_still_whole_in_details() -> None:
    body = "a\n" * (SEND_BODY_LIMIT // 2)
    prompt = prompt_for_tool_call(
        _call(body=body), requester_platform_user_id="requester", now=datetime.now(UTC)
    )
    assert prompt.detail_lines.count("a") == SEND_BODY_LIMIT // 2, "every line is in Details"


async def test_a_card_that_cannot_be_shown_is_not_reported_as_a_refusal() -> None:
    """Nothing runs, and the model is told the card failed, not that the person said no."""
    posture = chat_tool_confirmation(
        OPEN_TOOL_SAFETY,
        requester_platform_user_id="requester",
        confirm=AsyncMock(side_effect=RuntimeError("Teams is down")),
        trusted_servers=_TRUSTED,
        confirmed_tools=microsoft_send_tools(enabled=True),
    )
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(_call())
    assert result.allow is False, "a send must never run because its card failed"
    message = result.deny_message or ""
    assert "approval card" in message and "could not be shown" in message
    assert "did not run" in message and "denied" not in message
    assert "did not refuse" in message, "the person must not be blamed for the failure"

"""Discord's confirmation card: drawn from the core card, answered by the requester."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.tool_confirmation import (
    build_confirmation_view,
    discord_confirmation_hook,
)
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt, prompt_for_tool_call
from daimon.core.posted_controls.confirmation import NOT_YOURS_MESSAGE, build_confirmation_card
from daimon.core.tool_safety import ToolCall


def _prompt(*, expires_in: timedelta = timedelta(minutes=10)) -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="111", now=datetime.now(UTC))
    return prompt.model_copy(update={"expires_at": datetime.now(UTC) + expires_in})


def _texts(view: discord.ui.LayoutView) -> list[str]:
    return [
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    ]


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def _interaction(user_id: int) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def test_a_pending_card_shows_the_write_and_two_buttons() -> None:
    prompt = _prompt()
    view = build_confirmation_view(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    texts = _texts(view)
    assert texts[0] == "**✋ Approve a write to linear?**"
    assert "-# Tool: `create_issue`" in texts[1]
    assert '"title": "Bug"' in texts[2]
    assert texts[-1].startswith("-# Only <@111> can answer. Expires <t:")
    assert [b.label for b in _buttons(view)] == ["Approve", "Deny"]


def test_an_answered_card_has_no_buttons() -> None:
    prompt = _prompt()
    view = build_confirmation_view(build_confirmation_card(prompt, state="denied"), prompt)
    assert _buttons(view) == []
    assert _texts(view)[0] == "**🛡️ Denied — it did not run.**"


async def _post_and_click(user_id: int, choice: str) -> tuple[ConfirmationAnswer, MagicMock]:
    channel = MagicMock()
    posted: list[discord.ui.LayoutView] = []

    async def _send(*, view: discord.ui.LayoutView) -> MagicMock:
        posted.append(view)
        return MagicMock(edit=AsyncMock())

    channel.send = _send
    hook = discord_confirmation_hook(channel)
    waiting = asyncio.create_task(hook(_prompt()))
    while not posted:
        await asyncio.sleep(0)
    approve, deny = _buttons(posted[0])
    stranger = _interaction(999)
    await (approve if choice == "approve" else deny).callback(stranger)
    stranger.response.send_message.assert_awaited_once_with(NOT_YOURS_MESSAGE, ephemeral=True)
    assert not waiting.done(), "a stranger's click answers nothing"
    requester = _interaction(user_id)
    await (approve if choice == "approve" else deny).callback(requester)
    return await asyncio.wait_for(waiting, timeout=1), requester


async def test_the_requesters_approve_answers_approved_and_edits_the_card() -> None:
    answer, requester = await _post_and_click(111, "approve")
    assert answer == "approved"
    edited = requester.response.edit_message.await_args.kwargs["view"]
    assert _texts(edited)[0] == "**✅ Approved — running it.**"
    assert _buttons(edited) == []


async def test_the_requesters_deny_answers_denied() -> None:
    answer, _requester = await _post_and_click(111, "deny")
    assert answer == "denied"


async def test_an_unanswered_card_expires() -> None:
    channel = MagicMock()
    message = MagicMock(edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    hook = discord_confirmation_hook(channel)

    answer = await hook(_prompt(expires_in=timedelta(milliseconds=10)))

    assert answer == "expired"
    view = message.edit.await_args.kwargs["view"]
    assert _texts(view)[0].startswith("**⌛")


async def test_a_cancelled_wait_retires_the_card() -> None:
    channel = MagicMock()
    message = MagicMock(edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(_prompt()))
    while not channel.send.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    view = message.edit.await_args.kwargs["view"]
    assert _texts(view)[0] == "**🛡️ Denied — it did not run.**"
    assert _buttons(view) == []


async def _run_turn_with_stalled_card_retire(
    hook: object, monkeypatch: pytest.MonkeyPatch
) -> tuple[float, object]:
    """Drive a real turn whose write card is up when a 100ms deadline hits,
    with the platform stalled on the card edit that retires it."""
    import daimon.core.turn.driver as driver_mod
    from anthropic.types.beta.sessions import (
        BetaManagedAgentsAgentMCPToolUseEvent,
        BetaManagedAgentsSessionEndTurn,
        BetaManagedAgentsSessionRequiresAction,
        BetaManagedAgentsSessionStatusIdleEvent,
    )
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.core.turn import run_turn
    from daimon.core.turn.approvals import interactive_decider
    from daimon.core.turn.posture import BillingExempt, PolicyApproval
    from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent

    monkeypatch.setattr(driver_mod, "CLEANUP_BUDGET_S", 0.2)
    at = datetime.now(UTC)
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(
                BetaManagedAgentsAgentMCPToolUseEvent(
                    id="tu_w",
                    type="agent.mcp_tool_use",
                    name="create_issue",
                    input={"title": "x"},
                    mcp_server_name="linear",
                    processed_at=at,
                )
            ),
            YieldEvent(
                BetaManagedAgentsSessionStatusIdleEvent(
                    id="pause",
                    type="session.status_idle",
                    stop_reason=BetaManagedAgentsSessionRequiresAction(
                        type="requires_action", event_ids=["tu_w"]
                    ),
                    processed_at=at,
                )
            ),
            YieldEvent(
                BetaManagedAgentsSessionStatusIdleEvent(
                    id="end",
                    type="session.status_idle",
                    stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
                    processed_at=at,
                )
            ),
        ]
    ]
    started = asyncio.get_running_loop().time()
    final = await asyncio.wait_for(
        run_turn(
            anthropic=fa,  # type: ignore[arg-type]
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="cli-operator-run"),
            tool_confirmation=PolicyApproval(
                decide=interactive_decider(
                    ToolSafetyPolicy(enabled=True),
                    requester_platform_user_id="111",
                    confirm=hook,  # type: ignore[arg-type]
                )
            ),
            deadline=datetime.now(UTC) + timedelta(milliseconds=100),
        ),
        timeout=5,
    )
    elapsed = asyncio.get_running_loop().time() - started
    sent = [
        e
        for _sid, batch in fa.beta.sessions.events.sent_events
        for e in batch
        if e["type"] == "user.tool_confirmation"
    ]
    assert all(e["result"] != "allow" for e in sent), "a stalled cleanup never lets the write run"
    return elapsed, final


async def test_a_stalled_discord_retire_cannot_hold_the_turn_past_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.adapters.discord.tool_confirmation as hook_mod

    monkeypatch.setattr(hook_mod, "RETIRE_TIMEOUT_S", 60.0)  # the driver's budget must bound it

    async def stalled_edit(**_kwargs: object) -> None:
        await asyncio.Event().wait()  # Discord never answers

    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=stalled_edit))

    elapsed, final = await _run_turn_with_stalled_card_retire(
        discord_confirmation_hook(channel), monkeypatch
    )

    assert final.error is not None and final.error.kind == "ceiling"  # type: ignore[attr-defined]
    assert elapsed < 1.5, f"took {elapsed:.2f}s"
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "turn.decide_blocked"]

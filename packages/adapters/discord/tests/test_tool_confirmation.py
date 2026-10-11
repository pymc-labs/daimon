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
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationPrompt,
    prompt_for_tool_call,
)
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
    assert texts[0] == '**Create issue "Bug"?**'
    assert texts[-2] == "-# Only <@111> can approve or deny"
    assert texts[-1].startswith("-# Expires <t:")
    assert [b.label for b in _buttons(view)] == ["Approve", "Deny", "Details"]


@pytest.mark.parametrize(
    ("value", "safe"),
    [
        (
            "@everyone @here <@123456789012345678> <@&123456789012345678> <#123456789012345678>",
            "@\u200beveryone @\u200bhere <@\u200b123456789012345678> "
            "<@\u200b&123456789012345678> <#\u200b123456789012345678>",
        ),
        ("[text](https://example.test/path)", "\\[text](https://example.test/path)"),
        ("*x* `x` ```x``` rest", "\\*x\\* \\`x\\` \\`\\`\\`x\\`\\`\\` rest"),
    ],
)
async def test_tool_words_stay_literal_in_title_and_details(value: str, safe: str) -> None:
    prompt = _prompt().model_copy(
        update={
            "title": f'Publish "{value}"?',
            "consequence": f"Sharing {value} is public.",
            "detail_lines": (f"File: {value}",),
        }
    )
    view = build_confirmation_view(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    assert safe in _texts(view)[0]
    assert safe in _texts(view)[1]
    click = _interaction(999)
    await _buttons(view)[2].callback(click)
    kwargs = click.response.send_message.await_args.kwargs
    assert click.response.send_message.await_args.args[0] == (f"File: {safe}")
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}

    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(prompt))
    while not channel.send.await_count:
        await asyncio.sleep(0)
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting


def test_an_answered_card_has_no_buttons() -> None:
    prompt = _prompt()
    view = build_confirmation_view(build_confirmation_card(prompt, state="denied"), prompt)
    assert _buttons(view) == []
    assert _texts(view)[0] == "**Denied**"


async def _post_and_click(
    user_id: int, choice: str
) -> tuple[ConfirmationAnswer | ApprovedConfirmation, MagicMock]:
    channel = MagicMock()
    posted: list[discord.ui.LayoutView] = []

    async def _send(
        *, view: discord.ui.LayoutView, allowed_mentions: discord.AllowedMentions
    ) -> MagicMock:
        posted.append(view)
        return MagicMock(edit=AsyncMock())

    channel.send = _send
    hook = discord_confirmation_hook(channel)
    waiting = asyncio.create_task(hook(_prompt()))
    while not posted:
        await asyncio.sleep(0)
    approve, deny, _details = _buttons(posted[0])
    stranger = _interaction(999)
    await (approve if choice == "approve" else deny).callback(stranger)
    stranger.response.send_message.assert_awaited_once()
    assert stranger.response.send_message.await_args.args[0] == NOT_YOURS_MESSAGE.format(
        requester="<@111>"
    )
    assert stranger.response.send_message.await_args.kwargs["ephemeral"] is True
    assert stranger.response.send_message.await_args.kwargs["allowed_mentions"].users is False
    assert not waiting.done(), "a stranger's click answers nothing"
    requester = _interaction(user_id)
    await (approve if choice == "approve" else deny).callback(requester)
    return await asyncio.wait_for(waiting, timeout=1), requester


async def test_the_requesters_approve_answers_approved_and_edits_the_card() -> None:
    answer, requester = await _post_and_click(111, "approve")
    assert isinstance(answer, ApprovedConfirmation) and answer.answer == "approved"
    edited = requester.response.edit_message.await_args.kwargs["view"]
    assert _texts(edited)[0] == "**Approved**"
    assert _buttons(edited) == []


async def test_the_requesters_deny_answers_denied() -> None:
    answer, _requester = await _post_and_click(111, "deny")
    assert answer == "denied"


async def test_approved_card_can_retire_stopped_before_allow_is_sent() -> None:
    channel = MagicMock()
    message = MagicMock(edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(_prompt()))
    while not channel.send.await_count:
        await asyncio.sleep(0)
    view = channel.send.await_args.kwargs["view"]
    await _buttons(view)[0].callback(_interaction(111))

    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation)
    await result.retire_unsent()

    stopped = message.edit.await_args.kwargs["view"]
    assert _texts(stopped)[0] == "**Stopped**"
    assert _buttons(stopped) == []


async def test_an_unanswered_card_expires() -> None:
    channel = MagicMock()
    message = MagicMock(edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    hook = discord_confirmation_hook(channel)

    answer = await hook(_prompt(expires_in=timedelta(milliseconds=10)))

    assert answer == "expired"
    view = message.edit.await_args.kwargs["view"]
    assert _texts(view)[0].startswith("**Expired")
    assert any("Approval timed out. Ask again to run it." in text for text in _texts(view))


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
    assert _texts(view)[0] == "**Stopped**"
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


async def test_upload_card_and_details_are_scannable() -> None:
    prompt = _prompt().model_copy(
        update={
            "title": 'Upload "cg_meme.csv" to notebook "memecoin-scan"?',
            "consequence": "Anyone with the notebook's link can open these files.",
        }
    )
    view = build_confirmation_view(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    assert _texts(view)[0] == '**Upload "cg\\_meme.csv" to notebook "memecoin-scan"?**'
    assert [button.label for button in _buttons(view)] == ["Approve", "Deny", "Details"]
    click = _interaction(999)
    await _buttons(view)[2].callback(click)
    click.response.send_message.assert_awaited_once()
    assert click.response.send_message.await_args.kwargs["ephemeral"] is True
    assert click.response.send_message.await_args.args[0] == "Title: Bug"
    assert "```" not in click.response.send_message.await_args.args[0]

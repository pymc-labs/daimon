"""Discord's confirmation card: drawn from the core card, answered by the requester."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import tool_confirmation as confirmation_module
from daimon.adapters.discord.tool_confirmation import (
    build_confirmation_view,
    discord_confirmation_hook,
)
from daimon.core.authz import Subject
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationPrompt,
    ToolRuleCardReference,
    prompt_for_tool_call,
)
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCardState,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import drain_card_edits
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall


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
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
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
    assert click.response.send_message.await_args.kwargs["embed"].description == (f"File: {safe}")
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


@pytest.mark.parametrize(
    ("enabled", "state", "footer"),
    [
        (True, "approved", "-# Approved once by <@111>"),
        (True, "denied", "-# Denied by <@111>"),
        (False, "approved", "-# by <@111>"),
        (False, "denied", "-# by <@111>"),
    ],
)
def test_requester_answer_footer(enabled: bool, state: ConfirmationCardState, footer: str) -> None:
    prompt = _prompt().model_copy(update={"tool_rules_enabled": enabled})
    card = build_confirmation_card(
        prompt,
        state=state,
        answered_by_platform_user_id="111",
        how="once" if enabled else None,
    )
    assert _texts(build_confirmation_view(card, prompt))[-1] == footer


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
    assert stranger.response.send_message.await_args.kwargs[
        "embed"
    ].description == NOT_YOURS_MESSAGE.format(requester="<@111>")
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


async def test_always_button_allows_admin_and_names_the_clicker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh", allow_always=True)
    subject = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="222"))
    view = confirmation_module._ConfirmationView(
        card, prompt, answer, rule_factory=MagicMock(), rule_subject=subject
    )
    assert [b.label for b in _buttons(view)] == ["Approve", "Deny", "Always allow", "Details"]
    save = AsyncMock(return_value="saved")
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    click = _interaction(222)
    await _buttons(view)[2].callback(click)
    assert await drain_card_edits(1) == 0
    assert answer.result() == "approved"
    assert save.await_args.kwargs["scope"] == "agent"
    edited = click.edit_original_response.await_args.kwargs["view"]
    assert "-# Always allowed by <@222>" in _texts(edited)
    click.response.defer.assert_awaited_once_with()


async def test_always_button_refuses_requester_without_admin_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh", allow_always=True)
    view = confirmation_module._ConfirmationView(
        card,
        prompt,
        answer,
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject()),
    )
    save = AsyncMock()
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    click = _interaction(111)
    await _buttons(view)[2].callback(click)
    assert click.followup.send.await_args.kwargs["embed"].description == (
        "Only an admin can allow this for everyone.\n"
        "Ask an admin, or use Approve for just this time."
    )
    assert click.response.defer.await_count == 1
    assert not answer.done()
    save.assert_not_awaited()


async def test_always_defers_before_live_admin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    order: list[str] = []
    click = _interaction(222)
    click.response.defer.side_effect = lambda: order.append("defer")

    async def subject(_clicker: str) -> Subject:
        order.append("admin")
        return Subject(is_admin=True, platform_user_id="222")

    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(return_value="saved"))
    answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    view = confirmation_module._ConfirmationView(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh", allow_always=True),
        prompt,
        answer,
        rule_factory=MagicMock(),
        rule_subject=subject,
    )
    await view._on_always(click)
    assert order == ["defer", "admin"]


@pytest.mark.parametrize("failure", [RuntimeError("save failed"), asyncio.CancelledError()])
async def test_always_save_failure_marks_edit_done_and_approves_once(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    view = confirmation_module._ConfirmationView(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh", allow_always=True),
        prompt,
        answer,
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject(is_admin=True, platform_user_id="222")),
    )
    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(side_effect=failure))
    click = _interaction(222)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await view._on_always(click)
    else:
        await view._on_always(click)
    assert await drain_card_edits(1) == 0
    assert answer.result() == "approved"
    assert view.answered_edit_done.is_set()
    assert "-# Approved once by <@222>" in _texts(
        click.edit_original_response.await_args.kwargs["view"]
    )


async def test_blocked_always_save_does_not_block_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    channel = MagicMock()
    message = SimpleNamespace(id=123, edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(prompt))
    while not channel.send.await_count:
        await asyncio.sleep(0)
    view = channel.send.await_args.kwargs["view"]
    view._rule_factory = MagicMock()
    view._rule_subject = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="222"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        entered.set()
        await release.wait()
        return "saved"

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    click = asyncio.create_task(view._on_always(_interaction(222)))
    await entered.wait()
    result = await asyncio.wait_for(waiting, 1)
    assert isinstance(result, ApprovedConfirmation)
    await asyncio.wait_for(result.retire_unsent(), 0.5)
    assert not click.done()
    release.set()
    await click
    assert await drain_card_edits(1) == 0
    assert _texts(message.edit.await_args.kwargs["view"])[0] == "**Stopped**"


@pytest.mark.parametrize("save_result", ["saved", "stale"])
async def test_turn_cancel_after_always_settle_keeps_stopped_card(
    monkeypatch: pytest.MonkeyPatch, save_result: str
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    channel = MagicMock()
    message = SimpleNamespace(id=124, edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(prompt))
    while not channel.send.await_count:
        await asyncio.sleep(0)
    view = channel.send.await_args.kwargs["view"]
    view._rule_factory = MagicMock()
    view._rule_subject = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="222"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_save(*_args: object, **_kwargs: object) -> str:
        waiting.cancel()
        entered.set()
        await release.wait()
        return save_result

    monkeypatch.setattr(confirmation_module, "save_always_allow", blocked_save)
    interaction = _interaction(222)
    click = asyncio.create_task(view._on_always(interaction))
    await entered.wait()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert view.retired
    release.set()
    await click
    assert await drain_card_edits(1) == 0
    assert _texts(message.edit.await_args.kwargs["view"])[0] == "**Stopped**"
    assert interaction.edit_original_response.await_count == 1
    if save_result == "stale":
        assert interaction.followup.send.await_args.kwargs["embed"].description.startswith(
            "Approved this time only."
        )
    else:
        interaction.followup.send.assert_not_awaited()


async def test_always_edit_after_discord_token_lifetime_stays_approved_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
    view = confirmation_module._ConfirmationView(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh", allow_always=True),
        prompt,
        answer,
        rule_factory=MagicMock(),
        rule_subject=AsyncMock(return_value=Subject(is_admin=True, platform_user_id="222")),
    )
    monkeypatch.setattr(confirmation_module, "save_always_allow", AsyncMock(return_value="saved"))
    clock = iter([0.0, 901.0])
    monkeypatch.setattr(confirmation_module, "monotonic", lambda: next(clock))
    logger = MagicMock()
    monkeypatch.setattr(confirmation_module, "log", logger)
    click = _interaction(222)
    await view._on_always(click)
    assert await drain_card_edits(1) == 0
    assert click.edit_original_response.await_count == 1
    assert "-# Approved once by <@222>" in _texts(
        click.edit_original_response.await_args.kwargs["view"]
    )
    logger.info.assert_called_once_with("tool_confirmation.always_edit_token_expired")


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
    from daimon.core.turn.approvals import interactive_decider
    from daimon.core.turn.posture import BillingExempt, PolicyApproval
    from daimon.testing.turn_fakes import (
        FakeAnthropic,
        RecordingLifecycle,
        YieldEvent,
        run_turn_on_leg,
    )

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
        run_turn_on_leg(
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
    assert click.response.send_message.await_args.kwargs["embed"].description == "Title: Bug"
    assert "```" not in click.response.send_message.await_args.kwargs["embed"].description


async def test_admin_confirmation_rechecks_each_click_before_settlement() -> None:
    channel = MagicMock()
    message = MagicMock(edit=AsyncMock())
    channel.send = AsyncMock(return_value=message)
    prompt = _prompt().model_copy(update={"admin": True})
    prepare = AsyncMock(
        return_value=prompt.model_copy(update={"detail_lines": ("List price: verified",)})
    )
    check = AsyncMock(return_value="Current admin permission required.")
    waiting = asyncio.create_task(
        discord_confirmation_hook(channel, prepare_admin=prepare, check_admin=check)(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not channel.send.await_count:
                await asyncio.sleep(0)
        view = channel.send.await_args.kwargs["view"]
        assert _buttons(view)[0].label == "Confirm"
        assert "List price: verified" in _texts(view)
        denied = _interaction(111)
        await _buttons(view)[0].callback(denied)
        assert (
            denied.response.send_message.await_args.kwargs["embed"].description
            == "Current admin permission required."
        )
        denied.response.edit_message.assert_not_awaited()
        assert not waiting.done()
        check.return_value = None
        await _buttons(view)[0].callback(_interaction(111))
        assert isinstance(await asyncio.wait_for(waiting, timeout=1), ApprovedConfirmation)
        assert check.await_count == 2
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_admin_confirmation_without_a_verifier_posts_nothing() -> None:
    channel = MagicMock(send=AsyncMock())
    prompt = _prompt().model_copy(update={"admin": True})
    assert await discord_confirmation_hook(channel)(prompt) == "denied"
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("case", ["expired", "exception", "wrong_requester", "legacy"])
async def test_admin_await_cannot_bypass_requester_expiry_or_fail_closed(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.posted_controls import lifecycle

    channel = MagicMock(send=AsyncMock(return_value=MagicMock(edit=AsyncMock())))
    prompt = _prompt().model_copy(update={"admin": case != "legacy"})

    async def verify(*args: object) -> None:
        await asyncio.sleep(0)
        if case == "exception":
            raise RuntimeError("role lookup unavailable")
        if case == "expired":
            clock = MagicMock()
            clock.now.return_value = prompt.expires_at
            monkeypatch.setattr(lifecycle, "datetime", clock)

    check = AsyncMock(side_effect=verify)
    waiting = asyncio.create_task(
        discord_confirmation_hook(
            channel, prepare_admin=AsyncMock(return_value=prompt), check_admin=check
        )(prompt)
    )
    try:
        async with asyncio.timeout(2):
            while not channel.send.await_count:
                await asyncio.sleep(0)
        view = channel.send.await_args.kwargs["view"]
        click = _interaction(999 if case == "wrong_requester" else 111)
        await _buttons(view)[0].callback(click)
        if case == "legacy":
            assert isinstance(await asyncio.wait_for(waiting, 1), ApprovedConfirmation)
            check.assert_not_awaited()
        else:
            click.response.edit_message.assert_not_awaited()
            click.response.send_message.assert_awaited_once()
            assert not waiting.done()
            if case == "wrong_requester":
                check.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_runtime_hook_reuses_live_guild_admin_lookup(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    from uuid import uuid4

    from daimon.core import confirmation
    from daimon.core.config import TurnSettings
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        tenant_row = await make_tenant(session)
    tenant, account = tenant_row.id, uuid4()
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild.owner_id = 999
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.roles = []
    member.guild_permissions = discord.Permissions(administrator=True)
    channel.guild.fetch_member = AsyncMock(return_value=member)
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    discord_confirmation_hook(channel, runtime=runtime, tenant_id=tenant, account_id=account)
    kwargs = factory.call_args.kwargs
    assert kwargs["tenant_id"] == tenant and kwargs["account_id"] == account
    check = kwargs["current_admin"]
    assert await check("111") is True
    member.guild_permissions = discord.Permissions.none()
    assert await check("111") is False
    assert channel.guild.fetch_member.await_count == 2


async def test_runtime_hook_current_admin_refuses_outside_a_thread_or_text_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend-switch admin recheck is scoped to threads/text channels: a voice
    channel with the same guild and an administrator clicker must still refuse,
    not merely inherit whatever `requester_role` says about the guild member.
    """
    from uuid import uuid4

    from daimon.core import confirmation
    from daimon.core.config import TurnSettings

    tenant, account = uuid4(), uuid4()
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild.owner_id = 999
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.roles = []
    member.guild_permissions = discord.Permissions(administrator=True)
    channel.guild.fetch_member = AsyncMock(return_value=member)
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    discord_confirmation_hook(channel, runtime=runtime, tenant_id=tenant, account_id=account)
    check = factory.call_args.kwargs["current_admin"]
    assert await check("111") is False
    channel.guild.fetch_member.assert_not_awaited()


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired", "stopped"])
async def test_complete_backend_loss_list_survives_native_card_rendering(state):
    losses = "Won't be available on openai: files, mcp, memory, model_controls, repository, sandbox, skills, tools"
    prompt = _prompt().model_copy(
        update={
            "admin": True,
            "action": "Switch Research. " + losses,
            "denied_action": "Model provider unchanged",
            "consequence": losses,
            "detail_lines": ("Optional skill: " + "x" * 1000,),
        }
    )
    card = build_confirmation_card(
        prompt, state=state, token="abcdefgh" if state == "pending" else None
    )
    rendered = "\n".join(_texts(build_confirmation_view(card, prompt)))
    assert (losses in rendered.replace(r"\_", "_")) == (state in {"pending", "approved"})


_INTAKE = str(uuid.UUID(int=7))
_IDS = {
    "company_id": "1" * 20,
    "contact_id": "2" * 20,
    "deal_id": "3" * 20,
    "discord_message_id": "4" * 20,
}


def _lead_prompt(evidence: str, **resolution: object) -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="resolve",
        server_name=DAIMON_SERVER_NAME,
        tool_name="lead_intake_resolve",
        input={
            "intake_id": _INTAKE,
            "resolution": {**_IDS, "separate_opportunity_confirmed": True, **resolution}
            | {"evidence": evidence},
        },
    )
    return prompt_for_tool_call(call, requester_platform_user_id="111", now=datetime.now(UTC))


def _lead_view(
    prompt: ConfirmationPrompt, state: ConfirmationCardState = "pending", *, allowed: bool = True
) -> discord.ui.LayoutView:
    async def lead_details(_prompt: ConfirmationPrompt, _clicker: str) -> tuple[str, ...]:
        return ("Live record evidence: " + "*_~`|>\\@everyone " * 200,) if allowed else ()

    card = build_confirmation_card(
        prompt,
        state=state,
        token="tok_abcdefgh" if state == "pending" else None,
        answered_by_platform_user_id=None if state == "pending" else "111",
    )
    return confirmation_module._ConfirmationView(  # pyright: ignore[reportPrivateUsage]
        card, prompt, None, lead_details=lead_details
    )


@pytest.mark.parametrize("state", ["pending", "approved", "denied"])
def test_a_lead_card_keeps_every_selection_visible_in_every_state(
    state: ConfirmationCardState,
) -> None:
    prompt = _lead_prompt("Verified with the sales lead on the call.")
    shown = "\n".join(_texts(_lead_view(prompt, state)))
    for value in (_INTAKE, *_IDS.values()):
        assert value in shown, (state, value)
    assert "Separate opportunity: yes" in shown and "Starter post absent: no" in shown
    assert "Verified with the sales lead" not in shown, "evidence stays behind Details"


async def test_lead_details_send_max_escape_heavy_evidence_in_bounded_pages() -> None:
    evidence = ("*_~`|>\\@everyone " * 200)[:2000]
    prompt = _lead_prompt(evidence)
    click = _interaction(222)
    await _buttons(_lead_view(prompt))[-1].callback(click)
    click.response.defer.assert_awaited_once()
    pages = [sent.args[0] for sent in click.followup.send.await_args_list]
    assert 1 < len(pages) <= confirmation_module.DETAILS_MAX_PAGES
    assert all(len(page) <= confirmation_module.DETAILS_PAGE_LIMIT for page in pages)
    assert all(not page.endswith("\\") or page.endswith("\\\\") for page in pages)
    assert prompt.detail_lines == ()
    assert "Live record evidence:" in pages[0]
    for sent in click.followup.send.await_args_list:
        assert sent.kwargs["ephemeral"] is True
        assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_details_past_the_page_cap_are_unavailable_rather_than_cut() -> None:
    assert confirmation_module.details_pages(("x" * 2000,) * 4) is not None
    assert confirmation_module.details_pages(("x" * 2000,) * 5) is None
    assert confirmation_module.details_pages(("\\" * 3000,)) is not None


async def test_lead_details_are_refused_to_a_clicker_who_cannot_read_the_lead() -> None:
    prompt = _lead_prompt("Private enquirer evidence that must stay hidden.")
    click = _interaction(333)
    await _buttons(_lead_view(prompt, allowed=False))[-1].callback(click)
    sent = [call.args[0] for call in click.followup.send.await_args_list]
    assert sent == [confirmation_module.LEAD_DETAILS_REFUSED_MESSAGE]
    click.response.send_message.assert_not_awaited()


async def test_lead_details_without_a_reader_check_are_refused() -> None:
    prompt = _lead_prompt("Private enquirer evidence that must stay hidden.")
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    click = _interaction(333)
    await _buttons(build_confirmation_view(card, prompt))[-1].callback(click)
    sent = [call.args[0] for call in click.followup.send.await_args_list]
    assert sent == [confirmation_module.LEAD_DETAILS_REFUSED_MESSAGE]


@pytest.mark.parametrize(
    "resolution", [{"company_id": "9" * 200}, {"deal_id": "1\n2"}, {"contact_id": "x" * 121}]
)
def test_a_lead_card_that_cannot_show_its_selections_is_refused(
    resolution: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="visible_lines"):
        _lead_prompt("Verified with the sales lead on the call.", **resolution)


async def test_lead_details_reload_on_every_click_and_ignore_cached_prompt_lines() -> None:
    prompt = _lead_prompt("Evidence supplied in the proposed resolution.")
    assert prompt.detail_lines == ()
    # Even an old serialized prompt cannot supply the details being returned.
    prompt = prompt.model_copy(update={"detail_lines": ("stale private evidence",)})
    load = AsyncMock(side_effect=[("live evidence v1",), ("live evidence v2",)])
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    view = confirmation_module._ConfirmationView(  # pyright: ignore[reportPrivateUsage]
        card, prompt, None, lead_details=load
    )
    for version in (1, 2):
        click = _interaction(222)
        await _buttons(view)[-1].callback(click)
        assert click.followup.send.await_args.args[0] == f"live evidence v{version}"
    assert load.await_count == 2


def test_an_approval_card_shows_the_call_exactly_even_when_it_names_another_app() -> None:
    """The one text left unscrubbed: the card must show what will run, not a reworded copy.

    A scrubbed card over an unscrubbed call would misstate what the person approves."""
    call = ToolCall(
        tool_use_id="tu_1",
        server_name="linear",
        tool_name="create_issue",
        input={"title": "Move the Slack bot to Teams", "description": "Not Discord"},
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="111", now=datetime.now(UTC))
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    view = build_confirmation_view(card, prompt)

    assert 'Create issue "Move the Slack bot to Teams"?' in _texts(view)[0]
    assert card.detail_lines == (
        "Title: Move the Slack bot to Teams",
        "Description: Not Discord",
    )


async def test_runtime_hook_binds_pin_cards_to_the_admitted_origin_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from daimon.core import confirmation
    from daimon.core.config import TurnSettings
    from mux.contracts.ids import ChannelRef

    tenant, account = uuid4(), uuid4()
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    channel = MagicMock(spec=discord.TextChannel)
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    admission = SimpleNamespace(origin_channel_id="1561246827452571728")
    discord_confirmation_hook(
        channel,
        runtime=runtime,
        admission=admission,  # pyright: ignore[reportArgumentType]
        tenant_id=tenant,
        account_id=account,
    )
    assert factory.call_args.kwargs["pin_channel"] == ChannelRef(
        tenant_id=str(tenant), platform="discord", channel_id="1561246827452571728"
    )
    discord_confirmation_hook(channel, runtime=runtime, tenant_id=tenant, account_id=account)
    assert factory.call_args.kwargs["pin_channel"] is None, "no admitted channel, no pin card"


async def test_a_card_still_open_replies_once_mentioning_only_its_requester(monkeypatch) -> None:
    """ROOT 2: a long card pings its requester once in the card's thread."""
    from daimon.core.posted_controls import lifecycle

    monkeypatch.setattr(lifecycle, "REMIND_AFTER_S", 0.02)
    monkeypatch.setattr(lifecycle, "REMIND_MIN_LEFT_S", 0.05)
    prompt = _prompt(expires_in=timedelta(seconds=0.3))
    message = SimpleNamespace(id=7, edit=AsyncMock(), reply=AsyncMock())
    channel = MagicMock()
    channel.send = AsyncMock(return_value=message)
    answer = await discord_confirmation_hook(channel)(prompt)
    assert answer == "expired"
    (call,) = message.reply.await_args_list
    assert call.args == (f"<@{prompt.requester_platform_user_id}>",)
    assert call.kwargs["embed"].description == "This is still waiting for your click."
    mentions = call.kwargs["allowed_mentions"].to_dict()
    assert mentions.get("parse") == ["users"] and call.kwargs["mention_author"] is False


def _dm_runtime_hook(
    monkeypatch: pytest.MonkeyPatch, *, client: object, guild: object
) -> tuple[MagicMock, AsyncMock]:
    """A runtime hook on a DM channel; returns the backend factory and the tenant lookup."""
    from uuid import uuid4

    from daimon.core import confirmation
    from daimon.core.config import TurnSettings

    runtime = MagicMock()
    runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)
    factory = MagicMock(return_value=(AsyncMock(), AsyncMock()))
    monkeypatch.setattr(confirmation, "backend_switch_callbacks", factory)
    lookup = AsyncMock(return_value=guild)
    monkeypatch.setattr(confirmation_module, "tenant_guild", lookup)
    discord_confirmation_hook(
        MagicMock(spec=discord.DMChannel),
        runtime=runtime,
        tenant_id=uuid4(),
        account_id=uuid4(),
        client=client,  # pyright: ignore[reportArgumentType]
    )
    return factory, lookup


async def test_dm_admin_card_checks_the_tenants_server_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Friction #36: a DM has no guild, so the admin re-check uses the turn's tenant server."""
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 999
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.roles = []
    member.guild_permissions = discord.Permissions(manage_guild=True)
    guild.fetch_member = AsyncMock(return_value=member)
    factory, lookup = _dm_runtime_hook(monkeypatch, client=MagicMock(), guild=guild)
    check = factory.call_args.kwargs["current_admin"]
    assert await check("111") is True
    member.guild_permissions = discord.Permissions.none()
    assert await check("111") is False
    assert guild.fetch_member.await_count == 2
    assert lookup.await_count == 2


async def test_dm_admin_card_without_a_client_or_server_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, lookup = _dm_runtime_hook(monkeypatch, client=None, guild=MagicMock())
    assert await factory.call_args.kwargs["current_admin"]("111") is False
    lookup.assert_not_awaited()
    factory, lookup = _dm_runtime_hook(monkeypatch, client=MagicMock(), guild=None)
    assert await factory.call_args.kwargs["current_admin"]("111") is False
    lookup.assert_awaited_once()


class _OneSession:
    def __call__(self) -> _OneSession:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.parametrize(
    ("platform", "external_id", "cached", "fetched", "expected"),
    [
        ("discord", "42", "cached", None, "cached"),
        ("discord", "42", None, "fetched", "fetched"),
        ("discord", "42", None, discord.NotFound, None),
        ("cli", "42", "cached", None, None),
        ("discord", "not-a-guild", "cached", None, None),
        (None, "42", "cached", None, None),
    ],
)
async def test_tenant_guild_resolves_only_a_discord_tenants_server(
    monkeypatch: pytest.MonkeyPatch,
    platform: str | None,
    external_id: str,
    cached: str | None,
    fetched: object,
    expected: str | None,
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from daimon.core.stores import tenants

    tenant = (
        None if platform is None else SimpleNamespace(platform=platform, external_id=external_id)
    )
    monkeypatch.setattr(tenants, "get_tenant", AsyncMock(return_value=tenant))
    client = MagicMock()
    client.get_guild = MagicMock(return_value=cached)
    if fetched is discord.NotFound:
        client.fetch_guild = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404, reason="nf"), "Unknown Guild")
        )
    else:
        client.fetch_guild = AsyncMock(return_value=fetched)
    got = await confirmation_module.tenant_guild(client, _OneSession(), uuid4())  # pyright: ignore[reportArgumentType]
    assert got == expected
    if expected is not None:
        client.get_guild.assert_called_once_with(42)


def test_an_admin_card_shows_its_details_once_without_a_details_button() -> None:
    """Friction #55: the details are inline on an admin card, so no button repeats them."""
    prompt = _prompt().model_copy(update={"admin": True, "detail_lines": ("Model: gpt-6-luna",)})
    view = build_confirmation_view(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt
    )
    assert [b.label for b in _buttons(view)] == ["Confirm", "Deny"]
    assert _texts(view).count("Model: gpt-6-luna") == 1

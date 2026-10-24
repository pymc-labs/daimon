"""The turn's outbound scrub: what a lifecycle is handed never names another chat app."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from daimon.core.turn.card_state import CardEvent, CardState, update, update_activity
from daimon.core.turn.lifecycle import TurnLifecycle, acknowledge, stop_rendering
from daimon.core.turn.outbound import (
    OtherAppsScrubbedLifecycle,
    scrub_turn_state,
    scrubbed_for,
)
from daimon.core.turn.run import _CHAT_RENDERED_ORIGINS  # pyright: ignore[reportPrivateUsage]
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState


def _text(text: str) -> TextBlock:
    return TextBlock(kind="text", text=text)


def _tool(name: str) -> ToolUseBlock:
    return ToolUseBlock(kind="tool_use", id="t1", type="agent.tool_use", name=name, input={})


class _Recorder:
    """A lifecycle that keeps what it was handed, with no optional hooks."""

    def __init__(self) -> None:
        self.rendered: list[TurnState] = []
        self.succeeded: list[TurnState] = []
        self.failed: list[tuple[TurnState, Exception]] = []
        self.events: list[object] = []

    async def on_render(self, state: TurnState) -> None:
        self.rendered.append(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        self.succeeded.append(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self.failed.append((state, err))

    async def on_sse_event(self, event: Any) -> None:
        self.events.append(event)

    async def on_reconnect(self, reason: str) -> None:
        self.events.append(reason)

    async def on_rate_limited(self, until: object) -> None:
        self.events.append(until)

    async def on_interrupt_sent(self, source: str) -> None:
        self.events.append(source)


def test_only_text_blocks_are_scrubbed_and_an_untouched_state_is_the_same_object() -> None:
    tool = _tool("discord_send_message")
    state = TurnState(content=[_text("Use Slack."), tool, _text("Use Discord.")])
    scrubbed = scrub_turn_state(state, "discord")
    assert [b.text for b in scrubbed.content if isinstance(b, TextBlock)] == [
        "Use another app.",
        "Use Discord.",
    ]
    assert scrubbed.content[1] is tool, "tool calls are not posted as written"
    assert state.content[0] == _text("Use Slack."), "the model's own state is not edited"

    plain = TurnState(content=[_text("Nothing to hide.")])
    assert scrub_turn_state(plain, "discord") is plain
    assert scrub_turn_state(state, None) is state


async def test_the_wrapper_hands_the_adapter_scrubbed_state_for_every_state_hook() -> None:
    inner = _Recorder()
    lifecycle = OtherAppsScrubbedLifecycle(inner, "teams")
    state = TurnState(content=[_text("Never upload to Discord or Slack.")])
    err = RuntimeError("boom")

    await lifecycle.on_render(state)
    await lifecycle.on_terminal_success(state)
    await lifecycle.on_terminal_failure(state, err)

    expected = [_text("Never upload to another app.")]
    assert inner.rendered[0].content == expected
    assert inner.succeeded[0].content == expected
    assert inner.failed[0][0].content == expected and inner.failed[0][1] is err


async def test_the_wrapper_passes_other_hooks_and_optional_hooks_through() -> None:
    inner = _Recorder()
    lifecycle = OtherAppsScrubbedLifecycle(inner, "slack")
    await lifecycle.on_sse_event("event")
    await lifecycle.on_reconnect("clean_close")
    await lifecycle.on_rate_limited(None)
    await lifecycle.on_interrupt_sent("sigint")
    assert inner.events == ["event", "clean_close", None, "sigint"]

    # No optional hooks on `inner`: none appear on the wrapper, so the driver skips them.
    await acknowledge(lifecycle, "accepted")
    stop_rendering(lifecycle)
    assert not hasattr(lifecycle, "on_provider_event")

    class WithHooks(_Recorder):
        stopped = False

        def on_render_stopped(self) -> None:
            self.stopped = True

    with_hooks = WithHooks()
    stop_rendering(OtherAppsScrubbedLifecycle(with_hooks, "slack"))
    assert with_hooks.stopped


async def test_a_recovery_lifecycle_is_scrubbed_too() -> None:
    first, second = _Recorder(), _Recorder()
    lifecycle, recovery = scrubbed_for("discord", first, lambda _cancel: second)
    state = TurnState(content=[_text("See slack.com")])
    await lifecycle.on_render(state)
    await recovery(asyncio.Event()).on_render(state)
    assert first.rendered[0].content == second.rendered[0].content == [_text("See another app")]


def test_a_caller_with_no_chat_platform_gets_its_lifecycle_back() -> None:
    inner: TurnLifecycle = _Recorder()

    def recover(_cancel: asyncio.Event) -> TurnLifecycle:
        return inner

    assert scrubbed_for("cli", inner, recover) == (inner, recover)


def test_a_worker_turn_is_not_scrubbed_because_a_model_reads_its_result() -> None:
    assert {"chat", "handoff"} == _CHAT_RENDERED_ORIGINS


def test_a_shared_card_scrubs_its_draft_and_tool_lines() -> None:
    card = CardState(platform="slack")
    card = update(card, CardEvent(kind="message", label="Asking Discord or Microsoft Teams"))
    assert card.text_preview == "Asking another app"
    card = update_activity(card, TurnState(content=[_tool("discord_search_messages")]))
    assert all("iscord" not in line for line in card.tool_lines)

    # With no platform on the card nothing is scrubbed.
    bare = update(CardState(), CardEvent(kind="message", label="Asking Discord"))
    assert bare.text_preview == "Asking Discord"


async def test_a_render_tick_scrubs_only_the_block_that_grew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.core.turn.outbound as outbound

    calls: list[str] = []
    real = outbound.scrub_other_platforms

    def spy(text: str, platform: str | None) -> str:
        calls.append(text)
        return real(text, platform)

    monkeypatch.setattr(outbound, "scrub_other_platforms", spy)
    inner = _Recorder()
    lifecycle = OtherAppsScrubbedLifecycle(inner, "teams")
    sealed = "A sealed answer about Slack. " * 50
    tool = _tool("bash")

    await lifecycle.on_render(TurnState(content=[_text(sealed), tool, _text("Dis")]))
    await lifecycle.on_render(TurnState(content=[_text(sealed), tool, _text("Discord is")]))
    await lifecycle.on_terminal_success(
        TurnState(content=[_text(sealed), tool, _text("Discord is done")])
    )

    assert calls == [sealed, "Dis", "Discord is", "Discord is done"]
    assert inner.succeeded[0].content[2] == _text("Another app is done")
    assert inner.rendered[1].content[0].text.count("another app") == 50  # type: ignore[union-attr]


async def test_a_failing_scrub_still_delivers_the_reply_with_the_names_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.core.turn.outbound as outbound

    def broken(*_args: object, **_kwargs: object) -> TurnState:
        raise RuntimeError("the reply text must not be logged")

    monkeypatch.setattr(outbound, "scrub_turn_state", broken)
    inner = _Recorder()
    lifecycle = OtherAppsScrubbedLifecycle(inner, "teams")
    state = TurnState(content=[_text("Ask DISCORD or Slack."), _tool("bash"), _text("Done.")])

    with structlog.testing.capture_logs() as logs:
        await lifecycle.on_render(state)
        await lifecycle.on_terminal_success(state)
        await lifecycle.on_terminal_failure(state, RuntimeError("x"))

    for delivered in (inner.rendered[0], inner.succeeded[0], inner.failed[0][0]):
        assert delivered.content == [
            _text("Ask another app or another app."),
            state.content[1],
            _text("Done."),
        ]
    assert [(log["event"], log["error_type"]) for log in logs] == [
        ("outbound_scrub.failed", "RuntimeError")
    ] * 3
    assert "must not be logged" not in str(logs)

"""Neutral drafts reach actual Discord renders without acquiring turn authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, NoReturn, cast

import discord
import mux.profiles as profiles
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsStreamSessionEvents,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.discord import lifecycle as discord_lifecycle
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.tool_confirmation import build_confirmation_view
from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.posted_controls.confirmation import (
    ConfirmationCardState,
    build_confirmation_card,
)
from daimon.core.turn import driver
from daimon.core.turn.io import TurnEvent, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.privacy import TurnPrivacy
from daimon.core.turn.provider_events import ProviderDraft
from daimon.core.turn.run import _DeferredFailureLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.status_log import status_log_override
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.receipts import StopObservation
from mux.contracts.usage import UsageObservation

NOW = datetime(2026, 10, 10, tzinfo=UTC)
Provider = Literal["openai", "gemini", "anthropic"]


def message(provider: Provider, text: str, *, revision: int = 1) -> Event:
    return Event(
        id=f"message-{revision}",
        session_id="session",
        sequence=revision,
        turn_id="root",
        type="agent.message",
        observed_at=NOW,
        authority="record",
        payload={
            "item_id": "item",
            "revision": revision,
            "content": [{"type": "text", "text": text}],
        },
        native=NativeProvenance(provider=provider, api_revision="test"),
    )


def preview(
    provider: Provider,
    text: str,
    *,
    sequence: int = 0,
    item: str = "item",
    index: int = 0,
) -> Event:
    return Event(
        id=f"preview-{item}-{index}-{sequence}",
        session_id="session",
        sequence=sequence,
        turn_id="root",
        type="agent.message.delta",
        observed_at=NOW,
        authority="preview",
        payload={
            "item_id": item,
            "content_index": index,
            "text": text,
            "preview_sequence": sequence,
        },
        native=NativeProvenance(provider=provider, api_revision="test"),
    )


@dataclass(frozen=True)
class MessageRef:
    id: int = 42


class Posts:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []
        self.ref = cast(discord.Message, MessageRef())

    async def send(self, *args: object, **kwargs: object) -> discord.Message:
        self.sent.append(kwargs)
        return self.ref

    async def edit(self, ref: discord.Message, *args: object, **kwargs: object) -> discord.Message:
        assert ref is self.ref
        self.edited.append(kwargs)
        return self.ref

    def lifecycle(self) -> DiscordTurnLifecycle:
        cancel = discord.ui.View()
        cancel.add_item(discord.ui.Button(label="Cancel"))
        return DiscordTurnLifecycle(
            send=self.send,
            edit=self.edit,
            agent_name="Agent",
            model_id="provider-model",
            cancel_view=cancel,
            clock=lambda: 1000.0,
        )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("emits_legacy_events", [False, True])
async def test_draft_and_lifecycle_follow_declared_profile_rendering_policy(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, emits_legacy_events: bool
) -> None:
    monkeypatch.setattr(
        profiles,
        "PROFILES",
        {
            key: profile.model_copy(update={"emits_legacy_events": emits_legacy_events})
            for key, profile in profiles.PROFILES.items()
        },
    )
    event = preview(provider, "declared draft")
    draft = ProviderDraft()
    assert draft.text(event) == (None if emits_legacy_events else "declared draft")
    assert draft.text(event.model_copy(update={"thread_id": "child"})) is None

    posts = Posts()
    lifecycle = posts.lifecycle()
    await lifecycle.on_provider_event(event)
    assert posts.sent == posts.edited == []
    assert lifecycle._state.text_preview == (None if emits_legacy_events else "declared draft")
    assert (lifecycle._provider_draft is None) == emits_legacy_events
    lifecycle._terminal = True
    await lifecycle.on_provider_event(message(provider, "late terminal draft"))
    assert lifecycle._state.text_preview == (None if emits_legacy_events else "declared draft")


async def test_default_managed_profile_keeps_neutral_drafts_out_of_legacy_rendering() -> None:
    event = preview("anthropic", "unused neutral draft")
    assert ProviderDraft().text(event) is None
    lifecycle = Posts().lifecycle()
    await lifecycle.on_provider_event(event)
    assert lifecycle._provider_draft is None
    assert lifecycle._state.text_preview is None


@pytest.mark.parametrize("log_on", [True, False])
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_a_new_provider_message_joins_the_card_log_whatever_the_switch(
    provider: Provider, log_on: bool
) -> None:
    with status_log_override(log_on):
        lifecycle = Posts().lifecycle()
        await lifecycle.on_provider_event(preview(provider, "first ", item="one"))
        await lifecycle.on_provider_event(preview(provider, "second", item="two"))
        await lifecycle.on_provider_event(preview(provider, " more", item="two", sequence=1))
    assert lifecycle._state.text_preview == "second more"
    assert lifecycle._state.earlier_drafts == ("first",), "the card logs either way"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_neutral_preview_is_local_then_snapshot_replaces_it(provider: Provider) -> None:
    posts = Posts()
    lifecycle = posts.lifecycle()
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "text", sequence=1))
    assert posts.sent == posts.edited == [], "the inline tap must never do chat I/O"
    assert lifecycle._state.text_preview == "draft text"
    await lifecycle.on_render(TurnState())
    assert posts.sent and posts.sent[0]["view"] is not None

    await lifecycle.on_provider_event(message(provider, "Authoritative"))
    await lifecycle.on_provider_event(preview(provider, "stale", sequence=2))
    await lifecycle.on_provider_event(message(provider, "older", revision=1))
    assert lifecycle._state.text_preview == "Authoritative"
    assert not lifecycle._terminal
    await lifecycle.on_provider_event(message(provider, "Revised", revision=2))
    assert lifecycle._state.text_preview == "Revised"
    await lifecycle.on_provider_event(message(provider, "", revision=3))
    assert lifecycle._state.text_preview is None


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_child_and_retired_preview_items_cannot_replace_root_draft(
    provider: Provider,
) -> None:
    lifecycle = Posts().lifecycle()
    await lifecycle.on_provider_event(preview(provider, "first", item="old"))
    await lifecycle.on_provider_event(preview(provider, "root"))
    await lifecycle.on_provider_event(preview(provider, "late", item="old", sequence=1))
    child = preview(provider, "child").model_copy(update={"thread_id": "child-thread"})
    await lifecycle.on_provider_event(child)
    assert lifecycle._state.text_preview == "root"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("outcome", ["completed", "interrupted", "error", "tool_only"])
async def test_actual_terminal_callbacks_retire_provider_progress_card(
    provider: Provider, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider totals must never be made into Anthropic usage")

    monkeypatch.setattr(discord_lifecycle, "cost_of_totals", no_sdk_meter)
    posts = Posts()
    lifecycle = posts.lifecycle()
    await lifecycle.post_initial()
    await lifecycle.on_provider_event(preview(provider, "speculative draft"))
    state = TurnState(
        content=[TextBlock(kind="text", text="Actual answer")],
        usage_totals=ProviderUsageTotals(input_tokens=100, output_tokens=50),
        termination=TerminationReason.INTERRUPTED
        if outcome == "interrupted"
        else TerminationReason.COMPLETED,
    )
    if outcome == "tool_only":
        state = TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use", id="call", type="agent.tool_use", name="bash", input={}
                )
            ],
            usage_totals=state.usage_totals,
            termination=TerminationReason.COMPLETED,
        )
    if outcome == "error":
        await lifecycle.on_terminal_failure(state, RuntimeError("Provider unavailable"))
    else:
        await lifecycle.on_terminal_success(state)
    assert lifecycle._terminal
    assert lifecycle._state.cost_str is None
    assert posts.edited and posts.edited[-1]["view"] is None
    before = (len(posts.sent), len(posts.edited))
    await lifecycle.on_provider_event(preview(provider, "late final draft", sequence=2))
    await lifecycle.on_render(state)
    assert before == (len(posts.sent), len(posts.edited)), "a finalized card cannot reopen"
    rendered = "\n".join(str(row.get("content", "")) for row in posts.sent + posts.edited)
    if outcome in {"completed", "interrupted"}:
        assert "Actual answer" in rendered
    assert "speculative draft" not in rendered


class Stream:
    def __init__(self, events: Sequence[TurnEvent]) -> None:
        self.events = iter(events)

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> TurnEvent:
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        pass


class Codec:
    def __init__(self, events: Sequence[TurnEvent]) -> None:
        self.events = events

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return Stream(self.events)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        pass

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        return []

    async def status(self) -> str:
        return "idle"

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return None

    async def archive(self) -> None:
        pass


def native_events() -> tuple[BetaManagedAgentsStreamSessionEvents, ...]:
    text = BetaManagedAgentsAgentMessageEvent(
        id="native-message",
        type="agent.message",
        content=[BetaManagedAgentsTextBlock(type="text", text="Actual answer")],
        processed_at=NOW,
    )
    done = BetaManagedAgentsSessionStatusIdleEvent(
        id="native-done",
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        processed_at=NOW,
    )
    return text, done


class HookLifecycle(RecordingLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.provider_events: list[Event] = []

    async def on_provider_event(self, event: Event) -> None:
        self.provider_events.append(event)


@pytest.mark.parametrize("provider", ["openai", "gemini", "anthropic"])
@pytest.mark.parametrize("has_hook", [True, False])
@pytest.mark.parametrize("wrapped", [True, False])
async def test_driver_and_recovery_wrapper_optional_neutral_tap_preserve_native_delivery(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, has_hook: bool, wrapped: bool
) -> None:
    text, done = native_events()
    normalized = message(provider, "Actual answer")
    delta = preview(provider, "preview")
    # The optional-native frame is already consumed by the driver and provider
    # usage tests; N4 owns its pending TurnEvent annotation update.
    preview_frame = cast(TurnEvent, SimpleNamespace(native=None, normalized=delta, usage=None))
    codec = Codec(
        [preview_frame, TurnEvent(text, normalized), TurnEvent(text, normalized), TurnEvent(done)]
    )

    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)
    lifecycle = HookLifecycle() if has_hook else RecordingLifecycle()
    final = await driver.run_turn(
        privacy=TurnPrivacy("test"),
        anthropic=cast(AsyncAnthropic, FakeAnthropic()),
        session_id="session",
        user_message="hello",
        lifecycle=_DeferredFailureLifecycle(lifecycle) if wrapped else lifecycle,
        cancel=asyncio.Event(),
        billing=BillingExempt("headless-unrecorded"),
        render_interval_s=0.001,
        now=lambda: NOW,
    )
    assert final.termination == TerminationReason.COMPLETED
    assert lifecycle.sse_events == [text, done]
    if isinstance(lifecycle, HookLifecycle):
        assert lifecycle.provider_events == ([] if provider == "anthropic" else [delta, normalized])


@pytest.mark.parametrize("with_usage", [True, False])
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_actual_driver_to_discord_surface_finalizes_without_synthetic_meter(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, with_usage: bool
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider observation is not an Anthropic meter")

    monkeypatch.setattr(discord_lifecycle, "cost_of_totals", no_sdk_meter)
    text, done = native_events()
    observation = UsageObservation(
        id="usage",
        revision=1,
        session=ResourceRef(
            id="session", kind="session", provider=provider, account_scope_id="account"
        ),
        grain="turn",
        basis="cumulative",
        input_tokens=10,
        output_tokens=100,
        completeness="partial",
        observed_at=NOW,
    )
    codec = Codec(
        [
            TurnEvent(
                text, message(provider, "Actual answer"), observation if with_usage else None
            ),
            TurnEvent(done),
        ]
    )

    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)
    posts = Posts()
    lifecycle = posts.lifecycle()
    final = await driver.run_turn(
        privacy=TurnPrivacy("test"),
        anthropic=cast(AsyncAnthropic, FakeAnthropic()),
        session_id="session",
        user_message="hello",
        lifecycle=_DeferredFailureLifecycle(lifecycle),
        cancel=asyncio.Event(),
        billing=BillingExempt("headless-unrecorded"),
        render_interval_s=0.001,
        now=lambda: NOW,
    )
    if with_usage:
        assert isinstance(final.usage_totals, ProviderUsageTotals)
        assert final.usage_totals.observations == (observation,)
    else:
        assert not isinstance(final.usage_totals, ProviderUsageTotals)
    assert lifecycle._provider_draft is not None
    assert lifecycle._terminal and lifecycle._state.cost_str is None
    assert posts.edited[-1]["view"] is None
    assert any(row.get("content") == "Actual answer" for row in posts.sent + posts.edited)


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired"])
async def test_provider_prompt_keeps_only_existing_confirmation_controls(
    state: ConfirmationCardState,
) -> None:
    prompt = ConfirmationPrompt(
        title="Confirm this action?",
        requester_platform_user_id="123",
        expires_at=NOW,
    )
    card = build_confirmation_card(
        prompt, state=state, token="pending-card" if state == "pending" else None
    )
    view = build_confirmation_view(card, prompt)
    labels = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert labels == (["Approve", "Deny", "Details"] if state == "pending" else [])

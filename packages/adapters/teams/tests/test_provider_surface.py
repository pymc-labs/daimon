"""Neutral Teams drafts stay local and real terminal sends retire the card."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, NoReturn, cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsStreamSessionEvents,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.teams import lifecycle as teams_lifecycle
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.adapters.teams.tool_confirmation import confirmation_adaptive_card
from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.posted_controls.confirmation import ConfirmationCardState, build_confirmation_card
from daimon.core.turn import driver
from daimon.core.turn.io import TurnEvent, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.run import _DeferredFailureLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.turn_fakes import FakeAnthropic
from mux import profiles
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.receipts import StopObservation
from mux.contracts.usage import UsageObservation

from .conftest import CONVERSATION_ID, SERVICE_URL, FakeSender

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


class Posts:
    def __init__(self) -> None:
        self.sender = FakeSender()
        self.now = 100.0

    @property
    def rendered(self) -> str:
        return self.sender.activities[-1].model_dump_json(by_alias=True)

    def lifecycle(self, *, unprompted: bool = False) -> TeamsTurnLifecycle:
        return TeamsTurnLifecycle(
            sender=self.sender,
            conversation_id=CONVERSATION_ID,
            service_url=SERVICE_URL,
            cancel_key="key-1",
            agent_name="Agent",
            model_id="provider-model",
            clock=lambda: self.now,
            unprompted=unprompted,
        )


async def test_normalized_legacy_event_does_not_displace_sdk_rendering() -> None:
    posts = Posts()
    lifecycle = posts.lifecycle()
    before = len(posts.sender.activities)
    await lifecycle.on_provider_event(message("anthropic", "Normalized duplicate"))
    assert lifecycle._provider_draft is None
    assert len(posts.sender.activities) == before
    native, _ = native_events()
    await lifecycle.on_sse_event(cast(RawMessageStreamEvent, native))
    await lifecycle.post_initial()
    assert "Actual answer" in posts.rendered


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_neutral_preview_is_local_then_snapshot_replaces_it(provider: Provider) -> None:
    posts = Posts()
    lifecycle = posts.lifecycle()
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "text", sequence=1))
    assert not posts.sender.sent, "the inline tap must never do Teams I/O"
    assert lifecycle._state.draft == "draft text"
    await lifecycle.post_initial()
    assert "draft text" in posts.rendered and "cancel_turn" in posts.rendered
    await lifecycle.on_provider_event(message(provider, "Authoritative"))
    await lifecycle.on_provider_event(preview(provider, "stale", sequence=2))
    await lifecycle.on_provider_event(message(provider, "older", revision=1))
    text, _ = native_events()
    await lifecycle.on_sse_event(cast(RawMessageStreamEvent, text))
    assert lifecycle._state.draft == "Authoritative"
    posts.now += 6
    await lifecycle.on_render(TurnState())
    assert "Authoritative" in posts.rendered
    await lifecycle.on_provider_event(message(provider, "Revised", revision=2))
    assert lifecycle._state.draft == "Revised"
    await lifecycle.on_provider_event(message(provider, "", revision=3))
    assert lifecycle._state.draft is None


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
    assert lifecycle._state.draft == "root"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("outcome", ["completed", "interrupted", "error", "tool_only", "empty"])
async def test_actual_terminal_callbacks_retire_provider_progress_card(
    provider: Provider, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider totals must never be made into Anthropic usage")

    monkeypatch.setattr(teams_lifecycle, "cost_of_totals", no_sdk_meter)
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
    elif outcome == "empty":
        state = TurnState(usage_totals=state.usage_totals, termination=TerminationReason.COMPLETED)
    if outcome == "error":
        await lifecycle.on_terminal_failure(state, RuntimeError("Provider unavailable"))
    else:
        await lifecycle.on_terminal_success(state)
    assert lifecycle._terminal and lifecycle.card_closed
    assert "cancel_turn" not in posts.rendered
    assert "speculative draft" not in posts.rendered
    before = len(posts.sender.sent)
    await lifecycle.on_provider_event(preview(provider, "late final draft", sequence=2))
    posts.now += 6
    await lifecycle.on_render(state)
    assert before == len(posts.sender.sent), "a finalized card cannot reopen"
    if outcome in {"completed", "interrupted"}:
        assert any("Actual answer" in (activity.text or "") for activity in posts.sender.activities)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_unprompted_provider_draft_stays_silent_until_authoritative_answer(
    provider: Provider,
) -> None:
    posts = Posts()
    lifecycle = posts.lifecycle(unprompted=True)
    await lifecycle.post_initial()
    await lifecycle.on_provider_event(preview(provider, "speculative draft"))
    posts.now += 6
    await lifecycle.on_render(TurnState())
    assert not posts.sender.sent
    await lifecycle.on_terminal_success(
        TurnState(
            content=[TextBlock(kind="text", text="Actual answer")],
            usage_totals=ProviderUsageTotals(),
        )
    )
    assert lifecycle.card_closed
    assert posts.sender.activities[0].text == "Actual answer"


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired"])
def test_provider_prompt_keeps_only_existing_confirmation_controls(
    state: ConfirmationCardState,
) -> None:
    prompt = ConfirmationPrompt(
        title="Confirm this action?", requester_platform_user_id="123", expires_at=NOW
    )
    card = build_confirmation_card(
        prompt, state=state, token="pending-card" if state == "pending" else None
    )
    rendered = confirmation_adaptive_card(card, prompt).model_dump_json(by_alias=True)
    assert ('"title":"Approve"' in rendered) == (state == "pending")
    assert ('"title":"Deny"' in rendered) == (state == "pending")
    assert "browser_origin_access" not in rendered and "browser_authentication" not in rendered


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


@pytest.mark.parametrize("with_usage", [True, False])
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_actual_driver_to_teams_surface_finalizes_without_synthetic_meter(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, with_usage: bool
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider observation is not an Anthropic meter")

    monkeypatch.setattr(teams_lifecycle, "cost_of_totals", no_sdk_meter)
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
    assert lifecycle._terminal and lifecycle.card_closed
    assert "cancel_turn" not in posts.rendered
    assert any(activity.text == "Actual answer" for activity in posts.sender.activities)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_rendering_follows_declared_capability_without_network_io(
    monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    posts = Posts()
    for emits_legacy in (True, False):
        monkeypatch.setattr(
            profiles,
            "PROFILES",
            {
                pid: profile.model_copy(update={"emits_legacy_events": emits_legacy})
                if profile.provider == provider
                else profile
                for pid, profile in profiles.PROFILES.items()
            },
        )
        lifecycle = posts.lifecycle()
        await lifecycle.on_provider_event(message(provider, "Declared routing"))
        assert (lifecycle._provider_draft is None) is emits_legacy
        assert len(posts.sender.activities) == 0

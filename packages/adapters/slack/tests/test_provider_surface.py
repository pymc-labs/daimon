"""Neutral Slack drafts stay local; real SDK requests retire terminal cards."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, NoReturn, Protocol, cast

import pytest
import yarl
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
from daimon.adapters.slack import lifecycle as slack_lifecycle
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.tool_confirmation import _attachment
from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.posted_controls.confirmation import ConfirmationCardState, build_confirmation_card
from daimon.core.turn import driver
from daimon.core.turn.io import TurnEvent, TurnStream
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.run import _DeferredFailureLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.status_log import status_log_override
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.turn_fakes import FakeAnthropic
from mux import profiles
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.receipts import StopObservation
from mux.contracts.usage import UsageObservation

from .conftest import FakeSlackWebClient

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


class RecordedCall(Protocol):
    # aioresponses leaves its recorded kwargs untyped.
    kwargs: dict[str, object]


class Posts:
    def __init__(self, fake: FakeSlackWebClient) -> None:
        self.fake = fake
        self.now = 100.0
        self.registered: list[str] = []
        self.deregistered: list[str] = []

    def requests(self, method: str) -> list[dict[str, object]]:
        calls = self.fake.mock.requests.get(
            ("POST", yarl.URL(f"https://slack.com/api/{method}")), []
        )
        return [cast(dict[str, object], cast(RecordedCall, call).kwargs["json"]) for call in calls]

    @property
    def updated(self) -> list[dict[str, object]]:
        return self.requests("chat.update")

    @property
    def count(self) -> int:
        return sum(len(calls) for calls in self.fake.mock.requests.values())

    def register(self, key: str, cancel: asyncio.Event, author: str) -> None:
        self.registered.append(key)

    def lifecycle(self) -> SlackTurnLifecycle:
        return SlackTurnLifecycle(
            client=self.fake.client,
            channel="C_TEST",
            thread_ts="1000000000.0",
            cancel=asyncio.Event(),
            author_id="U_TEST",
            agent_name="Agent",
            model_id="provider-model",
            register=self.register,
            deregister=self.deregistered.append,
            clock=lambda: self.now,
        )


async def test_normalized_legacy_event_does_not_displace_sdk_rendering(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    posts = Posts(fake_slack_web_client)
    lifecycle = posts.lifecycle()
    before = posts.count
    await lifecycle.on_provider_event(message("anthropic", "Normalized duplicate"))
    assert lifecycle._provider_draft is None
    assert posts.count == before
    native, _ = native_events()
    await lifecycle.on_sse_event(cast(RawMessageStreamEvent, native))
    await lifecycle.post_initial()
    assert "Actual answer" in str(posts.requests("chat.postMessage"))


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_neutral_preview_is_local_then_snapshot_replaces_it(
    provider: Provider, fake_slack_web_client: FakeSlackWebClient
) -> None:
    posts = Posts(fake_slack_web_client)
    lifecycle = posts.lifecycle()
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "draft "))
    await lifecycle.on_provider_event(preview(provider, "text", sequence=1))
    assert posts.count == 0, "the inline tap must never do Slack I/O"
    assert lifecycle._state.text_preview == "draft text"
    await lifecycle.post_initial()
    assert "draft text" in str(posts.requests("chat.postMessage"))
    await lifecycle.on_provider_event(message(provider, "Authoritative"))
    await lifecycle.on_provider_event(preview(provider, "stale", sequence=2))
    await lifecycle.on_provider_event(message(provider, "older", revision=1))
    # Compatibility native payloads cannot overwrite a provider's normalized draft.
    text, _ = native_events()
    await lifecycle.on_sse_event(cast(RawMessageStreamEvent, text))
    assert lifecycle._state.text_preview == "Authoritative"
    posts.now += 6
    await lifecycle.on_render(TurnState())
    assert "Authoritative" in str(posts.updated[-1])
    await lifecycle.on_provider_event(message(provider, "Revised", revision=2))
    assert lifecycle._state.text_preview == "Revised"
    await lifecycle.on_provider_event(message(provider, "", revision=3))
    assert lifecycle._state.text_preview is None


@pytest.mark.parametrize("log_on", [True, False])
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_a_new_provider_message_joins_the_card_log_whatever_the_switch(
    provider: Provider, log_on: bool, fake_slack_web_client: FakeSlackWebClient
) -> None:
    with status_log_override(log_on):
        lifecycle = Posts(fake_slack_web_client).lifecycle()
        await lifecycle.on_provider_event(preview(provider, "first ", item="one"))
        await lifecycle.on_provider_event(preview(provider, "second", item="two"))
        await lifecycle.on_provider_event(preview(provider, " more", item="two", sequence=1))
    assert lifecycle._state.text_preview == "second more"
    assert lifecycle._state.earlier_drafts == ("first",), "the card logs either way"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_child_and_retired_preview_items_cannot_replace_root_draft(
    provider: Provider, fake_slack_web_client: FakeSlackWebClient
) -> None:
    lifecycle = Posts(fake_slack_web_client).lifecycle()
    await lifecycle.on_provider_event(preview(provider, "first", item="old"))
    await lifecycle.on_provider_event(preview(provider, "root"))
    await lifecycle.on_provider_event(preview(provider, "late", item="old", sequence=1))
    child = preview(provider, "child").model_copy(update={"thread_id": "child-thread"})
    await lifecycle.on_provider_event(child)
    assert lifecycle._state.text_preview == "root"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("outcome", ["completed", "interrupted", "error", "tool_only", "empty"])
async def test_actual_terminal_callbacks_retire_provider_progress_card(
    provider: Provider,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider totals must never be made into Anthropic usage")

    monkeypatch.setattr(slack_lifecycle, "cost_of_totals", no_sdk_meter)
    posts = Posts(fake_slack_web_client)
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
    assert lifecycle._terminal and lifecycle._state.cost_str is None
    assert posts.updated and "cancel_turn" not in str(posts.updated[-1])
    assert posts.deregistered == ["1000000000.000001"]
    before = posts.count
    await lifecycle.on_provider_event(preview(provider, "late final draft", sequence=2))
    posts.now += 6
    await lifecycle.on_render(state)
    assert before == posts.count, "a finalized card cannot reopen"
    rendered = str(posts.updated[-1])
    if outcome in {"completed", "interrupted"}:
        assert "Actual answer" in rendered
    assert "speculative draft" not in rendered


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "expired"])
def test_provider_prompt_keeps_only_existing_confirmation_controls(
    state: ConfirmationCardState,
) -> None:
    prompt = ConfirmationPrompt(
        title="Confirm this action?", requester_platform_user_id="U_TEST", expires_at=NOW
    )
    card = build_confirmation_card(
        prompt, state=state, token="pending-card" if state == "pending" else None
    )
    rendered = json.dumps(_attachment(card, prompt))
    assert ('"text": "Approve"' in rendered) == (state == "pending")
    assert ('"text": "Deny"' in rendered) == (state == "pending")
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
async def test_actual_driver_to_slack_surface_finalizes_without_synthetic_meter(
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    with_usage: bool,
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    def no_sdk_meter(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider observation is not an Anthropic meter")

    monkeypatch.setattr(slack_lifecycle, "cost_of_totals", no_sdk_meter)
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
    posts = Posts(fake_slack_web_client)
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
    assert lifecycle._terminal and lifecycle._state.cost_str is None
    assert "cancel_turn" not in str(posts.updated[-1])
    assert "Actual answer" in str(posts.updated[-1])
    assert posts.deregistered == ["1000000000.000001"]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_rendering_follows_declared_capability_without_network_io(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, fake_slack_web_client: FakeSlackWebClient
) -> None:
    posts = Posts(fake_slack_web_client)
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
        assert posts.count == 0

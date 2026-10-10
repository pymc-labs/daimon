"""Native observation billing remains independent of display IDs and SDK meters."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.turn import driver
from daimon.core.turn.io import LegacyTurnIO, TurnEvent, TurnStream
from daimon.core.usage_billing import ObservationBilled
from daimon.testing.ma_models import ma_model_usage
from daimon.testing.turn_fakes import BlockForever, FakeAnthropic, RecordingLifecycle, YieldEvent
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.receipts import StopObservation
from mux.contracts.usage import UsageObservation

from .conftest import make_agent_message, make_end_turn, make_status_idle


def usage(provider: Literal["openai", "gemini"], revision: int = 1) -> UsageObservation:
    return UsageObservation(
        id="native-usage",
        revision=revision,
        grain="turn",
        basis="cumulative",
        session=ResourceRef(
            id="sess_1",
            kind="session",
            provider=provider,
            account_scope_id="workspace",
            tenant_id="tenant",
        ),
        model=ModelRef(provider=provider, id="model"),
        input_tokens=10,
        input_cached_tokens=2,
        output_tokens=100 + revision,
        native_meter={"actual_provider_meter": revision},
        completeness="partial",
        observed_at=datetime(2026, 10, 10, tzinfo=UTC),
    )


class NativeStream:
    def __init__(self, source: TurnStream, live: deque[UsageObservation], pure_only: bool) -> None:
        self.source, self.live = source, live
        self.pure_only = pure_only

    def __aiter__(self) -> NativeStream:
        return self

    async def __anext__(self) -> TurnEvent:
        item = await self.source.__anext__()
        if self.pure_only and self.live:
            # N4 owns TurnEvent's optional native field declaration. Exercise
            # that runtime contract without constructing any SDK meter.
            return cast(
                TurnEvent, SimpleNamespace(native=None, normalized=None, usage=self.live.popleft())
            )
        return TurnEvent(item.native, item.normalized, self.live.popleft() if self.live else None)

    async def close(self) -> None:
        await self.source.close()


class Codec:
    def __init__(self, fake: FakeAnthropic, observations: Sequence[UsageObservation]) -> None:
        self.source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")
        self.live = deque(observations)
        self.reconciled = list(observations[-1:])
        self.opened = asyncio.Event()
        self.fetches = 0
        self.fail_open = False
        self.pure_only = False

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        if self.fail_open:
            raise RuntimeError("codec failure")
        stream = await self.source.open_stream(read_timeout_s=read_timeout_s)
        self.opened.set()
        return NativeStream(stream, self.live, self.pure_only)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self.source.send(events)

    async def status(self) -> str:
        return await self.source.status()

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        return await self.source.replay(timeout_s=timeout_s)

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return await self.source.interrupt(timeout_s=timeout_s)

    async def archive(self) -> None:
        await self.source.archive()

    async def replay_usage(self) -> Sequence[UsageObservation]:
        self.fetches += 1
        return self.reconciled


def install(monkeypatch: pytest.MonkeyPatch, codec: Codec) -> None:
    def factory(*args: object, **kwargs: object) -> Codec:
        return codec

    monkeypatch.setattr(driver, "turn_io", factory)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_revisions_survive_duplicate_display_ids_and_postrun_replay(
    monkeypatch: pytest.MonkeyPatch, provider: Literal["openai", "gemini"]
) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="same-display-id", text="hello")),
            YieldEvent(make_agent_message(event_id="same-display-id", text="hello")),
            YieldEvent(make_status_idle(event_id="terminal", stop_reason=make_end_turn())),
        ]
    ]
    codec = Codec(fake, [usage(provider), usage(provider, 2)])
    install(monkeypatch, codec)
    recorded: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        recorded.append(observation)
        return True

    final = await driver.run_turn(
        anthropic=cast(AsyncAnthropic, fake),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=ObservationBilled(record),
    )
    assert {value.revision for value in recorded} == {1, 2}
    assert final.usage_totals.output_tokens == 102
    assert codec.fetches == 1
    assert len(final.content) == 1 and final.content[0].kind == "text"
    assert all(value.input_cache_write_tokens is None for value in recorded)


async def test_pending_same_revision_is_retried_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_status_idle(event_id="terminal", stop_reason=make_end_turn())),
        ]
    ]
    codec = Codec(fake, [usage("openai")])
    install(monkeypatch, codec)
    recorded: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        recorded.append(observation)
        return len(recorded) > 1

    final = await driver.run_turn(
        anthropic=cast(AsyncAnthropic, fake),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=ObservationBilled(record),
    )
    assert len(recorded) == 2 and recorded[0] == recorded[1]
    assert final.usage_totals.output_tokens == 101


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_or_cancelled_pump_fetches_native_usage(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[BlockForever()]]
    codec = Codec(fake, [usage("openai")])
    codec.fail_open = not cancelled
    install(monkeypatch, codec)
    recorded: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        recorded.append(observation)
        return False  # captured durably, actual infrastructure still unknown

    task = asyncio.create_task(
        driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=ObservationBilled(record),
        )
    )
    if cancelled:
        await asyncio.wait_for(codec.opened.wait(), 2)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await task
    assert codec.fetches == 1 and recorded == codec.reconciled


async def test_billed_codec_without_reconciliation_refuses_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    source = LegacyTurnIO(cast(AsyncAnthropic, fake), "sess_1")

    def factory(*args: object, **kwargs: object) -> LegacyTurnIO:
        return source

    monkeypatch.setattr(driver, "turn_io", factory)

    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("must not bill")

    with pytest.raises(ValueError, match="before sending"):
        await driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_1",
            user_message="hi",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=ObservationBilled(record),
        )
    assert not fake.beta.sessions.events.sent_events


async def test_usage_only_native_frame_is_folded_without_an_sdk_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="ignored-display", text="unused")),
            YieldEvent(make_status_idle(event_id="terminal", stop_reason=make_end_turn())),
        ]
    ]
    codec = Codec(fake, [usage("gemini")])
    codec.pure_only = True
    install(monkeypatch, codec)
    recorded: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        recorded.append(observation)
        return True

    final = await driver.run_turn(
        anthropic=cast(AsyncAnthropic, fake),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=ObservationBilled(record),
    )
    assert final.usage_totals.output_tokens == 101 and final.content == []
    assert recorded and all(value == usage("gemini") for value in recorded)


async def test_neutral_billing_refuses_an_anthropic_span_meter() -> None:
    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("cannot bill a synthetic meter")

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="fake-meter",
        type="span.model_request_end",
        model_request_start_id="start",
        processed_at=datetime(2026, 10, 10, tzinfo=UTC),
        model_usage=ma_model_usage(),
    )
    with pytest.raises(ValueError, match="synthetic Anthropic meter"):
        await driver._bill_once(ObservationBilled(record), event, set())

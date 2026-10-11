"""Wire pause identities cannot substitute for accepted native/render evidence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from daimon.testing.anthropic_fixtures import (
    anthropic_replay,
    anthropic_turn_tape,
    load_anthropic_fixtures,
)
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import (
    Backend,
    GateWait,
    Object,
    WireFrame,
    WireReplay,
    WireReply,
    provider_replay,
)
from mux.drivers.anthropic.transport import LegacyTurnTransport
from mux.drivers.gemini.transport import SDKTransport as GeminiTransport
from mux.drivers.gemini.transport import close_iterator
from mux.drivers.openai.transport import SDKTransport as OpenAITransport

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "packages/testing/fixtures/target53"
NATIVE = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
ANTHROPIC = load_anthropic_fixtures(DATA / "anthropic/index.json", source_root=ROOT)
SCENARIO = "QA-D6-LONG-TURN-CARD-LIFECYCLE"


@dataclass(frozen=True)
class Script:
    replies: tuple[WireReply, ...]


def wire(*, path: str = "/events", gate: str = "complete") -> WireReplay:
    return WireReplay(
        Script(
            replies=(
                WireReply(
                    id="events",
                    method="GET",
                    path=path,
                    frames=(
                        WireFrame(payload={"type": "prefix"}),
                        WireFrame(payload={"type": "tail"}, after_gate=gate),
                    ),
                ),
            )
        )
    )


async def consume(http: httpx.AsyncClient, path: str, seen: list[str]) -> None:
    async with http.stream("GET", path) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                seen.append(line)


@pytest.mark.asyncio
async def test_actual_iterator_pause_has_exact_reply_and_frame_identity() -> None:
    replay = wire()
    seen: list[str] = []
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        consumer = asyncio.create_task(consume(http, "/events", seen))
        try:
            waits = await asyncio.wait_for(replay.wait_blocked("complete"), timeout=5)
            assert waits == (GateWait(gate="complete", reply_id="events", frame_index=1),)
            assert replay.held("complete") == waits
            assert seen == ['data: {"type": "prefix"}']
            assert not consumer.done()
            # Serialized pause identity is stable, not an object-identity token.
            assert GateWait.model_validate_json(waits[0].model_dump_json()) == waits[0]
            replay.release("complete")
            assert replay.held("complete") == ()
            await consumer
            assert seen == ['data: {"type": "prefix"}', 'data: {"type": "tail"}']
            replay.assert_consumed()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_iterator_invalidates_past_hold_and_drains_gate_waiters() -> None:
    replay = wire()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        consumer = asyncio.create_task(consume(http, "/events", []))
        await asyncio.wait_for(replay.wait_blocked("complete"), timeout=5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert replay.held("complete") == ()
        with pytest.raises(ValueError, match="no longer held"):
            await replay.wait_blocked("complete")
        with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
            replay.assert_consumed()


@pytest.mark.asyncio
async def test_closed_response_is_not_a_current_hold() -> None:
    replay = wire()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        async with http.stream("GET", "/events") as response:
            consumer = asyncio.create_task(response.aread())
            await asyncio.wait_for(replay.wait_blocked("complete"), timeout=5)
            await response.aclose()
            assert replay.held("complete") == ()
            with pytest.raises(ValueError, match="no longer held"):
                await replay.wait_blocked("complete")
            await consumer
        with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
            replay.assert_consumed()


@pytest.mark.asyncio
async def test_cancelled_observer_does_not_release_or_cancel_actual_stream() -> None:
    replay = wire()
    started = asyncio.Event()

    async def observe() -> tuple[GateWait, ...]:
        started.set()
        return await replay.wait_blocked("complete")

    observer = asyncio.create_task(observe())
    await started.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert not replay.gate("complete").is_set()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        consumer = asyncio.create_task(consume(http, "/events", []))
        try:
            await asyncio.wait_for(replay.wait_blocked("complete"), timeout=5)
            assert not consumer.done()
            replay.release("complete")
            await consumer
            replay.assert_consumed()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("complete", "other"))
async def test_never_opened_stream_is_not_a_hold(name: str) -> None:
    replay = wire()
    assert replay.held(name) == ()
    if name == "other":
        with pytest.raises(ValueError, match="no scripted frame"):
            await replay.wait_blocked(name)
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(replay.wait_blocked(name), timeout=0)
    assert replay.requests == []
    assert not replay.gate("complete").is_set()


@pytest.mark.asyncio
async def test_released_before_observation_refuses_instead_of_crediting_completion() -> None:
    replay = wire()
    replay.release("complete")
    with pytest.raises(ValueError, match="already released"):
        await replay.wait_blocked("complete")
    assert replay.held("complete") == ()
    assert replay.requests == []


@pytest.mark.asyncio
async def test_unrelated_held_gate_does_not_satisfy_requested_gate() -> None:
    replay = wire(gate="other")
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        consumer = asyncio.create_task(consume(http, "/events", []))
        try:
            await asyncio.wait_for(replay.wait_blocked("other"), timeout=5)
            assert replay.held("complete") == ()
            with pytest.raises(ValueError, match="no scripted frame"):
                await replay.wait_blocked("complete")
            assert not consumer.done()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


class NamesOnly(Mapping[str, str]):
    """Synthetic settings: any attempt to fetch their value is a test failure."""

    def __iter__(self) -> Iterator[str]:
        return iter(("ANTHROPIC_FUTURE_GATE_SETTING",))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key != "ANTHROPIC_FUTURE_GATE_SETTING":
            raise KeyError(key)
        raise AssertionError("gate observation read an environment value")


async def current_hold(wire: WireReplay, gate: str, expected: GateWait) -> None:
    waits = await asyncio.wait_for(wire.wait_blocked(gate), timeout=5)
    # Keep the critical receipt checks active when Python disables assertions.
    if waits != (expected,) or wire.held(gate) != waits:
        raise AssertionError("wire hold belongs to a different reply/frame/gate")
    restored = GateWait.model_validate_json(waits[0].model_dump_json())
    if restored != expected:
        raise AssertionError("wire hold identity changed across JSON roundtrip")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize("cancel", (False, True))
async def test_owned_native_sdk_stream_reaches_gate_and_closes_without_env_reads(
    backend: Backend, cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario, fixture = NATIVE.select(SCENARIO, backend)
    turn = fixture.turns[0]
    gate = turn.completion_gate
    if gate is None:
        raise AssertionError("source recipe lost its authored completion gate")
    tape = turn_tape(scenario, fixture, turn, key="gate-control")
    reply = next(r for r in tape.replies if r.frames is not None)
    index = next(i for i, f in enumerate(reply.frames or ()) if f.after_gate == gate)
    async with provider_replay(
        tape,
        backend=backend,
        profile=fixture.profile,
        model=fixture.model,
        source_root=DATA / "catalog",
    ) as replay:
        edge = replay.transport
        if isinstance(edge, OpenAITransport):
            path = f"/agents/sessions/{turn.session_id}"
            await edge.request("GET", path)
            await edge.request("POST", path + "/events", body=turn.request, key="gate-control")
            stream = await edge.open_stream(path + "/events")
        else:
            await edge.create(turn.request)
            stream = await edge.open_stream(turn.root_id)
        seen: list[Object] = []

        async def collect() -> None:
            async for frame in stream:
                seen.append(frame)

        task = asyncio.create_task(collect())
        try:
            await current_hold(
                replay.wire,
                gate,
                GateWait(
                    gate=gate,
                    reply_id=reply.id,
                    frame_index=index,
                ),
            )
            if seen != list(turn.frames[:index]) or task.done():
                raise AssertionError("SDK delivered gated native content before release")
            # No observer API may inspect settings introduced after admission.
            with monkeypatch.context() as scoped:
                scoped.setattr(os, "environ", NamesOnly())
                await current_hold(
                    replay.wire,
                    gate,
                    GateWait(
                        gate=gate,
                        reply_id=reply.id,
                        frame_index=index,
                    ),
                )
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    with pytest.raises(ValueError, match="no longer held"):
                        await replay.wire.wait_blocked(gate)
                else:
                    replay.wire.release(gate)
                    await asyncio.wait_for(task, timeout=5)
                if replay.wire.held(gate):
                    raise AssertionError("finished SDK stream retained a current hold")
            await close_iterator(stream)
            if isinstance(edge, GeminiTransport):
                await edge.get(turn.root_id)
            if cancel:
                with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
                    replay.wire.assert_consumed()
            else:
                if seen != list(turn.frames):
                    raise AssertionError("SDK stream altered authored native frames")
                replay.wire.assert_consumed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await close_iterator(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", (False, True))
async def test_owned_anthropic_sdk_gate_retains_tool_start_and_blocks_result(
    cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, scenario = ANTHROPIC.select(SCENARIO)
    turn, binding = scenario.turns[0], entry.bindings[0]
    gate = binding.completion_gate
    if gate is None:
        raise AssertionError("Anthropic recipe lost its authored completion gate")
    tape = anthropic_turn_tape(entry, scenario, turn, codec=ANTHROPIC.index.codec)
    reply = next(r for r in tape.replies if r.frames is not None)
    index = next(i for i, f in enumerate(reply.frames or ()) if f.after_gate == gate)
    async with anthropic_replay(
        tape,
        source_root=DATA / "anthropic",
        codec_root=ROOT,
    ) as replay:
        edge = LegacyTurnTransport(replay.client, binding.session_id)
        await edge.status()
        await edge.send(
            [{"type": "user.message", "content": [{"type": "text", "text": turn.request_text}]}]
        )
        stream = await edge.open_stream(read_timeout_s=5)
        seen: list[str] = []

        async def collect() -> None:
            async for frame in stream:
                seen.append(str(frame.type))

        task = asyncio.create_task(collect())
        try:
            await current_hold(
                replay.wire,
                gate,
                GateWait(
                    gate=gate,
                    reply_id=reply.id,
                    frame_index=index,
                ),
            )
            if (
                seen
                != [
                    "user.message",
                    "session.status_running",
                    "span.model_request_start",
                    "agent.tool_use",
                ]
                or task.done()
            ):
                raise AssertionError("Anthropic tool result/terminal raced the wire gate")
            with monkeypatch.context() as scoped:
                scoped.setattr(os, "environ", NamesOnly())
                await current_hold(
                    replay.wire,
                    gate,
                    GateWait(
                        gate=gate,
                        reply_id=reply.id,
                        frame_index=index,
                    ),
                )
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    with pytest.raises(ValueError, match="no longer held"):
                        await replay.wire.wait_blocked(gate)
                else:
                    replay.wire.release(gate)
                    await asyncio.wait_for(task, timeout=5)
                if replay.wire.held(gate):
                    raise AssertionError("Anthropic stream retained a stale wire hold")
            await stream.close()
            if cancel:
                with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
                    replay.wire.assert_consumed()
            else:
                if seen[-2:] != ["span.model_request_end", "session.status_idle"]:
                    raise AssertionError("Anthropic terminal was not delivered after release")
                replay.wire.assert_consumed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await stream.close()


@pytest.mark.asyncio
async def test_opened_http_response_is_not_evidence_of_an_iterator_hold() -> None:
    replay = wire()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        async with http.stream("GET", "/events"):
            if replay.held("complete"):
                raise AssertionError("unread HTTP response earned a wire hold")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(replay.wait_blocked("complete"), timeout=0)
        with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
            replay.assert_consumed()


@pytest.mark.asyncio
async def test_shared_gate_keeps_distinct_live_reply_identities_after_one_closes() -> None:
    replay = WireReplay(
        Script(
            replies=tuple(
                WireReply(
                    id=name,
                    method="GET",
                    path="/" + name,
                    frames=(
                        WireFrame(payload={"type": "prefix"}),
                        WireFrame(payload={"type": "tail"}, after_gate="complete"),
                    ),
                )
                for name in ("first", "second")
            )
        )
    )
    async with httpx.AsyncClient(
        base_url="https://offline.invalid",
        transport=httpx.MockTransport(replay.dispatch),
        trust_env=False,
    ) as http:
        ready = {name: asyncio.Event() for name in ("first", "second")}

        async def collect(name: str) -> None:
            async with http.stream("GET", "/" + name) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        ready[name].set()

        first = asyncio.create_task(collect("first"))
        second = asyncio.create_task(collect("second"))
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready.values())), timeout=5)
            waits = await replay.wait_blocked("complete")
            if {w.reply_id for w in waits} != {"first", "second"}:
                raise AssertionError("shared gate lost one actual stream identity")
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await current_hold(
                replay,
                "complete",
                GateWait(
                    gate="complete",
                    reply_id="second",
                    frame_index=1,
                ),
            )
            replay.release("complete")
            await asyncio.wait_for(second, timeout=5)
            if replay.held("complete"):
                raise AssertionError("shared gate retained a stale receipt")
            with pytest.raises(AssertionError, match="unconsumed native SSE frames"):
                replay.assert_consumed()
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

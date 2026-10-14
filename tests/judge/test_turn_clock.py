"""Real SDK/turn/journal timing, with an independent controlled arrival clock."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.state import TurnState
from daimon.testing.host_clock import HostClock, use_host_clock
from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence, evaluate
from daimon.testing.turn_clock import ClockedLifecycle, NativeRoot, await_root_completion
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.events import Event
from mux.contracts.ids import ResourceRef
from mux.state.memory import MemoryStateStore

from . import headless_executor as executor
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted


@pytest.mark.parametrize("arrival_s,missing_journal", [(2.0, False), (4.0, False), (2.0, True)])
async def test_root_deadline_joins_actual_send_and_journal_before_cleanup_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrival_s: float, missing_journal: bool
) -> None:
    clock = ArrivalClock()

    class ProofStore(MemoryStateStore):
        async def read_events(
            self, session_id: str, *, after: int = -1, limit: int = 1000
        ) -> Sequence[Event]:
            events = await super().read_events(session_id, after=after, limit=limit)
            return (
                []
                if missing_journal and any(e.type == "session.turn_ended" for e in events)
                else events
            )

    store = ProofStore()
    monkeypatch.setattr(executor, "MemoryStateStore", lambda: store)
    actual_run = executor.run_turn
    cleanup_entered = asyncio.Event()
    cleanup_drained = asyncio.Event()
    row = source("QA-UNSCORED-ROOT-DEADLINE")
    row["surface"] = "headless"
    row["steps"] = row["steps"][:3]
    row["steps"][2]["timeout_s"] = 3.0
    plan = planned(tmp_path, row)
    tape = scripted(plan)

    async def measured(**kwargs: Any) -> TurnState:
        observer = kwargs["lifecycle"]
        assert isinstance(observer, ClockedLifecycle)
        actual_event = observer.on_sse_event

        async def arrived(event: RawMessageStreamEvent) -> None:
            await actual_event(event)
            if str(event.type) == "session.status_idle":
                clock.value = arrival_s

        observer.on_sse_event = arrived
        try:
            result = await actual_run(**kwargs)
            cleanup_entered.set()
            if arrival_s == 2.0 and not missing_journal:
                await asyncio.sleep(3.1)
            else:
                await asyncio.Event().wait()
            return result
        finally:
            cleanup_drained.set()

    monkeypatch.setattr(executor, "run_turn", measured)
    with use_host_clock(HostClock(clock)):
        if arrival_s > 3.0:
            with pytest.raises(TimeoutError, match="root completion deadline"):
                await executor.execute_cell(plan, tape)
        elif missing_journal:
            with pytest.raises(ValueError, match="no authoritative native terminal"):
                await executor.execute_cell(plan, tape)
        else:
            result = await executor.execute_cell(plan, tape)
            assert result.request_count == 3
            assert len(result.evidence.turns) == 1
            assert result.step_receipts[1].completion_clock is not None
    if arrival_s == 2.0 and not missing_journal:
        assert cleanup_entered.is_set()
    assert cleanup_drained.is_set()


@dataclass
class ArrivalClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


async def test_absent_root_deadline_cancels_and_drains_without_reading_proof() -> None:
    drained = asyncio.Event()
    observer = ClockedLifecycle(RecordingLifecycle(), clock=lambda: 0.0)

    async def blocked() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def proof() -> NativeRoot:
        raise AssertionError("an absent callback cannot authorize a native proof")

    with pytest.raises(TimeoutError, match="root completion deadline"):
        await await_root_completion(
            blocked(), lifecycle=observer, timeout_s=0.01, native_root=proof
        )
    assert drained.is_set() and observer.sample is None


async def test_caller_cancellation_drains_the_invocation_and_terminal_wait() -> None:
    entered = asyncio.Event()
    drained = asyncio.Event()
    observer = ClockedLifecycle(RecordingLifecycle(), clock=lambda: 0.0)

    async def blocked() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def proof() -> NativeRoot:
        raise AssertionError("cancelled invocation cannot authorize a native proof")

    task = asyncio.create_task(
        await_root_completion(blocked(), lifecycle=observer, timeout_s=5.0, native_root=proof)
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set() and observer.sample is None


@pytest.mark.parametrize("arrival_s,expected", [(2.0, "PASS"), (4.0, "FAIL")])
async def test_actual_root_callback_time_is_independent_of_return_cleanup_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrival_s: float, expected: str
) -> None:
    clock = ArrivalClock()

    observers: list[ClockedLifecycle] = []
    store = MemoryStateStore()
    monkeypatch.setattr(executor, "MemoryStateStore", lambda: store)
    actual_run = executor.run_turn

    async def measured(**kwargs: Any) -> TurnState:
        observer = kwargs["lifecycle"]
        assert isinstance(observer, ClockedLifecycle)
        observers.append(observer)
        actual_event = observer.on_sse_event

        async def arrived(event: RawMessageStreamEvent) -> None:
            await actual_event(event)
            if str(event.type) == "session.status_idle":
                clock.value = arrival_s

        observer.on_sse_event = arrived
        result = await actual_run(**kwargs)
        clock.value = 99.0
        return result

    monkeypatch.setattr(executor, "run_turn", measured)
    row = source("QA-UNSCORED-TERMINAL-CLOCK")
    row["surface"] = "headless"
    row["steps"] = row["steps"][:3]
    row["assert"] = [{"kind": "done_within_s", "turn": 1, "max": 3.0}]
    plan = planned(tmp_path, row)
    with use_host_clock(HostClock(clock)):
        result = await executor.execute_cell(plan, scripted(plan))
    observer = observers[0]
    delegate = observer.delegate
    assert isinstance(delegate, RecordingLifecycle)
    assert result.request_count == 3
    turn = result.evidence.turns[0]
    journal = await store.read_events(turn.session_id)
    session = ResourceRef(
        id=turn.session_id, kind="session", provider="anthropic", account_scope_id="offline"
    )
    completion = observer.completion_time(
        session=session, root_turn_id=turn.root_turn_id, journal=journal
    )
    assert completion == arrival_s and completion != clock.value
    assert len(delegate.terminal_success) == 1
    actual = TurnEvidence.model_validate(
        turn.model_copy(
            update={
                "started_s": observer.started_s,
                "terminals": tuple(
                    terminal.model_copy(update={"observed_s": completion})
                    for terminal in turn.terminals
                ),
            }
        ).model_dump()
    )
    outcome = evaluate(
        RunEvidence(scenario_id=plan.scenario.id, backend="anthropic", turns=(actual,)),
        plan.assertions,
    )
    assert outcome.status == expected
    ends = [event for event in journal if event.type == "session.turn_ended"]
    assert ends and ends[0].turn_id == turn.root_turn_id
    assert (
        observer.completion_time(
            session=ResourceRef.model_validate_json(session.model_dump_json()),
            root_turn_id=turn.root_turn_id,
            journal=tuple(
                type(event).model_validate_json(event.model_dump_json()) for event in journal
            ),
        )
        == arrival_s
    )
    with pytest.raises(ValueError, match="duplicate journal"):
        observer.completion_time(
            session=session, root_turn_id=turn.root_turn_id, journal=(ends[0], ends[0])
        )
    for mutation in (
        {"session_id": "foreign-session"},
        {"turn_id": "foreign-root"},
        {"thread_id": "delegated-child"},
        {"authority": "preview"},
        {"authority": "gap"},
        {"payload": {"root_turn_id": "foreign-root", "outcome": "completed"}},
        {"native": ends[0].native.model_copy(update={"provider": "gemini"})},
    ):
        with pytest.raises(ValueError, match="acknowledged authoritative root"):
            observer.completion_time(
                session=session,
                root_turn_id=turn.root_turn_id,
                journal=(ends[0].model_copy(update=mutation),),
            )
    with pytest.raises(ValueError, match="no authoritative native terminal"):
        observer.completion_time(session=session, root_turn_id=turn.root_turn_id, journal=())
    with pytest.raises(ValueError, match="multiple terminal"):
        await observer.on_terminal_success(delegate.terminal_success[0])
    with pytest.raises(ValueError, match="single settled"):
        observer.completion_time(session=session, root_turn_id=turn.root_turn_id, journal=journal)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True])
def test_clock_refuses_invalid_samples(value: float) -> None:
    with pytest.raises(ValueError, match="clock"):
        ClockedLifecycle(RecordingLifecycle(), clock=lambda: value)


async def test_unsettled_or_missing_terminal_never_measures_completion() -> None:
    observer = ClockedLifecycle(RecordingLifecycle(), clock=lambda: 0.0)
    with pytest.raises(ValueError, match="single settled"):
        observer.completion_time(
            session=ResourceRef(id="s", kind="session", provider="openai", account_scope_id="p"),
            root_turn_id="root",
            journal=(),
        )
    assert observer.sample is None
    # A bare bookkeeping callback lacks actual host termination and native authority.
    await observer.on_terminal_success(TurnState())
    with pytest.raises(ValueError, match="single settled"):
        observer.completion_time(
            session=ResourceRef(id="s", kind="session", provider="openai", account_scope_id="p"),
            root_turn_id="root",
            journal=(),
        )


async def test_delegate_failure_is_preserved_without_a_completion_sample() -> None:
    failure = RuntimeError("actual terminal delivery failed")

    class BrokenLifecycle(RecordingLifecycle):
        async def on_terminal_success(self, state: TurnState) -> None:
            raise failure

    observer = ClockedLifecycle(BrokenLifecycle(), clock=lambda: 0.0)
    with pytest.raises(RuntimeError) as raised:
        await observer.on_terminal_success(TurnState())
    assert raised.value is failure
    assert observer.sample is None


async def test_clock_backwards_does_not_create_a_terminal_sample() -> None:
    clock = ArrivalClock(2.0)
    observer = ClockedLifecycle(RecordingLifecycle(), clock=clock, started_s=1.0)
    assert observer.started_s == 1.0
    clock.value = 0.0
    with pytest.raises(ValueError, match="clock"):
        await observer.on_terminal_success(TurnState())
    assert observer.sample is None


@pytest.mark.parametrize("start", [-1.0, 3.0, float("nan"), True])
def test_start_must_be_an_earlier_real_clock_sample(start: float) -> None:
    with pytest.raises(ValueError, match="host start"):
        ClockedLifecycle(RecordingLifecycle(), clock=lambda: 2.0, started_s=start)

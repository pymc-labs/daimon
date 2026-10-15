"""Gemini host cases; shared helpers belong to test_catalog_clock_binding."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from daimon.core.turn.state import TurnState
from daimon.testing.host_clock import HostClock, use_host_clock
from daimon.testing.provider_fixtures import load_provider_fixtures
from daimon.testing.provider_replay import SourcePin
from daimon.testing.turn_clock import ClockedLifecycle
from mux.contracts.events import Event
from mux.contracts.ids import Provider
from mux.state.memory import MemoryStateStore

from . import headless_executor
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .test_catalog_clock_binding import (
    FIXTURES as FIXTURES,
)
from .test_catalog_clock_binding import (
    REPO as REPO,
)
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted
from .test_turn_clock import ArrivalClock


@pytest.mark.parametrize("provider", ("gemini",))
@pytest.mark.parametrize("control", ("timely_cleanup", "late_root", "missing_journal"))
async def test_real_native_root_budget_does_not_time_outer_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider, control: str
) -> None:
    arrival = ArrivalClock()
    if provider == "openai":
        from . import openai_catalog_host as host
    elif provider == "gemini":
        from . import gemini_catalog_host as host
    else:
        host = headless_executor
    actual_run = host.run_turn
    drained = asyncio.Event()
    observers: list[ClockedLifecycle] = []

    class ProofStore(MemoryStateStore):
        async def read_events(
            self, session_id: str, *, after: int = -1, limit: int = 1000
        ) -> Sequence[Event]:
            journal = await super().read_events(session_id, after=after, limit=limit)
            return (
                ()
                if control == "missing_journal"
                and any(e.type == "session.turn_ended" for e in journal)
                else journal
            )

    store = ProofStore()
    if provider == "anthropic":
        monkeypatch.setattr(headless_executor, "MemoryStateStore", lambda: store)
    else:
        if provider == "openai":
            from . import openai_catalog_replay as replay
        else:
            from . import gemini_catalog_replay as replay
        monkeypatch.setattr(replay, "MemoryStateStore", lambda: store)

    async def measured(**kwargs: Any) -> TurnState:
        observer = kwargs["lifecycle"]
        if not isinstance(observer, ClockedLifecycle):
            raise AssertionError("actual core did not receive the clocked delegate")
        observers.append(observer)
        terminal = observer.on_terminal_success

        async def arrived(state: TurnState) -> None:
            # Controlled arrival comes from the real core finalizer, independently
            # of the source deadline and oracle predicate.
            arrival.value = 4.0 if control == "late_root" else 2.0
            await terminal(state)

        observer.on_terminal_success = arrived
        try:
            state = await actual_run(**kwargs)
            if control == "timely_cleanup":
                # Real async cleanup exceeds source3; root2 already settled.
                await asyncio.sleep(3.1)
                arrival.value = 99.0
            else:
                await asyncio.Event().wait()
            return state
        finally:
            drained.set()

    monkeypatch.setattr(host, "run_turn", measured)
    row = source(f"QA-UNSCORED-CLOCK-{provider}-{control}")
    row["surface"] = "headless"
    row["steps"] = row["steps"][:3]
    row["steps"][2]["timeout_s"] = 3.0
    row["assert"] = [{"kind": "done_within_s", "turn": 1, "max": 3.0}]
    if provider == "anthropic":
        plan = planned(tmp_path, row, provider)
        invocation = execute_cell(plan, scripted(plan))
        requests = 3
    else:
        pack = load_provider_fixtures(FIXTURES / "index.json", catalog_root=FIXTURES / "catalog")
        template, original = pack.select("QA-NEW37-MARKDOWN-TABLE-READABLE", provider)
        row["steps"][1]["text"] = original.turns[0].request_text
        plan = planned(tmp_path, row, provider)
        trigger = next(i for i in plan.invocations if i.operation == "host_turn")
        native = original.turns[0].model_copy(
            update={
                "turn": trigger.turn,
                "location": trigger.location,
                "channel": trigger.channel,
                "thread": trigger.thread,
                "completion_gate": None,
            }
        )
        fixture = original.model_copy(update={"turns": (native,), "gaps": ()})
        pin = SourcePin(path=f"scenarios/{plan.scenario.filename}", sha256=plan.scenario.sha256)
        scenario = template.model_copy(
            update={
                "scenario_id": plan.scenario.id,
                "source": pin,
                "projection": pin,
                "scenario": yaml.safe_load((tmp_path / pin.path).read_bytes()),
                "values": {},
                "assets": (),
                "providers": tuple(
                    fixture if f.backend == provider else f for f in template.providers
                ),
            }
        )
        execute = execute_openai_cell if provider == "openai" else execute_gemini_cell
        invocation = execute(plan, scenario, fixture, source_root=tmp_path, repository_root=REPO)
        requests = 6 if provider == "openai" else 3
    clock = HostClock(arrival)
    with use_host_clock(clock):
        if control == "late_root":
            with pytest.raises(TimeoutError, match="root completion deadline"):
                await invocation
        elif control == "missing_journal":
            with pytest.raises(ValueError, match="no authoritative native terminal"):
                await invocation
        else:
            result = await invocation
            assert result.request_count == requests
            assert result.catalog_outcome.checks[0].status == "PASS"
            turn = result.evidence.turns[0]
            assert turn.started_s == 0.0
            assert all(t.observed_s == 2.0 for t in turn.terminals)
            assert result.headless_outputs[0].returned_s == 99.0
            receipt = result.step_receipts[1]
            assert receipt.started_clock and receipt.completion_clock and receipt.finished_clock
            assert (
                receipt.started_clock.clock_id
                == receipt.completion_clock.clock_id
                == clock.last.clock_id
            )
            assert (
                receipt.started_clock.order
                < receipt.completion_clock.order
                < receipt.finished_clock.order
            )
            assert receipt.completion_clock.observed_s == 2.0 and receipt.finished_s == 99.0
            assert type(receipt).model_validate_json(receipt.model_dump_json()) == receipt
    assert len(observers) == 1 and drained.is_set()

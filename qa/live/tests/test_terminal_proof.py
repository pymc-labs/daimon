from __future__ import annotations

import pytest
from pydantic import JsonValue

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Turn, WatchTimeout, utcnow


@pytest.mark.parametrize("burst", [False, True])
def test_timeout_keeps_product_failure_without_passing_unproven_bounds(
    burst: bool, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    class TimeoutBackend(FakeBackend):
        def collect(self, turn: Turn, timeout: float) -> None:
            # Simulate stale terminal evidence followed by a still-running card.
            # The runner must clear proof even when a backend raises after updating it.
            super().collect(turn, timeout)
            turn.messages[0].update(content="Working on it…", working=True)
            turn.verdicts = ["working"]
            turn.ended_at = utcnow()
            raise WatchTimeout("still working")

    backend = TimeoutBackend()
    scenario.tier = "full"
    scenario.est_turns = 1
    first = Step(do="burst", texts=["APPLE"]) if burst else Step(do="mention", text="APPLE")
    scenario.steps = [first, Step(do="wait_done")]
    conditions: list[dict[str, JsonValue]] = [
        {"kind": "message_count", "max": 2},
        {"kind": "attachments", "max": 0, "name_pattern": r"\.pdf$"},
        {"kind": "fences_balanced"},
        {"kind": "footer_on_last_message"},
        {"kind": "message_count", "min": 1},
        {"kind": "message_count", "max": 0},
        {"kind": "attachments", "max": 0, "name_pattern": r"\.csv$"},
    ]
    scenario.assertions = [Assertion.model_validate({"turn": 1, **c}) for c in conditions]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "FAIL"
    assert any(c.kind == "watch" and c.status == "FAIL" for c in result.checks)
    assert result.turns[0].ended_at and not result.turns[0].settled
    assert [c.status for c in result.checks if c.kind != "watch"][:7] == [
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
        "PASS",
        "FAIL",
        "FAIL",
    ]
    assert backend.events[-1] == "delete"
    assert ledger.path.exists()


def test_settled_turn_proves_upper_bounds(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    scenario.tier = "full"
    scenario.est_turns = 1
    scenario.steps = [Step(do="mention", text="APPLE"), Step(do="wait_done")]
    scenario.assertions = [
        Assertion(kind="message_count", turn=1, maximum=2),
        Assertion(kind="attachments", turn=1, maximum=0, name_pattern=r"\.pdf$"),
        Assertion(kind="fences_balanced", turn=1),
        Assertion(kind="footer_on_last_message", turn=1),
    ]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PASS"
    assert result.turns[0].settled
    assert all(c.status == "PASS" for c in result.checks)

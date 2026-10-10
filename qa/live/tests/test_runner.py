from __future__ import annotations

import json

import pytest

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.report import Result
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Pending


def test_two_turn_followup_and_receipt(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PASS"
    assert "send:thread:False" in backend.events
    assert backend.events[-1] == "delete"
    receipt = json.loads(ledger.path.read_text())
    assert receipt["usd"] == 0.04
    assert receipt["input_tokens"] == 20
    assert json.loads(ledger.reservations.read_text()) == {}


@pytest.mark.parametrize(
    "error,status",
    [
        (RuntimeError("a secret must not appear"), "FAIL"),
        (KeyboardInterrupt(), "PENDING"),
        (SystemExit("token"), "PENDING"),
        (Pending("not available"), "PENDING"),
    ],
)
def test_cleanup_and_accounting_on_every_exit(
    error: BaseException,
    status: str,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    backend.error = error
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == status
    assert "delete" in backend.events
    assert ledger.path.exists()
    assert "secret" not in str(result.checks)
    assert "token" not in str(result.checks)


@pytest.mark.parametrize("surface", ["headless", "slack", "teams"])
def test_unsupported_surface_is_pending_without_spend(
    surface: str,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    changed = Scenario.model_validate(scenario.model_dump(by_alias=True) | {"surface": surface})
    result = Executor(backend, judge, ledger, pricing, "staging").run(changed)
    assert result.status == "PENDING"
    assert not backend.events
    assert not ledger.path.exists()


@pytest.mark.parametrize(
    "verdict,status",
    [
        ("over_cap", "PENDING"),
        ("provisioning", "PENDING"),
        ("error", "FAIL"),
        ("cancelled", "FAIL"),
    ],
)
def test_terminal_refusal_never_passes(
    verdict: str,
    status: str,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    backend.verdict = verdict
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == status
    assert "delete" in backend.events


def test_all_remaining_discord_step_dispatch(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    values = scenario.model_dump(by_alias=True) | {
        "tier": "weekly",
        "est_turns": 4,
        "setup": [{"do": "admin", "tool": "configure", "args": {"agent": "qa"}}],
        "steps": [
            {"do": "new_channel"},
            {"do": "channel_message", "text": "history"},
            {"do": "mention", "text": "hi"},
            {"do": "wait_done"},
            {"do": "react", "target": "last_answer", "emoji": "👍"},
            {"do": "wait", "s": 0},
            {"do": "burst", "texts": ["1", "2"], "interval_s": 0},
            {"do": "wait_done"},
            {"do": "restart_workers"},
        ],
        "teardown": [{"do": "admin", "tool": "reset"}],
    }
    result = Executor(backend, judge, ledger, pricing, "staging").run(
        Scenario.model_validate(values)
    )
    assert result.status == "PASS"
    assert backend.events.count("create") == 1
    assert "react" in backend.events
    assert "admin:restart_workers" in backend.events
    assert backend.events[-2:] == ["admin:reset", "delete"]


def test_judge_failure_preserves_product_checks_and_cleanup(
    backend: FakeBackend, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    class BrokenJudge(FakeJudge):
        def evaluate(self, rubric: str, answer: str) -> tuple[bool, str]:
            raise RuntimeError("secret provider response")

    scenario.assertions.append(Assertion(kind="judge", turn=2, rubric="contains APPLE"))
    result = Executor(backend, BrokenJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING"
    assert next(c for c in result.checks if c.kind == "text_present").status == "PASS"
    assert next(c for c in result.checks if c.kind == "judge").status == "PENDING"
    assert sum(c.kind == "text_absent" for c in result.checks) == 6
    assert "secret" not in str(result.checks)
    assert "turn 2: judge execution unavailable: RuntimeError" in result.notes
    assert backend.events[-1] == "delete"


@pytest.mark.parametrize(
    "step",
    [
        {"do": "dm", "text": "hello"},
        {"do": "headless_interrupt", "text": "wait", "interrupt_after_s": 1, "timeout_s": 5},
    ],
)
def test_recognized_pending_steps(
    step: dict[str, object],
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
) -> None:
    executor = Executor(backend, judge, ledger, pricing, "staging")
    with pytest.raises(Pending):
        executor.step(Step.model_validate(step), Result("test", "qa", "staging"), "parent")


def test_prod_disallows_admin_before_preflight(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    scenario.setup = [Step(do="admin", tool="configure")]
    with pytest.raises(ValueError, match="production"):
        Executor(backend, judge, ledger, pricing, "prod").run(scenario)
    assert not backend.events


def test_no_implicit_unsupported_assertions(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    scenario.assertions = [Assertion(kind="interrupt_within_s", maximum=5)]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING"


def test_pending_catalog_proposal_has_no_side_effects(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    from qa.live.schema import ProposedScenario

    entry = ProposedScenario.model_validate(
        scenario.model_dump(by_alias=True)
        | {
            "assert": [{"kind": "message_count", "turn": 2, "max": 3}],
        }
    )
    result = Executor(backend, judge, ledger, pricing, "staging").run(entry)
    assert result.status == "PENDING"
    assert not backend.events and not ledger.path.exists()

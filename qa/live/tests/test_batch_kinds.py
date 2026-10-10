from datetime import timedelta

import pytest
from pydantic import ValidationError

from qa.live.config import Pricing
from qa.live.cost import Ledger, estimate
from qa.live.discord import fingerprint
from qa.live.evaluate import evaluate
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Message, Turn, card_text_of, text_of, utcnow


def observed(number: int, identity: str, *, settled: bool = True) -> Turn:
    turn = Turn(
        number, str(number), "parent", utcnow(), thread_id=f"thread-{number}", settled=settled
    )
    turn.messages = [{"id": identity, "content": "ANSWER"}]
    return turn


@pytest.mark.parametrize("kind", ["answers_total", "threads_created"])
def test_whole_run_counts_prove_violation_but_not_timed_out_upper_bound(kind: str) -> None:
    backend, judge = FakeBackend(), FakeJudge()
    assertion = Assertion.model_validate({"kind": kind, "max": 1})
    turns = [observed(1, "a"), observed(2, "b", settled=False)]
    assert evaluate(assertion, turns, backend, judge).status == "FAIL"
    turns[1].thread_id = turns[0].thread_id
    turns[1].messages = turns[0].messages
    assert evaluate(assertion, turns, backend, judge).status == "PENDING"
    turns[1].settled = True
    assert evaluate(assertion, turns, backend, judge).status == "PASS"


@pytest.mark.parametrize(
    "pattern,missing,expected",
    [("ANSWER", True, "PASS"), ("OTHER", True, "PENDING"), ("OTHER", False, "FAIL")],
)
def test_any_of_three_valued_evidence(pattern: str, missing: bool, expected: str) -> None:
    assertion = Assertion.model_validate(
        {
            "kind": "any_of",
            "of": [
                {"kind": "text_present", "turn": 1, "pattern": pattern},
                {"kind": "text_present", "turn": 2 if missing else 1, "pattern": "NO"},
            ],
        }
    )
    assert evaluate(assertion, [observed(1, "a")], FakeBackend(), FakeJudge()).status == expected


def test_component_reads_nested_labels_and_refresh_reads_current_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    turn = observed(1, "card")
    turn.messages[0]["components"] = [
        {"type": 1, "components": [{"type": 2, "label": "Connect GitHub"}]}
    ]
    assert (
        evaluate(
            Assertion(kind="component_present", turn=1, label_pattern="(?i)connect github"),
            [turn],
            backend,
            FakeJudge(),
        ).status
        == "PASS"
    )
    monkeypatch.setattr(
        backend, "current_messages", lambda turn: [{"id": "card", "content": "Expired"}]
    )
    check = Assertion(
        kind="card_text_now", turn=1, pattern="(?i)expired", pattern_absent="received"
    )
    assert evaluate(check, [turn], backend, FakeJudge()).status == "PASS"
    assert turn.messages[0]["content"] == "ANSWER"


def test_running_edits_are_distinct_and_exclude_terminal_edits() -> None:
    turn = observed(1, "card")
    edit: Message = {
        "message_id": "card",
        "edited_timestamp": utcnow().isoformat(),
        "phase": "running",
    }
    turn.card_history = [edit, edit, {**edit, "edited_timestamp": "later", "phase": "terminal"}]
    assertion = Assertion(kind="card_edits_min", turn=1, minimum=2, during="running")
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.settled = False
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    turn.card_history.append({**edit, "edited_timestamp": "next"})
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def test_current_card_reads_nested_v2_copy_without_button_labels() -> None:
    turn = observed(1, "card")
    turn.messages = [
        {
            "id": "card",
            "components": [
                {
                    "type": 17,
                    "components": [
                        {
                            "type": 9,
                            "components": [{"type": 10, "content": "Request expired."}],
                            "accessory": {"type": 2, "label": "Received"},
                        }
                    ],
                }
            ],
        }
    ]
    check = Assertion(
        kind="card_text_now", turn=1, pattern="(?i)expired", pattern_absent="Received"
    )
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PASS"
    assert card_text_of(turn.messages[0]).strip() == "Request expired."
    assert not text_of(turn.messages[0]).strip()


@pytest.mark.parametrize("absence", [False, True])
@pytest.mark.parametrize("with_readable_row", [False, True])
def test_current_card_unreadable_copy_never_proves_presence_or_absence(
    absence: bool, with_readable_row: bool
) -> None:
    turn = observed(1, "card")
    turn.messages = [{"id": "card", "components": [{"type": 2, "label": "Expired"}]}]
    if with_readable_row:
        turn.messages.append({"id": "answer", "content": "Expired"})
    check = Assertion(
        kind="card_text_now",
        turn=1,
        pattern=None if absence else "Expired",
        pattern_absent="Received" if absence else None,
    )
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PENDING"


def test_component_only_edits_change_fingerprint_without_edit_timestamp() -> None:
    before: Message = {
        "id": "card",
        "components": [{"type": 17, "components": [{"type": 10, "content": "Received"}]}],
    }
    after: Message = {
        **before,
        "components": [{"type": 17, "components": [{"type": 10, "content": "Expired"}]}],
    }
    assert fingerprint(before) != fingerprint(after)


def test_v2_card_extraction_does_not_relax_anchored_answer_matching() -> None:
    turn = observed(1, "answer")
    turn.messages = [
        {
            "id": "answer",
            "content": "-# qa-agent\nNot B1: needs context\nB1",
            "components": [
                {"type": 10, "content": "B1"},
                {"type": 10, "content": "Not B1: context"},
            ],
        }
    ]
    turn.agent_subtext_headers = [{"message_id": "answer", "line": "-# qa-agent"}]
    assertion = Assertion(kind="text_present", turn=1, pattern="^B1$")
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.messages[0]["content"] = "-# qa-agent\nB1"
    turn.messages[0]["components"] = [{"type": 10, "content": "Footer: $0.01"}]
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def test_chunk_gap_uses_answer_edit_instead_of_progress_card_creation() -> None:
    turn = observed(1, "1")
    turn.baseline_message_ids = ["1"]
    turn.messages = [
        {
            "id": "1",
            "content": "one",
            "timestamp": turn.started_at.isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=40)).isoformat(),
        },
        {
            "id": "2",
            "content": "two",
            "timestamp": (turn.started_at + timedelta(seconds=43)).isoformat(),
        },
    ]
    assertion = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"
    turn.settled = False
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    turn.messages[1]["timestamp"] = (turn.started_at + timedelta(seconds=47)).isoformat()
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"


def test_foreign_or_unresolved_guild_refuses_before_setup_mutation(
    scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    scenario.setup = [Step(do="admin", tool="cli", args="daimon tenants credit forbidden")]
    scenario.steps[0].guild = "other-guild"
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert ledger.charged({result.run_id}) == 0


def test_step_scope_validation_and_nested_judge_budget(
    scenario: Scenario, pricing: Pricing
) -> None:
    with pytest.raises(ValidationError):
        Step(do="mention", text="x", allow_fail=True)
    scenario.assertions = [
        Assertion(
            kind="any_of",
            alternatives=[
                Assertion(kind="judge", turn=1, rubric="answer"),
                Assertion(kind="judge", turn=2, rubric="answer"),
            ],
        )
    ]
    assert estimate(scenario, pricing) > scenario.est_turns * pricing.per_turn_usd


def test_late_answer_edits_cannot_hide_or_invent_chunk_gaps() -> None:
    turn = observed(1, "1")
    turn.messages = [
        {
            "id": "1",
            "content": "one",
            "timestamp": turn.started_at.isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=40)).isoformat(),
        },
        {
            "id": "2",
            "content": "two",
            "timestamp": (turn.started_at + timedelta(seconds=43)).isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=100)).isoformat(),
        },
    ]
    assertion = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    # Creation-to-creation stall remains a failure despite later edits.
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.messages[1]["timestamp"] = (turn.started_at + timedelta(seconds=3)).isoformat()
    # A late footer edit cannot invent a stall.
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


@pytest.mark.parametrize(
    "args",
    [
        "daimon tenants credit discord 1435062989119295640 -999 --note qa",
        "uv run daimon tenants credit discord 1435062989119295640 -999 --note qa",
        "daimon --env staging tenants credit discord 1435062989119295640 -999 --note qa",
    ],
)
def test_shared_guild_credit_is_refused_before_any_mutation(
    args: str, scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    scenario.setup = [
        Step(
            do="admin",
            tool="cli",
            args=args,
        )
    ]
    scenario.steps[0].guild = "1435062989119295640"
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert any("credit mutations" in c.reason for c in result.checks)
    assert ledger.charged({result.run_id}) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"allow_fail": True},
        {"allow_fail": True, "allow_fail_pattern": ".*"},
        {"allow_fail": True, "allow_fail_pattern": "^"},
        {"allow_fail": True, "allow_fail_pattern": "["},
        {"allow_fail": True, "allow_fail_pattern": "refus", "allow_fail_exit_codes": [0]},
    ],
)
def test_expected_refusal_is_explicit_and_validated(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"do": "admin", "tool": "cli", "args": "daimon skills delete qa-test", **payload}
        )


@pytest.mark.parametrize("section", ["setup", "teardown"])
def test_dict_cli_arguments_are_refused_before_mutation(
    scenario: Scenario, ledger: Ledger, pricing: Pricing, section: str
) -> None:
    setattr(scenario, section, [Step(do="admin", tool="cli", args={"argv": "tenants credit"})])
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert any("validated command string" in c.reason for c in result.checks)
    assert ledger.charged({result.run_id}) == 0

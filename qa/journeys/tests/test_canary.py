import math
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from qa.journeys.canary import (
    CanaryExecutor,
    Haiku55Only,
    NoJudge,
    accounting,
    next_turn_fits,
    scenario,
    validate,
)
from qa.live.config import Config, Pricing
from qa.live.cost import Ledger
from qa.live.report import Result
from qa.live.schema import Step
from qa.live.types import Pending, Turn, Usage


def config():
    result = Config(
        pricing=Pricing(per_turn_usd=0.1, judge_input_per_million=0.1, judge_output_per_million=0.5)
    )
    result.staging.model_probe = ["read-only-model-probe"]
    result.staging.deployment_probe = ["read-only-deployment-probe"]
    result.staging.qa_agent_name = "qa-agent"
    return result


def test_canary_has_two_triggers_and_no_judge():
    assert scenario().est_turns == 2
    assert all(a.kind != "judge" for a in scenario().assertions)
    validate(config(), 0.25)


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-5", "gpt-6-luna"]
)
def test_old_staging_override_and_other_models_refused(model):
    assert not Haiku55Only().accepts("anthropic", model, "staging")


def test_haiku55_staging_only():
    policy = Haiku55Only()
    assert policy.accepts("anthropic", "claude-haiku-5-5", "staging")
    assert policy.accepts("anthropic", "claude-haiku-5-5-20261001", "staging")
    assert not policy.accepts("anthropic", "claude-haiku-5-5", "prod")
    assert not policy.accepts("openai", "claude-haiku-5-5", "staging")


@pytest.mark.parametrize("budget", [0, -1, 0.3, 0.19, math.nan, math.inf])
def test_invalid_or_insufficient_budget_refused(budget):
    with pytest.raises(ValueError):
        validate(config(), budget)


def test_missing_probes_refused():
    value = config()
    value.staging.model_probe = []
    with pytest.raises(ValueError):
        validate(value, 0.25)


def test_previous_spend_or_uncertain_usage_stops_next_turn():
    assert next_turn_fits([], 0.1, 0.25)
    assert next_turn_fits([Usage(usd=0.15, source="turn_outcomes")], 0.1, 0.25)
    assert not next_turn_fits([Usage(usd=0.16, source="turn_outcomes")], 0.1, 0.25)
    assert not next_turn_fits([Usage(usd=None, source="turn_outcomes")], 0.1, 0.25)
    assert not next_turn_fits([Usage(usd=0.01, source="footer")], 0.1, 0.25)


@pytest.mark.parametrize("cost", [None, 0.16])
def test_executor_refuses_second_send_before_budget_is_exceeded(tmp_path, cost):
    def unexpected_send(*args, **kwargs):
        pytest.fail("second paid trigger escaped the allowance check")

    backend = SimpleNamespace(
        fallback_watch_s=180,
        usage=lambda turn: Usage(usd=cost, source="turn_outcomes", models=["claude-haiku-5-5"]),
        verify_model=lambda channel: None,
        send=unexpected_send,
    )
    executor = CanaryExecutor(
        backend, NoJudge(), Ledger(tmp_path / "ledger.jsonl"), config().pricing, "staging"
    )
    executor.budget_usd = 0.25
    result = Result("test", "QA-JOURNEY-CONTEXT", "staging")
    result.turns.append(Turn(1, "message", "channel", datetime.now(UTC), thread_id="thread"))
    with pytest.raises(Pending, match="allowance"):
        executor.step(Step(do="thread_reply", text="next", mention=True), result, "channel")


def test_accounting_does_not_turn_missing_tokens_into_zero():
    result = Result("test", "QA-JOURNEY-CONTEXT", "staging")
    assert accounting(result, 0.5, 0.25)["application_input_tokens"] == 0
    result.turns.append(Turn(1, "message", "channel", datetime.now(UTC)))
    receipt = accounting(result, 0.5, 0.25)
    assert receipt["application_input_tokens"] is None
    assert receipt["minutes"] == 0.5
    assert receipt["controller_input_tokens"] == receipt["judge_input_tokens"] == 0

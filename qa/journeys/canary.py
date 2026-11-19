"""One staging-only Haiku 5.5 canary, using the existing live runner.

Default execution writes a plan. --go additionally requires a configured
staging bot driver, read-only usage database, and deployment/model probes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path

from qa.journeys.profile import DEFAULT_ROOT
from qa.live.config import Config, load_config
from qa.live.cost import Ledger, estimate
from qa.live.discord import DiscordBackend
from qa.live.locking import live_run_lock
from qa.live.models import BackendName, ModelPolicy
from qa.live.report import Result, report
from qa.live.runner import Executor
from qa.live.schema import Scenario, Step, load_catalog
from qa.live.types import Message, Pending, Usage

MODEL = "claude-haiku-5-5"
CATALOG = Path(__file__).parent / "canary_catalog"


class Haiku55Only(ModelPolicy):
    def accepts(self, backend: BackendName, model: str, env: str) -> bool:
        return (
            env == "staging"
            and backend == "anthropic"
            and self.policy("anthropic").matches_primary(model)
        )


class CanaryBackend(DiscordBackend):
    def sweep_orphans(self) -> None:
        # This effort cleans up only resources created by its current run.
        # qa/live's shared marker cannot establish this effort's ownership.
        return


class NoJudge:
    def __init__(self) -> None:
        self.usage: list[Usage] = []
        self.errors: list[Message] = []

    def evaluate(self, rubric: str, answer: str) -> tuple[bool, str]:
        raise RuntimeError("This canary uses deterministic assertions only")


def next_turn_fits(usages: list[Usage], next_estimate: float, budget: float) -> bool:
    """Unknown prior spend cannot fund another turn; no estimate is a refund."""
    if any(
        usage.source != "turn_outcomes"
        or usage.usd is None
        or not math.isfinite(usage.usd)
        or usage.usd < 0
        for usage in usages
    ):
        return False
    charged = sum((Decimal(str(usage.usd)) for usage in usages), Decimal(0))
    return charged + Decimal(str(next_estimate)) <= Decimal(str(budget))


def accounting(result: Result, minutes: float, budget_usd: float) -> dict[str, object]:
    """Keep unknown provider usage distinct from a measured zero."""
    inputs = [turn.usage.input_tokens for turn in result.turns]
    outputs = [turn.usage.output_tokens for turn in result.turns]
    return {
        "run_id": result.run_id,
        "status": result.status,
        "environment": "staging",
        "model": MODEL,
        "minutes": minutes,
        "budget_usd": budget_usd,
        "application_input_tokens": None
        if None in inputs
        else sum(v for v in inputs if v is not None),
        "application_output_tokens": None
        if None in outputs
        else sum(v for v in outputs if v is not None),
        "controller_input_tokens": 0,
        "controller_output_tokens": 0,
        "judge_input_tokens": 0,
        "judge_output_tokens": 0,
        "checks": [{"kind": check.kind, "status": check.status} for check in result.checks],
        "native_client": "PENDING",
    }


class CanaryExecutor(Executor):
    budget_usd: float = 0.0

    def step(self, step: Step, result: Result, channel: str) -> None:
        if step.do in {"mention", "thread_reply"}:
            if len(result.turns) >= 2:
                raise Pending("Two-turn canary limit reached")
            usages = [self.backend.usage(turn) for turn in result.turns]
            if not next_turn_fits(usages, self.pricing.per_turn_usd, self.budget_usd):
                raise Pending("Canary allowance insufficient or earlier usage unknown")
            if any(
                not usage.models
                or any(
                    not self.models.accepts("anthropic", model, "staging") for model in usage.models
                )
                for usage in usages
            ):
                raise Pending("Earlier turn lacks Haiku 5.5 model evidence")
        super().step(step, result, channel)


def scenario() -> Scenario:
    candidates = load_catalog(CATALOG)
    if len(candidates) != 1 or not isinstance(candidates[0], Scenario):
        raise ValueError("Expected one supported canary")
    result = candidates[0]
    if result.est_turns != 2 or result.setup or result.teardown:
        raise ValueError("Canary must have exactly two estimated turns and no hooks")
    if sum(s.do in {"mention", "thread_reply"} for s in result.steps) != 2:
        raise ValueError("Canary must have exactly two triggers")
    if any(s.do not in {"mention", "thread_reply", "wait_done"} for s in result.steps):
        raise ValueError("Unsupported canary action")
    if any(a.kind == "judge" or a.alternatives for a in result.assertions):
        raise ValueError("Canary must not invoke a paid judge")
    return result


def validate(config: Config, budget_usd: float) -> None:
    if not math.isfinite(budget_usd) or not 0 < budget_usd <= 0.25:
        raise ValueError("State a finite per-run allowance greater than $0 and at most $0.25")
    if config.prod.enabled or config.staging.backend != "anthropic":
        raise ValueError("Canary is staging-only and Haiku-only")
    if not config.staging.model_probe or not config.staging.deployment_probe:
        raise ValueError("Configure read-only staging model and deployment probes")
    if not config.staging.qa_agent_name:
        raise ValueError("Configure the dedicated staging QA agent")
    if estimate(scenario(), config.pricing) > budget_usd:
        raise ValueError("Two-turn reservation exceeds the stated allowance")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.budget_usd) or not 0 < args.budget_usd <= 0.25:
        parser.error("--budget-usd must be greater than $0 and at most $0.25")
    os.umask(0o077)
    root = args.root.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        parser.error("Keep run artifacts outside Git checkouts")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan = {
        "scenario": scenario().id,
        "environment": "staging",
        "model": MODEL,
        "maximum_turns": 2,
        "budget_usd": args.budget_usd,
        "input_token_allocation": 60000,
        "output_token_allocation": 4000,
        "runtime_minutes_allocation": 6,
        "automatic_repeat": False,
        "status": "PENDING",
        "note": "Planning allocations are not provider-enforced billing limits.",
        "native_client": "PENDING: bot transport does not observe rendered UI",
    }
    plan_path = root / "canary-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    print(plan_path)
    if not args.go:
        return 0
    if not args.config or not args.ledger:
        parser.error("Live run requires --config and the shared --ledger")
    config = load_config(args.config)
    validate(config, args.budget_usd)
    # Both pre-trigger model probes and post-turn usage use this stricter policy.
    config.models = Haiku55Only()
    started = time.monotonic()
    with live_run_lock(Path(config.live_lock)):
        executor = CanaryExecutor(
            CanaryBackend(config, "staging"),
            NoJudge(),
            Ledger(args.ledger),
            config.pricing,
            "staging",
            models=config.models,
        )
        executor.budget_usd = args.budget_usd
        result = executor.run(scenario())
        minutes = (time.monotonic() - started) / 60
        result.notes.extend(
            [
                f"elapsed_minutes={minutes:.4f}",
                f"budget_usd={args.budget_usd}; model={MODEL}; maximum_turns=2",
                "Native-client evidence PENDING; this run uses the bot transport.",
                "No automatic repeat. Usage and conservative reservations use qa/live ledger.",
            ]
        )
        report(result, root / "canary-runs")
        receipt = root / "canary-runs" / f"{result.run_id}-accounting.json"
        receipt.write_text(
            json.dumps(accounting(result, minutes, args.budget_usd), indent=2) + "\n"
        )
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

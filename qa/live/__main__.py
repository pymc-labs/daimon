"""python -m qa.live validate|run|canary. No live action without explicit GO."""

from __future__ import annotations

import argparse
import signal
from functools import partial
from pathlib import Path
from types import FrameType

from qa.live.config import load_config, write_example
from qa.live.cost import Ledger, estimate
from qa.live.deployment import run_with_deploy_retry
from qa.live.discord import DiscordBackend
from qa.live.judge import HaikuJudge
from qa.live.locking import live_run_lock
from qa.live.report import Alerter, Result, report
from qa.live.runner import Executor
from qa.live.schema import ProposedScenario, Scenario, load_catalog


def interrupted(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def retry_fits_budget(ledger: Ledger, attempted_ids: set[str], cost: float, budget: float) -> bool:
    return ledger.charged(attempted_ids) + cost <= budget


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "run", "canary", "example-config"])
    parser.add_argument("--catalog", type=Path, default=Path("qa/live/scenarios"))
    parser.add_argument("--config", type=Path, default=Path("qa/live/config.local.json"))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--env", choices=["staging", "prod"], default="staging")
    parser.add_argument("--scenario")
    parser.add_argument("--go", action="store_true", help="driver has approved live calls")
    args = parser.parse_args()
    if args.command == "example-config":
        write_example(args.config)
        return 0
    scenarios = load_catalog(args.catalog)
    if args.command == "validate":
        pending = [s for s in scenarios if isinstance(s, ProposedScenario)]
        print(f"validated {len(scenarios)} scenarios; {len(pending)} extensions are PENDING")
        for scenario in pending:
            print(f"PENDING {scenario.id}: {'; '.join(scenario.unsupported)}")
        return 0
    if not args.go:
        parser.error("live execution requires the driver's GO (--go)")
    if args.ledger is None:
        parser.error("live execution requires --ledger (shared driver cost/ledger.jsonl)")
    config = load_config(args.config)
    config.target(
        args.env
    )  # Refuse disabled production before loading a token or calling anything.
    if args.command == "canary":
        scenarios = [s for s in scenarios if s.tier == "canary"]
        if len(scenarios) != 1:
            parser.error("canary requires exactly one two-turn canary scenario")
    elif args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
        if not scenarios:
            parser.error("scenario id not found")
    else:
        scenarios = [s for s in scenarios if s.tier == "full"]
        if not scenarios:
            parser.error("no full scenarios; use --scenario or canary")
    signal.signal(signal.SIGTERM, interrupted)
    config.validate_plan(
        sum(estimate(s, config.pricing) for s in scenarios if isinstance(s, Scenario))
        if args.command == "run"
        else 0,
        canary_estimate=sum(
            estimate(s, config.pricing) for s in scenarios if isinstance(s, Scenario)
        )
        if args.command == "canary"
        else None,
    )
    ledger = Ledger(args.ledger)
    with live_run_lock(Path(config.live_lock)):
        ledger.claim_schedule(args.command, args.env)
        failed = False
        attempted_ids: set[str] = set()
        notified_ids: set[str] = set()
        for scenario in scenarios:
            scenario_estimate = (
                estimate(scenario, config.pricing) if isinstance(scenario, Scenario) else 0
            )

            def factory() -> Executor:
                backend = DiscordBackend(config, args.env)
                return Executor(
                    backend,
                    HaikuJudge(config.pricing, go=args.go, models=config.models),
                    ledger,
                    config.pricing,
                    args.env,
                    models=config.models,
                    model_backend=backend.target.backend,
                )

            def persist(result: Result) -> None:
                attempted_ids.add(result.run_id)
                path = report(result, args.results)
                if result.run_id in notified_ids:
                    return
                notified_ids.add(result.run_id)
                try:
                    Alerter(config.alerts, args.results / "alert-state.json").notify(result, path)
                except Exception as exc:
                    result.notes.append(
                        f"alert inbox unavailable: {type(exc).__name__}; pass continues"
                    )
                    report(result, args.results)
                    print("alert inbox unavailable; pass continues with retained result evidence")

            attempts = run_with_deploy_retry(
                scenario,
                factory,
                persist,
                retry_allowed=partial(
                    retry_fits_budget,
                    ledger,
                    attempted_ids,
                    scenario_estimate,
                    config.schedule.catalog_budget_usd,
                ),
            )
            result = attempts[-1]
            failed |= result.status != "PASS"
            if result.status != "PASS":
                break  # Stop spending after failure or an unavailable capability.
        return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Tool/DB exception text may contain credentials. Detailed QA evidence is
        # written by the executor; entrypoint failures expose only their type.
        print(f"QA runner stopped before completing: {type(exc).__name__}")
        raise SystemExit(1) from None

"""Explicit budgeted orchestration for normalized evidence; offline by default."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from mux.conformance.budget import (
    ActualSpend,
    BudgetGuard,
    ProbeModel,
    ProbePlan,
    SpendReceipt,
    TokenUsage,
)
from mux.conformance.recording import Recorder, sensitive
from mux.conformance.runner import Result


class ProbeRunError(Exception):
    pass


class ProbeOutcome(ProbeModel):
    usage: TokenUsage = TokenUsage()
    actual: ActualSpend | None = None
    result: Result


class ProbeRun(ProbeModel):
    receipt: SpendReceipt
    result: Result
    recording: Path


async def run_probe(
    guard: BudgetGuard,
    plan: ProbePlan,
    recording_path: Path,
    invoke: Callable[[Recorder], Awaitable[ProbeOutcome]],
    *,
    secrets: tuple[str, ...] = (),
) -> ProbeRun:
    """Explicit callback only; future live callers need the lead's authorization.

    The callback must enforce plan.limits across its whole run (including setup
    and cleanup). Providers exceeding those limits trip an overrun and lock out
    subsequent admission; this harness cannot impose server-side token limits.
    """
    recorder = Recorder(secrets=secrets)
    if any(sensitive(name, secrets) for name in (plan.provider, plan.model)):
        raise ProbeRunError("probe metadata must not contain credentials")
    if not recording_path.parent.is_dir():
        raise ProbeRunError("recording directory must already exist")
    reservation = guard.reserve(
        plan, fixture_exists=recording_path.exists() or recording_path.is_symlink()
    )
    status: Literal["completed", "failed", "cancelled"] = "failed"
    outcome: ProbeOutcome | None = None
    try:
        outcome = await invoke(recorder)
        if outcome.result.fixture_id != plan.fixture_id:
            raise ProbeRunError("probe returned another fixture's result")
        status = "completed"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception:
        raise ProbeRunError("probe failed; conservative estimate retained") from None
    finally:
        # A receipt survives even if recording export fails. Failed/cancelled
        # callbacks never release their unknown provider costs.
        receipt = guard.settle(
            reservation,
            status=status,
            limits=plan.limits,
            usage=outcome.usage
            if status == "completed" and outcome is not None and outcome.actual is None
            else None,
            actual=outcome.actual if outcome is not None else None,
        )
        recorder.save(
            recording_path,
            fixture_id=plan.fixture_id,
            provider=plan.provider,
            model=plan.model,
            complete=status == "completed",
        )
    if outcome is None:
        raise ProbeRunError("probe returned no outcome")
    if receipt.status == "overrun":
        raise ProbeRunError("probe exceeded its reservation; provider is blocked")
    return ProbeRun(receipt=receipt, result=outcome.result, recording=recording_path)

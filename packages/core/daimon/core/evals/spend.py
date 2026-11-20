"""What a run has spent and who approved it, kept on the run document (`store.update_run`)."""

from __future__ import annotations

from daimon.core.evals.format import Approval, JsonObject, Run, Spend, to_document
from pydantic import JsonValue


def add_spend(run: Run, category: str, usd: float) -> Run:
    """The run with `usd` more spent under `category` ("agent", "simulator", "judge"...)."""
    spend = run.spend or Spend()
    by = dict(spend.by_category or {})
    by[category] = by.get(category, 0.0) + usd
    total = (spend.total_usd or 0.0) + usd
    return run.model_copy(update={"spend": Spend(total_usd=total, by_category=by)})


def add_judge_approval(run: Run, judge_id: str, approval: Approval, spent_usd: float) -> Run:
    """The run with one more approved judging pass in `ext.judge_approvals`: the judge, who
    approved which ceiling when, and what it spent. The run's own `approval` is for its arms."""
    ext: JsonObject = dict(run.ext or {})
    record: JsonValue = {"judge": judge_id, **to_document(approval), "spent_usd": spent_usd}
    earlier = ext.get("judge_approvals")
    ext["judge_approvals"] = [*(earlier if isinstance(earlier, list) else []), record]
    return run.model_copy(update={"ext": ext})

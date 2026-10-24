"""Run the cases against a target and report PASS, SKIP (with reason) or FAIL per case."""

from __future__ import annotations

import traceback
from collections.abc import Sequence

from .cases import CASES, Case
from .client import HarnessClient
from .target import CaseContext, CaseResult, HarnessTarget, SkipCase


async def run_conformance(
    target: HarnessTarget, *, only: Sequence[str] | None = None, cases: Sequence[Case] = CASES
) -> list[CaseResult]:
    """Run each case in order; one case failing does not stop the next."""
    selected = [case for case in cases if only is None or case.name in only]
    unknown = set(only or ()) - {case.name for case in cases}
    if unknown:
        raise ValueError(f"unknown cases: {sorted(unknown)}")
    client = HarnessClient(
        target.base_url, target.token, timeout_s=target.timeout_s, transport=target.http_transport
    )
    try:
        return [await _run_case(case, target, client) for case in selected]
    finally:
        await client.aclose()


async def _run_case(case: Case, target: HarnessTarget, client: HarnessClient) -> CaseResult:
    client.base_url = target.base_url
    context = CaseContext(target, client, case.name)
    try:
        await case.run(context)
    except SkipCase as skipped:
        return CaseResult(case.name, "skip", str(skipped))
    except AssertionError as failed:
        return CaseResult(case.name, "fail", str(failed) or "assertion failed")
    except Exception as error:
        detail = "".join(traceback.format_exception_only(type(error), error)).strip()
        return CaseResult(case.name, "fail", detail)
    return CaseResult(case.name, "pass")


def format_report(results: Sequence[CaseResult]) -> str:
    lines = [
        f"{result.status.upper():4}  {result.name}"
        + (f"  ({result.detail})" if result.detail else "")
        for result in results
    ]
    counts = {
        status: sum(r.status == status for r in results) for status in ("pass", "skip", "fail")
    }
    lines.append(f"{counts['pass']} passed, {counts['skip']} skipped, {counts['fail']} failed")
    return "\n".join(lines)

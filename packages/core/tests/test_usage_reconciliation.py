"""Offline wire and money-path proofs for Admin bucket reconciliation."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from daimon.core.usage_reconciliation import (
    AdminScope,
    AdminSnapshot,
    AttributionAttestation,
    RecordedPage,
    assess_admin_reconciliation,
    fetch_admin_snapshot,
    propose_admin_reconciliation,
)
from mux.conformance.budget import BudgetLedgerError, BudgetRefused
from mux.conformance.test_budget import plan, setup_guard
from pydantic import SecretStr, ValidationError

DAY = datetime(2026, 10, 9, tzinfo=UTC)
START = int(DAY.timestamp())
END = START + 86400
SCOPE = AdminScope(
    organization_id="org-offline", project_id="proj-isolated", start_time=START, end_time=END
)
RUN = "1" * 32


def cost_row(line_item: str = "luna input", value: str = ".002") -> dict:
    return {
        "object": "organization.costs.result",
        "project_id": SCOPE.project_id,
        "api_source": "agents_api",
        "line_item": line_item,
        "amount": {"value": value, "currency": "usd"},
    }


def usage_row() -> dict:
    return {
        "object": "organization.usage.completions.result",
        "project_id": SCOPE.project_id,
        "api_source": "agents_api",
        "model": "gpt-6-luna",
        "input_tokens": 1000,
        "output_tokens": 100,
        "num_model_requests": 1,
    }


def page_body(rows: list[dict], *, start: int = START, next_page: str | None = None) -> str:
    return json.dumps(
        {
            "object": "page",
            "data": [
                {
                    "object": "bucket",
                    "start_time": start,
                    "end_time": start + 86400,
                    "results": rows,
                }
            ],
            "has_more": next_page is not None,
            "next_page": next_page,
        }
    )


def exports() -> tuple[AdminSnapshot, AdminSnapshot]:
    costs = AdminSnapshot(
        scope=SCOPE,
        endpoint="costs",
        fetched_at=DAY + timedelta(days=2),
        pages=(
            RecordedPage(
                cursor=None,
                status_code=200,
                body=page_body([cost_row(), cost_row("hosted small", ".03")]),
            ),
        ),
    )
    usage = AdminSnapshot(
        scope=SCOPE,
        endpoint="completions",
        fetched_at=costs.fetched_at,
        pages=(RecordedPage(cursor=None, status_code=200, body=page_body([usage_row()])),),
    )
    return costs, usage


def attest(costs: AdminSnapshot, usage: AdminSnapshot, run: str = RUN) -> AttributionAttestation:
    return AttributionAttestation(
        scope_sha256=costs.scope.digest,
        costs_sha256=costs.digest,
        usage_sha256=usage.digest,
        inventory_evidence_sha256="a" * 64,
        final_billing_evidence_sha256="b" * 64,
        run_ids=(run,),
        run_started_at=costs.scope.start_time,
        run_finished_at=costs.scope.end_time - 1,
        finalized_through=END,
        line_items={"luna input": "token", "hosted small": "container"},
    )


@pytest.mark.parametrize("endpoint", ["costs", "completions"])
async def test_injected_admin_transport_covers_all_keys_models_sources_and_pages(endpoint) -> None:
    requests: list[httpx.Request] = []
    scope = SCOPE.model_copy(update={"end_time": END + 86400})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        first = len(requests) == 1
        row = cost_row() if endpoint == "costs" else usage_row()
        return httpx.Response(
            200,
            text=page_body(
                [row], start=START if first else END, next_page="next-offline" if first else None
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await fetch_admin_snapshot(
            client=client,
            admin_key=SecretStr("offline-admin-placeholder"),
            scope=scope,
            endpoint=endpoint,
        )
    assert len(requests) == 2
    assert requests[0].url.host == "api.openai.com"
    assert requests[0].url.scheme == "https"
    path = "costs" if endpoint == "costs" else "usage/completions"
    assert requests[0].url.path == f"/v1/organization/{path}"
    assert all(request.method == "GET" for request in requests)
    expected = [
        ("start_time", str(START)),
        ("end_time", str(END + 86400)),
        ("bucket_width", "1d"),
        ("limit", "31"),
        ("project_ids", "proj-isolated"),
        ("group_by", "project_id"),
        ("group_by", "api_source"),
        ("group_by", "line_item" if endpoint == "costs" else "model"),
    ]
    assert list(requests[0].url.params.multi_items()) == expected
    assert list(requests[1].url.params.multi_items()) == [*expected, ("page", "next-offline")]
    assert requests[0].headers["authorization"] == "Bearer offline-admin-placeholder"
    assert requests[0].headers["openai-organization"] == "org-offline"
    assert "offline-admin-placeholder" not in snapshot.model_dump_json()
    assert tuple(page.cursor for page in snapshot.pages) == (None, "next-offline")


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
async def test_admin_failures_do_not_retry_redirect_or_capture_secret_error_bodies(status) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private error body", headers={"Location": "https://other.invalid"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        costs = await fetch_admin_snapshot(
            client=client, admin_key=SecretStr("offline"), scope=SCOPE, endpoint="costs"
        )
    _, usage = exports()
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=None)
    assert len(calls) == 1
    assert costs.pages[0].body == ""
    assert result.reason == f"admin_http_{status}"
    assert result.accounting_status == "estimated_unverified"
    assert result.proposed_actual_usd is None


async def test_transport_failure_is_unverified_without_exception_text() -> None:
    def handler(request):
        raise httpx.ConnectError("credential or infrastructure details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        costs = await fetch_admin_snapshot(
            client=client, admin_key=SecretStr("offline"), scope=SCOPE, endpoint="costs"
        )
    _, usage = exports()
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=None)
    assert result.reason == "admin_http_0"
    assert "credential" not in costs.model_dump_json()


def test_exact_billed_amount_includes_unlabeled_infrastructure_without_token_price_guess() -> None:
    costs, usage = exports()
    hosted = cost_row("hosted small", ".03")
    hosted["api_source"] = "unlabeled"
    body = page_body([cost_row(value="0.123456789012345678"), hosted])
    # Exercise actual JSON numeric literals (not only string-valued exports).
    body = body.replace('"0.123456789012345678"', "0.123456789012345678")
    costs = costs.model_copy(
        update={"pages": (RecordedPage(cursor=None, status_code=200, body=body),)}
    )
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attest(costs, usage))
    assert result.accounting_status == "ready_for_approval"
    assert result.token_usd == Decimal(".123456789012345678")
    assert result.container_usd == Decimal(".03")
    assert result.proposed_actual_usd == Decimal(".153456789012345678")


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("missing_attestation", "attribution_and_finality_unverified"),
        ("empty_costs", "billed_amount_missing"),
        ("missing_amount", "cost_attribution_unverified"),
        ("null_source", "cost_attribution_unverified"),
        ("wrong_project", "cost_attribution_unverified"),
        ("unclassified_charge", "unclassified_billed_charge"),
        ("duplicate_cost", "duplicate_cost_group"),
        ("duplicate_usage", "duplicate_usage_group"),
        ("unknown_usage", "usage_attribution_unverified"),
        ("foreign_currency", "admin_schema_unverified"),
        ("negative_charge", "admin_schema_unverified"),
        ("mixed_runs", "per_run_attribution_ambiguous"),
        ("no_runs", "per_run_attribution_ambiguous"),
        ("earlier_activity", "run_activity_outside_billed_window"),
        ("later_activity", "run_activity_outside_billed_window"),
        ("late_billing", "billing_not_finalized"),
        ("open_window", "billing_window_still_open"),
        ("wrong_digest", "attestation_digest_mismatch"),
        ("scope_mismatch", "admin_scope_mismatch"),
        ("wrong_endpoint", "admin_endpoint_mismatch"),
        ("partial_page", "pagination_incomplete"),
        ("bad_cursor", "pagination_chain_mismatch"),
        ("duplicate_bucket", "daily_window_incomplete_or_duplicate"),
        ("missing_day", "daily_window_incomplete_or_duplicate"),
        ("truncated_json", "admin_schema_unverified"),
    ],
)
def test_ambiguous_or_incomplete_exports_never_become_actual(defect, reason) -> None:
    costs, usage = exports()
    rows = [cost_row(), cost_row("hosted small", ".03")]
    usages = [usage_row()]
    if defect == "empty_costs":
        rows = []
    if defect == "missing_amount":
        rows[0]["amount"] = None
    if defect == "null_source":
        rows[0]["api_source"] = None
    if defect == "wrong_project":
        rows[0]["project_id"] = "proj-other"
    if defect == "unclassified_charge":
        rows.append(cost_row("web search", ".004"))
    if defect == "duplicate_cost":
        rows.append(cost_row())
    if defect == "duplicate_usage":
        usages.append(usage_row())
    if defect == "unknown_usage":
        usages[0]["output_tokens"] = None
    if defect == "foreign_currency":
        rows[0]["amount"]["currency"] = "eur"
    if defect == "negative_charge":
        rows[0]["amount"]["value"] = "-.01"
    costs = costs.model_copy(
        update={"pages": (RecordedPage(cursor=None, status_code=200, body=page_body(rows)),)}
    )
    usage = usage.model_copy(
        update={"pages": (RecordedPage(cursor=None, status_code=200, body=page_body(usages)),)}
    )
    if defect == "open_window":
        costs = costs.model_copy(update={"fetched_at": DAY})
    if defect == "scope_mismatch":
        usage = usage.model_copy(
            update={"scope": SCOPE.model_copy(update={"organization_id": "org-other"})}
        )
    if defect == "wrong_endpoint":
        costs = costs.model_copy(update={"endpoint": "completions"})
    if defect == "partial_page":
        costs = costs.model_copy(
            update={
                "pages": (
                    RecordedPage(
                        cursor=None, status_code=200, body=page_body(rows, next_page="later")
                    ),
                )
            }
        )
    if defect == "bad_cursor":
        costs = costs.model_copy(
            update={
                "pages": (RecordedPage(cursor="unexpected", status_code=200, body=page_body(rows)),)
            }
        )
    if defect == "duplicate_bucket":
        duplicated = json.loads(costs.pages[0].body)
        duplicated["data"].append(duplicated["data"][0])
        costs = costs.model_copy(
            update={
                "pages": (RecordedPage(cursor=None, status_code=200, body=json.dumps(duplicated)),)
            }
        )
    if defect == "missing_day":
        costs = costs.model_copy(
            update={"scope": SCOPE.model_copy(update={"end_time": END + 86400})}
        )
        usage = usage.model_copy(update={"scope": costs.scope})
    if defect == "truncated_json":
        costs = costs.model_copy(
            update={"pages": (RecordedPage(cursor=None, status_code=200, body="{"),)}
        )
    attestation = attest(costs, usage)
    if defect == "mixed_runs":
        attestation = attestation.model_copy(update={"run_ids": (RUN, "2" * 32)})
    if defect == "no_runs":
        attestation = attestation.model_copy(update={"run_ids": ()})
    if defect == "earlier_activity":
        attestation = attestation.model_copy(update={"run_started_at": START - 1})
    if defect == "later_activity":
        attestation = attestation.model_copy(update={"run_finished_at": END})
    if defect == "late_billing":
        attestation = attestation.model_copy(update={"finalized_through": END - 1})
    if defect == "wrong_digest":
        attestation = attestation.model_copy(update={"costs_sha256": "c" * 64})
    result = assess_admin_reconciliation(
        costs=costs,
        usage=usage,
        attestation=None if defect == "missing_attestation" else attestation,
    )
    assert result.accounting_status == "estimated_unverified"
    assert result.reason == reason
    assert result.proposed_actual_usd is result.token_usd is result.container_usd is None


def test_evidence_hash_changes_on_raw_export_or_inventory_changes() -> None:
    costs, usage = exports()
    attestation = attest(costs, usage)
    first = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attestation)
    second = assess_admin_reconciliation(
        costs=costs,
        usage=usage,
        attestation=attestation.model_copy(update={"inventory_evidence_sha256": "d" * 64}),
    )
    assert first.evidence_sha256 != second.evidence_sha256
    changed = costs.model_copy(
        update={
            "pages": (RecordedPage(cursor=None, status_code=200, body=costs.pages[0].body + " "),)
        }
    )
    third = assess_admin_reconciliation(costs=changed, usage=usage, attestation=attestation)
    assert third.evidence_sha256 != first.evidence_sha256
    assert third.reason == "attestation_digest_mismatch"


def test_readonly_cli_and_proposal_then_explicit_approved_append_releases_hold(
    tmp_path: Path,
) -> None:
    guard = setup_guard(tmp_path)
    reservation = guard.reserve(plan())
    held = guard.settle(reservation, status="failed", limits=plan().limits)
    costs, usage = exports()
    today_start = int(held.timestamp.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    scope = SCOPE.model_copy(update={"start_time": today_start, "end_time": today_start + 86400})
    costs = costs.model_copy(
        update={
            "scope": scope,
            "fetched_at": datetime.fromtimestamp(today_start + 172800, UTC),
            "pages": (
                RecordedPage(
                    cursor=None,
                    status_code=200,
                    body=page_body(
                        [cost_row(), cost_row("hosted small", ".03")], start=today_start
                    ),
                ),
            ),
        }
    )
    usage = usage.model_copy(
        update={
            "scope": scope,
            "fetched_at": costs.fetched_at,
            "pages": (
                RecordedPage(
                    cursor=None, status_code=200, body=page_body([usage_row()], start=today_start)
                ),
            ),
        }
    )
    attestation = attest(costs, usage, held.run_id).model_copy(
        update={"finalized_through": scope.end_time}
    )
    paths = [guard.config_path, guard.spend_path, guard.lock_path, guard.checkpoint_path]
    before = {path: path.read_bytes() for path in paths}
    proposal = propose_admin_reconciliation(
        guard=guard, costs=costs, usage=usage, attestation=attestation
    )
    assert {path: path.read_bytes() for path in paths} == before
    assert proposal.actual_usd == Decimal(".032")
    assert proposal.basis == "billed_export"
    assert guard.report()[0].actual_usd is None
    assert guard.report()[0].held_usd == held.held_usd
    for name, value in [("costs", costs), ("usage", usage), ("attestation", attestation)]:
        (tmp_path / f"{name}.json").write_text(value.model_dump_json())
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "daimon.core.usage_reconciliation",
            "--costs",
            str(tmp_path / "costs.json"),
            "--usage",
            str(tmp_path / "usage.json"),
            "--attestation",
            str(tmp_path / "attestation.json"),
            "--guard-config",
            str(guard.config_path),
            "--spend-ledger",
            str(guard.spend_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert process.stdout.splitlines()[-1] == proposal.digest
    assert {path: path.read_bytes() for path in paths} == before
    with pytest.raises(BudgetRefused):
        guard.reconcile(proposal)
    config = json.loads(guard.config_path.read_text())
    config["approved_reconciliations"] = [proposal.digest]
    guard.config_path.write_text(json.dumps(config))
    settled = guard.reconcile(proposal)
    assert settled.accounting_status == "actual"
    assert settled.actual_usd == Decimal(".032") and settled.held_usd == 0
    assert guard.spend_path.read_bytes().startswith(before[guard.spend_path])
    with pytest.raises(BudgetLedgerError):
        guard.reconcile(proposal)
    with pytest.raises(BudgetLedgerError):
        propose_admin_reconciliation(guard=guard, costs=costs, usage=usage, attestation=attestation)


@pytest.mark.parametrize(
    "update", [{"start_time": START + 1}, {"end_time": START}, {"end_time": START + 32 * 86400}]
)
def test_scope_refuses_partial_or_unbounded_days(update) -> None:
    with pytest.raises(ValidationError):
        AdminScope.model_validate({**SCOPE.model_dump(), **update})


async def test_repeated_page_cursor_stops_and_partial_export_keeps_hold() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=page_body([cost_row()], next_page="repeated"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        costs = await fetch_admin_snapshot(
            client=client, admin_key=SecretStr("offline"), scope=SCOPE, endpoint="costs"
        )
    _, usage = exports()
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attest(costs, usage))
    assert len(calls) == 2
    assert result.reason == "pagination_cursor_invalid"
    assert result.accounting_status == "estimated_unverified"


def test_final_explicit_zero_is_distinct_from_empty_or_missing_amount() -> None:
    costs, usage = exports()
    costs = costs.model_copy(
        update={
            "pages": (
                RecordedPage(cursor=None, status_code=200, body=page_body([cost_row(value="0")])),
            )
        }
    )
    usage = usage.model_copy(
        update={"pages": (RecordedPage(cursor=None, status_code=200, body=page_body([])),)}
    )
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attest(costs, usage))
    assert result.accounting_status == "ready_for_approval"
    assert result.proposed_actual_usd == Decimal(0)


@pytest.mark.parametrize(
    "provider,reason",
    [
        ("anthropic", "held_openai_run_required"),
        ("openai", "held_run_outside_billed_window"),
    ],
)
def test_billed_export_cannot_reconcile_a_foreign_provider_or_window(
    tmp_path, provider, reason
) -> None:
    guard = setup_guard(tmp_path)
    reservation = guard.reserve(plan(provider=provider))
    held = guard.settle(reservation, status="failed", limits=plan(provider=provider).limits)
    costs, usage = exports()
    attestation = attest(costs, usage, held.run_id)
    before = guard.spend_path.read_bytes()
    with pytest.raises(ValueError, match=reason):
        propose_admin_reconciliation(guard=guard, costs=costs, usage=usage, attestation=attestation)
    assert guard.spend_path.read_bytes() == before

"""Read-only OpenAI Admin exports and unsigned, evidence-bound spend proposals.

Daily organization buckets are not session bills. Only an externally audited,
isolated project window belonging to one run can be proposed here. Pagination
does not establish billing finality. No credential discovery, settlement or
approval is performed by this module; existing holds remain authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mux.conformance.budget import BudgetGuard, Money, Reconciliation
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Count = Annotated[int, Field(strict=True, ge=0)]
Endpoint = Literal["costs", "completions"]


class _Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class AdminScope(_Evidence):
    """All keys, models and sources in one project, over whole UTC days."""

    organization_id: Annotated[str, Field(min_length=1, max_length=128)]
    project_id: Annotated[str, Field(min_length=1, max_length=128)]
    start_time: Count
    end_time: Count

    @model_validator(mode="after")
    def daily_window(self) -> AdminScope:
        if (
            self.start_time % 86400
            or self.end_time % 86400
            or not 0 < self.end_time - self.start_time <= 31 * 86400
        ):
            raise ValueError("require one to 31 complete UTC days")
        return self

    def parameters(self, endpoint: Endpoint, cursor: str | None) -> list[tuple[str, str]]:
        groups = (
            ("project_id", "api_source", "line_item")
            if endpoint == "costs"
            else ("project_id", "api_source", "model")
        )
        result = [
            ("start_time", str(self.start_time)),
            ("end_time", str(self.end_time)),
            ("bucket_width", "1d"),
            ("limit", "31"),
            ("project_ids[]", self.project_id),
            *(("group_by[]", group) for group in groups),
        ]
        if cursor is not None:
            result.append(("page", cursor))
        return result


class AdminRequestEvidence(_Evidence):
    method: Literal["GET"]
    url: str
    organization_id: str
    parameters: tuple[tuple[str, str], ...]


class RecordedPage(_Evidence):
    cursor: str | None
    status_code: Count
    request: AdminRequestEvidence | None = None
    # Successful response bytes are retained as UTF-8 text, preserving numeric
    # literals for Decimal parsing. Error response bodies/headers are omitted.
    body: str


class AdminSnapshot(_Evidence):
    scope: AdminScope
    endpoint: Endpoint
    fetched_at: datetime
    pages: tuple[RecordedPage, ...]


class _APIModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Amount(_APIModel):
    value: Money
    currency: Literal["usd"]


class _Cost(_APIModel):
    object: Literal["organization.costs.result"]
    amount: _Amount | None = None
    project_id: str | None = None
    api_source: Literal["agents_api", "unlabeled"] | None = None
    line_item: str | None = None


class _Usage(_APIModel):
    object: Literal["organization.usage.completions.result"]
    project_id: str | None = None
    api_source: Literal["agents_api", "unlabeled"] | None = None
    model: str | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    num_model_requests: Count | None = None


class _Bucket[Row](_APIModel):
    object: Literal["bucket"]
    start_time: Count
    end_time: Count
    results: tuple[Row, ...]


class _Page[Row](_APIModel):
    object: Literal["page"]
    data: tuple[_Bucket[Row], ...]
    has_more: bool = Field(strict=True)
    next_page: str | None


def _request_url(endpoint: Endpoint) -> str:
    path = "costs" if endpoint == "costs" else "usage/completions"
    return f"https://api.openai.com/v1/organization/{path}"


def _request_matches(
    evidence: AdminRequestEvidence | None, scope: AdminScope, endpoint: Endpoint, cursor: str | None
) -> bool:
    return evidence is not None and (
        evidence.method == "GET"
        and evidence.url == _request_url(endpoint)
        and evidence.organization_id == scope.organization_id
        and sorted(evidence.parameters) == sorted(scope.parameters(endpoint, cursor))
    )


def _reject_client_defaults(client: httpx.AsyncClient) -> None:
    standard_headers = {"accept", "accept-encoding", "connection", "user-agent"}
    if (
        client.params
        or client.auth is not None
        or client.cookies
        or set(client.headers).difference(standard_headers)
        or any(client.event_hooks.values())
    ):
        raise ValueError("admin_client_defaults_not_allowed")


async def fetch_admin_snapshot(
    *, client: httpx.AsyncClient, admin_key: SecretStr, scope: AdminScope, endpoint: Endpoint
) -> AdminSnapshot:
    """Explicit injected transport/key only; no retries or alternate credentials.

    Callers own live authorization and client lifetime. Tests use MockTransport.
    Query/auth/cookie/custom-header/event-hook defaults are rejected before IO;
    every page binds the effective request to the complete declared scope.
    Redirects and pagination loops cannot forward the credential elsewhere.
    A failed request is recorded without a body or exception text, then refused
    by the assessor; a partial fetch never supplies a final cost.
    """
    pages: list[RecordedPage] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        _reject_client_defaults(client)
        # Request/send avoids merging client query/header/cookie defaults. The
        # preflight rejection also catches defaults changed between pages.
        request = httpx.Request(
            "GET",
            _request_url(endpoint),
            params=tuple(scope.parameters(endpoint, cursor)),
            headers={
                "Authorization": f"Bearer {admin_key.get_secret_value()}",
                "OpenAI-Organization": scope.organization_id,
            },
        )
        try:
            response = await client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError:
            pages.append(RecordedPage(cursor=cursor, status_code=0, body=""))
            break
        effective = response.request
        evidence = None
        if effective.method == "GET" and effective.headers.get("authorization") == (
            f"Bearer {admin_key.get_secret_value()}"
        ):
            evidence = AdminRequestEvidence(
                method="GET",
                url=str(effective.url.copy_with(query=None)),
                organization_id=effective.headers.get("openai-organization", ""),
                parameters=tuple(effective.url.params.multi_items()),
            )
        verified = _request_matches(evidence, scope, endpoint, cursor)
        pages.append(
            RecordedPage(
                cursor=cursor,
                status_code=response.status_code,
                request=evidence if verified else None,
                body=response.text if response.status_code == 200 and verified else "",
            )
        )
        if response.status_code != 200 or not verified:
            break
        try:
            page = _Page[_Cost | _Usage].model_validate(
                json.loads(response.text, parse_float=Decimal)
            )
        except (ValidationError, ValueError):
            break
        if not page.has_more or not page.next_page or page.next_page in seen:
            break
        cursor = page.next_page
        seen.add(cursor)
    return AdminSnapshot(
        scope=scope, endpoint=endpoint, fetched_at=datetime.now(UTC), pages=tuple(pages)
    )


class AdminUsageTotals(_Evidence):
    input_tokens: Count
    output_tokens: Count
    num_model_requests: Count


class AttributionAttestation(_Evidence):
    """Audited external facts, NOT facts inferred from Admin bucket responses.

    The reviewer must inspect the referenced inventory and final billing
    evidence before approving the resulting BudgetGuard proposal. The inventory
    covers every key/source/session in the full project window, including
    billable infrastructure. Multiple runs or shared projects are unsupported.
    Exact provider line item names must be classified without dropping charges.
    Independent final-bill amounts and inventory usage totals must match the
    captured exports exactly. Copying totals from those exports is not evidence
    of completeness and must not be approved.
    """

    scope_sha256: Digest
    costs_sha256: Digest
    usage_sha256: Digest
    inventory_evidence_sha256: Digest
    final_billing_evidence_sha256: Digest
    run_ids: tuple[RunId, ...]
    # Audited activity bounds, including the last billable infrastructure use.
    run_started_at: Count
    run_finished_at: Count
    finalized_through: Count
    line_items: dict[str, Literal["token", "container"]]
    # These are independently evidenced full-project amounts/counters, never
    # calculated by the assessor from the candidate export being checked.
    billed_total_usd: Money
    billed_line_item_totals: dict[str, Money]
    expected_usage: AdminUsageTotals


class ReconciliationAssessment(_Evidence):
    accounting_status: Literal["estimated_unverified", "ready_for_approval"]
    reason: str
    evidence_sha256: Digest
    run_id: RunId | None = None
    token_usd: Money | None = None
    container_usd: Money | None = None
    # This is an unsigned proposed amount. Actual dollars are recorded only by
    # the existing lead-approved append-only BudgetGuard.reconcile path.
    proposed_actual_usd: Money | None = None


class _Incomplete(ValueError):
    pass


def _buckets[Row](snapshot: AdminSnapshot, schema: type[_Page[Row]]) -> tuple[_Bucket[Row], ...]:
    if not snapshot.pages:
        raise _Incomplete("admin_export_missing")
    expected_cursor: str | None = None
    seen_cursors: set[str] = set()
    buckets: list[_Bucket[Row]] = []
    for index, recorded in enumerate(snapshot.pages):
        if recorded.status_code != 200:
            raise _Incomplete(f"admin_http_{recorded.status_code}")
        if recorded.cursor != expected_cursor:
            raise _Incomplete("pagination_chain_mismatch")
        if not _request_matches(
            recorded.request, snapshot.scope, snapshot.endpoint, recorded.cursor
        ):
            raise _Incomplete("effective_request_scope_unverified")
        try:
            page = schema.model_validate(json.loads(recorded.body, parse_float=Decimal))
        except (ValidationError, ValueError):
            raise _Incomplete("admin_schema_unverified") from None
        buckets.extend(page.data)
        if page.has_more:
            if not page.next_page or page.next_page in seen_cursors:
                raise _Incomplete("pagination_cursor_invalid")
            seen_cursors.add(page.next_page)
            expected_cursor = page.next_page
            if index == len(snapshot.pages) - 1:
                raise _Incomplete("pagination_incomplete")
        elif page.next_page is not None or index != len(snapshot.pages) - 1:
            raise _Incomplete("pagination_terminal_mismatch")
    wanted = tuple(range(snapshot.scope.start_time, snapshot.scope.end_time, 86400))
    if tuple(bucket.start_time for bucket in buckets) != wanted or any(
        bucket.end_time != bucket.start_time + 86400 for bucket in buckets
    ):
        raise _Incomplete("daily_window_incomplete_or_duplicate")
    return tuple(buckets)


def assess_admin_reconciliation(
    *, costs: AdminSnapshot, usage: AdminSnapshot, attestation: AttributionAttestation | None
) -> ReconciliationAssessment:
    """No apportionment, token-price guess, inferred zero, or ledger mutation."""
    evidence = hashlib.sha256(
        f"{costs.digest}:{usage.digest}:{attestation.digest if attestation else 'none'}".encode()
    ).hexdigest()
    try:
        if costs.endpoint != "costs" or usage.endpoint != "completions":
            raise _Incomplete("admin_endpoint_mismatch")
        if costs.scope != usage.scope:
            raise _Incomplete("admin_scope_mismatch")
        cost_buckets = _buckets(costs, _Page[_Cost])
        usage_buckets = _buckets(usage, _Page[_Usage])
        if attestation is None:
            raise _Incomplete("attribution_and_finality_unverified")
        if (
            attestation.scope_sha256 != costs.scope.digest
            or attestation.costs_sha256 != costs.digest
            or attestation.usage_sha256 != usage.digest
        ):
            raise _Incomplete("attestation_digest_mismatch")
        if len(attestation.run_ids) != 1:
            raise _Incomplete("per_run_attribution_ambiguous")
        if not (
            costs.scope.start_time
            <= attestation.run_started_at
            <= attestation.run_finished_at
            < costs.scope.end_time
        ):
            raise _Incomplete("run_activity_outside_billed_window")
        if attestation.finalized_through < costs.scope.end_time:
            raise _Incomplete("billing_not_finalized")
        if any(
            snapshot.fetched_at.tzinfo is None
            or snapshot.fetched_at.timestamp() < costs.scope.end_time
            for snapshot in (costs, usage)
        ):
            raise _Incomplete("billing_window_still_open")
        input_tokens = output_tokens = requests = 0
        for bucket in usage_buckets:
            seen_usage: set[tuple[str, str]] = set()
            for result in bucket.results:
                if (
                    result.project_id != usage.scope.project_id
                    or result.api_source is None
                    or not result.model
                    or result.input_tokens is None
                    or result.output_tokens is None
                    or result.num_model_requests is None
                ):
                    raise _Incomplete("usage_attribution_unverified")
                key = (result.api_source, result.model)
                if key in seen_usage:
                    raise _Incomplete("duplicate_usage_group")
                seen_usage.add(key)
                input_tokens += result.input_tokens
                output_tokens += result.output_tokens
                requests += result.num_model_requests
        if (
            AdminUsageTotals(
                input_tokens=input_tokens, output_tokens=output_tokens, num_model_requests=requests
            )
            != attestation.expected_usage
        ):
            raise _Incomplete("complete_usage_totals_mismatch")
        with localcontext() as context:
            context.prec = 64
            token = container = Decimal(0)
            billed_lines: dict[str, Decimal] = {}
            if not attestation.billed_line_item_totals or (
                set(attestation.line_items) != set(attestation.billed_line_item_totals)
                or sum(attestation.billed_line_item_totals.values(), Decimal(0))
                != attestation.billed_total_usd
            ):
                raise _Incomplete("complete_billing_inventory_unverified")
            for bucket in cost_buckets:
                # Empty successful responses can be delayed publishing. They
                # never prove zero; zero requires an explicit finalized row.
                if not bucket.results:
                    raise _Incomplete("billed_amount_missing")
                seen_costs: set[tuple[str, str]] = set()
                for result in bucket.results:
                    if (
                        result.project_id != costs.scope.project_id
                        or result.api_source is None
                        or result.line_item is None
                        or result.amount is None
                    ):
                        raise _Incomplete("cost_attribution_unverified")
                    key = (result.api_source, result.line_item)
                    if key in seen_costs:
                        raise _Incomplete("duplicate_cost_group")
                    seen_costs.add(key)
                    category = attestation.line_items.get(result.line_item)
                    if category == "token":
                        token += result.amount.value
                    elif category == "container":
                        container += result.amount.value
                    else:
                        raise _Incomplete("unclassified_billed_charge")
                    billed_lines[result.line_item] = (
                        billed_lines.get(result.line_item, Decimal(0)) + result.amount.value
                    )
            if billed_lines != attestation.billed_line_item_totals:
                raise _Incomplete("complete_billed_line_items_mismatch")
            if token + container != attestation.billed_total_usd:
                raise _Incomplete("complete_billed_total_mismatch")
            return ReconciliationAssessment(
                accounting_status="ready_for_approval",
                reason="isolated_run_final_billed_export",
                evidence_sha256=evidence,
                run_id=attestation.run_ids[0],
                token_usd=token,
                container_usd=container,
                proposed_actual_usd=token + container,
            )
    except _Incomplete as error:
        return ReconciliationAssessment(
            accounting_status="estimated_unverified", reason=str(error), evidence_sha256=evidence
        )


def propose_admin_reconciliation(
    *,
    guard: BudgetGuard,
    costs: AdminSnapshot,
    usage: AdminSnapshot,
    attestation: AttributionAttestation,
) -> Reconciliation:
    """Re-assess originals, then bind the unsigned proposal to the current hold."""
    result = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attestation)
    if result.accounting_status != "ready_for_approval":
        raise ValueError(result.reason)
    assert result.run_id is not None and result.token_usd is not None
    assert result.container_usd is not None
    receipt = next((row for row in guard.report() if row.run_id == result.run_id), None)
    if receipt is None or receipt.provider != "openai":
        raise ValueError("held_openai_run_required")
    if not costs.scope.start_time <= receipt.timestamp.timestamp() < costs.scope.end_time:
        raise ValueError("held_run_outside_billed_window")
    return guard.propose_reconciliation(
        result.run_id,
        evidence_sha256=result.evidence_sha256,
        basis="billed_export",
        token_usd=result.token_usd,
        container_usd=result.container_usd,
    )


def main() -> None:
    """Offline exports only; stdout assessment/proposal, never approval/application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--costs", type=Path, required=True)
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--guard-config", type=Path)
    parser.add_argument("--spend-ledger", type=Path)
    args = parser.parse_args()
    costs = AdminSnapshot.model_validate_json(args.costs.read_bytes())
    usage = AdminSnapshot.model_validate_json(args.usage.read_bytes())
    attestation = (
        AttributionAttestation.model_validate_json(args.attestation.read_bytes())
        if args.attestation
        else None
    )
    assessment = assess_admin_reconciliation(costs=costs, usage=usage, attestation=attestation)
    print(assessment.model_dump_json())
    if args.guard_config or args.spend_ledger:
        if not args.guard_config or not args.spend_ledger:
            parser.error("--guard-config and --spend-ledger must be supplied together")
        if assessment.accounting_status == "ready_for_approval" and attestation is not None:
            proposal = propose_admin_reconciliation(
                guard=BudgetGuard(config_path=args.guard_config, spend_path=args.spend_ledger),
                costs=costs,
                usage=usage,
                attestation=attestation,
            )
            print(proposal.model_dump_json())
            print(proposal.digest)


if __name__ == "__main__":
    main()

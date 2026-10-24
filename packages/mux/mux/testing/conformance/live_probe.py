"""Explicit budgeted orchestration for normalized evidence; offline by default."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, get_args

import httpx
from pydantic import Field, model_validator

from mux.testing.conformance.budget import (
    H6_MODELS,
    ActualSpend,
    BudgetGuard,
    BudgetRefused,
    Identifier,
    ProbeModel,
    ProbePlan,
    Sha256,
    SpendReceipt,
    TokenUsage,
    omit_none,
)
from mux.testing.conformance.recording import Recorder, sensitive
from mux.testing.conformance.runner import PendingKind, PendingReason, Result

H6EvidenceKind = Literal[
    "go_finance",
    "arrangement",
    "preview_confirm",
    "setting_audit",
    "revert_audit",
    "usage_settlement",
    "cleanup",
    "signed_manifest",
]


class H6TurnReceipt(ProbeModel):
    """Observed native/visible identities, not fixture-declared generation."""

    provider: Identifier
    model: Identifier
    thread_id: Identifier
    session_id: Identifier
    binding_id: Identifier
    root_id: Identifier
    input_id: Identifier
    message_ids: tuple[Identifier, ...] = Field(min_length=1)
    footer_provider: Identifier
    footer_model: Identifier
    native_reply_sha256: Sha256
    visible_reply_sha256: Sha256


class H6Receipt(ProbeModel):
    """Whole-cell receipt shape; callers must verify its referenced artifacts."""

    fixture_id: Literal["H6-ANTHROPIC", "H6-OPENAI", "H6-GEMINI"]
    attempt_id: Identifier
    scope_sha256: Sha256
    turns: tuple[H6TurnReceipt, ...] = Field(min_length=5, max_length=5)
    evidence: tuple[tuple[H6EvidenceKind, Sha256], ...]

    @model_validator(mode="after")
    def chain(self) -> H6Receipt:
        required = set(get_args(H6EvidenceKind))
        if {name for name, _ in self.evidence} != required or len(self.evidence) != len(required):
            raise ValueError("staging cell needs every bound receipt reference")
        target = self.fixture_id.removeprefix("H6-").lower()
        predecessor = {"anthropic": "gemini", "openai": "anthropic", "gemini": "openai"}[target]
        suffixes = (
            "baseline",
            "target",
            "retained-before-revert",
            "reverted-new",
            "retained-after-revert",
        )
        for turn, provider, suffix in zip(
            self.turns,
            (predecessor, target, predecessor, predecessor, target),
            suffixes,
            strict=True,
        ):
            expected = hashlib.sha256(f"H6-{self.attempt_id}-{suffix}".encode()).hexdigest()
            if (
                turn.provider != provider
                or turn.footer_provider != provider
                or turn.model != H6_MODELS[provider]
                or turn.footer_model != turn.model
                or turn.native_reply_sha256 != expected
                or turn.visible_reply_sha256 != expected
            ):
                raise ValueError("staging native provider, model, footer or reply changed")
        identities = [(t.thread_id, t.session_id, t.binding_id) for t in self.turns]
        if identities[0] != identities[2] or identities[1] != identities[4]:
            raise ValueError("staging follow-up replaced its original binding")
        for field, expected_count in (
            ("thread_id", 3),
            ("session_id", 3),
            ("binding_id", 3),
            ("root_id", 5),
            ("input_id", 5),
        ):
            if len({getattr(turn, field) for turn in self.turns}) != expected_count:
                raise ValueError("staging native identities collide")
        messages = [message for turn in self.turns for message in turn.message_ids]
        if len(set(messages)) != len(messages):
            raise ValueError("staging visible delivery identities collide")
        return self


class ProbeRunError(Exception):
    pass


class ProbeOutcome(ProbeModel):
    usage: TokenUsage = TokenUsage()
    actual: ActualSpend | None = None
    result: Result
    h6: H6Receipt | None = Field(default=None, exclude_if=omit_none)


class ProbeRun(ProbeModel):
    receipt: SpendReceipt
    result: Result
    recording: Path
    h6: H6Receipt | None = Field(default=None, exclude_if=omit_none)


async def run_probe(
    guard: BudgetGuard,
    plan: ProbePlan,
    recording_path: Path,
    invoke: Callable[[Recorder], Awaitable[ProbeOutcome]],
    *,
    secrets: tuple[str, ...] = (),
    transport: httpx.AsyncBaseTransport | None = None,
    verify_h6: Callable[[H6Receipt], None] | None = None,
) -> ProbeRun:
    """Explicit callback only; future live callers need the lead's authorization.

    The callback must enforce plan.limits across its whole run (including setup
    and cleanup). Providers exceeding those limits trip an overrun and lock out
    subsequent admission; this harness cannot impose server-side token limits.
    """
    plan = ProbePlan.model_validate(plan.model_dump())
    if plan.h6 is not None and transport is None:
        raise ProbeRunError("staging request edge is unbound")
    sent: set[tuple[str, str]] = set()

    def request_edge(request: httpx.Request) -> None:
        binding = plan.h6
        if binding is None:
            raise BudgetRefused("staging request binding is absent")
        guard.require_reservation(reservation, plan)
        route = (request.method, request.url.path)
        origin = str(request.url.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        if (
            request.url.username
            or request.url.password
            or request.url.fragment
            or origin != binding.origin
            or route not in binding.requests
            or len(recorder.request_edges) >= binding.request_limit
            or (request.method != "GET" and route in sent)
        ):
            raise BudgetRefused("staging native request is outside the admitted leg")
        sent.add(route)

    recorder = Recorder(
        secrets=secrets,
        transport=transport,
        request_guard=request_edge if plan.h6 is not None else None,
    )
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
        if plan.h6 is not None:
            if not recorder.request_edges:
                raise ProbeRunError("staging callback bypassed the bound native transport")
            if outcome.h6 is not None:
                proof = H6Receipt.model_validate(outcome.h6.model_dump())
                if (proof.fixture_id, proof.attempt_id, proof.scope_sha256) != (
                    plan.fixture_id,
                    plan.h6.attempt_id,
                    plan.h6.scope_sha256,
                ):
                    raise ProbeRunError("staging cell evidence belongs to another arrangement")
                if verify_h6 is not None and verify_h6(proof) is not None:
                    raise ProbeRunError("staging artifact verifier returned an approval value")
        status = (
            "failed" if plan.h6 is not None and outcome.result.status == "fail" else "completed"
        )
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
    result = outcome.result
    if (
        plan.h6 is not None
        and result.status == "pass"
        and (outcome.h6 is None or verify_h6 is None or receipt.accounting_status != "actual")
    ):
        result = Result(
            plan.fixture_id,
            "pending",
            ("whole-cell receipts or actual accounting unbound",),
            PendingReason(PendingKind.ADAPTER_DEPENDENCY, "staging evidence unbound"),
        )
    return ProbeRun(receipt=receipt, result=result, recording=recording_path, h6=outcome.h6)

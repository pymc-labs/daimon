"""Tests for daimon.core.turn.posture — the billing posture tagged union.

Covers D-05/D-06/D-07: a bare async callable satisfies `UsageRecorder`, a
bound `functools.partial(record_turn_usage, ...)` satisfies it too, and both
`Billed`/`BillingExempt` are frozen dataclasses.
"""

from __future__ import annotations

import dataclasses
import functools
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core import usage_recording
from daimon.core.turn.posture import (
    AutoApprove,
    Billed,
    BillingExempt,
    RequireApproval,
    UsageRecorder,
)


def _make_event(*, event_id: str = "sevt_1") -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        type="span.model_request_end",
        model_request_start_id="mrs_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            input_tokens=10,
            output_tokens=5,
        ),
        processed_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_error=False,
    )


async def test_bare_async_callable_satisfies_usage_recorder_protocol() -> None:
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []

    async def recorder(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        calls.append(event)

    typed_recorder: UsageRecorder = recorder
    event = _make_event()
    await typed_recorder(event=event)

    assert calls == [event], "a bare async callable matching the Protocol shape should be invocable"


async def test_bound_record_turn_usage_partial_satisfies_usage_recorder_protocol() -> None:
    bound: UsageRecorder = functools.partial(
        usage_recording.record_turn_usage,
        sessionmaker=None,  # type: ignore[arg-type]
        tenant_id=None,
        platform_user_id=None,
        managed_session_id="s1",
        model_id="claude-opus-4-7",
        markup=Decimal("1.0"),
        pricing=None,
    )

    # tenant_id=None short-circuits before touching sessionmaker, so this
    # exercises the real DM no-op path through the bound partial.
    await bound(event=_make_event())


def test_billed_is_frozen() -> None:
    async def recorder(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        return None

    billed = Billed(record=recorder)
    with pytest.raises(dataclasses.FrozenInstanceError):
        billed.record = recorder  # type: ignore[misc]


def test_billing_exempt_is_frozen() -> None:
    exempt = BillingExempt(reason="cli-operator-run")
    with pytest.raises(dataclasses.FrozenInstanceError):
        exempt.reason = "cli-operator-run"  # type: ignore[misc]


def test_billing_exempt_accepts_headless_unrecorded_reason() -> None:
    """Plan 19-09: a headless turn invoked without a usage_record_factory is
    genuinely exempt, distinct from the CLI operator bypass."""
    exempt = BillingExempt(reason="headless-unrecorded")
    assert exempt.reason == "headless-unrecorded"


def test_require_approval_is_frozen() -> None:
    posture = RequireApproval()
    with pytest.raises(dataclasses.FrozenInstanceError):
        posture.never = "never"  # type: ignore[attr-defined]


def test_auto_approve_is_frozen() -> None:
    posture = AutoApprove()
    with pytest.raises(dataclasses.FrozenInstanceError):
        posture.never = "never"  # type: ignore[attr-defined]


def test_require_approval_instances_are_equal() -> None:
    assert RequireApproval() == RequireApproval(), (
        "same-type frozen instances should compare equal so `match` and default "
        "arguments behave predictably"
    )


def test_auto_approve_instances_are_equal() -> None:
    assert AutoApprove() == AutoApprove(), (
        "same-type frozen instances should compare equal so `match` and default "
        "arguments behave predictably"
    )


def test_require_approval_and_auto_approve_are_not_equal() -> None:
    assert RequireApproval() != AutoApprove()  # type: ignore[comparison-overlap]


def _describe(posture: RequireApproval | AutoApprove) -> str:
    match posture:
        case RequireApproval():
            return "require-approval"
        case AutoApprove():
            return "auto-approve"


def test_tool_confirmation_union_narrows_correctly_under_match() -> None:
    assert _describe(RequireApproval()) == "require-approval"
    assert _describe(AutoApprove()) == "auto-approve"

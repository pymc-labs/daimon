"""Every way a turn ends yields exactly one `TerminationReason`.

Driver paths run through the scripted fakes and assert the reason on both the
returned state and the state the terminal hook received -- the two have to
agree, because notices draw from the hook and records from the return value.
Refusals that never reach a driver go through `termination_reason`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast, get_args

import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_mcp_authentication_failed_error import (
    BetaManagedAgentsMCPAuthenticationFailedError,
)
from anthropic.types.beta.sessions.beta_managed_agents_model_overloaded_error import (
    BetaManagedAgentsModelOverloadedError,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_exhausted import (
    BetaManagedAgentsRetryStatusExhausted,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_retrying import (
    BetaManagedAgentsRetryStatusRetrying,
)
from daimon.core.errors import TurnError, TurnKind
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.turn import TerminationReason, run_turn, termination_reason
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    AdmissionDenied,
    MissingTurnConfigError,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import denial_termination_reason
from daimon.testing.turn_fakes import (
    BlockForever,
    FakeAnthropic,
    RaiseConnection,
    RaiseRateLimit,
    RaiseStatus,
    RecordingLifecycle,
    YieldEvent,
)

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_requires_action,
    make_session_error,
    make_status_idle,
    make_status_terminated,
)

_FROZEN_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)
_EXEMPT = BillingExempt(reason="cli-operator-run")


def _now() -> datetime:
    return _FROZEN_NOW


def _idle(event_id: str) -> YieldEvent:
    return YieldEvent(make_status_idle(event_id=event_id, stop_reason=make_end_turn()))


def _mcp_exhausted(event_id: str) -> YieldEvent:
    return YieldEvent(
        make_session_error(
            event_id=event_id,
            error=BetaManagedAgentsMCPAuthenticationFailedError(
                type="mcp_authentication_failed_error",
                mcp_server_name="notion",
                message="access forbidden",
                retry_status=BetaManagedAgentsRetryStatusExhausted(type="exhausted"),
            ),
        )
    )


def _overloaded_retrying(event_id: str) -> YieldEvent:
    return YieldEvent(
        make_session_error(
            event_id=event_id,
            error=BetaManagedAgentsModelOverloadedError(
                type="model_overloaded_error",
                message="overloaded",
                retry_status=BetaManagedAgentsRetryStatusRetrying(type="retrying"),
            ),
        )
    )


@dataclass(frozen=True)
class _Case:
    scripts: list[list[object]]
    expected: TerminationReason
    cancel_after_s: float | None = None
    interrupt_timeout_s: float = 5.0


_DRIVER_CASES: dict[str, _Case] = {
    "completed": _Case(
        [[YieldEvent(make_agent_message(event_id="m", text="hi")), _idle("s")]],
        TerminationReason.COMPLETED,
    ),
    "degraded_but_answered": _Case(
        [
            [
                _mcp_exhausted("e"),
                YieldEvent(make_agent_message(event_id="m", text="hi")),
                _idle("s"),
            ]
        ],
        TerminationReason.COMPLETED,
    ),
    "session_terminated": _Case(
        [[YieldEvent(make_status_terminated(event_id="t")), BlockForever()]],
        TerminationReason.SESSION_TERMINATED,
    ),
    "requires_action": _Case(
        [
            [
                YieldEvent(
                    make_status_idle(
                        event_id="s", stop_reason=make_requires_action(event_ids=["tu_1"])
                    )
                )
            ]
        ],
        TerminationReason.REQUIRES_ACTION,
    ),
    "mcp_degraded_empty": _Case(
        [[_mcp_exhausted("e"), _idle("s")]], TerminationReason.MCP_DEGRADED_EMPTY
    ),
    "retrying_unsettled": _Case(
        [[_overloaded_retrying("e"), _idle("s")]], TerminationReason.RETRYING_UNSETTLED
    ),
    "terminal_session_error": _Case(
        [[YieldEvent(make_session_error(event_id="e")), _idle("s")]], TerminationReason.UPSTREAM
    ),
    "connection_lost": _Case(
        [[RaiseConnection()], [RaiseConnection()]], TerminationReason.CONNECTION_LOST
    ),
    "upstream_status": _Case([[RaiseStatus(status_code=500)]], TerminationReason.UPSTREAM),
    "rate_limited": _Case([[RaiseRateLimit()]], TerminationReason.RATE_LIMITED),
    "interrupt_acked": _Case(
        [
            [YieldEvent(make_agent_message(event_id="m", text="partial")), BlockForever()],
            [_idle("a")],
        ],
        TerminationReason.INTERRUPTED,
        cancel_after_s=0.02,
    ),
    "interrupt_timeout": _Case(
        [[BlockForever()], [BlockForever()]],
        TerminationReason.INTERRUPT_TIMEOUT,
        cancel_after_s=0.02,
        interrupt_timeout_s=0.05,
    ),
}


def _terminal_state(lc: RecordingLifecycle) -> TurnState:
    states = [s for s, _ in lc.terminal_failures] + list(lc.terminal_success)
    assert len(states) == 1, "exactly one terminal hook fires per turn"
    return states[0]


@pytest.mark.parametrize("case", _DRIVER_CASES.values(), ids=list(_DRIVER_CASES))
async def test_every_driver_exit_sets_its_reason(case: _Case) -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = case.scripts
    fa.beta.sessions.events.replay_events = []
    lc = RecordingLifecycle()
    cancel = asyncio.Event()

    async def _cancel_later(delay: float) -> None:
        await asyncio.sleep(delay)
        cancel.set()

    async with asyncio.TaskGroup() as tg:
        if case.cancel_after_s is not None:
            tg.create_task(_cancel_later(case.cancel_after_s))
        final = await asyncio.wait_for(
            run_turn(
                anthropic=cast(AsyncAnthropic, fa),
                session_id="sess_1",
                user_message="hi",
                lifecycle=lc,
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=case.interrupt_timeout_s,
                now=_now,
                billing=_EXEMPT,
            ),
            timeout=5.0,
        )

    assert final.termination is case.expected
    assert _terminal_state(lc).termination is case.expected, (
        "the terminal hook must see the same reason the caller gets back"
    )
    assert final.termination.is_failure == (final.error is not None) or (
        final.termination is TerminationReason.INTERRUPTED
    )


async def test_interrupt_before_the_stream_opens_is_interrupted() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[_idle("s")]]
    cancel = asyncio.Event()
    cancel.set()
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=cancel,
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.termination is TerminationReason.INTERRUPTED
    assert _terminal_state(lc).termination is TerminationReason.INTERRUPTED


async def test_driver_ceiling_is_ceiling() -> None:
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[BlockForever()]]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
        deadline=_FROZEN_NOW - timedelta(seconds=1),
    )

    assert final.termination is TerminationReason.CEILING
    assert _terminal_state(lc).termination is TerminationReason.CEILING


@pytest.mark.parametrize("kind", get_args(TurnKind))
def test_every_turn_kind_maps_to_the_member_with_the_same_value(kind: TurnKind) -> None:
    reason = termination_reason(TurnError(kind=kind))
    assert reason.value == kind


_RETRY_AT = datetime(2026, 4, 21, 12, 5, tzinfo=UTC)

_REFUSALS: dict[str, tuple[Callable[[], BaseException | None], TerminationReason]] = {
    "none": (lambda: None, TerminationReason.COMPLETED),
    "balance": (
        lambda: AdmissionDenied(reason="balance_depleted"),
        TerminationReason.ADMISSION_BALANCE_DEPLETED,
    ),
    "cap": (
        lambda: AdmissionDenied(reason="cap_exceeded"),
        TerminationReason.ADMISSION_CAP_EXCEEDED,
    ),
    "channel_budget": (
        lambda: AdmissionDenied(reason="channel_budget_exceeded"),
        TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
    ),
    "protected": (
        lambda: AdmissionDenied(reason="channel_protected"),
        TerminationReason.ADMISSION_CHANNEL_PROTECTED,
    ),
    "pinned": (
        lambda: AdmissionDenied(reason="agent_pinned_elsewhere"),
        TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE,
    ),
    "isolated": (
        lambda: AdmissionDenied(reason="channel_isolated"),
        TerminationReason.ADMISSION_CHANNEL_ISOLATED,
    ),
    "invoker_not_allowed": (
        lambda: AdmissionDenied(reason="invoker_not_allowed"),
        TerminationReason.ADMISSION_DENIED,
    ),
    "missing_config": (
        lambda: MissingTurnConfigError(
            missing=("agent",), agent_name_tier=None, environment_name_tier=None
        ),
        TerminationReason.MISSING_CONFIG,
    ),
    "resolver_miss": (
        lambda: MAResolverMissError(kind="agent", tenant_id=uuid.uuid4(), daimon_tag="x"),
        TerminationReason.RESOLVER_MISS,
    ),
    "preparation_failed": (
        lambda: SessionPreparationFailed(reasons=("r",), stage="s", retry_after=_RETRY_AT),
        TerminationReason.SESSION_PREPARATION_FAILED,
    ),
    "busy": (
        lambda: SessionBusyError(pending_reasons=("r",), retry_after=_RETRY_AT),
        TerminationReason.SESSION_BUSY,
    ),
    "agent_mismatch": (
        lambda: SessionAgentMismatch(
            mapping_id=uuid.uuid4(),
            session_id="sess_1",
            source_agent_id="a",
            destination_agent_id="b",
        ),
        TerminationReason.SESSION_AGENT_MISMATCH,
    ),
    "unclassified": (lambda: RuntimeError("boom"), TerminationReason.UNKNOWN),
}


@pytest.mark.parametrize("case", _REFUSALS.values(), ids=list(_REFUSALS))
def test_refusals_before_a_driver_runs_map_from_the_exception(
    case: tuple[Callable[[], BaseException | None], TerminationReason],
) -> None:
    make_err, expected = case
    assert termination_reason(make_err()) is expected


def test_a_new_admission_denial_maps_to_the_generic_member() -> None:
    """A gate added later needs no enum edit to be recorded."""
    err = AdmissionDenied(reason="balance_depleted")
    err.reason = "access_policy"  # type: ignore[assignment]  # a value this module does not know yet
    assert termination_reason(err) is TerminationReason.ADMISSION_DENIED


@pytest.mark.parametrize("denial", get_args(AdmissionDenialReason))
def test_a_denial_string_maps_like_the_exception_carrying_it(
    denial: AdmissionDenialReason,
) -> None:
    """A gate that decides with `authorize` instead of raising records the same member."""
    assert denial_termination_reason(denial) is termination_reason(
        AdmissionDenied(reason=denial)
    ), f"{denial} must record one member whichever way the refusal is reported"


def test_only_completed_and_interrupted_are_not_failures() -> None:
    assert {r for r in TerminationReason if not r.is_failure} == {
        TerminationReason.COMPLETED,
        TerminationReason.INTERRUPTED,
    }


async def test_a_terminated_session_with_no_terminal_event_is_not_completed() -> None:
    """MA reports `terminated`, but neither the stream nor the replay shows the
    turn ending: nothing proves it finished, so it must not read as success."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[]]
    fa.beta.sessions.events.replay_events = []
    fa.beta.sessions.retrieve_statuses = ["terminated"]
    lc = RecordingLifecycle()

    final = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=_EXEMPT,
    )

    assert final.termination is TerminationReason.SESSION_TERMINATED
    assert final.error is not None and final.error.kind == "upstream"
    assert lc.terminal_success == [] and len(lc.terminal_failures) == 1
    assert _terminal_state(lc).termination is TerminationReason.SESSION_TERMINATED


def test_an_unknown_turn_kind_maps_to_unknown_instead_of_raising() -> None:
    err = TurnError(kind="bogus")  # type: ignore[arg-type]  # off the Literal on purpose
    assert termination_reason(err) is TerminationReason.UNKNOWN


def test_a_denial_without_a_reason_maps_to_the_generic_member() -> None:
    class _Bare(AdmissionDenied):
        def __init__(self) -> None:
            Exception.__init__(self, "denied")

    assert termination_reason(_Bare()) is TerminationReason.ADMISSION_DENIED


def test_the_value_set_is_pinned() -> None:
    """Values are storage strings. A rename or removal must fail here first."""
    assert {m.value for m in TerminationReason} == {
        "completed",
        "interrupted",
        "interrupt_timeout",
        "connection_lost",
        "upstream",
        "rate_limited",
        "session_terminated",
        "mcp_degraded_empty",
        "retrying_unsettled",
        "requires_action",
        "ceiling",
        "recovery_cancelled",
        "recovery_failed",
        "reducer_bug",
        "admission_balance_depleted",
        "admission_cap_exceeded",
        "admission_channel_budget_exceeded",
        "admission_channel_protected",
        "admission_agent_pinned_elsewhere",
        "admission_channel_isolated",
        "admission_denied",
        "admission_concurrency_shed",
        "missing_config",
        "resolver_miss",
        "session_preparation_failed",
        "session_busy",
        "session_agent_mismatch",
        "unknown",
    }

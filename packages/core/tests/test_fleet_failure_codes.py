"""A worker's failure code tells a refusal from a bug (RESCORE-2 J4)."""

import asyncio
import uuid

import pytest
from daimon.core.fleet import FleetConflict, worker_error_text
from daimon.core.fleet_orchestration import FleetRefused, worker_failure_code
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.resources import AgentPatch
from mux.errors import ProviderError, UnsupportedCapability
from pydantic import ValidationError


def validation_error() -> ValidationError:
    # The real J4 failure: an AgentSpec's extensions list validated as an AgentPatch.
    try:
        AgentPatch.model_validate({"extensions": [{"namespace": "x", "version": 1, "value": {}}]})
    except ValidationError as error:
        return error
    raise AssertionError("expected a ValidationError")


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
@pytest.mark.parametrize(
    "error",
    [
        AdmissionDenied(reason="backend_unsupported"),
        WorkerSpecRefused("backend", "not_subset"),
        FleetRefused("run_limit_reached"),
        FleetConflict("claim changed", reason="claim_changed"),
        ProviderError("invalid_request", retryable=False),
    ],
)
def test_refusals_keep_their_phase_code(error, phase):
    assert worker_failure_code(error, phase) == phase


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
@pytest.mark.parametrize(
    "error", [validation_error(), TypeError("bad"), KeyError("k"), AttributeError("a")]
)
def test_any_other_exception_is_a_setup_error_on_our_side(error, phase):
    assert worker_failure_code(error, phase) == "setup_error"


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
@pytest.mark.parametrize("busy", ["policy", "fence"])
def test_a_busy_policy_fence_reads_as_try_again_without_a_reference(phase, busy):
    # J10 6930b189: a worker that outwaits a policy writer was "a setup error".
    from daimon.core.session_fence_retry import FenceUnavailable
    from daimon.core.stores.access_policy import PolicyBusyError

    error = PolicyBusyError() if busy == "policy" else FenceUnavailable(0.0)
    code = worker_failure_code(error, phase)
    assert code == "preparation_deadline_exceeded"
    assert worker_error_text(code, ref="6930b189") == ("it took too long to start; try again")


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
def test_a_raw_provider_or_transport_error_is_the_providers_not_ours(phase):
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.invalid/v1")
    for error in (
        httpx.ConnectError("down", request=request),
        httpx.ReadTimeout("slow", request=request),
        ConnectionResetError(),
        anthropic.APIConnectionError(request=request),
    ):
        assert worker_failure_code(error, phase) == "provider_error"
    assert "didn't respond" in (worker_error_text("provider_error") or "")


def test_the_card_never_calls_a_setup_error_a_model_limit():
    text = worker_error_text("setup_error", ref="8d427e0b")
    assert text == "a setup error on our side; give an admin the reference (ref 8d427e0b)"
    assert "model" not in text
    assert worker_error_text("preparation_refused", ref="8d427e0b") == (
        "Daimon refused to set it up; try again, or give an admin the reference (ref 8d427e0b)"
    )


def _runtime_selection_refusal() -> AdmissionDenied:
    """A real refusal from runtime row selection (no admitted revision)."""
    from types import SimpleNamespace

    from daimon.core.turn.runtime_manifest import select_runtime_manifest

    request = SimpleNamespace(admission=SimpleNamespace(backend_revision=None))
    try:
        select_runtime_manifest(request, object)  # type: ignore[arg-type]
    except AdmissionDenied as error:
        return error
    raise AssertionError("expected a refusal")


def test_a_backend_refusal_records_the_code_site_that_raised_it():
    """SMOKE-1 B: 12+ raise sites, and the log could not say which one refused."""
    from daimon.core.channel_backend import backend_refusal_causes

    refused = _runtime_selection_refusal()
    assert refused.site is not None
    assert refused.site.startswith("turn.runtime_manifest.select_runtime_manifest:")
    (cause,) = backend_refusal_causes(refused)
    assert cause == {
        "type": "AdmissionDenied",
        "reason": "backend_unsupported",
        "site": refused.site,
    }
    # Other refusals carry no site; outside daimon code there is none either.
    assert AdmissionDenied(reason="channel_budget_exceeded").site is None
    assert AdmissionDenied(reason="backend_unsupported").site is None


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
def test_a_missing_worker_runtime_or_provider_key_reads_as_that(phase):
    missing = _runtime_selection_refusal()
    assert worker_failure_code(missing, phase) == "worker_runtime_missing"
    assert worker_failure_code(FleetConflict("x", reason="claim_changed"), phase) == phase
    unconfigured = AdmissionDenied(
        reason="backend_unsupported", cause="provider_not_configured", provider="openai"
    )
    assert worker_failure_code(unconfigured, phase) == "provider_not_configured"
    assert worker_error_text("worker_runtime_missing", ref="8d427e0b") == (
        "this server isn't set up to run that model's workers yet; ask for another model, "
        "or give an admin the reference (ref 8d427e0b)"
    )
    assert worker_error_text("provider_not_configured") == (
        "that model provider isn't set up on this server yet; an admin can add its key, "
        "or ask for another model"
    )
    # Any other backend refusal keeps its phase.
    other = AdmissionDenied(reason="backend_unsupported")
    assert worker_failure_code(other, phase) == phase


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
def test_a_suppressed_context_never_names_the_cause(phase):
    """#1380 review: `raise X from None` replaced the refusal; its context is not the cause."""
    missing = _runtime_selection_refusal()
    try:
        try:
            raise missing
        except AdmissionDenied:
            raise AdmissionDenied(reason="backend_unsupported") from None
    except AdmissionDenied as replaced:
        assert replaced.__context__ is missing and replaced.__suppress_context__
        assert worker_failure_code(replaced, phase) == phase
    # An explicit cause and an unsuppressed context still count.
    explicit = AdmissionDenied(reason="backend_unsupported")
    explicit.__cause__ = missing
    assert worker_failure_code(explicit, phase) == "worker_runtime_missing"
    try:
        try:
            raise missing
        except AdmissionDenied:
            raise AdmissionDenied(reason="backend_unsupported")  # noqa: B904
    except AdmissionDenied as implicit:
        assert worker_failure_code(implicit, phase) == "worker_runtime_missing"


@pytest.mark.parametrize(
    ("phase", "backend", "code"),
    [
        ("preparation_refused", None, "preparation_deadline_exceeded"),
        ("preparation_refused", "gemini", "preparation_deadline_exceeded"),
        ("dispatch_refused", None, "timed_out"),
        ("dispatch_refused", "openai", "timed_out"),
        ("dispatch_refused", "anthropic", "timed_out"),
        # Gemini can accept a turn and never start it: at the deadline that is
        # indistinguishable from slow, so the words say both.
        ("dispatch_refused", "gemini", "provider_busy"),
    ],
)
def test_a_timeout_reads_as_running_out_of_time_or_the_provider_not_answering(phase, backend, code):
    assert worker_failure_code(TimeoutError(), phase, backend=backend) == code


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
def test_a_cancel_from_outside_is_never_a_refusal(phase):
    assert worker_failure_code(asyncio.CancelledError(), phase) == "watch_ended"


@pytest.mark.parametrize(
    ("backend", "code"), [("gemini", "provider_busy"), ("openai", "timed_out"), (None, "timed_out")]
)
def test_the_turn_ceiling_run_worker_raises_reads_as_a_timeout(backend, code):
    """run_worker re-raises its TimeoutError as ceiling_error(); that is a timeout too."""
    from daimon.core.turn.ceiling import ceiling_error

    ceiling = ceiling_error()
    assert worker_failure_code(ceiling, "dispatch_refused", backend=backend) == code
    try:
        try:
            raise TimeoutError
        except TimeoutError as error:
            raise ceiling_error() from error
    except Exception as raised:
        assert worker_failure_code(raised, "dispatch_refused", backend=backend) == code
    assert worker_failure_code(ceiling, "preparation_refused") == "preparation_deadline_exceeded"


def test_provider_busy_never_claims_it_was_busy():
    text = worker_error_text("provider_busy") or ""
    assert text.startswith("the model provider didn't answer in time (it may be busy)")
    assert text.endswith("try again in a minute")


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
@pytest.mark.parametrize(
    ("native_code", "code", "words"),
    [
        ("quota_zero", "provider_quota_zero", "Google plan doesn't include that model"),
        ("quota_exhausted", "provider_rate_limited", "Google is rate-limiting that model"),
    ],
)
def test_a_provider_quota_refusal_names_the_plan_or_the_rate_limit(phase, native_code, code, words):
    quota = ProviderError(
        "rate_limited", retryable=True, native_code=native_code, native_param="gemini-4-argon"
    )
    assert worker_failure_code(quota, phase, backend="gemini") == code
    wrapped = RuntimeError("worker failed")
    wrapped.__cause__ = quota
    assert worker_failure_code(wrapped, phase) == code
    assert words in (worker_error_text(code) or "")
    # Any other rate limit keeps its phase (a refusal), as before.
    other = ProviderError("rate_limited", retryable=True, native_code="other")
    assert worker_failure_code(other, phase) == phase


def test_the_hold_cap_line_uses_the_root_turns_words():
    assert worker_error_text("provider_hold_cap_reached") == (
        "too many replies were already running on that model for this account; "
        "try again when one finishes"
    )


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
@pytest.mark.parametrize(
    ("refused", "code"),
    [
        (AdmissionDenied(reason="balance_depleted"), "balance_depleted"),
        (AdmissionDenied(reason="cap_exceeded"), "cap_exceeded"),
        (AdmissionDenied(reason="channel_budget_exceeded"), "channel_budget_exceeded"),
        (AdmissionDenied(reason="provider_hold_cap_reached"), "provider_hold_cap_reached"),
        (AdmissionDenied(reason="provider_setup_pending"), "provider_setup_pending"),
        (
            AdmissionDenied(reason="backend_unsupported", cause="provider_not_allowed"),
            "provider_not_allowed",
        ),
        (
            AdmissionDenied(reason="backend_unsupported", cause="channel_pin"),
            "channel_backend_pinned",
        ),
        (
            AdmissionDenied(reason="backend_unsupported", cause="agent_backend"),
            "worker_runtime_missing",
        ),
    ],
)
def test_an_admission_refusal_records_its_own_cause(refused, code, phase):
    assert worker_failure_code(refused, phase) == code
    assert worker_error_text(code) is not None


def test_every_failure_reason_names_a_cause_and_a_fix():
    """SMOKE-1: 'it couldn't start on that model here' named neither."""
    from typing import get_args

    from daimon.core.fleet import WORKER_ERROR_TEXT
    from daimon.core.fleet_orchestration import WorkerFailureCode

    assert set(get_args(WorkerFailureCode)) <= set(WORKER_ERROR_TEXT)
    # The budget watch's stop reasons, when they end a worker, read in words too.
    watch_reasons = {
        "worker_stop_requested",
        "funding_unavailable",
        "pricing_unavailable",
        "terminal",
        "accepted_attempt_inventory_unavailable",
    }
    assert watch_reasons <= set(WORKER_ERROR_TEXT)
    for code, text in WORKER_ERROR_TEXT.items():
        assert "on that model here" not in text, code
        if code not in ("stop_requested", "worker_stop_requested"):
            assert "; " in text, f"{code} names no fix: {text}"


def test_timeouts_and_quotas_follow_the_traceback_chain():
    """The same rule as a refusal's cause: an explicit cause, else an unsuppressed
    context; a context `raise ... from ...` replaced is not the cause."""
    try:
        try:
            raise TimeoutError
        except TimeoutError:
            raise RuntimeError("cleanup failed")  # noqa: B904 - an implicit context
    except RuntimeError as implicit:
        assert worker_failure_code(implicit, "dispatch_refused") == "timed_out"
    try:
        try:
            raise TimeoutError
        except TimeoutError:
            raise FleetConflict("x", reason="claim_changed") from None
    except FleetConflict as replaced:
        assert worker_failure_code(replaced, "dispatch_refused") == "dispatch_refused"
    quota = ProviderError("rate_limited", retryable=True, native_code="quota_zero")
    explicit = FleetConflict("x", reason="result_not_successful")
    explicit.__cause__ = quota
    assert worker_failure_code(explicit, "dispatch_refused") == "provider_quota_zero"


async def test_the_budget_watch_stamps_its_cancel_reason():
    from daimon.core.channel_budget import WorkerCancel, cancel_workers
    from daimon.core.fleet import WORKER_ERROR_TEXT
    from daimon.core.fleet_orchestration import (
        _CANCEL_CODES,  # pyright: ignore[reportPrivateUsage]
    )

    first, plain = WorkerCancel(), asyncio.Event()
    cancel_workers({uuid.uuid4(): first, uuid.uuid4(): plain}, "shutdown", run_id=None)
    assert first.is_set() and plain.is_set() and first.reason == "shutdown"
    # The first reason wins: an event already set isn't restamped.
    cancel_workers({uuid.uuid4(): first}, "budget", run_id=None)
    assert first.reason == "shutdown"
    assert set(_CANCEL_CODES.values()) <= set(WORKER_ERROR_TEXT)
    assert WORKER_ERROR_TEXT[_CANCEL_CODES["shutdown"]].startswith("Daimon restarted")


@pytest.mark.parametrize("phase", ["preparation_refused", "dispatch_refused"])
def test_a_worker_whose_model_cant_run_its_setup_says_so_plainly(phase):
    # Carlos's Opus worker f95639c4: "Daimon refused to start it" hid what went wrong.
    error = UnsupportedCapability(("session_resource",), "anthropic.managed_agents")
    code = worker_failure_code(error, phase)
    assert code == "capability_unsupported"
    text = worker_error_text(code, ref="f95639c4")
    assert text is not None and text.startswith("its model can't run here")
    assert text.endswith("(ref f95639c4)")


def test_refusal_causes_name_our_capabilities_and_redact_user_names():
    from daimon.core.channel_backend import backend_refusal_causes

    error = UnsupportedCapability(
        ("session_resource", "environment_sources", "mcp_tool:acme:secret_tool", "Bad Name"),
        "anthropic.managed_agents",
    )
    (cause,) = backend_refusal_causes(error)
    assert cause["missing"] == ["environment_sources", "redacted", "session_resource"]

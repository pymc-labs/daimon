"""A worker's failure code tells a refusal from a bug (RESCORE-2 J4)."""

import asyncio

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
        UnsupportedCapability(("x",), "anthropic.managed_agents"),
        ProviderError("invalid_request", retryable=False),
        TimeoutError(),
        asyncio.CancelledError(),
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
    assert text == "a setup error on our side (ref 8d427e0b)"
    assert "model" not in text
    assert worker_error_text("preparation_refused", ref="8d427e0b") == (
        "it couldn't be set up on that model here"
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
    assert worker_error_text("worker_runtime_missing") == (
        "this server isn't set up to run that model's workers yet"
    )
    assert worker_error_text("provider_not_configured") == (
        "that model provider isn't set up on this server yet"
    )
    # Any other backend refusal keeps its phase.
    other = AdmissionDenied(reason="backend_unsupported")
    assert worker_failure_code(other, phase) == phase

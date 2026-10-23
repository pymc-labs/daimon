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


def test_the_card_never_calls_a_setup_error_a_model_limit():
    text = worker_error_text("setup_error", ref="8d427e0b")
    assert text == "a setup error on our side (ref 8d427e0b)"
    assert "model" not in text
    assert worker_error_text("preparation_refused", ref="8d427e0b") == (
        "it couldn't be set up on that model here"
    )

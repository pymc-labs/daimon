"""Safe fleet facts preserve outcomes without persisting private model inputs."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.fleet import FleetWorker, result_digest
from daimon.testing.outcome_oracle import (
    FleetWorkerCapture,
    FleetWorkerFacts,
    RunEvidence,
    evaluate,
)
from mux.contracts.events import Event
from pydantic import ValidationError

from .test_outcome_oracle import fixture_finding_judge, fleet_recording, replace_capture


def original_worker(contract=None, status="succeeded"):
    source, _ = fleet_recording()
    facts = source.fleet.workers[0].worker
    return (
        FleetWorker(
            **{
                name: getattr(facts, name)
                for name in FleetWorkerFacts.__dataclass_fields__
                if name not in {"review", "review_complete"}
            },
            spec={"private_config": {"token": "never persist this value"}},
            result_contract=contract
            if contract is not None
            else {"format": "text", "review_required": True},
            budget_usd=Decimal(".1"),
            reserved_usd=Decimal(".1"),
            deadline_at=datetime(2026, 10, 13, tzinfo=UTC),
            max_turns=1,
        )
        if status == "succeeded"
        else replace(original_worker(contract), status=status)
    )


def test_genuine_worker_projects_without_private_maps_and_preserves_pointer():
    source, _ = fleet_recording()
    original = original_worker()
    capture = FleetWorkerCapture.model_validate(
        source.fleet.workers[0].model_dump() | {"worker": original}
    )
    body = capture.model_dump_json()
    restored = FleetWorkerCapture.model_validate_json(body)
    assert (
        "never persist" not in body and '"spec":' not in body and '"result_contract":' not in body
    )
    assert restored.worker == FleetWorkerFacts.from_worker(original)
    assert restored.worker.result == original.result
    assert restored.worker.spec_digest == original.spec_digest
    assert restored.worker.review_complete
    assert restored.result == Event.model_validate_json(restored.result.model_dump_json())
    native = restored.result.native.model_copy(update={"record": "opaque"})
    assert restored.result != restored.result.model_copy(update={"native": native})
    assert "record" not in json.loads(body)["result"]["native"]
    assert "raw_ref" not in json.loads(body)["result"]["native"]


@pytest.mark.parametrize("field", ["spec", "result_contract"])
def test_persisted_worker_refuses_arbitrary_maps(field):
    source, _ = fleet_recording()
    raw = json.loads(source.fleet.workers[0].model_dump_json())
    raw["worker"][field] = {"secret": "must refuse"}
    with pytest.raises(ValidationError):
        FleetWorkerCapture.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("damage", ["record", "raw_ref", "payload", "image", "native", "tool"])
def test_result_capture_refuses_opaque_or_arbitrary_payloads(damage):
    source, _ = fleet_recording()
    raw = json.loads(source.fleet.workers[0].model_dump_json())
    event = raw["result"]
    if damage in {"record", "raw_ref"}:
        event["native"][damage] = {"headers": "secret"} if damage == "record" else "private/raw"
    elif damage == "payload":
        event["payload"]["input"] = {"credential": "secret"}
    elif damage == "image":
        event["payload"]["content"] = [
            {"type": "image", "media_type": "image/png", "data_base64": "AQ=="}
        ]
    elif damage == "native":
        event["payload"]["content"] = [
            {"type": "native", "namespace": "opaque", "version": 1, "payload": {"secret": "value"}}
        ]
    else:
        event["type"] = "agent.tool_use"
        event["payload"] = {
            "call_id": "call",
            "tool_name": "tool",
            "input": {"secret": "value"},
            "executor": "agent",
        }
    with pytest.raises(ValidationError):
        FleetWorkerCapture.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("optional", [{}, {"complete": False}, {"revision": 2, "phase": None}])
def test_safe_result_keeps_original_payload_and_digest_without_filling_defaults(optional):
    source, _ = fleet_recording()
    before = source.fleet.workers[0]
    payload = {
        "item_id": "result",
        "content": [{"type": "text", "text": "unchanged finding"}],
        **optional,
    }
    event = Event.model_validate(before.result.model_dump() | {"payload": payload})
    capture = FleetWorkerCapture.model_validate(
        before.model_dump() | {"worker": before.worker, "result": event}
    )
    restored = FleetWorkerCapture.model_validate_json(capture.model_dump_json())
    assert dict(restored.result.payload) == payload
    assert result_digest(dict(restored.result.payload)) == result_digest(payload)


@pytest.mark.parametrize("status,expected", [("succeeded", "PENDING"), ("failed", "FAIL")])
def test_unsupported_contract_is_pending_but_does_not_erase_known_failure(status, expected):
    source, assertion = fleet_recording()
    original = original_worker(
        {"format": "text", "review_required": True, "private_map": {"secret": "hidden"}}, status
    )
    facts = FleetWorkerFacts.from_worker(original)
    recording = replace_capture(source, worker=facts)
    restored = RunEvidence.model_validate_json(recording.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == expected
    assert any(c.code == "REVIEW_CONTRACT_UNBOUND" for c in report.checks)
    assert "hidden" not in restored.model_dump_json()
    assert not restored.fleet.workers[0].worker.review_complete


def test_known_missing_review_requirement_survives_unsupported_contract():
    source, assertion = fleet_recording()
    facts = FleetWorkerFacts.from_worker(
        original_worker({"review_required": False, "unknown": "not exported"})
    )
    recording = replace_capture(source, worker=facts)
    restored = RunEvidence.model_validate_json(recording.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert any(c.code == "REVIEW_NOT_REQUIRED" for c in report.checks)


def test_safe_json_still_checks_original_result_binding_and_early_review():
    source, assertion = fleet_recording()
    source = replace_capture(
        source, worker=replace(source.fleet.workers[0].worker, binding_id="foreign")
    )
    source = replace_capture(source, 3, native_started_s=12.0)
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert {"INVALID_RESULT_BINDING", "REVIEW_STARTED_BEFORE_READY_ROOT"} <= {
        c.code for c in report.checks
    }


@pytest.mark.parametrize("invalid", [{"format": "json"}, {"reviewed_digest": 7}])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("missing_reviewer", [False, True])
def test_invalid_review_sibling_preserves_valid_requirement(invalid, required, missing_reviewer):
    source, assertion = fleet_recording()
    facts = FleetWorkerFacts.from_worker(original_worker({"review_required": required, **invalid}))
    recording = replace_capture(source, worker=facts)
    if missing_reviewer:
        recording = recording.model_copy(
            update={
                "fleet": recording.fleet.model_copy(
                    update={
                        "workers": recording.fleet.workers[:3],
                        "workers_capture_complete": False,
                        "wakes": recording.fleet.wakes[:1],
                        "wakes_capture_complete": False,
                    }
                )
            }
        )
    restored = RunEvidence.model_validate_json(recording.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert restored.fleet.workers[0].worker.review.review_required is required
    assert not restored.fleet.workers[0].worker.review_complete
    assert any(c.code == "REVIEW_CONTRACT_UNBOUND" for c in report.checks)
    if required:
        assert report.status == "PENDING"
    else:
        assert report.status == "FAIL"
        assert any(c.code == "REVIEW_NOT_REQUIRED" for c in report.checks)

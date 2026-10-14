"""Original four-worker review evidence survives the existing credential audit."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from daimon.core.fleet import FleetWorker
from daimon.testing import fleet_export as export
from daimon.testing.outcome_oracle import FleetExpectation, FleetWorkerFacts, RunEvidence, evaluate
from mux.contracts.events import Event
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProjectionSnapshot

from packages.testing.tests.test_fleet_export import FakeReader, binding
from packages.testing.tests.test_outcome_oracle import fleet_recording


async def four_worker_reader(failed=False):
    source, assertion = fleet_recording()
    run = source.fleet.run
    workers, bindings, events = [], [], {run.orchestrator_session_id: []}
    bindings.append(
        binding(run.orchestrator_binding_id, export.root_slot(run), run.orchestrator_session_id)
    )
    for c in source.fleet.workers:
        f = c.worker
        w = FleetWorker(
            **{
                n: getattr(f, n)
                for n in FleetWorkerFacts.__dataclass_fields__
                if n not in {"review", "review_complete"}
            },
            spec={"model": f.model},
            result_contract=f.result_contract,
            budget_usd=Decimal(".1"),
            reserved_usd=Decimal(0),
            deadline_at=run.deadline_at,
            max_turns=1,
        )
        if failed and w.ordinal == 1:
            w = replace(w, status="failed", error_code="preparation_refused")
        workers.append(w)
        b = c.binding.model_copy(update={"legacy_account_id": str(run.orchestrator_account_id)})
        bindings.append(b)
        event = Event.model_validate_json(c.result.model_dump_json())
        first = event.model_copy(update={"sequence": 0, "id": "preceding-message"})
        events[event.session_id] = [first, event]
    reader = FakeReader(run, tuple(workers), bindings, events)

    async def projection(session_id):
        b = next(b for b in bindings if b.native_refs["session"] == session_id)
        return ProjectionSnapshot(
            session=ResourceRef(
                provider=b.provider,
                account_scope_id="synthetic-account",
                kind="session",
                id=session_id,
            ),
            cursor="frozen",
            state="idle",
            taken_at=run.deadline_at,
        )

    reader.projection = projection
    return reader, assertion


async def four_workers(failed=False):
    reader, assertion = await four_worker_reader(failed)
    value = await export.capture_fleet(
        reader,
        tenant_id=reader.run.tenant_id,
        run_id=reader.run.id,
        attempt_id=assertion["attempt_id"],
        source_sha="a" * 40,
    )
    return value, assertion, reader


@pytest.mark.parametrize("failed,expected", [(False, "PENDING"), (True, "FAIL")])
async def test_verified_export_is_directly_consumed_by_the_existing_scorer(
    tmp_path, failed, expected
):
    value, assertion, reader = await four_workers(failed)
    export.publish_export(value, tmp_path / "bundle")
    actual = export.verify_export(tmp_path / "bundle")
    manifest = export.ExportManifest.model_validate_json(
        (tmp_path / "bundle" / "manifest.json").read_bytes()
    )
    assert manifest.status == "PENDING" and manifest.scenario_credit == 0
    for original, restored in zip(value.fleet.workers, actual.fleet.workers, strict=True):
        assert original.result.payload == restored.result.payload
        assert original.worker.result == restored.worker.result
    root = next(j for j in actual.journals if j.worker_id is None)
    recording = RunEvidence.model_validate_json(
        json.dumps(
            {
                "scenario_id": "synthetic-I7-export-replay",
                "backend": root.binding.provider,
                "turns": [],
                "fleet": actual.fleet.model_dump(mode="json"),
            }
        )
    )
    report = evaluate(
        recording, [FleetExpectation.model_validate(assertion).model_dump(mode="json")]
    )
    assert report.status == expected
    if failed:
        assert any(c.code == "WORKER_FAILED" for c in report.checks)
    assert all(c.terminal is None and c.native_started_s is None for c in actual.fleet.workers)
    assert not any(
        call[0] == "events" and call[1] == actual.fleet.run.orchestrator_session_id
        for call in reader.calls
    )


def replace_review(reader, text, *, split=None):
    from daimon.core.fleet import ResultPointer, result_digest

    workers = list(reader.workers)
    worker = workers[3]
    session = worker.result.session_id
    original = reader.events[session][-1]
    chunks = [text] if split is None else [text[:split], text[split:]]
    result = original.model_copy(
        update={
            "payload": {
                **dict(original.payload),
                "content": [{"type": "text", "text": chunk} for chunk in chunks],
            }
        }
    )
    workers[3] = replace(
        worker,
        result=ResultPointer(session, result.sequence, result_digest(dict(result.payload)), text),
    )
    reader.workers = tuple(workers)
    reader.events[session] = [
        result.model_copy(update={"sequence": 0, "id": "preceding-message"}),
        result,
    ]


async def captured(reader, assertion):
    return await export.capture_fleet(
        reader,
        tenant_id=reader.run.tenant_id,
        run_id=reader.run.id,
        attempt_id=assertion["attempt_id"],
        source_sha="a" * 40,
    )


@pytest.mark.parametrize("split", [None, 48, 56])
async def test_pointer_bound_review_digest_across_text_parts(tmp_path, split):
    reader, assertion = await four_worker_reader()
    digest = reader.workers[0].result.digest
    text = json.dumps({"reviewed_digest": digest, "verdict": "approved"})
    position = None if split is None else text.index(digest) + split
    replace_review(reader, text, split=position)
    value = await captured(reader, assertion)
    original = value.fleet.workers[3].result.payload
    export.publish_export(value, tmp_path / "bundle")
    restored = export.verify_export(tmp_path / "bundle")
    assert restored.fleet.workers[3].result.payload == original
    assert restored.fleet.workers[3].worker.review.reviewed_digest == digest
    evidence = RunEvidence(
        scenario_id="synthetic-I7", backend="anthropic", turns=(), fleet=restored.fleet
    )
    assert evaluate(evidence, [assertion]).status == "PENDING"


@pytest.mark.parametrize("attack", ["raw", "glued", "base64", "escaped", "split_digest"])
async def test_review_digest_context_never_exempts_credentials(tmp_path, attack):
    import base64

    reader, assertion = await four_worker_reader()
    key = "sk-" + "x" * 40
    verdict = {
        "raw": key,
        "glued": "approved" + key,
        "base64": base64.b64encode(key.encode()).decode(),
        "escaped": key.replace("s", r"\u0073"),
        "split_digest": "sk-",
    }[attack]
    text = json.dumps({"verdict": verdict, "reviewed_digest": reader.workers[0].result.digest})
    replace_review(reader, text)
    value = await captured(reader, assertion)
    with pytest.raises(export.ExportRefused):
        export.publish_export(value, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("attack", ["unbound", "ordinary", "duplicate"])
async def test_review_digest_context_is_not_a_blanket_hex_exemption(tmp_path, attack):
    reader, assertion = await four_worker_reader()
    digest = reader.workers[0].result.digest
    if attack == "ordinary":
        workers = list(reader.workers)
        workers[1] = replace(workers[1], role=digest)
        reader.workers = tuple(workers)
    elif attack == "duplicate":
        replace_review(
            reader,
            '{"verdict":"approved","reviewed_digest":"'
            + digest
            + '","reviewed_digest":"'
            + digest
            + '"}',
        )
    else:
        workers = list(reader.workers)
        workers[3] = replace(workers[3], reviewer_of=workers[1].id)
        reader.workers = tuple(workers)
    value = await captured(reader, assertion)
    with pytest.raises(export.ExportRefused):
        export.publish_export(value, tmp_path / "bundle")
    assert not list(tmp_path.iterdir())


async def test_rejected_review_is_evidence_not_a_publication_refusal(tmp_path):
    reader, assertion = await four_worker_reader()
    replace_review(
        reader,
        json.dumps({"verdict": "rejected", "reviewed_digest": reader.workers[0].result.digest}),
    )
    value = await captured(reader, assertion)
    export.publish_export(value, tmp_path / "bundle")
    restored = export.verify_export(tmp_path / "bundle")
    evidence = RunEvidence(
        scenario_id="synthetic-I7", backend="anthropic", turns=(), fleet=restored.fleet
    )
    assert evaluate(evidence, [assertion]).status == "FAIL"


def test_four_worker_controls_and_refusals_under_optimization(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-O", "-m", "pytest", __file__, "-q", "-k", "not under_optimization"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)

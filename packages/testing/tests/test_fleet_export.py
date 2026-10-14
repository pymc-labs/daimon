"""Trust-boundary regressions for offline scoped capture and publication."""

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core.fleet import FleetRun, FleetWorker, ResultPointer, result_digest, worker_slot
from daimon.testing import fleet_export as exporter
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.state.store import binding_slot

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def fixture():
    run = FleetRun(
        id=UUID(int=20),
        tenant_id=UUID(int=21),
        orchestrator_account_id=UUID(int=22),
        orchestrator_session_id="root-session",
        orchestrator_binding_id="root-binding",
        platform="discord",
        channel_id="qa",
        thread_id="thread",
        status="completed",
        total_budget_usd=Decimal(".9"),
        reserved_usd=Decimal(".1"),
        observed_usd=Decimal("0"),
        deadline_at=NOW,
        max_workers=4,
        quorum=None,
        version=2,
    )
    worker = FleetWorker(
        id=UUID(int=23),
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=1,
        role="W1",
        spec={"prompt": "not exported", "credential": "sk-" + "x" * 40},
        spec_digest="a" * 64,
        backend="anthropic",
        profile="anthropic.managed",
        model="claude-haiku-5-5",
        binding_id="worker-binding",
        status="succeeded",
        budget_usd=Decimal(".1"),
        reserved_usd=Decimal(".01"),
        deadline_at=NOW,
        max_turns=1,
        turns_used=1,
        attempts=1,
        reviewer_of=None,
        result_contract={"format": "text", "review_required": True},
        result=None,
        error_code=None,
        version=2,
    )
    result = event("worker-session", 1, "finding")
    result = result.model_copy(update={"id": f"fleet:{worker.id}:result"})
    worker = replace(
        worker,
        result=ResultPointer(
            result.session_id,
            result.sequence,
            result_digest(dict(result.payload)),
            "finding",
        ),
    )
    root = binding(
        run.orchestrator_binding_id, exporter.root_slot(run), run.orchestrator_session_id
    )
    child = binding(worker.binding_id, worker_slot(run, worker), result.session_id)
    return FakeReader(
        run,
        (worker,),
        (root, child),
        {
            root.native_refs["session"]: [],
            child.native_refs["session"]: [event(result.session_id, 0, "hello"), result],
        },
    )


def binding(identity, slot, session):
    return ProviderBinding(
        id=identity,
        thread=slot.thread,
        legacy_account_id=slot.account_id,
        provider="anthropic",
        profile="anthropic.managed",
        native_refs={"session": session},
        generation=0,
        config_revision=1,
    )


def event(session, sequence, text):
    return Event(
        id=f"message-{sequence}",
        session_id=session,
        sequence=sequence,
        type="agent.message",
        turn_id="turn",
        observed_at=NOW,
        authority="record",
        native=NativeProvenance(
            provider="anthropic", api_revision="fixture", record={"secret": "raw"}
        ),
        payload={"item_id": f"message-{sequence}", "content": [{"type": "text", "text": text}]},
    )


class FakeReader:
    def __init__(self, run, workers, bindings, events):
        self.run, self.workers, self.bindings, self.events = run, workers, list(bindings), events
        self.calls = []
        self.changed = None
        self.pages = None

    async def run_status(self, *, tenant_id, run_id):
        self.calls.append(("run", tenant_id, run_id))
        if (tenant_id, run_id) != (self.run.tenant_id, self.run.id):
            raise exporter.ExportRefused("exact run unavailable")
        if self.changed and sum(c[0] == "run" for c in self.calls) > 1:
            return self.changed, self.workers
        return self.run, self.workers

    async def get_binding(self, slot):
        self.calls.append(("binding", slot))
        return next((b for b in self.bindings if binding_slot(b) == slot), None)

    async def projection(self, session_id):
        self.calls.append(("projection", session_id))
        return ProjectionSnapshot(
            session=ResourceRef(
                provider="anthropic",
                account_scope_id="fixture-account",
                kind="session",
                id=session_id,
            ),
            cursor="fixture-head",
            state="idle",
            taken_at=NOW,
        )

    async def read_events(self, session_id, *, after, limit):
        self.calls.append(("events", session_id, after, limit))
        if self.pages is not None and limit != 1:
            return self.pages
        return [e for e in self.events[session_id] if e.sequence > after][:limit]


async def capture(reader, **kwargs):
    return await exporter.capture_fleet(
        reader,
        tenant_id=reader.run.tenant_id,
        run_id=reader.run.id,
        attempt_id="offline-fixture",
        source_sha="a" * 40,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_scoped_results_roundtrip_and_known_failure_are_retained(tmp_path):
    reader = fixture()
    reader.workers = (
        replace(reader.workers[0], status="failed", error_code="preparation_refused"),
    )
    recording = await capture(reader)
    output = tmp_path / "export"
    exporter.publish_export(recording, output)
    restored = exporter.verify_export(output)
    assert restored.fleet.workers[0].worker.status == "failed"
    assert restored.fleet.workers[0].result.payload == reader.events["worker-session"][1].payload
    body = (output / "export.json").read_text()
    assert "not exported" not in body and "sk-" not in body and '"record": {' not in body
    assert restored.fleet.workers[0].terminal is None
    assert {g.owner for g in restored.gaps} >= {"N3", "N4", "N5", "N8"}
    assert (output.stat().st_mode & 0o777) == 0o700
    assert all((f.stat().st_mode & 0o777) == 0o600 for f in output.iterdir())


@pytest.mark.asyncio
async def test_foreign_scope_is_refused_before_binding_or_journal():
    reader = fixture()
    with pytest.raises(exporter.ExportRefused):
        await exporter.capture_fleet(
            reader,
            tenant_id=UUID(int=999),
            run_id=reader.run.id,
            attempt_id="fixture",
            source_sha="a" * 40,
        )
    assert len(reader.calls) == 1 and reader.calls[0][0] == "run"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["tenant", "run", "duplicate", "pointer", "provider", "profile"])
async def test_wrong_worker_identities_never_query_foreign_sessions(damage):
    reader = fixture()
    worker = reader.workers[0]
    if damage in ("tenant", "run"):
        worker = replace(worker, **{f"{damage}_id": UUID(int=999)})
    elif damage == "duplicate":
        reader.workers = (worker, worker)
    elif damage == "pointer":
        worker = replace(worker, result=replace(worker.result, session_id="foreign"))
    elif damage in ("provider", "profile"):
        reader.bindings[1] = reader.bindings[1].model_copy(update={damage: "gemini"})
    if damage != "duplicate":
        reader.workers = (worker,)
    with pytest.raises((exporter.ExportRefused, ValueError)):
        await capture(reader)
    assert not any(c[0] == "events" and c[1] in ("foreign", "worker-session") for c in reader.calls)


@pytest.mark.asyncio
async def test_pages_beyond_one_thousand_preserve_full_result():
    reader = fixture()
    events = [event("worker-session", i, "hello") for i in range(1005)]
    result = reader.events["worker-session"][1].model_copy(update={"sequence": 1005})
    reader.events["worker-session"] = [*events, result]
    reader.workers = (
        replace(reader.workers[0], result=replace(reader.workers[0].result, sequence=1005)),
    )
    recording = await capture(reader)
    assert len(recording.journals[1].records) == 1006
    assert recording.fleet.workers[0].result == exporter.normalized_event(result, project=True)
    assert any(c[0] == "events" and c[2] == 999 for c in reader.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["cap", "sequence", "changed", "digest"])
async def test_incomplete_or_changed_sources_are_refused(damage):
    reader = fixture()
    kwargs = {}
    if damage == "cap":
        kwargs["max_events"] = 1
    elif damage == "sequence":
        reader.events["worker-session"][0] = event("worker-session", 3, "hello")
    elif damage == "changed":
        reader.changed = replace(reader.run, version=3)
    elif damage == "digest":
        reader.workers = (
            replace(reader.workers[0], result=replace(reader.workers[0].result, digest="f" * 64)),
        )
    with pytest.raises((exporter.ExportRefused, exporter.FleetConflict)):
        await capture(reader, **kwargs)


@pytest.mark.asyncio
async def test_tool_mapping_is_omitted_without_touching_result(tmp_path):
    reader = fixture()
    reader.events["worker-session"][0] = Event(
        id="tool",
        session_id="worker-session",
        sequence=0,
        type="agent.tool_use",
        observed_at=NOW,
        authority="record",
        native=NativeProvenance(provider="anthropic", api_revision="fixture"),
        payload={
            "call_id": "call",
            "tool_name": "bash",
            "executor": "agent",
            "input": {"password": "secret"},
        },
    )
    recording = await capture(reader)
    exporter.publish_export(recording, tmp_path / "export")
    body = (tmp_path / "export" / "export.json").read_text()
    assert "input_omitted" in body and "password" not in body and '"secret"' not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("fragments", [("gluedsk-", "x" * 40), ("AI", "za" + "x" * 40)])
async def test_reconstructed_secret_refuses_without_any_artifacts(tmp_path, fragments):
    reader = fixture()
    reader.events["worker-session"][0] = event("worker-session", 0, fragments[0])
    result = event("worker-session", 1, fragments[1]).model_copy(
        update={"id": f"fleet:{reader.workers[0].id}:result"}
    )
    reader.events["worker-session"][1] = result
    reader.workers = (
        replace(
            reader.workers[0],
            result=ResultPointer(
                result.session_id,
                1,
                result_digest(dict(result.payload)),
                fragments[1],
            ),
        ),
    )
    recording = await capture(reader)
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(recording, tmp_path / "export")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_existing_or_symlink_output_is_untouched(tmp_path):
    reader = fixture()
    reader.workers = ()
    recording = await capture(reader)
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(recording, output)
    assert output.is_dir() and not list(output.iterdir())
    link = tmp_path / "link"
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(recording, link / "new")
    assert not list(output.iterdir())


@pytest.mark.asyncio
async def test_atomic_publish_race_keeps_even_empty_existing_directory(tmp_path, monkeypatch):
    reader = fixture()
    reader.workers = ()
    recording = await capture(reader)
    original = exporter._publish_new
    output = tmp_path / "export"

    def raced(source, target):
        target.mkdir()
        original(source, target)

    monkeypatch.setattr(exporter, "_publish_new", raced)
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(recording, output)
    assert output.is_dir() and list(tmp_path.iterdir()) == [output]


@pytest.mark.asyncio
async def test_write_failure_removes_only_owned_temp(tmp_path, monkeypatch):
    reader = fixture()
    reader.workers = ()
    recording = await capture(reader)
    existing = tmp_path / "other"
    existing.write_text("keep")
    monkeypatch.setattr(exporter.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(recording, tmp_path / "export")
    assert list(tmp_path.iterdir()) == [existing] and existing.read_text() == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["byte", "extra", "symlink", "schema", "scope", "mapping"])
async def test_verifier_refuses_mutation_even_with_recomputed_hashes(tmp_path, damage):
    reader = fixture()
    reader.workers = ()
    recording = await capture(reader)
    output = tmp_path / "export"
    exporter.publish_export(recording, output)
    body = output / "export.json"
    if damage == "byte":
        body.write_text("{}")
    elif damage == "extra":
        (output / "other").write_text("extra")
    elif damage == "symlink":
        body.unlink()
        body.symlink_to(output / "manifest.json")
    else:
        value = json.loads(body.read_text())
        if damage == "schema":
            value["arbitrary"] = {"password": "secret"}
        elif damage == "scope":
            value["tenant_id"] = str(UUID(int=999))
        else:
            value["journals"][0]["binding"]["native_refs"]["password"] = "secret"
        rewrite_hashed_bundle(output, value)
    with pytest.raises(exporter.ExportRefused):
        exporter.verify_export(output)


@pytest.mark.asyncio
async def test_optimized_interpreter_preserves_refusals(tmp_path):
    reader = fixture()
    reader.workers = ()
    recording = await capture(reader)
    exporter.publish_export(recording, tmp_path / "export")
    code = """
import sys
from pathlib import Path
from daimon.testing.fleet_export import verify_export, publish_export, ExportRefused
root = Path(sys.argv[1])
value = verify_export(root)
try:
    publish_export(value.model_copy(update={"tenant_id": value.run_id}), root.parent / "bad")
except ExportRefused:
    pass
else:
    raise SystemExit("unsafe scope accepted")
if (root.parent / "bad").exists():
    raise SystemExit("unsafe artifact published")
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code, str(tmp_path / "export")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_reused_root_history_is_not_queried_or_persisted(tmp_path):
    reader = fixture()
    reader.events["root-session"] = [event("root-session", 0, "private prior-run text")]
    value = await capture(reader)
    exporter.publish_export(value, tmp_path / "export")
    assert not any(c[0] == "events" and c[1] == "root-session" for c in reader.calls)
    assert "private prior-run text" not in (tmp_path / "export" / "export.json").read_text()
    assert not value.journals[0].metadata_complete
    assert any(g.code == "ROOT_INPUT_JOIN_UNBOUND" for g in value.gaps)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["id", "session", "account"])
async def test_root_binding_corruption_is_refused_before_journal(damage):
    reader = fixture()
    root = reader.bindings[0]
    changes = (
        {"id": "foreign"}
        if damage == "id"
        else (
            {"native_refs": {"session": "foreign"}}
            if damage == "session"
            else {"legacy_account_id": "foreign"}
        )
    )
    root = root.model_copy(update=changes)

    async def wrong_binding(slot):
        return root

    reader.get_binding = wrong_binding
    with pytest.raises(exporter.ExportRefused):
        await capture(reader)
    assert not any(c[0] == "events" for c in reader.calls)


@pytest.mark.asyncio
async def test_changed_projection_and_missing_result_are_refused():
    reader = fixture()
    original = reader.projection
    calls = 0

    async def changed(session):
        nonlocal calls
        value = await original(session)
        if session == "worker-session":
            calls += 1
        return value.model_copy(update={"cursor": str(calls)})

    reader.projection = changed
    with pytest.raises(exporter.ExportRefused):
        await capture(reader)


@pytest.mark.asyncio
async def test_binary_required_result_refuses_without_publication(tmp_path):
    reader = fixture()
    old = reader.events["worker-session"][1]
    result = old.model_copy(
        update={
            "payload": {
                "item_id": "result",
                "content": [{"type": "image", "media_type": "image/png", "data_base64": "AQ=="}],
            }
        }
    )
    reader.events["worker-session"][1] = result
    reader.workers = (
        replace(
            reader.workers[0],
            result=ResultPointer(
                result.session_id,
                1,
                result_digest(dict(result.payload)),
                json.dumps(dict(result.payload), sort_keys=True),
            ),
        ),
    )
    with pytest.raises(exporter.ExportRefused):
        await capture(reader)
    assert not list(tmp_path.iterdir())


def rewrite_hashed_bundle(output, value):
    """A forger changes canonical data AND all dependent integrity metadata."""
    body = output / "export.json"
    body.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["export_sha256"] = hashlib.sha256(body.read_bytes()).hexdigest()
    manifest["export_bytes"] = body.stat().st_size
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}\n"
            for name in ("export.json", "manifest.json")
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("version", 999), ("error_code", "contradictory_error")])
async def test_paired_worker_mismatches_refuse_publication(tmp_path, field, value):
    recording = await capture(fixture())
    row = recording.worker_rows[0].model_copy(update={field: value})
    inconsistent = recording.model_copy(update={"worker_rows": (row,)})
    with pytest.raises(exporter.ExportRefused):
        exporter.publish_export(inconsistent, tmp_path / "export")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("version", 999), ("error_code", "contradictory_error")])
async def test_paired_worker_mismatches_refuse_rehashed_verification(tmp_path, field, value):
    recording = await capture(fixture())
    output = tmp_path / "export"
    exporter.publish_export(recording, output)
    original = (output / "export.json").read_text()
    document = json.loads(original)
    # The forgery must reach the join check, not fail just because of formatting.
    assert json.dumps(document, ensure_ascii=False, indent=2) + "\n" == original
    document["worker_rows"][0][field] = value
    rewrite_hashed_bundle(output, document)
    with pytest.raises(exporter.ExportRefused):
        exporter.verify_export(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [None, "preparation_refused"])
async def test_paired_worker_same_generation_stays_pending(tmp_path, error_code):
    reader = fixture()
    reader.workers = (
        replace(
            reader.workers[0], error_code=error_code, status="failed" if error_code else "succeeded"
        ),
    )
    recording = await capture(reader)
    output = tmp_path / "export"
    exporter.publish_export(recording, output)
    loaded = exporter.verify_export(output)
    assert loaded.fleet.workers[0].worker.version == loaded.worker_rows[0].version == 2
    assert (
        loaded.fleet.workers[0].worker.error_code == loaded.worker_rows[0].error_code == error_code
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "PENDING" and manifest["scenario_credit"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("version", 999), ("error_code", "contradictory_error")])
async def test_paired_worker_mismatches_refuse_under_optimization(tmp_path, field, value):
    recording = await capture(fixture())
    control = tmp_path / "control"
    forged = tmp_path / "forged"
    exporter.publish_export(recording, control)
    shutil.copytree(control, forged)
    document = json.loads((forged / "export.json").read_text())
    document["worker_rows"][0][field] = value
    rewrite_hashed_bundle(forged, document)
    code = """
import json
import sys
from pathlib import Path
from daimon.testing.fleet_export import verify_export, publish_export, ExportRefused
control, forged = Path(sys.argv[1]), Path(sys.argv[2])
field, replacement = sys.argv[3], json.loads(sys.argv[4])
value = verify_export(control)
manifest = json.loads((control / 'manifest.json').read_text())
if manifest['status'] != 'PENDING' or manifest['scenario_credit'] != 0:
    raise SystemExit('matching control was misclassified')
row = value.worker_rows[0].model_copy(update={field: replacement})
invalid = value.model_copy(update={'worker_rows': (row,)})
try:
    publish_export(invalid, control.parent / 'invalid')
except ExportRefused:
    pass
else:
    raise SystemExit('contradictory worker publication accepted')
if (control.parent / 'invalid').exists():
    raise SystemExit('contradictory worker artifact published')
try:
    verify_export(forged)
except ExportRefused:
    pass
else:
    raise SystemExit('contradictory rehashed worker artifact accepted')
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code, str(control), str(forged), field, json.dumps(value)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

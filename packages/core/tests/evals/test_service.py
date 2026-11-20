"""`EvalService` against Postgres: bundle round trips, imports, scrub-gated shares, blobs."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from daimon.core.config import EvalsSettings
from daimon.core.evals.access import Share, Subject, WorkspaceRole
from daimon.core.evals.bundle import DirSource, ZipSource, sign, to_zip
from daimon.core.evals.format import JsonObject, sha256_hex
from daimon.core.evals.service import (
    EvalForbidden,
    EvalNotFound,
    EvalRefused,
    EvalService,
    ExportRequest,
    Page,
    SuiteCasePick,
    SuiteDraft,
    SuitePick,
)
from daimon.core.evals.validate import ValidationReport, validate_bundle
from daimon.core.stores import evals as store
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import bare_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EXAMPLE = DirSource(Path(__file__).parent / "example_bundle")
SUITE, RUN = "support-basics", "baseline-a-2026-01"
CASES = f"suites/{SUITE}/cases.jsonl"


def _files() -> dict[str, bytes]:
    return {name: EXAMPLE.read(name) for name in EXAMPLE.names()}


def _edited(
    edits: dict[str, Callable[[JsonObject], JsonObject]],
    manifest: Callable[[JsonObject], JsonObject] = lambda m: m,
) -> bytes:
    """The example bundle zipped with documents edited and the manifest rehashed."""
    files = _files()
    for path, edit in edits.items():
        if path.endswith(".json"):
            files[path] = json.dumps(edit(json.loads(files[path]))).encode()
            continue
        lines = [json.dumps(edit(json.loads(line))) for line in files[path].splitlines()]
        files[path] = ("\n".join(lines) + "\n").encode()
    document = json.loads(files["bundle.json"])
    for entry in document["files"]:
        data = files[entry["path"]]
        entry.update(sha256=sha256_hex(data), size=len(data))
    files["bundle.json"] = json.dumps(manifest(document)).encode()
    return to_zip(files)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _documents(report: ValidationReport) -> list[object]:
    return [*((s.document, s.cases) for s in report.suites), *(vars(r) for r in report.runs)]


async def _setup(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    role: WorkspaceRole,
    evals: EvalsSettings | None = None,
) -> tuple[EvalService, Subject, Subject]:
    update: dict[str, object] = {"features": {"evals": "on"}}
    settings = bare_settings().model_copy(update=update | ({"evals": evals} if evals else {}))
    a = await make_tenant(db_session, platform="web", workspace_id="team-a")
    b = await make_tenant(db_session, platform="web", workspace_id="team-b")
    return (
        EvalService(factory, settings),
        Subject(tenant_id=a.id, platform="web", key="alex", role=role),
        Subject(tenant_id=b.id, platform="web", key="sam", role=role),
    )


async def test_bundle_when_exported_and_imported_elsewhere_then_documents_are_equal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, sam = await _setup(db_session, db_session_factory, "member")
    first = await service.import_bundle(alex, _chunks(to_zip(_files())))
    assert [s.id for s in first.suites] == [SUITE], "the suite lands under its id"
    request = ExportRequest(suites=[SuitePick(id=SUITE)], runs=[RUN])
    exported = await (await service.export_bundle(alex, request)).read()
    await service.import_bundle(sam, _chunks(exported))
    again = await (await service.export_bundle(sam, request)).read()
    original, there, back = (
        validate_bundle(s) for s in (EXAMPLE, ZipSource(exported), ZipSource(again))
    )
    assert there.ok and back.ok, f"exports validate: {there.problems} {back.problems}"
    assert _documents(there) == _documents(original), "export keeps every document as imported"
    assert _documents(back) == _documents(there), "import then export changes nothing"


async def test_unscrubbed_bundle_when_imported_then_only_an_accepting_admin_succeeds(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, admin, _ = await _setup(db_session, db_session_factory, "admin")
    member = Subject(tenant_id=admin.tenant_id, platform="web", key="kim", role="member")
    data = _edited({}, lambda m: m | {"scrub": {"status": "unscrubbed", "unchecked_blobs": 0}})
    with pytest.raises(EvalForbidden, match="must accept"):
        await service.import_bundle(admin, _chunks(data))
    with pytest.raises(EvalForbidden, match="owner or an admin"):
        await service.import_bundle(member, _chunks(data), accept_unscrubbed=True)
    report = await service.import_bundle(admin, _chunks(data), accept_unscrubbed=True)
    assert report.runs == [RUN], "an accepting admin imports it"
    sam = Subject(tenant_id=admin.tenant_id, platform="web", key="sam", role="member")
    await service.share(admin, SUITE, [Share(kind="person", id="sam", role="viewer")])
    with pytest.raises(EvalNotFound):
        await service.run(sam, RUN)  # the run came unscrubbed: shares never reach it


async def test_share_when_unscrubbed_case_is_added_then_viewers_lose_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, _ = await _setup(db_session, db_session_factory, "member")
    sam = Subject(tenant_id=alex.tenant_id, platform="web", key="sam", role="member")
    await service.import_bundle(alex, _chunks(to_zip(_files())))
    await service.share(alex, SUITE, [Share(kind="person", id="sam", role="viewer")])
    assert [s.id for s in await service.suites(sam)] == [SUITE], "a scrubbed suite is shared"
    case = json.loads(EXAMPLE.read(CASES))
    del case["provenance"]["scrub"]  # no scrub record reads as unscrubbed
    ref = await service.put_case(alex, SUITE, case)
    with pytest.raises(EvalNotFound):
        await service.case(sam, SUITE, case["id"])
    await service.save_suite(
        alex, SuiteDraft(id=SUITE, name="Support", cases=[SuiteCasePick(id=ref.id)])
    )
    assert await service.suites(sam) == [], "a viewer no longer sees it"
    with pytest.raises(EvalNotFound):
        await service.cases(sam, SUITE)
    assert [c.revision for c in await service.cases(alex, SUITE)] == [2], "the owner still does"


async def test_imported_run_when_unfinished_then_stored_as_cancelled_record(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, _ = await _setup(db_session, db_session_factory, "member")
    approval = {"by": {"kind": "person", "id": "x"}, "at": "2026-01-05T00:00:00Z", "cap_usd": 99}
    run_path, items_path = f"runs/{RUN}/run.json", f"runs/{RUN}/items.jsonl"
    data = _edited(
        {
            run_path: lambda r: (
                r | {"status": "running", "origin": {"kind": "local"}} | {"approval": approval}
            ),
            items_path: lambda i: i | {"status": "pending"},
        }
    )
    await service.import_bundle(alex, _chunks(data))
    run = await service.run(alex, RUN)
    assert (run.status, run.origin.kind, run.approval) == ("cancelled", "imported", None), run
    assert run.origin.deployment == "example", "the origin names the source deployment"
    async with db_session_factory() as session:
        found = await store.run_by_key(session, tenant_id=alex.tenant_id, key=RUN)
        assert found is not None and found.origin == "imported", "never queued as local work"
        items, _, _ = await store.run_parts(session, run_id=found.id)
    assert [i["status"] for i in items] == ["cancelled"], "pending items are cancelled"


async def test_case_when_it_names_a_blob_from_another_suite_then_refused_like_a_missing_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, _ = await _setup(db_session, db_session_factory, "member")
    kim = Subject(tenant_id=alex.tenant_id, platform="web", key="kim", role="member")
    await service.import_bundle(alex, _chunks(to_zip(_files())))
    await service.save_suite(kim, SuiteDraft(id="mine", name="Mine"), new=True)
    case = json.loads(EXAMPLE.read(CASES))
    sha = case["turn"]["messages"][0]["attachments"][0]["blob"]["sha256"]
    for key in (sha, "0" * 64):
        with pytest.raises(EvalNotFound, match="No such file here"):
            await service.blob(kim, key)  # the same answer whether or not the file exists
    _, content = await service.blob(alex, sha)
    assert content == EXAMPLE.read(f"blobs/sha256/{sha}"), "the suite's owner reads it"
    with pytest.raises(EvalRefused, match="the suite does not have") as refused:
        await service.put_case(kim, "mine", case)  # last: a refusal rolls the test back
    assert refused.value.problems == (sha,), "the refused hash is listed"


async def test_trusted_keys_when_set_then_only_bundles_they_signed_import(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    evals = EvalsSettings(trusted_keys=[public.decode()])
    service, admin, _ = await _setup(db_session, db_session_factory, "admin", evals)
    with pytest.raises(EvalForbidden, match="signed by a trusted key"):
        await service.import_bundle(admin, _chunks(to_zip(_files())))
    report = await service.import_bundle(admin, _chunks(_edited({}, lambda m: sign(m, key))))
    assert report.signature == "valid", "a bundle a trusted key signed imports"


async def test_suite_when_imported_from_an_unscrubbed_bundle_then_its_version_is_unscrubbed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, admin, _ = await _setup(db_session, db_session_factory, "admin")
    data = _edited({}, lambda m: m | {"scrub": {"status": "unscrubbed", "unchecked_blobs": 0}})
    await service.import_bundle(admin, _chunks(data), accept_unscrubbed=True)
    async with db_session_factory() as session:
        suite = await store.suite_by_key(session, tenant_id=admin.tenant_id, key=SUITE)
        assert suite is not None
        scrub = await store.version_scrub(session, suite_id=suite.id, version=1)
    assert (scrub, suite.scrub_status) == ("unscrubbed", "unscrubbed"), "the manifest's word holds"
    with pytest.raises(EvalForbidden, match="not scrubbed"):
        await service.share(admin, SUITE, [Share(kind="person", id="sam", role="viewer")])


async def test_run_when_a_reviewer_reads_it_then_arms_and_item_ids_are_neutral(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, _ = await _setup(db_session, db_session_factory, "member")
    sam = Subject(tenant_id=alex.tenant_id, platform="web", key="sam", role="member")
    await service.import_bundle(alex, _chunks(to_zip(_files())))
    await service.share(alex, SUITE, [Share(kind="person", id="sam", role="reviewer")])
    run = await service.run(sam, RUN)
    assert [(a.id, a.label) for a in run.arms] == [("Arm 1", "Arm 1")], "arms are labelled"
    assert run.blind_labels is None and run.name is None, "nothing maps a label back"
    [summary] = await service.items(sam, RUN, Page())
    assert summary.arm_id == "Arm 1" and summary.key == summary.id, "no arm in the item key"
    item = await service.item(sam, summary.id)
    assert (item.id, item.arm_id) == (summary.id, "Arm 1"), "nor in the item id"
    owner = await service.run(alex, RUN)
    assert [a.id for a in owner.arms] == ["baseline-a"], "the owner sees the real run"
    assert [a.id for a in (await service.run(alex, RUN, blind=True)).arms] == ["Arm 1"]

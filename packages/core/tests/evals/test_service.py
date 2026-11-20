"""Bundles through `EvalService` against Postgres: export then import keeps every document."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from daimon.core.evals.access import Subject, WorkspaceRole
from daimon.core.evals.bundle import DirSource, ZipSource, to_zip
from daimon.core.evals.service import EvalForbidden, EvalService, ExportRequest, SuitePick
from daimon.core.evals.validate import ValidationReport, validate_bundle
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import bare_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EXAMPLE = DirSource(Path(__file__).parent / "example_bundle")


def _files() -> dict[str, bytes]:
    return {name: EXAMPLE.read(name) for name in EXAMPLE.names()}


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _documents(report: ValidationReport) -> list[object]:
    return [*((s.document, s.cases) for s in report.suites), *(vars(r) for r in report.runs)]


async def _setup(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession], role: WorkspaceRole
) -> tuple[EvalService, Subject, Subject]:
    settings = bare_settings().model_copy(update={"features": {"evals": "on"}})
    a = await make_tenant(db_session, platform="web", workspace_id="team-a")
    b = await make_tenant(db_session, platform="web", workspace_id="team-b")
    service = EvalService(factory, settings)
    return (
        service,
        Subject(tenant_id=a.id, platform="web", key="web:alex", role=role),
        Subject(tenant_id=b.id, platform="web", key="web:sam", role=role),
    )


async def test_bundle_when_exported_and_imported_elsewhere_then_documents_are_equal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    service, alex, sam = await _setup(db_session, db_session_factory, "member")
    first = await service.import_bundle(alex, _chunks(to_zip(_files())))
    assert [s.id for s in first.suites] == ["support-basics"], "the suite lands under its id"
    request = ExportRequest(suites=[SuitePick(id="support-basics")], runs=["baseline-a-2026-01"])
    ref = await service.export_bundle(alex, request)
    _, exported = await service.blob(alex, ref.sha256)
    await service.import_bundle(sam, _chunks(exported))
    _, again = await service.blob(sam, (await service.export_bundle(sam, request)).sha256)
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
    member = Subject(tenant_id=admin.tenant_id, platform="web", key="web:kim", role="member")
    files = _files()
    manifest = json.loads(files["bundle.json"])
    manifest["scrub"]["status"] = "unscrubbed"
    data = to_zip(files | {"bundle.json": json.dumps(manifest).encode()})
    with pytest.raises(EvalForbidden, match="must accept"):
        await service.import_bundle(admin, _chunks(data))
    with pytest.raises(EvalForbidden, match="owner or an admin"):
        await service.import_bundle(member, _chunks(data), accept_unscrubbed=True)
    report = await service.import_bundle(admin, _chunks(data), accept_unscrubbed=True)
    assert report.runs == ["baseline-a-2026-01"], "an accepting admin imports it"

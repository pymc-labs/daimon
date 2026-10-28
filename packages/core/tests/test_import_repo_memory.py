"""Offline export mapping and stable, content-sensitive manifests."""

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

spec = importlib.util.spec_from_file_location(
    "import_repo_memory", Path(__file__).parents[3] / "scripts/import_repo_memory.py"
)
assert spec is not None and spec.loader is not None
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


def test_maps_and_manifest_preserve_personal_ownership() -> None:
    tenant, account = uuid.uuid4(), uuid.uuid4()
    stamp = datetime.now(UTC)
    source = importer.ExportEntry(
        id="source-id",
        repo_owner="Owner",
        repo_name="Repo",
        name="my-style",
        title="My style",
        body="Be brief",
        kind="directive",
        scope="user",
        created_by_discord_id="person",
        created_at=stamp,
        updated_at=stamp,
    )
    repos = {"owner/repo": importer.RepoTarget(tenant_id=tenant, repo_id=42)}
    entries = importer.prepare_entries([source], {"person": account}, repos)
    assert entries[0].name == "my-style"
    assert entries[0].created_by_account_id == account
    assert entries[0].created_at == stamp
    manifest = importer.manifest_for(entries)
    assert manifest[f"{tenant}/42"]["count"] == 1
    assert manifest == importer.manifest_for(list(reversed(entries)))
    assert manifest != importer.manifest_for([entries[0].model_copy(update={"body": "Changed"})])
    assert "Be brief" not in str(manifest)
    with pytest.raises(ValueError, match="Unmapped"):
        importer.prepare_entries([source], {}, repos)
    with pytest.raises(ValueError, match="Duplicate"):
        importer.prepare_entries([source, source], {"person": account}, repos)
    with pytest.raises(ValueError, match="personal reference"):
        importer.prepare_entries(
            [source.model_copy(update={"kind": "reference"})], {"person": account}, repos
        )
    with pytest.raises(ValueError, match="Ambiguous"):
        importer.prepare_entries(
            [source], {"person": account}, {**repos, "OWNER/REPO": repos["owner/repo"]}
        )


async def test_dry_run_rolls_back_and_apply_replays_exactly(
    db_committing_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from daimon.core.stores.repo_memory import list_entries
    from daimon.testing.factories import make_account, make_tenant

    committing_sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        owner = await make_account(session, tenant=tenant)
    source = importer.ExportEntry(
        id="source",
        repo_owner="owner",
        repo_name="repo",
        name="note",
        title="Note",
        body="Fact",
        created_by_discord_id="owner",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    entries = importer.prepare_entries(
        [source],
        {"owner": owner.id},
        {"owner/repo": importer.RepoTarget(tenant_id=tenant.id, repo_id=17)},
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(importer, "create_async_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(importer, "async_sessionmaker", lambda engine: committing_sessionmaker)
    await importer.run_import(entries, database_url="unused", apply=False)
    async with committing_sessionmaker() as session:
        assert not await list_entries(session, tenant_id=tenant.id, repo_id=17, account_id=owner.id)
    for _ in range(2):
        await importer.run_import(entries, database_url="unused", apply=True)
    async with committing_sessionmaker() as session:
        assert (
            await list_entries(session, tenant_id=tenant.id, repo_id=17, account_id=owner.id)
            == entries
        )

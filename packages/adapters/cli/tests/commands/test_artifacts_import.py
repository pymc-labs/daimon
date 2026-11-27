"""`daimon artifacts import`: a dry run writes nothing, --apply keeps each page."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.cli.commands import artifacts as commands
from daimon.adapters.cli.main import app
from daimon.core.config import ArtifactPagesSettings
from daimon.core.stores.artifact_pages import get_by_public_id
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

GUILD = "777"
LINK = "legacy-link-value-0123456789"


def _bundle(guild: str = GUILD) -> dict[str, object]:
    return {
        "version": 1,
        "guild_id": guild,
        "exported_at": "2026-03-01T00:00:00Z",
        "page": {
            "legacy_url": "https://pages.example.org/a/q3-plan",
            "public_id": "abcdefgh23",
            "slug": "q3-plan",
            "title": "Q3 plan",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "owner": {"discord_id": "1001"},
            "access": "public",
            "link_secret": LINK,
            "current_version": 1,
            "versions": [
                {
                    "version": 1,
                    "html": '<img src="https://tracker.example.com/p.png">',
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
    }


@pytest.fixture
async def operator(db_nullpool_engine, db_clean, monkeypatch):
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id=GUILD)
    settings = SimpleNamespace(
        artifact_pages=ArtifactPagesSettings(),
        crypto=SimpleNamespace(keys=[SecretStr(Fernet.generate_key().decode())]),
        web=SimpleNamespace(artifact_asset_hosts=(), artifact_content_url=None),
    )

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=factory, anthropic=object(), settings=settings)

    async def agent(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(commands, "build_runtime", runtime)
    monkeypatch.setattr(commands, "load_settings", lambda: None)
    monkeypatch.setattr(commands, "find_agent_by_daimon_tag", agent)
    return SimpleNamespace(tenant=tenant, factory=factory, settings=settings)


async def run(operator, folder: Path, *extra: str):
    args = [
        "artifacts",
        "import",
        str(folder),
        "--tenant",
        str(operator.tenant.id),
        "--agent",
        "analyst",
    ]
    return await asyncio.to_thread(CliRunner().invoke, app, [*args, *extra])


async def test_a_dry_run_writes_nothing_and_apply_keeps_the_page(operator, tmp_path):
    (tmp_path / "q3-plan.json").write_text(json.dumps(_bundle()))

    dry = await run(operator, tmp_path)
    assert dry.exit_code == 0, dry.output
    assert "q3-plan" in dry.output and "create" in dry.output
    assert "tracker.example.com" in dry.output and "Dry run" in dry.output
    assert LINK not in dry.output
    async with operator.factory() as db:
        assert await get_by_public_id(db, "abcdefgh23") is None

    done = await run(operator, tmp_path, "--apply")
    assert done.exit_code == 0, done.output
    async with operator.factory() as db:
        page = await get_by_public_id(db, "abcdefgh23")
    assert page is not None and page.link_secret == LINK

    again = await run(operator, tmp_path, "--apply")
    assert "replace" in again.output and again.exit_code == 0, again.output


async def test_files_for_another_server_or_a_bad_file_write_nothing(operator, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_bundle(guild="888")))
    wrong = await run(operator, tmp_path, "--apply")
    assert wrong.exit_code != 0 and "888" in wrong.output

    (tmp_path / "a.json").write_text(json.dumps({**_bundle(), "extra": LINK}))
    bad = await run(operator, tmp_path, "--apply")
    assert bad.exit_code != 0 and LINK not in bad.output, bad.output
    async with operator.factory() as db:
        assert await get_by_public_id(db, "abcdefgh23") is None


async def test_a_server_the_deployment_leaves_out_of_artifacts_is_refused(operator, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_bundle()))
    operator.settings.artifact_pages = ArtifactPagesSettings(workspaces=(uuid.uuid4(),))
    refused = await run(operator, tmp_path)
    assert refused.exit_code != 0 and "DAIMON_ARTIFACT_PAGES__WORKSPACES" in refused.output


async def test_a_provisional_export_can_be_previewed_but_not_applied(operator, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({**_bundle(), "provisional": True}))

    preview = await run(operator, tmp_path)
    assert preview.exit_code == 0, preview.output
    refused = await run(operator, tmp_path, "--apply")
    assert refused.exit_code != 0 and "re-export with --apply first" in refused.output
    async with operator.factory() as db:
        assert await get_by_public_id(db, "abcdefgh23") is None


async def test_each_file_is_read_once_so_what_was_checked_is_imported(
    operator, tmp_path, monkeypatch
):
    (tmp_path / "q3-plan.json").write_text(json.dumps(_bundle()))
    reads: list[str] = []
    read = commands._read
    monkeypatch.setattr(commands, "_read", lambda path: reads.append(path.name) or read(path))

    done = await run(operator, tmp_path, "--apply")

    assert done.exit_code == 0, done.output
    assert reads == ["q3-plan.json"], "a file changed after the checks is never imported"

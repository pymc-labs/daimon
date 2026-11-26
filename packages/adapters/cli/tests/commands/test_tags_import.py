"""Tag opt-out import: one transaction, idempotent, bound to the right server."""

import json
import uuid
from types import SimpleNamespace

import pytest
import typer
from daimon.adapters.cli.commands.tags import TagOptOutManifest, import_opt_outs
from daimon.adapters.cli.main import app
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tag_opt_outs import is_opted_out
from daimon.testing.factories import make_tenant
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner


def _manifest(guild_id: str, ids: list[str]) -> TagOptOutManifest:
    return TagOptOutManifest(version=1, guild_id=guild_id, discord_ids=ids)


async def _opted_out(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, external_id: str
) -> bool:
    async with factory.begin() as db:
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant_id, platform="discord", external_id=external_id
        )
        return await is_opted_out(db, account_id=principal.account_id)


async def test_import_opts_out_new_people_and_counts_repeats(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    manifest = _manifest("777", ["1", "2", "2", "3"])

    assert await import_opt_outs(db_session_factory, tenant_id=tenant.id, manifest=manifest) == (
        3,
        0,
    )
    assert await import_opt_outs(db_session_factory, tenant_id=tenant.id, manifest=manifest) == (
        0,
        3,
    )
    for external_id in ("1", "2", "3"):
        assert await _opted_out(db_session_factory, tenant.id, external_id)
    assert not await _opted_out(db_session_factory, tenant.id, "4")


async def test_import_counts_people_who_already_opted_out_here(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    await import_opt_outs(db_session_factory, tenant_id=tenant.id, manifest=_manifest("777", ["1"]))

    result = await import_opt_outs(
        db_session_factory, tenant_id=tenant.id, manifest=_manifest("777", ["1", "5"])
    )

    assert result == (1, 1)


async def test_import_refuses_other_server_and_leaves_nothing(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    with pytest.raises(typer.BadParameter, match="different Discord server"):
        await import_opt_outs(
            db_session_factory, tenant_id=tenant.id, manifest=_manifest("888", ["1"])
        )
    assert not await _opted_out(db_session_factory, tenant.id, "1")


async def test_import_unknown_tenant(db_session_factory):
    with pytest.raises(typer.BadParameter, match="No such tenant"):
        await import_opt_outs(
            db_session_factory, tenant_id=uuid.uuid4(), manifest=_manifest("777", ["1"])
        )


@pytest.mark.parametrize(
    "body",
    [
        {"version": 2, "guild_id": "1", "discord_ids": []},
        {"version": 1, "guild_id": "1", "discord_ids": ["abc"]},
        {"version": 1, "guild_id": "1", "discord_ids": [], "extra": 1},
        {"version": 1, "guild_id": "x", "discord_ids": []},
    ],
)
def test_manifest_rejects_other_shapes(body):
    with pytest.raises(ValidationError):
        TagOptOutManifest.model_validate(body)


def test_cli_rejects_oversized_file(tmp_path):
    export = tmp_path / "opt-outs.json"
    export.write_text("x" * 1048577)
    result = CliRunner().invoke(
        app, ["tags", "import", str(export), "--tenant", "00000000-0000-0000-0000-000000000000"]
    )
    assert result.exit_code == 2 and "exceeds 1 MiB" in result.output


def test_cli_prints_counts(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    import daimon.adapters.cli.commands.tags as tags

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=object())

    async def fake_import(sessionmaker, *, tenant_id, manifest):
        return 2, 1

    monkeypatch.setattr(tags, "build_runtime", runtime)
    monkeypatch.setattr(tags, "load_settings", lambda: None)
    monkeypatch.setattr(tags, "import_opt_outs", fake_import)
    export = tmp_path / "opt-outs.json"
    export.write_text(json.dumps({"version": 1, "guild_id": "1", "discord_ids": ["2", "3", "4"]}))
    result = CliRunner().invoke(
        app, ["tags", "import", str(export), "--tenant", "00000000-0000-0000-0000-000000000000"]
    )
    assert result.exit_code == 0 and "2 new, 1 already opted out" in result.output

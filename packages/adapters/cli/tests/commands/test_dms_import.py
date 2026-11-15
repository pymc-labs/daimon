"""DM carry-over import: bounded files, one transaction per file, safe to repeat."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from daimon.adapters.cli.commands import dms
from daimon.adapters.cli.commands.dms import import_carry_overs
from daimon.adapters.cli.main import app
from daimon.core.dm_carry_over import CarryOverExport
from daimon.core.stores.direct_messages import COLD_DM_ORIGIN, DirectMessageRow, start_conversation
from daimon.core.stores.dm_carry_overs import take_carry_over
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

TENANT_ARG = "00000000-0000-0000-0000-000000000000"


def body(guild: str = "777", users: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "guild_id": guild,
        "source_name": "Old Bot",
        "exported_at": "2026-03-01T12:00:00Z",
        "users": users
        if users is not None
        else [
            {
                "external_user_id": "1",
                "turns": [
                    {"role": "user", "text": "hello", "at": "2026-03-01T10:00:00Z"},
                    {"role": "assistant", "text": "hi", "at": "2026-03-01T10:01:00Z"},
                ],
            },
            {"external_user_id": "2", "turns": []},
        ],
    }


async def taken(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, user: str):
    async with factory.begin() as db:
        return await take_carry_over(
            db, tenant_id=tenant_id, platform="discord", external_user_id=user
        )


async def test_import_holds_each_conversation_and_skips_empty_ones(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    export = CarryOverExport.model_validate(body())

    counts = await import_carry_overs(db_session_factory, tenant_id=tenant.id, export=export)

    assert (counts["new"], counts["empty"]) == (1, 1)
    held = await taken(db_session_factory, tenant.id, "1")
    assert held is not None and [item["role"] for item in held.history] == ["user", "agent"]
    assert await taken(db_session_factory, tenant.id, "2") is None


async def test_repeat_updates_waiting_ones_and_never_revives_used_ones(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    export = CarryOverExport.model_validate(body())
    await import_carry_overs(db_session_factory, tenant_id=tenant.id, export=export)

    again = await import_carry_overs(db_session_factory, tenant_id=tenant.id, export=export)
    assert again["updated"] == 1 and again["new"] == 0
    assert await taken(db_session_factory, tenant.id, "1") is not None

    after = await import_carry_overs(db_session_factory, tenant_id=tenant.id, export=export)
    assert after["consumed"] == 1
    assert await taken(db_session_factory, tenant.id, "1") is None


async def test_people_who_already_have_a_private_conversation_are_skipped(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
        account = await make_account(db, tenant=tenant)
        await start_conversation(
            db,
            conversation=DirectMessageRow(
                platform="discord",
                route_key="r",
                external_user_id="1",
                tenant_id=tenant.id,
                account_id=account.id,
                workspace_id="777",
                channel_id="r",
                scope_id="dm:x",
                source_url=COLD_DM_ORIGIN,
                context="",
                memory_read_only=False,
                history=[],
                recent_message_ids=[],
                active_until=None,
            ),
            now=datetime.now(UTC),
        )
    export = CarryOverExport.model_validate(body())

    counts = await import_carry_overs(db_session_factory, tenant_id=tenant.id, export=export)

    assert counts["existing"] == 1 and counts["new"] == 0
    assert await taken(db_session_factory, tenant.id, "1") is None


async def test_other_server_or_unknown_tenant_changes_nothing(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id="777")
    with pytest.raises(typer.BadParameter, match="different Discord server"):
        await import_carry_overs(
            db_session_factory,
            tenant_id=tenant.id,
            export=CarryOverExport.model_validate(body(guild="888")),
        )
    with pytest.raises(typer.BadParameter, match="No such tenant"):
        await import_carry_overs(
            db_session_factory,
            tenant_id=uuid.uuid4(),
            export=CarryOverExport.model_validate(body()),
        )
    assert await taken(db_session_factory, tenant.id, "1") is None


def test_cli_rejects_oversized_file(tmp_path: Path):
    big = tmp_path / "big.json"
    big.write_text("x" * 1048577)
    result = CliRunner().invoke(app, ["dms", "import", str(big), "--tenant", TENANT_ARG])
    assert result.exit_code == 2 and "exceeds 1 MiB" in result.output


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "version": 2},
        lambda b: {**b, "extra": 1},
        lambda b: {
            **b,
            "users": [
                {
                    "external_user_id": "1",
                    "turns": [{"role": "bot", "text": "x", "at": "2026-03-01T10:00:00Z"}],
                }
            ],
        },
    ],
)
def test_cli_rejects_a_file_with_the_wrong_shape(tmp_path: Path, mutate):
    path = tmp_path / "dms.json"
    path.write_text(json.dumps(mutate(body())))
    result = CliRunner().invoke(app, ["dms", "import", str(path), "--tenant", TENANT_ARG])
    assert result.exit_code != 0


def test_cli_imports_each_file_and_prints_counts(tmp_path: Path, monkeypatch):
    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=object())

    seen: list[str] = []

    async def fake(sessionmaker, *, tenant_id, export):
        seen.append(export.guild_id)
        from collections import Counter

        return Counter(new=2, existing=1)

    monkeypatch.setattr(dms, "build_runtime", runtime)
    monkeypatch.setattr(dms, "load_settings", lambda: None)
    monkeypatch.setattr(dms, "import_carry_overs", fake)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(body("1")))
    second.write_text(json.dumps(body("2")))

    result = CliRunner().invoke(
        app, ["dms", "import", str(first), str(second), "--tenant", TENANT_ARG]
    )

    assert result.exit_code == 0 and seen == ["1", "2"]
    assert "a.json: 2 new, 0 updated, 0 already used, 1 already have a private conversation" in (
        result.output
    )

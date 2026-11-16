"""Operator import of timers and callback watches: tenant checks, reruns, unpause, renew, rotate."""

import asyncio
import hashlib
import json
import stat
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.adapters.cli.commands import watches as watch_commands
from daimon.adapters.cli.commands.watches import watches_app
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

GUILD = "777"
TOKEN = "legacy-token-" + "ab" * 16


@pytest.fixture
async def operator(db_nullpool_engine, db_clean, monkeypatch):
    """A tenant for server 777 and a CLI runtime on the real test database."""
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.setenv("DAIMON_WATCHES__CALLBACK_BASE_URL", "https://hooks.example.com")
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with factory.begin() as db:
        tenant = await make_tenant(db, platform="discord", workspace_id=GUILD)

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=factory, anthropic=object())

    monkeypatch.setattr(watch_commands, "build_runtime", runtime)
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    return SimpleNamespace(tenant=tenant, factory=factory)


async def run(*args: str):
    """The CLI runs its own event loop, so it goes to a thread."""
    return await asyncio.to_thread(CliRunner().invoke, watches_app, list(args))


def timer(source_id: str, *, place: str = "thread", minutes: int = 90, note: str = "ping"):
    return {
        "source_id": source_id,
        "owner_discord_id": "555",
        "channel_id": "900",
        "thread_id": "901" if place == "thread" else None,
        "place_type": place,
        "fire_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
        "note": note,
    }


def manifest(tmp_path: Path, *, guild: str | None = GUILD, **parts: object) -> Path:
    body: dict[str, object] = {"version": 1, "watches": [], **parts}
    if guild is not None:
        body["guild_id"] = guild
    path = tmp_path / "export.json"
    path.write_text(json.dumps(body))
    return path


async def rows(operator, query: str):
    async with operator.factory() as db:
        return [tuple(r) for r in await db.execute(text(query))]


async def test_file_for_another_server_writes_nothing(operator, tmp_path):
    path = manifest(tmp_path, guild="888", timers=[timer("a")])
    result = await run("import", str(path), "--tenant", str(operator.tenant.id))
    assert result.exit_code == 2 and "The file is for server 888" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers") == []


async def test_file_without_a_server_is_refused(operator, tmp_path):
    path = manifest(tmp_path, guild=None, timers=[timer("a")])
    result = await run("import", str(path), "--tenant", str(operator.tenant.id))
    assert result.exit_code != 0 and "guild_id" in str(result.exception), result.exception


async def test_timer_import_counts_each_kind_and_a_rerun_updates(operator, tmp_path):
    tenant = str(operator.tenant.id)
    first = manifest(
        tmp_path,
        timers=[
            timer("t"),
            timer("dm", place="direct-message"),
            timer("room", place="observer"),
            timer("long", note="n" * 5000),
        ],
    )
    result = await run("import", str(first), "--tenant", tenant)
    assert result.exit_code == 0, result.output
    assert "timers held=3 updated=0 existing=0 past=0 unsupported=1 truncated=1" in result.output
    again = manifest(tmp_path, timers=[timer("t", minutes=200, note="moved")])
    result = await run("import", str(again), "--tenant", tenant)
    assert "updated=1" in result.output and "cancelled" not in result.output, result.output
    stored = await rows(operator, "SELECT source_id, note FROM held_timers ORDER BY source_id")
    assert ("t", "moved") in stored and len(stored) == 3, stored


async def test_full_import_cancels_timers_the_file_no_longer_lists(operator, tmp_path):
    tenant = str(operator.tenant.id)
    await run(
        "import", str(manifest(tmp_path, timers=[timer("a"), timer("b")])), "--tenant", tenant
    )
    result = await run(
        "import", str(manifest(tmp_path, timers=[timer("a")])), "--tenant", tenant, "--full"
    )
    assert result.exit_code == 0 and "cancelled=1" in result.output, result.output
    stored = await rows(operator, "SELECT source_id, outcome FROM held_timers ORDER BY source_id")
    assert stored == [("a", None), ("b", "cancelled")], stored


async def test_unpause_lets_a_paused_timer_be_scheduled(operator, tmp_path):
    tenant = str(operator.tenant.id)
    paused = {**timer("p"), "paused": True}
    await run("import", str(manifest(tmp_path, timers=[paused])), "--tenant", tenant)
    result = await run("unpause", "--tenant", tenant, "--timer-id", "nope")
    assert "timers unpaused=0" in result.output, result.output
    result = await run("unpause", "--tenant", tenant, "--all-timers")
    assert result.exit_code == 0 and "timers unpaused=1" in result.output, result.output
    assert await rows(operator, "SELECT paused FROM held_timers") == [(False,)]
    result = await run("unpause", "--tenant", tenant)
    assert result.exit_code == 2 and "exactly one" in result.output, result.output


def callback_entry():
    return {
        "legacy_id": "11111111-1111-1111-1111-111111111111",
        "source": "callback",
        "instructions": "Report each event.",
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
        "owner_discord_id": "555",
        "channel_id": "900",
        "thread_id": None,
        "place_type": "channel",
    }


async def test_agent_must_belong_to_the_tenant(operator, tmp_path, monkeypatch):
    async def other(*_args, **_kwargs):
        return SimpleNamespace(id="agent-other")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", other)
    path = manifest(tmp_path, watches=[callback_entry()])
    result = await run(
        *("import", str(path), "--tenant", str(operator.tenant.id)),
        *("--agent-id", "agent-1", "--agent-name", "analyst"),
        *("--urls-out", str(tmp_path / "urls.json")),
    )
    assert result.exit_code == 2 and "must name one agent" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM watches") == []


async def test_a_timers_only_import_checks_the_agent_it_is_given(operator, tmp_path, monkeypatch):
    async def other(*_args, **_kwargs):
        return SimpleNamespace(id="agent-other")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", other)
    path = manifest(tmp_path, timers=[timer("t")])
    result = await run(
        *("import", str(path), "--tenant", str(operator.tenant.id)),
        *("--agent-id", "agent-1", "--agent-name", "analyst"),
    )
    assert result.exit_code == 2 and "must name one agent" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers") == []


async def test_renew_and_rotate_link_for_an_imported_callback(operator, tmp_path, monkeypatch):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    tenant = str(operator.tenant.id)
    urls = tmp_path / "urls.json"
    path = manifest(tmp_path, watches=[callback_entry()])
    result = await run(
        *("import", str(path), "--tenant", tenant, "--agent-id", "agent-1"),
        *("--agent-name", "analyst", "--urls-out", str(urls)),
    )
    assert result.exit_code == 0, result.output
    watch_id = (await rows(operator, "SELECT id FROM watches"))[0][0]

    result = await run("renew", str(watch_id), "--tenant", tenant)
    assert result.exit_code == 0 and "expires" in result.output, result.output
    result = await run("renew", str(watch_id), "--tenant", str(uuid.uuid4()))
    assert result.exit_code == 2 and "not found" in result.output, result.output

    printed = await run("rotate-link", str(watch_id), "--tenant", tenant)
    assert printed.exit_code == 0, printed.output
    assert f"https://hooks.example.com/callbacks/watches/{watch_id}/" in printed.output
    written = tmp_path / "rotated.json"
    saved = await run("rotate-link", str(watch_id), "--tenant", tenant, "--urls-out", str(written))
    assert saved.exit_code == 0 and "callbacks/watches" not in saved.output, saved.output
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert str(watch_id) in next(iter(json.loads(written.read_text()).values()))
    live = await rows(operator, "SELECT count(*) FROM watch_capabilities WHERE revoked_at IS NULL")
    assert live == [(1,)], "each rotation leaves exactly one live link"


async def test_unpaused_timer_stays_unpaused_when_the_file_is_imported_again(operator, tmp_path):
    tenant = str(operator.tenant.id)
    path = manifest(tmp_path, timers=[{**timer("p"), "paused": True}])
    await run("import", str(path), "--tenant", tenant)
    await run("unpause", "--tenant", tenant, "--all-timers")

    again = await run("import", str(path), "--tenant", tenant)

    assert again.exit_code == 0 and "existing=1" in again.output, again.output
    assert await rows(operator, "SELECT paused FROM held_timers") == [(False,)]
    moved = manifest(tmp_path, timers=[{**timer("p", minutes=300), "paused": True}])
    await run("import", str(moved), "--tenant", tenant)
    assert await rows(operator, "SELECT paused FROM held_timers") == [(False,)]


async def test_unpaused_watch_stays_active_when_the_file_is_imported_again(
    operator, tmp_path, monkeypatch
):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    tenant = str(operator.tenant.id)
    entry = {**callback_entry(), "paused": True}
    path = manifest(tmp_path, watches=[entry])
    args = ("import", str(path), "--tenant", tenant, "--agent-id", "agent-1")
    args += ("--agent-name", "analyst", "--urls-out", str(tmp_path / "urls.json"))
    assert (await run(*args)).exit_code == 0
    watch_id, owner = (await rows(operator, "SELECT id, owner_account_id FROM watches"))[0]
    confirmed = await run("confirm", str(watch_id), tenant, str(owner), "agent-1", "900", "900")
    assert confirmed.exit_code == 0, confirmed.output
    assert (await run("unpause", "--tenant", tenant, "--watch-id", str(watch_id))).exit_code == 0
    assert await rows(operator, "SELECT status FROM watches") == [("active",)]

    assert (await run(*args)).exit_code == 0

    assert await rows(operator, "SELECT status FROM watches") == [("active",)]


@pytest.mark.parametrize("command", ["confirm-timers", "confirm"])
async def test_confirming_checks_the_agent_belongs_to_the_tenant(
    operator, tmp_path, monkeypatch, command
):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    async def other(*_args, **_kwargs):
        return SimpleNamespace(id="agent-other")

    tenant = str(operator.tenant.id)
    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    urls = tmp_path / "urls.json"
    path = manifest(tmp_path, watches=[callback_entry()], timers=[timer("t")])
    imported = await run(
        *("import", str(path), "--tenant", tenant, "--agent-id", "agent-1"),
        *("--agent-name", "analyst", "--urls-out", str(urls)),
    )
    assert imported.exit_code == 0, imported.output
    watch_id, owner = (await rows(operator, "SELECT id, owner_account_id FROM watches"))[0]
    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", other)
    if command == "confirm-timers":
        args = ("confirm-timers", "--tenant", tenant, "--agent-id", "agent-1")
        args += ("--agent-name", "analyst")
    else:
        args = ("confirm", str(watch_id), tenant, str(owner), "agent-1", "900", "900")

    result = await run(*args)

    assert result.exit_code == 2 and "must name one agent" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers WHERE settled_at IS NOT NULL") == []
    assert await rows(operator, "SELECT status FROM watches") == [("paused",)]


@pytest.mark.parametrize("state", ["archived", "slack"])
async def test_confirm_timers_refuses_an_archived_or_other_platform_tenant(
    operator, tmp_path, monkeypatch, state
):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    tenant = str(operator.tenant.id)
    await run("import", str(manifest(tmp_path, timers=[timer("t")])), "--tenant", tenant)
    change = "archived_at = now()" if state == "archived" else "platform = 'slack'"
    async with operator.factory.begin() as db:
        await db.execute(text(f"UPDATE tenants SET {change} WHERE id = :id"), {"id": tenant})

    result = await run(
        *("confirm-timers", "--tenant", tenant, "--agent-id", "agent-1"),
        *("--agent-name", "analyst"),
    )

    assert result.exit_code == 2 and "is archived" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers WHERE settled_at IS NOT NULL") == []

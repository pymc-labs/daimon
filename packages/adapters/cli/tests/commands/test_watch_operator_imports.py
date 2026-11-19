"""Operator import of watches and timers: tenant checks, reruns, go-live, unpause, renew, rotate."""

import asyncio
import hashlib
import json
import stat
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from daimon.adapters.cli.commands import watches as watch_commands
from daimon.adapters.cli.commands.watches import watches_app
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

GUILD = "777"
TOKEN = "legacy-token-" + "ab" * 16
GO_LIVE = ("go-live", "--tenant")


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
        yield SimpleNamespace(sessionmaker=factory, anthropic=object(), settings=None)

    async def agent(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "build_runtime", runtime)
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", agent)
    return SimpleNamespace(tenant=tenant, factory=factory)


async def run(*args: str):
    """The CLI runs its own event loop, so it goes to a thread. An import names agent-1
    unless the test gives its own agent."""
    if args[0] == "import" and "--agent-id" not in args:
        args = (*args, "--agent-id", "agent-1", "--agent-name", "analyst")
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


def manifest(
    tmp_path: Path, *, guild: str | None = GUILD, name: str = "export.json", **parts: object
) -> Path:
    body: dict[str, object] = {"version": 1, "watches": [], **parts}
    if guild is not None:
        body["guild_id"] = guild
    path = tmp_path / name
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
            timer("call", place="voice"),
            timer("long", note="n" * 5000),
        ],
    )
    result = await run("import", str(first), "--tenant", tenant)
    assert result.exit_code == 0, result.output
    assert "timers held=4 updated=0 existing=0 past=0 unsupported=1 truncated=1" in result.output
    again = manifest(tmp_path, timers=[timer("t", minutes=200, note="moved")])
    result = await run("import", str(again), "--tenant", tenant)
    assert "updated=1" in result.output and "cancelled" not in result.output, result.output
    stored = await rows(operator, "SELECT source_id, note FROM held_timers ORDER BY source_id")
    assert ("t", "moved") in stored and len(stored) == 4, stored


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


async def test_full_import_over_a_split_export_keeps_what_any_file_lists(operator, tmp_path):
    tenant = str(operator.tenant.id)
    first = manifest(tmp_path, name="1.json", timers=[timer("a"), timer("b")])
    later = manifest(tmp_path, name="2.json", timers=[])
    assert (await run("import", str(first), str(later), "--tenant", tenant)).exit_code == 0

    alone = await run("import", str(later), "--tenant", tenant, "--full")

    assert alone.exit_code == 2 and "No file lists timers" in alone.output, alone.output
    cancelled = "SELECT 1 FROM held_timers WHERE outcome = 'cancelled'"
    assert await rows(operator, cancelled) == [], "a later file alone cancels nothing"
    trimmed = manifest(tmp_path, name="1.json", timers=[timer("a")])
    result = await run("import", str(trimmed), str(later), "--tenant", tenant, "--full")
    assert result.exit_code == 0 and "cancelled=1" in result.output, result.output
    stored = await rows(operator, "SELECT source_id, outcome FROM held_timers ORDER BY source_id")
    assert stored == [("a", None), ("b", "cancelled")], stored


async def test_files_for_different_servers_are_refused_together(operator, tmp_path):
    first = manifest(tmp_path, name="1.json", timers=[timer("a")])
    other = manifest(tmp_path, name="2.json", guild="888", timers=[timer("b")])
    result = await run("import", str(first), str(other), "--tenant", str(operator.tenant.id))
    assert result.exit_code == 2 and "different servers" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers") == [], "nothing is written"


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
    watch_id = (await rows(operator, "SELECT id FROM watches"))[0][0]
    live = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")
    assert live.exit_code == 0 and "kept_paused=1" in live.output, live.output
    assert (await run("unpause", "--tenant", tenant, "--watch-id", str(watch_id))).exit_code == 0
    assert await rows(operator, "SELECT status FROM watches") == [("active",)]

    assert (await run(*args)).exit_code == 0

    assert await rows(operator, "SELECT status FROM watches") == [("active",)]


async def test_going_live_checks_the_agent_belongs_to_the_tenant(operator, tmp_path, monkeypatch):
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
    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", other)

    result = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert result.exit_code == 2 and "must name one agent" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers WHERE settled_at IS NOT NULL") == []
    assert await rows(operator, "SELECT status FROM watches") == [("paused",)]


@pytest.mark.parametrize("state", ["archived", "slack"])
async def test_go_live_refuses_an_archived_or_other_platform_tenant(
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

    result = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert result.exit_code == 2 and "is archived" in result.output, result.output
    assert await rows(operator, "SELECT 1 FROM held_timers WHERE settled_at IS NOT NULL") == []


async def bind_owner(operator) -> uuid.UUID:
    """The owner's live thread with agent-1, which a polled watch reports to."""
    async with operator.factory.begin() as db:
        principal = await get_or_create_platform_principal(
            db, tenant_id=operator.tenant.id, platform="discord", external_id="555"
        )
        await create_thread_session(
            db,
            tenant_id=operator.tenant.id,
            platform="discord",
            thread_id="4242",
            channel_id="4241",
            account_id=principal.account_id,
            ma_session_id="session-bound",
            ma_agent_id="agent-1",
        )
    return principal.account_id


def poll_entry(operator, owner: uuid.UUID, legacy_id: str, **parts: object) -> dict[str, object]:
    binding = ContinuationRequest(
        tenant_id=operator.tenant.id,
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        requester_account_id=owner,
        requester_external_user_id="555",
        target_ma_agent_id="agent-1",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    return {
        "legacy_id": legacy_id,
        "os_binding": binding.model_dump(mode="json"),
        "source": "poll",
        "instructions": "Report changes.",
        "tool": "discord_read_channel_since",
        "arguments": {"channel_id": "4343"},
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        **parts,
    }


async def test_go_live_makes_everything_an_import_held_live_once(operator, tmp_path, monkeypatch):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    tenant = str(operator.tenant.id)
    owner = await bind_owner(operator)
    polled = [poll_entry(operator, owner, "poll"), poll_entry(operator, owner, "held", paused=True)]
    path = manifest(
        tmp_path,
        watches=[*polled, callback_entry()],
        timers=[timer("soon"), timer("due"), {**timer("paused"), "paused": True}],
    )
    imported = await run(
        *("import", str(path), "--tenant", tenant, "--agent-id", "agent-1"),
        *("--agent-name", "analyst", "--urls-out", str(tmp_path / "urls.json")),
    )
    assert imported.exit_code == 0, imported.output
    async with operator.factory.begin() as db:
        await db.execute(text("UPDATE held_timers SET fire_at = now() WHERE source_id = 'due'"))

    live = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert live.exit_code == 0, live.output
    assert "watches live=2 kept_paused=1 refused=0" in live.output, live.output
    assert "timers past=1 scheduled=1" in live.output, live.output
    statuses = await rows(operator, "SELECT source, status FROM watches ORDER BY source, status")
    assert statuses == [("callback", "active"), ("poll", "active"), ("poll", "paused")], (
        "the callback and the poll go live; the one paused at the source waits"
    )
    held = await rows(operator, "SELECT source_id FROM held_timers WHERE settled_at IS NULL")
    assert held == [("paused",)], "a timer paused at the source is never scheduled"

    again = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert "watches live=0 kept_paused=0 refused=0" in again.output, again.output
    assert "timers none" in again.output, again.output


async def test_go_live_lists_a_watch_it_cannot_make_live(operator, tmp_path, monkeypatch):
    async def found(*_args, **_kwargs):
        return SimpleNamespace(id="agent-1")

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", found)
    tenant = str(operator.tenant.id)
    unmapped = poll_entry(operator, await bind_owner(operator), "poll", tool="unknown_tool")
    assert (await run("import", str(manifest(tmp_path, watches=[unmapped])))).exit_code == 0

    live = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert live.exit_code == 0 and "refused=1" in live.output, live.output
    assert "not live: Unmapped imported sources" in live.output, live.output
    assert await rows(operator, "SELECT status FROM watches") == [("paused",)]


async def test_timers_need_an_agent_to_be_held_for(operator, tmp_path):
    path = manifest(tmp_path, timers=[timer("t")])
    result = await asyncio.to_thread(
        CliRunner().invoke,
        watches_app,
        ["import", str(path), "--tenant", str(operator.tenant.id)],
    )
    assert result.exit_code == 2 and "--agent-id" in click.unstyle(result.output), result.output
    assert await rows(operator, "SELECT 1 FROM held_timers") == []


async def test_go_live_and_full_import_leave_another_agents_timers_alone(
    operator, tmp_path, monkeypatch
):
    async def agent(*_args, name, **_kwargs):
        return SimpleNamespace(id={"analyst": "agent-1", "writer": "agent-2"}[name])

    monkeypatch.setattr(watch_commands, "find_agent_by_daimon_tag", agent)
    tenant = str(operator.tenant.id)
    other = ("--agent-id", "agent-2", "--agent-name", "writer")
    assert (
        await run("import", str(manifest(tmp_path, timers=[timer("a")])), "--tenant", tenant)
    ).exit_code == 0
    stored = ("import", str(manifest(tmp_path, timers=[timer("b")])), "--tenant", tenant)
    assert (await run(*stored, *other)).exit_code == 0

    full = await run(
        "import", str(manifest(tmp_path, timers=[timer("c")])), "--tenant", tenant, "--full"
    )
    assert "cancelled=1" in full.output, full.output
    assert await rows(operator, "SELECT source_id, outcome FROM held_timers ORDER BY 1") == [
        ("a", "cancelled"),
        ("b", None),
        ("c", None),
    ]

    live = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")
    assert "timers scheduled=1" in live.output, live.output
    assert await rows(operator, "SELECT source_id FROM held_timers WHERE outcome IS NULL") == [
        ("b",)
    ]


async def test_go_live_names_the_timers_an_agent_off_managed_agents_cannot_run(
    operator, tmp_path, monkeypatch
):
    async def off_managed_agents(*_args, **_kwargs):
        return False

    monkeypatch.setattr(watch_commands, "agent_runs_scope_timers", off_managed_agents)
    tenant = str(operator.tenant.id)
    timers = [timer("room", place="channel"), timer("dm", place="direct-message"), timer("t")]
    await run("import", str(manifest(tmp_path, timers=timers)), "--tenant", tenant)

    live = await run(*GO_LIVE, tenant, "--agent-id", "agent-1", "--agent-name", "analyst")

    assert live.exit_code == 0, live.output
    assert "timer dm not scheduled" in live.output and "timer room not scheduled" in live.output
    assert "timers not_scheduled=2 scheduled=1" in live.output, live.output
    held = await rows(
        operator, "SELECT source_id FROM held_timers WHERE outcome IS NULL ORDER BY 1"
    )
    assert held == [("dm",), ("room",)], "they stay held, never counted as scheduled"


async def test_each_callback_watch_commits_on_its_own(operator, tmp_path, monkeypatch):
    real = watch_commands.import_callback_watch
    seen: list[int] = []

    async def spy(db, *args, **kwargs):
        seen.append(len(await rows(operator, "SELECT 1 FROM watches")))
        return await real(db, *args, **kwargs)

    monkeypatch.setattr(watch_commands, "import_callback_watch", spy)
    second = {**callback_entry(), "legacy_id": "22222222-2222-2222-2222-222222222222"}
    path = manifest(tmp_path, watches=[callback_entry(), second])

    result = await run(
        *("import", str(path), "--tenant", str(operator.tenant.id)),
        *("--urls-out", str(tmp_path / "urls.json")),
    )

    assert result.exit_code == 0, result.output
    assert seen == [0, 1], "the first watch is committed, its lock released, before the next"

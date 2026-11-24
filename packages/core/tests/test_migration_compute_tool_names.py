"""The compute tool rename migration, on both sides of a rolling deploy.

For a while the release before the rename and the release after it share one
database, each on either side of this migration. The tests run the code of this
release against the schema before it, and statements shaped like the older
release's against the schema after it.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.compute_models import ComputeSession
from daimon.core.compute_provider import app_name
from daimon.core.stores import agent_tool_rules as rules
from daimon.core.stores import compute_sessions as sessions
from daimon.core.tool_safety import DAIMON_SERVER_NAME, RENAMED_DAIMON_TOOLS
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

_URL = "https://daimon.example/mcp"
_RULE = text(
    "INSERT INTO agent_tool_rules (tenant_id, agent_id, agent_name, tool_key, channel_id,"
    " server_url, rule, scope, revision, set_by, set_at) VALUES (:tenant, :agent, :agent,"
    f" :key, :channel, '{_URL}', :rule, :scope, 1, 'admin', now())"
)
_HISTORY = text(
    "INSERT INTO agent_tool_rule_history (tenant_id, agent_id, tool_key, revision, set_by,"
    " set_at, after_rule, after_scope, after_agent_name, after_server_url) VALUES (:tenant,"
    f" 'a1', :key, 1, 'admin', now(), 'deny', 'chat', 'a1', '{_URL}')"
)
_ROWS = text(
    "SELECT agent_id, tool_key, channel_id, rule, scope FROM agent_tool_rules"
    " WHERE tenant_id = :t ORDER BY agent_id, tool_key"
)
# The older release names every column, the CPU class included.
_OLD_RELEASE_INSERT = text(
    "INSERT INTO compute_sessions (id, tenant_id, owner_account_id, owner_external_user_id,"
    " deployment_id, platform, parent_channel_id, thread_id, agent_id, agent_name,"
    " request_key, app_name, profile, image, region, cpu_kind, cpus, memory_mb, ttl_hours,"
    " expires_at, created_at) VALUES (:id, :tenant, :account, 'user', 'd', 'discord',"
    " 'parent', 'thread', 'agent', 'a', :name, :name, 'mmm', 'image', 'iad', 'performance', 2,"
    " 4096, 24, now(), now())"
)


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0226_compute_tool_names.py"
    spec = importlib.util.spec_from_file_location("migration_0226", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_the_lookup_and_the_session_builder_agree_on_the_renamed_tools() -> None:
    """One map names the renamed tools. The migration's frozen copy must equal it."""
    module = migration()
    assert module._SERVER == DAIMON_SERVER_NAME
    assert {f"fly_{tool}": f"compute_{tool}" for tool in module._TOOLS} == RENAMED_DAIMON_TOOLS
    old_keys = {
        f"{DAIMON_SERVER_NAME}/{new}": f"{DAIMON_SERVER_NAME}/{old}"
        for old, new in RENAMED_DAIMON_TOOLS.items()
    }
    assert old_keys == rules._OLD_KEY


def _session(tenant_id: uuid.UUID, account_id: uuid.UUID) -> ComputeSession:
    session_id, now = uuid.uuid4(), datetime.now(UTC)
    return ComputeSession(
        id=session_id,
        tenant_id=tenant_id,
        owner_account_id=account_id,
        owner_external_user_id="user",
        deployment_id="d",
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        agent_id="agent",
        agent_name="a",
        request_key=str(session_id),
        app_name=app_name("d", session_id),
        profile="mmm",
        image="image",
        region="us-central1",
        cpus=2,
        memory_mb=4096,
        ttl_hours=24,
        expires_at=now + timedelta(days=1),
        created_at=now,
    )


def _run(connection: Connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration(), step)()


def _cpu_kind(connection: Connection) -> tuple[bool, str | None]:
    here = connection.scalar(text("SELECT current_schema()"))
    (column,) = [
        c
        for c in inspect(connection).get_columns("compute_sessions", here)
        if c["name"] == "cpu_kind"
    ]
    return column["nullable"], column["default"]


@pytest.mark.fresh_schema
async def test_this_release_and_the_one_before_both_work_on_either_side_of_the_migration(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    params = {"tenant": tenant.id, "account": account.id}

    async def rule(agent: str, key: str, value: str | None, **more: str) -> None:
        values = {"agent": agent, "key": key, "rule": value}
        values |= {"scope": more.get("scope", "chat"), "channel": more.get("channel")}
        await db_session.execute(_RULE, params | values)

    async def in_force(agent: str, tool: str, channel: str | None = None) -> str | None:
        resolved = await rules.get_for_call(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent,
            tool_key=f"daimon-mcp/{tool}",
            server_url=_URL,
            channel_id=channel,
        )
        return resolved.rule if resolved else None

    # --- The schema before the migration.
    await connection.run_sync(_run, "downgrade")
    assert await connection.run_sync(_cpu_kind) == (False, None)
    await rule("a1", "daimon-mcp/fly_run_bash", "deny")
    await rule("a1", "daimon-mcp/fly_stop_session", "ask", channel="c1")
    # Not Daimon's own tools: a relayed server's `fly_*`, and the wildcard.
    await rule("a1", "internal/fly_run_bash", "deny")
    await rule("a1", "daimon-mcp/*", "deny")
    # A place that already has a rule for the new name keeps the stricter one.
    await rule("a2", "daimon-mcp/fly_read_file", "deny", scope="chat_and_routines")
    await rule("a2", "daimon-mcp/compute_read_file", "allow")
    await rule("a3", "daimon-mcp/fly_write_file", "allow")
    await rule("a3", "daimon-mcp/compute_write_file", "ask")
    for key in ("daimon-mcp/fly_run_bash", "internal/fly_run_bash"):
        await db_session.execute(_HISTORY, params | {"key": key})
    history = text("SELECT tool_key FROM agent_tool_rule_history WHERE tenant_id = :tenant")
    before = sorted((await db_session.scalars(history, params)).all())

    # This release on that schema: it finds the deny under the old key, and writes a
    # session the NOT NULL column accepts.
    assert await in_force("a1", "compute_run_bash") == "deny"
    assert await in_force("a1", "compute_stop_session", "c1") == "ask"
    early = await sessions.register(db_session, _session(tenant.id, account.id), cap=9)
    assert await sessions.get(db_session, tenant.id, early.id) == early

    # --- The schema after the migration.
    await connection.run_sync(_run, "upgrade")
    assert await connection.run_sync(_cpu_kind) == (False, "'shared'::text")
    rows = [tuple(row) for row in (await db_session.execute(_ROWS, {"t": tenant.id})).all()]
    assert rows == [
        ("a1", "daimon-mcp/*", None, "deny", "chat"),
        ("a1", "daimon-mcp/compute_run_bash", None, "deny", "chat"),
        ("a1", "daimon-mcp/compute_stop_session", "c1", "ask", "chat"),
        # The older release still finds each rule under the key it reads.
        ("a1", "daimon-mcp/fly_run_bash", None, "deny", "chat"),
        ("a1", "daimon-mcp/fly_stop_session", "c1", "ask", "chat"),
        ("a1", "internal/fly_run_bash", None, "deny", "chat"),
        ("a2", "daimon-mcp/compute_read_file", None, "deny", "chat_and_routines"),
        ("a2", "daimon-mcp/fly_read_file", None, "deny", "chat_and_routines"),
        ("a3", "daimon-mcp/compute_write_file", None, "ask", "chat"),
        ("a3", "daimon-mcp/fly_write_file", None, "allow", "chat"),
    ]
    # History says what an admin did; the copy adds nothing to it.
    assert sorted((await db_session.scalars(history, params)).all()) == before
    assert await in_force("a1", "compute_run_bash") == "deny"
    assert await in_force("a2", "compute_read_file") == "deny"

    # The older release on this schema: it names the CPU class and reads it back.
    await sessions.scope(db_session, tenant.id)
    old_id = uuid.uuid4()
    await db_session.execute(_OLD_RELEASE_INSERT, params | {"id": old_id, "name": old_id.hex})
    kinds = text("SELECT cpu_kind FROM compute_sessions WHERE tenant_id = :tenant ORDER BY 1")
    # This release wrote `shared` before the default existed, and still writes it.
    late = await sessions.register(db_session, _session(tenant.id, account.id), cap=9)
    assert (await db_session.scalars(kinds, params)).all() == ["performance", "shared", "shared"]
    # Each release reads the rows the other wrote.
    assert {s.id for s in await sessions.list_sessions(db_session, tenant.id)} == {
        early.id,
        old_id,
        late.id,
    }

    def matches_the_models(sync: Connection) -> None:
        differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
        assert not [d for d in differences if "compute_sessions" in str(d)], differences

    await connection.run_sync(matches_the_models)

    # --- Back down: the new name was in force, so its rule is the one kept.
    await db_session.execute(
        text(
            "UPDATE agent_tool_rules SET rule = 'allow' WHERE tenant_id = :tenant"
            " AND tool_key = 'daimon-mcp/compute_run_bash'"
        ),
        params,
    )
    await connection.run_sync(_run, "downgrade")
    rows = [tuple(row) for row in (await db_session.execute(_ROWS, {"t": tenant.id})).all()]
    assert rows == [
        ("a1", "daimon-mcp/*", None, "deny", "chat"),
        ("a1", "daimon-mcp/fly_run_bash", None, "allow", "chat"),
        ("a1", "daimon-mcp/fly_stop_session", "c1", "ask", "chat"),
        ("a1", "internal/fly_run_bash", None, "deny", "chat"),
        ("a2", "daimon-mcp/fly_read_file", None, "deny", "chat_and_routines"),
        ("a3", "daimon-mcp/fly_write_file", None, "ask", "chat"),
    ]
    assert await connection.run_sync(_cpu_kind) == (False, None)

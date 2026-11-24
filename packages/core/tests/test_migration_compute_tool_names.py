"""The compute tool rename migration: an admin's rule keeps applying under the new name."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.core.tool_safety import DAIMON_SERVER_NAME, RENAMED_DAIMON_TOOLS
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

_RULE = text(
    "INSERT INTO agent_tool_rules (tenant_id, agent_id, agent_name, tool_key, channel_id,"
    " server_url, rule, scope, revision, set_by, set_at) VALUES (:tenant, :agent, :agent,"
    " :key, :channel, 'https://daimon.example/mcp', :rule, :scope, 1, 'admin', now())"
)
_HISTORY = text(
    "INSERT INTO agent_tool_rule_history (tenant_id, agent_id, tool_key, revision, set_by,"
    " set_at, after_rule, after_scope, after_agent_name, after_server_url) VALUES (:tenant,"
    " 'a1', :key, 1, 'admin', now(), 'deny', 'chat', 'a1', 'https://daimon.example/mcp')"
)


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0226_compute_tool_names.py"
    spec = importlib.util.spec_from_file_location("migration_0226", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_moves_exactly_the_tools_the_code_renamed() -> None:
    module = migration()
    assert module._SERVER == DAIMON_SERVER_NAME
    assert {f"fly_{tool}": f"compute_{tool}" for tool in module._TOOLS} == RENAMED_DAIMON_TOOLS


@pytest.mark.fresh_schema
async def test_rules_follow_the_renamed_tools_and_no_deny_is_lost(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    module = migration()
    tenant = uuid.uuid4()

    def rule(sync: Connection, agent: str, key: str, value: str | None, **more: str) -> None:
        sync.execute(
            _RULE,
            {
                "tenant": tenant,
                "agent": agent,
                "key": key,
                "rule": value,
                "scope": more.get("scope", "chat"),
                "channel": more.get("channel"),
            },
        )

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))
        with Operations.context(MigrationContext.configure(sync)):
            module.downgrade()
            columns = {c["name"] for c in inspect(sync).get_columns("compute_sessions", here)}
            assert "cpu_kind" in columns
            sync.execute(
                text("INSERT INTO tenants (id, platform, external_id) VALUES (:t, 'discord', 'g')"),
                {"t": tenant},
            )
            rule(sync, "a1", "daimon-mcp/fly_run_bash", "deny")
            rule(sync, "a1", "daimon-mcp/fly_stop_session", "ask", channel="c1")
            # Not Daimon's own tools: a relayed server's `fly_*`, and the wildcard.
            rule(sync, "a1", "internal/fly_run_bash", "deny")
            rule(sync, "a1", "daimon-mcp/*", "deny")
            # A place that already has a rule for the new name keeps the stricter one.
            rule(sync, "a2", "daimon-mcp/fly_read_file", "deny", scope="chat_and_routines")
            rule(sync, "a2", "daimon-mcp/compute_read_file", "allow")
            rule(sync, "a3", "daimon-mcp/fly_write_file", "allow")
            rule(sync, "a3", "daimon-mcp/compute_write_file", "ask")
            rule(sync, "a4", "daimon-mcp/fly_list_files", "deny", scope="chat_and_routines")
            rule(sync, "a4", "daimon-mcp/compute_list_files", "deny")
            for key in ("daimon-mcp/fly_run_bash", "internal/fly_run_bash"):
                sync.execute(_HISTORY, {"tenant": tenant, "key": key})
            module.upgrade()
            rows = sync.execute(
                text(
                    "SELECT agent_id, tool_key, channel_id, rule, scope FROM agent_tool_rules"
                    " WHERE tenant_id = :t ORDER BY agent_id, tool_key"
                ),
                {"t": tenant},
            ).all()
            assert [tuple(row) for row in rows] == [
                ("a1", "daimon-mcp/*", None, "deny", "chat"),
                ("a1", "daimon-mcp/compute_run_bash", None, "deny", "chat"),
                ("a1", "daimon-mcp/compute_stop_session", "c1", "ask", "chat"),
                ("a1", "internal/fly_run_bash", None, "deny", "chat"),
                ("a2", "daimon-mcp/compute_read_file", None, "deny", "chat_and_routines"),
                ("a3", "daimon-mcp/compute_write_file", None, "ask", "chat"),
                ("a4", "daimon-mcp/compute_list_files", None, "deny", "chat_and_routines"),
            ]
            history = sync.scalars(
                text(
                    "SELECT tool_key FROM agent_tool_rule_history WHERE tenant_id = :t"
                    " ORDER BY tool_key"
                ),
                {"t": tenant},
            ).all()
            assert history == ["daimon-mcp/compute_run_bash", "internal/fly_run_bash"]
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [d for d in differences if "compute_sessions" in str(d)], differences

    await connection.run_sync(cycle)

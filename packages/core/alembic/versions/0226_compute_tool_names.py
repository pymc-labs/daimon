"""compute_tool_names: the session tools are `compute_*`, and a session has no CPU class.

Daimon's own session tools were named `fly_*` and are now `compute_*`. A tool rule is
keyed `connection/tool`, so a rule an admin set on `daimon-mcp/fly_run_bash` would stop
matching, and a deny would silently stop denying. Every rule and history row keyed by
one of the old names moves to the new name.

Only `daimon-mcp/` keys move. A `fly_*` tool of any other connection belongs to
another server and keeps its key.

A place that already holds a rule for the new name keeps the stricter of the two
(deny, then ask, then allow, then cleared; at the same rule, the wider scope), so no
deny is lost. The old row then goes.

`compute_sessions.cpu_kind` was the Fly CPU class. No provider reads it; it is dropped.
Downgrading adds it back as `shared` for every row and moves the keys back.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0226_compute_tool_names"
down_revision: str | None = "0225_agent_github_grant_removals"
branch_labels: str | None = None
depends_on: str | None = None

_SERVER = "daimon-mcp"
_TOOLS = (
    "launch_session",
    "extend_session",
    "stop_session",
    "run_bash",
    "write_file",
    "read_file",
    "list_files",
    "list_sessions",
    "get_session_status",
    "task_status",
    "task_result",
    "list_templates",
)

_RANK = "CASE {row}.rule WHEN 'deny' THEN 3 WHEN 'ask' THEN 2 WHEN 'allow' THEN 1 ELSE 0 END"
_TWIN = """
    stale.tool_key = :stale AND kept.tool_key = :kept
    AND stale.tenant_id = kept.tenant_id AND stale.agent_id = kept.agent_id
    AND stale.channel_id IS NOT DISTINCT FROM kept.channel_id
"""
_KEEP_STRICTER = sa.text(
    f"""
    UPDATE agent_tool_rules AS kept
    SET rule = stale.rule, scope = stale.scope, revision = kept.revision + 1,
        set_by = stale.set_by, set_at = stale.set_at
    FROM agent_tool_rules AS stale
    WHERE {_TWIN} AND (
        {_RANK.format(row="stale")} > {_RANK.format(row="kept")}
        OR ({_RANK.format(row="stale")} = {_RANK.format(row="kept")}
            AND stale.scope = 'chat_and_routines' AND kept.scope = 'chat')
    )
    """
)
_DROP_TWIN = sa.text(
    f"DELETE FROM agent_tool_rules AS stale USING agent_tool_rules AS kept WHERE {_TWIN}"
)
_MOVE = "UPDATE {table} SET tool_key = :kept WHERE tool_key = :stale"


def _rename(old_prefix: str, new_prefix: str) -> None:
    connection = op.get_bind()
    for tool in _TOOLS:
        keys = {"stale": f"{_SERVER}/{old_prefix}{tool}", "kept": f"{_SERVER}/{new_prefix}{tool}"}
        connection.execute(_KEEP_STRICTER, keys)
        connection.execute(_DROP_TWIN, keys)
        for table in ("agent_tool_rules", "agent_tool_rule_history"):
            connection.execute(sa.text(_MOVE.format(table=table)), keys)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _rename("fly_", "compute_")
    op.drop_column("compute_sessions", "cpu_kind")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "compute_sessions",
        sa.Column("cpu_kind", sa.Text(), nullable=False, server_default="shared"),
    )
    op.alter_column("compute_sessions", "cpu_kind", server_default=None)
    _rename("compute_", "fly_")

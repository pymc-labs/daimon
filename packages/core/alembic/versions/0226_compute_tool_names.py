"""compute_tool_names: rules for the renamed session tools, and a default CPU class.

Daimon's own session tools were named `fly_*` and are now `compute_*`. A tool rule is
keyed `connection/tool`, so the new release looks for `daimon-mcp/compute_run_bash`
where an admin's deny says `daimon-mcp/fly_run_bash`.

Each rule under an old name is copied to the new name, and the old row stays. During
a deploy the older release still finds its rule under the old key and the newer one
under the new key, so a deny holds at every moment. A later release deletes the old
keys. A place that already holds a rule for the new name keeps the stricter of the two
(deny, then ask, then allow, then cleared; at the same rule, the wider scope).

Only `daimon-mcp/` keys are copied. A `fly_*` tool of any other connection belongs to
another server.

History rows are left alone. They record what an admin did, under the name the tool
had then; a copy would show changes nobody made. The copy keeps the old rule's author,
time and revision.

`compute_sessions.cpu_kind` was the Fly CPU class. No provider reads it. It keeps its
NOT NULL and gains the default `shared`, so the older release still reads and writes
it and a row written without it is valid. A later release drops the column.

Downgrading puts each new-name rule back under the old name, where it replaces the old
row: the new name was the one in force. It then removes the new-name rows and the
default.

downgrade: safe
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
_SAME_PLACE = """
    source.tenant_id = target.tenant_id AND source.agent_id = target.agent_id
    AND source.channel_id IS NOT DISTINCT FROM target.channel_id
"""
_KEEP_STRICTER = sa.text(
    f"""
    UPDATE agent_tool_rules AS target
    SET rule = source.rule, scope = source.scope, revision = target.revision + 1,
        set_by = source.set_by, set_at = source.set_at
    FROM agent_tool_rules AS source
    WHERE source.tool_key = :source AND target.tool_key = :target AND {_SAME_PLACE} AND (
        {_RANK.format(row="source")} > {_RANK.format(row="target")}
        OR ({_RANK.format(row="source")} = {_RANK.format(row="target")}
            AND source.scope = 'chat_and_routines' AND target.scope = 'chat')
    )
    """
)
_REPLACE = sa.text(
    f"""
    UPDATE agent_tool_rules AS target
    SET rule = source.rule, scope = source.scope, revision = target.revision + 1,
        set_by = source.set_by, set_at = source.set_at
    FROM agent_tool_rules AS source
    WHERE source.tool_key = :source AND target.tool_key = :target AND {_SAME_PLACE}
        AND (source.rule, source.scope) IS DISTINCT FROM (target.rule, target.scope)
    """
)
_COPY = sa.text(
    f"""
    INSERT INTO agent_tool_rules (tenant_id, agent_id, agent_name, tool_key, channel_id,
        server_url, rule, scope, revision, set_by, set_at)
    SELECT source.tenant_id, source.agent_id, source.agent_name, :target, source.channel_id,
        source.server_url, source.rule, source.scope, source.revision, source.set_by,
        source.set_at
    FROM agent_tool_rules AS source
    WHERE source.tool_key = :source AND NOT EXISTS (
        SELECT 1 FROM agent_tool_rules AS target
        WHERE target.tool_key = :target AND {_SAME_PLACE}
    )
    """
)
_REMOVE = sa.text("DELETE FROM agent_tool_rules WHERE tool_key = :source")


def _copy(source_prefix: str, target_prefix: str, *, move: bool) -> None:
    connection = op.get_bind()
    for tool in _TOOLS:
        keys = {
            "source": f"{_SERVER}/{source_prefix}{tool}",
            "target": f"{_SERVER}/{target_prefix}{tool}",
        }
        connection.execute(_REPLACE if move else _KEEP_STRICTER, keys)
        connection.execute(_COPY, keys)
        if move:
            connection.execute(_REMOVE, {"source": keys["source"]})


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _copy("fly_", "compute_", move=False)
    op.alter_column("compute_sessions", "cpu_kind", server_default="shared")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _copy("compute_", "fly_", move=True)
    op.alter_column("compute_sessions", "cpu_kind", server_default=None)

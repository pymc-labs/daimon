"""sandbox_tools_feature: built-in sandbox tools become the `routine_sandbox_tools` feature.

`feature_values` gains the `routine` tier (`routine_id`), the most specific for a
routine's runs; it takes no lock. `sandbox_pins` records what an On covers: the agent
id and environment name at the agent tier, the agent version and environment revision
at the routine tier. Each routine opt-in moves to a routine-tier On with its pin, and its
history to `channel_changes`; DM routines never ran built-ins, so theirs are dropped.
The routine columns and `routine_sandbox_history` go. `system` joins both surface
checks for changes Daimon makes itself. Downgrading restores routine opt-ins and their
history and drops agent opt-ins, routine Offs and the pins.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0206_sandbox_tools_feature"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None

_KEY = "routine_sandbox_tools"
_PIN_JSON = (
    "jsonb_build_object('agent_id', {p}agent_id, 'agent_version', {p}agent_version,"
    " 'environment_id', {p}environment_id,"
    " 'environment_updated_at', to_jsonb({p}environment_updated_at))"
)


def _feature_checks(tiers: str, surfaces: str, unlockable: str) -> None:
    for name in ("ck_feature_values_tier", "ck_feature_values_surface", "ck_feature_values_lock"):
        op.drop_constraint(name, "feature_values", type_="check")
    op.create_check_constraint("ck_feature_values_tier", "feature_values", f"tier IN ({tiers})")
    op.create_check_constraint(
        "ck_feature_values_surface", "feature_values", f"surface IN ({surfaces})"
    )
    op.create_check_constraint(
        "ck_feature_values_lock", "feature_values", f"NOT (locked AND tier IN ({unlockable}))"
    )


def _place_index(columns: list[str]) -> None:
    op.drop_index("uq_feature_values_place", table_name="feature_values")
    op.create_index(
        "uq_feature_values_place",
        "feature_values",
        ["tenant_id", "tier", *columns, "key"],
        unique=True,
        postgresql_where=sa.text("tier <> 'thread'"),
    )


def _change_surfaces(surfaces: str) -> None:
    op.drop_constraint("ck_channel_changes_surface", "channel_changes", type_="check")
    op.create_check_constraint(
        "ck_channel_changes_surface", "channel_changes", f"surface IN ({surfaces})"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "feature_values",
        sa.Column("routine_id", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    _feature_checks(
        "'workspace', 'agent', 'channel', 'thread', 'routine'",
        "'web', 'chat', 'panel', 'cli', 'system', 'spec', 'migration'",
        "'thread', 'routine'",
    )
    _place_index(["agent_id", "channel_id", "routine_id"])
    _change_surfaces("'web', 'chat', 'panel', 'cli', 'system'")
    op.create_table(
        "sandbox_pins",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("routine_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("environment_name", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.Column("environment_id", sa.Text(), nullable=True),
        sa.Column("environment_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["routine_id"], ["routines.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "(tier = 'agent' AND routine_id IS NULL AND environment_name IS NOT NULL"
            " AND agent_version IS NULL AND environment_id IS NULL"
            " AND environment_updated_at IS NULL)"
            " OR (tier = 'routine' AND routine_id IS NOT NULL AND environment_name IS NULL"
            " AND agent_version IS NOT NULL AND environment_id IS NOT NULL"
            " AND environment_updated_at IS NOT NULL)",
            name="ck_sandbox_pins_shape",
        ),
    )
    for tier, column in (("agent", "agent_id"), ("routine", "routine_id")):
        op.create_index(
            f"uq_sandbox_pins_{tier}",
            "sandbox_pins",
            ["tenant_id", column],
            unique=True,
            postgresql_where=sa.text(f"tier = '{tier}'"),
        )
    # An opt-in without a full pin never ran built-ins, so it is not carried over.
    opted_in = (
        "sandbox_tools_enabled AND sandbox_agent_id IS NOT NULL"
        " AND sandbox_agent_version IS NOT NULL AND sandbox_environment_id IS NOT NULL"
        " AND sandbox_environment_updated_at IS NOT NULL"
        " AND destination_kind IS DISTINCT FROM 'dm'"
    )
    op.execute(
        "INSERT INTO feature_values (tenant_id, tier, routine_id, key, value, surface, set_at)"
        f" SELECT tenant_id, 'routine', id::text, '{_KEY}', 'on', 'migration',"
        f" coalesce(sandbox_tools_set_at, now()) FROM routines WHERE {opted_in}"
    )
    op.execute(
        "INSERT INTO sandbox_pins (tenant_id, tier, agent_id, routine_id, agent_version,"
        " environment_id, environment_updated_at, set_at)"
        " SELECT tenant_id, 'routine', sandbox_agent_id, id, sandbox_agent_version,"
        " sandbox_environment_id, sandbox_environment_updated_at,"
        f" coalesce(sandbox_tools_set_at, now()) FROM routines WHERE {opted_in}"
    )
    # The old Off meant no opt-in, which is the new inherited value: a null side.
    op.execute(
        "INSERT INTO channel_changes (tenant_id, platform, channel_id, setting, before, after,"
        " surface, at)"
        f" SELECT h.tenant_id, t.platform, '*', '{_KEY}',"
        " CASE WHEN h.before_enabled THEN jsonb_build_object('routine', h.routine_id::text,"
        " 'value', 'on') END,"
        " CASE WHEN h.after_enabled THEN jsonb_build_object('routine', h.routine_id::text,"
        f" 'value', 'on', 'pin', {_PIN_JSON.format(p='h.')}) END,"
        " CASE h.via WHEN 'cli' THEN 'cli' WHEN 'mcp' THEN 'chat' ELSE 'system' END, h.set_at"
        " FROM routine_sandbox_history h JOIN tenants t ON t.id = h.tenant_id"
    )
    op.drop_table("routine_sandbox_history")
    for column in (
        "sandbox_tools_enabled",
        "sandbox_tools_set_by",
        "sandbox_tools_set_at",
        "sandbox_agent_id",
        "sandbox_agent_version",
        "sandbox_environment_id",
        "sandbox_environment_updated_at",
    ):
        op.drop_column("routines", column)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "routines",
        sa.Column("sandbox_tools_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("routines", sa.Column("sandbox_tools_set_by", sa.Text(), nullable=True))
    op.add_column(
        "routines", sa.Column("sandbox_tools_set_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("routines", sa.Column("sandbox_agent_id", sa.Text(), nullable=True))
    op.add_column("routines", sa.Column("sandbox_agent_version", sa.Integer(), nullable=True))
    op.add_column("routines", sa.Column("sandbox_environment_id", sa.Text(), nullable=True))
    op.add_column(
        "routines",
        sa.Column("sandbox_environment_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "routine_sandbox_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("routine_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("before_enabled", sa.Boolean(), nullable=False),
        sa.Column("after_enabled", sa.Boolean(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("agent_version", sa.Integer(), nullable=True),
        sa.Column("environment_id", sa.Text(), nullable=True),
        sa.Column("environment_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("via IN ('mcp', 'cli', 'edit')", name="ck_routine_sandbox_history_via"),
    )
    op.create_index(
        "ix_routine_sandbox_history_routine",
        "routine_sandbox_history",
        ["tenant_id", "routine_id", "set_at"],
    )
    op.execute(
        "UPDATE routines r SET sandbox_tools_enabled = true,"
        " sandbox_tools_set_by = coalesce(f.set_by::text, f.surface),"
        " sandbox_tools_set_at = f.set_at, sandbox_agent_id = p.agent_id,"
        " sandbox_agent_version = p.agent_version, sandbox_environment_id = p.environment_id,"
        " sandbox_environment_updated_at = p.environment_updated_at"
        " FROM feature_values f JOIN sandbox_pins p ON p.tenant_id = f.tenant_id"
        " AND p.tier = 'routine' AND p.routine_id::text = f.routine_id"
        f" WHERE f.tier = 'routine' AND f.key = '{_KEY}' AND f.value = 'on'"
        " AND r.tenant_id = f.tenant_id AND r.id::text = f.routine_id"
    )
    op.execute(
        "INSERT INTO routine_sandbox_history (tenant_id, routine_id, before_enabled,"
        " after_enabled, set_by, set_at, via, agent_id, agent_version, environment_id,"
        " environment_updated_at)"
        " SELECT c.tenant_id, (coalesce(c.after, c.before)->>'routine')::uuid,"
        " coalesce(c.before->>'value' = 'on', false), coalesce(c.after->>'value' = 'on', false),"
        " coalesce(c.actor_account_id::text, c.surface), c.at,"
        " CASE c.surface WHEN 'cli' THEN 'cli' WHEN 'system' THEN 'edit' ELSE 'mcp' END,"
        " c.after->'pin'->>'agent_id', (c.after->'pin'->>'agent_version')::int,"
        " c.after->'pin'->>'environment_id',"
        " (c.after->'pin'->>'environment_updated_at')::timestamptz"
        f" FROM channel_changes c WHERE c.setting = '{_KEY}'"
        " AND coalesce(c.after, c.before)->>'routine' IS NOT NULL"
    )
    # Agent opt-ins and Daimon's own changes have no older form.
    op.execute(f"DELETE FROM channel_changes WHERE setting = '{_KEY}' OR surface = 'system'")
    op.execute(f"DELETE FROM feature_values WHERE tier = 'routine' OR key = '{_KEY}'")
    op.execute("DELETE FROM feature_values WHERE surface = 'system'")
    op.drop_table("sandbox_pins")
    _change_surfaces("'web', 'chat', 'panel', 'cli'")
    _place_index(["agent_id", "channel_id"])
    _feature_checks(
        "'workspace', 'agent', 'channel', 'thread'",
        "'web', 'chat', 'panel', 'cli', 'spec', 'migration'",
        "'thread'",
    )
    op.drop_column("feature_values", "routine_id")

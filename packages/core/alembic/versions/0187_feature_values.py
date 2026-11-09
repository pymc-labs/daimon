"""feature_values: one table for every feature set at a workspace, agent, channel or thread

Moves thread participation and progress plans out of their scope tables and the
cost footer out of `tenants.cost_footer`. A legacy `disabled` becomes Off + Locked
(a thread row has nothing below it, so there it is plain Off). Where one tenant has
workspace rows for several platforms, the tenant's own platform wins. `tenants` gains
`feature_revision`, bumped on every write, which keys the resolver's cache.

Revision ID: 0187_feature_values
Revises: 0186_github_router_sessions

Downgrading drops agent-tier rows and a Lock on an On.

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0187_feature_values"
down_revision: str | None = "0186_github_router_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_TABLES = {
    "thread_participation_scopes": "participation",
    "progress_plan_scopes": "progress_plans",
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "feature_values",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("channel_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("thread_key", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "set_by",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(
            "tier IN ('workspace', 'agent', 'channel', 'thread')", name="ck_feature_values_tier"
        ),
        sa.CheckConstraint(
            "surface IN ('web', 'chat', 'panel', 'cli', 'spec', 'migration')",
            name="ck_feature_values_surface",
        ),
        sa.CheckConstraint("NOT (locked AND tier = 'thread')", name="ck_feature_values_lock"),
    )
    op.create_index(
        "uq_feature_values_place",
        "feature_values",
        ["tenant_id", "tier", "agent_id", "channel_id", "key"],
        unique=True,
        postgresql_where=sa.text("tier <> 'thread'"),
    )
    op.create_index(
        "uq_feature_values_thread",
        "feature_values",
        ["tenant_id", "agent_id", "thread_key", "key"],
        unique=True,
        postgresql_where=sa.text("tier = 'thread'"),
    )
    op.create_index("ix_feature_values_channel", "feature_values", ["tenant_id", "channel_id"])
    op.add_column(
        "tenants",
        sa.Column("feature_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    for table, key in _SCOPE_TABLES.items():
        op.execute(
            f"""
            INSERT INTO feature_values
                (tenant_id, tier, channel_id, thread_key, key, value, locked, surface, set_at)
            SELECT DISTINCT ON (s.tenant_id, s.scope, s.scope_id)
                s.tenant_id,
                s.scope,
                CASE s.scope WHEN 'channel' THEN s.scope_id
                    WHEN 'thread' THEN coalesce(s.parent_id, '') ELSE '' END,
                CASE s.scope WHEN 'thread' THEN s.scope_id ELSE '' END,
                '{key}',
                CASE s.mode WHEN 'disabled' THEN 'off' ELSE s.mode END,
                s.mode = 'disabled' AND s.scope <> 'thread',
                'migration',
                s.updated_at
            FROM {table} s JOIN tenants t ON t.id = s.tenant_id
            ORDER BY s.tenant_id, s.scope, s.scope_id, (s.platform = t.platform) DESC,
                s.updated_at DESC
            """
        )
        op.drop_table(table)
    op.execute(
        "INSERT INTO feature_values (tenant_id, tier, key, value, surface) "
        "SELECT id, 'workspace', 'cost_footer', 'hidden', 'migration' "
        "FROM tenants WHERE NOT cost_footer"
    )
    op.drop_column("tenants", "cost_footer")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenants",
        sa.Column("cost_footer", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.execute(
        "UPDATE tenants SET cost_footer = false FROM feature_values f "
        "WHERE f.tenant_id = tenants.id AND f.tier = 'workspace' "
        "AND f.key = 'cost_footer' AND f.value = 'hidden'"
    )
    for table, key in _SCOPE_TABLES.items():
        op.create_table(
            table,
            sa.Column(
                "tenant_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("platform", sa.Text(), nullable=False),
            sa.Column("scope", sa.Text(), nullable=False),
            sa.Column("scope_id", sa.Text(), nullable=False),
            sa.Column("mode", sa.Text(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("parent_id", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint(
                "tenant_id", "platform", "scope", "scope_id", name=f"pk_{table}"
            ),
            sa.CheckConstraint(
                "scope IN ('workspace', 'channel', 'thread')", name=f"ck_{table}_scope"
            ),
            sa.CheckConstraint("mode IN ('on', 'off', 'disabled')", name=f"ck_{table}_mode"),
        )
        op.create_index(
            f"ix_{table}_parent",
            table,
            ["tenant_id", "platform", "parent_id"],
            postgresql_where=sa.text("parent_id IS NOT NULL"),
        )
        op.execute(
            f"""
            INSERT INTO {table} (tenant_id, platform, scope, scope_id, mode, updated_at, parent_id)
            SELECT f.tenant_id, t.platform, f.tier,
                CASE f.tier WHEN 'channel' THEN f.channel_id
                    WHEN 'thread' THEN f.thread_key ELSE '' END,
                CASE WHEN f.locked AND f.value = 'off' THEN 'disabled' ELSE f.value END,
                f.set_at,
                CASE f.tier WHEN 'thread' THEN nullif(f.channel_id, '') END
            FROM feature_values f JOIN tenants t ON t.id = f.tenant_id
            WHERE f.key = '{key}' AND f.tier <> 'agent'
            """
        )
    op.drop_column("tenants", "feature_revision")
    op.drop_table("feature_values")

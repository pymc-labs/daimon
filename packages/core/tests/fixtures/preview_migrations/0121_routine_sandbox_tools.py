"""Let an admin opt one routine's own runs in to built-in sandbox tools.

The opt-in records the agent version and environment revision it covers.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0121_routine_sandbox_tools"
down_revision: str | None = "0120_watch_polling"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "routines",
        sa.Column("sandbox_tools_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("routines", sa.Column("sandbox_tools_set_by", sa.Text(), nullable=True))
    op.add_column(
        "routines",
        sa.Column("sandbox_tools_set_at", sa.DateTime(timezone=True), nullable=True),
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


def downgrade() -> None:
    op.drop_index("ix_routine_sandbox_history_routine", table_name="routine_sandbox_history")
    op.drop_table("routine_sandbox_history")
    op.drop_column("routines", "sandbox_environment_updated_at")
    op.drop_column("routines", "sandbox_environment_id")
    op.drop_column("routines", "sandbox_agent_version")
    op.drop_column("routines", "sandbox_agent_id")
    op.drop_column("routines", "sandbox_tools_set_at")
    op.drop_column("routines", "sandbox_tools_set_by")
    op.drop_column("routines", "sandbox_tools_enabled")

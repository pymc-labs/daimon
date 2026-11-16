"""Let an operator opt an agent's routine runs in to built-in sandbox tools.

An agent-level opt-in names the agent and the environment, not an agent version,
so an agent edit keeps it. `agent_sandbox_history` records each change.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0206_agent_sandbox_tools"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_sandbox_tools",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("environment_name", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_sandbox_tools"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_sandbox_tools_tenants",
        ),
    )
    op.create_table(
        "agent_sandbox_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("after_enabled", sa.Boolean(), nullable=False),
        sa.Column("environment_name", sa.Text(), nullable=True),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sandbox_history"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_sandbox_history_tenants",
        ),
    )
    op.create_index(
        "ix_agent_sandbox_history_agent",
        "agent_sandbox_history",
        ["tenant_id", "agent_id", "set_at"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_sandbox_history")
    op.drop_table("agent_sandbox_tools")

"""Remember which daimon tools an agent enables by name.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0165_agent_daimon_tools"
down_revision = "0164_watch_lifetime_caps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_daimon_tool_allowlists",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tool_names", ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("agent_daimon_tool_allowlists")

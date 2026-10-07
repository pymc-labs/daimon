"""Record the agent name on panel audit events.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0056_security_audit_agent_name"
down_revision: str | None = "0055_github_token_audit_versions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("security_audit_events", sa.Column("agent_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("security_audit_events", "agent_name")

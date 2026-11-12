"""selfhosted_agent_versions: every revision of a Daimon-owned agent record.

The record tables now also hold OpenAI and Gemini agents that Daimon owns, and
a session reads back the agent version it started on. Existing records get one
row for their current revision. Downgrading drops the history; the records
themselves stay, but earlier versions can no longer be read.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0195_agent_record_versions"
down_revision: str | None = "0194_host_call_cards"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "selfhosted_agent_versions",
        sa.Column(
            "agent_id",
            sa.Text(),
            sa.ForeignKey("selfhosted_agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        "INSERT INTO selfhosted_agent_versions "
        "(agent_id, version, tenant_id, name, spec, archived, created_at) "
        "SELECT id, revision, tenant_id, name, spec, archived_at IS NOT NULL, updated_at "
        "FROM selfhosted_agents"
    )


def downgrade() -> None:
    op.drop_table("selfhosted_agent_versions")

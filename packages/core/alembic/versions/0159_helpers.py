"""helpers: agents an agent may ask inside its turn, and the asks themselves.

`agent_helpers` is each agent's helper list. `helper_asks` holds one ask from a
running turn to a helper and its reply; it cascades with the asking turn's origin.
`turn_origins.helper_of` marks a helper's own turn, and `turn_outcomes` accepts
the `helper` origin. Downgrading drops the lists and any asks in flight.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0159_helpers"
down_revision: str | None = "0158_usage_agent_backfill"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "agent_helpers",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("helper_agent_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "agent_id", "helper_agent_id", name="pk_agent_helpers"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("agent_id <> helper_agent_id", name="ck_agent_helpers_not_self"),
    )
    op.add_column("turn_origins", sa.Column("helper_of", UUID(as_uuid=True), nullable=True))
    op.create_table(
        "helper_asks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("origin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("helper_agent_id", sa.Text(), nullable=False),
        sa.Column("helper_name", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["origin_id"], ["turn_origins.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'stopped')",
            name="ck_helper_asks_status",
        ),
    )
    op.create_index("helper_asks_origin_idx", "helper_asks", ["origin_id", "status"])
    op.drop_constraint("ck_turn_outcomes_origin", "turn_outcomes", type_="check")
    op.execute(
        "ALTER TABLE turn_outcomes ADD CONSTRAINT ck_turn_outcomes_origin "
        "CHECK (origin IN ('chat', 'routine', 'relay', 'handoff', 'helper')) NOT VALID"
    )


def downgrade() -> None:
    op.execute("DELETE FROM turn_outcomes WHERE origin = 'helper'")
    op.drop_constraint("ck_turn_outcomes_origin", "turn_outcomes", type_="check")
    op.execute(
        "ALTER TABLE turn_outcomes ADD CONSTRAINT ck_turn_outcomes_origin "
        "CHECK (origin IN ('chat', 'routine', 'relay', 'handoff')) NOT VALID"
    )
    op.drop_index("helper_asks_origin_idx", table_name="helper_asks")
    op.drop_table("helper_asks")
    op.drop_column("turn_origins", "helper_of")
    op.drop_table("agent_helpers")

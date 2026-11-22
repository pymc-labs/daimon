"""ops_events and ops_issue_states: what an operator watches, and who resolved or muted it.

`ops_events` is append-only and content-free (fixed reason vocabulary; `note` is the only free
text), coalesced in the emitter, deleted with the person's account and pruned after
`DAIMON_OPS_EVENTS__RETENTION_DAYS`. `ops_issue_states` keeps an admin's resolve or mute of one
issue by fingerprint. `turn_outcomes.is_dm` marks turns in a private direct message. Downgrading
drops both tables and the column.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0224_ops_events"
down_revision: str | None = "0223_dm_watch_delivery"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "turn_outcomes",
        sa.Column("is_dm", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "ops_events",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("routine_id", UUID(as_uuid=True), nullable=True),
        sa.Column("watch_id", UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("subject_key", sa.Text(), nullable=True),
        sa.Column("release", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ops_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_ops_events_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE", name="fk_ops_events_accounts"
        ),
        sa.CheckConstraint("severity IN ('error', 'warning')", name="ck_ops_events_severity"),
        sa.CheckConstraint(
            "area IN ('turn', 'dm', 'routine', 'delivery', 'watch', 'wake', 'bridge')",
            name="ck_ops_events_area",
        ),
        sa.CheckConstraint("count >= 1", name="ck_ops_events_count"),
        sa.CheckConstraint("char_length(note) <= 300", name="ck_ops_events_note"),
    )
    op.create_index(
        "ix_ops_events_tenant_at", "ops_events", ["tenant_id", sa.text("occurred_at DESC")]
    )
    op.create_index(
        "ix_ops_events_fingerprint",
        "ops_events",
        ["tenant_id", "fingerprint", sa.text("occurred_at DESC")],
    )
    op.create_index("ix_ops_events_occurred", "ops_events", ["occurred_at"])
    op.create_index(
        "ix_ops_events_account",
        "ops_events",
        ["account_id"],
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )
    op.create_table(
        "ops_issue_states",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "fingerprint", name="pk_ops_issue_states"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_ops_issue_states_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_ops_issue_states_accounts",
        ),
        sa.CheckConstraint("state IN ('resolved', 'muted')", name="ck_ops_issue_states_state"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("ops_issue_states")
    op.drop_table("ops_events")
    op.drop_column("turn_outcomes", "is_dm")

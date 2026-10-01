"""Effective session configuration, replacement lineage, and queued continuations.

Revision ID: 0016_session_continuity
Revises: 0015_setup_conversations

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0016_session_continuity"
down_revision: str | None = "0015_setup_conversations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # thread_sessions: what the session is actually running, plus lineage.
    # Every column is nullable — existing live rows predate snapshots and must
    # keep working (they read as "unknown configuration, refresh on next bind").
    op.add_column("thread_sessions", sa.Column("effective_config", JSONB(), nullable=True))
    op.add_column("thread_sessions", sa.Column("identity_fingerprint", sa.Text(), nullable=True))
    op.add_column("thread_sessions", sa.Column("mutable_fingerprint", sa.Text(), nullable=True))
    op.add_column(
        "thread_sessions",
        sa.Column(
            "predecessor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("thread_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "thread_sessions",
        sa.Column(
            "replaced_by_id",
            UUID(as_uuid=True),
            sa.ForeignKey("thread_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("thread_sessions", sa.Column("transfer_file_id", sa.Text(), nullable=True))
    op.add_column("thread_sessions", sa.Column("transfer_kind", sa.Text(), nullable=True))
    op.add_column(
        "thread_sessions",
        sa.Column("fresh_start_requested_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "session_preparations",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "mapping_id",
            UUID(as_uuid=True),
            sa.ForeignKey("thread_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_fingerprint", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("transfer_file_id", sa.Text(), nullable=True),
        sa.Column("transfer_kind", sa.Text(), nullable=True),
        sa.Column(
            "new_mapping_id",
            UUID(as_uuid=True),
            sa.ForeignKey("thread_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "mapping_id",
            "target_fingerprint",
            name="uq_session_preparations_mapping_fingerprint",
        ),
        sa.CheckConstraint(
            "stage IN ('decided', 'checkpointed', 'uploaded', 'created', 'completed', 'failed')",
            name="ck_session_preparations_stage",
        ),
    )
    op.create_index("session_preparations_mapping_idx", "session_preparations", ["mapping_id"])

    op.create_table(
        "task_continuations",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("parent_channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("requester_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("requester_external_user_id", sa.Text(), nullable=False),
        sa.Column("target_ma_agent_id", sa.Text(), nullable=False),
        sa.Column("target_name", sa.Text(), nullable=False),
        sa.Column("requested_work", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('task_handoff', 'private_input_applied')",
            name="ck_task_continuations_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'skipped')",
            name="ck_task_continuations_status",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_task_continuations_idempotency_key"),
    )
    op.create_index(
        "task_continuations_thread_idx",
        "task_continuations",
        ["tenant_id", "platform", "thread_id", "status"],
    )

    # A thread binding may now also record a task handoff, not only a setup
    # conversation. Postgres cannot widen a CHECK in place.
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff')",
    )


def downgrade() -> None:
    # Narrowing the CHECK again would fail against rows this phase wrote, so
    # drop them first: a handoff binding is meaningless without the code that
    # reads it.
    op.execute(sa.text("DELETE FROM thread_agent_bindings WHERE kind <> 'setup'"))
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind = 'setup'",
    )

    op.drop_index("task_continuations_thread_idx", table_name="task_continuations")
    op.drop_table("task_continuations")
    op.drop_index("session_preparations_mapping_idx", table_name="session_preparations")
    op.drop_table("session_preparations")

    for column in (
        "fresh_start_requested_at",
        "transfer_kind",
        "transfer_file_id",
        "replaced_by_id",
        "predecessor_id",
        "mutable_fingerprint",
        "identity_fingerprint",
        "effective_config",
    ):
        op.drop_column("thread_sessions", column)

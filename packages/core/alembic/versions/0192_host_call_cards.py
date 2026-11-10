"""host_call_cards: a gated tool call waiting on its requester's Approve card.

A provider-hosted turn (OpenAI, Gemini) has no confirmation surface of its own, so
Daimon records the validated call, posts the card itself, and runs the call in a
wake turn once the requester approves. Also allows the `confirmed_call` wake reason.
Additive only; downgrading drops the table, and unanswered cards then do nothing
when pressed. NOT VALID keeps any `confirmed_call` wake rows on downgrade.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0192_host_call_cards"
down_revision: str | None = "0191_deployment_identity"
branch_labels: str | None = None
depends_on: str | None = None

_REASONS = (
    "'task_handoff', 'private_input_applied', 'timer', "
    "'github_access_ready', 'watch_fired', 'job_finished', 'watch_report'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "host_call_cards",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "requester_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requester_platform_user_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("executing_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("executing_ma_agent_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args_digest", sa.Text(), nullable=False),
        sa.Column("reference", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'executing', 'applied', 'failed', 'denied', "
            "'cancelled', 'expired')",
            name="ck_host_call_cards_status",
        ),
    )
    op.create_index(
        "uq_host_call_cards_pending_call",
        "host_call_cards",
        ["origin_id", "tool_name", "args_digest"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_host_call_cards_thread_status",
        "host_call_cards",
        ["tenant_id", "platform", "thread_id", "status"],
    )
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        f"CHECK (reason IN ({_REASONS}, 'confirmed_call')) NOT VALID"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.execute(
        "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
        f"CHECK (reason IN ({_REASONS})) NOT VALID"
    )
    op.drop_index("ix_host_call_cards_thread_status", table_name="host_call_cards")
    op.drop_index("uq_host_call_cards_pending_call", table_name="host_call_cards")
    op.drop_table("host_call_cards")

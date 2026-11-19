"""pending_carry_overs: a replacement session's framing until its first message is sent.

One row per `(tenant_id, session_id)`: the earlier conversation's framing and its
system blocks, written when a replacement is prepared and deleted once the first
message is sent, on every path (chat, routines, watches, wakes). The scheduler
drops rows older than a week. Downgrading drops the table and any framing still
waiting, so such a session starts without its earlier conversation.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0203_pending_carry_overs"
down_revision: str | None = "0202_agent_backend_environment"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "pending_carry_overs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_prefix", sa.Text(), nullable=False),
        sa.Column(
            "system_blocks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pending_carry_overs"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_pending_carry_overs_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
            name="fk_pending_carry_overs_accounts",
        ),
        sa.UniqueConstraint("tenant_id", "session_id", name="uq_pending_carry_overs_session"),
    )
    op.create_index("ix_pending_carry_overs_created_at", "pending_carry_overs", ["created_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("pending_carry_overs")

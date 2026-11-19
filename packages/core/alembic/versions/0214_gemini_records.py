"""gemini_records: one row per Gemini driver record instead of one blob per account.

Schema only. The first new-code touch of an account moves its `records` blob
into these rows and leaves a tombstone that older code refuses to decode, so
after it these rows are the only copy of live conversations, so a downgrade
drops the table only while it is empty and refuses otherwise.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0214_gemini_records"
down_revision: str | None = "0213_provider_key_gate"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "gemini_records",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "touched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("binding_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column(
            "refs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", "project", "kind", "id"),
        sa.CheckConstraint(
            "kind IN ('account', 'session', 'agent', 'environment', 'skill', 'artifact')",
            name="ck_gemini_records_kind",
        ),
    )
    op.create_index("ix_gemini_records_kind_touched_at", "gemini_records", ["kind", "touched_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM gemini_records)")).scalar():
        raise RuntimeError("gemini_records holds the only copy of live conversations")
    op.drop_index("ix_gemini_records_kind_touched_at", table_name="gemini_records")
    op.drop_table("gemini_records")

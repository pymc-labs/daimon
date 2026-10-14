"""Admit each Teams message once, and keep an unhandled one for a restart to replay.

downgrade: destructive

Only the new table is dropped, with its receipts and any message still waiting
for a replay.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0101_teams_message_admissions"
down_revision: str | None = "0100_plain_text_mentions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The tenants foreign key locks that table briefly: fail fast rather than queue behind it.
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "teams_message_admissions",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("owner_key", sa.BigInteger(), nullable=False),
        sa.Column("handled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("message", postgresql.JSONB(), nullable=True),
        sa.Column("notice_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "channel_id", "message_id", name="pk_teams_message_admissions"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_teams_message_admissions_tenants",
        ),
    )
    op.create_index(
        "ix_teams_message_admissions_created",
        "teams_message_admissions",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("teams_message_admissions")

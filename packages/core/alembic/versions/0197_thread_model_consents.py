"""thread_model_consents: a conversation's model on another provider, per agent.

`thread_model_settings.provider` names the provider of the chosen model; null is
a Claude model chosen before providers could be. `thread_model_consents` holds,
per (thread, agent), the consent to the losses that model's provider brings,
taken when the choice was confirmed. Downgrading drops both; those
conversations go back to their agents' own models.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0197_thread_model_consents"
down_revision: str | None = "0196_connector_tokens"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("thread_model_settings", sa.Column("provider", sa.Text(), nullable=True))
    op.create_table(
        "thread_model_consents",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "thread_id", "agent_id", name="pk_thread_model_consents"
        ),
    )


def downgrade() -> None:
    op.drop_table("thread_model_consents")
    op.execute("DELETE FROM thread_model_settings WHERE provider IS NOT NULL")
    op.drop_column("thread_model_settings", "provider")

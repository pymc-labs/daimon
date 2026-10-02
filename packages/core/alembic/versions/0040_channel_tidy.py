"""Record what each agent posts, so it can edit and delete its own messages.

`agent_posted_messages` holds one row per message or thread an agent posted
through `send_message` or `create_thread`: ids, the agent, and a SHA-256 of
the text, never the text. The channel tidy tools act only on rows that name
the calling agent. `security_audit_events` gains four nullable columns the
tidy tools fill on each edit or delete: the target channel and message, the
hash of the text replaced, and the turn it ran in. Existing rows keep NULL.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_channel_tidy"
down_revision: str | None = "0039_skill_uploads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_posted_messages",
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
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("parent_channel_id", sa.Text()),
        sa.Column("thread_ts", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_sha256", sa.Text()),
        sa.Column(
            "posted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "channel_id",
            "message_id",
            name="uq_agent_posted_messages_target",
        ),
        sa.CheckConstraint("kind IN ('message', 'thread')", name="ck_agent_posted_messages_kind"),
    )
    op.add_column("security_audit_events", sa.Column("target_channel_id", sa.Text()))
    op.add_column("security_audit_events", sa.Column("target_message_id", sa.Text()))
    op.add_column("security_audit_events", sa.Column("content_sha256", sa.Text()))
    op.add_column("security_audit_events", sa.Column("turn_ref", sa.Text()))


def downgrade() -> None:
    op.drop_column("security_audit_events", "turn_ref")
    op.drop_column("security_audit_events", "content_sha256")
    op.drop_column("security_audit_events", "target_message_id")
    op.drop_column("security_audit_events", "target_channel_id")
    op.drop_table("agent_posted_messages")

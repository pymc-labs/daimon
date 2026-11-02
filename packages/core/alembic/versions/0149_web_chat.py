"""web_chat: web chats, their messages and who answers them.

Adds `web_threads` (one per chat, cascading from its person) and `web_messages`. A web
chat is bound to the agent it was started with (`thread_agent_bindings.kind = 'web'`),
which answers only the person who started it. `file_uploads.person_id` ties a web file to
its person, so erasing the person erases the file.

Downgrading drops every web chat and file column and deletes web bindings; the CHECK
narrows back.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0149_web_chat"
down_revision: str | None = "0148_web_people"
branch_labels: str | None = None
depends_on: str | None = None


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_threads",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_web_threads"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_web_threads_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["web_people.id"], ondelete="CASCADE", name="fk_web_threads_web_people"
        ),
    )
    op.create_index("ix_web_threads_person_updated", "web_threads", ["person_id", "updated_at"])
    op.create_table(
        "web_messages",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocks", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="done"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_web_messages"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_web_messages_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["web_threads.id"],
            ondelete="CASCADE",
            name="fk_web_messages_web_threads",
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_web_messages_role"),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'stopped', 'interrupted')",
            name="ck_web_messages_status",
        ),
        sa.CheckConstraint("feedback IN ('up', 'down')", name="ck_web_messages_feedback"),
    )
    op.create_index("ix_web_messages_thread_seq", "web_messages", ["thread_id", "seq"])
    op.create_index(
        "ix_web_messages_blocks",
        "web_messages",
        ["blocks"],
        postgresql_using="gin",
        postgresql_ops={"blocks": "jsonb_path_ops"},
    )
    op.add_column("file_uploads", sa.Column("person_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_file_uploads_web_people",
        "file_uploads",
        "web_people",
        ["person_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_file_uploads_person_id", "file_uploads", ["person_id"])
    # Postgres cannot widen a CHECK in place.
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff', 'owner', 'web')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_file_uploads_person_id", table_name="file_uploads")
    op.drop_constraint("fk_file_uploads_web_people", "file_uploads", type_="foreignkey")
    op.drop_column("file_uploads", "person_id")
    op.drop_table("web_messages")
    op.drop_table("web_threads")
    op.execute(sa.text("DELETE FROM thread_agent_bindings WHERE kind = 'web'"))
    op.drop_constraint("ck_thread_agent_bindings_kind", "thread_agent_bindings", type_="check")
    op.create_check_constraint(
        "ck_thread_agent_bindings_kind",
        "thread_agent_bindings",
        "kind IN ('setup', 'handoff', 'owner')",
    )

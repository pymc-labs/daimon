"""Repository memory entries, independent of agent memory stores.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0152_repo_memory"
down_revision = "0151_routine_claimed_slot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("turn_origins", sa.Column("memory_read_only", sa.Boolean(), nullable=True))
    op.create_table(
        "repo_memory",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("created_by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "repo_id", "name"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.CheckConstraint("kind IN ('reference', 'directive')", name="ck_repo_memory_kind"),
        sa.CheckConstraint("scope IN ('repo', 'user')", name="ck_repo_memory_scope"),
        sa.CheckConstraint("scope = 'repo' OR kind = 'directive'", name="ck_repo_memory_personal"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 64", name="ck_repo_memory_name"),
    )

    op.create_index("ix_repo_memory_turn", "repo_memory", ["tenant_id", "repo_id", "kind", "name"])


def downgrade() -> None:
    op.drop_table("repo_memory")
    op.drop_column("turn_origins", "memory_read_only")

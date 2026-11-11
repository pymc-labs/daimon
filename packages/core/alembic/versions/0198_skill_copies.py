"""skill_sizes, skill_copies: custom and channel skills on another provider.

`skill_sizes` measures each source skill version once, for the size limit a
provider profile sets. `skill_copies` records the provider copy of a version
uploaded once and reused by every session. Both are caches: downgrading drops
them and a turn rebuilds what it needs.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0198_skill_copies"
down_revision: str | None = "0197_self_helpers"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "skill_sizes",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("largest_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("text_only", sa.Boolean(), nullable=False),
        sa.Column(
            "measured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "skill_id", "version", name="pk_skill_sizes"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_skill_sizes_tenants"
        ),
    )
    op.create_table(
        "skill_copies",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_scope_id", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("copy_id", sa.Text(), nullable=False),
        sa.Column("copy_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "provider",
            "account_scope_id",
            "skill_id",
            "version",
            name="pk_skill_copies",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_skill_copies_tenants"
        ),
    )


def downgrade() -> None:
    op.drop_table("skill_copies")
    op.drop_table("skill_sizes")

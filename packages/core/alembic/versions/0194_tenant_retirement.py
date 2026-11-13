"""retire tenants instead of deleting them

`tenants.retired_at` marks a workspace whose data is gone but whose accounting
records and balance stay. `tenant_retirements` queues the provider and host cleanup
a retirement still owes; it has no tenant foreign key, so it outlives a tenant deletion.

Revision ID: 0194_tenant_retirement
Revises: 0193_retention_activity

downgrade: safe
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0194_tenant_retirement"
down_revision: str | None = "0193_retention_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("tenants", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "tenant_retirements",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pending", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("tenant_retirements")
    op.drop_column("tenants", "retired_at")

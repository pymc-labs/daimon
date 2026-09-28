"""Store a per-tenant access policy (invoker allowlist, protected and sealed channels).

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_sys047_access_policies"
down_revision: str | None = "0028_tenant_funding_mode"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_access_policies",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_tenant_access_policies_tenants",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_access_policies"),
    )


def downgrade() -> None:
    op.drop_table("tenant_access_policies")

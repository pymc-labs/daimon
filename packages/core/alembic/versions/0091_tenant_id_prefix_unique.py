"""No two tenants share the first 8 hex chars of their id.

Skill titles name a tenant by those 8 chars, and every tenant's skills share
one Managed Agents workspace, so two tenants sharing them would reach each
other's skills. The unique index makes a second registration with the same
prefix fail in the database, concurrent registrations included.

downgrade: safe
"""

from alembic import op

revision: str = "0091_tenant_id_prefix_unique"
down_revision: str | None = "0090_session_settle_request"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE UNIQUE INDEX uq_tenants_id_prefix ON tenants (left(CAST(id AS TEXT), 8))")


def downgrade() -> None:
    op.drop_index("uq_tenants_id_prefix", table_name="tenants")

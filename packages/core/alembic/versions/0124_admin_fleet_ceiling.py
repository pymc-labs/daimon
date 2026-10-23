"""admin_fleet_ceiling: the fleet limit an admin set, kept apart from the current grant.

`agent_backend_revision.admin_fleet_ceiling` is the "off" marker, a fleet grant, or
null (no admin ceiling: a non-admin owner's ceiling is the computed default). Only
an admin's fleet change writes it; every other change, an owner's included, carries
it forward. A non-admin owner can never undo it: an admin's "off" stays off for
them, and an admin's grant bounds them.

Existing rows are backfilled fail closed: a stored grant, or the off marker, becomes
that row's ceiling (who wrote it is unknown), and a revoke from before the off
marker (no grant after an earlier grant) becomes "off". Downgrading drops the
column; owners then fall back to the default.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0124_admin_fleet_ceiling"
down_revision: str | None = "0123_tenant_backends_explicit"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "agent_backend_revision",
        sa.Column("admin_fleet_ceiling", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        "UPDATE agent_backend_revision SET admin_fleet_ceiling = fleet_grant "
        "WHERE fleet_grant IS NOT NULL"
    )
    op.execute(
        "UPDATE agent_backend_revision AS r "
        "SET admin_fleet_ceiling = '{\"disabled\": true}'::jsonb "
        "WHERE r.fleet_grant IS NULL AND EXISTS ("
        "SELECT 1 FROM agent_backend_revision AS e "
        "WHERE e.tenant_id = r.tenant_id AND e.agent_id = r.agent_id "
        "AND e.local < r.local AND e.fleet_grant IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("agent_backend_revision", "admin_fleet_ceiling")

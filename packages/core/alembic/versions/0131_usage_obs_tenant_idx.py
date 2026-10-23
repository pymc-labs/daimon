"""usage_obs_tenant_idx: index the per-tenant usage read.

The OpenAI daily model cap sums one tenant's usage observations for today on
every OpenAI turn; without an index on `tenant_id` that read scans the whole
table. Built concurrently, outside the migration transaction, so turns keep
writing usage meanwhile. A run cut off mid-build leaves an invalid index; a
retried run drops it and builds again.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0131_usage_obs_tenant_idx"
down_revision: str | None = "0128_fleet_run_end_notice"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "ix_usage_observation_tenant_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX},
        )
        if invalid:
            op.drop_index(INDEX, table_name="usage_observation", postgresql_concurrently=True)
        op.create_index(
            INDEX,
            "usage_observation",
            ["tenant_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX, table_name="usage_observation", if_exists=True, postgresql_concurrently=True
        )

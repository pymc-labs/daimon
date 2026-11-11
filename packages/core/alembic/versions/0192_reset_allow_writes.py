"""reset every saved "Allow writes" approvals choice to Ask

While the operator switch for approval overrides was off, a saved "Allow writes" choice
had no effect. The switch is gone and saved choices always apply, so those dormant rows
would start allowing writes at once. They are deleted at every tier and each affected
tenant's `feature_revision` is bumped. A downgrade restores nothing: an older build reads
a missing row as inheriting.

Revision ID: 0192_reset_allow_writes
Revises: 0191_drop_dev_toggle_values

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0192_reset_allow_writes"
down_revision: str | None = "0191_drop_dev_toggle_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROWS = "key = 'approvals' AND value = 'off'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE tenants SET feature_revision = feature_revision + 1 "
        f"WHERE id IN (SELECT tenant_id FROM feature_values WHERE {_ROWS})"
    )
    op.execute(f"DELETE FROM feature_values WHERE {_ROWS}")


def downgrade() -> None:
    """The deleted rows stay gone; caches are refreshed so the older build reads inheritance."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE tenants SET feature_revision = feature_revision + 1")

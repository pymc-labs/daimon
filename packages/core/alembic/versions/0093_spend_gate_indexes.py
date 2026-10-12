"""Index the per-turn spend reads.

The cap gate sums one member's `usage_events` since the start of the cap
window; `occurred_at` joins the (tenant, user) index so the window is a
range scan instead of a filter over the member's whole history. The balance
gate sums `tenant_ledger.delta_usd` per tenant; carrying it in the tenant
index lets that sum run as an index-only scan. Each new index starts with
the columns of the one it replaces, so every read that used the old one can
use the new one. The indexes are built and dropped concurrently, outside the
migration transaction, so turns keep writing both tables meanwhile.

downgrade: safe
"""

from alembic import op

revision: str = "0093_spend_gate_indexes"
down_revision: str | None = "0092_routine_post_source"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "usage_events_tenant_user_occurred_idx",
            "usage_events",
            ["tenant_id", "platform_user_id", "occurred_at"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "usage_events_user_tenant_idx",
            table_name="usage_events",
            postgresql_concurrently=True,
        )
        op.create_index(
            "tenant_ledger_tenant_delta_idx",
            "tenant_ledger",
            ["tenant_id"],
            postgresql_include=["delta_usd"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "tenant_ledger_tenant_idx", table_name="tenant_ledger", postgresql_concurrently=True
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "tenant_ledger_tenant_idx",
            "tenant_ledger",
            ["tenant_id"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "tenant_ledger_tenant_delta_idx",
            table_name="tenant_ledger",
            postgresql_concurrently=True,
        )
        op.create_index(
            "usage_events_user_tenant_idx",
            "usage_events",
            ["tenant_id", "platform_user_id"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "usage_events_tenant_user_occurred_idx",
            table_name="usage_events",
            postgresql_concurrently=True,
        )

"""Index the per-turn spend reads.

The cap gate sums one member's `usage_events` since the start of the cap
window; `occurred_at` joins the (tenant, user) index so the window is a
range scan instead of a filter over the member's whole history. The balance
gate sums `tenant_ledger.delta_usd` per tenant; carrying it in the tenant
index lets that sum run as an index-only scan. Each new index starts with
the columns of the one it replaces, so every read that used the old one can
use the new one. The indexes are built and dropped concurrently, outside the
migration transaction, so turns keep writing both tables meanwhile. A run
cut off mid-build leaves an invalid index behind, so each step first drops
what it builds: a retried run rebuilds rather than failing on it.

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0093_spend_gate_indexes"
down_revision: str | None = "0092_routine_post_source"
branch_labels: str | None = None
depends_on: str | None = None


def _replace(
    table: str, old: str, new: str, columns: Sequence[str], include: Sequence[str] = ()
) -> None:
    """Build `new` concurrently, then drop `old`; safe to re-run after a partial run."""
    op.drop_index(new, table_name=table, if_exists=True, postgresql_concurrently=True)
    op.create_index(
        new, table, list(columns), postgresql_include=list(include), postgresql_concurrently=True
    )
    op.drop_index(old, table_name=table, if_exists=True, postgresql_concurrently=True)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _replace(
            "usage_events",
            "usage_events_user_tenant_idx",
            "usage_events_tenant_user_occurred_idx",
            ["tenant_id", "platform_user_id", "occurred_at"],
        )
        _replace(
            "tenant_ledger",
            "tenant_ledger_tenant_idx",
            "tenant_ledger_tenant_delta_idx",
            ["tenant_id"],
            include=["delta_usd"],
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _replace(
            "tenant_ledger",
            "tenant_ledger_tenant_delta_idx",
            "tenant_ledger_tenant_idx",
            ["tenant_id"],
        )
        _replace(
            "usage_events",
            "usage_events_tenant_user_occurred_idx",
            "usage_events_user_tenant_idx",
            ["tenant_id", "platform_user_id"],
        )

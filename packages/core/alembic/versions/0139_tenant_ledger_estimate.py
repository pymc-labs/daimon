"""tenant_ledger_estimate: mark a debit priced from incomplete provider counts.

OpenAI's Agents API turn usage has no cache-write count, while its tariff bills
cache writes. Such a turn is debited as if it wrote nothing to the cache, an
estimate never above what the counts prove; its ledger row says so. NULL is a
fully metered debit (every existing row).

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0139_tenant_ledger_estimate"
down_revision: str | None = "0135_mcp_token_skill_bytes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("tenant_ledger", sa.Column("estimate", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_tenant_ledger_estimate",
        "tenant_ledger",
        "estimate IS NULL OR estimate IN ('cache_writes_unreported')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenant_ledger_estimate", "tenant_ledger", type_="check")
    op.drop_column("tenant_ledger", "estimate")

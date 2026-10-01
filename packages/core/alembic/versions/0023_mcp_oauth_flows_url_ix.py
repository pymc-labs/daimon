"""Partial expression index for the per-turn sign-in read on mcp_oauth_flows.

`list_completed_grants` asks, for one tenant, which completed flows match
the caller's server URLs (trailing slash ignored). The table is keyed by
OAuth `state` and indexed by `request_token` and `(tenant_id, agent_id)`,
so that read walked every flow row in the tenant on every turn. This index
is the predicate itself: tenant, normalised URL, completed rows only.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023_mcp_oauth_flows_url_ix"
down_revision: str | None = "0022_mcp_oauth_flows_completed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_mcp_oauth_flows_tenant_url_completed",
        "mcp_oauth_flows",
        ["tenant_id", sa.text("rtrim(mcp_server_url, '/')")],
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_flows_tenant_url_completed", table_name="mcp_oauth_flows")

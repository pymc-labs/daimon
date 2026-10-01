"""Index mcp_oauth_flows on (tenant_id, agent_id).

Every turn now asks which of an agent's MCP servers this caller has signed
in to, which reads the spent flow rows for one (tenant, agent). The table is
keyed by the OAuth `state` and indexed only by `request_token`, so that
question was a sequential scan on a table that grows with every connect
click.

downgrade: safe
"""

from __future__ import annotations

from alembic import op

revision: str = "0021_mcp_oauth_flows_agent_ix"
down_revision: str | None = "0020_mcp_oauth_flows"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_mcp_oauth_flows_tenant_agent", "mcp_oauth_flows", ["tenant_id", "agent_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_flows_tenant_agent", table_name="mcp_oauth_flows")

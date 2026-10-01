"""mcp_oauth_flows.completed_at — the row was exchanged for a stored grant.

`used_at` is stamped at the top of the callback, before the decline check and
before the token exchange, because it is the single-use replay gate. Somebody
who pressed "Deny" therefore looks identical to somebody who signed in, and
per-caller MCP visibility needs to tell them apart: the first must not be
handed a server they hold no credential for. `completed_at` is written only
once the grant is in the person's vault.

Existing rows are backfilled from `used_at`, but only where the request row
they hang off records `outcome = 'applied'` — the callback writes that only
after the grant is stored, so a historical decline is not resurrected as a
connection.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022_mcp_oauth_flows_completed"
down_revision: str | None = "0021_mcp_oauth_flows_agent_ix"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_oauth_flows", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE mcp_oauth_flows SET completed_at = used_at "
        "WHERE used_at IS NOT NULL AND request_token IN "
        "(SELECT token FROM credential_requests WHERE outcome = 'applied')"
    )


def downgrade() -> None:
    op.drop_column("mcp_oauth_flows", "completed_at")

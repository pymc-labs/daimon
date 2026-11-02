"""mcp_token_purpose: what each agent-kind credential was made for.

`mcp_tokens.purpose` is `coding` for a key a person made from an agent's Use from
your coding tools button (or the CLI), `report` for a published report's seam token
and `internal` for a short-lived credential (an in-session chat credential, a
provider turn's). The MCP verifier holds a `coding` key, on every request, to its
holder's current standing: a server admin by stored role, or the owner of the key's
agent. `report` and `internal` credentials are not held to that rule.

Existing agent rows are classified here: a label starting `report:` is `report`; a
lifetime of one day or less is `internal`; every other agent row is `coding`. Operator
and CLI rows keep NULL. An agent row an older process writes during a rolling deploy
has NULL too; the verifier reads that as `coding` unless it is plainly internal.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0144_mcp_token_purpose"
down_revision: str | None = "0143_agent_co_owners"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("mcp_tokens", sa.Column("purpose", sa.Text(), nullable=True))
    op.execute(
        "UPDATE mcp_tokens SET purpose = 'report' WHERE kind = 'agent' AND label LIKE 'report:%'"
    )
    op.execute(
        "UPDATE mcp_tokens SET purpose = 'internal' "
        "WHERE kind = 'agent' AND purpose IS NULL "
        "AND (surface IS NOT NULL "
        "OR (expires_at IS NOT NULL AND expires_at - created_at <= interval '1 day'))"
    )
    op.execute("UPDATE mcp_tokens SET purpose = 'coding' WHERE kind = 'agent' AND purpose IS NULL")
    op.create_check_constraint(
        "ck_mcp_tokens_purpose",
        "mcp_tokens",
        "purpose IS NULL OR purpose IN ('coding', 'report', 'internal')",
    )
    op.create_check_constraint(
        "ck_mcp_tokens_coding_purpose",
        "mcp_tokens",
        "purpose IS DISTINCT FROM 'coding' OR (kind = 'agent' AND surface IS NULL)",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_mcp_tokens_coding_purpose", "mcp_tokens", type_="check")
    op.drop_constraint("ck_mcp_tokens_purpose", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "purpose")

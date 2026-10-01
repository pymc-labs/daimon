"""Token kinds, scopes and expiry on `mcp_tokens`; token fields on audit rows.

`kind` tells an agent-scoped key (`agent`, every existing row) from an
operator token (`operator`, scoped and tied to a server admin) and a CLI
token (`cli`). Only agent keys name an agent, so `agent_id` becomes
nullable. `scopes` is what an operator token may call, read live by the
verifier so narrowing or emptying it takes effect on the next request.
`expires_at` mirrors the token's `exp` claim; existing rows carry none.
`max_issued_usd` caps the promo credit one operator token may issue and
`issued_usd` counts what it has issued so far.

Audit rows gain the token kind, its jti and the scope a call used. Rows
written before this migration have none.

Downgrading deletes operator and CLI token rows, so those tokens stop
verifying, and drops the audit columns.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0036_operator_tokens"
down_revision: str | None = "0035_channel_admins"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_tokens",
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'agent'")),
    )
    op.add_column(
        "mcp_tokens",
        sa.Column(
            "scopes",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column("mcp_tokens", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("mcp_tokens", sa.Column("max_issued_usd", sa.Numeric(12, 2)))
    op.add_column(
        "mcp_tokens",
        sa.Column("issued_usd", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
    )
    op.alter_column("mcp_tokens", "agent_id", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        "ck_mcp_tokens_kind", "mcp_tokens", "kind IN ('agent', 'operator', 'cli')"
    )
    op.create_check_constraint(
        "ck_mcp_tokens_agent_id", "mcp_tokens", "(kind = 'agent') = (agent_id IS NOT NULL)"
    )
    op.create_check_constraint(
        "ck_mcp_tokens_operator_fields",
        "mcp_tokens",
        "kind = 'operator' OR (scopes = '{}' AND max_issued_usd IS NULL)",
    )
    op.create_check_constraint(
        "ck_mcp_tokens_issued",
        "mcp_tokens",
        "issued_usd >= 0 AND (max_issued_usd IS NULL OR max_issued_usd > 0)",
    )
    op.add_column("security_audit_events", sa.Column("token_kind", sa.Text()))
    op.add_column("security_audit_events", sa.Column("token_jti", pg.UUID(as_uuid=True)))
    op.add_column("security_audit_events", sa.Column("scope", sa.Text()))


def downgrade() -> None:
    op.drop_column("security_audit_events", "scope")
    op.drop_column("security_audit_events", "token_jti")
    op.drop_column("security_audit_events", "token_kind")
    op.drop_constraint("ck_mcp_tokens_issued", "mcp_tokens", type_="check")
    op.drop_constraint("ck_mcp_tokens_operator_fields", "mcp_tokens", type_="check")
    op.drop_constraint("ck_mcp_tokens_agent_id", "mcp_tokens", type_="check")
    op.drop_constraint("ck_mcp_tokens_kind", "mcp_tokens", type_="check")
    op.execute("DELETE FROM mcp_tokens WHERE agent_id IS NULL")
    op.alter_column("mcp_tokens", "agent_id", existing_type=sa.Text(), nullable=False)
    op.drop_column("mcp_tokens", "issued_usd")
    op.drop_column("mcp_tokens", "max_issued_usd")
    op.drop_column("mcp_tokens", "expires_at")
    op.drop_column("mcp_tokens", "scopes")
    op.drop_column("mcp_tokens", "kind")

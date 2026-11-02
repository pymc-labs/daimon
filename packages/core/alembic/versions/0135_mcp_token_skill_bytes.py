"""mcp_token_skill_bytes: count one provider turn's skill calls and bytes.

A GPT or Gemini turn reads its agent's skills through Daimon MCP (`skill_list`,
`skill_read`). Its turn-lived credential counts the calls and the bytes
returned, so a loop can't spend the turn's tokens or the shared provider quota
past a fixed budget. An atomic UPDATE checks and adds in one statement, on every
instance.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0135_mcp_token_skill_bytes"
down_revision: str | None = "0134_thread_binding_owner_kind"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_tokens",
        sa.Column("skill_bytes_read", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "mcp_tokens",
        sa.Column("skill_calls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_mcp_tokens_skill_reads", "mcp_tokens", "skill_bytes_read >= 0 AND skill_calls >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_mcp_tokens_skill_reads", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "skill_calls")
    op.drop_column("mcp_tokens", "skill_bytes_read")

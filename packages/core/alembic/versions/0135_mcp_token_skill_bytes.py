"""mcp_token_skill_bytes: count the skill bytes one provider turn has read.

A GPT or Gemini turn reads its agent's skills through Daimon MCP (`skill_read`).
Its per-turn credential counts the bytes returned, so a loop of reads can't
spend the turn's tokens past a fixed budget. An atomic UPDATE checks and adds
in one statement, on every instance.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0135_mcp_token_skill_bytes"
down_revision: str | None = "0131_usage_obs_tenant_idx"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_tokens",
        sa.Column("skill_bytes_read", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_mcp_tokens_skill_bytes_read", "mcp_tokens", "skill_bytes_read >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_mcp_tokens_skill_bytes_read", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "skill_bytes_read")

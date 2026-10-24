"""mcp_token_surface: what surface a registered MCP credential runs on.

`mcp_tokens.surface` is null for every existing credential: an ordinary
chat, CLI, operator or external-agent token. `provider_turn` marks a
chat-execution credential minted for an OpenAI or Gemini turn, which has no
confirmation card, so the server refuses every confirmation-gated or
publishing tool for it. Additive only; downgrading drops the column, and
those short-lived (at most 900 s) credentials lose the server-side refusal
until they expire.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0120_mcp_token_surface"
down_revision: str | None = "0119_agent_owner_settings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("mcp_tokens", sa.Column("surface", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_mcp_tokens_surface",
        "mcp_tokens",
        "surface IS NULL OR (surface = 'provider_turn' AND kind = 'agent')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_mcp_tokens_surface", "mcp_tokens", type_="check")
    op.drop_column("mcp_tokens", "surface")

"""coding_key_claims: single-use codes that trade for a coding key, and the key's tool

Revision ID: 0184_coding_key_claims
Revises: 0183_helper_files

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0184_coding_key_claims"
down_revision: str | None = "0183_helper_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_tokens", sa.Column("client", sa.Text(), nullable=True))
    op.create_table(
        "coding_key_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("batch", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "jti",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_tokens.jti", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_coding_key_claims_batch", "coding_key_claims", ["batch"])


def downgrade() -> None:
    op.drop_index("ix_coding_key_claims_batch", table_name="coding_key_claims")
    op.drop_table("coding_key_claims")
    op.drop_column("mcp_tokens", "client")

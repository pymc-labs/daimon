"""github_router_sessions, and the router session each issued token belongs to

A session whose GitHub grants need more than one installation token reaches
GitHub's MCP server through Daimon's router. The router authenticates the
session by an opaque bearer whose SHA-256 is stored here, and picks a token
from the issued-token rows stamped with the same router session.

Revision ID: 0174a_github_router_sessions
Revises: 0174_drop_channel_admins

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0174a_github_router_sessions"
down_revision: str | None = "0174_drop_channel_admins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_router_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ma_session_id", sa.Text(), nullable=True),
        sa.Column("credential_sha256", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_github_router_sessions_open_ma_session",
        "github_router_sessions",
        ["ma_session_id"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "ix_github_router_sessions_open_account",
        "github_router_sessions",
        ["account_id"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.add_column(
        "github_issued_tokens",
        sa.Column(
            "router_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("github_router_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_github_issued_tokens_router_session",
        "github_issued_tokens",
        ["router_session_id"],
        postgresql_where=sa.text("router_session_id IS NOT NULL AND status = 'delivered'"),
    )


def downgrade() -> None:
    op.drop_index("ix_github_issued_tokens_router_session", table_name="github_issued_tokens")
    op.drop_column("github_issued_tokens", "router_session_id")
    op.drop_index("ix_github_router_sessions_open_account", table_name="github_router_sessions")
    op.drop_index("ix_github_router_sessions_open_ma_session", table_name="github_router_sessions")
    op.drop_table("github_router_sessions")

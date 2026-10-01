"""mcp_oauth_flows — in-flight MCP OAuth authorizations, plus the mcp_oauth request kind.

A member connecting their own account to an OAuth-only MCP server (Notion,
Slack, …) goes through the browser; the row keyed by the OAuth `state` holds
the PKCE verifier and the dynamically registered client across the redirect.
It cascades from its `credential_requests` row, so the existing
platform-user erasure covers it. The request kind CHECK gains `mcp_oauth`
for the card that starts the flow.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0020_mcp_oauth_flows"
down_revision: str | None = "0019_skill_repo_credentials"
branch_labels: str | None = None
depends_on: str | None = None

_KINDS_BEFORE = "kind IN ('env', 'env_file', 'mcp', 'repo', 'skill_repo')"
_KINDS_AFTER = "kind IN ('env', 'env_file', 'mcp', 'mcp_oauth', 'repo', 'skill_repo')"


def upgrade() -> None:
    op.drop_constraint("ck_credential_requests_kind", "credential_requests", type_="check")
    op.create_check_constraint("ck_credential_requests_kind", "credential_requests", _KINDS_AFTER)
    op.create_table(
        "mcp_oauth_flows",
        sa.Column("state", sa.Text(), primary_key=True),
        sa.Column(
            "request_token",
            sa.Text(),
            sa.ForeignKey("credential_requests.token", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("server_name", sa.Text(), nullable=False),
        sa.Column("mcp_server_url", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=True),
        sa.Column("authorization_endpoint", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mcp_oauth_flows_request_token", "mcp_oauth_flows", ["request_token"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_flows_request_token", table_name="mcp_oauth_flows")
    op.drop_table("mcp_oauth_flows")
    # Rows of the new kind cannot satisfy the old CHECK; they are short-lived
    # handshake rows, so dropping them is the price of the narrower constraint.
    op.execute("DELETE FROM credential_requests WHERE kind = 'mcp_oauth'")
    op.drop_constraint("ck_credential_requests_kind", "credential_requests", type_="check")
    op.create_check_constraint("ck_credential_requests_kind", "credential_requests", _KINDS_BEFORE)

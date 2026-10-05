"""Add tenant-scoped GitHub authorization and issued-token inventory.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_github_access_foundation"
down_revision: str | None = "0047_turn_origin_external"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("github_app_installations", sa.Column("account_id", sa.BigInteger()))
    op.add_column("github_app_installations", sa.Column("account_type", sa.Text()))
    op.add_column("github_app_installations", sa.Column("repository_selection", sa.Text()))
    op.add_column("github_app_installations", sa.Column("permissions", postgresql.JSONB()))
    op.add_column("github_app_installations", sa.Column("suspended_at", sa.DateTime(timezone=True)))
    op.add_column(
        "security_audit_events", sa.Column("github_token_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column("security_audit_events", sa.Column("github_session_id", sa.Text()))
    op.add_column("security_audit_events", sa.Column("github_installation_id", sa.BigInteger()))
    op.add_column(
        "security_audit_events", sa.Column("github_repo_ids", postgresql.ARRAY(sa.BigInteger()))
    )
    op.add_column("security_audit_events", sa.Column("github_permissions", postgresql.JSONB()))
    op.add_column(
        "security_audit_events", sa.Column("github_expires_at", sa.DateTime(timezone=True))
    )
    op.execute(
        """CREATE TABLE tenant_github_repos (
    tenant_id UUID NOT NULL,
    repo_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    repo_full_name TEXT NOT NULL,
    max_access TEXT NOT NULL,
    authorized_by_github_user_id BIGINT NOT NULL,
    authorized_by_account_id UUID,
    authorized_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    status TEXT DEFAULT 'active' NOT NULL,
    status_reason TEXT,
    version INTEGER DEFAULT '1' NOT NULL,
    PRIMARY KEY (tenant_id, repo_id),
    CHECK (max_access IN ('read', 'write')),
    CHECK (status IN ('active', 'suspended', 'revoked')),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    FOREIGN KEY(authorized_by_account_id) REFERENCES accounts (id) ON DELETE SET NULL
);"""
    )
    op.execute(
        """CREATE TABLE agent_github_grants (
    tenant_id UUID NOT NULL,
    agent_id UUID NOT NULL,
    repo_id BIGINT NOT NULL,
    baseline_access TEXT NOT NULL,
    ceiling_access TEXT NOT NULL,
    staged BOOLEAN DEFAULT true NOT NULL,
    mount_path TEXT,
    is_working_repo BOOLEAN DEFAULT false NOT NULL,
    granted_by_account_id UUID,
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, repo_id),
    FOREIGN KEY(tenant_id, repo_id)
        REFERENCES tenant_github_repos (tenant_id, repo_id) ON DELETE CASCADE,
    CHECK (baseline_access IN ('none', 'read', 'write')),
    CHECK (ceiling_access IN ('read', 'write')),
    CHECK (baseline_access = 'none' OR baseline_access = 'read' OR ceiling_access = 'write'),
    FOREIGN KEY(granted_by_account_id) REFERENCES accounts (id) ON DELETE SET NULL
);"""
    )
    op.execute(
        """CREATE TABLE agent_github_mode (
    tenant_id UUID NOT NULL,
    agent_id UUID NOT NULL,
    mode TEXT DEFAULT 'legacy' NOT NULL,
    PRIMARY KEY (tenant_id, agent_id),
    CHECK (mode IN ('legacy', 'app')),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);"""
    )
    op.execute(
        """CREATE TABLE github_user_links (
    github_user_id BIGINT NOT NULL,
    login TEXT NOT NULL,
    encrypted_access_token BYTEA NOT NULL,
    encrypted_refresh_token BYTEA,
    access_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    refresh_expires_at TIMESTAMP WITH TIME ZONE,
    token_generation INTEGER DEFAULT '1' NOT NULL,
    link_generation INTEGER DEFAULT '1' NOT NULL,
    status TEXT DEFAULT 'active' NOT NULL,
    linked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (github_user_id),
    CHECK (status IN ('active', 'broken'))
);"""
    )
    op.execute(
        """CREATE TABLE account_github_links (
    account_id UUID NOT NULL,
    github_user_id BIGINT NOT NULL,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    verified_via TEXT NOT NULL,
    linked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (account_id),
    CHECK (verified_via IN ('discord_oauth', 'slack_oidc', 'auto_attach_discord')),
    FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY(github_user_id) REFERENCES github_user_links (github_user_id) ON DELETE CASCADE
);"""
    )
    op.execute(
        """CREATE TABLE github_issued_tokens (
    token_id UUID DEFAULT gen_random_uuid() NOT NULL,
    tenant_id UUID NOT NULL,
    agent_id UUID NOT NULL,
    session_id TEXT NOT NULL,
    installation_id BIGINT NOT NULL,
    repo_ids BIGINT[] NOT NULL,
    permissions JSONB NOT NULL,
    grant_versions JSONB NOT NULL,
    requester_account_id UUID,
    link_generation INTEGER,
    encrypted_token BYTEA,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status TEXT DEFAULT 'pending' NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoke_attempts INTEGER DEFAULT '0' NOT NULL,
    PRIMARY KEY (token_id),
    CHECK (status IN ('pending', 'stored', 'delivered', 'revoked')),
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
    FOREIGN KEY(requester_account_id) REFERENCES accounts (id) ON DELETE SET NULL
);"""
    )


def downgrade() -> None:
    op.drop_table("github_issued_tokens")
    op.drop_table("account_github_links")
    op.drop_table("github_user_links")
    op.drop_table("agent_github_mode")
    op.drop_table("agent_github_grants")
    op.drop_table("tenant_github_repos")
    op.drop_column("security_audit_events", "github_expires_at")
    op.drop_column("security_audit_events", "github_permissions")
    op.drop_column("security_audit_events", "github_repo_ids")
    op.drop_column("security_audit_events", "github_installation_id")
    op.drop_column("security_audit_events", "github_session_id")
    op.drop_column("security_audit_events", "github_token_id")
    op.drop_column("github_app_installations", "suspended_at")
    op.drop_column("github_app_installations", "permissions")
    op.drop_column("github_app_installations", "repository_selection")
    op.drop_column("github_app_installations", "account_type")
    op.drop_column("github_app_installations", "account_id")

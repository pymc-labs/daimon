"""Tenant-scoped delegated Daimon admins and their audit targets.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op

revision = "0148_daimon_admins"
down_revision = "0147_long_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""CREATE TABLE daimon_admins (
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        platform TEXT NOT NULL CHECK (platform IN ('discord', 'slack', 'teams')),
        subject_kind TEXT NOT NULL CHECK (subject_kind IN ('user', 'role')),
        subject_id TEXT NOT NULL CHECK (length(subject_id) > 0),
        granted_by TEXT,
        granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, platform, subject_kind, subject_id),
        CHECK (subject_kind = 'user' OR platform = 'discord')
    )""")
    op.add_column(
        "security_audit_events", sa.Column("admin_subject_kind", sa.Text(), nullable=True)
    )
    op.add_column("security_audit_events", sa.Column("admin_subject_id", sa.Text(), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("Delegation audit history must be retained")

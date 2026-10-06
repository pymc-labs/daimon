"""Record grant and authorization versions on GitHub token audit rows.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0051_github_token_audit_versions"
down_revision: str | None = "0050_github_new_repo_notices"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("security_audit_events", sa.Column("github_grant_versions", JSONB()))
    op.add_column("security_audit_events", sa.Column("github_turn_origin_id", sa.UUID()))


def downgrade() -> None:
    op.drop_column("security_audit_events", "github_turn_origin_id")
    op.drop_column("security_audit_events", "github_grant_versions")

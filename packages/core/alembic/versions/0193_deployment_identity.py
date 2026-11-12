"""deployment_identity: this install's own id for naming hosted provider environments

One row, stamped on first need where no cloud names the install (GCP's project id
does). It stops two installs that share an OpenAI project from adopting each
other's per-tenant environments. A downgrade drops the table; the next upgrade
stamps a new id, so environments named under the old one are not adopted again.

Revision ID: 0193_deployment_identity
Revises: 0192_reset_allow_writes

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0193_deployment_identity"
down_revision: str | None = "0192_reset_allow_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_identity",
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton", name="ck_deployment_identity_singleton"),
        sa.PrimaryKeyConstraint("singleton"),
    )


def downgrade() -> None:
    op.drop_table("deployment_identity")

"""Evals: the map from a real value's keyed hash to its fake, per tenant.

One new tenant-scoped table. It holds no real value: only an HMAC of it under the deployment's
scrub key and the fake that replaced it. Additive only.

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0225_eval_scrub_entities"
down_revision: str | None = "0224_evals_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "eval_scrub_entities",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("fake", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "kind", "key_hash"),
    )


def downgrade() -> None:
    op.drop_table("eval_scrub_entities")

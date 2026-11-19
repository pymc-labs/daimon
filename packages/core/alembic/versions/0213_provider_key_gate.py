"""provider_key_gate: per-key rate-limit waits and running-turn leases.

`provider_key_cooldowns` records when a provider API key, named by a digest of
it, may serve a model again after a 429. `provider_key_leases` holds one row per
running turn so every replica counts the turns on a key and per workspace.
Leases expire on their own. Downgrading drops both tables; they hold only
short-lived state.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0213_provider_key_gate"
down_revision: str | None = "0212_outbox_attribution"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "provider_key_cooldowns",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider", "key_digest", "model"),
    )
    op.create_table(
        "provider_key_leases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_provider_key_leases_tenants", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_key_leases_key",
        "provider_key_leases",
        ["provider", "key_digest", "tenant_id"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("provider_key_leases")
    op.drop_table("provider_key_cooldowns")

"""Retained alternate-backend agent revisions, skill bundles with upload ownership.

downgrade: destructive

Only the opt-in catalog rows are removed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0079_neutral_catalog"
down_revision: str | None = "0078_usage_observation_revision"
branch_labels: str | None = None
depends_on: str | None = None


def _scope_columns(*, foreign_keys: bool) -> list[sa.Column]:
    return [
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            *([sa.ForeignKey("tenants.id", ondelete="CASCADE")] if foreign_keys else []),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            *([sa.ForeignKey("accounts.id", ondelete="CASCADE")] if foreign_keys else []),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_scope_id", sa.Text(), nullable=False),
    ]


def upgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    scope = ("tenant_id", "account_id", "provider", "account_scope_id")
    op.create_table(
        "neutral_agent_revisions",
        *_scope_columns(foreign_keys=True),
        sa.Column("catalog_id", sa.Text(), nullable=False),
        sa.Column("local_revision", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("agent", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(*scope, "catalog_id", "local_revision"),
        sa.CheckConstraint("provider IN ('openai', 'gemini')", name="ck_neutral_agent_provider"),
        sa.CheckConstraint("local_revision > 0", name="ck_neutral_agent_revision"),
    )
    op.create_table(
        "neutral_skill_versions",
        *_scope_columns(foreign_keys=True),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("zip_bytes", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint(*scope, "skill_id", "version"),
        sa.CheckConstraint("provider IN ('openai', 'gemini')", name="ck_neutral_skill_provider"),
    )


def downgrade() -> None:
    op.drop_table("neutral_skill_versions")
    op.drop_table("neutral_agent_revisions")

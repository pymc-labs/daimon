"""Agent backend preferences in the existing configuration namespace.

downgrade: destructive

Preference history is removed; existing channel revisions remain intact.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0091_agent_backend_settings"
down_revision = "0089_neutral_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "agent_backend_revision",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local", sa.Integer(), nullable=False),
        sa.Column("backend", sa.Text()),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("preview_digest", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("previous_local", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "local"),
        sa.CheckConstraint("local > 0", name="ck_agent_backend_local"),
        sa.CheckConstraint(
            "backend IS NULL OR backend IN ('anthropic','openai','gemini')",
            name="ck_agent_backend_provider",
        ),
    )
    op.add_column(
        "tenant_access_policies",
        sa.Column(
            "enabled_backends",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"anthropic\"]'::jsonb"),
        ),
    )
    op.add_column(
        "channel_config_revision",
        sa.Column("origin", sa.Text(), nullable=False, server_default="channel"),
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM channel_config_revision WHERE origin = 'agent'"))
    op.drop_column("channel_config_revision", "origin")
    op.drop_table("agent_backend_revision")
    op.drop_column("tenant_access_policies", "enabled_backends")

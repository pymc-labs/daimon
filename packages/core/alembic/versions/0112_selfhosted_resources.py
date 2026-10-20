"""Persist tenant-owned self-hosted agents, environments and operation receipts.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0112_selfhosted_resources"
down_revision = "0111_turn_origin_foreign_sender"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in ("selfhosted_agents", "selfhosted_environments"):
        op.create_table(
            name,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("spec", postgresql.JSONB(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            f"uq_{name}_active_name",
            name,
            ["tenant_id", "name"],
            unique=True,
            postgresql_where=sa.text("archived_at IS NULL"),
        )
    op.create_table(
        "selfhosted_resource_operations",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("account_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '7 days'"),
        ),
    )

    op.create_index(
        "ix_selfhosted_resource_operations_expires_at",
        "selfhosted_resource_operations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("selfhosted_resource_operations")
    op.drop_table("selfhosted_environments")
    op.drop_table("selfhosted_agents")

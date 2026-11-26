"""Eval fake world: every call an item made, its overlay state and the simulator cache.

Three new tables, `eval_run_items.world_token_sha256` (the hash of the bearer token a sandbox
gateway uses to reach the item's world) and `mcp_tokens.eval_item_id` (the item a credential
answers for). Additive only.

downgrade: destructive
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "0236_eval_world"
down_revision: str | None = "0235_eval_scrub_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("eval_run_items", sa.Column("world_token_sha256", sa.Text(), nullable=True))
    op.create_index(
        "uq_eval_run_items_world_token",
        "eval_run_items",
        ["world_token_sha256"],
        unique=True,
        postgresql_where=sa.text("world_token_sha256 IS NOT NULL"),
    )
    op.add_column("mcp_tokens", sa.Column("eval_item_id", sa.UUID(), nullable=True))
    op.create_table(
        "eval_world_calls",
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("exchange_id", sa.Text(), nullable=True),
        sa.Column("sim_key", sa.Text(), nullable=True),
        sa.Column("model_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        _created(),
        sa.ForeignKeyConstraint(["item_id"], ["eval_run_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "seq"),
    )
    op.create_table(
        "eval_world_overlay",
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=Text()), nullable=True),
        _created(),
        sa.ForeignKeyConstraint(["item_id"], ["eval_run_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "service", "resource_key"),
    )
    op.create_table(
        "eval_sim_cache",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("case_sha256", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        _created(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "case_sha256", "key"),
    )


def downgrade() -> None:
    for table in ("eval_sim_cache", "eval_world_overlay", "eval_world_calls"):
        op.drop_table(table)
    op.drop_column("mcp_tokens", "eval_item_id")
    op.drop_index("uq_eval_run_items_world_token", table_name="eval_run_items")
    op.drop_column("eval_run_items", "world_token_sha256")

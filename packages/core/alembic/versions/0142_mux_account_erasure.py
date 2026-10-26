"""Mux account erasure: settlement rows outlive their slot, and erased accounts are fenced.

Account deletion now erases a person's private provider slots (bindings, journal,
leases, operations). Two things have to survive that:

* `usage_observation` and `accounting_outbox` rows are financial evidence. They stop
  cascading from `provider_binding_slot` (their tenant foreign key still removes them
  with the tenant), and `accounting_binding_context` keeps the profile and channel the
  host needs to settle them.
* `erased_account` records which accounts were erased, so a writer that held a stale
  scope or lease cannot write for one afterwards.

Upgrade only adds two tables and drops two foreign keys; it rewrites no rows.
The downgrade drops both tables and puts the two foreign keys back NOT VALID, so rows
whose slot was already erased stay and only new rows are checked.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0142_mux_account_erasure"
down_revision: str | None = "0141_remove_fleet"
branch_labels: str | None = None
depends_on: str | None = None

_DETACHED = ("usage_observation", "accounting_outbox")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "accounting_binding_context",
        sa.Column("binding_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("profiles", postgresql.ARRAY(sa.Text()), nullable=False),
    )
    op.create_table(
        "erased_account",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "erased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for table in _DETACHED:
        op.drop_constraint(f"{table}_binding_id_fkey", table, type_="foreignkey")


def downgrade() -> None:
    for table in _DETACHED:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_binding_id_fkey "
            "FOREIGN KEY (binding_id) REFERENCES provider_binding_slot (binding_id) "
            "ON DELETE CASCADE NOT VALID"
        )
    op.drop_table("erased_account")
    op.drop_table("accounting_binding_context")

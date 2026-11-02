"""usage_agent_allowances: spend attributed to agents, and spend allowances.

`usage_events.agent_id` is the agent whose turn or tool call spent the money, so
a person's or a workspace's spend can be read by agent. Only rows written after
this migration carry one. The index is built concurrently, outside the migration
transaction, so turns keep writing usage meanwhile; a run cut off mid-build
leaves an invalid index, which a retried run drops and builds again.

`spend_allowances` holds what a person, a group or an invite may spend, per person,
in billed dollars, optionally on one agent. Downgrading drops the allowances.

`tenant_ledger.actor_account_id` records who made a manual credit from the web. The
foreign key is added NOT VALID: every existing row is NULL, so nothing needs scanning
and only new rows are checked.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0150_usage_agent_allowances"
down_revision: str | None = "0149_web_chat"
branch_labels: str | None = None
depends_on: str | None = None

USAGE_AGENT_INDEX = "usage_events_tenant_agent_occurred_idx"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("usage_events", sa.Column("agent_id", sa.Text(), nullable=True))
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": USAGE_AGENT_INDEX},
        )
        if invalid:
            op.drop_index(
                USAGE_AGENT_INDEX, table_name="usage_events", postgresql_concurrently=True
            )
        op.create_index(
            USAGE_AGENT_INDEX,
            "usage_events",
            ["tenant_id", "agent_id", "occurred_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
    op.add_column(
        "tenant_ledger",
        sa.Column("actor_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        "ALTER TABLE tenant_ledger ADD CONSTRAINT fk_tenant_ledger_accounts "
        "FOREIGN KEY (actor_account_id) REFERENCES accounts (id) ON DELETE SET NULL NOT VALID"
    )
    op.create_table(
        "spend_allowances",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("amount_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_spend_allowances"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_spend_allowances_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_spend_allowances_accounts",
        ),
        sa.CheckConstraint(
            "subject_kind IN ('person', 'group', 'invite')", name="ck_spend_allowances_subject"
        ),
        sa.CheckConstraint("\"window\" IN ('month', 'total')", name="ck_spend_allowances_window"),
        sa.CheckConstraint("amount_usd >= 0", name="ck_spend_allowances_amount"),
    )
    op.create_index(
        "ix_spend_allowances_tenant_subject",
        "spend_allowances",
        ["tenant_id", "subject_kind", "subject_id"],
    )


def downgrade() -> None:
    op.drop_table("spend_allowances")
    op.drop_constraint("fk_tenant_ledger_accounts", "tenant_ledger", type_="foreignkey")
    op.drop_column("tenant_ledger", "actor_account_id")
    with op.get_context().autocommit_block():
        op.drop_index(
            USAGE_AGENT_INDEX,
            table_name="usage_events",
            if_exists=True,
            postgresql_concurrently=True,
        )
    op.drop_column("usage_events", "agent_id")

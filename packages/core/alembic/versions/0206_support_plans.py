"""support_plans: credit plans, option catalogs and a settled ledger for Ask a person.

`support_catalogs` holds each tenant's option menus, `support_plans` a pool or
per-person cap on a workspace, agent or channel, and `support_allowances` one
person's own cap. `support_escalations` gains the ledger columns: which ledger
a row is charged to, its plan, the option snapshot and answers, the hold and
charge, the settle status and the support post. Server defaults make existing
rows, and rows written by older pods, default-ledger rows charged 1, so the
default sum equals the old count. Downgrading drops the tables and columns,
losing every plan, catalog, allowance and settlement.

downgrade: destructive
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0206_support_plans"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None

_WINDOW_BOUNDS = (
    "(\"window\" = 'fixed' AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
    "AND starts_at < ends_at) "
    "OR (\"window\" = 'total' AND ends_at IS NULL) "
    "OR (\"window\" = 'monthly' AND starts_at IS NULL AND ends_at IS NULL)"
)


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "tenant_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _audit_columns() -> list[sa.Column[uuid.UUID] | sa.Column[datetime]]:
    return [
        sa.Column(
            "set_by_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "support_catalogs",
        _id(),
        _tenant(),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("options", JSONB(), nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("tenant_id", "key", name="uq_support_catalogs_tenant_key"),
        sa.CheckConstraint("key ~ '^[a-z0-9-]{1,32}$'", name="ck_support_catalogs_key"),
        sa.CheckConstraint("revision >= 1", name="ck_support_catalogs_revision"),
    )
    op.create_table(
        "support_plans",
        _id(),
        _tenant(),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column(
            "catalog_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_catalogs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("pool", sa.Numeric(10, 2), nullable=True),
        sa.Column("per_person", sa.Numeric(10, 2), nullable=True),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.UniqueConstraint("tenant_id", "scope", "scope_id", name="uq_support_plans_scope"),
        sa.CheckConstraint(
            "scope IN ('workspace', 'agent', 'channel')", name="ck_support_plans_scope"
        ),
        sa.CheckConstraint(
            "(scope = 'workspace') = (scope_id = '')", name="ck_support_plans_scope_id"
        ),
        sa.CheckConstraint(
            "(pool IS NOT NULL OR per_person IS NOT NULL) AND pool >= 0 AND per_person >= 0",
            name="ck_support_plans_limits",
        ),
        sa.CheckConstraint(
            "\"window\" IN ('monthly', 'total', 'fixed')", name="ck_support_plans_window"
        ),
        sa.CheckConstraint(_WINDOW_BOUNDS, name="ck_support_plans_bounds"),
    )
    op.create_table(
        "support_allowances",
        _id(),
        _tenant(),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_plans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("credits", sa.Numeric(10, 2), nullable=False),
        *_audit_columns(),
        sa.CheckConstraint("credits >= 0", name="ck_support_allowances_credits"),
    )
    op.create_index(
        "uq_support_allowances_plan_user",
        "support_allowances",
        ["tenant_id", "plan_id", "platform_user_id"],
        unique=True,
        postgresql_where=sa.text("plan_id IS NOT NULL"),
    )
    op.create_index(
        "uq_support_allowances_default_user",
        "support_allowances",
        ["tenant_id", "platform_user_id"],
        unique=True,
        postgresql_where=sa.text("plan_id IS NULL"),
    )
    _upgrade_escalations()


def _upgrade_escalations() -> None:
    table = "support_escalations"
    columns = [
        sa.Column("charged_to", sa.Text(), nullable=False, server_default="default"),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("option_key", sa.Text(), nullable=True),
        sa.Column("option", JSONB(), nullable=True),
        sa.Column("answers", JSONB(), nullable=True),
        sa.Column("hold", sa.Numeric(10, 2), nullable=False, server_default="1"),
        sa.Column("charge", sa.Numeric(10, 2), nullable=True, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="settled"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_by", sa.Text(), nullable=True),
        sa.Column(
            "settled_by_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("support_channel_id", sa.Text(), nullable=True),
        sa.Column("support_post_id", sa.Text(), nullable=True),
    ]
    for column in columns:
        op.add_column(table, column)
    checks = {
        "ck_support_escalations_charged_to": "charged_to IN ('default', 'plan', 'exempt')",
        "ck_support_escalations_default": "charged_to <> 'default' OR plan_id IS NULL",
        "ck_support_escalations_status": "status IN ('pending', 'settled', 'declined')",
        "ck_support_escalations_pending": "(status = 'pending') = (charge IS NULL)",
        "ck_support_escalations_amounts": "hold >= 0 AND (charge IS NULL OR charge >= 0)",
    }
    for name, condition in checks.items():
        op.create_check_constraint(name, table, condition)
    op.create_index("support_escalations_plan_idx", table, ["tenant_id", "plan_id", "created_at"])
    op.create_index(
        "support_escalations_pending_idx",
        table,
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    table = "support_escalations"
    op.drop_index("support_escalations_pending_idx", table_name=table)
    op.drop_index("support_escalations_plan_idx", table_name=table)
    for name in (
        "ck_support_escalations_amounts",
        "ck_support_escalations_pending",
        "ck_support_escalations_status",
        "ck_support_escalations_default",
        "ck_support_escalations_charged_to",
    ):
        op.drop_constraint(name, table, type_="check")
    for column in (
        "support_post_id",
        "support_channel_id",
        "settled_by_account_id",
        "settled_by",
        "settled_at",
        "status",
        "charge",
        "hold",
        "answers",
        "option",
        "option_key",
        "plan_id",
        "charged_to",
    ):
        op.drop_column(table, column)
    op.drop_table("support_allowances")
    op.drop_table("support_plans")
    op.drop_table("support_catalogs")

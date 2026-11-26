"""support_plans: limits, menus, a request channel and a settled ledger for Ask a person.

`support_limits` holds credits for a workspace, an agent or a channel, and a
person's own limit at one of them (scope workspace: everywhere).
`support_menus` holds each tenant's option menus and `support_menu_places`
which menu a place uses. `support_channels` is a workspace's own request
channel. `support_escalations` gains the ledger columns: which ledger a row is
charged to, its limit and the person's own limit that capped it, the option
snapshot and answers, the hold and charge,
the settle status and the support post. Server defaults make existing rows,
and rows written by older pods, default-ledger rows charged 1, so the default
sum equals the old count. Downgrading drops the tables and columns, losing
every limit, menu, channel and settlement, and deletes the requests that did
not count as default credits (limit, exempt, declined or pending).

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

_SCOPES = "scope IN ('workspace', 'agent', 'channel')"
_SCOPE_ID = "(scope = 'workspace') = (scope_id = '')"


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant(*, primary_key: bool = False) -> sa.Column[uuid.UUID]:
    return sa.Column(
        "tenant_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


def _revision() -> sa.Column[int]:
    return sa.Column(
        "revision",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("nextval('support_revision_seq')"),
    )


def _set_by() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "set_by_account_id",
        UUID(as_uuid=True),
        sa.ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )


def _times() -> list[sa.Column[datetime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # One counter for menus, limits and channels: a deleted and recreated row never
    # repeats a revision an open form still carries.
    op.execute("CREATE SEQUENCE support_revision_seq")
    op.create_table(
        "support_menus",
        _id(),
        _tenant(),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _revision(),
        sa.Column("options", JSONB(), nullable=False),
        _set_by(),
        *_times(),
        sa.UniqueConstraint("tenant_id", "key", name="uq_support_menus_tenant_key"),
        sa.CheckConstraint("key ~ '^[a-z0-9-]{1,32}$'", name="ck_support_menus_key"),
    )
    op.create_table(
        "support_menu_places",
        _tenant(primary_key=True),
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("scope_id", sa.Text(), primary_key=True),
        sa.Column(
            "menu_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_menus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _set_by(),
        *_times(),
        sa.CheckConstraint(_SCOPES, name="ck_support_menu_places_scope"),
        sa.CheckConstraint(_SCOPE_ID, name="ck_support_menu_places_scope_id"),
    )
    op.create_table(
        "support_limits",
        _id(),
        _tenant(),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=True),
        sa.Column("shared", sa.Numeric(10, 2), nullable=True),
        sa.Column("each_person", sa.Numeric(10, 2), nullable=True),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        _revision(),
        _set_by(),
        *_times(),
        sa.CheckConstraint(_SCOPES, name="ck_support_limits_scope"),
        sa.CheckConstraint(_SCOPE_ID, name="ck_support_limits_scope_id"),
        sa.CheckConstraint(
            "(platform_user_id IS NULL AND (shared IS NOT NULL OR each_person IS NOT NULL)) "
            "OR (platform_user_id IS NOT NULL AND shared IS NULL AND each_person IS NOT NULL)",
            name="ck_support_limits_amounts",
        ),
        sa.CheckConstraint("shared >= 0 AND each_person >= 0", name="ck_support_limits_positive"),
        sa.CheckConstraint("\"window\" IN ('total', 'monthly')", name="ck_support_limits_window"),
        sa.CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at",
            name="ck_support_limits_dates",
        ),
    )
    op.create_index(
        "uq_support_limits_place",
        "support_limits",
        ["tenant_id", "scope", "scope_id"],
        unique=True,
        postgresql_where=sa.text("platform_user_id IS NULL"),
    )
    op.create_index(
        "uq_support_limits_person",
        "support_limits",
        ["tenant_id", "scope", "scope_id", "platform_user_id"],
        unique=True,
        postgresql_where=sa.text("platform_user_id IS NOT NULL"),
    )
    op.create_table(
        "support_channels",
        _tenant(primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("slack_team_id", sa.Text(), nullable=True),
        sa.Column("teams_team_id", sa.Text(), nullable=True),
        sa.Column("teams_service_url", sa.Text(), nullable=True),
        _revision(),
        _set_by(),
        *_times(),
    )
    _upgrade_escalations()


def _upgrade_escalations() -> None:
    table = "support_escalations"
    columns = [
        sa.Column("charged_to", sa.Text(), nullable=False, server_default="default"),
        sa.Column(
            "limit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_limits.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "person_limit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("support_limits.id", ondelete="SET NULL"),
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
        "ck_support_escalations_charged_to": "charged_to IN ('default', 'limit', 'exempt')",
        "ck_support_escalations_default": "charged_to <> 'default' OR limit_id IS NULL",
        "ck_support_escalations_status": "status IN ('pending', 'settled', 'declined')",
        "ck_support_escalations_pending": "(status = 'pending') = (charge IS NULL)",
        "ck_support_escalations_amounts": "hold >= 0 AND (charge IS NULL OR charge >= 0)",
    }
    for name, condition in checks.items():
        op.create_check_constraint(name, table, condition)
    op.create_index("support_escalations_limit_idx", table, ["tenant_id", "limit_id", "created_at"])
    op.create_index(
        "support_escalations_pending_idx",
        table,
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    table = "support_escalations"
    # The old ledger counts every row as one default credit. Limit, exempt, declined
    # and pending rows would be charged to the person's default allowance.
    op.execute(f"DELETE FROM {table} WHERE charged_to <> 'default' OR status <> 'settled'")
    op.drop_index("support_escalations_pending_idx", table_name=table)
    op.drop_index("support_escalations_limit_idx", table_name=table)
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
        "person_limit_id",
        "limit_id",
        "charged_to",
    ):
        op.drop_column(table, column)
    op.drop_table("support_channels")
    op.drop_table("support_limits")
    op.drop_table("support_menu_places")
    op.drop_table("support_menus")
    op.execute("DROP SEQUENCE support_revision_seq")

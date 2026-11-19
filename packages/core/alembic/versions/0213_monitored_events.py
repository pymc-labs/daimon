"""monitored_events: what people ask a monitored agent and what it answers.

One row per question, answer, MCP tool call or notice the person was shown, kept while the agent's
`monitor_direct_messages` or `monitor_mcp` feature is on, deleted with the person's
account, and pruned after `DAIMON_MONITORING__RETENTION_DAYS`. A trigger refuses a row
for an erased account (`daimon.core._erasure_guards`). Downgrading drops every row.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0213_monitored_events"
down_revision: str | None = "0212_sandbox_tools_feature"
branch_labels: str | None = None
depends_on: str | None = None

# A frozen copy of `daimon.core._erasure_guards.MONITORING_INSTALL`, which a test keeps equal.
GUARD_STATEMENTS: tuple[str, ...] = (
    """CREATE OR REPLACE FUNCTION mux_reject_erased_account() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    IF mux_account_erased(NEW.account_id::text) THEN
        RAISE EXCEPTION 'the account was erased';
    END IF;
    RETURN NEW;
END
$guard$""",
    """CREATE OR REPLACE TRIGGER monitored_events_erased_account_guard
    BEFORE INSERT ON monitored_events
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_account()""",
)
GUARD_REMOVAL: tuple[str, ...] = (
    """DROP TRIGGER IF EXISTS monitored_events_erased_account_guard
    ON monitored_events""",
    "DROP FUNCTION IF EXISTS mux_reject_erased_account()",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "monitored_events",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation", sa.Text(), nullable=True),
        sa.Column("turn_ref", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitored_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_monitored_events_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
            name="fk_monitored_events_accounts",
        ),
        sa.CheckConstraint(
            "platform IN ('discord', 'slack', 'teams', 'web', 'mcp')",
            name="ck_monitored_events_platform",
        ),
        sa.CheckConstraint(
            "kind IN ('question', 'answer', 'tool', 'notice')", name="ck_monitored_events_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'tool') = (tool_name IS NOT NULL)", name="ck_monitored_events_tool_name"
        ),
    )
    op.create_index(
        "ix_monitored_events_agent_created",
        "monitored_events",
        ["tenant_id", "agent_id", "created_at"],
    )
    op.create_index("ix_monitored_events_created", "monitored_events", ["created_at"])
    op.create_index("ix_monitored_events_account", "monitored_events", ["account_id"])
    op.create_index(
        "ix_monitored_events_notice",
        "monitored_events",
        ["tenant_id", "agent_id", "created_at"],
        postgresql_where=sa.text("kind = 'notice'"),
    )
    op.create_index(
        "uq_monitored_events_turn",
        "monitored_events",
        ["tenant_id", "conversation", "turn_ref", "kind"],
        unique=True,
        postgresql_where=sa.text("turn_ref IS NOT NULL"),
    )
    for statement in GUARD_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for statement in GUARD_REMOVAL:
        op.execute(statement)
    op.drop_table("monitored_events")

"""Fleet runs and their workers: the orchestrator's durable inbox.

downgrade: destructive

Only the fleet rows are removed. No existing table changes: each worker's
session, lease and journal stay in the neutral state store tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093_fleet_runs"
down_revision: str | None = "0092_merge_agent_backend"
branch_labels: str | None = None
depends_on: str | None = None


def _amount(name: str, *, default: bool) -> sa.Column:
    return sa.Column(
        name,
        sa.Numeric(12, 6),
        nullable=False,
        **({"server_default": sa.text("0")} if default else {}),
    )


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "fleet_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_id(),
        sa.Column("orchestrator_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("orchestrator_session_id", sa.Text(), nullable=False),
        sa.Column("orchestrator_binding_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("operation_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _amount("total_budget_usd", default=False),
        _amount("reserved_usd", default=True),
        _amount("observed_usd", default=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_workers", sa.Integer(), nullable=False),
        sa.Column("quorum", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_fleet_run_tenant_id"),
        sa.UniqueConstraint("tenant_id", "operation_key", name="uq_fleet_run_operation"),
        sa.CheckConstraint(
            "status IN ('running', 'collecting', 'completed', 'cancelled', 'failed')",
            name="ck_fleet_run_status",
        ),
        sa.CheckConstraint("max_workers > 0", name="ck_fleet_run_max_workers"),
        sa.CheckConstraint(
            "quorum IS NULL OR (quorum > 0 AND quorum <= max_workers)", name="ck_fleet_run_quorum"
        ),
        sa.CheckConstraint(
            "total_budget_usd >= 0 AND reserved_usd >= 0 AND observed_usd >= 0",
            name="ck_fleet_run_amounts",
        ),
    )
    op.create_table(
        "fleet_worker",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        _tenant_id(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("spec_digest", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("binding_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        _amount("budget_usd", default=False),
        _amount("reserved_usd", default=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("turns_used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("reviewer_of", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_contract", postgresql.JSONB(), nullable=True),
        sa.Column("result_session_id", sa.Text(), nullable=True),
        sa.Column("result_sequence", sa.Integer(), nullable=True),
        sa.Column("result_digest", sa.Text(), nullable=True),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_fleet_worker_ordinal"),
        sa.UniqueConstraint("run_id", "id", name="uq_fleet_worker_run_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["fleet_run.tenant_id", "fleet_run.id"],
            name="fk_fleet_worker_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "reviewer_of"],
            ["fleet_worker.run_id", "fleet_worker.id"],
            name="fk_fleet_worker_reviewer",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'starting', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out', 'budget_exceeded')",
            name="ck_fleet_worker_status",
        ),
        sa.CheckConstraint(
            "backend IN ('anthropic', 'openai', 'gemini')", name="ck_fleet_worker_backend"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_fleet_worker_ordinal"),
        sa.CheckConstraint(
            "reviewer_of IS NULL OR reviewer_of <> id", name="ck_fleet_worker_review"
        ),
        sa.CheckConstraint(
            "max_turns > 0 AND turns_used >= 0 AND turns_used <= max_turns AND attempts >= 0",
            name="ck_fleet_worker_counts",
        ),
        sa.CheckConstraint("budget_usd >= 0 AND reserved_usd >= 0", name="ck_fleet_worker_amounts"),
        sa.CheckConstraint(
            "(result_session_id IS NULL) = (result_sequence IS NULL) "
            "AND (result_session_id IS NULL) = (result_digest IS NULL) "
            "AND (result_excerpt IS NULL OR char_length(result_excerpt) <= 4096)",
            name="ck_fleet_worker_result",
        ),
    )
    op.create_index(
        "ix_fleet_worker_active",
        "fleet_worker",
        ["tenant_id"],
        postgresql_where=sa.text("status IN ('starting', 'running')"),
    )
    op.create_index("ix_fleet_worker_run_status", "fleet_worker", ["run_id", "status"])


def downgrade() -> None:
    op.drop_table("fleet_worker")
    op.drop_table("fleet_run")

"""Workflows: every table the engine, triggers, waits and deliveries use.

A workflow keeps its draft and owner (`workflows`), published versions, access, grants
and publish requests. Triggers keep their secrets and received events. A run projects its
ledger (`workflow_run_events`, hash-chained, append-only by trigger) onto steps, attempts, lanes,
waits with their answers and posts, proposals, activity lines and deliveries. Every table
cascades with its tenant. `web_threads.subject` names what a thread is about (one per
person and subject). `usage_events` gains the run and step that spent it: nullable,
foreign keys added NOT VALID (every existing row is NULL) and ON DELETE SET NULL, with
partial indexes built concurrently, outside the migration transaction, so turns keep
writing usage meanwhile. Downgrading drops the workflow tables and their data.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0222_workflows"
down_revision: str | None = "0221_routine_delivery_hold"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = (
    "workflow_event_buffer",
    "workflows",
    "workflow_access",
    "workflow_grants",
    "workflow_publish_requests",
    "workflow_runs",
    "workflow_triggers",
    "workflow_versions",
    "workflow_lanes",
    "workflow_run_events",
    "workflow_steps",
    "workflow_trigger_events",
    "workflow_trigger_secrets",
    "workflow_attempts",
    "workflow_proposals",
    "workflow_step_activity",
    "workflow_waits",
    "workflow_deliveries",
    "workflow_wait_answers",
    "workflow_wait_posts",
)
USAGE_INDEXES = {
    "ix_usage_events_workflow_run": "workflow_run_id",
    "ix_usage_events_workflow_step": "workflow_step_id",
}
USAGE_FKS = {
    "fk_usage_events_workflow_runs": ("workflow_run_id", "workflow_runs"),
    "fk_usage_events_workflow_steps": ("workflow_step_id", "workflow_steps"),
}

#: A copy of `daimon.core._workflow_ledger_guard`; a test keeps the two equal.
LEDGER_GUARD = (
    """CREATE OR REPLACE FUNCTION workflow_run_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $guard$
DECLARE
    gone boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('daimon.workflow_ledger_maintenance', true) = 'on' THEN
            RETURN OLD;
        END IF;
        EXECUTE 'SELECT NOT EXISTS (SELECT 1 FROM ' || quote_ident(TG_TABLE_SCHEMA)
            || '.workflow_runs WHERE id = $1) OR NOT EXISTS (SELECT 1 FROM '
            || quote_ident(TG_TABLE_SCHEMA) || '.tenants WHERE id = $2)'
            INTO STRICT gone USING OLD.run_id, OLD.tenant_id;
        IF gone THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'workflow run events are append-only' USING ERRCODE = 'restrict_violation';
END
$guard$""",
    """CREATE OR REPLACE TRIGGER workflow_run_events_append_only
BEFORE UPDATE OR DELETE ON workflow_run_events
FOR EACH ROW EXECUTE FUNCTION workflow_run_events_append_only()""",
)
LEDGER_GUARD_REMOVAL = ("DROP FUNCTION IF EXISTS workflow_run_events_append_only() CASCADE",)


def create_tables() -> None:
    """Every workflow table, `web_threads.subject` and the `usage_events` columns."""
    op.create_table(
        "workflow_event_buffer",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_event_buffer_expires", "workflow_event_buffer", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_workflow_event_buffer_match",
        "workflow_event_buffer",
        ["tenant_id", "source", "event_key"],
        unique=False,
    )
    op.create_table(
        "workflows",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("home_channel_id", sa.Text(), nullable=True),
        sa.Column("visibility", sa.Text(), server_default="private", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("owner_account_id", sa.UUID(), nullable=True),
        sa.Column("live_version", sa.Integer(), nullable=True),
        sa.Column("draft_spec", sa.JSON(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("draft_base_version", sa.Integer(), nullable=True),
        sa.Column("draft_updated_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "draft_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_counter", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("monthly_cap_usd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'needs_owner')", name="ck_workflows_status"
        ),
        sa.CheckConstraint("visibility IN ('private', 'shared')", name="ck_workflows_visibility"),
        sa.ForeignKeyConstraint(
            ["draft_updated_by_account_id"], ["accounts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflows_owner", "workflows", ["owner_account_id"], unique=False)
    op.create_index("ix_workflows_tenant", "workflows", ["tenant_id"], unique=False)
    op.create_table(
        "workflow_access",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("group_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('editor', 'runner', 'viewer')", name="ck_workflow_access_role"
        ),
        sa.CheckConstraint(
            "(account_id IS NULL) <> (group_id IS NULL)", name="ck_workflow_access_subject"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "account_id", name="uq_workflow_access_account"),
        sa.UniqueConstraint("workflow_id", "group_id", name="uq_workflow_access_group"),
    )
    op.create_index("ix_workflow_access_account", "workflow_access", ["account_id"], unique=False)
    op.create_table(
        "workflow_grants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "pins",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("approved_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('unattended_write', 'builtins', 'code_network', 'untrusted_waiver', "
            "'unverified_webhook', 'budget_over_default')",
            name="ck_workflow_grants_kind",
        ),
        sa.ForeignKeyConstraint(["approved_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_grants_version", "workflow_grants", ["workflow_id", "version"], unique=False
    )
    op.create_table(
        "workflow_publish_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "grants",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("requested_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by_account_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_workflow_publish_requests_status",
        ),
        sa.ForeignKeyConstraint(["decided_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_publish_requests_workflow",
        "workflow_publish_requests",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        "uq_workflow_publish_requests_pending",
        "workflow_publish_requests",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), server_default="live", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("waiting_reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("failure_step", sa.Text(), nullable=True),
        sa.Column("run_as_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "triggered_by",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column(
            "inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "untrusted_inputs", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("concurrency_key", sa.Text(), nullable=True),
        sa.Column("parent_run_id", sa.UUID(), nullable=True),
        sa.Column("parent_step_id", sa.UUID(), nullable=True),
        sa.Column("forked_from_run_id", sa.UUID(), nullable=True),
        sa.Column("forked_from_step", sa.Text(), nullable=True),
        sa.Column("redrive_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("test_options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("budget_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("head_hash", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('live', 'test')", name="ck_workflow_runs_mode"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('completed', 'nothing_to_do')",
            name="ck_workflow_runs_outcome",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled', "
            "'timed_out')",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint(
            "waiting_reason IS NULL OR waiting_reason IN ('approval', 'question', 'timer', "
            "'event', 'blocked', 'unknown')",
            name="ck_workflow_runs_waiting_reason",
        ),
        sa.ForeignKeyConstraint(["forked_from_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_as_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "number", name="uq_workflow_runs_number"),
    )
    op.create_index(
        "ix_workflow_runs_active",
        "workflow_runs",
        ["tenant_id", "status"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting')"),
    )
    op.create_index(
        "ix_workflow_runs_event",
        "workflow_runs",
        [sa.text("(triggered_by ->> 'event_id')")],
        unique=False,
        postgresql_where=sa.text("triggered_by ? 'event_id'"),
    )
    op.create_index(
        "ix_workflow_runs_forked_from",
        "workflow_runs",
        ["forked_from_run_id"],
        unique=False,
        postgresql_where=sa.text("forked_from_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_workflow_runs_parent",
        "workflow_runs",
        ["parent_run_id"],
        unique=False,
        postgresql_where=sa.text("parent_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_workflow_runs_parent_step",
        "workflow_runs",
        ["parent_step_id"],
        unique=False,
        postgresql_where=sa.text("parent_step_id IS NOT NULL"),
    )
    op.create_index("ix_workflow_runs_run_as", "workflow_runs", ["run_as_account_id"], unique=False)
    op.create_index(
        "ix_workflow_runs_workflow_created",
        "workflow_runs",
        ["workflow_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "workflow_triggers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seeded_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("listening_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_routine_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('manual', 'schedule', 'webhook', 'event', 'call')",
            name="ck_workflow_triggers_kind",
        ),
        sa.ForeignKeyConstraint(["from_routine_id"], ["routines.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "trigger_key", name="uq_workflow_triggers_key"),
    )
    op.create_index(
        "ix_workflow_triggers_due",
        "workflow_triggers",
        ["next_fire_at"],
        unique=False,
        postgresql_where=sa.text("enabled AND next_fire_at IS NOT NULL"),
    )
    op.create_index(
        "ix_workflow_triggers_routine",
        "workflow_triggers",
        ["from_routine_id"],
        unique=False,
        postgresql_where=sa.text("from_routine_id IS NOT NULL"),
    )
    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("published_by_account_id", sa.UUID(), nullable=True),
        sa.Column("approved_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('web', 'chat', 'import', 'routine')", name="ck_workflow_versions_source"
        ),
        sa.ForeignKeyConstraint(["approved_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workflow_id", "number", name="pk_workflow_versions"),
    )
    op.create_index(
        "ix_workflow_versions_publisher",
        "workflow_versions",
        ["published_by_account_id"],
        unique=False,
    )
    op.create_table(
        "workflow_lanes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("lane_key", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('open', 'archived')", name="ck_workflow_lanes_status"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "lane_key", name="uq_workflow_lanes_key"),
    )
    op.create_table(
        "workflow_run_events",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "seq", name="pk_workflow_run_events"),
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("visit", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=True),
        sa.Column("parent_step_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("waiting_reason", sa.Text(), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "files",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("route", sa.Text(), nullable=True),
        sa.Column("route_reason", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "taint", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "via", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("lane", sa.Text(), nullable=True),
        sa.Column("reused", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "error_class IS NULL OR error_class IN ('retryable', 'gate', 'contract', 'agent', "
            "'unknown', 'fatal')",
            name="ck_workflow_steps_error_class",
        ),
        sa.CheckConstraint(
            "reused IS NULL OR reused IN ('fork', 'pinned')", name="ck_workflow_steps_reused"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'waiting', 'succeeded', 'failed', "
            "'skipped', 'cancelled', 'unknown')",
            name="ck_workflow_steps_status",
        ),
        sa.CheckConstraint(
            "waiting_reason IS NULL OR waiting_reason IN ('approval', 'question', 'timer', "
            "'event', 'blocked', 'unknown')",
            name="ck_workflow_steps_waiting_reason",
        ),
        sa.ForeignKeyConstraint(["parent_step_id"], ["workflow_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "node_id",
            "visit",
            "item_index",
            name="uq_workflow_steps_visit",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_workflow_steps_leased",
        "workflow_steps",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_workflow_steps_ready",
        "workflow_steps",
        ["ready_at"],
        unique=False,
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_table(
        "workflow_trigger_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=True),
        sa.Column("test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'started', 'skipped', 'rejected', 'duplicate')",
            name="ck_workflow_trigger_events_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_id"], ["workflow_triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_trigger_events_accepted",
        "workflow_trigger_events",
        ["received_at"],
        unique=False,
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.create_index(
        "ix_workflow_trigger_events_run", "workflow_trigger_events", ["run_id"], unique=False
    )
    op.create_index(
        "ix_workflow_trigger_events_trigger",
        "workflow_trigger_events",
        ["trigger_id", "received_at"],
        unique=False,
    )
    op.create_index(
        "uq_workflow_trigger_events_delivery",
        "workflow_trigger_events",
        ["trigger_id", "delivery_id"],
        unique=True,
        postgresql_where=sa.text("delivery_id IS NOT NULL AND status <> 'duplicate'"),
    )
    op.create_index(
        "uq_workflow_trigger_events_payload",
        "workflow_trigger_events",
        ["trigger_id", "payload_hash"],
        unique=True,
        postgresql_where=sa.text("payload_hash IS NOT NULL AND status <> 'duplicate'"),
    )
    op.create_table(
        "workflow_trigger_secrets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False),
        sa.Column("created_by_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_id"], ["workflow_triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_trigger_secrets_trigger",
        "workflow_trigger_secrets",
        ["trigger_id"],
        unique=False,
    )
    op.create_table(
        "workflow_attempts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("send_intent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "error_class IS NULL OR error_class IN ('retryable', 'gate', 'contract', 'agent', "
            "'unknown', 'fatal')",
            name="ck_workflow_attempts_error_class",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'abandoned')",
            name="ck_workflow_attempts_status",
        ),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", "number", name="uq_workflow_attempts_number"),
    )
    op.create_index(
        "ix_workflow_attempts_session", "workflow_attempts", ["session_id"], unique=False
    )
    op.create_table(
        "workflow_proposals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("server_url", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("arguments_hash", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_by_account_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'sent', 'failed', 'unknown')",
            name="ck_workflow_proposals_status",
        ),
        sa.ForeignKeyConstraint(["decided_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_proposals_step", "workflow_proposals", ["step_id"], unique=False)
    op.create_table(
        "workflow_step_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('message', 'tool', 'note')", name="ck_workflow_step_activity_kind"
        ),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_step_activity_step", "workflow_step_activity", ["step_id", "id"], unique=False
    )
    op.create_table(
        "workflow_waits",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="waiting", nullable=False),
        sa.Column(
            "addressees",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("deliver", sa.Text(), nullable=True),
        sa.Column("min_approvals", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("shown", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("shown_hash", sa.Text(), nullable=True),
        sa.Column("event_source", sa.Text(), nullable=True),
        sa.Column("event_key", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decided_by_account_id", sa.UUID(), nullable=True),
        sa.Column("decided_via", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("answer_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answer_files", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('approval', 'question', 'timer', 'event', 'blocked', 'unknown')",
            name="ck_workflow_waits_kind",
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'completed', 'timed_out', 'cancelled')",
            name="ck_workflow_waits_status",
        ),
        sa.ForeignKeyConstraint(["decided_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_waits_decider", "workflow_waits", ["decided_by_account_id"], unique=False
    )
    op.create_index(
        "ix_workflow_waits_event",
        "workflow_waits",
        ["tenant_id", "event_source", "event_key"],
        unique=False,
        postgresql_where=sa.text("status = 'waiting' AND kind = 'event'"),
    )
    op.create_index(
        "ix_workflow_waits_open",
        "workflow_waits",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'waiting'"),
    )
    op.create_index("ix_workflow_waits_run", "workflow_waits", ["run_id"], unique=False)
    op.create_index("ix_workflow_waits_step", "workflow_waits", ["step_id"], unique=False)
    op.create_table(
        "workflow_deliveries",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("wait_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('finish', 'ask', 'reminder', 'ask_update', 'notice')",
            name="ck_workflow_deliveries_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'sent', 'skipped', 'refused', 'failed')",
            name="ck_workflow_deliveries_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wait_id"], ["workflow_waits.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_deliveries_due",
        "workflow_deliveries",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.create_index("ix_workflow_deliveries_run", "workflow_deliveries", ["run_id"], unique=False)
    op.create_index("ix_workflow_deliveries_wait", "workflow_deliveries", ["wait_id"], unique=False)
    op.create_index(
        "ix_workflow_deliveries_workflow", "workflow_deliveries", ["workflow_id"], unique=False
    )
    op.create_table(
        "workflow_wait_answers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("wait_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wait_id"], ["workflow_waits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wait_id", "account_id", name="uq_workflow_wait_answers_person"),
        sa.UniqueConstraint(
            "wait_id", "idempotency_key", name="uq_workflow_wait_answers_idempotency"
        ),
    )
    op.create_index(
        "ix_workflow_wait_answers_account", "workflow_wait_answers", ["account_id"], unique=False
    )
    op.create_table(
        "workflow_wait_posts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("wait_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("addressee_account_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_workflow_wait_posts_platform"
        ),
        sa.ForeignKeyConstraint(["addressee_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wait_id"], ["workflow_waits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_wait_posts_message",
        "workflow_wait_posts",
        ["tenant_id", "platform", "message_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_wait_posts_thread",
        "workflow_wait_posts",
        ["tenant_id", "platform", "thread_id"],
        unique=False,
    )
    op.create_index("ix_workflow_wait_posts_wait", "workflow_wait_posts", ["wait_id"], unique=False)

    op.add_column(
        "web_threads",
        sa.Column("subject", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "uq_web_threads_subject",
        "web_threads",
        ["tenant_id", "person_id", "subject"],
        unique=True,
        postgresql_where=sa.text("subject IS NOT NULL"),
    )
    for statement in LEDGER_GUARD:
        op.execute(statement)
    for name, (column, table) in USAGE_FKS.items():
        op.add_column("usage_events", sa.Column(column, sa.UUID(), nullable=True))
        op.execute(
            f"ALTER TABLE usage_events ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES {table} (id) ON DELETE SET NULL NOT VALID"
        )


def create_usage_indexes(*, concurrently: bool = True) -> None:
    """A run cut off mid-build leaves an invalid index, which a retried run drops and rebuilds."""
    for name, column in USAGE_INDEXES.items():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": name},
        )
        if invalid:
            op.drop_index(name, table_name="usage_events", postgresql_concurrently=concurrently)
        op.create_index(
            name,
            "usage_events",
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
            postgresql_concurrently=concurrently,
            if_not_exists=True,
        )


def drop_all(*, concurrently: bool = True) -> None:
    for name in USAGE_INDEXES:
        op.drop_index(
            name, table_name="usage_events", if_exists=True, postgresql_concurrently=concurrently
        )
    for name, (column, _) in USAGE_FKS.items():
        op.drop_constraint(name, "usage_events", type_="foreignkey")
        op.drop_column("usage_events", column)
    op.drop_index("uq_web_threads_subject", table_name="web_threads")
    op.drop_column("web_threads", "subject")
    for table in reversed(TABLES):
        op.drop_table(table)
    for statement in LEDGER_GUARD_REMOVAL:
        op.execute(statement)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    create_tables()
    with op.get_context().autocommit_block():  # commits the DDL above, releasing its locks
        create_usage_indexes()


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in USAGE_INDEXES:
            op.drop_index(
                name, table_name="usage_events", if_exists=True, postgresql_concurrently=True
            )
    op.execute("SET LOCAL lock_timeout = '5s'")
    drop_all(concurrently=False)

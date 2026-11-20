"""Evals: suites, case revisions, blobs, shares, arms, runs, items, scores, reviews, bundles.

Fifteen new tables, all tenant scoped. Documents are JSONB copies of the bundle format;
columns carry what queries filter on. Additive only.

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "0222_evals_foundation"
down_revision: str | None = "0221_routine_delivery_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "eval_suites",
    "eval_suite_versions",
    "eval_cases",
    "eval_case_revisions",
    "eval_suite_version_cases",
    "eval_blobs",
    "eval_shares",
    "eval_arms",
    "eval_runs",
    "eval_run_items",
    "eval_scores",
    "eval_review_queues",
    "eval_review_assignments",
    "eval_comments",
    "eval_bundles",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("scrub_status", sa.Text(), server_default="scrubbed", nullable=False),
        sa.Column("source_instance", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
            "scrub_status IN ('scrubbed', 'partial', 'unscrubbed')",
            name="ck_eval_suites_scrub_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_eval_suites_tenant_key"),
    )
    op.create_table(
        "eval_suite_versions",
        sa.Column("suite_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suite_id", "version"),
    )
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("suite_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suite_id", "key", name="uq_eval_cases_suite_key"),
    )
    op.create_table(
        "eval_case_revisions",
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("fidelity", sa.Text(), nullable=False),
        sa.Column(
            "flags", postgresql.ARRAY(Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "blob_shas", postgresql.ARRAY(Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("scrub_status", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["case_id"], ["eval_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("case_id", "revision"),
    )
    op.create_table(
        "eval_suite_version_cases",
        sa.Column("suite_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Double(), server_default=sa.text("1"), nullable=False),
        sa.Column("split", sa.Text(), server_default="test", nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["eval_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["suite_id", "version"],
            ["eval_suite_versions.suite_id", "eval_suite_versions.version"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suite_id", "version", "position"),
    )
    op.create_table(
        "eval_blobs",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("scrub", sa.Text(), nullable=False),
        sa.Column(
            "needs_human_check", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "sha256"),
    )
    op.create_table(
        "eval_shares",
        sa.Column("suite_id", sa.UUID(), nullable=False),
        sa.Column("who_kind", sa.Text(), nullable=False),
        sa.Column("who", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('viewer', 'reviewer', 'editor')", name="ck_eval_shares_role"),
        sa.CheckConstraint("who_kind IN ('person', 'group')", name="ck_eval_shares_who_kind"),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suite_id", "who_kind", "who"),
    )
    op.create_table(
        "eval_arms",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_eval_arms_tenant_key"),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("suite_id", sa.UUID(), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("cap_usd", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("spend_usd", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_eval_runs_tenant_key"),
    )
    op.create_table(
        "eval_run_items",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("case_key", sa.Text(), nullable=False),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("arm_key", sa.Text(), nullable=False),
        sa.Column("repeat", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column(
            "blob_shas", postgresql.ARRAY(Text()), server_default=sa.text("'{}'"), nullable=False
        ),
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
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "key", name="uq_eval_run_items_run_key"),
    )
    op.create_index(
        "ix_eval_run_items_run_status", "eval_run_items", ["run_id", "status"], unique=False
    )
    op.create_table(
        "eval_scores",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=True),
        sa.Column("criterion", sa.Text(), nullable=False),
        sa.Column("grader_kind", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "key", name="uq_eval_scores_run_key"),
    )
    op.create_table(
        "eval_review_queues",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "key", name="uq_eval_review_queues_run_key"),
    )
    op.create_table(
        "eval_review_assignments",
        sa.Column("queue_id", sa.UUID(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("reviewer_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["queue_id"], ["eval_review_queues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("queue_id", "item_key", "reviewer_key"),
    )
    op.create_table(
        "eval_comments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("suite_id", sa.UUID(), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_id IS NOT NULL OR suite_id IS NOT NULL", name="ck_eval_comments_parent"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_eval_comments_tenant_key"),
    )
    op.create_table(
        "eval_bundles",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("actor_key", sa.Text(), nullable=False),
        sa.Column("scrub_status", sa.Text(), nullable=False),
        sa.Column("blob_sha256", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("direction IN ('export', 'import')", name="ck_eval_bundles_direction"),
        sa.CheckConstraint(
            "scrub_status IN ('scrubbed', 'partial', 'unscrubbed')",
            name="ck_eval_bundles_scrub_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)

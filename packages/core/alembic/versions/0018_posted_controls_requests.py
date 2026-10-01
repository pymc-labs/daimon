"""Provenance and outcome columns for credential requests and agent files.

A posted control has to survive the turn that minted it: the row now carries
who it was minted for (`target_ma_agent_id` / `target_name`, the responder's
own name), what work was waiting on it (`requested_work`), the compare-and-set
precondition the card promised (`replaces_updated_at`), and how the click
actually ended (`outcome`). `idempotency_key` makes a re-posted control
collapse onto one row rather than stacking, mirroring `task_continuations`.

`agent_files` gains creator and last-setter attribution so a replacement can
name the person whose value was overwritten.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018_posted_controls_requests"
down_revision: str | None = "0017_thread_session_unsaved_work"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL on a table that already has rows: give the column a default so
    # live rows backfill with fresh keys, then drop the default so every
    # future insert must state its own key (the mint site owns it, not the DB).
    op.add_column(
        "credential_requests",
        sa.Column(
            "idempotency_key",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.alter_column("credential_requests", "idempotency_key", server_default=None)
    op.create_unique_constraint(
        "uq_credential_requests_idempotency_key",
        "credential_requests",
        ["idempotency_key"],
    )

    op.add_column("credential_requests", sa.Column("requested_work", sa.Text(), nullable=True))
    op.add_column("credential_requests", sa.Column("target_ma_agent_id", sa.Text(), nullable=True))
    op.add_column("credential_requests", sa.Column("target_name", sa.Text(), nullable=True))
    op.add_column("credential_requests", sa.Column("responder_name", sa.Text(), nullable=True))
    op.add_column(
        "credential_requests",
        sa.Column("replaces_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # No CHECK on `outcome`: the vocabulary is pinned in Python by
    # `CredentialRequestOutcome`, the same way 0017 left `pending_unsaved_work`
    # untyped — widening it later must not need a table lock.
    op.add_column("credential_requests", sa.Column("outcome", sa.Text(), nullable=True))

    op.create_check_constraint(
        "ck_credential_requests_kind",
        "credential_requests",
        "kind IN ('env', 'env_file', 'mcp', 'repo', 'skill_repo')",
    )

    # No FK to accounts.id, for the same reason CredentialRequest.account_id
    # has none: these are erased through the platform-user-scoped erasure
    # helper, not through an accounts.id cascade. No index either — every read
    # of an agent file is already keyed by the (tenant, agent, key) PK.
    op.add_column(
        "agent_files",
        sa.Column("created_by_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "agent_files",
        sa.Column("last_set_by_account_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_files", "last_set_by_account_id")
    op.drop_column("agent_files", "created_by_account_id")

    op.drop_constraint("ck_credential_requests_kind", "credential_requests", type_="check")
    op.drop_constraint(
        "uq_credential_requests_idempotency_key", "credential_requests", type_="unique"
    )
    for column in (
        "outcome",
        "replaces_updated_at",
        "responder_name",
        "target_name",
        "target_ma_agent_id",
        "requested_work",
        "idempotency_key",
    ):
        op.drop_column("credential_requests", column)

"""Skills mirrored from a skills server, their deployed versions, and agents' follow choices.

`server_skills` holds one row per (tenant, server, upstream skill): the library skill it
owns, the accepted manifest and digest, and an update waiting for approval.
`server_skill_deployments` records the concrete version each provider holds for a digest.
`agent_skill_follows` stores an agent's choice to pin a skill (no row means it follows),
and the update its backend consent held back until the owner re-confirms.
Downgrading drops all three tables; the library skills stay as plain custom skills.

downgrade: destructive
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0179_server_skills"
down_revision: str | None = "0178_web_logins"
branch_labels: str | None = None
depends_on: str | None = None


def _tenant() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _at(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "server_skills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        _tenant(),
        sa.Column("server", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("allowed_tools", JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="current"),
        sa.Column("pending_digest", sa.Text(), nullable=True),
        sa.Column("pending_label", sa.Text(), nullable=True),
        sa.Column("pending_changes", JSONB(), nullable=True),
        sa.Column(
            "added_by_person_id",
            UUID(as_uuid=True),
            sa.ForeignKey("web_people.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _at("created_at"),
        _at("updated_at"),
        sa.CheckConstraint(
            "status IN ('current', 'pending', 'unavailable')", name="ck_server_skills_status"
        ),
        sa.UniqueConstraint("tenant_id", "server", "uri", name="uq_server_skills_upstream"),
        sa.UniqueConstraint("tenant_id", "skill_id", name="uq_server_skills_skill"),
    )
    op.create_table(
        "server_skill_deployments",
        sa.Column(
            "mirror_id",
            UUID(as_uuid=True),
            sa.ForeignKey("server_skills.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.Text(), primary_key=True),
        _tenant(),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        _at("updated_at"),
        sa.CheckConstraint("version <> 'latest'", name="ck_server_skill_deployments_concrete"),
    )
    op.create_table(
        "agent_skill_follows",
        _tenant(),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("follow", sa.Boolean(), nullable=False),
        sa.Column("held_digest", sa.Text(), nullable=True),
        sa.Column("held_reason", sa.Text(), nullable=True),
        _at("updated_at"),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "skill_id"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("agent_skill_follows")
    op.drop_table("server_skill_deployments")
    op.drop_table("server_skills")

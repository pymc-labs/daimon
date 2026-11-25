"""workspace_skills: a library skill's versions, files and scope kept locally.

`workspace_skills` holds one row per (tenant, library skill): its name, latest version and
an optional `scope` (NULL: visible to everyone who may use the library). Each published
version gets a `workspace_skill_versions` row and one `workspace_skill_files` row per file,
so a skill can be served per caller without asking the provider. The tables are empty until
a version is published; nothing reads them unless a scope checker is wired in.

Downgrading drops the tables; the library skills at the provider are unchanged.

downgrade: destructive
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0250_workspace_skills"
down_revision: str | None = "0242_data_ops"
branch_labels: str | None = None
depends_on: str | None = None


def _tenant() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id",
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "workspace_skills",
        _tenant(),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "skill_id"),
    )
    op.create_table(
        "workspace_skill_versions",
        _tenant(),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "skill_id", "version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["workspace_skills.tenant_id", "workspace_skills.skill_id"],
            ondelete="CASCADE",
            name="fk_workspace_skill_versions_skill",
        ),
    )
    op.create_table(
        "workspace_skill_files",
        _tenant(),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "skill_id", "version", "path"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id", "version"],
            [
                "workspace_skill_versions.tenant_id",
                "workspace_skill_versions.skill_id",
                "workspace_skill_versions.version",
            ],
            ondelete="CASCADE",
            name="fk_workspace_skill_files_version",
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("workspace_skill_files")
    op.drop_table("workspace_skill_versions")
    op.drop_table("workspace_skills")

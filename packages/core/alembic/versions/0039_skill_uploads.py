"""Record where each agent skill came from, so uploads sit beside repo syncs.

`user_skills` rows have only ever come from a skill repo. A skill can now also
be added by pasting it, uploading it or naming one GitHub path, so each row
records its `source` (`repo` or `upload`), a short `origin` a person can read
back, and the account that added it. Existing rows are repo syncs, which is
what the defaults say, so nothing changes for them.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_skill_uploads"
down_revision: str | None = "0038_teams_parity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "user_skills",
        sa.Column("source", sa.Text(), nullable=False, server_default="repo"),
    )
    op.add_column(
        "user_skills",
        sa.Column("origin", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "user_skills",
        sa.Column("added_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_user_skills_source", "user_skills", "source IN ('repo', 'upload')"
    )
    op.create_foreign_key(
        "fk_user_skills_added_by_account_id",
        "user_skills",
        "accounts",
        ["added_by_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_user_skills_added_by_account_id", "user_skills", type_="foreignkey")
    op.drop_constraint("ck_user_skills_source", "user_skills", type_="check")
    op.drop_column("user_skills", "added_by_account_id")
    op.drop_column("user_skills", "origin")
    op.drop_column("user_skills", "source")

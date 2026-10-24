"""Retain uploader provenance for selfhosted agent skill pins.

downgrade: unsupported
Removing attachment provenance would strand skills on shared agents.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114_selfhosted_skill_pins"
down_revision: str = "0113_selfhosted_skill_catalog"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "selfhosted_agents",
        sa.Column(
            "skill_attachments",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("Agent skill attachment provenance cannot be discarded")

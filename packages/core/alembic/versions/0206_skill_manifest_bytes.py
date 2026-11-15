"""skill_manifest_bytes: each measured skill version's SKILL.md size.

`skill_sizes` gains nullable `manifest_bytes`. Gemini uploads a skill past its
inline limits and mounts only its SKILL.md inline, so capture and the turn
count those bytes against the shared inline budget. Rows measured before it
stay NULL and are measured again on their next capture. Downgrading drops the
column; those rows are then measured again after a later upgrade.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0206_skill_manifest_bytes"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("skill_sizes", sa.Column("manifest_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("skill_sizes", "manifest_bytes")

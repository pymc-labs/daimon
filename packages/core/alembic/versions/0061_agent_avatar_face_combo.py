"""Store the assigned face combination beside each agent avatar.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0061_agent_avatar_face_combo"
down_revision: str | None = "0060_platform_names"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agent_avatars", sa.Column("face_combo", sa.Text(), nullable=True))
    op.add_column("agent_avatars", sa.Column("face_thumbnail", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_avatars", "face_thumbnail")
    op.drop_column("agent_avatars", "face_combo")

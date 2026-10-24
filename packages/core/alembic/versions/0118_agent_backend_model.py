"""agent_backend_model: the model an admin picked with an agent's provider.

`agent_backend_revision.model` is null when the provider's default model
applies, which is every revision written before this column existed.
Additive only; downgrading drops the column and every agent falls back to
its provider's default model.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0118_agent_backend_model"
down_revision: str | None = "0117_agent_channel_memory_store"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_backend_revision", sa.Column("model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("agent_backend_revision", "model")

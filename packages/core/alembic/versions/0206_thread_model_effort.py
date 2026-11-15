"""thread_model_settings.effort: a conversation's chosen effort, with or without a model.

`effort` keeps the effort someone picked for a conversation (low, medium, high,
xhigh or max); null keeps the agent's own. `model` becomes nullable so a pick can
set the effort alone and leave the model to the agent. Downgrading drops effort-only
rows, whose conversations go back to their agents' own settings, and the column.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0206_thread_model_effort"
down_revision: str | None = "0205_web_search_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("thread_model_settings", sa.Column("effort", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_thread_model_settings_effort",
        "thread_model_settings",
        "effort IN ('low', 'medium', 'high', 'xhigh', 'max')",
    )
    op.alter_column("thread_model_settings", "model", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM thread_model_settings WHERE model IS NULL")
    op.alter_column("thread_model_settings", "model", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("ck_thread_model_settings_effort", "thread_model_settings")
    op.drop_column("thread_model_settings", "effort")

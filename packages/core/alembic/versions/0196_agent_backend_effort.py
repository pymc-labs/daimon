"""agent_backend_revision: the agent's reasoning effort on a non-default provider.

Null keeps the model's own effort. Downgrading drops the column; agents then run
at their providers' default effort.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0196_agent_backend_effort"
down_revision: str | None = "0195_thread_model_consents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_backend_revision", sa.Column("effort", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_agent_backend_effort",
        "agent_backend_revision",
        "effort IS NULL OR effort IN ('low', 'medium', 'high')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_backend_effort", "agent_backend_revision", type_="check")
    op.drop_column("agent_backend_revision", "effort")

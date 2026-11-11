"""turn_origins: self-helpers, an agent's own copies on a provider without multiagent.

`self_helpers` marks an asking turn whose agent may ask copies of itself.
`helper_key` is a helper turn's own session key when it is not derived from
the helper's agent id: each copy of the asking agent runs under its own key.
Downgrading drops both; copies then run as no helper at all.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0197_self_helpers"
down_revision: str | None = "0196_agent_backend_effort"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "turn_origins",
        sa.Column("self_helpers", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("turn_origins", sa.Column("helper_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turn_origins", "helper_key")
    op.drop_column("turn_origins", "self_helpers")

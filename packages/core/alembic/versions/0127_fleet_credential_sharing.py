"""fleet_credential_sharing: per connector, whether an agent's fleet workers get its login.

`agent_backend_revision.fleet_credential_sharing` maps a login's stable key (a stored
connector login, "caller", "github_app", "env:<KEY>", "repo:<name>") to "share",
"skip" or "ask". Null or a missing key means "ask". Written only through a
confirmed choice; every other change carries it forward. Additive; downgrading
drops it, and every login then reads "ask" (workers run without it until asked).

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0127_fleet_credential_sharing"
down_revision: str | None = "0122_thread_provider_release"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "agent_backend_revision",
        sa.Column("fleet_credential_sharing", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("agent_backend_revision", "fleet_credential_sharing")

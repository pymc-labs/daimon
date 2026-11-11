"""agent_backend_revision: an agent may run on a self-hosted model in a pinned environment.

`environment` is the self-hosted environment record (`sh_env_...`) the agent
runs in; null everywhere else. Downgrading deletes self-hosted agent revisions
(they would violate the narrower check) and drops the column.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0202_agent_backend_environment"
down_revision: str | None = "0200_skill_copies"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_backend_revision", sa.Column("environment", sa.Text(), nullable=True))
    op.drop_constraint("ck_agent_backend_provider", "agent_backend_revision", type_="check")
    op.create_check_constraint(
        "ck_agent_backend_provider",
        "agent_backend_revision",
        "backend IS NULL OR backend IN ('anthropic', 'openai', 'gemini', 'selfhosted')",
    )
    op.create_check_constraint(
        "ck_agent_backend_environment",
        "agent_backend_revision",
        "(environment IS NULL) = (backend IS DISTINCT FROM 'selfhosted')"
        " AND (environment IS NULL OR left(environment, 7) = 'sh_env_')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_backend_environment", "agent_backend_revision", type_="check")
    op.execute("DELETE FROM agent_backend_revision WHERE backend = 'selfhosted'")
    op.drop_constraint("ck_agent_backend_provider", "agent_backend_revision", type_="check")
    op.create_check_constraint(
        "ck_agent_backend_provider",
        "agent_backend_revision",
        "backend IS NULL OR backend IN ('anthropic', 'openai', 'gemini')",
    )
    op.drop_column("agent_backend_revision", "environment")

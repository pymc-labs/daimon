"""eval_key on thread_sessions and turn_outcomes

What a session was created with and what each turn ran with: the session's
identity fingerprint and tools hash, content hashes of the safety block, guidance
and turn controls, the platform whose adapter framed the turn, and every resolved
feature with the tier that decided it. Evals group results by it. Existing rows
keep NULL.

Revision ID: 0188_eval_key
Revises: 0187_feature_values

Downgrading drops the recorded keys.

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0188_eval_key"
down_revision: str | None = "0187_feature_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("thread_sessions", "turn_outcomes")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("eval_key", JSONB(), nullable=True))


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "eval_key")

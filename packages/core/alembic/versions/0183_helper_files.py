"""helper_asks: files passed to a helper and the files it sends back

Revision ID: 0183_helper_files
Revises: 0182_server_skills

downgrade: destructive
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0183_helper_files"
down_revision: str | None = "0182_server_skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.add_column(
        "helper_asks",
        sa.Column("files", postgresql.JSONB(), server_default=_EMPTY, nullable=False),
    )
    op.add_column(
        "helper_asks",
        sa.Column("reply_files", postgresql.JSONB(), server_default=_EMPTY, nullable=False),
    )
    op.add_column("helper_asks", sa.Column("files_token", sa.Text(), nullable=True))
    op.create_index("helper_asks_files_token_idx", "helper_asks", ["files_token"], unique=True)


def downgrade() -> None:
    op.drop_index("helper_asks_files_token_idx", table_name="helper_asks")
    op.drop_column("helper_asks", "files_token")
    op.drop_column("helper_asks", "reply_files")
    op.drop_column("helper_asks", "files")

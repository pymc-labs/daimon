"""What a chat session read from data sources, and the data a repository memory records.

`session_data_reads` holds each data item a chat session read; `repo_memory.data_refs` the
items read before a memory was saved (None: none). Credential forms gain the `data_source`
kind. Additive only.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0247_data_memory"
down_revision: str | None = "0246_data_redaction"
branch_labels: str | None = None
depends_on: str | None = None

_KINDS = "'env', 'env_file', 'mcp', 'mcp_oauth', 'repo', 'skill_repo', 'watch_credential'"


def _kinds(kinds: str) -> None:
    op.execute("ALTER TABLE credential_requests DROP CONSTRAINT ck_credential_requests_kind")
    op.execute(
        "ALTER TABLE credential_requests ADD CONSTRAINT ck_credential_requests_kind "
        f"CHECK (kind IN ({kinds}))"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "session_data_reads",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_session_id", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_session_data_reads_tenants"
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "chat_session_id", "ref", name="pk_session_data_reads"
        ),
    )
    op.add_column("repo_memory", sa.Column("data_refs", postgresql.ARRAY(sa.Text()), nullable=True))
    _kinds(f"{_KINDS}, 'data_source'")


def downgrade() -> None:
    op.execute("DELETE FROM credential_requests WHERE kind = 'data_source'")
    _kinds(_KINDS)
    op.drop_column("repo_memory", "data_refs")
    op.drop_table("session_data_reads")

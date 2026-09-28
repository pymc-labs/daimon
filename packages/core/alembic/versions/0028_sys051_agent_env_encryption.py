"""Tag and encrypt existing agent environment values using deployment keys.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from daimon.core.agent_env_crypto import decode_value, encode_value
from daimon.core.config import load_crypto_settings
from daimon.core.github_credentials import build_multifernet
from sqlalchemy.engine import Connection

revision: str = "0028_agent_env_encryption"
down_revision: str | None = "0028_tenant_funding_mode"
branch_labels: str | None = None
depends_on: str | None = None


def _lock() -> Connection:
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(sa.text("LOCK TABLE agent_files IN ACCESS EXCLUSIVE MODE"))
    return connection


def _rewrite(connection: Connection, *, decrypt: bool) -> None:
    keys = tuple(k.get_secret_value() for k in load_crypto_settings().keys)
    if not keys and not decrypt:
        return
    cipher = build_multifernet(keys) if keys else None
    files = sa.table(
        "agent_files",
        sa.column("tenant_id", sa.Uuid),
        sa.column("agent_id", sa.Uuid),
        sa.column("key", sa.Text),
        sa.column("content", sa.Text),
        sa.column("encoding", sa.Text),
    )
    # Select by metadata only: arbitrary legacy text (even enc:v1:...) is plain.
    encoding = "fernet_v1" if decrypt else "plain"
    rows = connection.execute(sa.select(files).where(files.c.encoding == encoding)).mappings()
    for row in rows:
        if decrypt:
            value = decode_value(cipher, row["content"], encoding=row["encoding"])
            new_encoding = "plain"
        else:
            value, new_encoding = encode_value(cipher, row["content"])
        # Keep metadata and value atomic; timestamps/attribution are unchanged.
        connection.execute(
            files.update()
            .where(
                files.c.tenant_id == row["tenant_id"],
                files.c.agent_id == row["agent_id"],
                files.c.key == row["key"],
            )
            .values(content=value, encoding=new_encoding)
        )


def upgrade() -> None:
    connection = _lock()
    # Also supports a maintenance re-run after enabling keys on a keyless DB.
    if "encoding" not in {
        column["name"] for column in sa.inspect(connection).get_columns("agent_files")
    }:
        op.add_column(
            "agent_files", sa.Column("encoding", sa.Text(), nullable=False, server_default="plain")
        )
        op.create_check_constraint(
            "ck_agent_files_encoding", "agent_files", "encoding IN ('plain', 'fernet_v1')"
        )
    _rewrite(connection, decrypt=False)


def downgrade() -> None:
    connection = _lock()
    _rewrite(connection, decrypt=True)
    op.drop_constraint("ck_agent_files_encoding", "agent_files", type_="check")
    op.drop_column("agent_files", "encoding")

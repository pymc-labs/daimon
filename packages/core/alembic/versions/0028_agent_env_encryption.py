"""Encrypt existing agent environment values using deployment keys.

downgrade: unsupported
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from daimon.core.config import load_crypto_settings
from daimon.core.github_credentials import build_multifernet, encrypt_token

revision: str = "0028_agent_env_encryption"
down_revision: str | None = "0027_turn_card_intents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()
    files = sa.table(
        "agent_files",
        sa.column("tenant_id", sa.Uuid),
        sa.column("agent_id", sa.Uuid),
        sa.column("key", sa.Text),
        sa.column("content", sa.Text),
    )
    # The migration transaction holds the table lock until all values are encrypted.
    # No timestamps or attribution change; session fingerprints remain stable.
    connection.execute(sa.text("LOCK TABLE agent_files IN ACCESS EXCLUSIVE MODE"))
    rows = connection.execute(sa.select(files)).mappings()
    cipher = None
    for row in rows:
        if cipher is None:
            cipher = build_multifernet(
                tuple(k.get_secret_value() for k in load_crypto_settings().keys)
            )
        connection.execute(
            files.update()
            .where(
                files.c.tenant_id == row["tenant_id"],
                files.c.agent_id == row["agent_id"],
                files.c.key == row["key"],
            )
            .values(content=encrypt_token(cipher, row["content"]).decode("ascii"))
        )


def downgrade() -> None:
    raise NotImplementedError("Restoring plaintext secrets requires an explicit operator procedure")

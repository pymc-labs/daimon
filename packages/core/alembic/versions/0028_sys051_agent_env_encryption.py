"""Encrypt existing agent environment values using deployment keys.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from daimon.core.agent_env_crypto import decode_value, encode_value
from daimon.core.config import load_crypto_settings
from daimon.core.github_credentials import build_multifernet

revision: str = "0028_agent_env_encryption"
down_revision: str | None = "0028_tenant_funding_mode"
branch_labels: str | None = None
depends_on: str | None = None


def _rewrite(*, decrypt: bool) -> None:
    keys = tuple(k.get_secret_value() for k in load_crypto_settings().keys)
    if not keys and not decrypt:
        return
    cipher = build_multifernet(keys) if keys else None
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
    connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(sa.text("LOCK TABLE agent_files IN ACCESS EXCLUSIVE MODE"))
    rows = connection.execute(sa.select(files)).mappings()
    for row in rows:
        original = row["content"]
        value = decode_value(cipher, original) if decrypt else encode_value(cipher, original)
        if value == original:
            continue
        connection.execute(
            files.update()
            .where(
                files.c.tenant_id == row["tenant_id"],
                files.c.agent_id == row["agent_id"],
                files.c.key == row["key"],
            )
            .values(content=value)
        )


def upgrade() -> None:
    _rewrite(decrypt=False)


def downgrade() -> None:
    _rewrite(decrypt=True)

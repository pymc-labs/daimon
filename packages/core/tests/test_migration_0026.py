"""Schema-shape test for migration 0026.

The db_session fixture guarantees Base.metadata.create_all has been applied,
which includes the SlackBotToken ORM model. Asserts: slack_bot_tokens table
exists with PK on team_id, encrypted_token is BYTEA not-null, and
created_at/updated_at columns are present.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_migration_0026_schema_shape(db_session: AsyncSession) -> None:
    """After migration 0026: slack_bot_tokens exists with correct schema."""
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()

    # slack_bot_tokens must exist
    exists = (
        await db_session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = 'slack_bot_tokens'"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert exists == "slack_bot_tokens", "slack_bot_tokens must exist after migration 0026"

    # PK must be on team_id (exactly 1 column)
    # Use a subquery for the schema OID — ":s::regnamespace" conflicts with
    # asyncpg's parameter binding because "::" is interpreted as part of ":s".
    pk_count = (
        await db_session.execute(
            text(
                "SELECT array_length(conkey, 1) FROM pg_constraint "
                "WHERE conname = 'slack_bot_tokens_pkey' "
                "AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = :s)"
            ),
            {"s": schema},
        )
    ).scalar_one_or_none()
    assert pk_count == 1, (
        "slack_bot_tokens PK must have exactly 1 column (team_id) after migration 0026"
    )

    # encrypted_token must be BYTEA not-null
    encrypted_col = (
        await db_session.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'slack_bot_tokens' "
                "AND column_name = 'encrypted_token'"
            ),
            {"s": schema},
        )
    ).one_or_none()
    assert encrypted_col is not None, "encrypted_token column must exist in slack_bot_tokens"
    assert encrypted_col.data_type == "bytea", "encrypted_token must be BYTEA (not plaintext text)"
    assert encrypted_col.is_nullable == "NO", "encrypted_token must be NOT NULL"

    # created_at and updated_at must be present
    for col_name in ("created_at", "updated_at"):
        col = (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'slack_bot_tokens' "
                    "AND column_name = :c"
                ),
                {"s": schema, "c": col_name},
            )
        ).scalar_one_or_none()
        assert col == col_name, (
            f"{col_name} column must exist in slack_bot_tokens after migration 0026"
        )

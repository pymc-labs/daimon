"""Integration tests for slack_bot_tokens store — real Postgres + UPSERT + Fernet round-trip."""

from __future__ import annotations

from cryptography.fernet import Fernet
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.stores.slack_bot_tokens import (
    delete_slack_bot_token,
    get_slack_bot_token,
    upsert_slack_bot_token,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def test_upsert_inserts_then_replaces_on_conflict(
    db_session: AsyncSession,
) -> None:
    first = await upsert_slack_bot_token(
        db_session,
        team_id="T123",
        encrypted_token=b"first-token-bytes",
    )
    assert first.encrypted_token == b"first-token-bytes"

    second = await upsert_slack_bot_token(
        db_session,
        team_id="T123",
        encrypted_token=b"second-token-bytes",
    )
    assert second.encrypted_token == b"second-token-bytes", (
        "UPSERT must replace encrypted_token on conflict"
    )
    assert second.updated_at >= first.updated_at, "UPSERT must bump updated_at"


async def test_get_returns_none_when_absent(db_session: AsyncSession) -> None:
    row = await get_slack_bot_token(db_session, team_id="T_UNKNOWN")
    assert row is None, (
        "get_slack_bot_token must return None for unknown team_id (distinct from 'something broke')"
    )


async def test_get_returns_row_after_upsert(db_session: AsyncSession) -> None:
    await upsert_slack_bot_token(db_session, team_id="T456", encrypted_token=b"tok")
    row = await get_slack_bot_token(db_session, team_id="T456")
    assert row is not None, "row should be retrievable after upsert"
    assert row.encrypted_token == b"tok"


async def test_delete_returns_one_then_zero(db_session: AsyncSession) -> None:
    await upsert_slack_bot_token(db_session, team_id="T789", encrypted_token=b"tok")

    count = await delete_slack_bot_token(db_session, team_id="T789")
    assert count == 1, "delete must return 1 when a row existed"

    count_again = await delete_slack_bot_token(db_session, team_id="T789")
    assert count_again == 0, "delete must return 0 when row is already gone (idempotent)"


async def test_slack_bot_token_fernet_roundtrip(db_session: AsyncSession) -> None:
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    ciphertext = encrypt_token(fernet, "xoxb-EXAMPLE")
    assert ciphertext != b"xoxb-EXAMPLE", "stored bytes must not be plaintext"
    await upsert_slack_bot_token(db_session, team_id="T_FERNET", encrypted_token=ciphertext)
    row = await get_slack_bot_token(db_session, team_id="T_FERNET")
    assert row is not None
    # read-back exercises the asyncpg memoryview→bytes path inside decrypt_token
    assert decrypt_token(fernet, row.encrypted_token) == "xoxb-EXAMPLE", (
        "Fernet round-trip must decrypt to the original plaintext token"
    )

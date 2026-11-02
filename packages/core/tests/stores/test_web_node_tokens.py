from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.stores import web_node_tokens as tokens
from daimon.testing.factories import make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


async def test_a_node_token_is_kept_per_person_and_key_and_replaced_on_save(
    db_session: AsyncSession,
) -> None:
    person = await make_web_person(db_session)
    for enc, at in ((b"old", _NOW), (b"new", _NOW + timedelta(minutes=5))):
        await tokens.upsert_node_token(
            db_session,
            tenant_id=person.tenant_id,
            person_id=person.id,
            token_key="iss#data",
            access_token_enc=enc,
            expires_at=at,
        )

    got = await tokens.get_node_token(db_session, person_id=person.id, token_key="iss#data")
    other = await tokens.get_node_token(db_session, person_id=person.id, token_key="iss#else")

    assert got is not None and bytes(got.access_token_enc) == b"new", "the last save wins"
    assert got.expires_at == _NOW + timedelta(minutes=5), "with its own expiry"
    assert other is None, "another server's key holds nothing"


async def test_a_deleted_node_token_is_gone(db_session: AsyncSession) -> None:
    person = await make_web_person(db_session)
    await tokens.upsert_node_token(
        db_session,
        tenant_id=person.tenant_id,
        person_id=person.id,
        token_key="iss#data",
        access_token_enc=b"x",
        expires_at=_NOW,
    )

    await tokens.delete_node_token(db_session, person_id=person.id, token_key="iss#data")

    assert (
        await tokens.get_node_token(db_session, person_id=person.id, token_key="iss#data") is None
    ), "a token the server stopped taking is dropped"

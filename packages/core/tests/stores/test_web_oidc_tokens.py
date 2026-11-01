"""A person's provider tokens: a later sign-in refreshes them without losing the refresh token."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core.stores import web_oidc_tokens as tokens
from daimon.testing.factories import make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_ISSUER = "https://id.example.com"
_EXPIRES = datetime(2026, 5, 1, tzinfo=UTC)


async def test_a_sign_in_without_a_refresh_token_keeps_the_stored_one(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant)

    async def sign_in(access: bytes, refresh: bytes | None) -> None:
        await tokens.upsert_oidc_token(
            db_session,
            tenant_id=tenant.id,
            person_id=person.id,
            issuer=_ISSUER,
            access_token_enc=access,
            refresh_token_enc=refresh,
            expires_at=_EXPIRES,
        )

    await sign_in(b"access-1", b"refresh-1")
    await sign_in(b"access-2", None)
    kept = await tokens.get_oidc_token(db_session, person_id=person.id, issuer=_ISSUER)
    await sign_in(b"access-3", b"refresh-3")
    rotated = await tokens.get_oidc_token(db_session, person_id=person.id, issuer=_ISSUER)

    assert kept is not None and kept.access_token_enc == b"access-2", "the access token is new"
    assert kept.refresh_token_enc == b"refresh-1", "the stored refresh token is not erased"
    assert rotated is not None and rotated.refresh_token_enc == b"refresh-3", (
        "a new refresh token replaces it"
    )

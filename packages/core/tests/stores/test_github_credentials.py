"""Integration tests for github_credentials store — real Postgres + UPSERT semantics."""

from __future__ import annotations

import uuid

from daimon.core.stores.github_credentials import (
    delete_credential_for_principal,
    get_credential_by_principal,
    get_credential_login_by_principal,
    upsert_credential,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def test_upsert_inserts_then_replaces_on_re_oauth(
    db_session: AsyncSession,
) -> None:
    principal_id = uuid.uuid4()

    first = await upsert_credential(
        db_session,
        principal_id=principal_id,
        github_login="octocat",
        encrypted_token=b"first-token-bytes",
        scopes=("repo",),
    )
    assert first.github_login == "octocat"
    assert first.encrypted_token == b"first-token-bytes"
    assert tuple(first.scopes) == ("repo",)

    second = await upsert_credential(
        db_session,
        principal_id=principal_id,
        github_login="octocat-renamed",
        encrypted_token=b"second-token-bytes",
        scopes=("repo", "read:user", "workflow"),
    )
    assert second.github_login == "octocat-renamed", "UPSERT must replace github_login on re-OAuth"
    assert second.encrypted_token == b"second-token-bytes", (
        "UPSERT must replace encrypted_token on re-OAuth"
    )
    assert tuple(second.scopes) == ("repo", "read:user", "workflow"), (
        "UPSERT must replace scopes on re-OAuth"
    )
    assert second.updated_at >= first.updated_at, "UPSERT must bump updated_at"


async def test_get_credential_by_principal_returns_none_when_absent(
    db_session: AsyncSession,
) -> None:
    row = await get_credential_by_principal(
        db_session,
        principal_id=uuid.uuid4(),
    )
    assert row is None, (
        "get_credential_by_principal must return None for unknown principals "
        "(distinct from 'something broke')"
    )


async def test_get_credential_by_principal_returns_row_after_upsert(
    db_session: AsyncSession,
) -> None:
    principal_id = uuid.uuid4()
    await upsert_credential(
        db_session,
        principal_id=principal_id,
        github_login="octocat",
        encrypted_token=b"tok",
        scopes=("repo",),
    )
    row = await get_credential_by_principal(db_session, principal_id=principal_id)
    assert row is not None, "row should be retrievable after upsert"
    assert row.github_login == "octocat"


async def test_delete_credential_for_principal_returns_one_and_row_is_gone(
    db_session: AsyncSession,
) -> None:
    principal_id = uuid.uuid4()
    await upsert_credential(
        db_session,
        principal_id=principal_id,
        github_login="octocat",
        encrypted_token=b"tok",
        scopes=("repo",),
    )

    rowcount = await delete_credential_for_principal(db_session, principal_id=principal_id)

    assert rowcount == 1, "delete must return 1 when a credential row existed"
    login = await get_credential_login_by_principal(db_session, principal_id=principal_id)
    assert login is None, "credential row must be gone after delete"


async def test_delete_credential_for_principal_returns_zero_when_no_credential(
    db_session: AsyncSession,
) -> None:
    rowcount = await delete_credential_for_principal(db_session, principal_id=uuid.uuid4())
    assert rowcount == 0, "delete on a principal with no credential must return 0 (idempotent)"


async def test_delete_credential_for_principal_leaves_other_principals_untouched(
    db_session: AsyncSession,
) -> None:
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()

    await upsert_credential(
        db_session,
        principal_id=principal_a,
        github_login="user-a",
        encrypted_token=b"tok-a",
        scopes=("repo",),
    )
    await upsert_credential(
        db_session,
        principal_id=principal_b,
        github_login="user-b",
        encrypted_token=b"tok-b",
        scopes=("repo",),
    )

    await delete_credential_for_principal(db_session, principal_id=principal_a)

    login_b = await get_credential_login_by_principal(db_session, principal_id=principal_b)
    assert login_b == "user-b", "principal_b's credential must survive principal_a's delete"
